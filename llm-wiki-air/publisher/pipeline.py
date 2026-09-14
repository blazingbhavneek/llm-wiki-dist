"""One reconciliation pass for the minimal publisher."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from graph.clients.embeddings import Embedder
from graph.common.markdown import strip_big_tables, strip_image_media
from graph.growi import GrowiClient, GrowiPublisher
from graph.workspace.parser_client import UnsupportedDocument, parse_document
from graph.workspace.project import Project, raw_name_for
from graph.workspace.writer import wiki_config, write_wiki
from graph.wiki.model import ChatModelPort

from .ledger import Ledger, load_ledger, save_ledger
from .scanner import Scan, SourceFile, scan_mount

log = logging.getLogger(__name__)
PARSER_TIMEOUT = 7200.0


def _source_row(item: SourceFile, raw_rel: str, error: str = "") -> dict[str, Any]:
    return {
        "source_sha256": item.source_sha256 if not error else "",
        "size": item.size,
        "mtime_ns": item.mtime_ns,
        "raw_rel": raw_rel,
        "wiki_rel": item.rel,
        "parser": item.parser,
        "completed_at": "" if error else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_error": error,
    }


def _content_hash(folder: Path) -> str:
    pages = []
    for page in sorted(Path(folder).glob("*.md")):
        pages.append((page.name, hashlib.sha256(page.read_bytes()).hexdigest()))
    payload = json.dumps(pages, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _raw_rel(item: SourceFile) -> str:
    path = Path(item.rel)
    return (path.parent / raw_name_for(path.name)).as_posix()


def _wiki_document(project: Project, source: dict[str, Any]) -> str:
    return str(source.get("wiki_rel") or Path(str(source.get("raw_rel", ""))).with_suffix("").as_posix())


def _folders(project: Project) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for marker in project.wiki.rglob("_planning/linker.json") if project.wiki.exists() else ():
        result[marker.parent.parent.relative_to(project.wiki).as_posix()] = marker.parent.parent
    return result


def _write_raw(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".raw-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _assert_source_unchanged(item: SourceFile, path: Path) -> None:
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest != item.source_sha256:
        raise RuntimeError(f"source changed during parse: {item.rel}")


def _connection() -> Any | None:
    url = os.environ.get("GROWI_URL", "").strip()
    if not url:
        return None
    return SimpleNamespace(
        name="publisher",
        write_path=os.environ.get("GROWI_WRITE_PATH", "").strip(),
        root_path=os.environ.get("GROWI_ROOT_PATH", "/").strip() or "/",
        mode=os.environ.get("GROWI_MODE", "attach").strip() or "attach",
    )


@contextmanager
def _lock(project: Project):
    path = project.metadata / "pipeline.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("publisher already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _parse(item: SourceFile, path: Path, settings: Any) -> str:
    if item.parser == "md":
        return path.read_text(encoding="utf-8")
    base_url = str(getattr(settings, "parser_base_url", ""))
    if not base_url:
        raise RuntimeError(f"WIKI_PARSER_BASE_URL is required for {item.rel}")
    return parse_document(path, base_url=base_url, settings=settings, timeout_s=float(os.environ.get("WIKI_PARSER_TIMEOUT", PARSER_TIMEOUT)))


def _model(settings: Any, project: Project) -> ChatModelPort:
    return ChatModelPort(wiki_config(settings, run_dir=project.metadata / "state" / "publisher"))


def _publish_sweep(project: Project, ledger: Ledger, publisher: GrowiPublisher | None, run_id: str) -> list[str]:
    failures: list[str] = []
    folders = _folders(project)
    for document, folder in sorted(folders.items()):
        content_sha = _content_hash(folder)
        previous = ledger.published_documents.get(document, {})
        if previous.get("content_sha256") == content_sha:
            continue
        if publisher is None:
            continue
        source = next((row for row in ledger.sources.values() if _wiki_document(project, row) == document), {})
        raw_rel = str(source.get("raw_rel") or document)
        started = time.monotonic()
        try:
            publisher.publish_document(project, raw_rel)
            ledger.published_documents[document] = {
                "content_sha256": content_sha,
                "growi_path": publisher.doc_path(project, raw_rel),
                "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            log.info("run=%s path=%s stage=publish elapsed=%.2fs", run_id, document, time.monotonic() - started)
        except Exception as exc:
            failures.append(f"{document}: {type(exc).__name__}: {exc}")
            log.error("run=%s path=%s stage=publish error=%s: %s", run_id, document, type(exc).__name__, exc)
    for document, row in list(ledger.published_documents.items()):
        if document in folders or publisher is None:
            continue
        source = next((item for item in ledger.sources.values() if _wiki_document(project, item) == document), {})
        raw_rel = str(source.get("raw_rel") or document)
        try:
            publisher.delete_document(project, raw_rel)
            ledger.published_documents.pop(document, None)
        except Exception as exc:
            failures.append(f"{document}: {type(exc).__name__}: {exc}")
    return failures


def sync_once(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """One reconciliation pass. `only` restricts generation to those mount-relative
    paths (empty list = publish sweep only); `force` regenerates them even if unchanged."""
    project = Project(Path(settings.data_root or "data")).ensure()
    ledger_path = project.metadata / "pipeline.json"
    run_id = "prun-" + uuid.uuid4().hex[:16]
    done: list[dict[str, Any]] = []
    failures: list[str] = []
    with _lock(project):
        ledger = load_ledger(ledger_path)
        scan = scan_mount(project.mount, ledger.sources)
        client = None
        connection = _connection()
        publisher = None
        if connection is not None:
            client = GrowiClient(os.environ["GROWI_URL"], os.environ.get("GROWI_TOKEN", ""), timeout=float(os.environ.get("GROWI_TIMEOUT", "30")))
            publisher = GrowiPublisher(client, connection)
        for rel in scan.deleted:
            source = ledger.sources.get(rel, {})
            raw_rel = str(source.get("raw_rel") or _raw_rel(SourceFile(rel, "", 0, 0, "md")))
            try:
                if (project.wiki_dir(raw_rel) / "_planning" / "linker.json").exists():
                    from graph.linker import remove_document
                    touched = remove_document(project, raw_rel)
                else:
                    touched = []
                if publisher is not None:
                    publisher.delete_document(project, raw_rel)
                shutil.rmtree(project.wiki_dir(raw_rel), ignore_errors=True)
                shutil.rmtree(project.state_dir(raw_rel), ignore_errors=True)
                project.raw_file(raw_rel).unlink(missing_ok=True)
                ledger.sources.pop(rel, None)
                save_ledger(ledger_path, ledger)
                done.append({"path": rel, "status": "deleted", "touched": touched})
            except Exception as exc:
                failures.append(f"{rel}: {type(exc).__name__}: {exc}")
        changed = set(scan.added) | set(scan.changed)
        if only is not None:
            wanted = {rel.strip().lstrip("/") for rel in only}
            missing = wanted - set(scan.files)
            if missing:
                raise FileNotFoundError(f"not under mount/: {sorted(missing)}")
            changed = (changed | wanted) if force else (changed & wanted)
        changed = sorted(changed)
        model = _model(settings, project) if changed else None
        try:
            embedder = Embedder(settings) if changed else None
        except Exception as exc:
            log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
            embedder = None
        for rel in changed:
            item = scan.files[rel]
            raw_rel = _raw_rel(item)
            started = time.monotonic()
            try:
                markdown = _parse(item, project.mount / rel, settings)
                _assert_source_unchanged(item, project.mount / rel)
                _write_raw(project.raw_file(raw_rel), markdown)
                result = write_wiki(project, raw_rel, mode=str(settings.ingest_mode), settings=settings, llm=model, embedder=embedder, on_progress=on_progress)
                ledger.sources[rel] = _source_row(item, raw_rel)
                save_ledger(ledger_path, ledger)
                done.append({"path": rel, "status": "changed" if rel in scan.changed else "added", "touched": result.touched})
                log.info("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
                ledger.sources[rel] = _source_row(item, raw_rel, error)
                save_ledger(ledger_path, ledger)
                failures.append(f"{rel}: {error}")
                log.error("run=%s path=%s stage=generate error=%s: %s", run_id, rel, type(exc).__name__, exc)
        failures.extend(_publish_sweep(project, ledger, publisher, run_id))
        save_ledger(ledger_path, ledger)
    return {"run_id": run_id, "scan": scan, "done": done, "failures": failures}


__all__ = ["sync_once"]
