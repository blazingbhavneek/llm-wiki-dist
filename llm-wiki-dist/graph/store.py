# region Imports

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .core import Edge, Node, NodeStatus, now_iso

# endregion Imports

# region Global Vars/ Helpers

_FTS_SPECIAL = re.compile(r'["()*:^]')
log = logging.getLogger("raw_sqlite")


# Takes a text string, cleans special FTS characters, splits into words, and joins them with OR for SQLite full-text search.
def _fts_query(text: str) -> str:
    terms = [t for t in _FTS_SPECIAL.sub(" ", text).split() if t]
    return " OR ".join(f'"{t}"' for t in terms)


# Converts one SQLite row into a Node object
def _row_to_node(row: sqlite3.Row) -> Node:
    import json

    return Node(
        id=row["id"],
        body=row["body"],
        type=row["type"],
        title=row["title"] or "",
        original_document_name=row["original_document_name"],
        source_path=row["source_path"],
        source_ranges=[tuple(r) for r in json.loads(row["source_ranges_json"] or "[]")],
        source_version=row["source_version"],
        source_material_hash=row["source_material_hash"],
        entity=row["entity"] or "",
        claims=json.loads(row["claims_json"] or "[]"),
        keywords=json.loads(row["keywords_json"] or "[]"),
        summary=row["summary"] or "",
        cluster=row["cluster"],
        bridge_probe=(row["bridge_probe"] or "") if "bridge_probe" in row.keys() else "",
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# Converts one SQLite row into a Node object
def _row_to_edge(row: sqlite3.Row) -> Edge:
    import json

    keys = row.keys()

    episodes_raw = (
        row["source_episode_ids_json"] if "source_episode_ids_json" in keys else "[]"
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


# endregion Global Vars/ Helpers

# DB API


class GraphStore:

    # region Init

    def __init__(
        self,
        path: str | Path = ".wiki/wiki.sqlite",  # Path of the sqlite file
        readonly: bool = False,  # Either for a researcher (read only, can have many "researchers" access at the same time, or librarian (can write) so 1 change at a time)
    ) -> None:

        self.path = Path(path)
        self.readonly = readonly

        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)

        # Thread-local storage for sqlite3.Connection.
        # LangGraph ToolNode may execute tools in worker threads, and Python's
        # sqlite3 connections cannot safely be reused across threads, this gives each thread its own connection.
        # The reason why we need this so that same Graphstore object can be shared across different threads (reader agent, write queues etc) and it would internally manage
        # everything, for each worker it would create this local storage (kind of like a locker where each thread can put its local connections/objects here) and share some global configs
        self._local = threading.local()

        # Track all opened connections so close() can clean them up.
        self._connections: list[sqlite3.Connection] = []

        # Prevents race conditions when multiple threads add/close connections
        self._connections_lock = threading.RLock()

        self._dim: int | None = None

        log.debug(
            "database.created thread=%s path=%s readonly=%s",
            threading.get_ident(),
            self.path,
            self.readonly,
        )

        # Initialize DB schema from the creating thread's own connection.
        if not self.readonly:
            self._create_core_tables()

        self._restore_dim()

    # Connection to DB for the CURRENT thread.
    # During __init__, this is usually the main/creating thread.
    # In worker threads, this creates/returns that worker thread's own connection.
    @property
    def connection(self) -> sqlite3.Connection:

        # Check if this thread has its connection made
        conn = getattr(self._local, "connection", None)

        # if not, then open a connection and store in thread-local storage
        if conn is None:
            conn = self._open_connection()
            self._local.connection = conn

            # lock the list and append to it that this connection has started
            with self._connections_lock:
                self._connections.append(conn)

            log.debug(
                "connection.opened thread=%s path=%s readonly=%s",
                threading.get_ident(),
                self.path,
                self.readonly,
            )

        return conn

    # Open a new SQLite connection for the current thread
    def _open_connection(self) -> sqlite3.Connection:
        # SQLite URI mode:
        # "ro"  = read-only: open existing DB, cannot write
        # "rwc" = read-write-create: open DB for reading/writing, create if missing
        mode = "ro" if self.readonly else "rwc"

        # Build SQLite URI path, e.g.
        # file:.wiki/wiki.sqlite?mode=rwc
        uri = f"file:{self.path}?mode={mode}"

        conn = sqlite3.connect(
            uri,
            uri=True,  # Tells sqlite3 that `uri` is a SQLite URI, not a plain file path
            # Normally SQLite connections can only be used in the thread that created them.
            # Your code already uses thread-local connections, so each thread gets its own.
            #
            # check_same_thread=False relaxes Python's safety check.
            # It is mainly useful here for cleanup/debug situations.
            check_same_thread=False,
        )

        # Makes query results behave like dictionaries:
        #
        # row["id"]
        #
        # instead of only tuple indexes:
        #
        # row[0]
        conn.row_factory = sqlite3.Row

        # WAL = Write-Ahead Logging.
        #
        # Default SQLite rollback journal can block readers during writes more often.
        # WAL improves concurrency:
        #
        # - readers can keep reading while another connection writes
        # - better for multi-thread / multi-connection apps
        #
        # WAL changes database journal mode and needs write access,
        # so do NOT run this in readonly mode.
        if not self.readonly:
            conn.execute("PRAGMA journal_mode=WAL")

        # Wait up to 5000 ms = 5 seconds if the database is locked.
        #
        # Without this, SQLite may immediately fail with:
        # "database is locked"
        #
        # Useful when another thread/process is briefly writing.
        conn.execute("PRAGMA busy_timeout=5000")

        # Controls how safely SQLite flushes data to disk.
        #
        # FULL   = safest, slower
        # NORMAL = good balance, faster, commonly used with WAL
        # OFF    = fastest, least safe
        #
        # NORMAL is usually fine for app/local DB usage.
        conn.execute("PRAGMA synchronous=NORMAL")

        # Enforces foreign key constraints.
        #
        # SQLite supports foreign keys, but they are OFF by default per connection.
        # This makes sure relations like edges -> nodes are checked if FK constraints exist.
        conn.execute("PRAGMA foreign_keys=ON")

        # sqlite-vec is a SQLite extension for vector search.
        #
        # Extensions are loaded per connection, not once globally.
        # Since every thread has its own connection, every connection must load it.
        self._load_vec_extension(conn)

        return conn

    def _load_vec_extension(self, conn: sqlite3.Connection | None = None) -> None:
        # sqlite-vec is a SQLite extension used for vector search / embeddings.
        # It must be loaded separately for each SQLite connection.
        import sqlite_vec

        # Use the provided connection if given.
        # Otherwise, use this thread's connection via self.connection.
        conn = conn or self.connection

        # Temporarily allow SQLite extensions to be loaded.
        # This is disabled by default for safety.
        conn.enable_load_extension(True)

        # Load sqlite-vec into this specific SQLite connection.
        sqlite_vec.load(conn)

        # Disable extension loading again after loading sqlite-vec.
        conn.enable_load_extension(False)

    def _create_core_tables(self) -> None:
        # Create the main database schema if it does not already exist.
        # executescript lets us run many SQL statements at once.
        self.connection.executescript("""
            -- Small key/value table for DB-level settings.
            -- Example: storing embedding dimension.
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            -- Main table for graph nodes.
            -- Stores the node text, metadata, claims, keywords, status, timestamps, etc.
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

            -- Indexes make common node lookups faster.
            CREATE INDEX IF NOT EXISTS idx_nodes_type
                ON nodes(type);

            CREATE INDEX IF NOT EXISTS idx_nodes_status
                ON nodes(status);

            CREATE INDEX IF NOT EXISTS idx_nodes_doc
                ON nodes(original_document_name);

            -- Main table for graph edges.
            -- Each edge connects one source node to one target node.
            CREATE TABLE IF NOT EXISTS edges (
                id TEXT PRIMARY KEY,
                source_node_id TEXT NOT NULL,
                target_node_id TEXT NOT NULL,
                label TEXT NOT NULL,
                summary TEXT,
                created_at TEXT NOT NULL
            );

            -- Indexes make graph traversal faster.
            -- Example: finding all outgoing or incoming edges for a node.
            CREATE INDEX IF NOT EXISTS idx_edges_source
                ON edges(source_node_id);

            CREATE INDEX IF NOT EXISTS idx_edges_target
                ON edges(target_node_id);

            -- Tracks the latest ingested version/hash of each source document.
            CREATE TABLE IF NOT EXISTS sources (
                document_name TEXT PRIMARY KEY,
                source_hash TEXT NOT NULL,
                ingested_at TEXT NOT NULL
            );

            -- Keeps history of all ingested versions of each document.
            CREATE TABLE IF NOT EXISTS source_versions (
                document_name TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                PRIMARY KEY(document_name, source_hash)
            );

            -- Full-text search table for node-level searching.
            -- FTS5 is SQLite's built-in full-text search engine.
            -- node_id is UNINDEXED because we search text, not node_id.
            CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
                node_id UNINDEXED,
                text
            );

            -- Searchable sub-items of nodes.
            -- Useful for searching specific fields/chunks with source positions.
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

            -- Speeds up lookup of search items belonging to a node.
            CREATE INDEX IF NOT EXISTS idx_search_items_node
                ON search_items(node_id);

            -- Full-text search table for search_items.
            -- item_id/node_id/field are stored but not full-text indexed.
            -- The actual searchable content is text.
            CREATE VIRTUAL TABLE IF NOT EXISTS search_items_fts USING fts5(
                item_id UNINDEXED,
                node_id UNINDEXED,
                field UNINDEXED,
                text
            );
            """)

        # Add missing columns for older databases if the schema changed over time.
        self._ensure_node_columns()
        self._ensure_edge_columns()

        # Save schema changes.
        self._commit()

    def _ensure_node_columns(self) -> None:
        # Read the current columns in the nodes table.
        # PRAGMA table_info(nodes) returns metadata for each column.
        existing = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(nodes)").fetchall()
        }

        # Columns that newer versions of the app expect to exist.
        # This lets older databases be upgraded without deleting/recreating tables.
        additions = {
            "source_version": "TEXT",
            "source_material_hash": "TEXT",
            "entity": "TEXT",
            "claims_json": "TEXT NOT NULL DEFAULT '[]'",
            "bridge_probe": "TEXT",
        }

        # Add any missing columns to the existing nodes table.
        # ALTER TABLE ADD COLUMN is used for lightweight schema migration.
        for column, ddl in additions.items():
            if column not in existing:
                self.connection.execute(f"ALTER TABLE nodes ADD COLUMN {column} {ddl}")

        # Create indexes for the added columns.
        # Indexes make filtering/searching by these fields faster.
        self.connection.executescript("""
            CREATE INDEX IF NOT EXISTS idx_nodes_source_version
                ON nodes(source_version);

            CREATE INDEX IF NOT EXISTS idx_nodes_source_material_hash
                ON nodes(source_material_hash);

            CREATE INDEX IF NOT EXISTS idx_nodes_entity
                ON nodes(entity);
            """)

    def _ensure_edge_columns(self) -> None:
        # Read the current columns in the edges table.
        # Used to check whether this DB already has the newer schema.
        existing = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(edges)").fetchall()
        }

        # Columns that newer edge records may need.
        # valid_at / invalid_at / expired_at support temporal edge validity.
        # source_episode_ids_json stores related source episode IDs as JSON text.
        additions = {
            "valid_at": "TEXT",
            "invalid_at": "TEXT",
            "expired_at": "TEXT",
            "source_episode_ids_json": "TEXT NOT NULL DEFAULT '[]'",
        }

        # Add missing columns only.
        # This avoids errors if the database was already upgraded before.
        for column, ddl in additions.items():
            if column not in existing:
                self.connection.execute(f"ALTER TABLE edges ADD COLUMN {column} {ddl}")

    def _restore_dim(self) -> None:
        # Read the stored embedding dimension from the meta table, if it exists.
        # This tells us what vector size the database was originally built with.
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = 'embed_dim'"
        ).fetchone()

        # If the value exists, cache it in memory as an integer.
        if row:
            self._dim = int(row["value"])

    def ensure_vec_tables(self, dim: int) -> None:
        # Vector tables require creating/modifying DB schema,
        # so this cannot run on a readonly database.
        if self.readonly:
            raise RuntimeError("cannot ensure vector tables on readonly database")

        # If the DB already has an embedding dimension, it must match the new one.
        # Example: a DB built for 1536-dim embeddings cannot safely store 768-dim vectors.
        if self._dim is not None and self._dim != dim:
            raise ValueError(
                f"embedding dim mismatch: db built for {self._dim}, got {dim}"
            )

        # Create vector search tables if they do not already exist.
        # vec0 comes from sqlite-vec.
        #
        # Each table stores:
        # - node_id: links the vector back to a node/search item
        # - embedding: fixed-size float vector, e.g. float[1536]
        #
        # CREATE IF NOT EXISTS makes this safe to run multiple times.
        # Virtual tables are special SQLite tables powered by extensions/modules.
        # We use them here for features normal tables do not provide efficiently,
        # such as full-text search with FTS5 or vector search with sqlite-vec.
        for table in ("vec_body", "vec_summary", "vec_bridge", "vec_search_item"):
            self.connection.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} "
                f"USING vec0(node_id TEXT PRIMARY KEY, embedding float[{dim}])"
            )

        # If this is the first time vector tables are being initialized,
        # save the embedding dimension in meta so future startups can verify it.
        if self._dim is None:
            self.connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('embed_dim', ?)",
                (str(dim),),
            )
            self._dim = dim

        # Persist schema/meta changes.
        self._commit()

    def reset_vec_tables(self) -> None:

        # Vector tables are part of the writable DB schema,
        # so resetting them is not allowed in readonly mode.
        if self.readonly:
            raise RuntimeError("cannot reset vector tables on readonly database")

        # Drop all vector-search tables if they exist.
        # These are recreated later by ensure_vec_tables(...).
        self.connection.execute("DROP TABLE IF EXISTS vec_body")
        self.connection.execute("DROP TABLE IF EXISTS vec_summary")
        self.connection.execute("DROP TABLE IF EXISTS vec_bridge")
        self.connection.execute("DROP TABLE IF EXISTS vec_search_item")

        # Remove the saved embedding dimension from metadata.
        # A new dimension will be stored when vector tables are recreated.
        self.connection.execute("DELETE FROM meta WHERE key = 'embed_dim'")

        # Persist the table drops and metadata deletion.
        self._commit()

        # Also clear the cached in-memory dimension.
        self._dim = None

    def get_meta(self, key: str) -> str | None:
        # Look up a single metadata value by key.
        # The meta table is used as a small key/value store for DB settings.
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?",
            (key,),
        ).fetchone()

        # Return the stored value if found, otherwise return None.
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        # Metadata changes write to the database,
        # so they are not allowed in readonly mode.
        if self.readonly:
            raise RuntimeError("cannot set metadata on readonly database")

        # Use a transaction so the metadata update is committed safely.
        # INSERT OR REPLACE means: insert new key, or update existing key.
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (key, value),
            )

    def _commit(self) -> None:

        # If this thread is inside self.transaction(), do not commit here.
        # The transaction context manager will commit/rollback at the right time.
        if getattr(self._local, "in_transaction", False):
            return

        # Otherwise, commit this thread's connection immediately.
        self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # @contextmanager lets this method be used like:
        #
        #     with self.transaction() as conn:
        #         ...
        #
        # Code before `yield` runs when entering the with-block.
        # Code after `yield` runs when leaving the with-block.
        # This makes commit/rollback handling cleaner and centralized.

        # Get this thread's SQLite connection.
        conn = self.connection

        # If this thread is already inside a transaction, do not start a new one.
        # The outermost transaction owns commit/rollback so the full batch stays atomic.
        if getattr(self._local, "in_transaction", False):
            yield conn
            return

        # Mark this thread as being inside a transaction.
        # This also makes _commit() skip committing early.
        self._local.in_transaction = True
        try:
            # Give the caller access to the connection inside the with-block.
            yield conn

            # If the with-block completed successfully, save all changes.
            conn.commit()
        except Exception:
            # If anything failed inside the with-block, undo all changes
            # made since the transaction started.
            conn.rollback()
            raise
        finally:
            # Always clear the transaction flag for this thread.
            self._local.in_transaction = False

    def close(self) -> None:

        # Copy and clear the shared connection list while holding the lock.
        # This prevents other threads from modifying the list at the same time.
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()

        # Close each connection.
        # Ignore close errors so cleanup does not crash the program.
        for conn in connections:
            try:
                conn.close()
            except Exception:
                pass

    def snapshot_to(self, dest_path: str | Path) -> None:
        """Consistent whole-database copy via SQLite's online backup API.

        Copies committed state (including WAL) page-by-page, so it is safe to
        run while other connections read the same file. Used to take a revert
        point right before a long ingest.
        """
        if self.readonly:
            raise RuntimeError("cannot snapshot from a readonly database")

        dest_path = Path(dest_path)

        # Start from a clean destination so no stale pages/side files survive.
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(dest_path) + suffix)
            if candidate.exists():
                candidate.unlink()

        dest = sqlite3.connect(str(dest_path))
        try:
            with dest:
                # backup() copies this thread's live connection -> dest.
                self.connection.backup(dest)
        finally:
            dest.close()

    def restore_from(self, src_path: str | Path) -> None:
        """Overwrite this database's contents from a snapshot file (backup API,
        reverse direction). Reverts a failed/cancelled ingest to the snapshot.

        Must run with no write transaction open on this thread's connection and
        with the librarian write lock held so no other writer is active.
        """
        if self.readonly:
            raise RuntimeError("cannot restore into a readonly database")

        src_path = Path(src_path)
        if not src_path.exists():
            raise FileNotFoundError(f"snapshot not found: {src_path}")

        # Never restore on top of a half-open transaction on this connection.
        try:
            self.connection.rollback()
        except sqlite3.Error:
            pass

        src = sqlite3.connect(str(src_path))
        try:
            # backup() copies snapshot -> this thread's live connection.
            src.backup(self.connection)
        finally:
            src.close()

        # The restored meta table may carry a different embedding dim; drop the
        # cached value and re-read it so vector ops stay consistent.
        self._dim = None
        self._restore_dim()

    def upsert_node(self, node: Node) -> None:
        # Upserting modifies the database, so block it in readonly mode.
        if self.readonly:
            raise RuntimeError("cannot upsert node on readonly database")

        import json

        # Check whether this node already exists.
        # If it does, preserve its original created_at timestamp.
        existing = self.get_node(node.id)

        if existing:
            node.created_at = existing.created_at

        # Always refresh updated_at because this write touches the node.
        node.updated_at = now_iso()

        # Insert the node if it is new.
        # If the id already exists, update the existing row instead.
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
                bridge_probe,
                status,
                created_at,
                updated_at
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                bridge_probe=excluded.bridge_probe,
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
                # Store Python lists/dicts as JSON text in SQLite.
                json.dumps(node.source_ranges),
                node.source_version,
                node.source_material_hash,
                node.entity,
                json.dumps(node.claims),
                json.dumps(node.keywords),
                node.summary,
                node.cluster,
                node.bridge_probe,
                node.status.value,
                node.created_at,
                node.updated_at,
            ),
        )

        # Update the full-text-search table for this node.
        self._reindex_fts(node)

        # Commit now unless we are inside self.transaction().
        self._commit()

    def get_node(self, node_id: str) -> Node | None:
        # Fetch one node row by primary key.
        row = self.connection.execute(
            "SELECT * FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()

        # Convert the SQLite row into a Node object if found.
        # Return None if no node exists with this id.
        return _row_to_node(row) if row else None

    def set_node_status(self, node_id: str, status: NodeStatus) -> None:
        # Status update writes to the database,
        # so it is not allowed in readonly mode.
        if self.readonly:
            raise RuntimeError("cannot set node status on readonly database")

        # Update only the node status and updated_at timestamp.
        self.connection.execute(
            "UPDATE nodes SET status=?, updated_at=? WHERE id=?",
            (status.value, now_iso(), node_id),
        )

        # Commit now unless this is part of a larger transaction.
        self._commit()

    def delete_node(self, node_id: str) -> None:
        # Deleting modifies the database, so block it in readonly mode.
        if self.readonly:
            raise RuntimeError("cannot delete node on readonly database")

        # Use one transaction so either all related data is deleted,
        # or none of it is deleted if an error happens midway.
        with self.transaction() as conn:
            # Remove edges connected to this node, both incoming and outgoing.
            conn.execute(
                "DELETE FROM edges WHERE source_node_id=? OR target_node_id=?",
                (node_id, node_id),
            )

            # Remove the node itself.
            conn.execute(
                "DELETE FROM nodes WHERE id=?",
                (node_id,),
            )

            # Remove this node from the node-level full-text-search table.
            conn.execute(
                "DELETE FROM nodes_fts WHERE node_id=?",
                (node_id,),
            )

            # Get search item IDs before deleting search_items.
            # These IDs are needed to also delete related vector rows.
            item_ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM search_items WHERE node_id=?",
                    (node_id,),
                ).fetchall()
            ]

            # Remove this node's search items from the FTS table.
            conn.execute(
                "DELETE FROM search_items_fts WHERE node_id=?",
                (node_id,),
            )

            # Remove the regular search item rows for this node.
            conn.execute(
                "DELETE FROM search_items WHERE node_id=?",
                (node_id,),
            )

            # If vector tables are initialized, delete this node's embeddings too.
            if self._dim is not None:
                # Delete body and summary vectors for this node.
                conn.execute(
                    "DELETE FROM vec_body WHERE node_id=?",
                    (node_id,),
                )

                conn.execute(
                    "DELETE FROM vec_summary WHERE node_id=?",
                    (node_id,),
                )

                # Delete vectors for each search item belonging to this node.
                # vec_search_item uses the search item ID as its stored key.
                for item_id in item_ids:
                    conn.execute(
                        "DELETE FROM vec_search_item WHERE node_id=?",
                        (item_id,),
                    )

    def get_all_nodes(self, include_deleted: bool = False) -> list[Node]:
        # Start with all nodes.
        sql = "SELECT * FROM nodes"

        # By default, hide soft-deleted nodes.
        # Pass include_deleted=True to return them too.
        if not include_deleted:
            sql += " WHERE status != 'deleted'"

        # Return newest updated nodes first.
        sql += " ORDER BY updated_at DESC"

        # Convert SQLite rows into Node objects.
        return [_row_to_node(r) for r in self.connection.execute(sql).fetchall()]

    def get_nodes_by_document(
        self,
        document_name: str,
        active_only: bool = False,
    ) -> list[Node]:
        # Fetch nodes that came from one original document.
        sql = "SELECT * FROM nodes WHERE original_document_name=?"
        params: list[str] = [document_name]

        # Optionally return only active nodes.
        if active_only:
            sql += " AND status='active'"

        # Return newest updated nodes first.
        sql += " ORDER BY updated_at DESC"

        # Execute with parameters to avoid SQL injection,
        # then convert each row into a Node object.
        return [
            _row_to_node(r) for r in self.connection.execute(sql, params).fetchall()
        ]

    def get_nodes_by_entity(self, entity: str, limit: int = 20) -> list[Node]:
        # Nodes tagged with the exact same entity string. Cheap indexed lookup
        # (idx_nodes_entity) used as a bridge-candidate channel: catches nodes
        # that share a named subject even when their body embeddings sit far apart.
        rows = self.connection.execute(
            "SELECT * FROM nodes WHERE entity=? AND status='active' "
            "ORDER BY updated_at DESC LIMIT ?",
            (entity, limit),
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    def upsert_edge(self, edge: Edge) -> None:
        # Upserting modifies the database, so block it in readonly mode.
        if self.readonly:
            raise RuntimeError("cannot upsert edge on readonly database")

        import json

        # Insert a new edge, or update the existing edge if the ID already exists.
        # This keeps relationships between nodes current without creating duplicates.
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
                # Store Python list as JSON text in SQLite.
                json.dumps(edge.source_episode_ids),
            ),
        )

        # Commit now unless this is already inside a transaction().
        self._commit()

    def get_all_edges(self) -> list[Edge]:
        # Fetch every edge in the graph.
        # Newest edges are returned first based on created_at.
        rows = self.connection.execute(
            "SELECT * FROM edges ORDER BY created_at DESC"
        ).fetchall()

        # Convert SQLite rows into Edge objects.
        return [_row_to_edge(r) for r in rows]

    def get_edges_for_node(self, node_id: str) -> list[Edge]:
        # Fetch all edges connected to this node.
        # This includes both outgoing edges and incoming edges.
        rows = self.connection.execute(
            """
            SELECT *
            FROM edges
            WHERE source_node_id=? OR target_node_id=?
            ORDER BY created_at DESC
            """,
            (node_id, node_id),
        ).fetchall()

        # Convert SQLite rows into Edge objects.
        return [_row_to_edge(r) for r in rows]

    def get_outgoing_edges(
        self,
        node_id: str,
        label: str | None = None,
    ) -> list[Edge]:
        # Start with edges where this node is the source.
        sql = "SELECT * FROM edges WHERE source_node_id=?"
        params: list[str] = [node_id]

        # Optionally filter by edge label.
        # Example: only return edges labeled "mentions" or "supports".
        if label is not None:
            sql += " AND label=?"
            params.append(label)

        # Return newest edges first.
        sql += " ORDER BY created_at DESC"

        # Execute safely with parameters, then convert rows into Edge objects.
        return [
            _row_to_edge(r) for r in self.connection.execute(sql, params).fetchall()
        ]

    def get_incoming_edges(
        self,
        node_id: str,
        label: str | None = None,
    ) -> list[Edge]:
        # Start with edges where this node is the target.
        sql = "SELECT * FROM edges WHERE target_node_id=?"
        params: list[str] = [node_id]

        # Optionally filter by edge label.
        # Useful when only a certain relationship type is needed.
        if label is not None:
            sql += " AND label=?"
            params.append(label)

        # Return newest edges first.
        sql += " ORDER BY created_at DESC"

        # Execute safely with parameters, then convert rows into Edge objects.
        return [
            _row_to_edge(r) for r in self.connection.execute(sql, params).fetchall()
        ]

    def delete_edge(self, edge_id: str) -> None:
        # Deleting modifies the database, so block it in readonly mode.
        if self.readonly:
            raise RuntimeError("cannot delete edge on readonly database")

        # Delete the edge with the given primary key.
        self.connection.execute(
            "DELETE FROM edges WHERE id=?",
            (edge_id,),
        )

        # Commit now unless this is inside a larger transaction().
        self._commit()

    def delete_edges_by_label_for_nodes(
        self,
        label: str,
        node_ids: set[str],
    ) -> None:
        # This method deletes rows, so it cannot run on a readonly database.
        if self.readonly:
            raise RuntimeError("cannot delete edges on readonly database")

        # Nothing to delete if no node IDs were provided.
        if not node_ids:
            return

        # Build one SQL placeholder per node ID for the IN (...) clauses.
        # Values still go through params, so node IDs are not directly injected into SQL.
        placeholders = ",".join("?" for _ in node_ids)

        # Params are used for:
        # 1 label, then source_node_id IN (...), then target_node_id IN (...).
        params = [label, *node_ids, *node_ids]

        # Delete edges with this label where both endpoints are in node_ids.
        self.connection.execute(
            f"""
            DELETE FROM edges
            WHERE label=?
              AND source_node_id IN ({placeholders})
              AND target_node_id IN ({placeholders})
            """,
            params,
        )

        # Persist the deletion.
        self._commit()

    def record_source(self, document_name: str, source_hash: str) -> None:
        # Recording source info writes to the database,
        # so it is not allowed in readonly mode.
        if self.readonly:
            raise RuntimeError("cannot record source on readonly database")

        # Timestamp for when this source version was ingested.
        stamp = now_iso()

        # Use one transaction so both tables stay in sync.
        # If either insert fails, the whole operation is rolled back.
        with self.transaction() as conn:
            # Store the latest known hash for this document.
            # INSERT OR REPLACE updates the existing row if the document already exists.
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

            # Keep a historical record of this document/hash pair.
            # INSERT OR IGNORE avoids duplicating the same version.
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
        # Fetch the latest stored source hash and ingest timestamp for this document.
        row = self.connection.execute(
            """
            SELECT source_hash, ingested_at
            FROM sources
            WHERE document_name=?
            """,
            (document_name,),
        ).fetchone()

        # Return (source_hash, ingested_at) if found, otherwise None.
        return (row["source_hash"], row["ingested_at"]) if row else None

    def _reindex_fts(self, node: Node) -> None:
        # Remove any old full-text-search entry for this node.
        # This keeps the FTS index in sync when the node changes.
        self.connection.execute(
            "DELETE FROM nodes_fts WHERE node_id=?",
            (node.id,),
        )

        # Deleted nodes should not appear in search results.
        if node.status == NodeStatus.deleted:
            return

        # Build one searchable text field from the most useful node fields.
        # Empty values are skipped by filter(None, ...).
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

        # Insert the fresh searchable text into the FTS table.
        self.connection.execute(
            "INSERT INTO nodes_fts(node_id, text) VALUES(?, ?)",
            (node.id, text),
        )

    def keyword_search(self, text: str, limit: int = 20) -> list[Node]:
        # Convert user text into a safe/valid FTS query.
        query = _fts_query(text)

        # If the query is empty after cleanup, there is nothing to search.
        if not query:
            return []

        # Search the FTS table, join back to nodes, and only return active nodes.
        # ORDER BY rank puts the best text matches first.
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

        # Convert SQLite rows into Node objects.
        return [_row_to_node(r) for r in rows]

    def set_vector(self, node_id: str, table: str, vector: list[float]) -> None:
        # Setting vectors writes to the database, so block it in readonly mode.
        if self.readonly:
            raise RuntimeError("cannot set vector on readonly database")

        import sqlite_vec

        # Vector tables must exist before storing embeddings.
        if self._dim is None:
            raise RuntimeError("ensure_vec_tables() must run before set_vector()")

        # sqlite-vec stores float vectors as serialized float32 blobs.
        blob = sqlite_vec.serialize_float32(vector)

        # Replace any existing vector for this node/item in the chosen vector table.
        self.connection.execute(
            f"DELETE FROM {table} WHERE node_id=?",
            (node_id,),
        )

        self.connection.execute(
            f"INSERT INTO {table}(node_id, embedding) VALUES(?, ?)",
            (node_id, blob),
        )

        # Commit now unless this is inside a larger transaction().
        self._commit()

    def count_vectors(self, table: str = "vec_body") -> int:
        """Number of stored vectors in a table.

        Returns 0 if vectors are not set up yet.

        Used at startup to detect a half-finished re-embed: when this is less
        than the active node count, coverage is incomplete and all vectors are
        rebuilt.
        """
        # If vector tables were never initialized, no vectors are available.
        if self._dim is None:
            return 0

        try:
            # Count how many embeddings are stored in the selected vector table.
            row = self.connection.execute(
                f"SELECT COUNT(*) AS n FROM {table}"
            ).fetchone()
        except sqlite3.OperationalError:
            # If the table does not exist yet, treat it as empty.
            return 0

        return int(row["n"]) if row else 0

    def has_vector(self, node_id: str, table: str = "vec_body") -> bool:
        # If vector support is not initialized, this node cannot have a vector.
        if self._dim is None:
            return False

        # Check whether a vector row exists for this node/item.
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

        # No stored embedding dimension means vectors are not initialized.
        if self._dim is None:
            return None

        # Fetch the serialized embedding blob for this node/item.
        row = self.connection.execute(
            f"SELECT embedding FROM {table} WHERE node_id=?",
            (node_id,),
        ).fetchone()

        if row is None:
            return None

        # Convert the raw float32 blob back into a Python list of floats.
        return list(struct.unpack(f"{self._dim}f", row["embedding"]))

    def vector_search(
        self,
        vector: list[float],
        table: str = "vec_body",
        limit: int = 20,
    ) -> list[tuple[str, float]]:
        import sqlite_vec

        # If vector tables are not initialized, there is nothing to search.
        if self._dim is None:
            return []

        # Serialize the query vector into the format expected by sqlite-vec.
        blob = sqlite_vec.serialize_float32(vector)

        if table == "vec_search_item":
            # vec_search_item stores search_items.id in its node_id column.
            # After matching, join through search_items -> nodes so deleted nodes are excluded.
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

            # Return search item IDs with their vector distance.
            return [(r["item_id"], r["distance"]) for r in rows]

        # For normal node vector tables, match directly by node_id.
        # Lower distance means a closer vector match.
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

        # Return node IDs with their vector distance.
        return [(r["node_id"], r["distance"]) for r in rows]

    # --- evidence-first search items -----------------------------------------

    def replace_search_items(self, node_id: str, items: list[dict]) -> None:
        """Delete then re-insert every search_items row, FTS row, and stale vector.

        Vectors for the new rows are set separately.
        """
        # Replacing search items modifies the database,
        # so it is not allowed in readonly mode.
        if self.readonly:
            raise RuntimeError("cannot replace search items on readonly database")

        # Use one transaction so old items are deleted and new items are inserted
        # as one atomic operation.
        with self.transaction() as conn:
            # Remove existing search items, their FTS rows, and stale vectors.
            self._delete_search_items_conn(conn, node_id)

            for item in items:
                # Insert the regular search item row.
                # These rows store evidence chunks linked back to the node.
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

                # Also insert the item into the FTS table so it can be found
                # by keyword/full-text search.
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
        # Deleting search items modifies the database,
        # so block it in readonly mode.
        if self.readonly:
            raise RuntimeError("cannot delete search items on readonly database")

        # Use a transaction so search_items, FTS rows, and vectors stay in sync.
        with self.transaction() as conn:
            self._delete_search_items_conn(conn, node_id)

    def _delete_search_items_conn(
        self,
        conn: sqlite3.Connection,
        node_id: str,
    ) -> None:
        # Fetch item IDs before deleting search_items.
        # The IDs are needed to delete matching vector rows afterward.
        item_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM search_items WHERE node_id=?",
                (node_id,),
            ).fetchall()
        ]

        # Delete full-text-search rows for this node's search items.
        conn.execute(
            "DELETE FROM search_items_fts WHERE node_id=?",
            (node_id,),
        )

        # Delete the main search item rows for this node.
        conn.execute(
            "DELETE FROM search_items WHERE node_id=?",
            (node_id,),
        )

        # If vector tables are enabled, remove stale vectors for each item.
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
        # Setting vectors writes to the database,
        # so it is not allowed in readonly mode.
        if self.readonly:
            raise RuntimeError("cannot set search item vector on readonly database")

        import sqlite_vec

        # Vector tables must be created before storing search item embeddings.
        if self._dim is None:
            raise RuntimeError(
                "ensure_vec_tables() must run before set_search_item_vector()"
            )

        # Convert the Python float list into sqlite-vec's float32 blob format.
        blob = sqlite_vec.serialize_float32(vector)

        # Replace any existing vector for this search item.
        # vec_search_item stores item_id in the node_id column.
        self.connection.execute(
            "DELETE FROM vec_search_item WHERE node_id=?",
            (item_id,),
        )

        self.connection.execute(
            "INSERT INTO vec_search_item(node_id, embedding) VALUES(?, ?)",
            (item_id, blob),
        )

        # Commit now unless this is already inside a transaction().
        self._commit()

    def search_items_fts_query(
        self,
        text: str,
        limit: int = 150,
    ) -> list[dict]:
        # Convert raw user text into a valid FTS query.
        query = _fts_query(text)

        # If the cleaned query is empty, there is nothing to search.
        if not query:
            return []

        # Search evidence/search-item text, join back to nodes,
        # and only return items from active nodes.
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

        # Return plain dictionaries instead of model objects.
        return [dict(r) for r in rows]

    def get_search_items(self, ids: list[str]) -> dict[str, dict]:
        # Avoid building invalid SQL like `IN ()`.
        if not ids:
            return {}

        # Build one placeholder per ID for the IN (...) clause.
        # The actual values still go through parameters for safety.
        placeholders = ",".join("?" for _ in ids)

        # Fetch search items by ID.
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

        # Return a lookup map: item_id -> item data.
        return {r["item_id"]: dict(r) for r in rows}
