"""
FastAPI backend over four actors:

  - ModelGateway  — chat LLM + embedder + reranker + settings
  - GraphStore    — SQLite persistence (one writable, one read-only instance)
  - Librarian     — all writes: job queue + enrichment drip + bootstrap
  - Researcher    — all reads: search + ask() agent, bounded concurrency

This file is transport only: HTTP/SSE in, actor methods out.

Run:
    pip install fastapi "uvicorn[standard]"
    WIKI_DB=.wiki/wiki3.sqlite uvicorn app:app --port 51023
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.requests import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from graph.core import Settings
from graph.gateway import ModelGateway
from graph.librarian import Librarian, job_to_dict
from graph.researcher import AgentStopped, Researcher
from graph.store import GraphStore
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("app")
SSE_EXECUTOR = ThreadPoolExecutor(max_workers=64, thread_name_prefix="sse")
INGEST_ROOT = Path(os.environ.get("WIKI_INGEST_ROOT", ".")).resolve()

# ============================================================================
# lifecycle
# ============================================================================


class AgentRunRegistry:
    """Tracks in-flight streaming agent runs so /stop can cancel them."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_events: dict[str, threading.Event] = {}

    def register(self, run_id: str, task: asyncio.Task, stop: threading.Event) -> None:
        self._tasks[run_id] = task
        self._stop_events[run_id] = stop

    def remove(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        self._stop_events.pop(run_id, None)

    def stop(self, run_id: str) -> bool:
        """Signal + cancel a run. False when the run id is unknown."""
        task = self._tasks.get(run_id)
        stop_event = self._stop_events.get(run_id)
        if task is None and stop_event is None:
            return False
        if stop_event is not None:
            stop_event.set()
        if task is not None and not task.done():
            task.cancel()
        return True


# Per-db runtime config (reverse-proxy prefix + where the .sqlite files live).
DB_DIR = Path(os.environ.get("WIKI_DB_DIR", ".wiki"))
DEFAULT_DB = os.environ.get("WIKI_DEFAULT_DB", "wiki")
PREFIX = os.environ.get("WIKI_PREFIX", "/llm-wiki").rstrip("/")  # e.g. "/llm-wiki"
_DB_RE = re.compile(r"[A-Za-z0-9_-]+")
_RESERVED_DB_NAMES = {"admin", "assets"}

ADMIN_PASSWORD = os.environ.get("WIKI_ADMIN_PASSWORD", "seigyo@rikiseisan")
MAX_SQLITE_UPLOAD_BYTES = int(
    os.environ.get("WIKI_MAX_SQLITE_UPLOAD_BYTES", str(512 * 1024 * 1024))
)
ADMIN_DB_LOCK = asyncio.Lock()

# The db for the current request; set by the db_routing middleware from the URL.
current_db: ContextVar[str] = ContextVar("current_db", default=DEFAULT_DB)

# One stack per db, built lazily on first request and cached. Each value is a
# dict: {"gateway", "write_store", "librarian", "read_store", "researcher"}.
STACKS: dict[str, dict] = {}
stages: dict[str, str] = {}
errors: dict[str, str | None] = {}
building: set[str] = set()
agent_runs = AgentRunRegistry()


def api_error(detail: str, retryable: bool, code: str, **extra: Any) -> dict[str, Any]:
    return {"detail": detail, "retryable": retryable, "code": code, **extra}


def _not_ready_detail(db: str) -> dict[str, Any]:
    stage = stages.get(db, "starting")
    return api_error(
        errors.get(db) or f"server is not ready ({stage})",
        True,
        "not_ready",
        stage=stage,
    )


def _build_stack(db_path: str) -> dict:
    log.info("startup: building stack db=%s", db_path)
    settings = Settings.from_env()
    settings.database_path = db_path  # pick the sqlite for this db

    new_gateway = ModelGateway(settings)
    new_write_store = GraphStore(settings.database_path)  # rwc: created if missing
    new_librarian = Librarian(new_gateway, new_write_store, max_queue_size=100)

    log.info("startup: running bootstrap (embed/search/cluster catch-up)")
    try:
        new_librarian.bootstrap()  # empty db -> no-op, same as first-run
        new_read_store = GraphStore(settings.database_path, readonly=True)
        new_researcher = Researcher(new_gateway, new_read_store)
    except BaseException:
        try:
            new_librarian._enrich_stopping.set()
            new_librarian._enrich_queue.put(None)
        except Exception:
            pass
        new_write_store.close()
        new_gateway.close()
        raise

    return {
        "gateway": new_gateway,
        "write_store": new_write_store,
        "librarian": new_librarian,
        "read_store": new_read_store,
        "researcher": new_researcher,
    }


async def _bootstrap_db(db: str) -> None:
    """Build + start the stack for one db, caching it in STACKS."""
    if db in building or db in STACKS:
        return

    building.add(db)
    errors[db] = None
    stages[db] = "starting"
    try:
        stack = await asyncio.to_thread(_build_stack, str(DB_DIR / f"{db}.sqlite"))
        await stack["librarian"].start()
        STACKS[db] = stack
        stages[db] = "ready"
        errors[db] = None
        log.info("startup: ready db=%s, serving requests", db)
    except Exception as exc:
        stages[db] = "failed"
        errors[db] = f"{type(exc).__name__}: {exc}"
        log.exception("startup/bootstrap failed db=%s", db)
    finally:
        building.discard(db)


async def _close_stack(db: str) -> None:
    """Stop and remove one db stack before deleting/replacing its SQLite file."""
    stack = STACKS.pop(db, None)
    if stack is None:
        return

    with suppress(Exception):
        await stack["librarian"].stop()
    with suppress(Exception):
        stack["write_store"].close()
    with suppress(Exception):
        stack["read_store"].close()
    with suppress(Exception):
        stack["gateway"].close()

    stages.pop(db, None)
    errors.pop(db, None)


def _ensure_building(db: str) -> None:
    """Kick off a lazy build for db if it isn't ready or already building."""
    if db not in STACKS and db not in building:
        asyncio.create_task(_bootstrap_db(db))


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Surface graph.* INFO logs (bootstrap / reclustering / cluster naming) that
    # otherwise stay hidden behind the root logger's WARNING default.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("graph_librarian").setLevel(logging.INFO)

    DB_DIR.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=64, thread_name_prefix="default")
    )

    # Stacks are built lazily on first request per db; nothing to pre-build.
    try:
        yield
    finally:
        for stack in list(STACKS.values()):
            try:
                await stack["librarian"].stop()
            except Exception:
                pass
            stack["write_store"].close()
            stack["read_store"].close()
            stack["gateway"].close()
        STACKS.clear()


