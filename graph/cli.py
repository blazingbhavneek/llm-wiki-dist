"""Command line interface: `python -m graph.cli ...`. Thin — calls Graph."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .graph import Graph
from .models import Settings


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _graph(args: argparse.Namespace) -> Graph:
    settings = Settings.from_env()
    if args.database:
        settings.database_path = args.database
    return Graph(settings)


def cmd_init(args: argparse.Namespace) -> None:
    graph = _graph(args)
    graph.close()
    print(f"database ready: {graph.settings.database_path}")


def cmd_add(args: argparse.Namespace) -> None:
    graph = _graph(args)
    try:
        nodes = graph.ingest_md_output(args.md_output_dir)
        print(f"ingested {len(nodes)} node(s) from {args.md_output_dir}")
        for node in nodes[:10]:
            print(f"- {node.id}: {node.title or node.summary[:80]}")
    finally:
        graph.close()


def cmd_query(args: argparse.Namespace) -> None:
    graph = _graph(args)
    try:
        _print(graph.query(args.query_type, args.value).model_dump())
    finally:
        graph.close()


def cmd_ask(args: argparse.Namespace) -> None:
    graph = _graph(args)
    try:
        _print(graph.ask(args.question, persist=not args.no_persist).model_dump())
    finally:
        graph.close()


def cmd_recon(args: argparse.Namespace) -> None:
    graph = _graph(args)
    try:
        _print(graph.recon(args.source_file))
    finally:
        graph.close()


def cmd_cascade(args: argparse.Namespace) -> None:
    graph = _graph(args)
    try:
        _print({"actions": graph.cascading_update(args.md_output_dir)})
    finally:
        graph.close()


def cmd_get(args: argparse.Namespace) -> None:
    graph = _graph(args)
    try:
        nodes, edges = graph.get()
        _print(
            {
                "nodes": [n.model_dump() for n in nodes],
                "edges": [e.model_dump() for e in edges],
            }
        )
    finally:
        graph.close()


def cmd_health(args: argparse.Namespace) -> None:
    graph = _graph(args)
    try:
        _print(graph.health(args.node_id).model_dump())
    finally:
        graph.close()


def cmd_delete(args: argparse.Namespace) -> None:
    graph = _graph(args)
    try:
        graph.delete(args.node_id)
        print(f"deleted {args.node_id}")
    finally:
        graph.close()


def cmd_update(args: argparse.Namespace) -> None:
    graph = _graph(args)
    try:
        body = Path(args.markdown_path).read_text(encoding="utf-8")
        node = graph.update_node(args.node_id, body)
        print(f"updated {node.id}")
    finally:
        graph.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="graph", description="LLM wiki graph")
    parser.add_argument("--database", help="SQLite path (overrides WIKI_DB)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database").set_defaults(func=cmd_init)

    add = sub.add_parser("add", help="ingest an md.py output directory")
    add.add_argument("md_output_dir")
    add.set_defaults(func=cmd_add)

    query = sub.add_parser("query", help="query by keyword, vector, or id")
    query.add_argument("query_type", choices=["keyword", "vector", "id"])
    query.add_argument("value")
    query.set_defaults(func=cmd_query)

    ask = sub.add_parser("ask", help="answer a question via the reasoning agent")
    ask.add_argument("question")
    ask.add_argument(
        "--no-persist", action="store_true", help="do not save the answer node"
    )
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
