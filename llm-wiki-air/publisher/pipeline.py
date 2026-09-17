"""One reconciliation pass for the minimal publisher."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
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
from graph.workspace.project import Project, open_project, raw_name_for
from graph.workspace.writer import links_up_to_date, run_linkers, wiki_config, wiki_up_to_date, write_wiki_pages
from graph.wiki.model import ChatModelPort

from .ledger import Ledger, load_ledger, save_ledger
from .scanner import Scan, SourceFile, scan_mount

log = logging.getLogger(__name__)
PARSER_TIMEOUT = 7200.0


@contextmanager
def _progress_heartbeat(
    callback: Callable[[dict[str, Any]], None] | None,
    *,
    stage: str,
    file: str,
    interval: float = 10.0,
):
    if callback is None:
        yield
        return
    stopped = threading.Event()
    started = time.monotonic()

    def pulse() -> None:
        while not stopped.wait(interval):
            callback({
                "stage": stage,
                "step": "waiting",
                "file": file,
                "elapsed_seconds": round(time.monotonic() - started),
            })

    thread = threading.Thread(target=pulse, name=f"{stage}-progress", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1.0)


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


def _document_raw_rel(document: str) -> str:
    path = Path(document)
    return (path.parent / raw_name_for(path.name)).as_posix()


def _folders(project: Project) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for marker in project.wiki.rglob("_planning/linker.json") if project.wiki.exists() else ():
        try:
            if json.loads(marker.read_text(encoding="utf-8")).get("status") not in ("complete", "disabled"):
                continue
        except (OSError, ValueError):
            continue
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


def _connection(settings: Any) -> Any | None:
    url = str(getattr(settings, "growi_url", "") or os.environ.get("GROWI_URL", "")).strip()
    if not url:
        return None
    target = str(settings.target_name).strip("/")
    return SimpleNamespace(
        name=target,
        write_path=f"/{target}",
        root_path=f"/{target}",
        mode=str(getattr(settings, "growi_mode", "attach")).strip() or "attach",
    )


def _publisher(settings: Any) -> GrowiPublisher | None:
    connection = _connection(settings)
    if connection is None:
        return None
    client = GrowiClient(
        str(getattr(settings, "growi_url", "") or os.environ["GROWI_URL"]),
        str(getattr(settings, "growi_token", "") or os.environ.get("GROWI_TOKEN", "")),
        timeout=float(getattr(settings, "growi_timeout", 30)),
    )
    return GrowiPublisher(client, connection)


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
    return parse_document(
        path,
        base_url=base_url,
        settings=settings,
        timeout_s=float(getattr(settings, "parser_timeout", PARSER_TIMEOUT)),
    )


def _model(settings: Any, project: Project) -> ChatModelPort:
    return ChatModelPort(wiki_config(settings, run_dir=project.metadata / "state" / "publisher"))


def _wiki_raw_rels(project: Project) -> list[str]:
    result: list[str] = []
    for marker in sorted(project.wiki.rglob("_planning/source.json")):
        try:
            result.append(str(json.loads(marker.read_text(encoding="utf-8"))["raw"]))
        except (OSError, KeyError, TypeError, ValueError):
            continue
    return list(dict.fromkeys(result))


def _pending_link_rels(project: Project, settings: Any, rels: list[str] | None = None, *, force: bool = False) -> list[str]:
    if not getattr(settings, "wiki_linker_enabled", True):
        return []
    candidates = rels if rels is not None else _wiki_raw_rels(project)
    mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
    return [rel for rel in candidates if project.wiki_dir(rel).exists() and (force or not links_up_to_date(project, rel, mode=mode))]


def _publish_sweep(
    project: Project,
    ledger: Ledger,
    publisher: GrowiPublisher | None,
    run_id: str,
    *,
    only: set[str] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> list[str]:
    failures: list[str] = []
    folders = _folders(project)
    scoped_documents = (
        {project.wiki_dir(rel).relative_to(project.wiki).as_posix() for rel in only}
        if only is not None
        else None
    )
    if scoped_documents is not None:
        folders = {document: folder for document, folder in folders.items() if document in scoped_documents}
    blocked: set[str] = set()
    if publisher is not None:
        try:
            if not ledger.published_pages and ledger.published_documents:
                rels = [_document_raw_rel(document) for document in sorted(folders)]
                discovered = publisher.discover_documents(project, rels)
                ledger.published_pages = {
                    local_path: {
                        "growi_path": page.path,
                        "page_id": page.page_id,
                        "revision_id": "",
                    }
                    for local_path, page in discovered.items()
                }
            current_pages = {
                path: row for path, row in ledger.published_pages.items()
                if Path(path).parent.as_posix() in folders
            }
            unchanged = {
                document for document, folder in folders.items()
                if ledger.published_documents.get(document, {}).get("content_sha256") == _content_hash(folder)
            }
            pulled, conflicts, blocked = publisher.pull_changes(project, current_pages, unchanged)
            failures.extend(conflicts)
            if pulled:
                for document in {Path(path).parent.as_posix() for path in pulled}:
                    ledger.published_documents[document]["content_sha256"] = _content_hash(folders[document])
                log.info("run=%s pages=%d stage=pull", run_id, len(pulled))
        except Exception as exc:
            blocked = set(folders)
            failures.append(f"pull: {type(exc).__name__}: {exc}")
    documents = [
        (document, folder, _document_raw_rel(document))
        for document, folder in sorted(folders.items())
        if document not in blocked
    ]
    if publisher is not None:
        started = time.monotonic()
        try:
            if on_progress:
                on_progress({
                    "stage": "growi-publish",
                    "step": "start",
                    "current": 0,
                    "total": len(documents),
                    "documents": len(documents),
                })
            pages = publisher.publish_documents(
                project,
                [raw_rel for _, _, raw_rel in documents],
                known_pages=ledger.published_pages,
            )
            published_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for document, folder, raw_rel in documents:
                ledger.published_documents[document] = {
                    "content_sha256": _content_hash(folder),
                    "growi_path": publisher.doc_path(project, raw_rel),
                    "raw_rel": raw_rel,
                    "published_at": published_at,
                }
            updated_pages = {
                local_path: {
                    "growi_path": page.path,
                    "page_id": page.page_id,
                    "revision_id": page.revision_id,
                }
                for local_path, page in pages.items()
            }
            published_prefixes = tuple(document.rstrip("/") + "/" for document, _, _ in documents)
            ledger.published_pages = {
                path: row for path, row in ledger.published_pages.items()
                if not path.startswith(published_prefixes)
            }
            ledger.published_pages.update(updated_pages)
            log.info("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
            if on_progress:
                on_progress({
                    "stage": "growi-publish",
                    "step": "done",
                    "current": len(documents),
                    "total": len(documents),
                    "pages": len(pages),
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                })
        except Exception as exc:
            failures.append(f"publish: {type(exc).__name__}: {exc}")
            log.error("run=%s stage=publish error=%s: %s", run_id, type(exc).__name__, exc)
    for document, row in list(ledger.published_documents.items()):
        if scoped_documents is not None and document not in scoped_documents:
            continue
        if document in folders or publisher is None:
            continue
        raw_rel = _document_raw_rel(document)
        try:
            publisher.delete_document(project, raw_rel)
            ledger.published_documents.pop(document, None)
            prefix = document.rstrip("/") + "/"
            ledger.published_pages = {
                path: row for path, row in ledger.published_pages.items() if not path.startswith(prefix)
            }
        except Exception as exc:
            failures.append(f"{document}: {type(exc).__name__}: {exc}")
    return failures


def _remove_sources(project: Project, ledger: Ledger, sources: dict[str, str]) -> tuple[list[dict[str, Any]], set[str], list[str]]:
    done: list[dict[str, Any]] = []
    touched_raw: set[str] = set()
    failures: list[str] = []
    for rel, raw_rel in sources.items():
        try:
            if (project.wiki_dir(raw_rel) / "_planning" / "linker.json").exists():
                from graph.linker import remove_document
                touched = remove_document(project, raw_rel)
                touched_raw.update(touched)
            else:
                touched = []
            shutil.rmtree(project.wiki_dir(raw_rel), ignore_errors=True)
            shutil.rmtree(project.state_dir(raw_rel), ignore_errors=True)
            project.raw_file(raw_rel).unlink(missing_ok=True)
            ledger.sources.pop(rel, None)
            done.append({"path": rel, "status": "deleted", "touched": touched})
        except Exception as exc:
            failures.append(f"{rel}: {type(exc).__name__}: {exc}")
    return done, touched_raw, failures


def sync_once(
    settings: Any,
    *,
    only: list[str] | None = None,
    force: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
    include_pending: bool = False,
    resume: bool | None = None,
) -> dict[str, Any]:
    """One reconciliation pass, optionally scoped to mount-relative paths."""
    project = open_project(settings)
    ledger_path = project.metadata / "pipeline.json"
    run_id = "prun-" + uuid.uuid4().hex[:16]
    done: list[dict[str, Any]] = []
    failures: list[str] = []
    cancelled = False
    with _lock(project):
        ledger = load_ledger(ledger_path)
        scan = scan_mount(project.mount, ledger.sources)
        publisher = _publisher(settings)
        if publisher is None:
            raise RuntimeError("GROWI_URL is required for sync/watch; use build for local-only output")
        wanted = {rel.strip().lstrip("/") for rel in only} if only is not None else None
        if wanted is not None:
            missing = wanted - set(scan.files) - set(ledger.sources)
            if missing:
                raise FileNotFoundError(f"not under mount/: {sorted(missing)}")
        scoped_raw = {
            str(ledger.sources.get(rel, {}).get("raw_rel") or _raw_rel(scan.files[rel]))
            for rel in wanted or ()
            if rel in scan.files or rel in ledger.sources
        }
        touched_raw: set[str] = set()
        deleted = scan.deleted if wanted is None else sorted(set(scan.deleted) & wanted)
        removed, touched, remove_failures = _remove_sources(project, ledger, {
            rel: str(ledger.sources.get(rel, {}).get("raw_rel") or _raw_rel(SourceFile(rel, "", 0, 0, "md")))
            for rel in deleted
        })
        done.extend(removed)
        touched_raw.update(touched)
        failures.extend(remove_failures)
        if deleted:
            save_ledger(ledger_path, ledger)
        changed = set(scan.added) | set(scan.changed)
        if wanted is not None:
            changed &= wanted
            if force:
                changed |= wanted & set(scan.files)
        changed = sorted(changed)
        scoped_candidates = None if include_pending else (sorted(scoped_raw) if wanted is not None else None)
        pending_before = _pending_link_rels(project, settings, scoped_candidates)
        model = _model(settings, project) if changed or pending_before else None
        try:
            embedder = Embedder(settings) if changed or pending_before else None
        except Exception as exc:
            log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
            embedder = None
        for rel in changed:
            if should_continue is not None and not should_continue():
                cancelled = True
                break
            item = scan.files[rel]
            raw_rel = _raw_rel(item)
            started = time.monotonic()
            try:
                log.info("run=%s path=%s stage=parse start", run_id, rel)
                if on_progress:
                    on_progress({
                        "stage": "parse",
                        "step": "start",
                        "file": rel,
                        "parser": item.parser,
                        "bytes": item.size,
                    })
                with _progress_heartbeat(on_progress, stage="parse", file=rel):
                    markdown = _parse(item, project.mount / rel, settings)
                _assert_source_unchanged(item, project.mount / rel)
                if on_progress:
                    on_progress({
                        "stage": "parse",
                        "step": "done",
                        "file": rel,
                        "characters": len(markdown),
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                    })
                _write_raw(project.raw_file(raw_rel), markdown)
                wiki_started = time.monotonic()
                if on_progress:
                    on_progress({"stage": "wiki", "step": "start", "file": raw_rel})
                with _progress_heartbeat(on_progress, stage="wiki", file=raw_rel):
                    result = write_wiki_pages(
                        project, raw_rel, mode=str(settings.ingest_mode), settings=settings,
                        llm=model, embedder=embedder, on_progress=on_progress,
                        resume=not force if resume is None else resume,
                        stop_check=(lambda: not should_continue()) if should_continue is not None else None,
                    )
                if on_progress:
                    on_progress({
                        "stage": "wiki",
                        "step": "done",
                        "file": raw_rel,
                        "touched_documents": len(result.touched),
                        "elapsed_seconds": round(time.monotonic() - wiki_started, 1),
                    })
                ledger.sources[rel] = _source_row(item, raw_rel)
                save_ledger(ledger_path, ledger)
                done.append({"path": rel, "status": "changed" if rel in scan.changed else "added", "touched": result.touched})
                log.info("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
            except asyncio.CancelledError:
                cancelled = True
                break
            except Exception as exc:
                if should_continue is not None and not should_continue():
                    cancelled = True
                    break
                error = f"{type(exc).__name__}: {exc}"[:500]
                ledger.sources[rel] = _source_row(item, raw_rel, error)
                save_ledger(ledger_path, ledger)
                failures.append(f"{rel}: {error}")
                log.error("run=%s path=%s stage=generate error=%s: %s", run_id, rel, type(exc).__name__, exc)
        pending_links = _pending_link_rels(project, settings, scoped_candidates)
        if should_continue is not None and not should_continue():
            cancelled = True
        if pending_links and not failures and not cancelled:
            try:
                if on_progress:
                    on_progress({
                        "stage": "linker",
                        "step": "batch_start",
                        "current": 0,
                        "total": len(pending_links),
                        "documents": len(pending_links),
                    })
                with _progress_heartbeat(on_progress, stage="linker", file="batch"):
                    touched = run_linkers(
                        project, pending_links, settings=settings, llm=model, embedder=embedder,
                        on_progress=on_progress,
                        stop_check=(lambda: not should_continue()) if should_continue is not None else None,
                    )
                touched_raw.update(touched)
                done.append({"path": "*", "status": "linked", "documents": len(pending_links), "touched": touched})
                if on_progress:
                    on_progress({
                        "stage": "linker",
                        "step": "batch_done",
                        "current": len(pending_links),
                        "total": len(pending_links),
                        "touched": len(touched),
                    })
            except asyncio.CancelledError:
                cancelled = True
            except Exception as exc:
                if should_continue is not None and not should_continue():
                    cancelled = True
                else:
                    failures.append(f"link: {type(exc).__name__}: {exc}")
        if should_continue is not None and not should_continue():
            cancelled = True
        publish_only = scoped_raw | set(pending_links) | touched_raw if wanted is not None else None
        if not failures and not cancelled:
            failures.extend(_publish_sweep(
                project,
                ledger,
                publisher,
                run_id,
                only=publish_only,
                on_progress=on_progress,
            ))
        save_ledger(ledger_path, ledger)
    return {"run_id": run_id, "scan": scan, "done": done, "failures": failures, "cancelled": cancelled}


def delete_sources(
    settings: Any,
    sources: dict[str, str],
    *,
    should_continue: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Idempotently remove queued source documents locally and from GROWI."""
    project = open_project(settings)
    publisher = _publisher(settings)
    if publisher is None:
        raise RuntimeError("GROWI_URL is required for queued deletion")
    ledger_path = project.metadata / "pipeline.json"
    run_id = "del-" + uuid.uuid4().hex[:16]
    with _lock(project):
        ledger = load_ledger(ledger_path)
        done, touched, failures = _remove_sources(project, ledger, sources)
        save_ledger(ledger_path, ledger)
        cancelled = should_continue is not None and not should_continue()
        if not failures and not cancelled:
            failures.extend(_publish_sweep(project, ledger, publisher, run_id, only=set(sources.values()) | touched))
        save_ledger(ledger_path, ledger)
    return {"run_id": run_id, "done": done, "failures": failures, "cancelled": cancelled}