def _ready_stack() -> dict:
    db = current_db.get()
    if stages.get(db) == "ready" and db in STACKS:
        return STACKS[db]
    _ensure_building(db)
    raise HTTPException(status_code=503, detail=_not_ready_detail(db))


def _gateway() -> ModelGateway:
    return _ready_stack()["gateway"]


def reads() -> Researcher:
    return _ready_stack()["researcher"]


def writes() -> Librarian:
    return _ready_stack()["librarian"]


# ============================================================================
# helpers
# ============================================================================


def _dump(obj: Any) -> Any:
    """Recursive pydantic / dataclass serialization."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _dump(v) for k, v in asdict(obj).items()}
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, (list, tuple)):
        return [_dump(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _dump(v) for k, v in obj.items()}
    return obj


def _compact_node(obj: Any) -> dict[str, Any]:
    data = _dump(obj)
    if not isinstance(data, dict):
        return {"id": str(data)}
    fields = ("id", "title", "summary", "type", "cluster")
    return {key: data[key] for key in fields if data.get(key) is not None}


def _job_response(job):
    d = job_to_dict(job)
    # Job payloads can embed whole markdown documents; clients polling job
    # status only need type/status/result/error. Truncate long strings so
    # every poll stays cheap.
    payload = d.get("payload")
    if isinstance(payload, dict):
        d["payload"] = {
            k: (
                v[:200] + f"… ({len(v)} chars)"
                if isinstance(v, str) and len(v) > 200
                else v
            )
            for k, v in payload.items()
        }
    if job.status == "queued":
        d["position"] = writes().queue_position(job.id)
    return _dump(d)


def _path_within_ingest_root(value: str) -> str:
    path = Path(value).expanduser().resolve()
    if not path.is_relative_to(INGEST_ROOT):
        raise HTTPException(
            status_code=400,
            detail=api_error("path outside allowed ingest root", False, "bad_path"),
        )
    return str(path)


def _validate_db_name(db: str) -> str:
    if not _DB_RE.fullmatch(db) or db in _RESERVED_DB_NAMES:
        raise HTTPException(
            status_code=400,
            detail=api_error("invalid db name", False, "bad_db_name"),
        )
    return db


def _db_path(db: str) -> Path:
    return DB_DIR / f"{db}.sqlite"


def _db_url(db: str) -> str:
    return f"{PREFIX}/{quote(db)}/"


def _db_sidecar_paths(db: str) -> list[Path]:
    path = _db_path(db)
    return [
        path,
        path.with_name(path.name + "-wal"),
        path.with_name(path.name + "-shm"),
    ]


def _unlink_db_files(db: str) -> bool:
    deleted = False
    for path in _db_sidecar_paths(db):
        if path.exists():
            path.unlink()
            deleted = True
    return deleted


def _require_admin(
    password: str | None = Header(default=None, alias="X-Admin-Password"),
) -> None:
    if not ADMIN_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail=api_error(
                "admin API disabled: set WIKI_ADMIN_PASSWORD",
                False,
                "admin_disabled",
            ),
        )
    if password != ADMIN_PASSWORD:
        raise HTTPException(
            status_code=401,
            detail=api_error("invalid admin password", False, "unauthorized"),
        )


def _open_sqlite_ro(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path.resolve()), safe="/:\\") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")

    # Best effort: vector virtual tables may need sqlite-vec loaded for counts.
    with suppress(Exception):
        import sqlite_vec

        with suppress(Exception):
            conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        with suppress(Exception):
            conn.enable_load_extension(False)

    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE name=?
          AND type IN ('table', 'virtual table')
        LIMIT 1
        """,
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _count_sql(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
    default: int = 0,
) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return default
    if row is None:
        return default
    return int(row[0] or 0)


