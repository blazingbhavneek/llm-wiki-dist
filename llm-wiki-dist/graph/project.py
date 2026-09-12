"""Paths for one project data folder."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


def wiki_folder_name(raw_name: str) -> str:
    stem = PurePosixPath(raw_name).stem
    base, sep, ext = stem.rpartition("_")
    return f"{base}.{ext}" if sep and base and ext.isalnum() else stem


def raw_name_for(mount_name: str) -> str:
    path = PurePosixPath(mount_name)
    ext = path.suffix.lstrip(".").lower()
    return f"{path.stem}_{ext}.md" if ext else f"{path.stem}.md"


@dataclass(frozen=True)
class Project:
    root: Path

    @property
    def mount(self) -> Path:
        return self.root / "mount"

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def metadata(self) -> Path:
        return self.root / "metadata"

    @property
    def wiki(self) -> Path:
        return self.root / "wiki"

    @property
    def database(self) -> Path:
        return self.root / "graph.sqlite"

    @property
    def last_sha_path(self) -> Path:
        return self.metadata / "last_sha"

    @property
    def convert_log_path(self) -> Path:
        return self.metadata / "convert.json"

    def raw_file(self, rel: str) -> Path:
        return self.raw / rel

    def wiki_dir(self, rel: str) -> Path:
        path = PurePosixPath(rel)
        return self.wiki / path.parent / wiki_folder_name(path.name)

    def state_dir(self, rel: str) -> Path:
        path = PurePosixPath(rel)
        return self.metadata / "state" / path.parent / wiki_folder_name(path.name)

    def work_dir(self, rel: str) -> Path:
        path = PurePosixPath(rel)
        return self.metadata / "work" / path.parent / wiki_folder_name(path.name)

    def ensure(self) -> "Project":
        for directory in (self.raw, self.metadata, self.wiki):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def raw_files(self) -> list[str]:
        return sorted(
            path.relative_to(self.raw).as_posix()
            for path in self.raw.rglob("*.md")
            if ".git" not in path.parts
        )


def zip_wiki(project: Project) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(project.wiki.rglob("*")):
            if not path.is_file() or "_planning" in path.parts:
                continue
            archive.write(path, path.relative_to(project.wiki).as_posix())
    return buffer.getvalue()
