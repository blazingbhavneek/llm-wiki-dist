"""Vector storage seam used by ingestion and candidate retrieval."""

from __future__ import annotations

from typing import Any, Protocol


class VectorIndex(Protocol):
    def ensure(self, channel: str, dim: int) -> None: ...

    def upsert(
        self,
        channel: str,
        ids: list[str],
        vectors: list[list[float]],
        payload: dict[str, Any] | None = None,
    ) -> None: ...

    def search(
        self,
        channel: str,
        vector: list[float],
        k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[tuple[str, float]]: ...

    def delete(self, channel: str, ids: list[str]) -> None: ...


class SqliteVecIndex:
    """Compatibility wrapper around the existing GraphStore sqlite-vec API."""

    _TABLES = {
        "body": "vec_body",
        "summary": "vec_summary",
        "bridge": "vec_bridge",
        "search_item": "vec_search_item",
    }

    def __init__(self, store: Any) -> None:
        self.store = store

    def _table(self, channel: str) -> str:
        try:
            return self._TABLES[channel]
        except KeyError as exc:
            raise ValueError(f"unknown vector channel: {channel}") from exc

    def ensure(self, channel: str, dim: int) -> None:
        self._table(channel)
        self.store.ensure_vec_tables(dim)

    def upsert(
        self,
        channel: str,
        ids: list[str],
        vectors: list[list[float]],
        payload: dict[str, Any] | None = None,
    ) -> None:
        if len(ids) != len(vectors):
            raise ValueError("vector ids and values must have the same length")
        table = self._table(channel)
        for item_id, vector in zip(ids, vectors):
            self.store.set_vector(item_id, table, vector)

    def search(
        self,
        channel: str,
        vector: list[float],
        k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[tuple[str, float]]:
        return self.store.vector_search(vector, self._table(channel), k)

    def delete(self, channel: str, ids: list[str]) -> None:
        table = self._table(channel)
        delete = getattr(self.store, "delete_vector", None)
        if delete is None:
            return
        for item_id in ids:
            delete(item_id, table)