def _validate_sqlite_file(path: Path, *, final: bool) -> None:
    if not path.exists() or not path.is_file():
        raise ValueError("file does not exist")
    if path.stat().st_size <= 0:
        raise ValueError("file is empty")

    try:
        conn = _open_sqlite_ro(path)
    except sqlite3.Error as exc:
        raise ValueError(f"cannot open sqlite: {exc}") from exc

    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        integrity = [str(row[0]) for row in rows]
        if integrity != ["ok"]:
            raise ValueError(f"integrity_check failed: {integrity[:5]}")

        required_tables = {
            "meta",
            "nodes",
            "edges",
            "sources",
            "source_versions",
        }
        if final:
            required_tables |= {
                "nodes_fts",
                "search_items",
                "search_items_fts",
            }

        for table in sorted(required_tables):
            if not _table_exists(conn, table):
                raise ValueError(f"missing required table: {table}")

        required_columns = {
            "nodes": {
                "id",
                "body",
                "type",
                "title",
                "original_document_name",
                "source_path",
                "source_ranges_json",
                "keywords_json",
                "summary",
                "cluster",
                "status",
                "created_at",
                "updated_at",
            },
            "edges": {
                "id",
                "source_node_id",
                "target_node_id",
                "label",
                "summary",
                "created_at",
            },
            "sources": {
                "document_name",
                "source_hash",
                "ingested_at",
            },
            "source_versions": {
                "document_name",
                "source_hash",
                "ingested_at",
            },
        }

        if final:
            required_columns["nodes"] |= {
                "source_version",
                "source_material_hash",
                "entity",
                "claims_json",
                "bridge_probe",
            }
            required_columns["edges"] |= {
                "valid_at",
                "invalid_at",
                "expired_at",
                "source_episode_ids_json",
            }

        for table, cols in required_columns.items():
            have = _columns(conn, table)
            missing = sorted(cols - have)
            if missing:
                raise ValueError(
                    f"missing required columns in {table}: {', '.join(missing)}"
                )
    finally:
        conn.close()


def _migrate_sqlite_file(path: Path) -> None:
    store = None
    try:
        store = GraphStore(str(path), readonly=False)
    finally:
        if store is not None:
            store.close()


