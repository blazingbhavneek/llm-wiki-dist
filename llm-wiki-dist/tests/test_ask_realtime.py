"""End-to-end wiring: real store, real hybrid retrieval, fake generation.

Everything except the model calls is the production path — the SQLite store,
`search_with_evidence`, the vocabulary sheet, the neighbourhood walk and the
staged pipeline — so this catches wiring mistakes that a pipeline unit test
with injected doubles cannot.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from graph import researcher as researcher_module
from graph.core import Edge, Node, Settings
from graph.realtime import ShallowResearchAnswer
from graph.researcher import ResearchSession, Researcher, _profile_updates
from graph.store import GraphStore


class FakeEmbedder:
    dim = 8
    model_name = "fake-embedder"

    def embed_query(self, _text: str):
        raise RuntimeError("no embedding server in tests")

    def embed_document(self, _text: str):
        raise RuntimeError("no embedding server in tests")


class FakeGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.embedder = FakeEmbedder()
        self.reranker = None
        self.llm = None


class FakeLlmClient:
    """Stands in for the chat client the pipeline builds per worker thread."""

    prompts: list[str] = []

    def __init__(self, **_kwargs) -> None:
        pass

    def complete_structured(self, _system: str, user: str, _model):
        FakeLlmClient.prompts.append(user)
        import re

        found = re.findall(r"node_id: (\S+)", user)
        if not found:
            return ShallowResearchAnswer()
        return ShallowResearchAnswer(
            answer=f"Register pmf_prg.txt. ({found[0]})", node_ids=[found[0]]
        )


class AskRealtimeTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeLlmClient.prompts = []
        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "graph.sqlite"
        self.write_store = GraphStore(path)

        pages = [
            (
                "node:open",
                "mpf_mfs_open",
                "mpf_mfs_open はファイルを開きます。第3引数は `filenum` です。",
            ),
            (
                "node:prg",
                "pmf_prg.txt の登録",
                "処理要求には pmf_prg.txt の登録が必要です。",
            ),
            (
                "node:procdata",
                "pmf_procdata.txt の登録",
                "続いて pmf_procdata.txt も登録します。",
            ),
        ]
        for index, (node_id, title, body) in enumerate(pages):
            self.write_store.upsert_node(
                Node(
                    id=node_id,
                    body=body,
                    title=title,
                    summary=title,
                    keywords=["mpf_mfs_open", "登録"],
                    source_path="docs/mfs.md",
                    original_document_name="mfs.md",
                    source_ranges=[(index * 100, index * 100 + 80)],
                )
            )

        # The continuation of the same page. It shares no vocabulary with the
        # question, so retrieval alone can never reach it: only the chain can.
        self.write_store.upsert_node(
            Node(
                id="node:cyclic",
                body="その後、周期実行を開始します。",
                title="周期実行の開始",
                summary="周期実行の開始",
                source_path="docs/mfs.md",
                original_document_name="mfs.md",
                source_ranges=[(300, 380)],
            )
        )
        for index, (left, right) in enumerate(
            [("node:prg", "node:procdata"), ("node:procdata", "node:cyclic")]
        ):
            self.write_store.upsert_edge(
                Edge(
                    id=f"follows:{index}",
                    source_node_id=left,
                    target_node_id=right,
                    label="follows",
                )
            )

        self.store = GraphStore(path, readonly=True)
        self.researcher = Researcher(FakeGateway(Settings()), self.store)

    def tearDown(self) -> None:
        self.researcher.close()
        self.store.close()
        self.write_store.close()
        self._dir.cleanup()

    def _run(self, question: str, **options) -> list[dict]:
        events: list[dict] = []
        with mock.patch.object(researcher_module, "OpenAiLlmClient", FakeLlmClient):
            asyncio.run(
                self.researcher.ask_realtime(
                    question,
                    on_event=events.append,
                    options={"max_levels": 1, **options},
                )
            )
        return events

    def test_a_spoken_question_reaches_a_grounded_answer(self):
        events = self._run("エムピーエフ エムエフエス オープン について")

        plan = events[0]
        level = next(event for event in events if event["type"] == "level")
        done = next(event for event in events if event["type"] == "done")

        # The vocabulary sheet was built from the real corpus and repaired the
        # transcription before retrieval ran.
        self.assertEqual(plan["pinned_identifier"], "mpf_mfs_open")
        self.assertIn("mpf_mfs_open", plan["search_query"])
        self.assertIn("node:open", [c["node_id"] for c in plan["candidates"]])
        self.assertTrue(level["text"])
        self.assertTrue(set(level["reference_node_ids"]) <= {"node:open", "node:prg", "node:procdata"})
        self.assertEqual(done["status"], "complete")

    def test_the_retrieval_clamp_is_gone(self):
        # The realtime path used to cut the evidence pool to 40 snippets and 2
        # per node, which throttled exactly the questions it exists for.
        captured: dict[str, Settings] = {}
        original = ResearchSession.search_with_evidence

        def spy(self, text, limit=None):
            captured["settings"] = self.settings
            return original(self, text, limit)

        with mock.patch.object(ResearchSession, "search_with_evidence", spy):
            self._run("必要なファイルは")

        settings = captured["settings"]
        self.assertEqual(settings.evidence_rerank_pool, Settings().evidence_rerank_pool)
        self.assertEqual(
            settings.evidence_max_per_node, Settings().evidence_max_per_node
        )

    def test_realtime_subagent_budgets_replace_the_documentation_ones(self):
        captured: dict[str, Settings] = {}
        original = ResearchSession.search_with_evidence

        def spy(self, text, limit=None):
            captured["settings"] = self.settings
            return original(self, text, limit)

        with mock.patch.object(ResearchSession, "search_with_evidence", spy):
            self._run("mpf_mfs_open とは")

        settings = captured["settings"]
        self.assertEqual(settings.subagent_max_steps, 5)
        self.assertEqual(settings.subagent_min_reads, 1)
        self.assertEqual(settings.subagent_max_reads, 4)

    def test_the_deep_stage_reads_the_next_chunk_of_the_same_page(self):
        reports: list[str] = []

        def fake_subagent(
            self, start_id, sibling_ids, question, index, emit,
            stop_event=None, extra_instructions="",
        ):
            reports.append(start_id)
            return {"start": start_id, "answer": "続きの手順を確認しました。", "cited": [start_id]}

        with mock.patch.object(ResearchSession, "_run_single_subagent", fake_subagent):
            events = self._run("mpf_mfs_open について", max_levels=2, subagent_count=1)

        levels = [event for event in events if event["type"] == "level"]
        self.assertEqual(len(levels), 2)
        # node:cyclic shares no words with the question; the document chain is
        # the only thing that could have reached it.
        self.assertEqual(reports, ["node:cyclic"])
        self.assertNotIn("node:cyclic", levels[0]["reference_node_ids"])
        self.assertIn("node:cyclic", levels[1]["reference_node_ids"])

    def test_the_vocabulary_sheet_is_cached_until_the_corpus_changes(self):
        first = self.researcher.vocabulary()
        self.assertIs(self.researcher.vocabulary(), first)

        self.write_store.upsert_node(
            Node(id="node:new", body="`newflag` を設定します。", title="newflag")
        )
        rebuilt = self.researcher.vocabulary(force=True)
        self.assertIsNot(rebuilt, first)
        self.assertTrue(rebuilt.knows("newflag"))


class ProfileTests(unittest.TestCase):
    def test_multipliers_scale_the_configured_weights(self):
        settings = Settings()
        updates = _profile_updates(
            settings, {"weight_node_bm25": 1.6, "pool_item_bm25": 1.5}
        )

        self.assertAlmostEqual(updates["weight_node_bm25"], settings.weight_node_bm25 * 1.6)
        self.assertEqual(updates["pool_item_bm25"], round(settings.pool_item_bm25 * 1.5))

    def test_unknown_or_non_numeric_settings_are_skipped(self):
        updates = _profile_updates(Settings(), {"entity_dedup": 2.0, "nonesuch": 1.5})
        self.assertEqual(updates, {})


if __name__ == "__main__":
    unittest.main()
