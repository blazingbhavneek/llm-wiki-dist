from __future__ import annotations

from types import SimpleNamespace
import unittest

from graph.vectors import QdrantIndex


class FakeQdrant:
    def __init__(self) -> None:
        self.collection: dict | None = None
        self.points: list[dict] = []
        self.indexes: list[dict] = []

    def collection_exists(self, *, collection_name: str) -> bool:
        return self.collection is not None

    def create_collection(self, *, collection_name: str, vectors_config) -> None:
        self.collection = {"name": collection_name, "vectors": vectors_config}

    def get_collection(self, *, collection_name: str):
        vectors = self.collection["vectors"]
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors=vectors),
            )
        )

    def create_payload_index(self, **kwargs) -> None:
        self.indexes.append(kwargs)

    def upsert(self, *, collection_name: str, points: list[dict], wait: bool) -> None:
        for point in points:
            self.points = [item for item in self.points if item["id"] != point["id"] or item["payload"]["growi_id"] != point["payload"]["growi_id"] or item["payload"]["channel"] != point["payload"]["channel"]]
            self.points.append(point)

    @staticmethod
    def _matches(point: dict, query_filter: dict) -> bool:
        for condition in query_filter["must"]:
            if "has_id" in condition:
                if point["id"] not in condition["has_id"]:
                    return False
                continue
            key = condition["key"]
            match = condition["match"]
            if "value" in match and point["payload"].get(key) != match["value"]:
                return False
            if "any" in match and point["payload"].get(key) not in match["any"]:
                return False
        return True

    def search(self, *, collection_name: str, query_vector, query_filter, limit: int, with_payload: bool):
        matches = [point for point in self.points if self._matches(point, query_filter)]
        return [SimpleNamespace(id=point["id"], score=float(sum(query_vector[i] * point["vector"][i] for i in range(len(query_vector))))) for point in matches[:limit]]

    def delete(self, *, collection_name: str, points_selector, wait: bool) -> None:
        query_filter = points_selector["filter"]
        self.points = [point for point in self.points if not self._matches(point, query_filter)]

    def count(self, *, collection_name: str, count_filter, exact: bool):
        return SimpleNamespace(count=sum(self._matches(point, count_filter) for point in self.points))


class QdrantVectorIndexTests(unittest.TestCase):
    def test_one_collection_is_tenant_filtered(self):
        client = FakeQdrant()
        first = QdrantIndex("http://qdrant", client=client, growi_id="first")
        second = QdrantIndex("http://qdrant", client=client, growi_id="second")

        first.upsert("body", ["same-id"], [[1.0, 0.0]])
        second.upsert("body", ["same-id"], [[0.0, 1.0]])

        self.assertEqual([("same-id", 1.0)], first.search("body", [1.0, 0.0], 5))
        self.assertEqual([("same-id", 1.0)], second.search("body", [0.0, 1.0], 5))
        self.assertEqual(1, first.count("body"))
        self.assertEqual(1, second.count("body"))
        self.assertEqual({"first", "second"}, {item["payload"]["growi_id"] for item in client.points})
        self.assertTrue(any(item.get("is_tenant") for item in client.indexes))

        first.delete("body", ["same-id"])
        self.assertEqual([], first.search("body", [1.0, 0.0], 5))
        self.assertEqual([("same-id", 1.0)], second.search("body", [0.0, 1.0], 5))

    def test_collection_dimension_is_a_startup_gate(self):
        client = FakeQdrant()
        QdrantIndex("http://qdrant", client=client, growi_id="first").ensure("body", 2)
        with self.assertRaisesRegex(ValueError, "dimension mismatch"):
            QdrantIndex("http://qdrant", client=client, growi_id="second").ensure("body", 3)


if __name__ == "__main__":
    unittest.main()
