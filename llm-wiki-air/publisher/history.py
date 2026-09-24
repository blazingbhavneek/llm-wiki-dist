"""Git-backed checkpoints for one publisher project."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from graph.wiki.storage import read_json, write_json_atomic
from graph.workspace.project import Project

LAST_GOOD_REF = "refs/llm-wiki/last-good"
GITIGNORE = """mount/
metadata/watch-queue.sqlite*
metadata/wiki-linker.sqlite*
metadata/*.lock
metadata/work/
metadata/candidates/
*.log
"""


@dataclass(frozen=True)
class Blob:
    oid: str
    sha256: str
    size: int
    suffix: str


def _git(project: Project, *args: str, input: bytes | None = None, text: bool = True) -> str | bytes:
    binary = input is not None or not text
    result = subprocess.run(
        ["git", "-C", str(project.root), *args],
        input=input,
        check=True,
        capture_output=True,
        text=not binary,
    )
    if binary:
        return result.stdout if not text else result.stdout.decode().strip()
    return result.stdout.strip()


def _validate_legacy_state(project: Project, ledger: Any) -> None:
    known_raw: set[str] = set()
    known_wiki: set[Path] = set()
    known_state: set[Path] = set()
    for rel, row in ledger.sources.items():
        raw_rel = str(row.get("raw_rel") or "")
        source = project.mount / rel
        raw = project.raw_file(raw_rel)
        wiki = project.wiki_dir(raw_rel)
        marker = read_json(wiki / "_planning" / "source.json", default={})
        document = wiki.relative_to(project.wiki).as_posix()
        valid = (
            raw_rel and source.is_file() and raw.is_file() and wiki.is_dir()
            and marker.get("raw") == raw_rel
            and marker.get("sha256") == hashlib.sha256(raw.read_bytes()).hexdigest()
            and document in ledger.published_documents
        )
        expected_source = str(row.get("source_sha256") or "")
        if expected_source and source.is_file():
            valid = valid and expected_source == hashlib.sha256(source.read_bytes()).hexdigest()
        if not valid:
            raise RuntimeError(f"cannot create Git baseline: incomplete generated state for {rel}; run a normal repair sync")
        known_raw.add(raw_rel)
        known_wiki.add(wiki)
        known_state.add(project.state_dir(raw_rel))
    orphaned = (
        set(project.raw_files()) - known_raw
        or {path.parent.parent for path in project.wiki.rglob("_planning/source.json")} - known_wiki
        or {path.parent.parent for path in (project.metadata / "state").rglob("state/plan.json")} - known_state
    )
    if orphaned:
        raise RuntimeError("cannot create Git baseline: generated data is not represented by pipeline.json; run a normal repair sync")


def _validate_empty_unledgered_state(project: Project) -> None:
    for root in (project.raw, project.wiki, project.metadata / "state"):
        if root.is_dir() and any(path.is_file() for path in root.rglob("*")):
            raise RuntimeError(
                "cannot create Git baseline from generated data without pipeline.json; "
                "run a normal repair sync"
            )


def _migrate_legacy_state(project: Project) -> None:
    from .ledger import load_ledger, save_ledger

    ledger_path = project.metadata / "pipeline.json"
    if not ledger_path.exists():
        return
    ledger = load_ledger(ledger_path)
    _validate_legacy_state(project, ledger)
    active: dict[str, str] = {}
    sources_dir = project.root / "sources"
    for rel, row in ledger.sources.items():
        source_id = str(row.get("source_id") or uuid.uuid4())
        raw_rel = str(row.get("raw_rel") or "")
        document = project.wiki_dir(raw_rel).relative_to(project.wiki).as_posix()
        id_seed = str(row.get("id_seed") or document)
        row.update({"source_id": source_id, "id_seed": id_seed, "mount_rel": rel})
        for name in ("source.json", "chunks.json"):
            path = project.wiki_dir(raw_rel) / "_planning" / name
            if path.exists():
                data = read_json(path)
                if isinstance(data, dict):
                    data["id_seed"] = id_seed
                    write_json_atomic(path, data)
        source = project.mount / rel
        if source.is_file():
            payload = source.read_bytes()
            oid = str(_git(project, "hash-object", "-w", "--stdin", input=payload))
            row["source_blob_oid"] = oid
            row["source_sha256"] = hashlib.sha256(payload).hexdigest()
            sources_dir.mkdir(parents=True, exist_ok=True)
            (sources_dir / f"{source_id}{source.suffix.lower()}").write_bytes(payload)
        active[rel] = source_id
    save_ledger(ledger_path, ledger)
    write_json_atomic(
        project.metadata / "source-identities.json",
        {"schema_version": 1, "active": active, "tombstones": {}},
    )


def ensure_repository(project: Project) -> str:
    """Initialize and baseline the per-project repository once."""
    project.ensure()
    if not (project.root / ".git").exists():
        from .ledger import load_ledger

        ledger_path = project.metadata / "pipeline.json"
        if ledger_path.exists():
            _validate_legacy_state(project, load_ledger(ledger_path))
        else:
            _validate_empty_unledgered_state(project)
        subprocess.run(["git", "init", "-q", str(project.root)], check=True, capture_output=True)
        _git(project, "config", "user.name", "llm-wiki")
        _git(project, "config", "user.email", "llm-wiki@local")
        ignore = project.root / ".gitignore"
        existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        missing = [line for line in GITIGNORE.splitlines() if line and line not in existing.splitlines()]
        ignore.write_text(existing.rstrip() + ("\n" if existing.strip() else "") + "\n".join(missing) + "\n", encoding="utf-8")
        _migrate_legacy_state(project)
        _stage_durable(project)
        _git(project, "commit", "-q", "--allow-empty", "-m", "baseline")
        _git(project, "update-ref", LAST_GOOD_REF, "HEAD")
    else:
        _git(project, "config", "user.name", "llm-wiki")
        _git(project, "config", "user.email", "llm-wiki@local")
        try:
            return last_good(project)
        except subprocess.CalledProcessError:
            from .ledger import load_ledger

            ignore = project.root / ".gitignore"
            existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
            missing = [line for line in GITIGNORE.splitlines() if line and line not in existing.splitlines()]
            ignore.write_text(existing.rstrip() + ("\n" if existing.strip() else "") + "\n".join(missing) + "\n", encoding="utf-8")
            ledger_path = project.metadata / "pipeline.json"
            if ledger_path.exists():
                _validate_legacy_state(project, load_ledger(ledger_path))
            else:
                _validate_empty_unledgered_state(project)
            _migrate_legacy_state(project)
            _stage_durable(project)
            _git(project, "commit", "-q", "--allow-empty", "-m", "baseline")
            _git(project, "update-ref", LAST_GOOD_REF, "HEAD")
    return last_good(project)


def last_good(project: Project) -> str:
    return str(_git(project, "rev-parse", "--verify", LAST_GOOD_REF))


def stage_blob(project: Project, source_path: Path) -> Blob:
    """Read one stable sample and store it in the project's Git object database."""
    ensure_repository(project)
    before = source_path.stat()
    payload = source_path.read_bytes()
    after = source_path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(payload) != after.st_size:
        raise RuntimeError(f"source changed while staging: {source_path}")
    digest = hashlib.sha256(payload).hexdigest()
    oid = str(_git(project, "hash-object", "-w", "--stdin", input=payload))
    _git(project, "update-ref", f"refs/llm-wiki/blobs/{digest}", oid)
    return Blob(oid, digest, len(payload), source_path.suffix.lower())


def read_blob(project: Project, oid: str) -> bytes:
    if not oid:
        return b""
    return bytes(_git(project, "cat-file", "blob", oid, text=False))


def _candidate_base(project: Project) -> Path:
    return project.root.parent / f".{project.root.name}-candidates"


def candidate_project(project: Project, operation_id: str) -> Project:
    base = _candidate_base(project).resolve()
    root = (base / operation_id / project.root.name).resolve()
    if base not in root.parents:
        raise ValueError(f"unsafe candidate path: {root}")
    return Project(root, root / "mount")


def reopen_candidate(project: Project, operation_id: str, commit: str) -> Project:
    """Restore a crashed candidate worktree from its recorded commit."""
    staged = candidate_project(project, operation_id)
    if staged.root.exists():
        return staged
    staged.root.parent.mkdir(parents=True, exist_ok=True)
    _git(project, "worktree", "prune")
    _git(project, "worktree", "add", "-q", "--detach", str(staged.root), commit)
    staged.ensure()
    staged.mount.mkdir(parents=True, exist_ok=True)
    return staged


@contextmanager
def candidate(project: Project, operation_id: str) -> Iterator[Project]:
    """Yield a detached worktree rooted at last-good and always remove it."""
    ensure_repository(project)
    staged = candidate_project(project, operation_id)
    root = staged.root
    root.parent.mkdir(parents=True, exist_ok=True)
    _git(project, "worktree", "prune")
    _git(project, "worktree", "add", "-q", "--detach", str(root), last_good(project))
    staged.ensure()
    staged.mount.mkdir(parents=True, exist_ok=True)
    try:
        if project.linker_database.exists():
            staged.linker_database.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(project.linker_database)) as source, closing(
                sqlite3.connect(staged.linker_database)
            ) as target:
                source.backup(target)
        yield staged
    finally:
        subprocess.run(
            ["git", "-C", str(project.root), "worktree", "remove", "--force", str(root)],
            check=False,
            capture_output=True,
        )
        shutil.rmtree(root.parent, ignore_errors=True)
        _git(project, "worktree", "prune")


def commit_candidate(candidate_project: Project, message: str, metadata: dict[str, Any]) -> str:
    body = "\n".join(f"{key}: {value}" for key, value in sorted(metadata.items()) if value)
    _stage_durable(candidate_project)
    args = ["commit", "-q", "--allow-empty", "-m", message]
    if body:
        args.extend(["-m", body])
    _git(candidate_project, *args)
    return str(_git(candidate_project, "rev-parse", "HEAD"))


def amend_candidate(candidate_project: Project) -> str:
    _stage_durable(candidate_project)
    _git(candidate_project, "commit", "-q", "--amend", "--no-edit", "--allow-empty")
    return str(_git(candidate_project, "rev-parse", "HEAD"))


def candidate_is_clean(candidate_project: Project, commit: str) -> bool:
    if str(_git(candidate_project, "rev-parse", "HEAD")) != commit:
        return False
    paths = [
        ".gitignore", "sources", "raw", "wiki", "metadata/state",
        "metadata/pipeline.json", "metadata/source-identities.json",
    ]
    return not str(_git(candidate_project, "status", "--porcelain", "--untracked-files=all", "--", *paths))


def checkpoint_live(project: Project, message: str) -> str:
    """Commit durable live state and advance last-good after remote success."""
    previous = ensure_repository(project)
    if str(_git(project, "rev-parse", "HEAD")) != previous:
        raise RuntimeError("live project HEAD is not last-good")
    _stage_durable(project)
    paths = [
        ".gitignore", "sources", "raw", "wiki", "metadata/state",
        "metadata/pipeline.json", "metadata/source-identities.json",
    ]
    if not str(_git(project, "status", "--porcelain", "--untracked-files=all", "--", *paths)):
        return previous
    _git(project, "commit", "-q", "-m", message)
    commit = str(_git(project, "rev-parse", "HEAD"))
    _git(project, "update-ref", LAST_GOOD_REF, commit, previous)
    return commit


def _stage_durable(project: Project) -> None:
    paths = [
        ".gitignore", "sources", "raw", "wiki", "metadata/state",
        "metadata/pipeline.json", "metadata/source-identities.json",
    ]
    selected = [
        path for path in paths
        if (project.root / path).exists() or str(_git(project, "ls-files", path))
    ]
    if selected:
        _git(project, "add", "-A", "--", *selected)


def promote(project: Project, candidate_project: Project, commit: str) -> None:
    _git(project, "cat-file", "-e", f"{commit}^{{commit}}")
    previous = last_good(project)
    _git(project, "update-ref", LAST_GOOD_REF, commit, previous)
    try:
        _git(project, "reset", "--hard", commit)
    except BaseException:
        _git(project, "update-ref", LAST_GOOD_REF, previous, commit)
        raise
    source_db = candidate_project.linker_database
    if source_db.exists():
        project.linker_database.parent.mkdir(parents=True, exist_ok=True)
        temp = project.linker_database.with_suffix(".sqlite.tmp")
        shutil.copy2(source_db, temp)
        temp.replace(project.linker_database)
    else:
        project.linker_database.unlink(missing_ok=True)
        project.linker_database.with_name(project.linker_database.name + "-wal").unlink(missing_ok=True)
        project.linker_database.with_name(project.linker_database.name + "-shm").unlink(missing_ok=True)


def restore_last_good(project: Project) -> None:
    _git(project, "reset", "--hard", last_good(project))


def prune_candidates(project: Project) -> None:
    ensure_repository(project)
    _git(project, "worktree", "prune")
    base = _candidate_base(project).resolve()
    if base.is_dir() and base != project.root.resolve() and project.root.resolve() not in base.parents:
        shutil.rmtree(base, ignore_errors=True)
        _git(project, "worktree", "prune")


__all__ = [
    "Blob", "LAST_GOOD_REF", "amend_candidate", "candidate", "candidate_is_clean", "candidate_project", "checkpoint_live", "commit_candidate",
    "ensure_repository", "last_good", "promote", "prune_candidates", "read_blob",
    "reopen_candidate", "restore_last_good", "stage_blob",
]
