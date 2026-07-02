from __future__ import annotations

import argparse
import json
import logging
from contextlib import contextmanager
from pathlib import Path

from .graph import (
    GraphDbSession,
    GraphReadSession,
    GraphWriteSession,
    SharedGraphRuntime,
    bootstrap_database,
)
from .models import Settings


# print a random json object
def _print(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if args.database:
        settings.database_path = args.database
    return settings


@contextmanager
def _read_session(args: argparse.Namespace):
    settings = _settings(args)
    runtime = SharedGraphRuntime(settings)
    db = GraphDbSession(settings, readonly=True)
    try:
        yield GraphReadSession(runtime, db)
    finally:
        db.close()
        runtime.close()


@contextmanager
def _write_session(args: argparse.Namespace):
    # No enrichment queue in the CLI: cascades and reclustering run inline.
    settings = _settings(args)
    runtime = SharedGraphRuntime(settings)
    db = GraphDbSession(settings, readonly=False)
    try:
        yield GraphWriteSession(runtime, db)
    finally:
        db.close()
        runtime.close()


def cmd_init(args: argparse.Namespace) -> None:
    settings = _settings(args)
    bootstrap_database(settings)
    print(f"database ready: {settings.database_path}")


def cmd_add(args: argparse.Namespace) -> None:
    with _write_session(args) as session:
        nodes = session.ingest_md_output(args.md_output_dir)
        print(f"ingested {len(nodes)} node(s) from {args.md_output_dir}")
        for node in nodes[:10]:
            print(f"- {node.id}: {node.title or node.summary[:80]}")


def cmd_query(args: argparse.Namespace) -> None:
    with _read_session(args) as session:
        _print(session.query(args.query_type, args.value).model_dump())


def cmd_ask(args: argparse.Namespace) -> None:
    with _read_session(args) as session:
        _print(session.ask(args.question, persist=False).model_dump())


def cmd_recon(args: argparse.Namespace) -> None:
    with _write_session(args) as session:
        _print(session.recon(args.source_file))


def cmd_cascade(args: argparse.Namespace) -> None:
    with _write_session(args) as session:
        _print({"actions": session.cascading_update(args.md_output_dir)})


def cmd_get(args: argparse.Namespace) -> None:
    with _read_session(args) as session:
        nodes, edges = session.get()
        _print(
            {
                "nodes": [n.model_dump() for n in nodes],
                "edges": [e.model_dump() for e in edges],
            }
        )


def cmd_health(args: argparse.Namespace) -> None:
    with _read_session(args) as session:
        _print(session.health(args.node_id).model_dump())


def cmd_delete(args: argparse.Namespace) -> None:
    with _write_session(args) as session:
        session.delete_node(args.node_id)
        print(f"deleted {args.node_id}")


def cmd_update(args: argparse.Namespace) -> None:
    with _write_session(args) as session:
        body = Path(args.markdown_path).read_text(encoding="utf-8")
        node = session.update_node(args.node_id, body)
        print(f"updated {node.id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="graph", description="LLM wiki graph")
    parser.add_argument("--database", help="SQLite path (overrides WIKI_DB)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create + bootstrap the database").set_defaults(
        func=cmd_init
    )

    add = sub.add_parser("add", help="ingest an md.py output directory")
    add.add_argument("md_output_dir")
    add.set_defaults(func=cmd_add)

    query = sub.add_parser("query", help="query by keyword, vector, or id")
    query.add_argument("query_type", choices=["keyword", "vector", "id"])
    query.add_argument("value")
    query.set_defaults(func=cmd_query)

    ask = sub.add_parser("ask", help="answer a question via the reasoning agent")
    ask.add_argument("question")
    ask.set_defaults(func=cmd_ask)

    recon = sub.add_parser("recon", help="check if a source doc is new/changed")
    recon.add_argument("source_file")
    recon.set_defaults(func=cmd_recon)

    cascade = sub.add_parser("cascade", help="apply a revised md.py output directory")
    cascade.add_argument("md_output_dir")
    cascade.set_defaults(func=cmd_cascade)

    sub.add_parser("get", help="dump all nodes and edges").set_defaults(func=cmd_get)

    health = sub.add_parser("health", help="graph health metrics")
    health.add_argument("node_id", nargs="?")
    health.set_defaults(func=cmd_health)

    delete = sub.add_parser("delete", help="delete one node")
    delete.add_argument("node_id")
    delete.set_defaults(func=cmd_delete)

    update = sub.add_parser("update", help="replace a node body from a markdown file")
    update.add_argument("node_id")
    update.add_argument("markdown_path")
    update.set_defaults(func=cmd_update)

    return parser


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="[graph] %(message)s")
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