def pull_growi_once(settings: Any) -> dict[str, Any]:
    """Pull user revisions into local wiki state without rebuilding or publishing."""
    project = open_project(settings)
    publisher = _publisher(settings)
    if publisher is None:
        return {"run_id": "pull-disabled", "done": [], "failures": []}
    ledger_path = project.metadata / "pipeline.json"
    run_id = "pull-" + uuid.uuid4().hex[:16]
    with _lock(project):
        ledger = load_ledger(ledger_path)
        folders = _folders(project)
        current_pages = {
            path: row for path, row in ledger.published_pages.items()
            if Path(path).parent.as_posix() in folders
        }
        unchanged = {
            document for document, folder in folders.items()
            if ledger.published_documents.get(document, {}).get("content_sha256") == _content_hash(folder)
        }
        try:
            pulled, failures, _blocked = publisher.pull_changes(project, current_pages, unchanged)
            for document in {Path(path).parent.as_posix() for path in pulled}:
                ledger.published_documents[document]["content_sha256"] = _content_hash(folders[document])
            save_ledger(ledger_path, ledger)
        except Exception as exc:
            pulled = []
            failures = [f"pull: {type(exc).__name__}: {exc}"]
    return {"run_id": run_id, "done": [{"status": "pulled", "path": path} for path in pulled], "failures": failures}


