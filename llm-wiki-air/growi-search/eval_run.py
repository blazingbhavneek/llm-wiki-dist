"""Run a sequential teacher/candidate search evaluation over JSONL questions."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def _split_set(values: list[str]) -> tuple[dict[str, str], dict[str, Any], dict[str, str]]:
    """Split --set KEY=VALUE into process settings and per-request overrides."""
    from researcher import _OVERRIDE_KEYS

    env_values: dict[str, str] = {}
    field_values: dict[str, Any] = {}
    request_values: dict[str, str] = {}
    from config import Settings

    fields = set(Settings.model_fields)
    for raw in values:
        key, sep, value = raw.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            raise ValueError(f"--set must be KEY=VALUE: {raw!r}")
        if key in _OVERRIDE_KEYS:
            request_values[key] = value
        elif key in fields:
            field_values[key] = value
        elif key.startswith(("WIKI_", "GROWI_")):
            if key != "WIKI_JEV_MODE" and key != "WIKI_PROJECT":
                normalized = (key.lower() if key.startswith("GROWI_")
                             else key.removeprefix("WIKI_").lower())
                aliases = {"search_llm_max_concurrency": "llm_max_concurrency"}
                if normalized not in fields and normalized not in aliases and not key.startswith("WIKI_JEV_"):
                    raise ValueError(f"unknown setting for --set: {key}")
            env_values[key] = value
        else:
            raise ValueError(f"unknown setting for --set: {key}")
    return env_values, field_values, request_values


def _route_document(event: dict[str, Any], index_page_name: str) -> str:
    value = event.get("document") or event.get("document_path")
    if not value:
        value = event.get("node")
    if isinstance(value, dict):
        value = value.get("path") or value.get("id") or ""
    path = str(value or "").strip()
    if not path or re.fullmatch(r"[0-9a-fA-F]{24}", path.strip("/")):
        return ""
    path = "/" + path.strip("/")
    suffix = "/" + index_page_name
    if path.endswith(suffix):
        path = path[:-len(suffix)] or "/"
    return path.rstrip("/") or "/"


def make_record(question: dict[str, Any], mode: str, answer: Any,
                events: list[dict[str, Any]], index_page_name: str = "00-目次") -> dict[str, Any]:
    seeds: dict[str, dict[str, Any]] = {}
    sweep_documents: set[str] = set()
    routed: set[str] = set()
    rescued: set[str] = set()
    timings: dict[str, Any] = {}
    counts: Counter[str] = Counter()
    for event in events:
        kind = str(event.get("type") or "unknown")
        counts[kind] += 1
        if kind == "timings":
            timings = {key: event[key] for key in ("stage_ms", "counts", "total_ms") if key in event}
        elif kind == "jev_gate":
            document = str(event.get("document") or "").rstrip("/")
            if document:
                sweep_documents.add(document)
            if event.get("stage") == "full" and event.get("status") == "confirmed":
                node = event.get("node") or {}
                page_id = node.get("id") if isinstance(node, dict) else ""
                if page_id:
                    seeds[str(page_id)] = {
                        "id": str(page_id),
                        "p": float(event.get("probability") or 0.0),
                        "document": document,
                    }
        elif kind == "route" and event.get("kept") is True:
            document = _route_document(event, index_page_name)
            if document:
                routed.add(document)
                if event.get("rescued"):
                    rescued.add(document)

    if mode == "exhaustive":
        routed.update(sweep_documents)
    return {
        "id": str(question["id"]),
        "question": question["question"],
        "kind": question.get("kind", "other"),
        "mode": mode,
        "answer": str(getattr(answer, "answer", "") or ""),
        "cited_ids": list(getattr(answer, "cited_node_ids", []) or []),
        "seeds": sorted(seeds.values(), key=lambda item: item["id"]),
        "routed_documents": sorted(routed),
        "rescued_documents": sorted(rescued),
        "timings": timings,
        "events_count": dict(sorted(counts.items())),
    }


def _questions(path: Path) -> list[dict[str, Any]]:
    rows = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(item, dict) or not isinstance(item.get("question"), str):
            raise ValueError(f"{path}:{number}: each row needs a question string")
        item_id = str(item.get("id") or "")
        if not _ID_RE.fullmatch(item_id):
            raise ValueError(f"{path}:{number}: id must contain only letters, digits, dot, underscore or dash")
        if item_id in seen:
            raise ValueError(f"{path}:{number}: duplicate id {item_id!r}")
        seen.add(item_id)
        kind = item.get("kind", "other")
        if kind not in {"fact", "list", "procedure", "other"}:
            raise ValueError(f"{path}:{number}: invalid kind {kind!r}")
        rows.append({"id": item_id, "question": item["question"], "kind": kind})
    return rows


async def _run(questions: list[dict[str, Any]], output: Path, mode: str,
               env_values: dict[str, str], field_values: dict[str, Any],
               request_values: dict[str, str]) -> int:
    from config import Settings
    from gateway import Embedder, Reranker
    from growi_client import GrowiSearchClient
    from researcher import Researcher, _sanitize_overrides

    os.environ.update(env_values)
    os.environ["WIKI_JEV_MODE"] = mode
    settings = Settings.from_env()
    if field_values:
        settings = Settings.model_validate({**settings.model_dump(), **field_values})
    from jev import JevConfig
    JevConfig.from_env()
    request_overrides = _sanitize_overrides(request_values, settings)
    settings.validate_strict()
    client = GrowiSearchClient(
        settings.growi_url, settings.growi_token,
        attachment_token=settings.growi_attachment_token,
        root_path=settings.growi_root_path, timeout=settings.growi_timeout,
        max_concurrency=settings.growi_concurrency,
    )
    researcher = None
    errors = 0
    try:
        researcher = Researcher(client, settings, Reranker.build(settings), Embedder.build(settings))
        if researcher.mirror is not None:
            researcher.mirror.start()
        output.mkdir(parents=True, exist_ok=True)
        for item in questions:
            events: list[dict[str, Any]] = []
            answer = None
            try:
                answer = await researcher.ask(item["question"], events.append,
                                              request_overrides or None)
            except Exception as exc:  # keep a partial run inspectable and continue
                errors += 1
                events.append({"type": "eval_error", "error": f"{type(exc).__name__}: {exc}"})
                print(f"[{item['id']}] evaluation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            record = make_record(item, mode, answer, events, settings.index_page_name)
            if errors and events and events[-1].get("type") == "eval_error":
                record["error"] = events[-1]["error"]
            (output / f"{item['id']}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        if researcher is not None and researcher.mirror is not None:
            researcher.mirror.stop()
        client.close()
        try:
            from jev import reset_engine
            reset_engine()
        except Exception:
            pass
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=("exhaustive", "cascade"), default="exhaustive")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args(argv)
    try:
        questions = _questions(args.questions)
        env_values, field_values, request_values = _split_set(args.set)
        errors = asyncio.run(_run(questions, args.out, args.mode, env_values,
                                  field_values, request_values))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
