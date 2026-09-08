from __future__ import annotations

import unittest

from graph.vectors import SqliteVecIndex


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def ensure_vec_tables(self, dim):
        self.calls.append(("ensure", dim))

    def set_vector(self, item_id, table, vector):
        self.calls.append(("upsert", item_id, table, vector))

    def vector_search(self, vector, table, limit):
        self.calls.append(("search", vector, table, limit))
        return [("id", 0.1)]

    def delete_vector(self, item_id, table):
        self.calls.append(("delete", item_id, table))


class SqliteVectorIndexTests(unittest.TestCase):
    def test_channels_map_to_existing_tables(self):
        store = FakeStore()
        index = SqliteVecIndex(store)
        index.ensure("body", 3)
        index.upsert("summary", ["a"], [[1.0, 2.0]])
        self.assertEqual(index.search("bridge", [1.0], 5), [("id", 0.1)])
        index.delete("body", ["a"])
        self.assertIn(("upsert", "a", "vec_summary", [1.0, 2.0]), store.calls)
        self.assertIn(("delete", "a", "vec_body"), store.calls)

    def test_unknown_channel_is_rejected(self):
        with self.assertRaises(ValueError):
            SqliteVecIndex(FakeStore()).search("unknown", [1.0], 1)


if __name__ == "__main__":
    unittest.main()
