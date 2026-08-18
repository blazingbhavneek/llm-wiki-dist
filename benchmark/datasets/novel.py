"""GraphRAG-Benchmark novel dataset loader (all answerable questions, or a
--sample-capped subset of them).

The corpus is always all 20 novels regardless of --sample: it doesn't scale
with question count, so a sample only cuts query cost, not ingest size.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import benchmark as legacy

from .common import prepare_dataset


NAME = "novel"


def load(args: SimpleNamespace) -> legacy.DatasetBundle:
    legacy.ensure_datasets(Path(args.corpus), Path(args.questions))
    documents = legacy.load_documents(Path(args.corpus))
    document_ids = {item.id for item in documents}
    questions = [
        item
        for item in legacy.load_questions(Path(args.questions))
        if item.source in document_ids
    ]
    if not questions:
        raise legacy.BenchmarkError("the novel dataset has no source-linked questions")
    questions = legacy.select_questions(
        questions, allowed_types=(), sample=args.sample, seed=args.seed
    )
    return legacy.DatasetBundle(documents, questions, "combined")


def prepare(datastore: Path, args: SimpleNamespace) -> legacy.DatasetBundle:
    return prepare_dataset(NAME, datastore, args, load)

