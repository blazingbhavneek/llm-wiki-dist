"""One entry point for every pipeline variant. `python main.py -h`.

Servers     serve | mcp | check
Factory     convert | wiki <rel>... [--all|--from-file] | sync | index
Linker      link status | link relink <doc> | link rebuild --mode legacy|neo [--no-edges]

Every command reads .env (graph.config.Settings) and works on WIKI_DATA_ROOT.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from graph.config import Settings
from graph.workspace.project import Project


# region helpers

def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if getattr(args, "data_root", None):
        settings.data_root = args.data_root
    if getattr(args, "mode", None) and args.command == "wiki":
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


def _project(settings: Settings) -> Project:
    if not settings.data_root:
        raise SystemExit("WIKI_DATA_ROOT (or --data-root) is required")
    return Project(Path(settings.data_root)).ensure()


def _progress(event: dict[str, Any]) -> None:
    print(f"[{event.get('stage', 'work')}] {json.dumps(event, ensure_ascii=False, default=str)[:300]}", flush=True)


def _llm(settings: Settings, project: Project, rel: str):
    from graph.formats import kind_of
    from graph.wiki.model import ChatModelPort
    from graph.workspace.writer import wiki_config

    return ChatModelPort(wiki_config(settings, run_dir=project.state_dir(rel), source_kind=kind_of(rel)))


def _growi_publisher(settings: Settings, project: Project, name: str):
    """Registered GROWI connection -> GrowiPublisher, or a no-op publisher when none is wanted."""
    if name == "none":
        class NoPublish:
            def publish_document(self, project: Any, rel: str) -> list[Any]:
                return []

            def delete_document(self, project: Any, rel: str) -> int:
                return 0

        return NoPublish()
    from graph.growi import GrowiClient, GrowiPublisher
    from graph.registry import ConnectionRegistry

    registry = ConnectionRegistry(Path(os.environ.get("WIKI_ENGINE_DB", str(project.engine_db))))
    connection = registry.get(name) if name else None
    if connection is None and not name:
        rows = registry.list()
        connection = rows[0] if len(rows) == 1 else None
    if connection is None:
        raise SystemExit(f"no GROWI connection {name!r}; register one in the admin UI or use --growi none")
    return GrowiPublisher(GrowiClient(connection.url, connection.api_token), connection)

# endregion helpers


# region commands

def cmd_check(args: argparse.Namespace) -> int:
    """Ping every external endpoint the pipeline depends on."""
    import requests

    settings = _settings(args)
    targets = {
        "chat": f"{settings.chat_base_url.rstrip('/')}/models",
        "embed": f"{settings.embed_base_url.rstrip('/')}/models",
        "rerank": f"{settings.rerank_base_url.rstrip('/')}/models",
        "parser": f"{settings.parser_base_url.rstrip('/')}/queue" if settings.parser_base_url else "",
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
    print(f"data    {settings.data_root or '(unset)'}  ingest={settings.ingest_mode} linker={'off' if not settings.wiki_linker_enabled else settings.wiki_linker_mode}")
    return 1 if bad else 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    sys.argv = ["mcp_server.py", "--host", args.host, "--port", str(args.port)]
    import mcp_server

    mcp_server.main()
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    """mount/ -> raw/ through the parser service (Markdown copied as-is)."""
    from graph.workspace.convert import convert_mount

    settings = _settings(args)
    project = _project(settings)
    report = convert_mount(project, parser_base_url=settings.parser_base_url, settings=settings, on_progress=_progress)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failed"] else 0


def cmd_wiki(args: argparse.Namespace) -> int:
    """raw file(s) -> wiki pages -> links (-> republish nothing; use `sync` for GROWI)."""
    from graph.workspace.writer import up_to_date, write_index, write_wiki

    settings = _settings(args)
    project = _project(settings)
    rels = [rel.strip().lstrip("/") for rel in args.rel]
    if args.from_file:
        rels += [line.strip().lstrip("/") for line in Path(args.from_file).read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    if args.all:
        rels += project.raw_files()
    if not rels:
        raise SystemExit("give raw-relative paths, --from-file, or --all")
    failed = 0
    for index, rel in enumerate(dict.fromkeys(rels), start=1):
        if not project.raw_file(rel).exists():
            print(f"[wiki] {index}/{len(rels)} missing raw file: {rel}", flush=True); failed += 1
            continue
        if not args.force and up_to_date(project, rel):
            print(f"[wiki] {index}/{len(rels)} up to date: {rel}", flush=True)
            continue
        print(f"[wiki] {index}/{len(rels)} {rel} mode={settings.ingest_mode} linker={'off' if not settings.wiki_linker_enabled else settings.wiki_linker_mode}", flush=True)
        try:
            result = write_wiki(project, rel, mode=settings.ingest_mode, settings=settings, llm=_llm(settings, project, rel), embedder=None, on_progress=_progress if args.verbose else None)
            print(f"[wiki] done -> {result.target}; touched={result.touched}", flush=True)
        except Exception as exc:
            failed += 1
            print(f"[wiki] FAILED {rel}: {type(exc).__name__}: {exc}", flush=True)
            if not args.keep_going:
                raise
    write_index(project)
    return 1 if failed else 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Full pass: convert mount, regenerate changed raw files, link, publish to GROWI."""
    from graph.clients.chat import make_llm
    from graph.sync import sync_raw

    settings = _settings(args)
    project = _project(settings)
    publisher = _growi_publisher(settings, project, args.growi)
    llm = make_llm(settings.chat_model, settings.chat_base_url, settings.chat_api_key, settings.chat_temperature, settings.wiki_request_timeout)
    result = sync_raw(project, publisher, mode=settings.ingest_mode, settings=settings, llm=llm, embedder=None, on_progress=_progress if args.verbose else None)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    from graph.workspace.writer import write_index

    write_index(_project(_settings(args)))
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    """Thin wrapper over `python -m graph.linker`, sharing the --linker/--data-root flags."""
    from graph.linker import __main__ as linker_cli

    settings = _settings(args)
    project = _project(settings)
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
    return 0

