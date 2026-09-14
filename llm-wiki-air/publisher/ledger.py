"""Atomic state for the no-Git publisher."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graph.wiki.storage import read_json, write_json_atomic


@dataclass
class Ledger:
    sources: dict[str, dict[str, Any]]
    published_documents: dict[str, dict[str, Any]]

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "sources": self.sources,
            "published_documents": self.published_documents,
        }


def load_ledger(path: Path) -> Ledger:
    if not Path(path).exists():
        return Ledger({}, {})
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"invalid pipeline ledger: {path}")
    sources = data.get("sources", {})
    published = data.get("published_documents", {})
    if not isinstance(sources, dict) or not isinstance(published, dict):
        raise ValueError(f"invalid pipeline ledger maps: {path}")
    return Ledger(dict(sources), dict(published))


def save_ledger(path: Path, ledger: Ledger) -> None:
    write_json_atomic(path, ledger.as_json())


__all__ = ["Ledger", "load_ledger", "save_ledger"]
