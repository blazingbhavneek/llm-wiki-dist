"""MuSiQue loader (all answerable, evidence-backed development questions,
or a --sample-capped subset of them)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import benchmark as legacy

from .common import prepare_dataset


NAME = "musique"


def load(args: SimpleNamespace) -> legacy.DatasetBundle:
    # args.sample is None for the full dataset, or an explicit cap from
    # --sample; either way load_musique_dataset only materializes the
    # supporting paragraphs the selected questions actually reference.
    return legacy.load_musique_dataset(args)


def prepare(datastore: Path, args: SimpleNamespace) -> legacy.DatasetBundle:
    # The archive extraction is atomic and reusable after interruption.
    return prepare_dataset(NAME, datastore, args, load)