# endregion commands


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python main.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-root", help="override WIKI_DATA_ROOT")
    parser.add_argument("-v", "--verbose", action="store_true", help="print pipeline progress events")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="ping chat/embed/rerank/parser endpoints").set_defaults(fn=cmd_check)
    serve = sub.add_parser("serve", help="FastAPI engine + UI (uvicorn app:app)")
    serve.add_argument("--host", default="0.0.0.0"); serve.add_argument("--port", type=int, default=8000); serve.add_argument("--reload", action="store_true")
    serve.set_defaults(fn=cmd_serve)
    mcp = sub.add_parser("mcp", help="MCP server (mcp_server.py)")
    mcp.add_argument("--host", default="0.0.0.0"); mcp.add_argument("--port", type=int, default=8001)
    mcp.set_defaults(fn=cmd_mcp)

    sub.add_parser("convert", help="mount/ -> raw/ via the parser service").set_defaults(fn=cmd_convert)
    wiki = sub.add_parser("wiki", help="raw file(s) -> wiki pages + links (no GROWI)")
    wiki.add_argument("rel", nargs="*", help="raw-relative paths, e.g. test/docx/Input1_docx.md")
    wiki.add_argument("--from-file", help="text file with one raw-relative path per line")
    wiki.add_argument("--all", action="store_true", help="every file under raw/")
    wiki.add_argument("--force", action="store_true", help="regenerate even when the wiki is up to date")
    wiki.add_argument("--keep-going", action="store_true", help="continue after a failed document")
    wiki.add_argument("--mode", choices=("wiki", "chunks"), help="ingest mode (default WIKI_INGEST_MODE)")
    wiki.add_argument("--linker", choices=("legacy", "neo", "off"), help="linker mode (default WIKI_LINKER_MODE)")
    wiki.add_argument("--timeout", type=int, help="per-call model timeout in seconds (default WIKI_REQUEST_TIMEOUT)")
    wiki.set_defaults(fn=cmd_wiki)
    sync = sub.add_parser("sync", help="convert + regenerate changed raw files + link + publish to GROWI")
    sync.add_argument("--growi", default=os.environ.get("WIKI_GROWI_NAME", ""), help="registered connection name, or 'none' to skip publishing")
    sync.add_argument("--linker", choices=("legacy", "neo", "off"))
    sync.add_argument("--timeout", type=int)
    sync.set_defaults(fn=cmd_sync)
    sub.add_parser("index", help="rewrite wiki/index.md").set_defaults(fn=cmd_index)

    link = sub.add_parser("link", help="cross-document linker")
    link.add_argument("--linker", choices=("legacy", "neo"), help="mode for relink (default WIKI_LINKER_MODE)")
    link_sub = link.add_subparsers(dest="link_command", required=True)
    link_sub.add_parser("status", help="catalog mode and counts")
    relink = link_sub.add_parser("relink", help="(re)link one document; free when nothing changed")
    relink.add_argument("document", help="wiki folder (test/docx/Input1.docx) or raw rel path")
    rebuild = link_sub.add_parser("rebuild", help="drop the catalog and relink everything (re-pays edge calls)")
    rebuild.add_argument("--mode", choices=("legacy", "neo"), required=True)
    rebuild.add_argument("--no-edges", action="store_true", help="strip footers/inline links, keep metadata")
    link.set_defaults(fn=cmd_link)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
