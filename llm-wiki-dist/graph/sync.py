"""Git-driven sync of raw/ into wiki/ and the graph."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .project import Project

log = logging.getLogger(__name__)

StopCheck = Callable[[], bool] | None
Progress = Callable[[dict[str, Any]], None] | None
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Change:
    status: str
    rel: str


def git(project: Project, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(project.raw), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def head_sha(project: Project) -> str:
    return git(project, "rev-parse", "HEAD").strip()


def last_sha(project: Project) -> str | None:
    if not project.last_sha_path.exists():
        return None
    value = project.last_sha_path.read_text(encoding="utf-8").strip()
    return value or None


def plan_changes(project: Project) -> list[Change]:
    previous = last_sha(project)
    if previous is None:
        return [Change("A", rel) for rel in project.raw_files()]
    if previous == head_sha(project):
        return []
    changes: list[Change] = []
    for line in git(
        project, "diff", "--name-status", "--no-renames", f"{previous}..HEAD", "--", "."
    ).splitlines():
        status, _, rel = line.partition("\t")
        status = status[:1]
        if status not in ("A", "M", "D") or not rel.endswith(".md"):
            continue
        changes.append(Change(status, rel))
    return changes


def changed_hunks(project: Project, rel: str) -> list[tuple[int, int, int, int]]:
    previous = last_sha(project)
    if previous is None:
        return []
    hunks: list[tuple[int, int, int, int]] = []
    for line in git(project, "diff", "-U0", f"{previous}..HEAD", "--", rel).splitlines():
        match = _HUNK_RE.match(line)
        if match:
            old_start, old_len, new_start, new_len = match.groups()
            hunks.append(
                (int(old_start), int(old_len or 1), int(new_start), int(new_len or 1))
            )
    return hunks


def sync_project(
    project: Project,
    librarian: Any,
    *,
    mode: str,
    settings: Any,
    llm: Any,
    embedder: Any,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
) -> dict[str, Any]:
    from .writers import write_index, write_wiki

    def emit(**event: Any) -> None:
        if on_progress:
            on_progress(event)

    def stop() -> bool:
        return bool(stop_check and stop_check())

    parser_base_url = str(getattr(settings, "parser_base_url", "") or "")
    if parser_base_url and project.mount.exists():
        from .convert import convert_mount

        emit(stage="convert", step="start")
        report = convert_mount(
            project,
            parser_base_url=parser_base_url,
            settings=settings,
            on_progress=on_progress,
        )
        emit(stage="convert", step="done", **{key: len(value) for key, value in report.items()})

    changes = plan_changes(project)
    head = head_sha(project)
    done: list[dict[str, Any]] = []
    for index, change in enumerate(changes, start=1):
        if stop():
            raise RuntimeError("sync cancelled")
        emit(
            stage="sync",
            step=change.status,
            current=index,
            total=len(changes),
            file=change.rel,
        )
        if change.status == "D":
            shutil.rmtree(project.wiki_dir(change.rel), ignore_errors=True)
            shutil.rmtree(project.state_dir(change.rel), ignore_errors=True)
            result = librarian.delete_document(change.rel)
            done.append({"file": change.rel, "status": "D", **result})
            continue
        if change.status == "M" and mode == "wiki":
            from .wiki.incremental import invalidate_pages

            result = invalidate_pages(
                project.state_dir(change.rel),
                changed_hunks(project, change.rel),
                # Bytes, not read_text(): run_pipeline hashes the undecoded newlines too.
                project.raw_file(change.rel).read_bytes().decode("utf-8"),
            )
            emit(stage="sync", step="invalidate", file=change.rel, result=result)
        target = write_wiki(
            project,
            change.rel,
            mode=mode,
            settings=settings,
            llm=llm,
            embedder=embedder,
            on_progress=lambda event, rel=change.rel: emit(stage="write", file=rel, **event),
            stop_check=stop_check,
        )
        nodes = librarian.ingest_md_output(
            target,
            stop_check=stop_check,
            raw_source_path=project.raw_file(change.rel),
        )
        done.append({"file": change.rel, "status": change.status, "pages": len(nodes)})
    write_index(project)
    project.metadata.mkdir(parents=True, exist_ok=True)
    project.last_sha_path.write_text(head + "\n", encoding="utf-8")
    return {"head": head, "changes": done}