def build_wiki_only(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Build raw Markdown into pristine wiki pages, leaving linking pending."""
    project = open_project(settings)
    available = set(project.raw_files())
    rels = list(dict.fromkeys(rel.strip().lstrip("/") for rel in only)) if only is not None else sorted(available)
    missing = [rel for rel in rels if rel not in available]
    if missing:
        raise FileNotFoundError(f"not under raw/: {missing}")
    pending = [rel for rel in rels if force or not wiki_up_to_date(project, rel)]
    done: list[dict[str, Any]] = []
    failures: list[str] = []
    run_id = "brun-" + uuid.uuid4().hex[:16]
    with _lock(project):
        if only is None:
            ledger = load_ledger(project.metadata / "pipeline.json")
            mount_raw = {str(row.get("raw_rel", "")) for row in ledger.sources.values()}
            for marker in sorted(project.wiki.rglob("_planning/source.json")):
                try:
                    raw_rel = str(json.loads(marker.read_text(encoding="utf-8"))["raw"])
                except (KeyError, TypeError, ValueError):
                    continue
                if raw_rel in available or raw_rel in mount_raw:
                    continue
                try:
                    from graph.linker import remove_document
                    touched = remove_document(project, raw_rel)
                    shutil.rmtree(project.wiki_dir(raw_rel), ignore_errors=True)
                    shutil.rmtree(project.state_dir(raw_rel), ignore_errors=True)
                    done.append({"path": raw_rel, "status": "deleted", "touched": touched})
                except Exception as exc:
                    failures.append(f"{raw_rel}: {type(exc).__name__}: {exc}")
        model = _model(settings, project) if pending else None
        for rel in rels:
            if rel not in pending:
                done.append({"path": rel, "status": "up-to-date"})
                continue
            try:
                result = write_wiki_pages(project, rel, mode=str(settings.ingest_mode), settings=settings, llm=model, embedder=None, on_progress=on_progress, resume=not force)
                done.append({"path": rel, "status": "built", "touched": result.touched})
            except Exception as exc:
                failures.append(f"{rel}: {type(exc).__name__}: {exc}")
    return {"run_id": run_id, "done": done, "failures": failures}


def link_raw(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Link pending wiki documents without regenerating their pages."""
    project = open_project(settings)
    rels = list(dict.fromkeys(rel.strip().lstrip("/") for rel in only)) if only is not None else _wiki_raw_rels(project)
    missing = [rel for rel in rels if not project.wiki_dir(rel).exists()]
    if missing:
        raise FileNotFoundError(f"no wiki output for: {missing}")
    pending = _pending_link_rels(project, settings, rels, force=force)
    run_id = "lrun-" + uuid.uuid4().hex[:16]
    done: list[dict[str, Any]] = []
    failures: list[str] = []
    with _lock(project):
        if not pending:
            return {"run_id": run_id, "done": [{"status": "up-to-date"}], "failures": []}
        model = _model(settings, project)
        try:
            embedder = Embedder(settings)
        except Exception as exc:
            log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
            embedder = None
        try:
            touched = run_linkers(project, pending, settings=settings, llm=model, embedder=embedder, on_progress=on_progress)
            done.append({"status": "linked", "documents": pending, "touched": touched})
        except Exception as exc:
            failures.append(f"link: {type(exc).__name__}: {exc}")
    return {"run_id": run_id, "done": done, "failures": failures}


def build_raw(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Generate the complete wiki batch first, then link it as one batch."""
    wiki = build_wiki_only(settings, only=only, force=force, on_progress=on_progress)
    project = open_project(settings)
    link_only = [rel for rel in only if project.wiki_dir(rel.strip().lstrip("/")).exists()] if only else None
    links = link_raw(settings, only=link_only, force=force, on_progress=on_progress)
    return {"run_id": wiki["run_id"], "done": [*wiki["done"], *links["done"]], "failures": [*wiki["failures"], *links["failures"]]}


def publish_only(settings: Any) -> dict[str, Any]:
    """Publish the current wiki tree without scanning or changing mount/raw."""
    project = open_project(settings)
    publisher = _publisher(settings)
    if publisher is None:
        raise RuntimeError("GROWI_URL is required for publish")
    ledger_path = project.metadata / "pipeline.json"
    run_id = "pub-" + uuid.uuid4().hex[:16]
    with _lock(project):
        ledger = load_ledger(ledger_path)
        failures = _publish_sweep(project, ledger, publisher, run_id)
        save_ledger(ledger_path, ledger)
    return {"run_id": run_id, "done": [], "failures": failures}


def reset_growi(settings: Any) -> dict[str, Any]:
    """Trash publisher-marked pages below the configured target and reset state."""
    project = open_project(settings)
    publisher = _publisher(settings)
    if publisher is None:
        raise RuntimeError("GROWI_URL is required for reset")
    ledger_path = project.metadata / "pipeline.json"
    with _lock(project):
        deleted = publisher.reset()
        ledger = load_ledger(ledger_path)
        ledger.published_documents.clear()
        ledger.published_pages.clear()
        save_ledger(ledger_path, ledger)
    return {"run_id": "reset-" + uuid.uuid4().hex[:16], "done": [{"status": "reset", "deleted": deleted}], "failures": []}


__all__ = ["build_raw", "build_wiki_only", "delete_sources", "link_raw", "publish_only", "pull_growi_once", "reset_growi", "sync_once"]
