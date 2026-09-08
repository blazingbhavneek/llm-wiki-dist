from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from graph import pages
from graph.librarian import Librarian


class AssembleTests(unittest.TestCase):
    def test_written_page_preserves_each_chunk_and_round_trips_ranges(self):
        chunks = {
            "c1": pages.ChunkRef(
                id="c1", title="一", summary="S", source_start=1, source_end=2, body="alpha\nline"
            ),
            "c2": pages.ChunkRef(
                id="c2", title="二", summary="S", source_start=9, source_end=10, body="beta\nline"
            ),
        }
        shelf = pages.finalize_shelf(
            pages.Shelf(
                pages=[
                    pages.ShelfPage(id="ch", title="章", is_chapter=True),
                    pages.ShelfPage(id="p", title="頁", parent_id="ch", chunk_ids=["c1", "c2"]),
                ]
            )
        )
        leaf = next(page for page in shelf.pages if not page.is_chapter)
        decisions = [
            pages.RouteDecision(chunk_id=chunk_id, page_id=leaf.id, heading=chunks[chunk_id].title)
            for chunk_id in chunks
        ]
        with tempfile.TemporaryDirectory() as temporary:
            output = pages.write_pages_output(
                out_dir=Path(temporary), shelf=shelf, decisions=decisions,
                chunks_by_id=chunks, document_name="source.md", source_line_count=10
            )
            pages.assert_pages_preserve_chunks(shelf, decisions, chunks, Path(temporary))
            text = next((Path(temporary) / "docs").glob("*.md")).read_text()
            meta, _body = Librarian._split_frontmatter(object.__new__(Librarian), text)
            self.assertEqual(json.loads(meta["source_lines"]), [[1, 2], [9, 10]])
            self.assertEqual(output.file_count, 1)

    def test_sizing_splits_at_chunk_boundaries(self):
        chunks = {
            f"c{i}": pages.ChunkRef(
                id=f"c{i}", title=str(i), summary="", source_start=i * 10 + 1,
                source_end=i * 10 + 10, body=str(i)
            )
            for i in range(20)
        }
        shelf = pages.Shelf(
            pages=[
                pages.ShelfPage(id="ch", title="章", is_chapter=True),
                pages.ShelfPage(id="p", title="頁", parent_id="ch", chunk_ids=list(chunks)),
            ]
        )
        sized = pages.size_pages(
            shelf, chunks, SimpleNamespace(page_min_chunks=1, page_max_chunks=8, page_min_lines=1, page_max_lines=1000)
        )
        self.assertGreaterEqual(len([page for page in sized.pages if not page.is_chapter]), 3)
        self.assertTrue(all(len(page.chunk_ids) <= 8 for page in sized.pages if not page.is_chapter))


if __name__ == "__main__":
    unittest.main()
