#!/usr/bin/env python3
"""Ingest inputs/*.md into a standalone llm-wiki sqlite store.

Reuses the benchmark harness's llm-wiki ingestion path
(benchmark/clients/llm_wiki.py) directly -- no graphrag/vanilla clients, no
questions, no report. Just a resumable sqlite datastore built from your own
markdown files for ad-hoc testing later.

Usage:
    python ingest_inputs.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import benchmark as legacy
from benchmark.clients import llm_wiki
from benchmark.datasets.common import prepare_dataset
from benchmark.runner import build_args

ROOT = Path(__file__).resolve().parent
INPUTS_DIR = ROOT / "inputs"
DATASTORE = ROOT / "benchmark-results" / "datastores" / "inputs-test"
NAME = "inputs-test"
INGESTION_CONCURRENCY = 10
FINAL_DB = ROOT / "moove_wiki.sqlite"


def load(args: SimpleNamespace) -> legacy.DatasetBundle:
    documents = [
        # Strip stray NUL bytes (PDF-to-markdown conversion artifacts) -- they
        # survive into FTS5 MATCH queries as an unquotable term and crash
        # SQLite with "unterminated string" during graph linking.
        legacy.Document(
            id=path.stem, text=path.read_text(encoding="utf-8").replace("\x00", "")
        )
        for path in sorted(INPUTS_DIR.glob("*.md"))
    ]
    if not documents:
        raise legacy.BenchmarkError(f"no markdown files found in {INPUTS_DIR}")
    return legacy.DatasetBundle(documents, [], "combined")


def main() -> int:
    args = build_args("novel")
    args.ingestion_concurrency = INGESTION_CONCURRENCY
    legacy.validate_runtime(args, ["ours"])

    bundle = prepare_dataset(NAME, DATASTORE, args, load)
    manifest = json.loads((DATASTORE / "manifest.json").read_text(encoding="utf-8"))
    mapping = json.loads(
        (DATASTORE / "canonical" / "documents.json").read_text(encoding="utf-8")
    )

    workspace = DATASTORE / "clients" / "llm_wiki"
    result = llm_wiki.ingest(workspace, bundle, mapping, manifest, args)

    # llm_wiki.ingest hardcodes its own database name (used by the shared
    # benchmark harness's resume/completion checks) -- copy it out under the
    # requested name instead of renaming it in place.
    for suffix in ("", "-wal", "-shm"):
        source = workspace / f"wiki.sqlite{suffix}"
        if source.is_file():
            shutil.copy2(source, FINAL_DB.with_name(FINAL_DB.name + suffix))

    legacy.log(f"done: {result['status']} -- database at {FINAL_DB}")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
