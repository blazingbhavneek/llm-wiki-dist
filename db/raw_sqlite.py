"""Current raw SQLite backend used by the graph package."""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from graph.models import Edge, Node, NodeStatus, now_iso

from .base import BaseDatabase

_FTS_SPECIAL = re.compile(r'["()*:^]')


def _fts_query(text: str) -> str:
    terms = [t for t in _FTS_SPECIAL.sub(" ", text).split() if t]
    return " OR ".join(f'"{t}"' for t in terms)


class RawSqliteDatabase(BaseDatabase):
    def __init__(
        self,
        path: str | Path = ".wiki/wiki.sqlite",
        readonly: bool = False,
    ) -> None:
        self.path = Path(path)
        self.readonly = readonly

        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)

        # Thread-local storage for sqlite3.Connection.
        #
        # This is the important LangGraph fix:
        # LangGraph ToolNode may execute tools in worker threads, and Python's
        # sqlite3 connections cannot safely be reused across threads unless
        # configured carefully. This gives each thread its own connection.
        self._local = threading.local()

        # Track all opened connections so close() can clean them up.
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.RLock()

        self._dim: int | None = None

        print(
            f"[DB_DEBUG] RawSqliteDatabase object created "
            f"thread={threading.get_ident()} "
            f"path={self.path} "
            f"readonly={self.readonly}",
            flush=True,
        )

        # Initialize DB schema from the creating thread's own connection.
        if not self.readonly:
            self._create_core_tables()

        self._restore_dim()

    @property
    def connection(self) -> sqlite3.Connection:
        """
        Thread-local SQLite connection.

        Existing code can keep using:

            self.connection.execute(...)

        But every Python thread gets a connection created in that same thread.

        This fixes errors like:

            sqlite3.ProgrammingError:
            SQLite objects created in a thread can only be used in that same thread.
        """
        conn = getattr(self._local, "connection", None)

        if conn is None:
            conn = self._open_connection()
            self._local.connection = conn

            with self._connections_lock:
                self._connections.append(conn)

            print(
                f"[DB_DEBUG] SQLite connection opened "
                f"thread={threading.get_ident()} "
                f"path={self.path} "
                f"readonly={self.readonly}",
                flush=True,
            )

        return conn

    def _open_connection(self) -> sqlite3.Connection:
        mode = "ro" if self.readonly else "rwc"
        uri = f"file:{self.path}?mode={mode}"

        conn = sqlite3.connect(
            uri,
            uri=True,
            # Thread-local connection ownership is the main fix.
            # check_same_thread=False gives extra tolerance for cleanup/debug.
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row

        # WAL is persistent and requires write access.
        # Do not attempt to set it on a readonly connection.
        if not self.readonly:
            conn.execute("PRAGMA journal_mode=WAL")

        # Per-connection pragmas.
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")

        # sqlite-vec must be loaded once per SQLite connection.
        self._load_vec_extension(conn)

        return conn

    def _load_vec_extension(self, conn: sqlite3.Connection | None = None) -> None:
        import sqlite_vec

        conn = conn or self.connection
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

    def _create_core_tables(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                body TEXT NOT NULL,
                type TEXT NOT NULL,
                title TEXT,
                original_document_name TEXT,
                source_path TEXT,
                source_ranges_json TEXT NOT NULL,
                source_version TEXT,
                source_material_hash TEXT,
                entity TEXT,
                claims_json TEXT NOT NULL DEFAULT '[]',
                keywords_json TEXT NOT NULL,
                summary TEXT,
                cluster TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_nodes_type
                ON nodes(type);

            CREATE INDEX IF NOT EXISTS idx_nodes_status
                ON nodes(status);

            CREATE INDEX IF NOT EXISTS idx_nodes_doc
                ON nodes(original_document_name);

            CREATE TABLE IF NOT EXISTS edges (
                id TEXT PRIMARY KEY,
                source_node_id TEXT NOT NULL,
                target_node_id TEXT NOT NULL,
                label TEXT NOT NULL,
                summary TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_edges_source
                ON edges(source_node_id);

            CREATE INDEX IF NOT EXISTS idx_edges_target
                ON edges(target_node_id);

            CREATE TABLE IF NOT EXISTS sources (
                document_name TEXT PRIMARY KEY,
                source_hash TEXT NOT NULL,
                ingested_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_versions (
                document_name TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                PRIMARY KEY(document_name, source_hash)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
                node_id UNINDEXED,
                text
            );

            CREATE TABLE IF NOT EXISTS search_items (
                id TEXT PRIMARY KEY,
                node_id TEXT NOT NULL,
                field TEXT NOT NULL,
                text TEXT NOT NULL,
                ordinal INTEGER NOT NULL DEFAULT 0,
                start_char INTEGER,
                end_char INTEGER,
                source_path TEXT,
                source_hash TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_search_items_node
                ON search_items(node_id);

            CREATE VIRTUAL TABLE IF NOT EXISTS search_items_fts USING fts5(
                item_id UNINDEXED,
                node_id UNINDEXED,
                field UNINDEXED,
                text
            );
            """
        )
        self._ensure_node_columns()
        self._ensure_edge_columns()
        self.connection.commit()

    def _ensure_node_columns(self) -> None:
        existing = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(nodes)").fetchall()
        }

        additions = {
            "source_version": "TEXT",
            "source_material_hash": "TEXT",
            "entity": "TEXT",
            "claims_json": "TEXT NOT NULL DEFAULT '[]'",
        }

        for column, ddl in additions.items():
            if column not in existing:
                self.connection.execute(f"ALTER TABLE nodes ADD COLUMN {column} {ddl}")

        self.connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_nodes_source_version
                ON nodes(source_version);

            CREATE INDEX IF NOT EXISTS idx_nodes_source_material_hash
                ON nodes(source_material_hash);

            CREATE INDEX IF NOT EXISTS idx_nodes_entity
                ON nodes(entity);
            """
        )

    def _ensure_edge_columns(self) -> None:
        existing = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(edges)").fetchall()
        }

        additions = {
            "valid_at": "TEXT",
            "invalid_at": "TEXT",
            "expired_at": "TEXT",
            "source_episode_ids_json": "TEXT NOT NULL DEFAULT '[]'",
        }

        for column, ddl in additions.items():
            if column not in existing:
                self.connection.execute(f"ALTER TABLE edges ADD COLUMN {column} {ddl}")

    def _restore_dim(self) -> None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = 'embed_dim'"
        ).fetchone()

        if row:
            self._dim = int(row["value"])

    def ensure_vec_tables(self, dim: int) -> None:
        if self.readonly:
            raise RuntimeError("cannot ensure vector tables on readonly database")

        if self._dim is not None and self._dim != dim:
            raise ValueError(
                f"embedding dim mismatch: db built for {self._dim}, got {dim}"
            )

        # Idempotent CREATE IF NOT EXISTS so a DB built before vec_search_item
        # existed still gains the table on the next startup.
        for table in ("vec_body", "vec_summary", "vec_search_item"):
            self.connection.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} "
                f"USING vec0(node_id TEXT PRIMARY KEY, embedding float[{dim}])"
            )

        if self._dim is None:
            self.connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('embed_dim', ?)",
                (str(dim),),
            )
            self._dim = dim

        self.connection.commit()

    def reset_vec_tables(self) -> None:
        """Drop the vector tables and forget the stored dim.

        Used when the embedding model changes: stored vectors are no longer
        comparable, so they are wiped and rebuilt by re-embedding every node.
        """
        if self.readonly:
            raise RuntimeError("cannot reset vector tables on readonly database")

        self.connection.execute("DROP TABLE IF EXISTS vec_body")
        self.connection.execute("DROP TABLE IF EXISTS vec_summary")
        self.connection.execute("DROP TABLE IF EXISTS vec_search_item")
        self.connection.execute("DELETE FROM meta WHERE key = 'embed_dim'")
        self.connection.commit()
        self._dim = None

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?",
            (key,),
        ).fetchone()

        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        if self.readonly:
            raise RuntimeError("cannot set metadata on readonly database")

        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (key, value),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection

        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self) -> None:
        """
        Close every SQLite connection opened by this RawSqliteDatabase object.

        Because connections are opened lazily per thread, there may be more
        than 1 connection.
        """
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()

        for conn in connections:
            try:
                conn.close()
            except Exception:
                pass

    def upsert_node(self, node: Node) -> None:
        if self.readonly:
            raise RuntimeError("cannot upsert node on readonly database")

        import json

        existing = self.get_node(node.id)

        if existing:
            node.created_at = existing.created_at

        node.updated_at = now_iso()

        self.connection.execute(
            """
            INSERT INTO nodes (
                id,
                body,
                type,
                title,
                original_document_name,
                source_path,
                source_ranges_json,
                source_version,
                source_material_hash,
                entity,
                claims_json,
                keywords_json,
                summary,
                cluster,
                status,
                created_at,
                updated_at
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                body=excluded.body,
                type=excluded.type,
                title=excluded.title,
                original_document_name=excluded.original_document_name,
                source_path=excluded.source_path,
                source_ranges_json=excluded.source_ranges_json,
                source_version=excluded.source_version,
                source_material_hash=excluded.source_material_hash,
                entity=excluded.entity,
                claims_json=excluded.claims_json,
                keywords_json=excluded.keywords_json,
                summary=excluded.summary,
                cluster=excluded.cluster,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                node.id,
                node.body,
                node.type.value,
                node.title,
                node.original_document_name,
                node.source_path,
                json.dumps(node.source_ranges),
                node.source_version,
                node.source_material_hash,
                node.entity,
                json.dumps(node.claims),
                json.dumps(node.keywords),
                node.summary,
                node.cluster,
                node.status.value,
                node.created_at,
                node.updated_at,
            ),
        )

        self._reindex_fts(node)
        self.connection.commit()

    def get_node(self, node_id: str) -> Node | None:
        row = self.connection.execute(
            "SELECT * FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()

        return _row_to_node(row) if row else None

    def set_node_status(self, node_id: str, status: NodeStatus) -> None:
        if self.readonly:
            raise RuntimeError("cannot set node status on readonly database")

        self.connection.execute(
            "UPDATE nodes SET status=?, updated_at=? WHERE id=?",
            (status.value, now_iso(), node_id),
        )
        self.connection.commit()

    def delete_node(self, node_id: str) -> None:
        if self.readonly:
            raise RuntimeError("cannot delete node on readonly database")

        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM edges WHERE source_node_id=? OR target_node_id=?",
                (node_id, node_id),
            )

            conn.execute(
                "DELETE FROM nodes WHERE id=?",
                (node_id,),
            )

            conn.execute(
                "DELETE FROM nodes_fts WHERE node_id=?",
                (node_id,),
            )

            item_ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM search_items WHERE node_id=?",
                    (node_id,),
                ).fetchall()
            ]

            conn.execute(
                "DELETE FROM search_items_fts WHERE node_id=?",
                (node_id,),
            )

            conn.execute(
                "DELETE FROM search_items WHERE node_id=?",
                (node_id,),
            )

            if self._dim is not None:
                conn.execute(
                    "DELETE FROM vec_body WHERE node_id=?",
                    (node_id,),
                )

                conn.execute(
                    "DELETE FROM vec_summary WHERE node_id=?",
                    (node_id,),
                )

                for item_id in item_ids:
                    conn.execute(
                        "DELETE FROM vec_search_item WHERE node_id=?",
                        (item_id,),
                    )

    def get_all_nodes(self, include_deleted: bool = False) -> list[Node]:
        sql = "SELECT * FROM nodes"

        if not include_deleted:
            sql += " WHERE status != 'deleted'"

        sql += " ORDER BY updated_at DESC"

        return [_row_to_node(r) for r in self.connection.execute(sql).fetchall()]

    def get_nodes_by_document(
        self,
        document_name: str,
        active_only: bool = False,
    ) -> list[Node]:
        sql = "SELECT * FROM nodes WHERE original_document_name=?"
        params: list[str] = [document_name]

        if active_only:
            sql += " AND status='active'"

        sql += " ORDER BY updated_at DESC"

        return [
            _row_to_node(r)
            for r in self.connection.execute(sql, params).fetchall()
        ]

    def upsert_edge(self, edge: Edge) -> None:
        if self.readonly:
            raise RuntimeError("cannot upsert edge on readonly database")

        import json

        self.connection.execute(
            """
            INSERT INTO edges (
                id,
                source_node_id,
                target_node_id,
                label,
                summary,
                created_at,
                valid_at,
                invalid_at,
                expired_at,
                source_episode_ids_json
            )
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                label=excluded.label,
                summary=excluded.summary,
                valid_at=excluded.valid_at,
                invalid_at=excluded.invalid_at,
                expired_at=excluded.expired_at,
                source_episode_ids_json=excluded.source_episode_ids_json
            """,
            (
                edge.id,
                edge.source_node_id,
                edge.target_node_id,
                edge.label,
                edge.summary,
                edge.created_at,
                edge.valid_at,
                edge.invalid_at,
                edge.expired_at,
                json.dumps(edge.source_episode_ids),
            ),
        )

        self.connection.commit()

    def get_all_edges(self) -> list[Edge]:
        rows = self.connection.execute(
            "SELECT * FROM edges ORDER BY created_at DESC"
        ).fetchall()

        return [_row_to_edge(r) for r in rows]

    def get_edges_for_node(self, node_id: str) -> list[Edge]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM edges
            WHERE source_node_id=? OR target_node_id=?
            ORDER BY created_at DESC
            """,
            (node_id, node_id),
        ).fetchall()

        return [_row_to_edge(r) for r in rows]

    def get_outgoing_edges(
        self,
        node_id: str,
        label: str | None = None,
    ) -> list[Edge]:
        sql = "SELECT * FROM edges WHERE source_node_id=?"
        params: list[str] = [node_id]

        if label is not None:
            sql += " AND label=?"
            params.append(label)

        sql += " ORDER BY created_at DESC"

        return [
            _row_to_edge(r)
            for r in self.connection.execute(sql, params).fetchall()
        ]

    def get_incoming_edges(
        self,
        node_id: str,
        label: str | None = None,
    ) -> list[Edge]:
        sql = "SELECT * FROM edges WHERE target_node_id=?"
        params: list[str] = [node_id]

        if label is not None:
            sql += " AND label=?"
            params.append(label)

        sql += " ORDER BY created_at DESC"

        return [
            _row_to_edge(r)
            for r in self.connection.execute(sql, params).fetchall()
        ]

    def delete_edge(self, edge_id: str) -> None:
        if self.readonly:
            raise RuntimeError("cannot delete edge on readonly database")

        self.connection.execute(
            "DELETE FROM edges WHERE id=?",
            (edge_id,),
        )
        self.connection.commit()

    def delete_edges_by_label_for_nodes(
        self,
        label: str,
        node_ids: set[str],
    ) -> None:
        if self.readonly:
            raise RuntimeError("cannot delete edges on readonly database")

        if not node_ids:
            return

        placeholders = ",".join("?" for _ in node_ids)
        params = [label, *node_ids, *node_ids]

        self.connection.execute(
            f"""
            DELETE FROM edges
            WHERE label=?
              AND source_node_id IN ({placeholders})
              AND target_node_id IN ({placeholders})
            """,
            params,
        )

        self.connection.commit()

    def record_source(self, document_name: str, source_hash: str) -> None:
        if self.readonly:
            raise RuntimeError("cannot record source on readonly database")

        stamp = now_iso()

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sources(
                    document_name,
                    source_hash,
                    ingested_at
                )
                VALUES(?,?,?)
                """,
                (document_name, source_hash, stamp),
            )

            conn.execute(
                """
                INSERT OR IGNORE INTO source_versions(
                    document_name,
                    source_hash,
                    ingested_at
                )
                VALUES(?,?,?)
                """,
                (document_name, source_hash, stamp),
            )

    def get_source(self, document_name: str) -> tuple[str, str] | None:
        row = self.connection.execute(
            """
            SELECT source_hash, ingested_at
            FROM sources
            WHERE document_name=?
            """,
            (document_name,),
        ).fetchone()

        return (row["source_hash"], row["ingested_at"]) if row else None

    def _reindex_fts(self, node: Node) -> None:
        self.connection.execute(
            "DELETE FROM nodes_fts WHERE node_id=?",
            (node.id,),
        )

        if node.status == NodeStatus.deleted:
            return

        text = " ".join(
            filter(
                None,
                [
                    node.title,
                    node.summary,
                    node.body,
                    " ".join(node.keywords),
                ],
            )
        )

        self.connection.execute(
            "INSERT INTO nodes_fts(node_id, text) VALUES(?, ?)",
            (node.id, text),
        )

    def keyword_search(self, text: str, limit: int = 20) -> list[Node]:
        query = _fts_query(text)

        if not query:
            return []

        rows = self.connection.execute(
            """
            SELECT n.*
            FROM nodes_fts f
            JOIN nodes n ON n.id = f.node_id
            WHERE nodes_fts MATCH ?
              AND n.status = 'active'
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()

        return [_row_to_node(r) for r in rows]

    def set_vector(self, node_id: str, table: str, vector: list[float]) -> None:
        if self.readonly:
            raise RuntimeError("cannot set vector on readonly database")

        import sqlite_vec

        if self._dim is None:
            raise RuntimeError("ensure_vec_tables() must run before set_vector()")

        blob = sqlite_vec.serialize_float32(vector)

        self.connection.execute(
            f"DELETE FROM {table} WHERE node_id=?",
            (node_id,),
        )

        self.connection.execute(
            f"INSERT INTO {table}(node_id, embedding) VALUES(?, ?)",
            (node_id, blob),
        )

        self.connection.commit()

    def count_vectors(self, table: str = "vec_body") -> int:
        """Number of stored vectors in a table.

        Returns 0 if vectors are not set up yet.

        Used at startup to detect a half-finished re-embed: when this is less
        than the active node count, coverage is incomplete and all vectors are
        rebuilt.
        """
        if self._dim is None:
            return 0

        try:
            row = self.connection.execute(
                f"SELECT COUNT(*) AS n FROM {table}"
            ).fetchone()
        except sqlite3.OperationalError:
            return 0

        return int(row["n"]) if row else 0

    def has_vector(self, node_id: str, table: str = "vec_body") -> bool:
        if self._dim is None:
            return False

        row = self.connection.execute(
            f"SELECT 1 FROM {table} WHERE node_id=? LIMIT 1",
            (node_id,),
        ).fetchone()

        return row is not None

    def get_vector(
        self,
        node_id: str,
        table: str = "vec_body",
    ) -> list[float] | None:
        import struct

        if self._dim is None:
            return None

        row = self.connection.execute(
            f"SELECT embedding FROM {table} WHERE node_id=?",
            (node_id,),
        ).fetchone()

        if row is None:
            return None

        return list(struct.unpack(f"{self._dim}f", row["embedding"]))

    def vector_search(
        self,
        vector: list[float],
        table: str = "vec_body",
        limit: int = 20,
    ) -> list[tuple[str, float]]:
        import sqlite_vec

        if self._dim is None:
            return []

        blob = sqlite_vec.serialize_float32(vector)

        if table == "vec_search_item":
            # PK column stores search_items.id; resolve to node to filter active
            # and return (item_id, distance).
            rows = self.connection.execute(
                f"""
                WITH matches AS (
                    SELECT node_id AS item_id, distance
                    FROM {table}
                    WHERE embedding MATCH ?
                    ORDER BY distance
                    LIMIT ?
                )
                SELECT
                    m.item_id AS item_id,
                    m.distance AS distance
                FROM matches m
                JOIN search_items s ON s.id = m.item_id
                JOIN nodes n ON n.id = s.node_id
                WHERE n.status = 'active'
                ORDER BY m.distance
                """,
                (blob, limit),
            ).fetchall()

            return [(r["item_id"], r["distance"]) for r in rows]

        rows = self.connection.execute(
            f"""
            WITH matches AS (
                SELECT node_id, distance
                FROM {table}
                WHERE embedding MATCH ?
                ORDER BY distance
                LIMIT ?
            )
            SELECT
                m.node_id AS node_id,
                m.distance AS distance
            FROM matches m
            JOIN nodes n ON n.id = m.node_id
            WHERE n.status = 'active'
            ORDER BY m.distance
            """,
            (blob, limit),
        ).fetchall()

        return [(r["node_id"], r["distance"]) for r in rows]

    # --- evidence-first search items -----------------------------------------

    def replace_search_items(self, node_id: str, items: list[dict]) -> None:
        """Delete then re-insert every search_items row, FTS row, and stale vector.

        Vectors for the new rows are set separately.
        """
        if self.readonly:
            raise RuntimeError("cannot replace search items on readonly database")

        with self.transaction() as conn:
            self._delete_search_items_conn(conn, node_id)

            for item in items:
                conn.execute(
                    """
                    INSERT INTO search_items (
                        id,
                        node_id,
                        field,
                        text,
                        ordinal,
                        start_char,
                        end_char,
                        source_path,
                        source_hash
                    )
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        item["id"],
                        node_id,
                        item["field"],
                        item["text"],
                        item.get("ordinal", 0),
                        item.get("start_char"),
                        item.get("end_char"),
                        item.get("source_path"),
                        item.get("source_hash"),
                    ),
                )

                conn.execute(
                    """
                    INSERT INTO search_items_fts(
                        item_id,
                        node_id,
                        field,
                        text
                    )
                    VALUES(?,?,?,?)
                    """,
                    (
                        item["id"],
                        node_id,
                        item["field"],
                        item["text"],
                    ),
                )

    def delete_search_items(self, node_id: str) -> None:
        if self.readonly:
            raise RuntimeError("cannot delete search items on readonly database")

        with self.transaction() as conn:
            self._delete_search_items_conn(conn, node_id)

    def _delete_search_items_conn(
        self,
        conn: sqlite3.Connection,
        node_id: str,
    ) -> None:
        item_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM search_items WHERE node_id=?",
                (node_id,),
            ).fetchall()
        ]

        conn.execute(
            "DELETE FROM search_items_fts WHERE node_id=?",
            (node_id,),
        )

        conn.execute(
            "DELETE FROM search_items WHERE node_id=?",
            (node_id,),
        )

        if self._dim is not None:
            for item_id in item_ids:
                conn.execute(
                    "DELETE FROM vec_search_item WHERE node_id=?",
                    (item_id,),
                )

    def set_search_item_vector(
        self,
        item_id: str,
        vector: list[float],
    ) -> None:
        if self.readonly:
            raise RuntimeError("cannot set search item vector on readonly database")

        import sqlite_vec

        if self._dim is None:
            raise RuntimeError(
                "ensure_vec_tables() must run before set_search_item_vector()"
            )

        blob = sqlite_vec.serialize_float32(vector)

        self.connection.execute(
            "DELETE FROM vec_search_item WHERE node_id=?",
            (item_id,),
        )

        self.connection.execute(
            "INSERT INTO vec_search_item(node_id, embedding) VALUES(?, ?)",
            (item_id, blob),
        )

        self.connection.commit()

    def search_items_fts_query(
        self,
        text: str,
        limit: int = 150,
    ) -> list[dict]:
        query = _fts_query(text)

        if not query:
            return []

        rows = self.connection.execute(
            """
            SELECT
                s.id AS item_id,
                s.node_id AS node_id,
                s.field AS field,
                s.text AS text,
                s.ordinal AS ordinal,
                s.start_char AS start_char,
                s.end_char AS end_char
            FROM search_items_fts f
            JOIN search_items s ON s.id = f.item_id
            JOIN nodes n ON n.id = s.node_id
            WHERE search_items_fts MATCH ?
              AND n.status = 'active'
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()

        return [dict(r) for r in rows]

    def get_search_items(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}

        placeholders = ",".join("?" for _ in ids)

        rows = self.connection.execute(
            f"""
            SELECT
                id AS item_id,
                node_id,
                field,
                text,
                ordinal,
                start_char,
                end_char
            FROM search_items
            WHERE id IN ({placeholders})
            """,
            list(ids),
        ).fetchall()

        return {r["item_id"]: dict(r) for r in rows}


def _row_to_node(row: sqlite3.Row) -> Node:
    import json

    return Node(
        id=row["id"],
        body=row["body"],
        type=row["type"],
        title=row["title"] or "",
        original_document_name=row["original_document_name"],
        source_path=row["source_path"],
        source_ranges=[
            tuple(r)
            for r in json.loads(row["source_ranges_json"] or "[]")
        ],
        source_version=row["source_version"],
        source_material_hash=row["source_material_hash"],
        entity=row["entity"] or "",
        claims=json.loads(row["claims_json"] or "[]"),
        keywords=json.loads(row["keywords_json"] or "[]"),
        summary=row["summary"] or "",
        cluster=row["cluster"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_edge(row: sqlite3.Row) -> Edge:
    import json

    keys = row.keys()

    episodes_raw = (
        row["source_episode_ids_json"]
        if "source_episode_ids_json" in keys
        else "[]"
    )

    return Edge(
        id=row["id"],
        source_node_id=row["source_node_id"],
        target_node_id=row["target_node_id"],
        label=row["label"],
        summary=row["summary"] or "",
        created_at=row["created_at"],
        valid_at=row["valid_at"] if "valid_at" in keys else None,
        invalid_at=row["invalid_at"] if "invalid_at" in keys else None,
        expired_at=row["expired_at"] if "expired_at" in keys else None,
        source_episode_ids=json.loads(episodes_raw or "[]"),
    )


Database = RawSqliteDatabase
