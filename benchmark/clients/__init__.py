"""Retrieval client adapters used by :mod:`benchmark.runner`."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

import benchmark as legacy


NAMES = ("vanilla", "llm_wiki", "graphrag")


def get_client(name: str) -> ModuleType:
    if name not in NAMES:
        raise legacy.BenchmarkError(
            f"unknown client {name!r}; choose one of: {', '.join(NAMES)}"
        )
    module_name = "graphrag_cli" if name == "graphrag" else name
    return import_module(f"{__name__}.{module_name}")


__all__ = ["NAMES", "get_client"]

