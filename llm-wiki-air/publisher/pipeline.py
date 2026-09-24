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
from graph.workspace.project import Project, open_project, raw_name_for, wiki_folder_name
from graph.workspace.writer import links_up_to_date, run_linkers, wiki_config, wiki_up_to_date, write_wiki_pages
from graph.wiki.model import ChatModelPort
from graph.wiki.storage import read_json, write_json_atomic

from .ledger import Ledger, load_ledger, save_ledger
from .scanner import Scan, SourceFile, scan_mount

log = logging.getLogger(__name__)
PARSER_TIMEOUT = 7200.0
PARSE_MIN_RATIO = 0.30  # a re-parse this much smaller than before is treated as broken
PARSE_GATE_MIN_CHARS = 2000  # tiny documents may legitimately lose most text


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


def _source_row(
    item: SourceFile,
    raw_rel: str,
    error: str = "",
    *,
    details: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    details, previous = details or {}, previous or {}
    source_id = str(details.get("source_id") or previous.get("source_id") or uuid.uuid4())
    raw_path = Path(raw_rel)
    legacy_seed = (raw_path.parent / wiki_folder_name(raw_path.name)).as_posix()
    return {
        "source_id": source_id,
        "id_seed": str(previous.get("id_seed") or details.get("id_seed") or (legacy_seed if previous else source_id)),
        "mount_rel": item.rel,
        "source_sha256": item.source_sha256 if not error else "",
        "source_blob_oid": str(details.get("source_blob_oid") or previous.get("source_blob_oid") or ""),
        "size": item.size,
        "mtime_ns": item.mtime_ns,
        "raw_rel": raw_rel,
        "wiki_rel": item.rel,
        "parser": item.parser,
        "completed_at": "" if error else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_error": error,
    }


def _identity_path(project: Project) -> Path:
    return project.metadata / "source-identities.json"


def _identities(project: Project) -> dict[str, Any]:
    data = read_json(_identity_path(project), default={})
    return {
        "schema_version": 1,
        "active": dict(data.get("active") or {}),
        "tombstones": dict(data.get("tombstones") or {}),
    }


def _save_identities(project: Project, identities: dict[str, Any]) -> None:
    write_json_atomic(_identity_path(project), identities)


def _store_source(project: Project, item: SourceFile, row: dict[str, Any], source_path: Path) -> None:
    source_id = str(row.get("source_id") or "")
    if not source_id:
        return
    folder = project.root / "sources"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"{source_id}.*"):
        old.unlink()
    shutil.copyfile(source_path, folder / f"{source_id}{Path(item.rel).suffix.lower()}")
    identities = _identities(project)
    identities["active"][item.rel] = source_id
    identities["tombstones"].pop(item.rel, None)
    _save_identities(project, identities)


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


def _check_parse_size(rel: str, previous: str, current: str) -> None:
    """Refuse a parse that lost most of the document's text (R7)."""

    before = len(strip_image_media(previous).strip())
    after = len(strip_image_media(current).strip())
    if before >= PARSE_GATE_MIN_CHARS and after < PARSE_MIN_RATIO * before:
        raise RuntimeError(
            f"suspicious parse for {rel}: text shrank from {before} to {after} characters; "
            f"if this is intended run `python main.py sync --force {rel}`"
        )


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


