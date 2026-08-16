from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from graph.core import Edge, Node
from graph.neighborhood import build_payload, neighbors_for_seeds, walk
from graph.store import GraphStore


class NeighborhoodTests(unittest.TestCase):
    """A printed page split into chunks, plus one typed link off to the side."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.store = GraphStore(Path(self._dir.name) / "graph.sqlite")

        # page.md is one document chained across four chunks.
        for index in range(1, 5):
            self.store.upsert_node(
                Node(
                    id=f"node:{index}",
                    body=f"chunk {index}",
                    title=f"chunk {index}",
                    summary=f"summary {index}",
                    source_path="docs/page.md",
                    original_document_name="page.md",
                    source_ranges=[(index * 100, index * 100 + 50)],
                )
            )
        for index in range(1, 4):
            self.store.upsert_edge(
                Edge(
                    id=f"follows:{index}",
                    source_node_id=f"node:{index}",
                    target_node_id=f"node:{index + 1}",
                    label="follows",
                )
            )

        # A reference off the page, reachable in one typed hop.
        self.store.upsert_node(
            Node(id="node:ref", body="reference", title="reference", summary="linked")
        )
        self.store.upsert_edge(
            Edge(
                id="reference:1",
                source_node_id="node:1",
                target_node_id="node:ref",
                label="reference",
            )
        )
        # Another page entirely: never a neighbour of node:1.
        self.store.upsert_node(
            Node(id="node:other", body="elsewhere", source_path="docs/other.md")
        )

    def tearDown(self) -> None:
        self.store.close()
        self._dir.cleanup()

    def test_chain_travels_further_than_typed_edges(self):
        found = walk(self.store, ["node:1"], chain_hops=3, typed_hops=1, siblings=False)

        ids = [ref.node_id for ref in found["node:1"]]
        # Three hops down the chain plus the one-hop typed link, and nothing
        # from the other document.
        self.assertEqual(set(ids), {"node:2", "node:3", "node:4", "node:ref"})
        self.assertNotIn("node:other", ids)
        distances = {ref.node_id: ref.distance for ref in found["node:1"]}
        self.assertEqual(distances["node:4"], 3)
        self.assertEqual(distances["node:ref"], 1)

    def test_one_chain_hop_stops_at_the_next_chunk(self):
        found = walk(self.store, ["node:1"], chain_hops=1, typed_hops=1, siblings=False)
        self.assertEqual(
            {ref.node_id for ref in found["node:1"]}, {"node:2", "node:ref"}
        )

    def test_siblings_come_from_the_same_page_ordered_by_offset(self):
        # node:ref has no chain at all; its page is how it is reached.
        found = walk(self.store, ["node:4"], chain_hops=0, typed_hops=0, siblings=True)
        siblings = [ref for ref in found["node:4"] if ref.relation == "sibling"]

        self.assertEqual([ref.node_id for ref in siblings][:2], ["node:3", "node:2"])

    def test_stored_payload_answers_without_touching_the_edges(self):
        payload = build_payload(self.store, "node:1")
        self.store.set_node_neighborhood("node:1", payload)

        # Delete every edge: a cache hit must not need them.
        for index in range(1, 4):
            self.store.delete_edge(f"follows:{index}")
        self.store.delete_edge("reference:1")

        found = neighbors_for_seeds(
            self.store, ["node:1"], chain_hops=3, typed_hops=1, siblings=True, limit=8
        )
        self.assertEqual(
            {ref.node_id for ref in found["node:1"]},
            {"node:2", "node:3", "node:4", "node:ref"},
        )

    def test_a_shallower_cache_is_not_used_for_a_deeper_request(self):
        self.store.set_node_neighborhood(
            "node:1", build_payload(self.store, "node:1", chain_hops=1)
        )

        found = neighbors_for_seeds(
            self.store, ["node:1"], chain_hops=3, typed_hops=1, siblings=True, limit=8
        )
        # Fell back to a live walk, so the far chunks are present.
        self.assertIn("node:4", {ref.node_id for ref in found["node:1"]})

    def test_missing_cache_rows_fall_back_to_walking(self):
        found = neighbors_for_seeds(
            self.store, ["node:1"], chain_hops=3, typed_hops=1, siblings=True, limit=2
        )
        refs = found["node:1"]
        self.assertEqual(len(refs), 2)
        # Nearest first, chain before typed.
        self.assertEqual(refs[0].node_id, "node:2")

    def test_deleting_a_node_drops_its_stored_walk(self):
        self.store.set_node_neighborhood("node:1", build_payload(self.store, "node:1"))
        self.assertEqual(self.store.count_node_neighborhoods(), 1)

        self.store.delete_node("node:1")
        self.assertEqual(self.store.get_node_neighborhoods(["node:1"]), {})
        self.assertEqual(self.store.count_node_neighborhoods(), 0)

    def test_bulk_edge_fetch_returns_both_directions_once(self):
        edges = self.store.get_edges_for_nodes(["node:2", "node:3"])
        self.assertEqual(
            {edge.id for edge in edges}, {"follows:1", "follows:2", "follows:3"}
        )

    def test_inactive_neighbours_are_skipped(self):
        from graph.core import NodeStatus

        self.store.set_node_status("node:2", NodeStatus.deleted)
        found = walk(self.store, ["node:1"], chain_hops=3, typed_hops=1, siblings=False)
        ids = {ref.node_id for ref in found["node:1"]}
        self.assertNotIn("node:2", ids)


if __name__ == "__main__":
    unittest.main()
