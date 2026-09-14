"""One entry point for the no-Git publisher. `python main.py -h`.

check                       ping chat/embed/parser/GROWI endpoints
sync                        one pass: mount/ -> raw/ -> wiki/ -> links -> GROWI
watch [--interval S]        `sync` forever
wiki <mount-rel>... [--force] | --from-file F    same pass, restricted to those files
publish                     GROWI sweep only (republish folders whose content hash changed)
link status | relink <doc> | rebuild --mode legacy|neo [--no-edges]

Every command reads .env (graph.config.Settings) and works on WIKI_DATA_ROOT (default ./data).
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

from graph.config import Settings
from graph.workspace.project import Project


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    # downstream .env names the chat endpoint WIKI_CHAT_*; upstream reads OPENAI_*/WIKI_MODEL
    overrides = {
        "chat_base_url": os.environ.get("WIKI_CHAT_BASE_URL", ""),
        "chat_api_key": os.environ.get("WIKI_CHAT_API_KEY", ""),
        "chat_model": os.environ.get("WIKI_CHAT_MODEL", ""),
    }
    for key, value in overrides.items():
        if value:
            setattr(settings, key, value)
    if not settings.data_root:
        settings.data_root = "data"
    if getattr(args, "data_root", None):
        settings.data_root = args.data_root
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
    print(f"[{event.get('stage', 'work')}] {json.dumps(event, ensure_ascii=False, default=str)[:300]}", flush=True)


def _report(result: dict[str, Any]) -> int:
    for row in result["done"]:
        print(json.dumps(row, ensure_ascii=False))
    for failure in result["failures"]:
        logging.error("%s", failure)
    return 1 if result["failures"] else 0


def cmd_check(args: argparse.Namespace) -> int:
    import requests

    settings = _settings(args)
    growi = os.environ.get("GROWI_URL", "").rstrip("/")
    targets = {
        "chat": f"{settings.chat_base_url.rstrip('/')}/models",
        "embed": f"{settings.embed_base_url.rstrip('/')}/models",
        "parser": f"{settings.parser_base_url.rstrip('/')}/queue" if settings.parser_base_url else "",
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
    print(f"data    {Path(settings.data_root).resolve()}  ingest={settings.ingest_mode} linker={'off' if not settings.wiki_linker_enabled else settings.wiki_linker_mode}")
    return 1 if bad else 0


def cmd_sync(args: argparse.Namespace) -> int:
    from publisher.pipeline import sync_once

    return _report(sync_once(_settings(args), on_progress=_progress if args.verbose else None))


def cmd_watch(args: argparse.Namespace) -> int:
    from publisher.pipeline import sync_once

    settings = _settings(args)
    try:
        while True:
            result = sync_once(settings, on_progress=_progress if args.verbose else None)
            if result["failures"]:
                logging.error("sync run had %d failure(s)", len(result["failures"]))
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        return 0


def cmd_wiki(args: argparse.Namespace) -> int:
    from publisher.pipeline import sync_once

    rels = list(args.rel)
    if args.from_file:
        rels += [line.strip() for line in Path(args.from_file).read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    if not rels:
        raise SystemExit("give mount-relative paths or --from-file")
    return _report(sync_once(_settings(args), only=rels, force=args.force, on_progress=_progress if args.verbose else None))


def cmd_publish(args: argparse.Namespace) -> int:
    from publisher.pipeline import sync_once

    return _report(sync_once(_settings(args), only=[]))


def cmd_link(args: argparse.Namespace) -> int:
    from graph.linker import __main__ as linker_cli

    settings = _settings(args)
    project = Project(Path(settings.data_root)).ensure()
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
    parser.add_argument("--data-root", help="override WIKI_DATA_ROOT (default ./data)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print pipeline progress events")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="ping chat/embed/parser/GROWI endpoints").set_defaults(fn=cmd_check)

    def pipeline_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--mode", choices=("wiki", "chunks"), help="ingest mode (default WIKI_INGEST_MODE)")
        p.add_argument("--linker", choices=("legacy", "neo", "off"), help="linker mode (default WIKI_LINKER_MODE)")
        p.add_argument("--timeout", type=int, help="per-call model timeout in seconds (default WIKI_REQUEST_TIMEOUT)")

    sync = sub.add_parser("sync", help="one reconciliation pass over mount/"); pipeline_flags(sync); sync.set_defaults(fn=cmd_sync)
    watch = sub.add_parser("watch", help="sync forever"); pipeline_flags(watch)
    watch.add_argument("--interval", type=float, default=float(os.environ.get("PUBLISHER_INTERVAL_SECONDS", "60")))
    watch.set_defaults(fn=cmd_watch)
    wiki = sub.add_parser("wiki", help="generate only the given mount files (still ledger-tracked)"); pipeline_flags(wiki)
    wiki.add_argument("rel", nargs="*", help="mount-relative paths, e.g. team/docs/Input1.docx")
    wiki.add_argument("--from-file", help="text file with one mount-relative path per line")
    wiki.add_argument("--force", action="store_true", help="regenerate even when the source is unchanged")
    wiki.set_defaults(fn=cmd_wiki)
    sub.add_parser("publish", help="GROWI sweep only, no generation").set_defaults(fn=cmd_publish)

    link = sub.add_parser("link", help="cross-document linker")
    link.add_argument("--linker", choices=("legacy", "neo"), help="mode for relink (default WIKI_LINKER_MODE)")
    link_sub = link.add_subparsers(dest="link_command", required=True)
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
