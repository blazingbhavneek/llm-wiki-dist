from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import main
from graph.common.async_tools import run_async_blocking
from graph.config import Settings, resolve_project_path
from graph.workspace.project import Project, open_project
from graph.workspace.writer import wiki_config
from graph.wiki.model import ChatModelPort

from . import link_document
from .catalog import Catalog


def _documents(project: Project) -> list[str]:
    return sorted(
        path.parent.parent.relative_to(project.wiki).as_posix()
        for path in project.wiki.rglob("_planning/metadata.json")
    )


def _raw_rel(project: Project, document: str) -> str:
    marker = project.wiki / document / "_planning" / "source.json"
    if marker.exists():
        return str(__import__("json").loads(marker.read_text(encoding="utf-8")).get("raw", document))
    chunks = project.wiki / document / "_planning" / "chunks.json"
    if chunks.exists():
        cached = str(__import__("json").loads(chunks.read_text(encoding="utf-8")).get("raw_rel") or "")
        if cached:
            return cached
    # Older output without a stamp: invert the wiki folder naming rule.
    from pathlib import PurePosixPath

    from graph.workspace.project import raw_name_for

    path = PurePosixPath(document)
    return (path.parent / raw_name_for(path.name)).as_posix()


def _model_and_embedder(settings: Settings, project: Project, document: str):
    cfg = wiki_config(settings, run_dir=project.metadata / "state" / document)
    model = ChatModelPort(cfg)
    try:
        from graph.clients.embeddings import Embedder
        embedder = Embedder(settings)
    except Exception:
        embedder = None
    return model, embedder


def rebuild(project: Project, settings: Settings, mode: str, no_edges: bool = False) -> None:
    if project.linker_database.exists():
        project.linker_database.unlink()
    documents = _documents(project)
    for document in documents:
        (project.wiki / document / "_planning" / "links.json").unlink(missing_ok=True)
    settings.wiki_linker_mode = mode
    for document in documents:
        planning = project.wiki / document / "_planning"
        if no_edges:
            run_async_blocking(link_document(project, _raw_rel(project, document), model=None, embedder=None, settings=settings))
            continue
        raw_rel = _raw_rel(project, document)
        model, embedder = _model_and_embedder(settings, project, document)
        run_async_blocking(link_document(project, raw_rel, model=model, embedder=embedder, settings=settings))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m graph.linker")
    parser.add_argument("--project", required=True)
    parser.add_argument("--data-root", help="override selected INI data_root")
    sub = parser.add_subparsers(dest="command", required=True)
    rebuild_parser = sub.add_parser("rebuild")
    rebuild_parser.add_argument("--mode", choices=("legacy", "neo"), required=True)
    rebuild_parser.add_argument("--no-edges", action="store_true")
    relink_parser = sub.add_parser("relink")
    relink_parser.add_argument("document")
    sub.add_parser("status")
    args = parser.parse_args()
    settings = Settings.from_env(args.project)
    if args.data_root:
        settings.data_root = str(resolve_project_path(args.data_root).resolve())
    project = open_project(settings)
    if args.command == "status":
        if not project.linker_database.exists():
            print({"mode": None, "documents": 0, "chunks": 0, "edges": 0})
            return
        with sqlite3.connect(project.linker_database) as conn:
            mode = conn.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("documents", "chunks", "edges")
            }
        print({"mode": mode[0] if mode else None, **counts})
        return
    if args.command == "rebuild":
        rebuild(project, settings, args.mode, args.no_edges)
        return
    rel = args.document.strip("/")
    if rel in _documents(project):
        rel = _raw_rel(project, rel)
    elif not project.raw_file(rel).exists():
        raise SystemExit(f"unknown document: {args.document} (expected a wiki folder or raw rel path)")
    model, embedder = _model_and_embedder(settings, project, Path(rel).parent.as_posix())
    settings.wiki_linker_mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
    run_async_blocking(link_document(project, rel, model=model, embedder=embedder, settings=settings))


if __name__ == "__main__":
    main()
