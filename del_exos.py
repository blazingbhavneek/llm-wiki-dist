#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

DEFAULT_DB_PATH = Path("/home/seigyo/llm-wiki-dist/.wiki/moove_wiki.sqlite")


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")

    # Needed if sqlite-vec virtual tables exist.
    # Without this, DELETE/SELECT on vec0 tables may fail in some environments.
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        print("[INFO] sqlite_vec extension loaded")
    except Exception as exc:
        print(f"[WARN] Could not load sqlite_vec extension: {exc}")
        print(
            "[WARN] This is okay only if vector tables do not exist or are not accessed."
        )

    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE name = ?
          AND type IN ('table', 'virtual table')
        LIMIT 1
        """,
        (table,),
    ).fetchone()

    if row is not None:
        return True

    # Some virtual tables/extensions may not report exactly as expected.
    try:
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


def chunked(values: list[str], size: int = 500) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def count_where_in(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    values: list[str],
) -> int:
    if not values or not table_exists(conn, table):
        return 0

    total = 0

    for chunk in chunked(values):
        placeholders = ",".join("?" for _ in chunk)
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {column} IN ({placeholders})",
            chunk,
        ).fetchone()
        total += int(row["n"] if row else 0)

    return total


def count_edges(conn: sqlite3.Connection, node_ids: list[str]) -> int:
    if not node_ids or not table_exists(conn, "edges"):
        return 0

    total = 0

    for chunk in chunked(node_ids):
        placeholders = ",".join("?" for _ in chunk)
        params = [*chunk, *chunk]
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM edges
            WHERE source_node_id IN ({placeholders})
               OR target_node_id IN ({placeholders})
            """,
            params,
        ).fetchone()
        total += int(row["n"] if row else 0)

    return total


def delete_where_in(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    values: list[str],
) -> int:
    if not values or not table_exists(conn, table):
        return 0

    deleted = 0

    # Use executemany instead of large IN clauses.
    # This works more reliably with sqlite-vec virtual tables too.
    before = conn.total_changes
    conn.executemany(
        f"DELETE FROM {table} WHERE {column} = ?",
        [(v,) for v in values],
    )
    deleted = conn.total_changes - before

    return deleted


def delete_edges_for_nodes(conn: sqlite3.Connection, node_ids: list[str]) -> int:
    if not node_ids or not table_exists(conn, "edges"):
        return 0

    before = conn.total_changes

    for node_id in node_ids:
        conn.execute(
            """
            DELETE FROM edges
            WHERE source_node_id = ?
               OR target_node_id = ?
            """,
            (node_id, node_id),
        )

    return conn.total_changes - before


def backup_db(db_path: Path) -> Path:
    backup_path = db_path.with_suffix(db_path.suffix + f".bak_exo_delete_{now_stamp()}")

    # Simple file copy is usually fine if app/worker is stopped.
    # Stop the app/worker before running this script.
    shutil.copy2(db_path, backup_path)

    wal_path = Path(str(db_path) + "-wal")
    shm_path = Path(str(db_path) + "-shm")

    if wal_path.exists():
        shutil.copy2(wal_path, Path(str(backup_path) + "-wal"))

    if shm_path.exists():
        shutil.copy2(shm_path, Path(str(backup_path) + "-shm"))

    return backup_path


def get_exogenous_node_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("""
        SELECT id
        FROM nodes
        WHERE type = 'exogenous'
           OR id LIKE 'exo:%'
        ORDER BY id
        """).fetchall()

    return [r["id"] for r in rows]