def _db_summary_from_path(path: Path) -> dict[str, Any]:
    db = path.name[: -len(".sqlite")]
    stat = path.stat()

    base: dict[str, Any] = {
        "name": db,
        "url": _db_url(db),
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
    }

    try:
        conn = _open_sqlite_ro(path)
    except Exception as exc:
        return {
            **base,
            "valid": False,
            "error": f"cannot open sqlite: {type(exc).__name__}: {exc}",
        }

    try:
        required = [
            "nodes",
            "edges",
            "sources",
            "source_versions",
            "nodes_fts",
            "search_items",
            "search_items_fts",
        ]
        for table in required:
            if not _table_exists(conn, table):
                return {
                    **base,
                    "valid": False,
                    "error": f"missing required table: {table}",
                }

        docs = [
            row["document_name"]
            for row in conn.execute(
                """
                SELECT document_name
                FROM sources
                ORDER BY document_name
                """
            ).fetchall()
        ]

        vectors: dict[str, Any] = {
            "embed_dim": None,
            "vec_body": 0,
            "vec_summary": 0,
            "vec_bridge": 0,
            "vec_search_item": 0,
        }

        if _table_exists(conn, "meta"):
            row = conn.execute(
                "SELECT value FROM meta WHERE key='embed_dim'"
            ).fetchone()
            if row is not None:
                with suppress(Exception):
                    vectors["embed_dim"] = int(row["value"])

        for table in ("vec_body", "vec_summary", "vec_bridge", "vec_search_item"):
            if _table_exists(conn, table):
                try:
                    vectors[table] = _count_sql(conn, f"SELECT COUNT(*) FROM {table}")
                except sqlite3.Error:
                    vectors[table] = 0

        edge_labels = [
            {"label": row["label"], "count": int(row["count"])}
            for row in conn.execute(
                """
                SELECT label, COUNT(*) AS count
                FROM edges
                GROUP BY label
                ORDER BY count DESC, label ASC
                """
            ).fetchall()
        ]

        return {
            **base,
            "valid": True,
            "docs_count": len(docs),
            "docs": docs,
            "nodes_total": _count_sql(conn, "SELECT COUNT(*) FROM nodes"),
            "nodes_active": _count_sql(
                conn, "SELECT COUNT(*) FROM nodes WHERE status='active'"
            ),
            "nodes_deleted": _count_sql(
                conn, "SELECT COUNT(*) FROM nodes WHERE status='deleted'"
            ),
            "nodes_endo": _count_sql(
                conn,
                """
                SELECT COUNT(*)
                FROM nodes
                WHERE type IN ('endo', 'endogenous')
                """,
            ),
            "nodes_exo": _count_sql(
                conn,
                """
                SELECT COUNT(*)
                FROM nodes
                WHERE type IN ('exo', 'exogenous')
                """,
            ),
            "links_total": _count_sql(conn, "SELECT COUNT(*) FROM edges"),
            "search_items_total": _count_sql(
                conn, "SELECT COUNT(*) FROM search_items"
            ),
            "vectors": vectors,
            "edge_labels": edge_labels,
        }
    except Exception as exc:
        return {
            **base,
            "valid": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        conn.close()


def _db_summary(db: str) -> dict[str, Any]:
    return _db_summary_from_path(_db_path(db))


app = FastAPI(title="LLM-Wiki API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DIST_DIR = Path(__file__).parent / "frontend" / "dist"

STATIC_ROOT_FILES = {
    "/favicon.svg",
    "/favicon.ico",
    "/manifest.json",
    "/robots.txt",
}

STATIC_PREFIXES = (
    "/assets/",
)


@app.middleware("http")
async def db_routing(request: Request, call_next):
    """Peel the reverse-proxy prefix + the db segment off the URL.

    Layout served:
        {PREFIX}/{db}/              -> frontend
        {PREFIX}/{db}/api/...        -> backend for that db
        {PREFIX}/admin/api/...       -> admin backend, not a db
        {PREFIX}/favicon.svg         -> static frontend asset
        {PREFIX}/assets/...          -> static frontend assets

    The proxy should NOT strip PREFIX. The app owns PREFIX here.
    """
    raw = request.scope["path"]

    if PREFIX and raw.startswith(PREFIX):
        path = raw[len(PREFIX):] or "/"
    else:
        path = raw

    # Admin is prefix-level, not a wiki db named "admin".
    if path == "/admin" or path.startswith("/admin/"):
        request.scope["path"] = path
        request.scope["raw_path"] = path.encode()
        request.scope["root_path"] = PREFIX
        return await call_next(request)

    # ------------------------------------------------------------------
    # Safe static asset bypass.
    #
    # This handles paths like:
    #   /llm-wiki/favicon.svg
    #   /llm-wiki/assets/index-abc123.js
    #
    # It intentionally only allows known frontend asset locations, so it
    # does not interfere with db routes like:
    #   /llm-wiki/wiki/
    #   /llm-wiki/wiki/api/ready
    # ------------------------------------------------------------------
    is_static_asset = (
        path in STATIC_ROOT_FILES
        or any(path.startswith(prefix) for prefix in STATIC_PREFIXES)
    )

    if is_static_asset:
        static_candidate = (DIST_DIR / path.lstrip("/")).resolve()

        try:
            static_candidate.relative_to(DIST_DIR.resolve())
        except ValueError:
            return PlainTextResponse("not found", status_code=404)

        if static_candidate.is_file():
            request.scope["path"] = path
            request.scope["raw_path"] = path.encode()
            request.scope["root_path"] = PREFIX
            return await call_next(request)

        return PlainTextResponse("not found", status_code=404)

    stripped = path.strip("/")

    if stripped == "":
        return RedirectResponse(f"{PREFIX}/{DEFAULT_DB}/", status_code=307)

    seg, _, tail = stripped.partition("/")

    if not _DB_RE.fullmatch(seg):
        return PlainTextResponse("unknown wiki", status_code=404)

    # Important: normal wiki traffic must never create a new empty DB because
    # of a typo in the URL. Only admin create/upload may create DB files.
    if not _db_path(seg).exists():
        return PlainTextResponse("unknown wiki", status_code=404)

    if tail == "" and not path.endswith("/"):
        return RedirectResponse(f"{PREFIX}/{seg}/", status_code=307)

    clean = "/" + tail
    request.scope["path"] = clean
    request.scope["raw_path"] = clean.encode()
    request.scope["root_path"] = f"{PREFIX}/{seg}"

    token = current_db.set(seg)
    try:
        return await call_next(request)
    finally:
        current_db.reset(token)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=api_error(
            f"サーバー内部エラー: {type(exc).__name__}: {exc}",
            True,
            "internal_error",
        ),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and {"detail", "retryable", "code"} <= set(
        exc.detail
    ):
        content = exc.detail
    else:
        content = api_error(str(exc.detail), exc.status_code >= 500, "http_error")
    return JSONResponse(status_code=exc.status_code, content=content)


@app.get("/api/ready")
async def ready() -> dict[str, Any]:
    db = current_db.get()
    _ensure_building(db)
    stage = stages.get(db, "starting")
    return {
        "ready": stage == "ready",
        "stage": stage,
        "error": errors.get(db),
        "retryable": stage != "ready",
    }


@app.post("/api/admin/restart-bootstrap")
async def restart_bootstrap() -> dict[str, Any]:
    db = current_db.get()
    if stages.get(db) == "ready":
        return {"ready": True, "stage": "ready", "error": None}
    if db not in building:
        asyncio.create_task(_bootstrap_db(db))
    return {"ready": False, "stage": stages.get(db, "starting"), "error": errors.get(db)}


# ============================================================================
# prefix-level ADMIN DB MANAGEMENT
# ============================================================================


@app.get("/admin/api/dbs")
async def admin_list_dbs(_: str | None = Header(default=None, alias="X-Admin-Password")):
    _require_admin(_)
    DB_DIR.mkdir(parents=True, exist_ok=True)
    dbs = [
        _db_summary_from_path(path)
        for path in sorted(DB_DIR.glob("*.sqlite"), key=lambda p: p.name)
        if not path.name.endswith(".sqlite-wal")
        and not path.name.endswith(".sqlite-shm")
    ]
    return {
        "default": DEFAULT_DB,
        "db_dir": str(DB_DIR),
        "dbs": dbs,
    }


@app.get("/admin/api/dbs/{db}")
async def admin_get_db(
    db: str,
    _: str | None = Header(default=None, alias="X-Admin-Password"),
):
    _require_admin(_)
    db = _validate_db_name(db)
    path = _db_path(db)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=api_error("db not found", False, "not_found"),
        )
    return _db_summary(db)


@app.post("/admin/api/dbs/{db}")
async def admin_create_db(
    db: str,
    _: str | None = Header(default=None, alias="X-Admin-Password"),
):
    _require_admin(_)
    db = _validate_db_name(db)

    async with ADMIN_DB_LOCK:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        path = _db_path(db)
        if path.exists():
            raise HTTPException(
                status_code=409,
                detail=api_error("db already exists", False, "db_exists"),
            )

        await _bootstrap_db(db)

        if stages.get(db) != "ready" or not path.exists():
            raise HTTPException(
                status_code=500,
                detail=api_error(
                    errors.get(db) or "failed to create db",
                    True,
                    "create_failed",
                    stage=stages.get(db),
                ),
            )

        return {
            "db": db,
            "created": True,
            "url": _db_url(db),
            "stats": _db_summary(db),
        }


@app.post("/admin/api/dbs/{db}/upload")
async def admin_upload_db(
    db: str,
    file: UploadFile = File(...),
    replace: bool = Query(False),
    bootstrap: bool = Query(True),
    _: str | None = Header(default=None, alias="X-Admin-Password"),
):
    _require_admin(_)
    db = _validate_db_name(db)

    tmp_path: Path | None = None

    async with ADMIN_DB_LOCK:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        target = _db_path(db)

        if target.exists() and not replace:
            raise HTTPException(
                status_code=409,
                detail=api_error(
                    "db already exists; pass replace=true to replace it",
                    False,
                    "db_exists",
                ),
            )

        fd, tmp_name = tempfile.mkstemp(prefix=f"llm-wiki-upload-{db}-", suffix=".sqlite")
        tmp_path = Path(tmp_name)

        try:
            total = 0
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break

                    total += len(chunk)
                    if total > MAX_SQLITE_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=api_error(
                                (
                                    "uploaded sqlite is too large "
                                    f"({total} bytes > {MAX_SQLITE_UPLOAD_BYTES} bytes)"
                                ),
                                False,
                                "upload_too_large",
                            ),
                        )

                    out.write(chunk)

            try:
                _validate_sqlite_file(tmp_path, final=False)
                await asyncio.to_thread(_migrate_sqlite_file, tmp_path)
                _validate_sqlite_file(tmp_path, final=True)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=api_error(f"invalid sqlite: {exc}", False, "invalid_sqlite"),
                ) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=api_error(
                        f"sqlite migration/validation failed: {type(exc).__name__}: {exc}",
                        False,
                        "invalid_sqlite",
                    ),
                ) from exc

            await _close_stack(db)

            if replace:
                _unlink_db_files(db)

            shutil.move(str(tmp_path), str(target))
            tmp_path = None

            if bootstrap:
                await _bootstrap_db(db)

            return {
                "db": db,
                "uploaded": True,
                "replaced": bool(replace),
                "url": _db_url(db),
                "bootstrap": bool(bootstrap),
                "stage": stages.get(db),
                "stats": _db_summary(db),
            }
        finally:
            with suppress(Exception):
                await file.close()
            if tmp_path is not None and tmp_path.exists():
                with suppress(Exception):
                    tmp_path.unlink()


