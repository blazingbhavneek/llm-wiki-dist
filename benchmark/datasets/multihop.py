"""MultiHop-RAG loader (complete corpus; complete evidence-backed QA set, or
a --sample-capped subset of it).

The corpus is every MultiHop-RAG news article regardless of --sample: only
the question count shrinks, since evidence documents are drawn from a fixed
article set rather than derived per-question like musique/fanout.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import benchmark as legacy

from .common import prepare_dataset


NAME = "multihop"


def load(args: SimpleNamespace) -> legacy.DatasetBundle:
    return legacy.load_multihop_dataset(args)


def prepare(datastore: Path, args: SimpleNamespace) -> legacy.DatasetBundle:
    # Both upstream files are atomic cached downloads; canonical documents are
    # independently checkpointed by prepare_dataset.
    return prepare_dataset(NAME, datastore, args, load)