def get_search_item_ids_for_nodes(
    conn: sqlite3.Connection,
    node_ids: list[str],
) -> list[str]:
    if not node_ids or not table_exists(conn, "search_items"):
        return []

    item_ids: list[str] = []

    for chunk in chunked(node_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT id
            FROM search_items
            WHERE node_id IN ({placeholders})
            """,
            chunk,
        ).fetchall()

        item_ids.extend(r["id"] for r in rows)

    return item_ids


def print_plan(
    conn: sqlite3.Connection,
    node_ids: list[str],
    item_ids: list[str],
) -> None:
    print("")
    print("Delete plan")
    print("===========")
    print(f"Exogenous nodes matched: {len(node_ids)}")
    print(f"Search items matched:    {len(item_ids)}")
    print("")

    print("Rows that would be deleted:")
    print(f"- edges:            {count_edges(conn, node_ids)}")
    print(
        f"- nodes_fts:        {count_where_in(conn, 'nodes_fts', 'node_id', node_ids)}"
    )
    print(
        f"- search_items_fts: {count_where_in(conn, 'search_items_fts', 'node_id', node_ids)}"
    )
    print(
        f"- search_items:     {count_where_in(conn, 'search_items', 'node_id', node_ids)}"
    )
    print(
        f"- vec_body:         {count_where_in(conn, 'vec_body', 'node_id', node_ids)}"
    )
    print(
        f"- vec_summary:      {count_where_in(conn, 'vec_summary', 'node_id', node_ids)}"
    )

    # Important:
    # In your DB manager, vec_search_item.node_id stores search_items.id,
    # not the parent node id.
    by_item_id = count_where_in(conn, "vec_search_item", "node_id", item_ids)

    # Defensive cleanup in case older rows accidentally used node ids.
    by_node_id = count_where_in(conn, "vec_search_item", "node_id", node_ids)

    print(f"- vec_search_item by search item id: {by_item_id}")
    print(f"- vec_search_item by node id:        {by_node_id}")
    print(f"- nodes:            {count_where_in(conn, 'nodes', 'id', node_ids)}")
    print("")

    if node_ids:
        print("First matched node IDs:")
        for node_id in node_ids[:20]:
            print(f"  - {node_id}")

        if len(node_ids) > 20:
            print(f"  ... and {len(node_ids) - 20} more")

    print("")


def hard_delete_exogenous_nodes(conn: sqlite3.Connection) -> dict[str, int]:
    node_ids = get_exogenous_node_ids(conn)
    item_ids = get_search_item_ids_for_nodes(conn, node_ids)

    stats: dict[str, int] = {
        "matched_nodes": len(node_ids),
        "matched_search_items": len(item_ids),
        "edges": 0,
        "nodes_fts": 0,
        "search_items_fts": 0,
        "search_items": 0,
        "vec_body": 0,
        "vec_summary": 0,
        "vec_search_item_by_item_id": 0,
        "vec_search_item_by_node_id": 0,
        "nodes": 0,
    }

    if not node_ids:
        return stats

    with conn:
        # 1. Delete graph edges first.
        stats["edges"] = delete_edges_for_nodes(conn, node_ids)

        # 2. Delete FTS rows.
        stats["nodes_fts"] = delete_where_in(conn, "nodes_fts", "node_id", node_ids)
        stats["search_items_fts"] = delete_where_in(
            conn,
            "search_items_fts",
            "node_id",
            node_ids,
        )

        # 3. Delete vector rows.
        stats["vec_body"] = delete_where_in(conn, "vec_body", "node_id", node_ids)
        stats["vec_summary"] = delete_where_in(conn, "vec_summary", "node_id", node_ids)

        # In this codebase, vec_search_item.node_id stores search_items.id.
        stats["vec_search_item_by_item_id"] = delete_where_in(
            conn,
            "vec_search_item",
            "node_id",
            item_ids,
        )

        # Defensive cleanup for any malformed/old rows using the actual node id.
        stats["vec_search_item_by_node_id"] = delete_where_in(
            conn,
            "vec_search_item",
            "node_id",
            node_ids,
        )

        # 4. Delete search item rows.
        stats["search_items"] = delete_where_in(
            conn,
            "search_items",
            "node_id",
            node_ids,
        )

        # 5. Delete actual node rows last.
        stats["nodes"] = delete_where_in(conn, "nodes", "id", node_ids)

    return stats


def verify_no_exogenous_nodes(conn: sqlite3.Connection) -> int:
    row = conn.execute("""
        SELECT COUNT(*) AS n
        FROM nodes
        WHERE type = 'exogenous'
           OR id LIKE 'exo:%'
        """).fetchone()

    return int(row["n"] if row else 0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hard-delete all exogenous / exo: nodes from the SQLite wiki DB."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite DB. Default: {DEFAULT_DB_PATH}",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete rows. Without this, only a dry-run plan is shown.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create a backup before deleting. Not recommended.",
    )

    args = parser.parse_args()
    db_path: Path = args.db

    if not db_path.exists():
        print(f"[ERROR] DB file does not exist: {db_path}", file=sys.stderr)
        return 1

    print(f"[INFO] DB path: {db_path}")
    print("[WARN] Stop the app/server/worker before running with --apply.")

    conn = connect(db_path)

    try:
        node_ids = get_exogenous_node_ids(conn)
        item_ids = get_search_item_ids_for_nodes(conn, node_ids)

        print_plan(conn, node_ids, item_ids)

        if not args.apply:
            print("[DRY RUN] No rows were deleted.")
            print("[DRY RUN] Re-run with --apply to actually delete.")
            return 0

        if not node_ids:
            print("[INFO] No exogenous / exo: nodes found. Nothing to delete.")
            return 0

        if not args.no_backup:
            backup_path = backup_db(db_path)
            print(f"[INFO] Backup created: {backup_path}")
        else:
            print("[WARN] Backup skipped because --no-backup was provided.")

        stats = hard_delete_exogenous_nodes(conn)

        print("")
        print("Deletion result")
        print("===============")
        for key, value in stats.items():
            print(f"{key}: {value}")

        remaining = verify_no_exogenous_nodes(conn)

        print("")
        print("Verification")
        print("============")
        print(f"Remaining exogenous / exo: nodes: {remaining}")

        if remaining == 0:
            print("[OK] All exogenous / exo: nodes were removed.")
            return 0

        print("[WARN] Some exogenous / exo: nodes still remain.")
        return 2

    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
