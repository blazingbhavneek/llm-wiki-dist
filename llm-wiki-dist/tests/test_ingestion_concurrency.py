from __future__ import annotations

import asyncio
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from graph import chunk
from graph.core import Settings
from graph.librarian import Librarian


class ChunkPlanningConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_windows_are_bounded_and_returned_in_source_order(self) -> None:
        active = 0
        maximum = 0

        async def fake_split(**kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.005)
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
            finally:
                active -= 1

        with mock.patch.object(chunk, "split_window_until_valid", fake_split):
            result = await chunk.plan_concept_files_streaming(
                llm=object(),
                source_lines=[f"line {i}" for i in range(1, 13)],
                target_lines=2,
                max_extra=0,
                concurrency=3,
            )

        self.assertEqual(maximum, 3)
        self.assertEqual(result[0].source_start, 1)
        self.assertEqual(result[-1].source_end, 12)
        self.assertEqual(
            [item.source_start for item in result],
            sorted(item.source_start for item in result),
        )


class GraphPhaseConcurrencyTests(unittest.TestCase):
    def test_phase_runner_preserves_order_and_caps_workers(self) -> None:
        librarian = object.__new__(Librarian)
        librarian._ingest_concurrency = 3
        librarian.gateway = SimpleNamespace(
            settings=SimpleNamespace(ingest_concurrency=3)
        )
        active = 0
        maximum = 0
        lock = __import__("threading").Lock()

        def work(value: int) -> int:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.005)
                return value * 2
            finally:
                with lock:
                    active -= 1

        self.assertEqual(
            librarian._run_ingest_phase(work, list(range(8))),
            [i * 2 for i in range(8)],
        )
        self.assertLessEqual(maximum, 3)


class IngestSettingsTests(unittest.TestCase):
    def test_concurrency_and_recluster_settings_read_from_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"WIKI_INGEST_CONCURRENCY": "7", "WIKI_RECLUSTER_EVERY": "0"},
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.ingest_concurrency, 7)
        self.assertEqual(settings.recluster_every, 0)


if __name__ == "__main__":
    unittest.main()
