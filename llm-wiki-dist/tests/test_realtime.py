from __future__ import annotations

import threading
import time
import unittest
from dataclasses import dataclass

from graph.realtime import (
    LeveledPlan,
    PlannedLevel,
    RealtimeOptions,
    RealtimePipeline,
    ShallowResearchAnswer,
)


@dataclass
class FakeNode:
    id: str
    title: str
    summary: str
    body: str = ""


def result(node_id: str, text: str) -> dict:
    return {
        "node": FakeNode(node_id, node_id, text),
        "score": 1.0,
        "evidence": [{"field": "small_chunk", "text": text}],
    }


class ScriptedLlmFactory:
    def __init__(self, plan: LeveledPlan, answers: dict[str, list[ShallowResearchAnswer]]):
        self.plan = plan
        self.answers = answers
        self.answer_prompts: list[str] = []
        self.calls: list[type] = []
        self._lock = threading.Lock()

    def __call__(self):
        return self

    def complete_structured(self, _system, user, output_model):
        self.calls.append(output_model)
        if output_model is LeveledPlan:
            return self.plan
        self.assertIs(output_model, ShallowResearchAnswer)
        with self._lock:
            self.answer_prompts.append(user)
            for query, responses in self.answers.items():
                if f"Current shallow question:\n{query}\n" in user:
                    return responses.pop(0)
        raise AssertionError(f"No scripted answer for prompt: {user}")

    def assertIs(self, left, right):
        if left is not right:
            raise AssertionError(f"Expected {right}, got {left}")


