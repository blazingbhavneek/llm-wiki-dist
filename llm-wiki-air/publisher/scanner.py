"""Content-addressed mount scanner."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

SUPPORTED = {".md", ".docx", ".pdf", ".pptx", ".xlsx", ".xlsm", ".csv"}
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
IGNORED_DIRS = {".git", ".hg", ".svn"}


@dataclass(frozen=True)
class SourceFile:
    rel: str
    source_sha256: str
    size: int
    mtime_ns: int
    parser: str


@dataclass(frozen=True)
class Scan:
    files: dict[str, SourceFile]
    added: list[str]
    changed: list[str]
    deleted: list[str]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def scan_mount(root: Path, previous: dict[str, dict[str, object]] | None = None) -> Scan:
    root = Path(root)
    previous = previous or {}
    files: dict[str, SourceFile] = {}
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if any(part in IGNORED_DIRS for part in path.parts):
                continue
            if path.name in IGNORED_NAMES or path.name.startswith("~$") or not path.is_file() or not _inside(path, root):
                continue
            suffix = path.suffix.lower()
            if suffix not in SUPPORTED:
                continue
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rel = path.relative_to(root).as_posix()
            files[rel] = SourceFile(rel, digest, stat.st_size, stat.st_mtime_ns, "md" if suffix == ".md" else suffix[1:])
    current = set(files)
    known = set(previous)
    added = sorted(current - known)
    changed = sorted(rel for rel in current & known if str(previous[rel].get("source_sha256", "")) != files[rel].source_sha256)
    deleted = sorted(known - current)
    return Scan(files, added, changed, deleted)


__all__ = ["Scan", "SourceFile", "SUPPORTED", "scan_mount"]
