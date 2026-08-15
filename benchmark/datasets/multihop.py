"""MultiHop-RAG loader (complete corpus and complete evidence-backed QA set)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import benchmark as legacy

from .common import prepare_dataset


NAME = "multihop"


def load(args: SimpleNamespace) -> legacy.DatasetBundle:
    full_args = SimpleNamespace(**vars(args))
    full_args.sample = None
    return legacy.load_multihop_dataset(full_args)


def prepare(datastore: Path, args: SimpleNamespace) -> legacy.DatasetBundle:
    # Both upstream files are atomic cached downloads; canonical documents are
    # independently checkpointed by prepare_dataset.
    return prepare_dataset(NAME, datastore, args, load)

