from __future__ import annotations

import unittest
from unittest import mock

from graph import pages


class ShelfTests(unittest.IsolatedAsyncioTestCase):
    async def test_shelf_uses_summary_only_and_has_three_batches(self) -> None:
        chunks = [
            pages.ChunkRef(
                id=f"c{i}",
                title=f"題{i}",
                summary=f"要約{i}",
                source_start=i + 1,
                source_end=i + 1,
                body=f"SECRET BODY {i}",
            )
            for i in range(250)
        ]
        calls: list[str] = []

        async def fake_structured(_llm, schema, messages, **_kwargs):
            prompt = messages[-1].content
            calls.append(prompt)
            if schema is pages._LocalOutline:
                return pages._LocalOutline(
                    topics=[pages._Topic(title="局所", description="説明")]
                )
            return pages._ShelfTree(
                chapters=[
                    pages._Chapter(
                        title="章", description="説明", pages=[pages._Topic(title="頁")]
                    )
                ]
            )

        with mock.patch.object(pages, "structured_ainvoke", fake_structured):
            shelf = await pages.plan_shelf(object(), chunks, batch_size=100)

        self.assertEqual(len(calls), 4)
        self.assertTrue(all("SECRET BODY" not in prompt for prompt in calls))
        self.assertEqual(len({page.path for page in shelf.pages}), len(shelf.pages))
        self.assertTrue(any(not page.is_chapter for page in shelf.pages))


class ShelfFinalizationTests(unittest.TestCase):
    def test_paths_and_ids_are_stable_and_empty_chapters_are_removed(self) -> None:
        shelf = pages.Shelf(
            pages=[
                pages.ShelfPage(id="a", title="A", chunk_ids=["c1"]),
                pages.ShelfPage(id="empty", title="Empty", chunk_ids=[]),
            ]
        )
        first = pages.finalize_shelf(shelf)
        second = pages.finalize_shelf(shelf)
        self.assertEqual(first.model_dump(), second.model_dump())
        self.assertEqual(len({page.path for page in first.pages}), len(first.pages))
        self.assertFalse(any(page.title == "Empty" for page in first.pages))


if __name__ == "__main__":
    unittest.main()