class RealtimePipelineTests(unittest.TestCase):
    def test_level_workers_append_sections_and_seed_the_next_level(self):
        llms = ScriptedLlmFactory(
            LeveledPlan(
                levels=[
                    PlannedLevel(objective="Define X", queries=["What is X?", "What is its return value?"]),
                    PlannedLevel(objective="Use X", queries=["How is X used?"]),
                ]
            ),
            {
                "What is X?": [ShallowResearchAnswer(answer="## 概要\nX はコントローラーです。", node_ids=["node:x"])],
                "What is its return value?": [ShallowResearchAnswer(answer="## 戻り値\n成功時は 0 です。", node_ids=["node:r"])],
                "How is X used?": [ShallowResearchAnswer(answer="## 使用方法\nX を初期化して使います。", node_ids=["node:u"])],
            },
        )

        def search(query, _limit):
            return [result({"What is X?": "node:x", "What is its return value?": "node:r", "How is X used?": "node:u"}.get(query, "node:other"), query)]

        events: list[dict] = []
        summary = RealtimePipeline(llm_factory=llms, search=search).run(
            "Explain what X is, how its return value is used, and how callers should use it in detail.",
            emit=events.append,
            options=RealtimeOptions(max_levels=2, max_queries_per_level=2),
        )

        levels = [event for event in events if event["type"] == "level"]
        self.assertEqual(len(levels), 2)
        self.assertIn("## 概要", levels[0]["text"])
        self.assertIn("## 戻り値", levels[0]["text"])
        self.assertEqual(levels[0]["reference_node_ids"], ["node:x", "node:r"])
        third_prompt = next(p for p in llms.answer_prompts if "How is X used?" in p)
        self.assertIn("X はコントローラーです。", third_prompt)
        self.assertEqual(summary["status"], "complete")
        self.assertIsNone(summary["incomplete_reason"])

    def test_default_fast_path_uses_one_synthesis_call(self):
        llms = ScriptedLlmFactory(
            LeveledPlan(levels=[PlannedLevel(objective="Parameters", queries=["Explain parameters"])]),
            {
                "Explain parameters": [
                    ShallowResearchAnswer(answer="## 引数\n`opentype` はロック種別を指定します。", node_ids=["node:a"]),
                ]
            },
        )
        searches: list[str] = []

        def search(query, _limit):
            searches.append(query)
            return [result("node:b" if "opentype" in query else "node:a", query)]

        events: list[dict] = []
        RealtimePipeline(llm_factory=llms, search=search).run(
            "Explain parameters",
            emit=events.append,
            options=RealtimeOptions(),
        )

        level = next(event for event in events if event["type"] == "level")
        self.assertEqual(searches, ["Explain parameters"])
        self.assertEqual(level["text"], "## 引数\n`opentype` はロック種別を指定します。")
        self.assertEqual(level["reference_node_ids"], ["node:a"])
        self.assertEqual(len(llms.answer_prompts), 1)
        self.assertEqual(llms.calls, [ShallowResearchAnswer])

    def test_unretrieved_citations_are_not_streamed_and_do_not_make_run_partial(self):
        llms = ScriptedLlmFactory(
            LeveledPlan(levels=[PlannedLevel(objective="Answer", queries=["Question"])]),
            {"Question": [
                ShallowResearchAnswer(answer="Invented claim.", node_ids=["node:invented"]),
                ShallowResearchAnswer(answer="Invented claim.", node_ids=["node:invented"]),
            ]},
        )
        events: list[dict] = []
        summary = RealtimePipeline(
            llm_factory=llms,
            search=lambda _query, _limit: [result("node:real", "Real evidence")],
        ).run("Question", emit=events.append)

        level = next(event for event in events if event["type"] == "level")
        self.assertEqual(level["text"], "")
        self.assertEqual(level["facts"], [])
        self.assertTrue(level["complete"])
        self.assertEqual(summary["status"], "complete")

    def test_answer_without_citation_metadata_uses_top_retrieved_node(self):
        llms = ScriptedLlmFactory(
            LeveledPlan(levels=[PlannedLevel(objective="Answer", queries=["Question"])]),
            {"Question": [
                ShallowResearchAnswer(answer="Grounded answer."),
                ShallowResearchAnswer(answer="Grounded answer."),
            ]},
        )
        events: list[dict] = []
        RealtimePipeline(
            llm_factory=llms,
            search=lambda _query, _limit: [result("node:real", "Grounded evidence")],
        ).run("Question", emit=events.append)

        level = next(event for event in events if event["type"] == "level")
        self.assertEqual(level["text"], "Grounded answer.")
        self.assertEqual(level["reference_node_ids"], ["node:real"])

    def test_fast_path_keeps_lower_ranked_matches_as_compact_context(self):
        llms = ScriptedLlmFactory(
            LeveledPlan(levels=[]),
            {"Question": [ShallowResearchAnswer(answer="Grounded answer.", node_ids=["node:1"])]},
        )
        RealtimePipeline(
            llm_factory=llms,
            search=lambda _query, _limit: [
                result(f"node:{index}", f"Evidence {index}")
                for index in range(1, 18)
            ],
        ).run("Question", emit=lambda _event: None)

        prompt = llms.answer_prompts[0]
        self.assertIn("node_id: node:1", prompt)
        self.assertIn("Additional ranked matches:", prompt)
        self.assertIn("node_id: node:17", prompt)

    def test_evidence_budget_reads_later_concise_required_source(self):
        long_results = [
            {
                "node": FakeNode(
                    f"node:long:{index}",
                    f"Long {index}",
                    "overview",
                    "unrelated overview " * 4_000,
                ),
                "evidence": [{"text": "overview match"}],
            }
            for index in range(7)
        ]
        required = {
            "node": FakeNode(
                "node:required",
                "Processing-request registration",
                "required files",
                (
                    "For a processing request, register pmf_prg.txt and "
                    "pmf_procdata.txt, then register and create "
                    "mpf_mfs_cyclicfile.txt."
                ),
            ),
            "evidence": [{"text": "pmf_prg.txt pmf_procdata.txt mpf_mfs_cyclicfile.txt"}],
        }

        rendered = RealtimePipeline._format_evidence(
            [*long_results, required], 32_000
        )

        self.assertIn("node_id: node:required", rendered)
        self.assertIn("pmf_prg.txt", rendered)
        self.assertIn("pmf_procdata.txt", rendered)
        self.assertIn("mpf_mfs_cyclicfile.txt", rendered)

    def test_fast_path_does_not_run_follow_up_model_calls(self):
        llms = ScriptedLlmFactory(
            LeveledPlan(levels=[PlannedLevel(objective="Answer", queries=["Question"])]),
            {"Question": [
                ShallowResearchAnswer(answer="Final.", node_ids=["node:a"]),
            ]},
        )
        searches: list[str] = []

        def search(query, _limit):
            searches.append(query)
            return [result({"Question": "node:a"}[query], query)]

        events: list[dict] = []
        RealtimePipeline(llm_factory=llms, search=search).run(
            "Question", emit=events.append,
            options=RealtimeOptions(),
        )
        level = next(event for event in events if event["type"] == "level")
        self.assertEqual(searches, ["Question"])
        self.assertEqual(level["text"], "Final.")

    def test_single_query_plan_drops_disclaimers(self):
        llms = ScriptedLlmFactory(
            LeveledPlan(
                levels=[
                    PlannedLevel(
                        objective="第3引数",
                        queries=["mpf_mfs_open関数の第3引数は何ですか"],
                    )
                ]
            ),
            {
                "mpf_mfs_open関数の第3引数は何ですか": [
                    ShallowResearchAnswer(
                        answer=(
                            "### 第3引数\n"
                            "第3引数は `bufsize` です。\n\n"
                            "提供された資料には具体的な記載はありません。"
                        ),
                        node_ids=["node:open"],
                    ),
                    ShallowResearchAnswer(
                        answer="### 第3引数\n第3引数は `bufsize` です。",
                        node_ids=["node:open"],
                    ),
                ],
                (
                    "mpf_mfs_open関数の第3引数は何ですか "
                    "詳細・記述項目・設定パラメータ・関連条件"
                ): [
                    ShallowResearchAnswer(
                        answer="### 関連詳細\n`bufsize` の設定条件です。",
                        node_ids=["node:open"],
                    )
                ],
            },
        )
        events: list[dict] = []
        RealtimePipeline(
            llm_factory=llms,
            search=lambda _query, _limit: [result("node:open", "bufsize")],
        ).run(
            "mpf_mfs_open関数の第3引数は何ですか",
            emit=events.append,
            options=RealtimeOptions(max_levels=2),
        )

        plan = events[0]
        level = next(event for event in events if event["type"] == "level")
        self.assertTrue(plan["planning_fallback"] is False)
        self.assertEqual(len(plan["levels"]), 2)
        self.assertEqual(plan["levels"][0]["queries"], ["mpf_mfs_open関数の第3引数は何ですか"])
        self.assertEqual(
            plan["levels"][1]["queries"],
            [
                "mpf_mfs_open関数の第3引数は何ですか "
                "詳細・記述項目・設定パラメータ・関連条件"
            ],
        )
        self.assertIn("第3引数は `bufsize`", level["text"])
        self.assertNotIn("提供された資料", level["text"])

    def test_evidence_includes_node_body_even_when_match_snippets_exist(self):
        rendered = RealtimePipeline._format_evidence(
            [
                {
                    "node": FakeNode("node:open", "Open", "summary", "signature int open(..., int filenum, ...)"),
                    "evidence": [{"text": "nearby paragraph"}],
                }
            ],
            10_000,
        )
        self.assertIn("signature int open", rendered)
        self.assertIn("nearby paragraph", rendered)

    def test_deadline_still_reports_partial(self):
        llms = ScriptedLlmFactory(
            LeveledPlan(levels=[PlannedLevel(objective="First", queries=["First"]), PlannedLevel(objective="Second", queries=["Second"])]),
            {"First": [ShallowResearchAnswer(answer="First fact.", node_ids=["node:a"])]},
        )

        def slow_search(_query, _limit):
            time.sleep(0.15)
            return [result("node:a", "First fact")]

        events: list[dict] = []
        summary = RealtimePipeline(llm_factory=llms, search=slow_search).run(
            "Explain the first and second parts of this system in enough detail that the dependency order matters.",
            emit=events.append,
            options=RealtimeOptions(max_levels=2, deadline_seconds=0.1),
        )
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["incomplete_reason"], "deadline")


if __name__ == "__main__":
    unittest.main()