def _parse(
    item: SourceFile,
    path: Path,
    settings: Any,
    previous_markdown: str | None = None,
) -> str:
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
        previous_markdown=previous_markdown,
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
    only_pages: set[str] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    begin_publish: Callable[[], None] | None = None,
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
                        "revision_id": page.revision_id,
                        "marker_id": publisher.page_marker_id(project, local_path),
                    }
                    for local_path, page in discovered.items()
                }
            known_pages = dict(ledger.published_pages)
            current_pages = {
                path: row for path, row in ledger.published_pages.items()
                if Path(path).parent.as_posix() in folders
                and (only_pages is None or path in only_pages)
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
            if documents and begin_publish is not None:
                begin_publish()
            if on_progress:
                on_progress({
                    "stage": "growi-publish",
                    "step": "start",
                    "current": 0,
                    "total": len(documents),
                    "documents": len(documents),
                })
            publish_args = {
                "known_pages": known_pages,
                **({"only_pages": only_pages} if only_pages is not None else {}),
            }
            pages = publisher.publish_documents(
                project, [raw_rel for _, _, raw_rel in documents], **publish_args
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
                    "marker_id": publisher.page_marker_id(project, local_path),
                }
                for local_path, page in pages.items()
            }
            if only_pages is None:
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
            if begin_publish is not None:
                begin_publish()
            prefix = document.rstrip("/") + "/"
            if hasattr(publisher, "assert_known_revisions"):
                publisher.assert_known_revisions({
                    path: page for path, page in ledger.published_pages.items() if path.startswith(prefix)
                })
            publisher.delete_document(project, raw_rel)
            ledger.published_documents.pop(document, None)
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
            source = dict(ledger.sources.get(rel) or {})
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
            source_id = str(source.get("source_id") or "")
            if source_id:
                for stored in (project.root / "sources").glob(f"{source_id}.*"):
                    stored.unlink()
                identities = _identities(project)
                identities["active"].pop(rel, None)
                identities["tombstones"][rel] = {
                    "source_id": source_id,
                    "source_sha256": str(source.get("source_sha256") or ""),
                    "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                _save_identities(project, identities)
            done.append({"path": rel, "status": "deleted", "touched": touched, "rebuild": "full"})
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
    source_details: dict[str, dict[str, Any]] | None = None,
    prepare_publish: Callable[[], None] | None = None,
    begin_publish: Callable[[], None] | None = None,
    on_revision: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """One reconciliation pass, optionally scoped to mount-relative paths."""
    project = open_project(settings)
    history_enabled = (project.root / ".git").is_dir()
    ledger_path = project.metadata / "pipeline.json"
    run_id = "prun-" + uuid.uuid4().hex[:16]
    done: list[dict[str, Any]] = []
    failures: list[str] = []
    cancelled = False
    with _lock(project):
        if history_enabled:
            from .history import candidate_is_clean, last_good

            if not candidate_is_clean(project, last_good(project)):
                raise RuntimeError("cannot sync from a dirty last-good working tree")
        ledger = load_ledger(ledger_path)
        scan = scan_mount(project.mount, ledger.sources)
        publisher = _publisher(settings)
        if publisher is None:
            raise RuntimeError("GROWI_URL is required for sync/watch; use build for local-only output")
        if on_revision is not None and hasattr(publisher, "on_revision"):
            publisher.on_revision = on_revision
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
        incremental_pages: set[str] = set()
        regenerated_pages: set[str] = set()
        incremental_publish = bool(changed) and not deleted
        scoped_candidates = None if include_pending else (sorted(scoped_raw) if wanted is not None else None)
        pending_before = _pending_link_rels(project, settings, scoped_candidates)
        model = _model(settings, project) if changed or pending_before else None
        try:
            embedder = Embedder(settings) if changed or pending_before else None
        except Exception as exc:
            log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
            embedder = None
        source_details = source_details or {}
        for rel in changed:
            if should_continue is not None and not should_continue():
                cancelled = True
                break
            item = scan.files[rel]
            raw_rel = _raw_rel(item)
            details = source_details.get(rel, {})
            previous_source = dict(ledger.sources.get(rel) or {})
            classification = str(details.get("classification") or "none")
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
                    previous_markdown = (
                        project.raw_file(raw_rel).read_text(encoding="utf-8")
                        if previous_source and project.raw_file(raw_rel).exists()
                        else None
                    )
                    parse_args = (
                        {"previous_markdown": previous_markdown}
                        if previous_markdown is not None
                        else {}
                    )
                    markdown = _parse(item, project.mount / rel, settings, **parse_args)
                _assert_source_unchanged(item, project.mount / rel)
                if previous_markdown is not None and classification != "forced":
                    _check_parse_size(rel, previous_markdown, markdown)
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
                    requested_resume = not force if resume is None else resume
                    if classification == "forced":
                        requested_resume = False
                    result = write_wiki_pages(
                        project, raw_rel, mode=str(settings.ingest_mode), settings=settings,
                        llm=model, embedder=embedder, on_progress=on_progress,
                        resume=requested_resume,
                        stop_check=(lambda: not should_continue()) if should_continue is not None else None,
                        identity_seed=str(
                            previous_source.get("id_seed")
                            or ((Path(raw_rel).parent / wiki_folder_name(Path(raw_rel).name)).as_posix() if previous_source else "")
                            or details.get("source_id")
                            or raw_rel
                        ),
                    )
                if getattr(result, "rebuild", "full") == "incremental":
                    incremental_pages.update(getattr(result, "changed_pages", []))
                    regenerated_pages.update(getattr(result, "regenerated_pages", []))
                else:
                    incremental_publish = False
                if on_progress:
                    on_progress({
                        "stage": "wiki",
                        "step": "done",
                        "file": raw_rel,
                        "touched_documents": len(result.touched),
                        "elapsed_seconds": round(time.monotonic() - wiki_started, 1),
                    })
                ledger.sources[rel] = _source_row(item, raw_rel, details=details, previous=previous_source)
                _store_source(project, item, ledger.sources[rel], project.mount / rel)
                save_ledger(ledger_path, ledger)
                done.append({
                    "path": rel,
                    "status": "changed" if previous_source else "added",
                    "touched": result.touched,
                    "rebuild": "incremental" if getattr(result, "rebuild", "full") == "incremental" else "full",
                    "tier": getattr(result, "tier", 3),
                    "reason": getattr(result, "reason", ""),
                    "human_edits_overwritten": list(getattr(result, "human_edits_overwritten", [])),
                })
                log.info("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
            except asyncio.CancelledError:
                cancelled = True
                break
            except Exception as exc:
                if should_continue is not None and not should_continue():
                    cancelled = True
                    break
                error = f"{type(exc).__name__}: {exc}"[:500]
                ledger.sources[rel] = _source_row(item, raw_rel, error, details=details, previous=previous_source)
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
                        affected_pages=incremental_pages if incremental_publish else None,
                        regenerated_pages=regenerated_pages if incremental_publish else None,
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
            if prepare_publish is not None:
                prepare_publish()
            publish_args = {
                "only": publish_only,
                "on_progress": on_progress,
                **({"only_pages": incremental_pages} if incremental_publish else {}),
                **({"begin_publish": begin_publish} if begin_publish is not None else {}),
            }
            failures.extend(
                _publish_sweep(project, ledger, publisher, run_id, **publish_args)
            )
        save_ledger(ledger_path, ledger)
        if history_enabled and not failures and not cancelled:
            from .history import checkpoint_live

            checkpoint_live(project, f"sync {run_id}")
    return {"run_id": run_id, "scan": scan, "done": done, "failures": failures, "cancelled": cancelled}


def delete_sources(
    settings: Any,
    sources: dict[str, str],
    *,
    should_continue: Callable[[], bool] | None = None,
    prepare_publish: Callable[[], None] | None = None,
    begin_publish: Callable[[], None] | None = None,
    on_revision: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """Idempotently remove queued source documents locally and from GROWI."""
    project = open_project(settings)
    publisher = _publisher(settings)
    if publisher is None:
        raise RuntimeError("GROWI_URL is required for queued deletion")
    if on_revision is not None and hasattr(publisher, "on_revision"):
        publisher.on_revision = on_revision
    ledger_path = project.metadata / "pipeline.json"
    run_id = "del-" + uuid.uuid4().hex[:16]
    with _lock(project):
        ledger = load_ledger(ledger_path)
        if getattr(settings, "wiki_linker_enabled", True):
            from graph.linker.catalog import Catalog

            mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
            catalog = Catalog.open(project.linker_database, mode=mode)
            try:
                catalog.sync_from_planning(project)
            finally:
                catalog.close()
        done, touched, failures = _remove_sources(project, ledger, sources)
        save_ledger(ledger_path, ledger)
        cancelled = should_continue is not None and not should_continue()
        if not failures and not cancelled:
            if prepare_publish is not None:
                prepare_publish()
            failures.extend(_publish_sweep(
                project, ledger, publisher, run_id, only=set(sources.values()) | touched,
                **({"begin_publish": begin_publish} if begin_publish is not None else {}),
            ))
        save_ledger(ledger_path, ledger)
    return {"run_id": run_id, "done": done, "failures": failures, "cancelled": cancelled}


def _replace_metadata_paths(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            key: item if key == "id_seed" else _replace_metadata_paths(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_metadata_paths(item, replacements) for item in value]
    if isinstance(value, str):
        for old, new in replacements.items():
            if value == old:
                return new
            if value.startswith(old.rstrip("/") + "/"):
                return new.rstrip("/") + value[len(old):]
        return value
    return value


def move_sources(
    settings: Any,
    jobs: list[Any],
    *,
    should_continue: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    prepare_publish: Callable[[], None] | None = None,
    begin_publish: Callable[[], None] | None = None,
    on_revision: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """Move generated state and GROWI pages while retaining source/page IDs."""
    project = open_project(settings)
    publisher = _publisher(settings)
    if publisher is None:
        raise RuntimeError("GROWI_URL is required for queued move")
    if on_revision is not None and hasattr(publisher, "on_revision"):
        publisher.on_revision = on_revision
    ledger_path = project.metadata / "pipeline.json"
    run_id = "move-" + uuid.uuid4().hex[:16]
    done: list[dict[str, Any]] = []
    failures: list[str] = []
    touched_raw: set[str] = set()
    with _lock(project):
        ledger = load_ledger(ledger_path)
        identities = _identities(project)
        moves: list[tuple[Any, str, str, str, dict[str, dict[str, Any]]]] = []
        try:
            for job in jobs:
                if should_continue is not None and not should_continue():
                    return {"run_id": run_id, "done": done, "failures": failures, "cancelled": True}
                old_rel, new_rel = str(job.from_rel), str(job.rel)
                source = ledger.sources.get(old_rel)
                if not source:
                    failures.append(f"{old_rel}: source identity missing for move")
                    continue
                old_raw = str(source.get("raw_rel") or job.raw_rel)
                new_raw = (Path(new_rel).parent / raw_name_for(Path(new_rel).name)).as_posix()
                old_document = project.wiki_dir(old_raw).relative_to(project.wiki).as_posix()
                new_document = project.wiki_dir(new_raw).relative_to(project.wiki).as_posix()
                for old_path, new_path in (
                    (project.raw_file(old_raw), project.raw_file(new_raw)),
                    (project.wiki_dir(old_raw), project.wiki_dir(new_raw)),
                    (project.state_dir(old_raw), project.state_dir(new_raw)),
                ):
                    if old_path.exists() and old_path != new_path:
                        if new_path.exists():
                            raise FileExistsError(f"move target already exists: {new_path}")
                        new_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(old_path), str(new_path))
                replacements = {old_raw: new_raw, old_document: new_document, old_rel: new_rel}
                planning = project.wiki_dir(new_raw) / "_planning"
                for path in planning.glob("*.json") if planning.is_dir() else ():
                    data = read_json(path, default={})
                    if isinstance(data, dict):
                        data = _replace_metadata_paths(data, replacements)
                        if path.name in {"source.json", "chunks.json"}:
                            data["id_seed"] = str(source.get("id_seed") or old_document)
                        write_json_atomic(path, data)
                source.update({
                    "mount_rel": new_rel,
                    "raw_rel": new_raw,
                    "wiki_rel": new_rel,
                    "source_sha256": str(job.target_sha256 or source.get("source_sha256") or ""),
                    "source_blob_oid": str(job.target_blob_oid or source.get("source_blob_oid") or ""),
                })
                if (project.mount / new_rel).is_file():
                    stat = (project.mount / new_rel).stat()
                    source.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
                ledger.sources.pop(old_rel, None)
                ledger.sources[new_rel] = source
                identities["active"].pop(old_rel, None)
                identities["active"][new_rel] = str(source["source_id"])
                if old_document in ledger.published_documents:
                    ledger.published_documents[new_document] = ledger.published_documents.pop(old_document)
                    ledger.published_documents[new_document]["raw_rel"] = new_raw
                old_prefix = old_document.rstrip("/") + "/"
                moved_pages: dict[str, dict[str, Any]] = {}
                for local_path, row in list(ledger.published_pages.items()):
                    if local_path.startswith(old_prefix):
                        moved_pages[new_document + local_path[len(old_document):]] = ledger.published_pages.pop(local_path)
                ledger.published_pages.update(moved_pages)
                moves.append((job, old_raw, new_raw, new_document, moved_pages))
            if failures:
                return {"run_id": run_id, "done": done, "failures": failures, "cancelled": False}
            _save_identities(project, identities)
            if getattr(settings, "wiki_linker_enabled", True) and moves:
                from graph.linker.catalog import Catalog

                mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
                catalog = Catalog.open(project.linker_database, mode=mode)
                try:
                    catalog.sync_from_planning(project)
                finally:
                    catalog.close()
                touched_raw.update(run_linkers(
                    project, [new_raw for _job, _old_raw, new_raw, _document, _pages in moves],
                    settings=settings, llm=None, embedder=None, on_progress=on_progress,
                    stop_check=(lambda: not should_continue()) if should_continue is not None else None,
                ))
            save_ledger(ledger_path, ledger)
            if should_continue is not None and not should_continue():
                return {"run_id": run_id, "done": done, "failures": failures, "cancelled": True}
            if prepare_publish is not None:
                prepare_publish()
            if moves and begin_publish is not None:
                begin_publish()
            for job, old_raw, new_raw, new_document, moved_pages in moves:
                remote = publisher.move_document(project, old_raw, new_raw, moved_pages)
                for local_path, page in remote.items():
                    moved_pages[local_path] = {
                        "growi_path": page.path,
                        "page_id": page.page_id,
                        "revision_id": page.revision_id,
                        "marker_id": publisher.page_marker_id(project, local_path),
                    }
                ledger.published_pages.update(moved_pages)
                if new_document in ledger.published_documents:
                    ledger.published_documents[new_document]["growi_path"] = publisher.doc_path(project, new_raw)
                save_ledger(ledger_path, ledger)
                done.append({"path": str(job.rel), "from": str(job.from_rel), "status": "moved", "rebuild": "move"})
            if touched_raw:
                failures.extend(_publish_sweep(
                    project, ledger, publisher, run_id, only=touched_raw, on_progress=on_progress,
                    **({"begin_publish": begin_publish} if begin_publish is not None else {}),
                ))
            save_ledger(ledger_path, ledger)
        except Exception as exc:
            failures.append(f"move: {type(exc).__name__}: {exc}")
    return {"run_id": run_id, "done": done, "failures": failures, "cancelled": False}


def _sources_by_id(ledger: Ledger) -> dict[str, tuple[str, dict[str, Any]]]:
    return {
        str(row.get("source_id")): (rel, row)
        for rel, row in ledger.sources.items()
        if row.get("source_id")
    }


def _affected_documents(live: Project, staged: Project, base: Ledger, candidate: Ledger) -> set[str]:
    affected: set[str] = set(base.published_documents) ^ set(candidate.published_documents)
    base_sources, candidate_sources = _sources_by_id(base), _sources_by_id(candidate)
    for source_id in set(base_sources) | set(candidate_sources):
        old = base_sources.get(source_id)
        new = candidate_sources.get(source_id)
        if old == new:
            continue
        for project, item in ((live, old), (staged, new)):
            if item is not None:
                affected.add(project.wiki_dir(str(item[1]["raw_rel"])).relative_to(project.wiki).as_posix())
    for document in set(base.published_documents) & set(candidate.published_documents):
        old, new = base.published_documents[document], candidate.published_documents[document]
        folder = staged.wiki / document
        if old.get("content_sha256") != new.get("content_sha256") or (
            folder.is_dir() and old.get("content_sha256") != _content_hash(folder)
        ):
            affected.add(document)
    return affected


def candidate_publication_complete(settings: Any, candidate_project: Project) -> bool:
    """Return true only for a committed candidate whose remote revisions match."""
    live = open_project(settings)
    publisher = _publisher(settings)
    if publisher is None:
        return False
    base = load_ledger(live.metadata / "pipeline.json")
    candidate = load_ledger(candidate_project.metadata / "pipeline.json")
    folders = _folders(candidate_project)
    if set(folders) != set(candidate.published_documents):
        return False
    for document, folder in folders.items():
        row = candidate.published_documents[document]
        if row.get("content_sha256") != _content_hash(folder):
            return False
        if row.get("growi_path") != publisher.doc_path(candidate_project, str(row.get("raw_rel") or _document_raw_rel(document))):
            return False
        local_pages = {path.relative_to(candidate_project.wiki).as_posix() for path in folder.glob("*.md")}
        if local_pages != {path for path in candidate.published_pages if Path(path).parent.as_posix() == document}:
            return False

    async def check() -> bool:
        for row in candidate.published_pages.values():
            if not row.get("page_id") or not row.get("revision_id") or not row.get("growi_path"):
                return False
            page = await publisher.client.get_page(page_id=str(row["page_id"]))
            if page is None or page.revision_id != row.get("revision_id") or page.path != row.get("growi_path"):
                return False
        candidate_ids = {str(row.get("page_id")) for row in candidate.published_pages.values() if row.get("page_id")}
        for row in base.published_pages.values():
            page_id = str(row.get("page_id") or "")
            if page_id and page_id not in candidate_ids and await publisher.client.get_page(page_id=page_id) is not None:
                return False
        return True

    return asyncio.run(check())


def restore_publication(
    settings: Any,
    candidate_project: Project,
    jobs: list[Any],
    *,
    known_revisions: dict[str, set[str]] | None = None,
) -> str:
    """Restore affected base documents without overwriting unknown revisions."""
    del jobs  # The two ledgers are the authoritative transaction scope.
    live = open_project(settings)
    publisher = _publisher(settings)
    if publisher is None:
        return
    base = load_ledger(live.metadata / "pipeline.json")
    candidate = load_ledger(candidate_project.metadata / "pipeline.json")
    affected = _affected_documents(live, candidate_project, base, candidate)
    known_revisions = known_revisions or {}
    base_by_id, candidate_by_id = _sources_by_id(base), _sources_by_id(candidate)
    candidate_only_raw = [
        str(row["raw_rel"]) for source_id, (_rel, row) in candidate_by_id.items()
        if source_id not in base_by_id
    ]
    scoped_rows = [
        row for pages in (base.published_pages, candidate.published_pages) for path, row in pages.items()
        if Path(path).parent.as_posix() in affected and row.get("page_id")
    ]

    async def conflicts() -> list[str]:
        problems: list[str] = []
        by_id: dict[str, set[str]] = {}
        candidate_ids = {
            str(row.get("page_id")) for row in candidate.published_pages.values() if row.get("page_id")
        }
        for row in scoped_rows:
            page_id = str(row["page_id"])
            by_id.setdefault(page_id, set()).add(str(row.get("revision_id") or ""))
        for page_id, revisions in known_revisions.items():
            by_id.setdefault(page_id, set()).update(revisions)
        for page_id, revisions in by_id.items():
            current = await publisher.client.get_page(page_id=page_id)
            if current is None and (page_id in candidate_ids or page_id in known_revisions):
                problems.append(f"{page_id}: GROWI page disappeared during publication")
            elif current is not None and current.revision_id not in revisions:
                problems.append(f"{current.path}: unknown GROWI revision {current.revision_id}")
        for raw_rel in candidate_only_raw:
            doc_path = publisher.doc_path(candidate_project, raw_rel)
            for listed in await publisher.client.list_all_pages(doc_path):
                if listed.path == doc_path:
                    continue
                current = await publisher.client.get_page(page_id=listed.page_id)
                revisions = by_id.get(listed.page_id, set())
                if current is not None and current.revision_id not in revisions:
                    problems.append(f"{current.path}: unknown GROWI revision {current.revision_id}")
        return problems

    problems = asyncio.run(conflicts())
    if problems:
        raise RuntimeError("rollback conflict; " + "; ".join(problems))

    moved_candidate_docs: set[str] = set()
    for source_id in set(base_by_id) & set(candidate_by_id):
        _old_rel, old = base_by_id[source_id]
        _new_rel, new = candidate_by_id[source_id]
        old_raw, new_raw = str(old["raw_rel"]), str(new["raw_rel"])
        if old_raw == new_raw:
            continue
        new_document = candidate_project.wiki_dir(new_raw).relative_to(candidate_project.wiki).as_posix()
        pages = {
            path: row for path, row in candidate.published_pages.items()
            if Path(path).parent.as_posix() == new_document
        }
        publisher.move_document(live, new_raw, old_raw, pages, check_revisions=False)
        moved_candidate_docs.add(new_document)

    base_rels = [
        str(row["raw_rel"])
        for document, row in base.published_documents.items()
        if document in affected
    ]
    if base_rels:
        restored_pages = publisher.publish_documents(live, base_rels, known_pages=base.published_pages)
        base_prefixes = {
            document.rstrip("/") + "/"
            for document in affected
            if document in base.published_documents
        }
        base.published_pages = {
            path: row for path, row in base.published_pages.items()
            if not path.startswith(tuple(base_prefixes))
        }
        base.published_pages.update({
            local_path: {
                "growi_path": page.path,
                "page_id": page.page_id,
                "revision_id": page.revision_id,
                "marker_id": publisher.page_marker_id(live, local_path),
            }
            for local_path, page in restored_pages.items()
        })
        published_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for document in affected & set(base.published_documents):
            row = base.published_documents[document]
            raw_rel = str(row.get("raw_rel") or _document_raw_rel(document))
            row.update({
                "content_sha256": _content_hash(live.wiki / document),
                "growi_path": publisher.doc_path(live, raw_rel),
                "published_at": published_at,
            })
    base_source_ids = set(base_by_id)
    for source_id, (_rel, row) in candidate_by_id.items():
        document = candidate_project.wiki_dir(str(row["raw_rel"])).relative_to(candidate_project.wiki).as_posix()
        if source_id not in base_source_ids and document in affected and document not in moved_candidate_docs:
            publisher.delete_document(candidate_project, str(row["raw_rel"]))
    save_ledger(live.metadata / "pipeline.json", base)
    from .history import checkpoint_live

    return checkpoint_live(live, "restore last-good publication")


def pull_growi_once(settings: Any) -> dict[str, Any]:
    """Pull user revisions into local wiki state without rebuilding or publishing."""
    project = open_project(settings)
    history_enabled = (project.root / ".git").is_dir()
    if history_enabled:
        from .history import candidate_is_clean, last_good

        if not candidate_is_clean(project, last_good(project)):
            raise RuntimeError("cannot pull GROWI changes into a dirty last-good working tree")
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
            if not failures and history_enabled:
                from .history import checkpoint_live

                checkpoint_live(project, f"pull GROWI {run_id}")
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
        if not failures and (project.root / ".git").is_dir():
            from .history import checkpoint_live

            checkpoint_live(project, f"publish {run_id}")
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
        from publisher.index import delete_index_pages
        delete_index_pages(settings)  # index pages carry no chunk marker, reset() skips them
        ledger = load_ledger(ledger_path)
        ledger.published_documents.clear()
        ledger.published_pages.clear()
        save_ledger(ledger_path, ledger)
        if (project.root / ".git").is_dir():
            from .history import checkpoint_live

            checkpoint_live(project, "reset GROWI publication")
    return {"run_id": "reset-" + uuid.uuid4().hex[:16], "done": [{"status": "reset", "deleted": deleted}], "failures": []}


__all__ = ["build_raw", "build_wiki_only", "candidate_publication_complete", "delete_sources", "link_raw", "move_sources", "publish_only", "pull_growi_once", "reset_growi", "restore_publication", "sync_once"]