@app.delete("/admin/api/dbs/{db}")
async def admin_delete_db(
    db: str,
    _: str | None = Header(default=None, alias="X-Admin-Password"),
):
    _require_admin(_)
    db = _validate_db_name(db)

    async with ADMIN_DB_LOCK:
        await _close_stack(db)
        deleted = _unlink_db_files(db)
        stages.pop(db, None)
        errors.pop(db, None)

        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=api_error("db not found", False, "not_found"),
            )

        return {
            "db": db,
            "deleted": True,
        }



class AdminDbCopyRequest(BaseModel):
    target: str | None = None
    bootstrap: bool = True


class AdminDbRenameRequest(BaseModel):
    target: str


def _next_copy_name(db: str) -> str:
    """
    Initial copy name is {db}_copy.
    If it already exists, this returns {db}_copy_2, {db}_copy_3, ...
    You can remove the loop if you prefer strict 409 on existing {db}_copy.
    """
    base = f"{db}_copy"
    candidate = base
    i = 2

    while _db_path(candidate).exists():
        candidate = f"{base}_{i}"
        i += 1

    return candidate


@app.post("/admin/api/dbs/{db}/copy")
async def admin_copy_db(
    db: str,
    payload: AdminDbCopyRequest | None = None,
    _: str | None = Header(default=None, alias="X-Admin-Password"),
):
    """
    Copy a SQLite wiki DB.

    Default target:
        source: mywiki
        copy:   mywiki_copy

    Request body optional:
        {
          "target": "mywiki_backup",
          "bootstrap": true
        }
    """
    _require_admin(_)
    db = _validate_db_name(db)

    payload = payload or AdminDbCopyRequest()
    target_db = payload.target or _next_copy_name(db)
    target_db = _validate_db_name(target_db)

    if target_db == db:
        raise HTTPException(
            status_code=400,
            detail=api_error("target db must be different", False, "same_db_name"),
        )

    async with ADMIN_DB_LOCK:
        DB_DIR.mkdir(parents=True, exist_ok=True)

        source = _db_path(db)
        target = _db_path(target_db)

        if not source.exists():
            raise HTTPException(
                status_code=404,
                detail=api_error("source db not found", False, "not_found"),
            )

        if target.exists():
            raise HTTPException(
                status_code=409,
                detail=api_error("target db already exists", False, "db_exists"),
            )

        # Ensure pending writes are closed/flushed before filesystem copy.
        await _close_stack(db)
        await _close_stack(target_db)

        # Remove stale sidecars for target if somehow present.
        _unlink_db_files(target_db)

        try:
            shutil.copy2(str(source), str(target))
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=api_error(
                    f"failed to copy db: {type(exc).__name__}: {exc}",
                    True,
                    "copy_failed",
                ),
            ) from exc

        try:
            _validate_sqlite_file(target, final=True)
        except ValueError as exc:
            _unlink_db_files(target_db)
            raise HTTPException(
                status_code=400,
                detail=api_error(
                    f"copied sqlite is invalid: {exc}",
                    False,
                    "invalid_sqlite",
                ),
            ) from exc

        stages.pop(target_db, None)
        errors.pop(target_db, None)

        if payload.bootstrap:
            await _bootstrap_db(target_db)

        return {
            "db": db,
            "target": target_db,
            "copied": True,
            "url": _db_url(target_db),
            "bootstrap": bool(payload.bootstrap),
            "stage": stages.get(target_db),
            "stats": _db_summary(target_db),
        }


