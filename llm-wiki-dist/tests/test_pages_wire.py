from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from graph.librarian import Librarian, WriteJob


class PageWireTests(unittest.TestCase):
    def test_chunk_job_selects_pages_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = SimpleNamespace(
                database_path=str(Path(temporary) / "wiki.sqlite"),
                chat_model="model",
                chat_base_url="http://llm",
                chat_api_key="key",
                chat_temperature=0.0,
                ingest_concurrency=1,
                ingest_mode="chunks",
            )
            librarian = object.__new__(Librarian)
            librarian.gateway = SimpleNamespace(settings=settings, embedder=object())
            librarian._ingest_concurrency = 1
            job = WriteJob(
                id="job",
                type="chunk_and_ingest",
                payload={
                    "body": "line\n" * 301,
                    "document_name": "book.md",
                    "chunk_options": {"ingest_mode": "pages"},
                },
            )
            result_dir = Path(temporary) / "result"
            result_dir.mkdir()
            result = SimpleNamespace(out_dir=result_dir, file_count=1)

            with (
                mock.patch("graph.chunk.make_llm", return_value=object()),
                mock.patch("graph.pages.run_pages_pipeline", return_value=result) as run_pages,
                mock.patch.object(librarian, "ingest_md_output", return_value=[]),
            ):
                outcome = librarian.chunk_and_ingest(job)

        run_pages.assert_called_once()
        self.assertEqual(outcome["ingested"], 0)
        self.assertEqual(outcome["files"], 1)


if __name__ == "__main__":
    unittest.main()
