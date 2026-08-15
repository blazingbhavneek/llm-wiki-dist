"""Dataset adapters used by the benchmark runner."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

import benchmark as legacy


NAMES = ("novel", "fanout", "multihop", "musique")


def get_dataset(name: str) -> ModuleType:
    if name not in NAMES:
        raise legacy.BenchmarkError(
            f"unknown dataset {name!r}; choose one of: {', '.join(NAMES)}"
        )
    return import_module(f"{__name__}.{name}")


__all__ = ["NAMES", "get_dataset"]

