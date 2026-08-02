from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from graph import chunk
from graph.core import Node, NodeType, Settings
from graph.librarian import Librarian
from graph.store import GraphStore


class AsyncConcurrencyTracker:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0

    async def pause(self) -> None:
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(0.02)
        finally:
            self.active -= 1


class ChunkConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_planning_windows_use_bounded_concurrency_and_keep_order(
        self,
    ) -> None:
        tracker = AsyncConcurrencyTracker()

        async def fake_split(**kwargs):
            await tracker.pause()
            start = kwargs["source_start"]
            end = kwargs["source_end"]
            return [
                chunk.ConceptFilePlan(
                    title=f"topic-{start}",
                    filename=f"topic-{start}.md",
                    source_start=start,
                    source_end=end,
                    summary="summary",
                )
            ]

        source_lines = [f"line {index}" for index in range(1, 13)]
        with mock.patch.object(chunk, "split_window_until_valid", fake_split):
            result = await chunk.plan_concept_files_streaming(
                llm=object(),
                source_lines=source_lines,
                target_lines=2,
                max_extra=0,
                concurrency=3,
            )

        self.assertEqual(tracker.maximum, 3)
        self.assertEqual(result[0].source_start, 1)
        self.assertEqual(result[-1].source_end, len(source_lines))
        self.assertEqual(
            [item.source_start for item in result],
            sorted(item.source_start for item in result),
        )

    async def test_header_enrichment_fans_out_then_resolves_continuations(
        self,
    ) -> None:
        tracker = AsyncConcurrencyTracker()

        async def fake_structured(_llm, schema, messages, **_kwargs):
            await tracker.pause()
            if schema is chunk.GlobalName:
                return chunk.GlobalName(inferred_file_name="novel.md")
            if schema is chunk.ChunkHeader:
                return chunk.ChunkHeader(header="root")
            text = messages[-1].content
            current = text.split("現在のチャンク情報:", 1)[-1]
            if "title-1" in current:
                return chunk.ChunkHeaderDecision(
                    continues_previous=True, header=""
                )
            title = current.split("- タイトル: ", 1)[1].splitlines()[0]
            return chunk.ChunkHeaderDecision(
                continues_previous=False, header=f"header-{title}"
            )

        files = [
            chunk.ConceptFilePlan(
                title=f"title-{index}",
                filename=f"file-{index}.md",
                source_start=index + 1,
                source_end=index + 1,
                summary=f"summary-{index}",
            )
            for index in range(4)
        ]
        with mock.patch.object(chunk, "structured_ainvoke", fake_structured):
            result = await chunk.enrich_concept_plan(
                llm=object(),
                original_filename="novel.txt",
                files=files,
                concurrency=2,
            )

        self.assertEqual(tracker.maximum, 2)
        self.assertEqual(
            [item.header for item in result.files],
            ["root", "root", "header-title-2", "header-title-3"],
        )


class LocalRenderTests(unittest.TestCase):
    def test_unmatched_fence_like_prose_is_literal_but_paired_fences_are_safe(
        self,
    ) -> None:
        prose = ["before", "```Ah pudet annorum!", "after", "tail"]
        scan = chunk.scan_markdown_fences(prose)

        self.assertIsNotNone(scan.unclosed)
        self.assertFalse(any(scan.inside_after_line))
        chunk.assert_no_unclosed_markdown_fences(prose, label="test source")
        self.assertFalse(chunk.cut_is_inside_fence(prose, 4))

        fenced = ["before", "```python", "code", "```", "after"]
        self.assertTrue(chunk.cut_is_inside_fence(fenced, 4))
        self.assertFalse(chunk.cut_is_inside_fence(fenced, 5))

    def test_large_duplicate_plan_renders_with_bounded_filenames(self) -> None:
        source_lines = [f"line {index}" for index in range(1, 391)]
        long_filename = f"{'長い名前' * 100}.md"
        plans = [
            chunk.ConceptFilePlan(
                title=f"topic-{index}",
                filename=long_filename,
                source_start=index * 2 + 1,
                source_end=index * 2 + 2,
                summary="summary",
            )
            for index in range(195)
        ]

        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            coverage = chunk.render_concept_files(
                docs_dir=output_root / "docs",
                source_lines=source_lines,
                files=plans,
                manifest=chunk.init_manifest(Path("novel.txt")),
                output_root=output_root,
            )
            chunk.assert_rendered_docs_match_source(
                source_lines=source_lines,
                coverage=coverage,
                output_root=output_root,
            )

            rendered = list((output_root / "docs").iterdir())
            self.assertEqual(len(rendered), len(plans))
            self.assertTrue(
                all(len(path.name.encode("utf-8")) < 255 for path in rendered)
            )


