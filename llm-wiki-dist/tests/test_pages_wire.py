from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from graph.core import Node, NodeType
from graph.librarian import Librarian, WriteJob
from graph.store import GraphStore


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


    def test_one_wiki_holds_documents_ingested_in_both_modes(self) -> None:
        """Chunks-mode and pages-mode documents share a single wiki.

        Switching the ingest mode is a per-document decision, so a library
        built before the switch and a document ingested after it have to live
        side by side: separate scratch directories, separate ingest passes, and
        no leakage of one document's mode into the next document's run.
        """
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

            def make_job(document_name: str, **chunk_options) -> WriteJob:
                return WriteJob(
                    id=document_name,
                    type="chunk_and_ingest",
                    payload={
                        "body": "line\n" * 301,
                        "document_name": document_name,
                        "chunk_options": chunk_options,
                    },
                )

            modes: list[str] = []
            ingested: list[Path] = []

            def run_pipeline(mode: str, **kwargs):
                modes.append(mode)
                out_dir = Path(kwargs["out_dir"])
                out_dir.mkdir(parents=True, exist_ok=True)
                return SimpleNamespace(out_dir=out_dir, file_count=len(mode))

            def ingest(md_output_dir, **_kwargs):
                ingested.append(Path(md_output_dir))
                return []

            with (
                mock.patch("graph.chunk.make_llm", return_value=object()),
                mock.patch(
                    "graph.chunk.run_chunk_pipeline",
                    side_effect=lambda **kwargs: run_pipeline("chunks", **kwargs),
                ),
                mock.patch(
                    "graph.pages.run_pages_pipeline",
                    side_effect=lambda **kwargs: run_pipeline("pages", **kwargs),
                ),
                mock.patch.object(librarian, "ingest_md_output", side_effect=ingest),
            ):
                librarian.chunk_and_ingest(make_job("legacy.md"))
                librarian.chunk_and_ingest(
                    make_job("assembled.md", ingest_mode="pages")
                )
                # No per-document choice: this one follows the server default,
                # which the pages run above must not have disturbed.
                librarian.chunk_and_ingest(make_job("later.md"))

            self.assertEqual(modes, ["chunks", "pages", "chunks"])
            self.assertEqual(len(ingested), 3)

            # Each run got its own scratch directory inside the same wiki, so
            # neither the pages document nor the chunks documents collide.
            wiki_dir = Path(settings.database_path).parent
            self.assertEqual(len({path.name for path in ingested}), 3)
            for path in ingested:
                self.assertEqual(path.parent.parent, wiki_dir)
                self.assertEqual(path.parent.name, "chunked")

            self.assertEqual(settings.ingest_mode, "chunks")

    def test_page_and_chunk_nodes_answer_the_same_search(self) -> None:
        """A page node and a chunk node in one store are both retrievable."""
        with tempfile.TemporaryDirectory() as temporary:
            store = GraphStore(Path(temporary) / "wiki.sqlite")

            try:
                store.upsert_node(
                    Node(
                        id="chunk:legacy-1",
                        title="legacy filenum",
                        body="The legacy split stores filenum in the third argument.",
                        summary="legacy filenum",
                        original_document_name="legacy.md",
                        source_path="chunked/legacy.md",
                    )
                )
                store.upsert_node(
                    Node(
                        id="page:assembled-1",
                        title="assembled filenum page",
                        body="The assembled page collects every filenum mention.",
                        summary="assembled filenum page",
                        type=NodeType.page,
                        original_document_name="assembled.md",
                        source_path="pages/assembled/filenum.md",
                        source_ranges=[(0, 40), (40, 80)],
                    )
                )

                found = {node.id: node for node in store.keyword_search("filenum")}

                self.assertEqual(set(found), {"chunk:legacy-1", "page:assembled-1"})
                # The page node keeps its type: page-mode documents stay
                # identifiable after the round trip through the shared store.
                self.assertEqual(found["page:assembled-1"].type, NodeType.page)
                self.assertEqual(found["chunk:legacy-1"].type, NodeType.endogenous)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
