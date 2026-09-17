"""Persistent, coalescing work queue for cheap mount metadata scans."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graph.workspace.project import Project, open_project, raw_name_for

from .ledger import load_ledger
from .scanner import IGNORED_DIRS, IGNORED_NAMES, SUPPORTED, _inside


@dataclass(frozen=True)
class Job:
    rel: str
    raw_rel: str
    operation: str
    lane: str
    version: int
    token: str


@contextmanager
def _connect(project: Project):
    conn = sqlite3.connect(project.queue_database, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sources (
                rel TEXT PRIMARY KEY,
                raw_rel TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                rel TEXT PRIMARY KEY,
                raw_rel TEXT NOT NULL,
                operation TEXT NOT NULL CHECK(operation IN ('add','update','delete')),
                lane TEXT NOT NULL CHECK(lane IN ('fast','slow')),
                version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','failed')),
                token TEXT NOT NULL DEFAULT '',
                available_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, lane, available_at, created_at);
            """
        )
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    files: dict[str, tuple[str, int, int]] = {}
    if not root.is_dir():
        return files
    for path in root.rglob("*"):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if path.name in IGNORED_NAMES or path.name.startswith("~$") or not path.is_file() or not _inside(path, root):
            continue
        if path.suffix.lower() not in SUPPORTED:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        raw_rel = (Path(rel).parent / raw_name_for(Path(rel).name)).as_posix()
        files[rel] = (raw_rel, stat.st_size, stat.st_mtime_ns)
    return files


def _enqueue(conn: sqlite3.Connection, rel: str, raw_rel: str, operation: str, now: float, settle_seconds: float) -> None:
    lane = "fast" if operation == "delete" else "slow"
    available_at = now if lane == "fast" else now + settle_seconds
    conn.execute(
        """INSERT INTO jobs(rel,raw_rel,operation,lane,available_at,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(rel) DO UPDATE SET raw_rel=excluded.raw_rel,
             operation=excluded.operation,lane=excluded.lane,version=jobs.version+1,
             status='queued',token='',available_at=excluded.available_at,
             updated_at=excluded.updated_at,error=''""",
        (rel, raw_rel, operation, lane, available_at, now, now),
    )


def scan(settings: Any, *, only: list[str] | None = None, settle_seconds: float = 10.0, force: bool = False) -> dict[str, list[str]]:
    """Stat the mount and atomically coalesce add/update/delete events."""
    project = open_project(settings)
    current = _snapshot(project.mount)
    wanted = {item.strip().lstrip("/") for item in only or ()}
    if wanted:
        current = {rel: row for rel, row in current.items() if rel in wanted}
    now = time.time()
    result: dict[str, list[str]] = {"added": [], "updated": [], "deleted": [], "cancelled": []}
    ledger = load_ledger(project.metadata / "pipeline.json")
    with _connect(project) as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = {
            str(row["rel"]): (str(row["raw_rel"]), int(row["size"]), int(row["mtime_ns"]))
            for row in conn.execute("SELECT rel,raw_rel,size,mtime_ns FROM sources")
            if not wanted or str(row["rel"]) in wanted
        }
        queued = {str(row[0]) for row in conn.execute("SELECT rel FROM jobs")}
        for rel, (raw_rel, size, mtime_ns) in current.items():
            old = previous.get(rel)
            if old is None:
                _enqueue(conn, rel, raw_rel, "add", now, settle_seconds)
                result["added"].append(rel)
            elif force or old[1:] != (size, mtime_ns):
                _enqueue(conn, rel, raw_rel, "update", now, settle_seconds)
                result["updated"].append(rel)
            elif rel not in queued:
                source = ledger.sources.get(rel, {})
                document = project.wiki_dir(raw_rel).relative_to(project.wiki).as_posix()
                incomplete = (
                    not source
                    or bool(source.get("last_error"))
                    or not project.raw_file(raw_rel).exists()
                    or not project.wiki_dir(raw_rel).exists()
                    or document not in ledger.published_documents
                )
                if incomplete:
                    _enqueue(conn, rel, raw_rel, "update" if source else "add", now, settle_seconds)
                    result["updated" if source else "added"].append(rel)
            if old is None or old != (raw_rel, size, mtime_ns):
                conn.execute(
                    "INSERT OR REPLACE INTO sources(rel,raw_rel,size,mtime_ns) VALUES(?,?,?,?)",
                    (rel, raw_rel, size, mtime_ns),
                )
        for rel in sorted(set(previous) - set(current)):
            raw_rel = previous[rel][0]
            conn.execute("DELETE FROM sources WHERE rel=?", (rel,))
            job = conn.execute("SELECT operation,lane FROM jobs WHERE rel=?", (rel,)).fetchone()
            known = rel in ledger.sources or any(str(row.get("raw_rel", "")) == raw_rel for row in ledger.published_documents.values())
            if job is not None and str(job["lane"]) == "slow" and not known:
                conn.execute("DELETE FROM jobs WHERE rel=?", (rel,))
                result["cancelled"].append(rel)
            elif known:
                _enqueue(conn, rel, raw_rel, "delete", now, settle_seconds)
                result["deleted"].append(rel)
    return result