class ThreadConcurrencyTracker:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def pause(self) -> None:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            time.sleep(0.02)
        finally:
            with self.lock:
                self.active -= 1


class GraphIngestConcurrencyTests(unittest.TestCase):
    def test_node_prepare_and_link_phases_use_same_bounded_budget(self) -> None:
        librarian = object.__new__(Librarian)
        librarian._ingest_concurrency = 3
        librarian.gateway = SimpleNamespace(
            settings=SimpleNamespace(entity_dedup=False, ingest_concurrency=3)
        )
        nodes = [SimpleNamespace(id=f"node-{index}") for index in range(7)]
        prepared: set[str] = set()
        prepared_lock = threading.Lock()
        prepare_tracker = ThreadConcurrencyTracker()
        link_tracker = ThreadConcurrencyTracker()

        def prepare(node):
            prepare_tracker.pause()
            with prepared_lock:
                prepared.add(node.id)
            return ([1.0], None, None)

        def link(node, _vectors):
            with prepared_lock:
                self.assertEqual(len(prepared), len(nodes))
            link_tracker.pause()
            return []

        librarian._prepare_node = prepare
        librarian._link_node = link
        librarian._link_entity_duplicates = mock.Mock()

        librarian._prepare_and_link_nodes(nodes, concurrency=3)

        self.assertEqual(prepare_tracker.maximum, 3)
        self.assertEqual(link_tracker.maximum, 3)
        librarian._link_entity_duplicates.assert_not_called()

    def test_parallel_graph_phases_persist_all_nodes_in_sqlite(self) -> None:
        class FakeEmbedder:
            dim = 2

            def embed_document(self, text: str) -> list[float]:
                return [float((len(text) % 7) + 1), 1.0]

            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                return [self.embed_document(text) for text in texts]

        class FakeLlm:
            def complete(self, *_args) -> str:
                return "summary"

            def complete_structured(self, *_args):
                return _args[-1]()

        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(
                database_path=str(Path(temporary) / "wiki.sqlite"),
                entity_dedup=False,
                ingest_concurrency=5,
                edge_candidate_k=3,
                vector_query_k=3,
            )
            gateway = SimpleNamespace(
                settings=settings,
                embedder=FakeEmbedder(),
                llm=FakeLlm(),
            )
            store = GraphStore(settings.database_path)
            librarian = Librarian(gateway, store, background=False)
            nodes = [
                Node(
                    id=f"node-{index}",
                    body=f"body text {index}",
                    type=NodeType.endogenous,
                    title=f"Title {index}",
                    original_document_name="document.md",
                    source_version="version",
                    source_material_hash=f"hash-{index}",
                    entity=f"entity-{index}",
                    claims=[f"claim {index}"],
                    keywords=[f"keyword-{index}"],
                    summary=f"summary {index}",
                    bridge_probe=f"probe {index}",
                )
                for index in range(12)
            ]

            try:
                librarian._prepare_and_link_nodes(nodes, concurrency=5)
                self.assertEqual(len(store.get_all_nodes()), len(nodes))
            finally:
                store.close()


class IngestConfigurationTests(unittest.TestCase):
    def test_settings_accept_unified_ingest_concurrency(self) -> None:
        with mock.patch.dict(os.environ, {"WIKI_INGEST_CONCURRENCY": "7"}):
            self.assertEqual(Settings.from_env().ingest_concurrency, 7)


if __name__ == "__main__":
    unittest.main()