@app.patch("/admin/api/dbs/{db}/rename")
async def admin_rename_db(
    db: str,
    payload: AdminDbRenameRequest,
    _: str | None = Header(default=None, alias="X-Admin-Password"),
):
    """
    Rename a SQLite wiki DB.

    Request body:
        {
          "target": "new_name"
        }
    """
    _require_admin(_)
    db = _validate_db_name(db)
    target_db = _validate_db_name(payload.target)

    if target_db == db:
        raise HTTPException(
            status_code=400,
            detail=api_error("target db must be different", False, "same_db_name"),
        )

    async with ADMIN_DB_LOCK:
        DB_DIR.mkdir(parents=True, exist_ok=True)

        source = _db_path(db)
        target = _db_path(target_db)

        if not source.exists():
            raise HTTPException(
                status_code=404,
                detail=api_error("source db not found", False, "not_found"),
            )

        if target.exists():
            raise HTTPException(
                status_code=409,
                detail=api_error("target db already exists", False, "db_exists"),
            )

        await _close_stack(db)
        await _close_stack(target_db)

        source_sidecars = _db_sidecar_paths(db)
        target_sidecars = _db_sidecar_paths(target_db)

        # Do not overwrite any target sidecars.
        for path in target_sidecars:
            if path.exists():
                raise HTTPException(
                    status_code=409,
                    detail=api_error(
                        f"target sidecar already exists: {path.name}",
                        False,
                        "db_exists",
                    ),
                )

        moved: list[tuple[Path, Path]] = []

        try:
            for src, dst in zip(source_sidecars, target_sidecars):
                if src.exists():
                    shutil.move(str(src), str(dst))
                    moved.append((src, dst))
        except Exception as exc:
            # Best-effort rollback.
            for src, dst in reversed(moved):
                with suppress(Exception):
                    if dst.exists() and not src.exists():
                        shutil.move(str(dst), str(src))

            raise HTTPException(
                status_code=500,
                detail=api_error(
                    f"failed to rename db: {type(exc).__name__}: {exc}",
                    True,
                    "rename_failed",
                ),
            ) from exc

        stages.pop(db, None)
        errors.pop(db, None)
        stages.pop(target_db, None)
        errors.pop(target_db, None)

        return {
            "db": db,
            "target": target_db,
            "renamed": True,
            "url": _db_url(target_db),
            "stats": _db_summary(target_db),
        }

# ============================================================================
# request models
# ============================================================================


class AskBody(BaseModel):
    question: str
    # Optional per-request tunable overrides (subagents, depth, search net,
    # chat endpoint/model/key). Omitted keys fall back to server defaults.
    overrides: dict[str, Any] | None = None


class UpdateBody(BaseModel):
    body: str


class ExogenousBody(BaseModel):
    body: str
    source_node_ids: list[str] = Field(default_factory=list)
    origin: str | None = None

    # New: original user query this note answers.
    question: str | None = None


class DeleteDocumentBody(BaseModel):
    # Either side may be empty: agent-note "documents" exist only client-side
    # (their nodes carry no original_document_name), so the UI sends node ids;
    # ingested documents are addressed by name.
    document_name: str | None = None
    node_ids: list[str] = Field(default_factory=list)


class DocumentBody(BaseModel):
    body: str
    title: str | None = None
    document_name: str | None = None
    source_path: str | None = None
    source_ranges: list[tuple[int, int]] | None = None
    # Optional ChunkConfig overrides for the chunk-and-ingest path
    # (ablation/speed switches; unknown keys are ignored server-side).
    chunk_options: dict[str, Any] | None = None


# Documents longer than this many lines (~3 pages) always take the
# chunk-and-ingest path: split into concept pages before node creation.
CHUNK_LINE_THRESHOLD = int(os.environ.get("WIKI_CHUNK_THRESHOLD_LINES", "300"))


class RecondBody(BaseModel):
    source_file: str


class CascadingUpdateBody(BaseModel):
    source_file: str


class IngestBody(BaseModel):
    path: str


# ============================================================================
# SETTINGS (editable at runtime)
# ============================================================================


# Secret fields are server-only (compile-time config). Redact before sending
# to any client so our API keys never reach the browser.
_SECRET_KEYS = {"chat_api_key", "embed_api_key", "rerank_api_key"}


def _redact(data: dict) -> dict:
    return {k: ("" if k in _SECRET_KEYS and v else v) for k, v in data.items()}


@app.get("/api/settings")
async def get_settings() -> dict:
    return _redact(_dump(_gateway().settings))


