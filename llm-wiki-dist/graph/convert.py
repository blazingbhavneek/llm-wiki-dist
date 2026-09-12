"""Convert mount files to Markdown through the doc-parser service."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

import requests

from .project import Project, raw_name_for
from .sync import git

log = logging.getLogger(__name__)
SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


class UnsupportedDocument(RuntimeError):
    """doc-parser answered 415."""


def parse_document(
    path: Path, *, base_url: str, settings: Any, timeout_s: float = 7200
) -> str:
    headers = {
        key: value
        for key, value in {
            "X-LLM-Base-URL": getattr(settings, "chat_base_url", ""),
            "X-LLM-API-Key": getattr(settings, "chat_api_key", ""),
            "X-LLM-Model": getattr(settings, "chat_model", ""),
        }.items()
        if value
    }
    with path.open("rb") as handle:
        reply = requests.post(
            f"{base_url.rstrip('/')}/parse",
            params={"images": "true", "describe_images": "true"},
            headers=headers,
            files={"file": (path.name, handle)},
            timeout=(30, 120),
            stream=True,
        )
    if reply.status_code == 415:
        raise UnsupportedDocument(reply.text[:200])
    reply.raise_for_status()
    body = json.loads(reply.content)
    if "error" in body and "markdown" not in body:
        raise RuntimeError(f"doc-parser: {body['error']}")
    return str(body["markdown"])


def _stat_key(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"mtime": stat.st_mtime, "size": stat.st_size}


def _raw_target(project: Project, rel: str) -> Path:
    return project.raw / Path(rel).parent / raw_name_for(Path(rel).name)


def convert_mount(
    project: Project,
    *,
    parser_base_url: str,
    settings: Any,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    log_path = project.convert_log_path
    seen: dict[str, Any] = (
        json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {}
    )
    converted: list[str] = []
    removed: list[str] = []
    unsupported: list[str] = []
    failed: list[str] = []
    present: set[str] = set()

    for path in sorted(project.mount.rglob("*")):
        if not path.is_file() or path.name in SKIP_NAMES or path.name.startswith("~$"):
            continue
        rel = path.relative_to(project.mount).as_posix()
        present.add(rel)
        key = _stat_key(path)
        previous = seen.get(rel, {})
        if previous.get("mtime") == key["mtime"] and previous.get("size") == key["size"]:
            if previous.get("unsupported"):
                unsupported.append(rel)
            continue
        if on_progress:
            on_progress({"stage": "convert", "file": rel})
        try:
            markdown = parse_document(
                path, base_url=parser_base_url, settings=settings
            )
        except UnsupportedDocument as exc:
            seen[rel] = {**key, "unsupported": str(exc)}
            unsupported.append(rel)
            continue
        except Exception as exc:  # noqa: BLE001 - retry transient failures next run
            log.warning("convert failed for %s: %s", rel, exc)
            failed.append(rel)
            continue
        target = _raw_target(project, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
        seen[rel] = key
        converted.append(rel)

    for rel in list(seen):
        if rel not in present:
            _raw_target(project, rel).unlink(missing_ok=True)
            seen.pop(rel)
            removed.append(rel)

    if converted or removed:
        git(project, "add", "-A", ".")
        git(project, "commit", "-qm", f"convert: +{len(converted)} -{len(removed)}")
    project.metadata.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")
    if unsupported:
        log.warning("convert: %d unsupported: %s", len(unsupported), unsupported[:5])
    return {
        "converted": converted,
        "removed": removed,
        "unsupported": unsupported,
        "failed": failed,
    }
