from __future__ import annotations

import unittest
from unittest import mock

from graph import pages


class Embedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_document(self, text: str) -> list[float]:
        self.calls.append(text)
        return [float(len(text)), 1.0]


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_vectors_are_computed_once_and_bodies_stay_out_of_prompts(self):
        chunks = [
            pages.ChunkRef(
                id=f"c{i}", title=f"T{i}", summary=f"S{i}",
                source_start=i + 1, source_end=i + 1, body=f"BODY-{i}"
            )
            for i in range(3)
        ]
        shelf = pages.Shelf(
            pages=[
                pages.ShelfPage(id="ch", title="章", is_chapter=True),
                pages.ShelfPage(id="p1", title="P1", description="D1", parent_id="ch"),
                pages.ShelfPage(id="p2", title="P2", description="D2", parent_id="ch"),
            ]
        )
        embedder = Embedder()
        prompts: list[str] = []

        async def fake_structured(_llm, _schema, messages, **_kwargs):
            prompts.append(messages[-1].content)
            return pages._RouteResult(page_id="p1", heading="見出し", confidence=0.9)

        with mock.patch.object(pages, "structured_ainvoke", fake_structured):
            decisions, parked = await pages.route_chunks(
                object(), embedder, chunks, shelf, min_score=0.0, concurrency=2
            )
        self.assertEqual(len(decisions), len(chunks))
        self.assertFalse(parked)
        self.assertEqual(sum(1 for call in embedder.calls if call.startswith("P")), 2)
        self.assertTrue(all("BODY-" not in prompt for prompt in prompts))

    async def test_low_scores_are_parked(self):
        class ZeroEmbedder:
            def embed_document(self, _text):
                return [0.0, 0.0]

        chunk = pages.ChunkRef(
            id="c", title="T", summary="S", source_start=1, source_end=1, body="B"
        )
        shelf = pages.Shelf(
            pages=[
                pages.ShelfPage(id="ch", title="章", is_chapter=True),
                pages.ShelfPage(id="p", title="P", description="D", parent_id="ch"),
            ]
        )

        async def fake_structured(_llm, _schema, messages, **_kwargs):
            return pages._RouteResult(page_id="p", heading="見出し", confidence=1.0)

        with mock.patch.object(pages, "structured_ainvoke", fake_structured):
            decisions, parked = await pages.route_chunks(
                object(), ZeroEmbedder(), [chunk], shelf, min_score=0.25
            )
        self.assertIsNone(decisions[0].page_id)
        self.assertEqual([item.id for item in parked], ["c"])


if __name__ == "__main__":
    unittest.main()
