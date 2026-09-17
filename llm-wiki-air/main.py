"""One entry point for the no-Git publisher. `python main.py -h`.

check                       ping chat/embed/parser/GROWI endpoints
convert                     external mount -> raw Markdown only
build wiki [<raw-rel>...]   raw/ -> wiki pages only
build link [<raw-rel>...]   link pending wiki pages only
build all [<raw-rel>...]    wiki batch first, then link batch (bare build is an alias)
publish                     publish the current wiki/ tree to GROWI
sync [<mount-rel>...]       one pass: external mount -> raw/ -> wiki/ -> links -> GROWI
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

PROJECT_ROOT = Path(__file__).resolve().parent
config.PROJECT_ROOT = PROJECT_ROOT


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env(getattr(args, "project", ""))
    # downstream .env names the chat endpoint WIKI_CHAT_*; upstream reads OPENAI_*/WIKI_MODEL
    overrides = {
        "chat_base_url": os.environ.get("WIKI_CHAT_BASE_URL", ""),
        "chat_api_key": os.environ.get("WIKI_CHAT_API_KEY", ""),
        "chat_model": os.environ.get("WIKI_CHAT_MODEL", ""),
    }
    for key, value in overrides.items():
        if value:
            setattr(settings, key, value)
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
    print(f"[{stage}] {step}{progress} {details}".rstrip(), flush=True)


def _report(result: dict[str, Any]) -> int:
    for row in result["done"]:
        print(json.dumps(row, ensure_ascii=False))
    for failure in result["failures"]:
        logging.error("%s", failure)
    return 1 if result["failures"] else 0


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
    project = open_project(settings)
    print(f"data    {project.root.resolve()}  mount={project.mount} ingest={settings.ingest_mode} linker={'off' if not settings.wiki_linker_enabled else settings.wiki_linker_mode}")
    return 1 if bad else 0


def cmd_sync(args: argparse.Namespace) -> int:
    from publisher.pipeline import sync_once

    return _report(sync_once(_settings(args), only=args.items or None, force=args.force, on_progress=_progress if args.verbose else None))


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
        )
    except KeyboardInterrupt:
        return 0


def cmd_queue(args: argparse.Namespace) -> int:
    from publisher.queue import recover, retry_failed, scan, status, work_once, worker_lock

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
    with worker_lock(project):
        recover(project)
        while True:
            result = work_once(settings)
            if result is not None:
                print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
            if args.once:
                return 1 if result and result.get("failures") else 0
            if result is None:
                time.sleep(0.5)


def cmd_convert(args: argparse.Namespace) -> int:
    from graph.workspace.convert import convert_mount

    settings = _settings(args)
    result = convert_mount(
        open_project(settings),
        parser_base_url=settings.parser_base_url,
        settings=settings,
        on_progress=_progress if args.verbose else None,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["failed"] else 0


def cmd_build(args: argparse.Namespace) -> int:
    from publisher.pipeline import build_raw, build_wiki_only, link_raw

    items = list(args.items)
    phase = items.pop(0) if items and items[0] in {"wiki", "link", "all"} else "all"
    rels = items
    if args.from_file:
        rels += [line.strip() for line in Path(args.from_file).read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    runner = {"wiki": build_wiki_only, "link": link_raw, "all": build_raw}[phase]
    return _report(runner(_settings(args), only=rels or None, force=args.force, on_progress=_progress if args.verbose else None))


def cmd_publish(args: argparse.Namespace) -> int:
    from publisher.pipeline import publish_only

    return _report(publish_only(_settings(args)))


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

    check = sub.add_parser("check", help="ping chat/embed/parser/GROWI endpoints"); project_flags(check); check.set_defaults(fn=cmd_check)

    def pipeline_flags(p: argparse.ArgumentParser) -> None:
        project_flags(p)
        p.add_argument("--mode", choices=("wiki", "chunks"), help="ingest mode (default WIKI_INGEST_MODE)")
        p.add_argument("--linker", choices=("legacy", "neo", "off"), help="linker mode (default WIKI_LINKER_MODE)")
        p.add_argument("--timeout", type=int, help="per-call model timeout in seconds (default WIKI_REQUEST_TIMEOUT)")

    convert = sub.add_parser("convert", help="convert the configured mount to raw Markdown only"); project_flags(convert); convert.set_defaults(fn=cmd_convert)
    sync = sub.add_parser("sync", help="one reconciliation pass over the configured mount"); pipeline_flags(sync)
    sync.add_argument("items", nargs="*", metavar="mount-rel", help="mount-relative source paths; omit for the full project")
    sync.add_argument("--force", action="store_true", help="regenerate selected sources even when unchanged")
    sync.set_defaults(fn=cmd_sync)
    watch = sub.add_parser("watch", help="run the metadata scanner and persistent queue worker"); pipeline_flags(watch)
    watch.add_argument("items", nargs="*", metavar="mount-rel", help="mount-relative source paths; omit for the full project")
    watch.add_argument("--force", action="store_true", help="queue selected sources once at startup even when unchanged")
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
    queue_status = queue_sub.add_parser("status", help="list pending, active, and failed work"); project_flags(queue_status)
    queue_retry = queue_sub.add_parser("retry", help="requeue failed work"); project_flags(queue_retry)
    queue.set_defaults(fn=cmd_queue)
    build = sub.add_parser("build", aliases=("wiki",), help="run wiki, link, or all local build phases"); pipeline_flags(build)
    build.add_argument("items", nargs="*", metavar="[wiki|link|all] [raw-rel ...]", help="phase followed by raw-relative paths; omit phase for all")
    build.add_argument("--from-file", help="text file with one raw-relative path per line")
    build.add_argument("--force", action="store_true", help="regenerate even when the raw source is unchanged")
    build.set_defaults(fn=cmd_build)
    publish = sub.add_parser("publish", help="publish the current wiki tree only"); project_flags(publish); publish.set_defaults(fn=cmd_publish)
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
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args()
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