def recover(project: Project) -> int:
    """Return work left running by a dead worker to the queue."""
    with _connect(project) as conn:
        cursor = conn.execute("UPDATE jobs SET status='queued',token='',updated_at=? WHERE status='running'", (time.time(),))
        return cursor.rowcount


def claim(project: Project, lane: str) -> list[Job]:
    token = uuid.uuid4().hex
    now = time.time()
    with _connect(project) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = list(conn.execute(
            "SELECT rel,raw_rel,operation,lane,version FROM jobs WHERE status='queued' AND lane=? AND available_at<=? ORDER BY created_at,rel",
            (lane, now),
        ))
        if rows:
            conn.executemany(
                "UPDATE jobs SET status='running',token=?,updated_at=? WHERE rel=? AND version=? AND status='queued'",
                [(token, now, row["rel"], row["version"]) for row in rows],
            )
    return [Job(str(row["rel"]), str(row["raw_rel"]), str(row["operation"]), str(row["lane"]), int(row["version"]), token) for row in rows]


def current(project: Project, jobs: list[Job]) -> bool:
    if not jobs:
        return True
    with _connect(project) as conn:
        return all(
            (row := conn.execute("SELECT version,status,token FROM jobs WHERE rel=?", (job.rel,)).fetchone()) is not None
            and int(row["version"]) == job.version and row["status"] == "running" and row["token"] == job.token
            for job in jobs
        )


def finish(project: Project, jobs: list[Job], *, error: str = "", retry: bool = False) -> None:
    status = "queued" if retry else "failed"
    with _connect(project) as conn:
        for job in jobs:
            if error:
                conn.execute(
                    "UPDATE jobs SET status=?,token='',updated_at=?,error=? WHERE rel=? AND version=? AND token=?",
                    (status, time.time(), error[:1000], job.rel, job.version, job.token),
                )
            else:
                conn.execute("DELETE FROM jobs WHERE rel=? AND version=? AND token=?", (job.rel, job.version, job.token))


def retry_failed(project: Project) -> int:
    with _connect(project) as conn:
        cursor = conn.execute("UPDATE jobs SET status='queued',error='',available_at=?,updated_at=? WHERE status='failed'", (time.time(), time.time()))
        return cursor.rowcount


def status(project: Project) -> list[dict[str, Any]]:
    with _connect(project) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT rel,raw_rel,operation,lane,status,version,error FROM jobs ORDER BY CASE lane WHEN 'fast' THEN 0 ELSE 1 END,created_at,rel"
        )]


