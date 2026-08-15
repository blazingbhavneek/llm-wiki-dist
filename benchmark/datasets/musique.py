"""MuSiQue loader (all answerable, evidence-backed development questions)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import benchmark as legacy

from .common import prepare_dataset


NAME = "musique"


def load(args: SimpleNamespace) -> legacy.DatasetBundle:
    full_args = SimpleNamespace(**vars(args))
    full_args.sample = None
    return legacy.load_musique_dataset(full_args)


def prepare(datastore: Path, args: SimpleNamespace) -> legacy.DatasetBundle:
    # The archive extraction is atomic and reusable after interruption.
    return prepare_dataset(NAME, datastore, args, load)

