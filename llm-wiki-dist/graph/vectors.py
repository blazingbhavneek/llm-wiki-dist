"""Vector storage seam used by ingestion and candidate retrieval."""

from __future__ import annotations

import threading
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


class QdrantIndex:
    """Qdrant implementation of the vector storage seam.

    One collection is shared by all local databases.  ``growi_id`` is a
    mandatory tenant payload, while ``channel`` keeps body, summary, bridge,
    and search-item vectors logically separate.  The qdrant client is loaded
    lazily so the historical SQLite path does not acquire a new import or
    startup dependency.
    """

    def __init__(
        self,
        url: str,
        *,
        collection: str = "wiki_vectors",
        growi_id: str = "default",
        client: Any | None = None,
        models: Any | None = None,
    ) -> None:
        if client is None and not str(url).strip():
            raise ValueError("Qdrant URL is required when vector_backend=qdrant")
        self.url = str(url).strip()
        self.collection = collection.strip() or "wiki_vectors"
        self.growi_id = growi_id.strip() or "default"
        self._client = client
        self._models = models
        self._dim: int | None = None
        self._lock = threading.RLock()

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from qdrant_client import QdrantClient
            except ImportError as exc:
                raise RuntimeError(
                    "qdrant-client is required for vector_backend=qdrant; "
                    "install the qdrant optional dependency"
                ) from exc
            self._client = QdrantClient(url=self.url)
        return self._client

    @property
    def qdrant_models(self) -> Any | None:
        if self._models is None:
            try:
                from qdrant_client import models
            except ImportError:
                # A supplied fake client is useful for tests and does not need
                # the third-party model classes.
                if self._client is not None:
                    return None
                raise RuntimeError(
                    "qdrant-client is required for vector_backend=qdrant"
                )
            self._models = models
        return self._models

    def _collection_exists(self) -> bool:
        client = self.client
        exists = getattr(client, "collection_exists", None)
        if exists is not None:
            return bool(exists(collection_name=self.collection))
        try:
            client.get_collection(collection_name=self.collection)
        except Exception as exc:  # noqa: BLE001 - client versions differ here
            if "not found" in str(exc).lower() or "404" in str(exc):
                return False
            raise
        return True

    @staticmethod
    def _value(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def _collection_dimension(self, info: Any) -> int | None:
        config = self._value(info, "config", info)
        params = self._value(config, "params", config)
        vectors = self._value(params, "vectors", params)
        if isinstance(vectors, dict):
            if "size" in vectors:
                return int(vectors["size"])
            # Named vectors are not used by this adapter, but accepting the
            # first entry makes the mismatch check useful on older collections.
            if vectors:
                vectors = next(iter(vectors.values()))
        size = self._value(vectors, "size")
        return int(size) if size is not None else None

    def _vector_params(self, dim: int) -> Any:
        models = self.qdrant_models
        if models is None:
            return {
                "size": dim,
                "distance": "Cosine",
                "hnsw_config": {"m": 0, "payload_m": 16},
            }
        try:
            hnsw = models.HnswConfigDiff(m=0, payload_m=16)
        except TypeError:
            # Keep compatibility with older clients while retaining m=0.
            hnsw = models.HnswConfigDiff(m=0)
        return models.VectorParams(
            size=dim,
            distance=models.Distance.COSINE,
            hnsw_config=hnsw,
        )

    def _ensure_payload_indexes(self) -> None:
        create = getattr(self.client, "create_payload_index", None)
        if create is None:
            return
        models = self.qdrant_models
        schema = (
            models.PayloadSchemaType.KEYWORD if models is not None else "keyword"
        )
        for field, tenant in (("growi_id", True), ("channel", False)):
            kwargs = {
                "collection_name": self.collection,
                "field_name": field,
                "field_schema": schema,
                "wait": True,
            }
            if tenant:
                kwargs["is_tenant"] = True
            try:
                create(**kwargs)
            except TypeError:
                # is_tenant was added after the original payload-index API.
                kwargs.pop("is_tenant", None)
                create(**kwargs)

    def _ensure_collection(self, dim: int) -> None:
        with self._lock:
            if self._dim is not None and self._dim != dim:
                raise ValueError(
                    f"Qdrant vector dimension mismatch: configured {dim}, "
                    f"collection uses {self._dim}"
                )

            if self._collection_exists():
                actual = self._collection_dimension(
                    self.client.get_collection(collection_name=self.collection)
                )
                if actual is not None and actual != dim:
                    raise ValueError(
                        f"Qdrant vector dimension mismatch: configured {dim}, "
                        f"collection uses {actual}"
                    )
            else:
                self.client.create_collection(
                    collection_name=self.collection,
                    vectors_config=self._vector_params(dim),
                )

            self._ensure_payload_indexes()
            self._dim = dim

    def ensure(self, channel: str, dim: int) -> None:
        if not channel.strip():
            raise ValueError("vector channel is required")
        if dim <= 0:
            raise ValueError("vector dimension must be positive")
        self._ensure_collection(dim)

    def _condition(self, key: str, value: Any) -> Any:
        models = self.qdrant_models
        if models is None:
            if isinstance(value, (list, tuple, set)):
                return {"key": key, "match": {"any": list(value)}}
            return {"key": key, "match": {"value": value}}
        if isinstance(value, (list, tuple, set)):
            return models.FieldCondition(
                key=key, match=models.MatchAny(any=list(value))
            )
        return models.FieldCondition(key=key, match=models.MatchValue(value=value))

    def _filter(self, channel: str, filters: dict[str, Any] | None = None) -> Any:
        conditions = [
            self._condition("growi_id", self.growi_id),
            self._condition("channel", channel),
            self._condition("is_tenant", True),
        ]
        for key, value in (filters or {}).items():
            if key in {"growi_id", "channel", "is_tenant"}:
                continue
            conditions.append(self._condition(key, value))
        models = self.qdrant_models
        if models is not None:
            return models.Filter(must=conditions)
        return {"must": conditions}

    def _point(
        self,
        item_id: str,
        vector: list[float],
        channel: str,
        payload: dict[str, Any] | None,
    ) -> Any:
        point_payload = dict(payload or {})
        point_payload.update(
            {"growi_id": self.growi_id, "channel": channel, "is_tenant": True}
        )
        models = self.qdrant_models
        if models is None:
            return {"id": item_id, "vector": vector, "payload": point_payload}
        return models.PointStruct(id=item_id, vector=vector, payload=point_payload)

    def upsert(
        self,
        channel: str,
        ids: list[str],
        vectors: list[list[float]],
        payload: dict[str, Any] | None = None,
    ) -> None:
        if len(ids) != len(vectors):
            raise ValueError("vector ids and values must have the same length")
        if not ids:
            return
        dim = len(vectors[0])
        if any(len(vector) != dim for vector in vectors):
            raise ValueError("all vectors in one upsert must have the same dimension")
        self.ensure(channel, dim)
        self.client.upsert(
            collection_name=self.collection,
            points=[
                self._point(item_id, vector, channel, payload)
                for item_id, vector in zip(ids, vectors)
            ],
            wait=True,
        )

    @staticmethod
    def _points(result: Any) -> list[Any]:
        if result is None:
            return []
        points = getattr(result, "points", None)
        return list(points if points is not None else result)

    def search(
        self,
        channel: str,
        vector: list[float],
        k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[tuple[str, float]]:
        if k <= 0 or not vector:
            return []
        self.ensure(channel, len(vector))
        query_filter = self._filter(channel, filters)
        client = self.client
        if hasattr(client, "query_points"):
            try:
                result = client.query_points(
                    collection_name=self.collection,
                    query=vector,
                    query_filter=query_filter,
                    limit=k,
                    with_payload=False,
                )
            except TypeError:
                result = client.query_points(
                    collection_name=self.collection,
                    query_vector=vector,
                    query_filter=query_filter,
                    limit=k,
                    with_payload=False,
                )
        else:
            result = client.search(
                collection_name=self.collection,
                query_vector=vector,
                query_filter=query_filter,
                limit=k,
                with_payload=False,
            )
        return [
            (str(self._value(point, "id")), float(self._value(point, "score", 0.0)))
            for point in self._points(result)
        ]

    def delete(self, channel: str, ids: list[str]) -> None:
        if not ids or self._dim is None:
            return
        models = self.qdrant_models
        if models is not None:
            selector = models.FilterSelector(
                filter=models.Filter(
                    must=[
                        self._condition("growi_id", self.growi_id),
                        self._condition("channel", channel),
                        models.HasIdCondition(has_id=ids),
                    ]
                )
            )
        else:
            selector = {
                "filter": {
                    "must": [
                        self._condition("growi_id", self.growi_id),
                        self._condition("channel", channel),
                        {"has_id": ids},
                    ]
                }
            }
        self.client.delete(
            collection_name=self.collection,
            points_selector=selector,
            wait=True,
        )

    def count(self, channel: str) -> int:
        if not self._collection_exists():
            return 0
        result = self.client.count(
            collection_name=self.collection,
            count_filter=self._filter(channel),
            exact=True,
        )
        return int(self._value(result, "count", 0))

    def reset(self) -> None:
        """Remove this tenant's points while leaving other tenants intact."""
        if not self._collection_exists():
            return
        models = self.qdrant_models
        # Delete each channel because channel is part of the filter contract.
        for channel in ("body", "summary", "bridge", "search_item"):
            selector = (
                models.FilterSelector(filter=self._filter(channel))
                if models is not None
                else {"filter": self._filter(channel)}
            )
            self.client.delete(
                collection_name=self.collection,
                points_selector=selector,
                wait=True,
            )

    def get(self, channel: str, item_id: str) -> list[float] | None:
        """Fetch one vector for maintenance paths that need a local vector."""
        if not self._collection_exists():
            return None
        models = self.qdrant_models
        selector = (
            models.PointIdsList(points=[item_id])
            if models is not None
            else {"points": [item_id]}
        )
        points = self.client.retrieve(
            collection_name=self.collection,
            ids=selector.points if models is not None else selector["points"],
            with_vectors=True,
        )
        for point in self._points(points):
            payload = self._value(point, "payload", {}) or {}
            if (
                self._value(point, "id") == item_id
                and payload.get("growi_id") == self.growi_id
                and payload.get("channel") == channel
            ):
                return list(self._value(point, "vector", []) or [])
        return None