def work_once(settings: Any, *, on_event: Any = None) -> dict[str, Any] | None:
    """Run the fast lane first, otherwise one coalesced slow batch."""
    from .pipeline import delete_sources, sync_once

    project = open_project(settings)
    jobs = claim(project, "fast")
    if not jobs:
        with _connect(project) as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE lane='fast' AND status='failed' LIMIT 1").fetchone():
                return None
        jobs = claim(project, "slow")
    if not jobs:
        return None
    if on_event is not None:
        on_event({"stage": "queue-claim", "lane": jobs[0].lane, "paths": [job.rel for job in jobs]})
    is_current = lambda: current(project, jobs)
    try:
        if jobs[0].lane == "fast":
            result = delete_sources(settings, {job.rel: job.raw_rel for job in jobs}, should_continue=is_current)
        else:
            result = sync_once(
                settings,
                only=[job.rel for job in jobs],
                force=True,
                resume=True,
                should_continue=is_current,
                include_pending=True,
                on_progress=on_event,
            )
    except Exception as exc:
        finish(project, jobs, error=f"{type(exc).__name__}: {exc}")
        return {"lane": jobs[0].lane, "jobs": len(jobs), "failures": [f"{type(exc).__name__}: {exc}"], "cancelled": False}
    if result.get("cancelled"):
        finish(project, jobs, error="superseded by a newer mount event", retry=True)
    else:
        completed = {
            str(row.get("path")) for row in result.get("done", [])
            if row.get("status") in {"added", "changed", "deleted"}
        }
        omitted = sorted(job.rel for job in jobs if job.rel not in completed)
        if omitted:
            result.setdefault("failures", []).append(f"pipeline omitted claimed paths: {omitted}")
    if result.get("cancelled"):
        pass
    elif result.get("failures"):
        finish(project, jobs, error="; ".join(result["failures"]))
    else:
        finish(project, jobs)
    result.pop("scan", None)
    return {"lane": jobs[0].lane, "jobs": len(jobs), "paths": [job.rel for job in jobs], **result}


@contextmanager
def worker_lock(project: Project):
    import fcntl

    path = project.metadata / "watch-worker.lock"
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("queue worker already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def serve(
    settings: Any,
    *,
    only: list[str] | None = None,
    interval: float = 10.0,
    growi_interval: float = 300.0,
    force: bool = False,
    on_event: Any = None,
) -> None:
    """Scan in a lightweight thread while the foreground worker runs long jobs."""
    from .pipeline import pull_growi_once

    project = open_project(settings)
    if only:
        wanted = {item.strip().lstrip("/") for item in only}
        missing = sorted(wanted - set(_snapshot(project.mount)))
        if missing:
            raise FileNotFoundError(f"not under mount/: {missing}")
    stop = threading.Event()

    def emit(kind: str, value: Any) -> None:
        if on_event is not None:
            on_event({"stage": kind, "result": value})

    def scan_loop() -> None:
        first = True
        while not stop.is_set():
            try:
                result = scan(settings, only=only, settle_seconds=interval, force=force and first)
                if first or any(result.get(key) for key in ("added", "updated", "deleted", "cancelled")):
                    emit("scan", result)
            except Exception as exc:
                emit("scan-error", f"{type(exc).__name__}: {exc}")
            first = False
            stop.wait(max(1.0, interval))

    with worker_lock(project):
        recover(project)
        scanner = threading.Thread(target=scan_loop, name="mount-metadata-scanner", daemon=True)
        scanner.start()
        next_growi = time.monotonic()
        try:
            while True:
                result = work_once(settings, on_event=on_event)
                if result is not None:
                    emit("queue", result)
                    continue
                if growi_interval > 0 and time.monotonic() >= next_growi:
                    emit("growi-pull", pull_growi_once(settings))
                    next_growi = time.monotonic() + growi_interval
                stop.wait(0.5)
        finally:
            stop.set()
            scanner.join(timeout=max(1.0, interval + 1.0))


__all__ = ["Job", "claim", "current", "finish", "recover", "retry_failed", "scan", "serve", "status", "work_once", "worker_lock"]
