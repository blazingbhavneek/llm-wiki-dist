"""One entry point for the publisher. `python main.py -h`.

check                       ping chat/embed/parser/GROWI endpoints + the local Jev backend
convert                     external mount -> raw Markdown only
build wiki [<raw-rel>...]   raw/ -> wiki pages only
build link [<raw-rel>...]   link pending wiki pages only
build all [<raw-rel>...]    wiki batch first, then link batch (bare build is an alias)
publish                     publish the current wiki/ tree to GROWI
pull                        capture GROWI edits and update the local human overlay
index [<raw-rel>...]        publish per-document + root index pages for growi-search
sync [<mount-rel>...]       scan and drain queue -> candidate -> GROWI -> commit
                            then reconcile every index page against the wiki tree
watch [<mount-rel>...]      queued 10-second metadata watcher + worker
queue scan|work|status      operate the persistent watcher queue
reset                       trash all publisher-owned GROWI pages
link                        link every pending wiki (same as `build link`)
link status | relink <doc> | rebuild --mode legacy|neo [--no-edges]

Every command selects one INI from configs/ (or an absolute INI) with --project.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from graph import config
from graph.config import Settings, resolve_project_path
from graph.workspace.project import open_project

PROJECT_ROOT = Path(__file__).resolve().parent.parent
config.PROJECT_ROOT = PROJECT_ROOT

log = logging.getLogger(__name__)
LOG_FORMAT = "%(levelname)s %(message)s"


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env(getattr(args, "project", ""))
    # .env (WIKI_CHAT_*/OPENAI_*/WIKI_MODEL) is the default; the project INI is
    # project-specific and already wins inside Settings.from_env.
    if getattr(args, "data_root", None):
        settings.data_root = str(resolve_project_path(args.data_root).resolve())
    if getattr(args, "mode", None) and args.command != "link":
        settings.ingest_mode = args.mode
    linker = getattr(args, "linker", None)
    if linker == "off":
        settings.wiki_linker_enabled = False
    elif linker:
        settings.wiki_linker_enabled = True
        settings.wiki_linker_mode = linker
    if getattr(args, "timeout", None):
        settings.wiki_request_timeout = args.timeout
    if getattr(args, "fast", False):
        settings.policy = "fast"
    return settings


def _progress(event: dict[str, Any]) -> None:
    event = dict(event)
    stage = str(event.pop("stage", "work"))
    step = str(event.pop("step", ""))
    current, total = event.get("current"), event.get("total")
    progress = ""
    if isinstance(current, int) and isinstance(total, int) and total > 0:
        progress = f" {current}/{total} ({current * 100 // total}%)"
        event.pop("current", None)
        event.pop("total", None)
    details = json.dumps(event, ensure_ascii=False, default=str)
    log.debug(f"[{stage}] {step}{progress} {details}".rstrip())


def _report(result: dict[str, Any]) -> int:
    for row in result["done"]:
        print(json.dumps(row, ensure_ascii=False))
    for failure in result["failures"]:
        logging.error("%s", failure)
    return 1 if result["failures"] else 0


def _jev_check(settings: Settings) -> tuple[str, int]:
    """Resolve the project's Jev backend and, for gguf, start the local runtime once.

    The linker and the index builder only log a warning when the Jev engine fails to
    start and then fall back to the LLM, so a broken GGUF path would surface as slow,
    differently-scored links instead of an error. This proves the configured snapshot
    and jev-score binary answer one decision, without any sync, and it never downloads:
    a missing snapshot is reported rather than fetched.
    """

    from jev import JevConfig, JevQuestion, get_engine_for, reset_engine

    try:
        config = JevConfig.from_settings(settings)
    except Exception as exc:
        return f"jev     DOWN config ({type(exc).__name__}: {exc})", 1
    wanted = (str(getattr(settings, "wiki_linker_judge", "llm")) == "jev"
              or bool(getattr(settings, "wiki_index_related_docs", False)))
    if not wanted:
        return f"jev     skipped (judge={settings.wiki_linker_judge}, related-docs off)", 0
    if config.backend != "gguf":
        return f"jev     {config.backend} backend (local start not checked)", 0
    if not config.gguf_local_path:
        return "jev     DOWN wiki_jev_gguf_local_path is empty", 1
    folder = Path(config.gguf_local_path).expanduser()
    files = ("jev_style_decision_gguf.py", "readout_config.json", "tokenizer/tokenizer.json",
             f"Jev-Style-0.8B-Decision-v3-{config.gguf_quant}.gguf")
    missing = [name for name in files if not (folder / name).is_file()]
    binary = Path(config.gguf_binary).expanduser() if config.gguf_binary else folder / "build" / "jev-score"
    if not binary.is_file():
        missing.append(str(binary))
    elif not os.access(binary, os.X_OK):
        return f"jev     DOWN {binary} is not executable", 1
    if missing:
        return f"jev     DOWN missing from {folder}: {', '.join(missing)}", 1
    try:
        engine = get_engine_for(settings)
        result = engine.decide(
            {"section": {"page": "check", "heading": "接続手順",
                         "text": "本節ではシステムAからシステムBへの接続手順を説明する。"}},
            JevQuestion("この節は接続手順を説明しているか？"),
        )
    except Exception as exc:
        return f"jev     DOWN {type(exc).__name__}: {exc}", 1
    finally:
        reset_engine()
    return (f"jev     gguf {config.gguf_quant}/{config.gguf_many_mode} "
            f"p_yes={float(result.p_yes):.3f} {binary}"), 0


def cmd_check(args: argparse.Namespace) -> int:
    import requests

    settings = _settings(args)
    growi = str(settings.growi_url or os.environ.get("GROWI_URL", "")).rstrip("/")
    targets = {
        "chat": f"{settings.chat_base_url.rstrip('/')}/models",
        "embed": f"{settings.embed_base_url.rstrip('/')}/models",
        "parser": f"{settings.parser_base_url.rstrip('/')}/health" if settings.parser_base_url else "",
        "growi": f"{growi}/_api/v3/healthcheck" if growi else "",
    }
    bad = 0
    for name, url in targets.items():
        if not url:
            print(f"{name:7} skipped (not configured)")
            continue
        try:
            response = requests.get(url, timeout=5)
            print(f"{name:7} {response.status_code} {url}")
            bad += response.status_code >= 400
        except Exception as exc:
            print(f"{name:7} DOWN {url} ({type(exc).__name__})")
            bad += 1
    jev_line, jev_bad = _jev_check(settings)
    print(jev_line)
    bad += jev_bad
    project = open_project(settings)
    print(f"data    {project.root.resolve()}  mount={project.mount} ingest={settings.ingest_mode} linker={'off' if not settings.wiki_linker_enabled else settings.wiki_linker_mode}")
    return 1 if bad else 0


def cmd_sync(args: argparse.Namespace) -> int:
    from publisher import pipeline
    from publisher.queue import retry_failed, scan, work_once, worker_lock
    from publisher.progress import SyncProgress

    settings = _settings(args)
    requested_isolation = getattr(args, "isolated", None)
    isolated = (
        bool(getattr(settings, "sync_isolated", True))
        if requested_isolation is None
        else bool(requested_isolation)
    )
    if isolated:
        return _cmd_sync_isolated(args, settings)
    project = open_project(settings)
    combined: dict[str, Any] = {"done": [], "failures": []}
    progress = SyncProgress()

    def on_event(event: dict[str, Any]) -> None:
        progress.on_event(event)
        if args.verbose:
            _progress(event)

    try:
        stale = pipeline.republish_if_stale(settings)
        if stale is not None:
            print(f"growi endpoint or markers changed: full republish {stale['run_id']}", flush=True)
            combined["failures"].extend(stale["failures"])
        with worker_lock(project):
            retry_failed(project)
            first = True
            resume = bool(getattr(args, "continue_run", False))
            while True:
                scan_result = scan(
                    settings,
                    only=args.items or None,
                    settle_seconds=0,
                    force=args.force and first,
                    verify_content=True,
                )
                progress.add_scan_result(scan_result)
                first = False
                result = work_once(settings, on_event=on_event, continue_run=resume)
                # Only the first batch can resume a kept worktree; later batches
                # in the same process are always fresh.
                resume = False
                if result is None:
                    break
                paths = result.get("paths", [])
                progress.add_documents(paths)
                combined["done"].extend(result.get("done", []))
                combined["failures"].extend(result.get("failures", []))
                if result.get("failures"):
                    break
                progress.mark_completed(paths)
        if not combined["failures"]:
            # Index pages are derived output, so reconcile them against the whole wiki tree
            # here: a wiki built before its index exists catches up, and a page whose body
            # already matches GROWI is read but never rewritten.
            from publisher.index import build_index

            try:
                index_callback = on_event if args.verbose or progress.has_documents else None
                index = build_index(settings, on_progress=index_callback)
            except Exception as exc:  # a stale table of contents must not fail a sync
                index = {"done": [], "failures": [f"index: {type(exc).__name__}: {exc}"]}
            combined["done"].extend(index["done"])
            combined["failures"].extend(index["failures"])
        return _report(combined)
    finally:
        progress.close()


def _cmd_sync_isolated(args: argparse.Namespace, settings: Settings) -> int:
    """One retry owner; each operation starts from the latest accepted main."""
    import traceback
    import uuid

    from publisher import pipeline
    from publisher.failure_logs import save_failure
    from publisher.queue import recover, retry_failed, scan, status, work_once, worker_lock
    from publisher.progress import SyncProgress

    project = open_project(settings)
    run_id = "sync-" + uuid.uuid4().hex
    combined: dict[str, Any] = {"done": [], "failures": []}
    progress = SyncProgress()
    wanted = {item.strip().lstrip("/") for item in args.items or ()}
    logs: dict[str, list[str]] = {}
    discovery: dict[str, str] = {}
    recorded: set[tuple[int, str, str]] = set()
    recovered: list[str] = []
    blocked = False

    def selected(row: dict[str, Any]) -> bool:
        return not wanted or row["rel"] in wanted or row.get("from_rel") in wanted

    def on_event(event: dict[str, Any]) -> None:
        progress.on_event(event)
        if args.verbose:
            _progress(event)

    try:
        with worker_lock(project):
            # Uncertain remote state must be examined before pruning, scanning,
            # retrying jobs, or issuing any unrelated publication.
            recover(project, settings)
            stale = pipeline.republish_if_stale(settings)
            if stale is not None and stale.get("failures"):
                combined["failures"].extend(stale["failures"])
                return _report(combined)
            retry_failed(project, only=args.items or None)
            scanned = scan(
                settings,
                only=args.items or None,
                settle_seconds=0,
                force=args.force,
                verify_content=True,
            )
            progress.add_scan_result(scanned)
            progress.add_documents(
                row["rel"] for row in status(project)
                if selected(row) and row["status"] in {"queued", "running", "failed"}
            )
            discovery = {
                rel: error for rel, error in scanned.get("errors", {}).items()
                if not wanted or rel in wanted or rel == "." or any(
                    item.startswith(rel.rstrip("/") + "/") for item in wanted
                )
            }
            for rel, error in discovery.items():
                key = (1, rel, error)
                recorded.add(key)
                path = save_failure(
                    settings,
                    project,
                    run_id,
                    1,
                    rel,
                    {"stage": "scan", "error": error},
                    error,
                )
                logs.setdefault(rel, []).append(path)
                log.error("source discovery failed: %s; log=%s", rel, path)
            attempt = 1
            while True:
                result = work_once(settings, on_event=on_event, isolated=True,
                                   only=args.items or None, run_id=run_id, attempt=attempt,
                                   exclude=list(discovery), defer_linker=True)
                if result is None:
                    if attempt == 2:
                        break
                    # Snapshot exact current versions once. No scan or worker
                    # loop below automatically requeues failed final attempts.
                    targets = {row["rel"]: row["version"] for row in status(project)
                               if selected(row) and row["status"] == "failed"}
                    retry_failed(project, only=args.items or None, versions=targets)
                    attempt = 2
                    log.info("first pass complete; final retry documents=%d discovery=%d", len(targets), len(discovery))
                    continue
                paths = result.get("paths", [])
                progress.add_documents(paths)
                evidence = result.get("failure_logs", [])
                if evidence:
                    for rel in paths:
                        logs.setdefault(rel, []).extend(evidence)
                if result.get("recovery_required") or result.get("cancelled"):
                    blocked = True
                    combined["failures"].extend(result.get("failures") or ["sync cancelled; pending jobs retained"])
                    break
                if result.get("failures"):
                    log.warning("document attempt %d failed: %s; logs=%s", attempt, paths, result.get("failure_logs", []))
                    continue
                combined["done"].extend(result.get("done", []))
                progress.mark_completed(paths)
                if attempt == 2:
                    recovered.extend(paths)
            if not blocked and getattr(settings, "wiki_linker_enabled", True):
                link_only = None
                if wanted:
                    from publisher.ledger import load_ledger

                    ledger = load_ledger(project.metadata / "pipeline.json")
                    link_only = sorted({
                        str(row.get("raw_rel") or "")
                        for rel, row in ledger.sources.items()
                        if (rel in wanted or str(row.get("mount_rel") or "") in wanted)
                        and row.get("raw_rel")
                    })
                linked = pipeline.link_pending_isolated(
                    settings,
                    only=link_only,
                    on_progress=on_event,
                )
                combined["done"].extend(linked.get("done", []))
                combined["failures"].extend(linked.get("failures", []))
            pending = [row for row in status(project) if selected(row)]
            for row in pending:
                evidence = ", ".join(logs.get(row["rel"], []))
                combined["failures"].append(
                    f"{row['rel']}: {row.get('error') or 'pending dependent work'}"
                    + (f"; logs: {evidence}" if evidence else "")
                )
            for rel, error in discovery.items():
                combined["failures"].append(f"scan {rel}: {error.splitlines()[-1]}; logs: {', '.join(logs.get(rel, []))}")
            if not blocked:
                from publisher.index import build_index

                try:
                    index = build_index(settings, on_progress=on_event if args.verbose or progress.has_documents else None)
                    combined["done"].extend(index.get("done", []))
                    combined["failures"].extend(index.get("failures", []))
                except Exception as exc:
                    combined["failures"].append(f"index: {type(exc).__name__}: {exc}")
        if recovered:
            print(json.dumps({"recovered_on_final_retry": recovered}, ensure_ascii=False))
        if logs:
            print(json.dumps({"failure_logs": logs, "run_id": run_id}, ensure_ascii=False))
        return _report(combined)
    except Exception as exc:
        evidence = save_failure(settings, project, run_id, 1, "<shared>",
                                {"stage": "sync", "error": traceback.format_exc()}, traceback.format_exc())
        combined["failures"].append(f"shared sync failure: {type(exc).__name__}: {exc}; log: {evidence}")
        return _report(combined)
    finally:
        progress.close()


def cmd_pull(args: argparse.Namespace) -> int:
    from publisher.pipeline import pull_growi_once

    settings = _settings(args)
    result = (
        pull_growi_once(settings, force_inventory=True)
        if getattr(args, "inventory", False)
        else pull_growi_once(settings)
    )
    if result.get("human_sync"):
        print(json.dumps({"human_sync": result["human_sync"]}, ensure_ascii=False, default=str))
    return _report(result)


def cmd_human(args: argparse.Namespace) -> int:
    from publisher.human_changes import HumanStore

    settings = _settings(args)
    project = open_project(settings)
    store = HumanStore(project)
    if args.human_command == "status":
        print(json.dumps(store.project_summary(), ensure_ascii=False, default=str))
        return 0
    if args.human_command == "resolve":
        combined = ""
        if args.text_file:
            combined = Path(args.text_file).read_text(encoding="utf-8")
        result = store.resolve(
            args.edit_id,
            action=args.action,
            expected_revision=args.revision,
            combined_text=combined,
            document=args.document or "",
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.human_command == "recover-legacy":
        from publisher.legacy_recovery import recover_legacy_ancestor

        result = recover_legacy_ancestor(store, args.document)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0 if result.get("status") in {"recovered", "no_legacy_pin"} else 1
    if args.human_command == "live-plan":
        from publisher.live_verification import LiveVerificationReport

        report = LiveVerificationReport.create(project, settings, args.path)
        print(json.dumps({
            "report": str(report.path),
            "resolved_boundary": report.data["disposable_path"],
            "endpoint": report.data["endpoint"],
            "confirmation_code": report.data["confirmation_code"],
            "status": report.data["status"],
        }, ensure_ascii=False))
        return 0
    raise ValueError(f"unknown human command: {args.human_command}")


def cmd_watch(args: argparse.Namespace) -> int:
    from publisher.queue import serve

    settings = _settings(args)
    try:
        serve(
            settings,
            only=args.items or None,
            interval=args.interval,
            growi_interval=args.growi_interval,
            force=args.force,
            on_event=_progress if args.verbose else None,
            continue_run=bool(getattr(args, "continue_run", False)),
        )
    except KeyboardInterrupt:
        return 0


def cmd_queue(args: argparse.Namespace) -> int:
    from publisher.pipeline import _lock
    from publisher.queue import recover, resumable_operation_id, retry_failed, scan, status, work_once, worker_lock

    settings = _settings(args)
    project = open_project(settings)
    if args.queue_command == "scan":
        print(json.dumps(scan(settings, only=args.items or None, settle_seconds=args.settle, force=args.force), ensure_ascii=False))
        return 0
    if args.queue_command == "status":
        for row in status(project):
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.queue_command == "retry":
        print(json.dumps({"retried": retry_failed(project)}))
        return 0
    resume = bool(getattr(args, "continue_run", False))
    with worker_lock(project):
        with _lock(project):
            preserved = resumable_operation_id(project, adopt_orphan=True) if resume else None
            recover(project, settings, preserve_operation_id=preserved)
        while True:
            result = work_once(settings, continue_run=resume)
            # Only the first batch can resume a kept worktree.
            resume = False
            if result is not None:
                print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
            if args.once:
                index_failures = (result or {}).get("index", {}).get("failures", [])
                return 1 if result and (result.get("failures") or index_failures) else 0
            if result is None:
                time.sleep(0.5)


def cmd_convert(args: argparse.Namespace) -> int:
    settings = _settings(args)
    from runner.steps import convert

    result = convert(settings, on_progress=_progress if args.verbose else None)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["failed"] else 0


def cmd_build(args: argparse.Namespace) -> int:
    items = list(args.items)
    phase = items.pop(0) if items and items[0] in {"wiki", "link", "all"} else "all"
    rels = items
    if args.from_file:
        rels += [line.strip() for line in Path(args.from_file).read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    from runner.steps import build

    return _report(build(
        _settings(args), phase=phase, only=rels or None, force=args.force,
        on_progress=_progress if args.verbose else None,
    ))


def cmd_publish(args: argparse.Namespace) -> int:
    allow_unlinked = bool(getattr(args, "allow_unlinked", False))
    from publisher.phase import Config, Input, run

    settings = _settings(args)
    root = Path(settings.data_root) / settings.target_name
    result = run(
        Config(
            action="publish",
            publish=True,
            settings=settings,
            allow_unlinked=allow_unlinked,
            link_pending=not allow_unlinked,
        ),
        Input(root),
        root,
    )
    return _report({"done": list(result.pages), "failures": list(result.failures)})


def cmd_index(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if args.delete:
        from publisher.index import delete_index_pages

        return _report(delete_index_pages(settings))
    from runner.steps import index

    return _report(index(
        settings, only=args.items or None, publish=not args.no_publish,
        on_progress=_progress if args.verbose else None,
    ))


def cmd_reset(args: argparse.Namespace) -> int:
    from publisher.pipeline import reset_growi

    return _report(reset_growi(_settings(args)))


def cmd_link(args: argparse.Namespace) -> int:
    from graph.linker import __main__ as linker_cli

    settings = _settings(args)
    project = open_project(settings)
    if args.link_command is None:
        from publisher.pipeline import link_raw
        return _report(link_raw(settings, force=args.force, on_progress=_progress if args.verbose else None))
    if args.link_command == "status":
        import sqlite3

        if not project.linker_database.exists():
            print({"mode": None, "documents": 0, "chunks": 0, "edges": 0}); return 0
        with sqlite3.connect(project.linker_database) as conn:
            mode = conn.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
            counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("documents", "chunks", "edges")}
        print({"mode": mode[0] if mode else None, **counts})
    elif args.link_command == "rebuild":
        linker_cli.rebuild(project, settings, args.mode, args.no_edges)
    else:
        from graph.common.async_tools import run_async_blocking
        from graph.linker import link_document

        rel = args.document.strip("/")
        if rel in linker_cli._documents(project):
            rel = linker_cli._raw_rel(project, rel)
        elif not project.raw_file(rel).exists():
            raise SystemExit(f"unknown document: {args.document} (wiki folder or raw rel path)")
        model, embedder = linker_cli._model_and_embedder(settings, project, Path(rel).parent.as_posix())
        result = run_async_blocking(link_document(project, rel, model=model, embedder=embedder, settings=settings, on_progress=_progress if args.verbose else None))
        print(json.dumps({"touched": result.touched_documents}, ensure_ascii=False))
        if result.touched_documents:
            print("run `python main.py publish` to push the touched documents to GROWI", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python main.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=False, help="config name from configs/ or absolute INI path")
    parser.add_argument("--data-root", help="override the selected INI data_root")
    parser.add_argument("-v", "--verbose", action="store_true", help="print pipeline progress events")
    sub = parser.add_subparsers(dest="command", required=True)
    def project_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--project", default=argparse.SUPPRESS, help="config name from configs/ or absolute INI path")
        p.add_argument("--data-root", default=argparse.SUPPRESS, help="override selected INI data_root (may also go before the command)")

    check = sub.add_parser("check", help="ping chat/embed/parser/GROWI endpoints and the local Jev backend"); project_flags(check); check.set_defaults(fn=cmd_check)

    def pipeline_flags(p: argparse.ArgumentParser) -> None:
        project_flags(p)
        p.add_argument("--mode", choices=("wiki", "chunks"), help="ingest mode (default WIKI_INGEST_MODE)")
        p.add_argument("--linker", choices=("legacy", "neo", "off"), help="linker mode (default WIKI_LINKER_MODE)")
        p.add_argument("--timeout", type=int, help="per-call model timeout in seconds (default WIKI_REQUEST_TIMEOUT)")

    convert = sub.add_parser("convert", help="convert the configured mount to raw Markdown only"); project_flags(convert); convert.set_defaults(fn=cmd_convert)
    sync = sub.add_parser("sync", help="scan and drain the configured project's durable queue"); pipeline_flags(sync)
    sync.description = "Drain the queue, then reconcile index pages for the whole wiki tree."
    sync.add_argument("items", nargs="*", metavar="mount-rel", help="mount-relative source paths; omit for the full project")
    sync.add_argument("--force", action="store_true", help="regenerate selected sources even when unchanged")
    sync.add_argument("--fast", action="store_true", help="use the fast wiki and linker policies for changed sources")
    sync.add_argument(
        "--isolated", action=argparse.BooleanOptionalAction, default=None,
        help="build all sources with one final retry pass, then do the same for pending links (default: enabled; use --no-isolated for legacy batch mode)",
    )
    sync.add_argument("--continue", dest="continue_run", action="store_true",
                      help="in batch mode, resume a kept candidate; isolated mode recovers it and starts from accepted main")
    sync.set_defaults(fn=cmd_sync)
    watch = sub.add_parser("watch", help="run the metadata scanner and persistent queue worker"); pipeline_flags(watch)
    watch.add_argument("items", nargs="*", metavar="mount-rel", help="mount-relative source paths; omit for the full project")
    watch.add_argument("--force", action="store_true", help="queue selected sources once at startup even when unchanged")
    watch.add_argument("--continue", dest="continue_run", action="store_true",
                       help="resume the previous run's kept candidate worktree instead of discarding it")
    watch.add_argument("--interval", type=float, default=float(os.environ.get("PUBLISHER_INTERVAL_SECONDS", "10")))
    watch.add_argument("--growi-interval", type=float, default=float(os.environ.get("GROWI_WATCH_INTERVAL_SECONDS", "300")), help="seconds between low-priority GROWI revision checks; 0 disables")
    watch.set_defaults(fn=cmd_watch)

    queue = sub.add_parser("queue", help="inspect or operate the persistent watcher queue"); project_flags(queue)
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_scan = queue_sub.add_parser("scan", help="run one cheap mount metadata scan")
    project_flags(queue_scan)
    queue_scan.add_argument("items", nargs="*", metavar="mount-rel")
    queue_scan.add_argument("--settle", type=float, default=10.0, help="seconds a changed file must remain unchanged before work starts")
    queue_scan.add_argument("--force", action="store_true", help="queue selected sources even when metadata is unchanged")
    queue_work = queue_sub.add_parser("work", help="process queued work, fast deletes before slow batches")
    project_flags(queue_work)
    queue_work.add_argument("--once", action="store_true", help="process at most one queue batch and exit")
    queue_work.add_argument("--continue", dest="continue_run", action="store_true",
                            help="resume the previous run's kept candidate worktree instead of discarding it")
    queue_status = queue_sub.add_parser("status", help="list pending, active, and failed work"); project_flags(queue_status)
    queue_retry = queue_sub.add_parser("retry", help="requeue failed work"); project_flags(queue_retry)
    queue.set_defaults(fn=cmd_queue)
    build = sub.add_parser("build", aliases=("wiki",), help="run wiki, link, or all local build phases"); pipeline_flags(build)
    build.add_argument("items", nargs="*", metavar="[wiki|link|all] [raw-rel ...]", help="phase followed by raw-relative paths; omit phase for all")
    build.add_argument("--from-file", help="text file with one raw-relative path per line")
    build.add_argument("--force", action="store_true", help="regenerate even when the raw source is unchanged")
    build.set_defaults(fn=cmd_build)
    publish = sub.add_parser("publish", help="publish the current wiki tree only"); project_flags(publish)
    publish.add_argument(
        "--allow-unlinked",
        action="store_true",
        help="publish built wiki pages even when linking is pending or failed; keep them pending for a later sync",
    )
    publish.set_defaults(fn=cmd_publish)
    pull = sub.add_parser("pull", help="capture GROWI edits into the local human overlay"); project_flags(pull)
    pull.add_argument("--inventory", action="store_true", help="force a complete read-only inventory below the configured GROWI root")
    pull.set_defaults(fn=cmd_pull)
    human = sub.add_parser("human", help="inspect and resolve durable human overlays"); project_flags(human)
    human_sub = human.add_subparsers(dest="human_command", required=True)
    human_status = human_sub.add_parser("status", help="write and print the project-wide human-sync summary")
    project_flags(human_status)
    human_resolve = human_sub.add_parser("resolve", help="apply one revision-checked operator decision")
    project_flags(human_resolve)
    human_resolve.add_argument("edit_id")
    human_resolve.add_argument("--action", required=True, choices=("keep-human", "accept-source", "combine", "suppress", "delete", "retry-match"))
    human_resolve.add_argument("--revision", required=True, help="last inspected GROWI revision")
    human_resolve.add_argument("--document", help="expected raw document identity")
    human_resolve.add_argument("--text-file", help="UTF-8 combined body for --action combine")
    human_recover = human_sub.add_parser("recover-legacy", help="recover a uniquely verified pure ancestor from project Git")
    project_flags(human_recover)
    human_recover.add_argument("document", help="raw-relative document path")
    human_live = human_sub.add_parser("live-plan", help="create a local-only redacted plan for a disposable live verification subtree")
    project_flags(human_live)
    human_live.add_argument("--path", required=True, help="confirmed disposable path below the configured project boundary")
    human.set_defaults(fn=cmd_human)
    index = sub.add_parser("index", help="publish per-document + root index pages for growi-search"); project_flags(index)
    index.add_argument("items", nargs="*", metavar="raw-rel", help="raw-relative paths; omit for every linked document")
    index.add_argument("--no-publish", action="store_true", help="only write metadata/index/, do not touch GROWI")
    index.add_argument("--delete", action="store_true", help="remove index pages from GROWI")
    index.set_defaults(fn=cmd_index)
    reset = sub.add_parser("reset", help="trash publisher-owned GROWI pages and reset publish state"); project_flags(reset); reset.set_defaults(fn=cmd_reset)

    link = sub.add_parser("link", help="cross-document linker")
    project_flags(link)
    link.add_argument("--linker", choices=("legacy", "neo"), help="mode for relink (default WIKI_LINKER_MODE)")
    link.add_argument("--force", action="store_true", help="relink all wiki documents, including completed ones")
    link_sub = link.add_subparsers(dest="link_command")
    link_sub.add_parser("status", help="catalog mode and counts")
    relink = link_sub.add_parser("relink", help="(re)link one document; free when nothing changed")
    relink.add_argument("document", help="wiki folder (team/docs/Input1.docx) or raw rel path")
    rebuild = link_sub.add_parser("rebuild", help="drop the catalog and relink everything (re-pays edge calls)")
    rebuild.add_argument("--mode", choices=("legacy", "neo"), required=True)
    rebuild.add_argument("--no-edges", action="store_true", help="strip footers/inline links, keep metadata")
    link.set_defaults(fn=cmd_link)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    from publisher.progress import ShortNameFormatter, TqdmStreamHandler

    handler = TqdmStreamHandler(sys.stderr)
    handler.setFormatter(ShortNameFormatter(LOG_FORMAT))
    logging.basicConfig(
        level="DEBUG" if getattr(args, "verbose", False) else os.environ.get("LOG_LEVEL", "INFO"),
        handlers=[handler],
    )
    # -v means "show our debug", not third-party HTTP/client chatter.
    # Keep client warnings out of the progress display as well; real errors
    # from these clients remain visible.
    for noisy in (
        "httpx",
        "httpx2",
        "httpcore",
        "h11",
        "urllib3",
        "openai",
        "langchain",
        "langchain_openai",
        "langchain-openai",
        "anthropic",
    ):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