@app.get("/api/settings/schema")
async def get_settings_schema() -> dict:
    if hasattr(Settings, "model_json_schema"):
        return Settings.model_json_schema()
    if hasattr(Settings, "schema"):
        return Settings.schema()
    return {}


@app.put("/api/settings")
async def replace_settings(payload: dict[str, Any]) -> dict:
    try:
        new = Settings(**payload)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=api_error(str(exc), False, "invalid_settings"),
        )
    _gateway().update_settings(new)
    return _redact(_dump(new))


@app.patch("/api/settings")
async def patch_settings(payload: dict[str, Any]) -> dict:
    try:
        current_dict = _dump(_gateway().settings)
        # Ignore blanked-out secrets: clients receive redacted values ("") and
        # may echo them back; an empty secret must not wipe the real key.
        clean = {k: v for k, v in payload.items() if not (k in _SECRET_KEYS and not v)}
        current_dict.update(clean)
        new = Settings(**current_dict)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=api_error(str(exc), False, "invalid_settings"),
        )
    _gateway().update_settings(new)
    return _redact(_dump(new))


@app.post("/api/settings/reset")
async def reset_settings() -> dict:
    new = Settings.from_env()
    _gateway().update_settings(new)
    return _redact(_dump(new))


@app.post("/api/admin/resync")
async def resync() -> dict:
    try:
        job = await writes().enqueue("ensure_japanese_clusters", {})
    except RuntimeError as exc:
        raise HTTPException(
            status_code=429,
            detail=api_error(str(exc), True, "queue_full"),
        )
    return _job_response(job)


# ============================================================================
# READS
# ============================================================================


@app.get("/api/graph")
async def get_graph() -> dict:
    try:
        nodes, edges = await reads().get()
        return {"nodes": [_dump(n) for n in nodes], "edges": [_dump(e) for e in edges]}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=api_error(f"graph failed: {exc}", True, "search_failed"),
        ) from exc


@app.get("/api/health")
async def get_health(node_id: str | None = None) -> dict:
    try:
        stats = await reads().health(node_id)
        return _dump(stats)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=api_error(f"health failed: {exc}", True, "search_failed"),
        ) from exc


@app.get("/api/node/{node_id}")
async def read_node(node_id: str) -> dict:
    node = await reads().read_node(node_id)
    if node is None:
        raise HTTPException(
            status_code=404,
            detail=api_error("node not found", False, "not_found"),
        )
    return _dump(node)


@app.get("/api/node/{node_id}/links")
async def node_links(
    node_id: str,
    direction: str = "both",
    label: str | None = None,
    limit: int | None = None,
    compact: bool = False,
) -> list[dict]:
    if limit is not None:
        limit = max(1, min(limit, 200))
    try:
        pairs = await reads().follow_link(
            node_id, label=label, direction=direction, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=api_error(str(exc), False, "bad_request"),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=api_error(f"links failed: {exc}", True, "search_failed"),
        ) from exc
    return [
        {"edge": _dump(e), "node": _compact_node(n) if compact else _dump(n)}
        for e, n in pairs
    ]


@app.get("/api/search")
async def search(q: str, limit: int | None = None, compact: bool = False) -> list[dict]:
    try:
        if limit is not None:
            limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=api_error("limit must be an integer", False, "bad_request"),
        ) from exc
    try:
        nodes = await reads().search(q, limit)
        return [(_compact_node(n) if compact else _dump(n)) for n in nodes]
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=api_error(f"search failed: {exc}", True, "search_failed"),
        ) from exc


@app.get("/api/query")
async def query(query_type: str, value: str) -> dict:
    try:
        result = await reads().query(query_type, value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=api_error(str(exc), False, "bad_request"),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=api_error(f"query failed: {exc}", True, "search_failed"),
        ) from exc
    return _dump(result)


@app.post("/api/recon")
async def recon(payload: RecondBody) -> dict:
    # Read-only revision status: is this source doc new/changed/unchanged
    # relative to what was last ingested? Uses the Librarian for its doc
    # loaders but mutates nothing, so it stays off the write queue.
    source_file = _path_within_ingest_root(payload.source_file)
    if not Path(source_file).exists():
        raise HTTPException(
            status_code=404,
            detail=api_error(f"source does not exist: {source_file}", False, "bad_path"),
        )
    try:
        return await asyncio.to_thread(writes().recon, source_file)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=api_error(str(exc), False, "bad_path"),
        )


@app.post("/api/ask")
async def ask(payload: AskBody) -> dict:
    try:
        answer = await reads().ask(payload.question, overrides=payload.overrides)
        return _dump(answer)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=api_error(f"LLM request failed: {exc}", True, "llm_unavailable"),
        ) from exc


