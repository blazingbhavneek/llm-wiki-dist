"""GraphRAG-Benchmark novel dataset loader (all answerable questions)."""

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
    questions = sorted(
        (
            item
            for item in legacy.load_questions(Path(args.questions))
            if item.source in document_ids
        ),
        key=lambda item: (item.source, item.id),
    )
    if not questions:
        raise legacy.BenchmarkError("the novel dataset has no source-linked questions")
    return legacy.DatasetBundle(documents, questions, "combined")


def prepare(datastore: Path, args: SimpleNamespace) -> legacy.DatasetBundle:
    return prepare_dataset(NAME, datastore, args, load)

