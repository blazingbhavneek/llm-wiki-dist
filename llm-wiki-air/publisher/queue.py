"""Persistent, coalescing work queue for immutable mount snapshots."""

from __future__ import annotations

import copy
import hashlib
import io
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal

from docx import Document
from docx.table import Table

from graph.workspace.project import Project, open_project, raw_name_for
from graph.wiki.storage import read_json

from .history import (
    amend_candidate, candidate, candidate_is_clean, candidate_project, commit_candidate, ensure_repository, last_good, promote,
    prune_candidates, read_blob, reopen_candidate, restore_last_good, stage_blob,
)
from .ledger import load_ledger
from .scanner import IGNORED_DIRS, IGNORED_NAMES, SUPPORTED, _inside

SMALL_DOCUMENT_LINES = 1000
SMALL_DOCUMENT_RATIO = 0.25
LARGE_DOCUMENT_RATIO = 0.10
MOVE_SIMILARITY = 0.90


@dataclass(frozen=True)
class Job:
    rel: str
    raw_rel: str
    operation: str
    lane: str
    version: int
    token: str
    source_id: str = ""
    from_rel: str = ""
    target_blob_oid: str = ""
    target_sha256: str = ""
    classification: str = "none"
    base_commit: str = ""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    present = _columns(conn, table)
    for name, definition in definitions.items():
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


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
                mtime_ns INTEGER NOT NULL,
                source_id TEXT NOT NULL DEFAULT '',
                source_sha256 TEXT NOT NULL DEFAULT '',
                blob_oid TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS jobs (
                rel TEXT PRIMARY KEY,
                raw_rel TEXT NOT NULL,
                operation TEXT NOT NULL,
                lane TEXT NOT NULL CHECK(lane IN ('fast','slow')),
                version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','failed')),
                token TEXT NOT NULL DEFAULT '',
                available_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                from_rel TEXT NOT NULL DEFAULT '',
                target_blob_oid TEXT NOT NULL DEFAULT '',
                target_sha256 TEXT NOT NULL DEFAULT '',
                classification TEXT NOT NULL DEFAULT 'none',
                base_commit TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, lane, available_at, created_at);
            CREATE TABLE IF NOT EXISTS transactions (
                operation_id TEXT PRIMARY KEY,
                candidate_commit TEXT NOT NULL DEFAULT '',
                base_commit TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transaction_revisions (
                operation_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                PRIMARY KEY(operation_id,page_id,revision_id)
            );
            """
        )
        _add_columns(conn, "sources", {
            "source_id": "TEXT NOT NULL DEFAULT ''",
            "source_sha256": "TEXT NOT NULL DEFAULT ''",
            "blob_oid": "TEXT NOT NULL DEFAULT ''",
        })
        _add_columns(conn, "jobs", {
            "source_id": "TEXT NOT NULL DEFAULT ''",
            "from_rel": "TEXT NOT NULL DEFAULT ''",
            "target_blob_oid": "TEXT NOT NULL DEFAULT ''",
            "target_sha256": "TEXT NOT NULL DEFAULT ''",
            "classification": "TEXT NOT NULL DEFAULT 'none'",
            "base_commit": "TEXT NOT NULL DEFAULT ''",
        })
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


def _docx_text(payload: bytes) -> tuple[list[str], list[str]]:
    document = Document(io.BytesIO(payload))
    lines: list[str] = []
    outline: list[str] = []
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                cells = ["\n".join(p.text for p in cell.paragraphs if p.text.strip()) for cell in row.cells]
                if any(cells):
                    lines.append("\t".join(cells))
            continue
        text = block.text
        if text.strip():
            lines.append(text)
            if str(block.style.name).casefold() in {"title", "heading 1", "見出し 1"}:
                outline.append(text)
    return lines, outline


def _classification(
    old: bytes,
    new: bytes,
    suffix: str,
) -> dict[str, Any]:
    if suffix != ".docx":
        return {"kind": "large", "ratio": 1.0, "hunks": 1, "reason": "unsupported-format"}
    try:
        old_lines, old_outline = _docx_text(old)
        new_lines, new_outline = _docx_text(new)
        opcodes = SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()
        changed = [(a, b, c, d) for tag, a, b, c, d in opcodes if tag != "equal"]
        changed_lines = sum(max(b - a, d - c) for a, b, c, d in changed)
        ratio = changed_lines / max(len(old_lines), len(new_lines), 1)
        threshold = SMALL_DOCUMENT_RATIO if max(len(old_lines), len(new_lines)) < SMALL_DOCUMENT_LINES else LARGE_DOCUMENT_RATIO
        if old_outline != new_outline:
            kind, reason = "large", "outline-changed"
        elif ratio > threshold:
            kind, reason = "large", "ratio"
        else:
            kind, reason = "small", "below-threshold"
        return {"kind": kind, "ratio": round(ratio, 6), "hunks": len(changed), "reason": reason}
    except Exception as exc:
        return {"kind": "large", "ratio": 1.0, "hunks": 0, "reason": f"classification-failed:{type(exc).__name__}"}


def _docx_similarity(old: bytes, new: bytes) -> float:
    try:
        return SequenceMatcher(None, _docx_text(old)[0], _docx_text(new)[0], autojunk=False).ratio()
    except Exception:
        return 0.0


def _enqueue(
    conn: sqlite3.Connection,
    rel: str,
    raw_rel: str,
    operation: str,
    now: float,
    settle_seconds: float,
    *,
    source_id: str = "",
    from_rel: str = "",
    target_blob_oid: str = "",
    target_sha256: str = "",
    classification: str = "none",
    base_commit: str = "",
) -> None:
    lane = "fast" if operation == "delete" else "slow"
    available_at = now if lane == "fast" else now + settle_seconds
    statement = """INSERT INTO jobs(rel,raw_rel,operation,lane,available_at,created_at,updated_at,
                                     source_id,from_rel,target_blob_oid,target_sha256,classification,base_commit)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(rel) DO UPDATE SET raw_rel=excluded.raw_rel,
                      operation=excluded.operation,lane=excluded.lane,version=jobs.version+1,
                      status='queued',token='',available_at=excluded.available_at,
                      updated_at=excluded.updated_at,error='',source_id=excluded.source_id,
                      from_rel=excluded.from_rel,target_blob_oid=excluded.target_blob_oid,
                      target_sha256=excluded.target_sha256,classification=excluded.classification,
                      base_commit=excluded.base_commit"""
    values = (
        rel, raw_rel, operation, lane, available_at, now, now, source_id, from_rel,
        target_blob_oid, target_sha256, classification, base_commit,
    )
    try:
        conn.execute(statement, values)
    except sqlite3.IntegrityError:
        if operation != "move":
            raise
        # Legacy queues may still constrain operation to add/update/delete.
        conn.execute(statement, values[:2] + ("update",) + values[3:])


def _source_identity(
    project: Project,
    ledger: Any,
    rel: str,
    previous: sqlite3.Row | None = None,
    digest: str = "",
) -> str:
    identities = read_json(project.metadata / "source-identities.json", default={})
    tombstone = dict((identities.get("tombstones") or {}).get(rel) or {})
    restored = str(tombstone.get("source_id") or "") if digest and tombstone.get("source_sha256") == digest else ""
    return str(
        (previous["source_id"] if previous is not None and previous["source_id"] else "")
        or ledger.sources.get(rel, {}).get("source_id")
        or restored
        or uuid.uuid4()
    )


def scan(
    settings: Any,
    *,
    only: list[str] | None = None,
    settle_seconds: float = 10.0,
    force: bool = False,
    verify_content: bool = False,
) -> dict[str, Any]:
    """Stat the mount, stage changed bytes once, and coalesce desired state."""
    project = open_project(settings)
    base_commit = ensure_repository(project)
    current = _snapshot(project.mount)
    wanted = {item.strip().lstrip("/") for item in only or ()}
    if wanted:
        current = {rel: row for rel, row in current.items() if rel in wanted}
    now = time.time()
    result: dict[str, Any] = {"added": [], "updated": [], "deleted": [], "cancelled": [], "moved": [], "classification": {}}
    ledger = load_ledger(project.metadata / "pipeline.json")
    with _connect(project) as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = {
            str(row["rel"]): row
            for row in conn.execute("SELECT * FROM sources")
            if not wanted or str(row["rel"]) in wanted
        }
        previous_ids = {str(row["source_id"]) for row in previous.values() if row["source_id"]}
        for rel, source in ledger.sources.items():
            if (
                rel in previous or (wanted and rel not in wanted)
                or str(source.get("source_id") or "") in previous_ids
            ):
                continue
            conn.execute(
                """INSERT OR IGNORE INTO sources
                   (rel,raw_rel,size,mtime_ns,source_id,source_sha256,blob_oid) VALUES(?,?,?,?,?,?,?)""",
                (
                    rel, str(source.get("raw_rel") or raw_name_for(Path(rel).name)),
                    int(source.get("size") or 0), int(source.get("mtime_ns") or 0),
                    str(source.get("source_id") or ""), str(source.get("source_sha256") or ""),
                    str(source.get("source_blob_oid") or ""),
                ),
            )
            previous[rel] = conn.execute("SELECT * FROM sources WHERE rel=?", (rel,)).fetchone()
        ledger_rel_by_id = {
            str(source.get("source_id")): rel
            for rel, source in ledger.sources.items()
            if source.get("source_id")
        }
        for rel, row in list(previous.items()):
            source = ledger.sources.get(rel, {})
            if source and (not row["source_id"] or not row["source_sha256"] or not row["blob_oid"]):
                conn.execute(
                    "UPDATE sources SET source_id=?,source_sha256=?,blob_oid=? WHERE rel=?",
                    (
                        str(source.get("source_id") or ""), str(source.get("source_sha256") or ""),
                        str(source.get("source_blob_oid") or ""), rel,
                    ),
                )
                previous[rel] = conn.execute("SELECT * FROM sources WHERE rel=?", (rel,)).fetchone()
        queued = {str(row[0]) for row in conn.execute("SELECT rel FROM jobs")}
        changed_paths = {
            rel for rel, (_raw, size, mtime_ns) in current.items()
            if rel not in previous or force or verify_content
            or (int(previous[rel]["size"]), int(previous[rel]["mtime_ns"])) != (size, mtime_ns)
        }
        staged: dict[str, Any] = {}
        for rel in sorted(changed_paths):
            try:
                staged[rel] = stage_blob(project, project.mount / rel)
            except (FileNotFoundError, OSError, RuntimeError):
                continue

        disappeared = set(previous) - set(current)
        appeared = set(current) - set(previous)
        old_by_hash: dict[str, list[str]] = {}
        new_by_hash: dict[str, list[str]] = {}
        for rel in disappeared:
            digest = str(previous[rel]["source_sha256"] or ledger.sources.get(rel, {}).get("source_sha256") or "")
            if digest:
                old_by_hash.setdefault(digest, []).append(rel)
        for rel in appeared:
            if rel in staged:
                new_by_hash.setdefault(staged[rel].sha256, []).append(rel)
        moves: dict[str, str] = {}
        for digest in set(old_by_hash) & set(new_by_hash):
            if len(old_by_hash[digest]) == len(new_by_hash[digest]) == 1:
                old_rel, new_rel = old_by_hash[digest][0], new_by_hash[digest][0]
                source_id = str(previous[old_rel]["source_id"] or "")
                if source_id in ledger_rel_by_id and Path(old_rel).suffix.lower() == Path(new_rel).suffix.lower():
                    moves[old_rel] = new_rel
        fuzzy: dict[str, list[str]] = {}
        reverse_fuzzy: dict[str, list[str]] = {}
        for old_rel in disappeared - set(moves):
            old_oid = str(previous[old_rel]["blob_oid"] or ledger.sources.get(old_rel, {}).get("source_blob_oid") or "")
            source_id = str(previous[old_rel]["source_id"] or "")
            if source_id not in ledger_rel_by_id or Path(old_rel).suffix.lower() != ".docx" or not old_oid:
                continue
            old_payload = read_blob(project, old_oid)
            for new_rel in appeared - set(moves.values()):
                if Path(new_rel).suffix.lower() != ".docx" or new_rel not in staged:
                    continue
                if _docx_similarity(old_payload, read_blob(project, staged[new_rel].oid)) >= MOVE_SIMILARITY:
                    fuzzy.setdefault(old_rel, []).append(new_rel)
                    reverse_fuzzy.setdefault(new_rel, []).append(old_rel)
        for old_rel, candidates in fuzzy.items():
            if len(candidates) == 1 and len(reverse_fuzzy.get(candidates[0], [])) == 1:
                moves[old_rel] = candidates[0]
        for old_rel, new_rel in sorted(moves.items()):
            old = previous[old_rel]
            blob = staged[new_rel]
            source_id = _source_identity(project, ledger, old_rel, old, blob.sha256)
            origin = ledger_rel_by_id.get(source_id, old_rel)
            base_source = ledger.sources.get(origin, {})
            raw_rel = current[new_rel][0]
            conn.execute("DELETE FROM sources WHERE rel=?", (old_rel,))
            conn.execute("DELETE FROM jobs WHERE rel=? OR (source_id<>'' AND source_id=?)", (old_rel, source_id))
            conn.execute(
                "INSERT OR REPLACE INTO sources(rel,raw_rel,size,mtime_ns,source_id,source_sha256,blob_oid) VALUES(?,?,?,?,?,?,?)",
                (new_rel, raw_rel, current[new_rel][1], current[new_rel][2], source_id, blob.sha256, blob.oid),
            )
            old_oid = str(base_source.get("source_blob_oid") or old["blob_oid"] or "")
            decision = _classification(
                read_blob(project, old_oid), read_blob(project, blob.oid), Path(new_rel).suffix.lower(),
            )
            if new_rel == origin:
                if blob.sha256 == str(base_source.get("source_sha256") or ""):
                    result["cancelled"].append(old_rel)
                else:
                    _enqueue(conn, new_rel, raw_rel, "update", now, settle_seconds, source_id=source_id,
                             target_blob_oid=blob.oid, target_sha256=blob.sha256,
                             classification=str(decision["kind"]), base_commit=base_commit)
                    result["updated"].append(new_rel)
                    result["classification"][new_rel] = decision
                continue
            _enqueue(conn, new_rel, raw_rel, "move", now, settle_seconds, source_id=source_id,
                     from_rel=origin, target_blob_oid=blob.oid, target_sha256=blob.sha256,
                     classification=str(decision["kind"]), base_commit=base_commit)
            result["moved"].append({"from": old_rel, "to": new_rel})
            if blob.sha256 != str(old["source_sha256"] or ""):
                result["classification"][new_rel] = decision

        moved_old, moved_new = set(moves), set(moves.values())
        for rel, (raw_rel, size, mtime_ns) in current.items():
            if rel in moved_new:
                continue
            old = previous.get(rel)
            blob = staged.get(rel)
            content_changed = bool(blob and blob.sha256 != str(old["source_sha256"] if old is not None else ""))
            changed = old is None or force or content_changed
            if changed and blob is None:
                continue
            if changed:
                source_id = _source_identity(project, ledger, rel, old, blob.sha256)
                origin = ledger_rel_by_id.get(source_id, "")
                operation = "move" if origin and origin != rel else "update" if old is not None else "add"
                classification = {"kind": "none", "ratio": 0.0, "hunks": 0, "reason": "new"}
                if operation in {"update", "move"} or old is not None:
                    active = conn.execute("SELECT target_blob_oid,status FROM jobs WHERE rel=?", (rel,)).fetchone()
                    old_oid = str(
                        (active["target_blob_oid"] if active is not None and active["status"] == "running" else "")
                        or ledger.sources.get(origin or rel, {}).get("source_blob_oid")
                        or (old["blob_oid"] if old is not None else "")
                    )
                    classification = _classification(
                        read_blob(project, old_oid), read_blob(project, blob.oid), Path(rel).suffix.lower(),
                    )
                    result["classification"][rel] = classification
                _enqueue(conn, rel, raw_rel, operation, now, settle_seconds, source_id=source_id,
                         from_rel=origin if operation == "move" else "",
                         target_blob_oid=blob.oid, target_sha256=blob.sha256,
                         classification=str(classification["kind"]), base_commit=base_commit)
                result["updated" if old is not None else "added"].append(rel)
                conn.execute(
                    "INSERT OR REPLACE INTO sources(rel,raw_rel,size,mtime_ns,source_id,source_sha256,blob_oid) VALUES(?,?,?,?,?,?,?)",
                    (rel, raw_rel, size, mtime_ns, source_id, blob.sha256, blob.oid),
                )
            else:
                if blob is not None:
                    conn.execute(
                        "UPDATE sources SET size=?,mtime_ns=?,source_sha256=?,blob_oid=? WHERE rel=?",
                        (size, mtime_ns, blob.sha256, blob.oid, rel),
                    )
                if rel in queued:
                    continue
                source = ledger.sources.get(rel, {})
                if source and (not old["source_id"] or not old["source_sha256"] or not old["blob_oid"]):
                    conn.execute(
                        "UPDATE sources SET source_id=?,source_sha256=?,blob_oid=? WHERE rel=?",
                        (
                            str(source.get("source_id") or ""),
                            str(source.get("source_sha256") or ""),
                            str(source.get("source_blob_oid") or ""),
                            rel,
                        ),
                    )
                document = project.wiki_dir(raw_rel).relative_to(project.wiki).as_posix()
                incomplete = (
                    not source or bool(source.get("last_error")) or not project.raw_file(raw_rel).exists()
                    or not project.wiki_dir(raw_rel).exists() or document not in ledger.published_documents
                )
                if incomplete:
                    blob_oid = str(old["blob_oid"] or "")
                    digest = str(old["source_sha256"] or "")
                    if not blob_oid:
                        try:
                            staged_blob = stage_blob(project, project.mount / rel)
                        except (FileNotFoundError, OSError, RuntimeError):
                            continue
                        blob_oid, digest = staged_blob.oid, staged_blob.sha256
                    operation = "update" if source else "add"
                    _enqueue(conn, rel, raw_rel, operation, now, settle_seconds,
                             source_id=_source_identity(project, ledger, rel, old, digest), target_blob_oid=blob_oid,
                             target_sha256=digest, base_commit=base_commit)
                    result["updated" if source else "added"].append(rel)

        for rel in sorted((set(previous) - set(current)) - moved_old):
            old = previous[rel]
            source_id = _source_identity(project, ledger, rel, old)
            origin = ledger_rel_by_id.get(source_id, rel)
            raw_rel = str(ledger.sources.get(origin, {}).get("raw_rel") or old["raw_rel"])
            conn.execute("DELETE FROM sources WHERE rel=?", (rel,))
            job = conn.execute("SELECT operation,lane FROM jobs WHERE rel=?", (rel,)).fetchone()
            known = origin in ledger.sources or any(
                str(row.get("raw_rel", "")) == raw_rel for row in ledger.published_documents.values()
            )
            if job is not None and str(job["lane"]) == "slow" and not known:
                conn.execute("DELETE FROM jobs WHERE rel=?", (rel,))
                result["cancelled"].append(rel)
            elif known:
                conn.execute("DELETE FROM jobs WHERE rel=? OR (source_id<>'' AND source_id=?)", (rel, source_id))
                _enqueue(conn, origin, raw_rel, "delete", now, settle_seconds,
                         source_id=source_id, base_commit=base_commit)
                result["deleted"].append(rel)
    return result


def _job_from_row(row: sqlite3.Row) -> Job:
    operation = "move" if row["from_rel"] else str(row["operation"])
    return Job(
        str(row["rel"]), str(row["raw_rel"]), operation, str(row["lane"]), int(row["version"]),
        str(row["token"]), str(row["source_id"]), str(row["from_rel"]), str(row["target_blob_oid"]),
        str(row["target_sha256"]), str(row["classification"]), str(row["base_commit"]),
    )


def recover(project: Project, settings: Any | None = None) -> int:
    """Restore interrupted publication, then return claimed work to the queue."""
    ensure_repository(project)
    with _connect(project) as conn:
        transactions = list(conn.execute("SELECT * FROM transactions ORDER BY started_at"))
        jobs = [_job_from_row(row) for row in conn.execute("SELECT * FROM jobs")]
    if any(str(row["phase"]) == "publishing" for row in transactions) and settings is None:
        raise RuntimeError("publisher settings are required to recover an interrupted GROWI publication")
    if transactions:
        from .pipeline import candidate_publication_complete, restore_publication

        for transaction in transactions:
            operation_id = str(transaction["operation_id"])
            commit = str(transaction["candidate_commit"] or "")
            phase = str(transaction["phase"])
            staged = candidate_project(project, operation_id)
            if phase == "restored":
                if not commit or last_good(project) != commit:
                    raise RuntimeError(f"cannot recover publication {operation_id}: restored commit is missing")
                _finish_transaction(project, operation_id)
                continue
            if phase == "restoring" and last_good(project) != str(transaction["base_commit"]):
                if not candidate_publication_complete(settings, project):
                    raise RuntimeError(f"cannot recover publication {operation_id}: restored state is incomplete")
                _finish_transaction(project, operation_id)
                continue
            if commit and last_good(project) == commit:
                if staged.root.exists():
                    promote(project, staged, commit)
                else:
                    restore_last_good(project)
                with _connect(project) as conn:
                    conn.execute(
                        "DELETE FROM jobs WHERE status='running' AND base_commit=?",
                        (str(transaction["base_commit"]),),
                    )
                _finish_transaction(project, operation_id)
                continue
            if last_good(project) != str(transaction["base_commit"]):
                raise RuntimeError(f"cannot recover publication {operation_id}: last-good changed")
            if phase in {"publishing", "restoring"}:
                if not staged.root.exists():
                    if not commit:
                        raise RuntimeError(f"cannot recover publication {operation_id}: candidate commit is missing")
                    staged = reopen_candidate(project, operation_id, commit)
                if commit and candidate_is_clean(staged, commit) and candidate_publication_complete(settings, staged):
                    promote(project, staged, commit)
                    with _connect(project) as conn:
                        conn.execute(
                            "DELETE FROM jobs WHERE status='running' AND base_commit=?",
                            (str(transaction["base_commit"]),),
                        )
                else:
                    _transaction(project, operation_id, str(transaction["base_commit"]), "restoring", commit)
                    restored = restore_publication(
                        settings, staged, jobs, known_revisions=_transaction_revisions(project, operation_id)
                    )
                    _transaction(project, operation_id, str(transaction["base_commit"]), "restored", restored)
            _finish_transaction(project, operation_id)
    prune_candidates(project)
    if settings is not None and getattr(settings, "wiki_linker_enabled", True) and not project.linker_database.exists():
        from graph.linker.catalog import Catalog

        catalog = Catalog.open(project.linker_database, mode=str(getattr(settings, "wiki_linker_mode", "legacy")))
        try:
            catalog.sync_from_planning(project)
        finally:
            catalog.close()
    with _connect(project) as conn:
        cursor = conn.execute(
            """UPDATE jobs SET status='queued',token='',error='',updated_at=?
               WHERE status='running' OR (status='failed' AND error LIKE 'recovery required:%')""",
            (time.time(),),
        )
    return cursor.rowcount


def claim(project: Project, lane: str) -> list[Job]:
    token = uuid.uuid4().hex
    now = time.time()
    base_commit = last_good(project)
    with _connect(project) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = list(conn.execute(
            """SELECT rel,raw_rel,CASE WHEN from_rel<>'' THEN 'move' ELSE operation END AS operation,
                      lane,version,source_id,from_rel,target_blob_oid,target_sha256,classification,base_commit
               FROM jobs WHERE status='queued' AND lane=? AND available_at<=? ORDER BY created_at,rel""",
            (lane, now),
        ))
        if rows:
            conn.executemany(
                "UPDATE jobs SET status='running',token=?,base_commit=?,updated_at=? WHERE rel=? AND version=? AND status='queued'",
                [(token, base_commit, now, row["rel"], row["version"]) for row in rows],
            )
    return [_job_from_row(dict(row) | {"token": token, "base_commit": base_commit}) for row in rows]


def supersession(project: Project, jobs: list[Job]) -> Literal["continue", "cancel"]:
    if not jobs:
        return "continue"
    with _connect(project) as conn:
        for job in jobs:
            row = conn.execute(
                "SELECT version,status,token,operation,from_rel,classification FROM jobs WHERE rel=?", (job.rel,)
            ).fetchone()
            if row is None:
                return "cancel"
            if int(row["version"]) == job.version and row["status"] == "running" and row["token"] == job.token:
                continue
            operation = "move" if row["from_rel"] else str(row["operation"])
            if job.operation in {"delete", "move"} or operation != "update" or str(row["classification"]) != "small":
                return "cancel"
    return "continue"


def current(project: Project, jobs: list[Job]) -> bool:
    return supersession(project, jobs) == "continue"


def finish(project: Project, jobs: list[Job], *, error: str = "", retry: bool = False) -> None:
    status_value = "queued" if retry else "failed"
    with _connect(project) as conn:
        for job in jobs:
            if error:
                conn.execute(
                    "UPDATE jobs SET status=?,token='',updated_at=?,error=? WHERE rel=? AND version=? AND token=?",
                    (status_value, time.time(), error[:1000], job.rel, job.version, job.token),
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
            """SELECT rel,raw_rel,CASE WHEN from_rel<>'' THEN 'move' ELSE operation END AS operation,
                      lane,status,version,error,source_id,from_rel,target_sha256,classification,base_commit
               FROM jobs ORDER BY CASE lane WHEN 'fast' THEN 0 ELSE 1 END,created_at,rel"""
        )]


def _candidate_settings(settings: Any, staged: Project) -> Any:
    updates = {"data_root": str(staged.root.parent), "target_name": staged.root.name, "mount_path": str(staged.mount)}
    if hasattr(settings, "model_copy"):
        return settings.model_copy(update=updates)
    clone = copy.copy(settings)
    for name, value in updates.items():
        setattr(clone, name, value)
    return clone


def _commit_metadata(staged: Project, jobs: list[Job], base: str, operation_id: str) -> dict[str, str]:
    output = hashlib.sha256()
    for path in sorted(staged.wiki.rglob("*")) if staged.wiki.exists() else ():
        if path.is_file():
            output.update(path.relative_to(staged.wiki).as_posix().encode())
            output.update(hashlib.sha256(path.read_bytes()).digest())
    return {
        "base": base,
        "classification": ",".join(sorted({job.classification for job in jobs})),
        "input_sha256": ",".join(job.target_sha256 for job in jobs if job.target_sha256),
        "operation": operation_id,
        "output_sha256": output.hexdigest(),
        "source_ids": ",".join(job.source_id for job in jobs if job.source_id),
    }


def _transaction(project: Project, operation_id: str, base: str, phase: str, commit: str = "") -> None:
    with _connect(project) as conn:
        conn.execute(
            """INSERT INTO transactions(operation_id,candidate_commit,base_commit,phase,started_at)
               VALUES(?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET
               candidate_commit=excluded.candidate_commit,phase=excluded.phase""",
            (operation_id, commit, base, phase, time.time()),
        )


def _record_revision(project: Project, operation_id: str, page: Any) -> None:
    if not getattr(page, "page_id", "") or not getattr(page, "revision_id", ""):
        return
    with _connect(project) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO transaction_revisions(operation_id,page_id,revision_id) VALUES(?,?,?)",
            (operation_id, str(page.page_id), str(page.revision_id)),
        )


def _transaction_revisions(project: Project, operation_id: str) -> dict[str, set[str]]:
    with _connect(project) as conn:
        rows = conn.execute(
            "SELECT page_id,revision_id FROM transaction_revisions WHERE operation_id=?", (operation_id,)
        )
        revisions: dict[str, set[str]] = {}
        for row in rows:
            revisions.setdefault(str(row["page_id"]), set()).add(str(row["revision_id"]))
        return revisions


def _finish_transaction(project: Project, operation_id: str) -> None:
    with _connect(project) as conn:
        conn.execute("DELETE FROM transaction_revisions WHERE operation_id=?", (operation_id,))
        conn.execute("DELETE FROM transactions WHERE operation_id=?", (operation_id,))


def _work_once_locked(settings: Any, *, on_event: Any = None) -> dict[str, Any] | None:
    """Run one immutable candidate from last-good, then publish and promote it."""
    from .pipeline import delete_sources, move_sources, restore_publication, sync_once

    project = open_project(settings)
    ensure_repository(project)
    with _connect(project) as conn:
        unfinished = conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone() is not None
    if unfinished:
        recover(project, settings)
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
    operation_id = "op-" + uuid.uuid4().hex
    base = last_good(project)
    _transaction(project, operation_id, base, "building")
    is_current = lambda: supersession(project, jobs) == "continue"
    result: dict[str, Any]
    publishing = False
    try:
        with candidate(project, operation_id) as staged:
            for job in jobs:
                if job.target_blob_oid:
                    target = staged.mount / job.rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(read_blob(project, job.target_blob_oid))
            staged_settings = _candidate_settings(settings, staged)
            prepared_commit = ""

            def prepare_publish() -> None:
                nonlocal prepared_commit, publishing
                if prepared_commit:
                    return
                prepared_commit = commit_candidate(
                    staged, f"publish {operation_id}", _commit_metadata(staged, jobs, base, operation_id)
                )
                _transaction(project, operation_id, base, "prepared", prepared_commit)
                if on_event:
                    on_event({"stage": "history", "step": "candidate", "base_commit": base, "commit": prepared_commit})

            def begin_publish() -> None:
                nonlocal publishing
                if publishing:
                    return
                _transaction(project, operation_id, base, "publishing", prepared_commit)
                publishing = True

            record_revision = lambda page: _record_revision(project, operation_id, page)
            live_ledger = load_ledger(project.metadata / "pipeline.json")
            details: dict[str, dict[str, Any]] = {}
            for job in jobs:
                decision = {"kind": job.classification, "ratio": 0.0, "hunks": 0, "reason": "queued"}
                previous = live_ledger.sources.get(job.from_rel or job.rel, {})
                old_oid = str(previous.get("source_blob_oid") or "")
                if old_oid and job.target_blob_oid and job.operation != "move":
                    decision = _classification(
                        read_blob(project, old_oid), read_blob(project, job.target_blob_oid), Path(job.rel).suffix.lower(),
                    )
                if on_event and decision["kind"] in {"small", "large"}:
                    on_event({"stage": "diff-classified", "path": job.rel, "decision": decision["kind"],
                              "ratio": decision["ratio"], "hunks": decision["hunks"], "reason": decision["reason"]})
                details[job.rel] = {
                    "source_id": job.source_id,
                    "source_blob_oid": job.target_blob_oid,
                    "source_sha256": job.target_sha256,
                    "classification": decision["kind"],
                    "from_rel": job.from_rel,
                }
            if jobs[0].lane == "fast":
                result = delete_sources(staged_settings, {job.rel: job.raw_rel for job in jobs},
                                        should_continue=is_current,
                                        prepare_publish=prepare_publish, begin_publish=begin_publish,
                                        on_revision=record_revision)
            else:
                move_jobs = [job for job in jobs if job.operation == "move"]
                normal_jobs = [
                    job for job in jobs
                    if job.operation != "move"
                    or job.target_sha256 != str(live_ledger.sources.get(job.from_rel, {}).get("source_sha256") or "")
                ]
                result = {"run_id": operation_id, "done": [], "failures": [], "cancelled": False}
                if move_jobs:
                    moved = move_sources(staged_settings, move_jobs, should_continue=is_current,
                                         on_progress=on_event, prepare_publish=prepare_publish,
                                         begin_publish=begin_publish, on_revision=record_revision)
                    result["done"].extend(moved["done"])
                    result["failures"].extend(moved["failures"])
                    result["cancelled"] = moved["cancelled"]
                if normal_jobs and not result["failures"] and not result["cancelled"]:
                    synced = sync_once(
                        staged_settings, only=[job.rel for job in normal_jobs], force=True, resume=True,
                        should_continue=is_current, include_pending=True, on_progress=on_event,
                        source_details=details,
                        prepare_publish=prepare_publish,
                        begin_publish=begin_publish,
                        on_revision=record_revision,
                    )
                    result["run_id"] = synced["run_id"]
                    result["done"].extend(synced["done"])
                    result["failures"].extend(synced["failures"])
                    result["cancelled"] = synced["cancelled"]
            if not result.get("cancelled") and not result.get("failures"):
                commit = amend_candidate(staged) if prepared_commit else commit_candidate(
                    staged, f"publish {operation_id}", _commit_metadata(staged, jobs, base, operation_id)
                )
                _transaction(project, operation_id, base, "publishing", commit)
                promote(project, staged, commit)
                if on_event:
                    on_event({"stage": "history", "step": "promote", "base_commit": base, "commit": commit})
            elif result.get("failures") and publishing:
                _transaction(project, operation_id, base, "restoring", prepared_commit)
                restored = restore_publication(
                    settings, staged, jobs, known_revisions=_transaction_revisions(project, operation_id)
                )
                _transaction(project, operation_id, base, "restored", restored)
                if on_event:
                    on_event({"stage": "history", "step": "rollback", "base_commit": base})
    except Exception as exc:
        prefix = "recovery required: " if publishing else ""
        finish(project, jobs, error=f"{prefix}{type(exc).__name__}: {exc}")
        if not publishing:
            _finish_transaction(project, operation_id)
        return {"lane": jobs[0].lane, "jobs": len(jobs), "failures": [f"{type(exc).__name__}: {exc}"], "cancelled": False}
    if result.get("cancelled"):
        finish(project, jobs, error="superseded by a newer mount event", retry=True)
    else:
        completed = {
            str(row.get("path")) for row in result.get("done", [])
            if row.get("status") in {"added", "changed", "deleted", "moved"}
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
    _finish_transaction(project, operation_id)
    result.pop("scan", None)
    return {"lane": jobs[0].lane, "jobs": len(jobs), "paths": [job.rel for job in jobs], **result}


def work_once(settings: Any, *, on_event: Any = None) -> dict[str, Any] | None:
    """Serialize candidate promotion with direct live-project operations."""
    from .pipeline import _lock

    with _lock(open_project(settings)):
        return _work_once_locked(settings, on_event=on_event)


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
    """Scan cheaply while the foreground worker runs long jobs."""
    from .pipeline import _lock, pull_growi_once

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
                if first or any(result.get(key) for key in ("added", "updated", "deleted", "cancelled", "moved")):
                    emit("scan", result)
            except Exception as exc:
                emit("scan-error", f"{type(exc).__name__}: {exc}")
            first = False
            stop.wait(max(1.0, interval))

    with worker_lock(project):
        with _lock(project):
            recover(project, settings)
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
                    scan(settings, only=only, settle_seconds=interval, verify_content=True)
                    next_growi = time.monotonic() + growi_interval
                stop.wait(0.5)
        finally:
            stop.set()
            scanner.join(timeout=max(1.0, interval + 1.0))


__all__ = [
    "Job", "claim", "current", "finish", "recover", "retry_failed", "scan", "serve",
    "status", "supersession", "work_once", "worker_lock",
]