@app.post("/api/ask/stream")
async def ask_stream(payload: AskBody) -> StreamingResponse:
    loop = asyncio.get_running_loop()
    events: queue.Queue = queue.Queue()
    sentinel = object()
    run_id = str(uuid.uuid4())
    stop_event = threading.Event()

    async def run_agent():
        def emit(event: dict) -> None:
            events.put(event)

        try:
            answer = await reads().ask(
                payload.question,
                on_event=emit,
                overrides=payload.overrides,
                stop_event=stop_event,
            )
            events.put({"type": "answer", **_dump(answer)})
        except asyncio.CancelledError:
            stop_event.set()
            events.put({"type": "cancelled", "run_id": run_id})
            raise
        except AgentStopped:
            # The stop endpoint may land as AgentStopped (raised inside the
            # worker thread) instead of task cancellation; both are a clean
            # user-requested stop, not an error.
            events.put({"type": "cancelled", "run_id": run_id})
        except Exception as exc:
            events.put(
                {
                    "type": "error",
                    "message": str(exc),
                    "retryable": True,
                    "code": "agent_failed",
                }
            )
        finally:
            agent_runs.remove(run_id)
            events.put(sentinel)

    task = asyncio.create_task(run_agent())
    agent_runs.register(run_id, task, stop_event)

    async def stream():
        try:
            yield ": connected\n\n"
            yield f"data: {json.dumps({'type': 'run', 'run_id': run_id}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    event = await loop.run_in_executor(
                        SSE_EXECUTOR, lambda: events.get(timeout=15)
                    )
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                if event is sentinel:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            if not task.done():
                stop_event.set()
                task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/agent-runs/{run_id}/stop")
async def stop_agent_run(run_id: str) -> dict:
    if not agent_runs.stop(run_id):
        raise HTTPException(
            status_code=404,
            detail=api_error("agent run not found", False, "not_found"),
        )
    return {"run_id": run_id, "status": "stopping"}


# ============================================================================
# WRITES (all through the queue)
# ============================================================================


async def _enqueue(type_: str, payload: dict[str, Any]) -> dict:
    try:
        job = await writes().enqueue(type_, payload)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=429,
            detail=api_error(str(exc), True, "queue_full"),
        )
    return _job_response(job)


@app.put("/api/node/{node_id}")
async def update_node(node_id: str, payload: UpdateBody) -> dict:
    return await _enqueue("update_node", {"node_id": node_id, "body": payload.body})


@app.delete("/api/node/{node_id}")
async def delete_node(node_id: str) -> dict:
    return await _enqueue("delete_node", {"node_id": node_id})


# One job for the whole document, however many chunks it holds — a per-chunk
# delete would flood the bounded write queue.
@app.post("/api/document/delete")
async def delete_document(payload: DeleteDocumentBody) -> dict:
    if not payload.document_name and not payload.node_ids:
        raise HTTPException(
            status_code=400,
            detail=api_error("document_name or node_ids required", False, "bad_request"),
        )

    return await _enqueue(
        "delete_document",
        {
            "document_name": payload.document_name,
            "node_ids": payload.node_ids,
        },
    )


@app.post("/api/exogenous")
async def create_exogenous(payload: ExogenousBody) -> dict:
    return await _enqueue(
        "create_exogenous",
        {
            "body": payload.body,
            "source_node_ids": payload.source_node_ids,
            "origin": payload.origin,
            "question": payload.question,
        },
    )


@app.post("/api/document")
async def create_document(payload: DocumentBody) -> dict:
    if len(payload.body.splitlines()) > CHUNK_LINE_THRESHOLD:
        return await _enqueue(
            "chunk_and_ingest",
            {
                "body": payload.body,
                "title": payload.title,
                "document_name": payload.document_name,
                "source_path": payload.source_path,
            },
        )

    return await _enqueue(
        "create_document",
        {
            "body": payload.body,
            "title": payload.title,
            "document_name": payload.document_name,
            "source_path": payload.source_path,
            "source_ranges": payload.source_ranges,
        },
    )


@app.get("/api/assimilation")
async def assimilation_status() -> dict:
    """Background enrichment backlog still pending (summary/dedup/cascade/
    recluster). UI can show a 'graph assimilating (N)' badge."""
    return {"pending": writes().enrich_pending()}


@app.post("/api/recluster")
async def recluster(resolution: float = 1.0) -> dict:
    return await _enqueue("recluster", {"resolution": resolution})


@app.post("/api/cascading-update")
async def cascading_update(payload: CascadingUpdateBody) -> dict:
    source_file = _path_within_ingest_root(payload.source_file)
    return await _enqueue("cascading_update", {"source_file": source_file})


@app.post("/api/ingest")
async def ingest(payload: IngestBody) -> dict:
    path = _path_within_ingest_root(payload.path)
    if not Path(path).exists():
        raise HTTPException(
            status_code=404,
            detail=api_error(f"path not found: {path}", False, "bad_path"),
        )
    return await _enqueue("ingest_md_output", {"path": path})


# ============================================================================
# JOB STATUS
# ============================================================================


@app.get("/api/write-jobs")
async def list_write_jobs(status: str | None = None, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    jobs = writes().list_jobs(status=status, limit=limit)
    return [_job_response(j) for j in jobs]


@app.get("/api/write-jobs/{job_id}")
async def get_write_job(job_id: str) -> dict:
    job = writes().get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=api_error("job not found", False, "not_found"),
        )
    return _job_response(job)


@app.delete("/api/write-jobs/{job_id}")
async def cancel_write_job(job_id: str) -> dict:
    job = writes().get_job(job_id)
    status = job.status if job else None
    cancelled = writes().cancel_job(job_id)
    if not cancelled:
        raise HTTPException(
            status_code=409,
            detail=api_error("job not cancellable", False, "job_not_cancellable"),
        )
    return {"job_id": job_id, "status": "cancelling" if status == "running" else "cancelled"}


from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Serve admin frontend
@app.get("/admin")
@app.get("/admin/{path:path}")
def serve_admin(path: str = ""):
    return FileResponse(DIST_DIR / "admin.html")



app.mount(
    "/",
    StaticFiles(directory=DIST_DIR, html=True),
    name="frontend",
)
