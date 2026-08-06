from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass

from graph.realtime import (
    LeveledPlan,
    PlannedLevel,
    RealtimeOptions,
    RealtimePipeline,
    ReferencedFact,
    ShallowAnswer,
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
    def __init__(self, plan: LeveledPlan, answers: dict[str, ShallowAnswer]):
        self.plan = plan
        self.answers = answers
        self.answer_prompts: list[str] = []
        self._lock = threading.Lock()

    def __call__(self):
        return self

    def complete_structured(self, _system, user, output_model):
        if output_model is LeveledPlan:
            return self.plan
        with self._lock:
            self.answer_prompts.append(user)
        for query, answer in self.answers.items():
            marker = f"Current shallow question:\n{query}\n"
            if marker in user:
                return answer
        raise AssertionError(f"No scripted answer for prompt: {user}")


class RealtimePipelineTests(unittest.TestCase):
    def test_plan_precedes_levels_and_references_flow_forward(self):
        llms = ScriptedLlmFactory(
            LeveledPlan(
                levels=[
                    PlannedLevel(objective="Define X", queries=["What is X?"]),
                    PlannedLevel(
                        objective="Explain its behavior", queries=["Why does X do Y?"]
                    ),
                ]
            ),
            {
                "What is X?": ShallowAnswer(
                    facts=[ReferencedFact(text="X is a controller.", node_ids=["node:x"])],
                    enough=True,
                ),
                "Why does X do Y?": ShallowAnswer(
                    facts=[
                        ReferencedFact(
                            text="X does Y to limit retries.", node_ids=["node:x", "node:y"]
                        )
                    ],
                    enough=True,
                ),
            },
        )

        def search(query, _limit):
            if "Why does X do Y?" in query:
                return [result("node:y", "Y limits retries.")]
            return [result("node:x", "X is a controller.")]

        events: list[dict] = []
        pipeline = RealtimePipeline(llm_factory=llms, search=search)
        summary = pipeline.run(
            "What is X and why does it do Y?",
            emit=events.append,
            options=RealtimeOptions(min_search_results=1),
        )

        self.assertEqual(events[0]["type"], "plan")
        self.assertEqual(
            [level["queries"] for level in events[0]["levels"]],
            [["What is X?"], ["Why does X do Y?"]],
        )
        level_events = [event for event in events if event["type"] == "level"]
        self.assertEqual([event["level_id"] for event in level_events], ["level_1", "level_2"])
        self.assertEqual(level_events[0]["reference_node_ids"], ["node:x"])
        self.assertEqual(level_events[1]["reference_node_ids"], ["node:x", "node:y"])
        self.assertTrue(
            any("X is a controller. [node:x]" in prompt for prompt in llms.answer_prompts)
        )
        self.assertEqual(summary["status"], "complete")

    def test_insufficient_level_inserts_plan_update_before_recovery(self):
        llms = ScriptedLlmFactory(
            LeveledPlan(
                levels=[PlannedLevel(objective="Find policy", queries=["Find policy"])]
            ),
            {
                "Find policy": ShallowAnswer(
                    facts=[ReferencedFact(text="The policy has a retry rule.", node_ids=["node:a"])],
                    enough=False,
                    missing_queries=["Find the retry exception"],
                ),
                "Find the retry exception": ShallowAnswer(
                    facts=[
                        ReferencedFact(
                            text="The exception applies during maintenance.",
                            node_ids=["node:b"],
                        )
                    ],
                    enough=True,
                ),
            },
        )

        def search(query, _limit):
            if "retry exception" in query:
                return [result("node:b", "The exception applies during maintenance.")]
            return [result("node:a", "The policy has a retry rule.")]

        events: list[dict] = []
        RealtimePipeline(llm_factory=llms, search=search).run(
            "Explain the retry policy",
            emit=events.append,
            options=RealtimeOptions(min_search_results=1, max_recovery_levels=1),
        )

        update_index = next(i for i, event in enumerate(events) if event["type"] == "plan_update")
        recovery_start_index = next(
            i
            for i, event in enumerate(events)
            if event["type"] == "level_start" and event["level_id"] == "recovery_1"
        )
        self.assertLess(update_index, recovery_start_index)
        update = events[update_index]
        self.assertEqual([level["id"] for level in update["levels"]], ["level_1", "recovery_1"])
        recovery = next(
            event
            for event in events
            if event["type"] == "level" and event["level_id"] == "recovery_1"
        )
        self.assertEqual(recovery["reference_node_ids"], ["node:b"])

    def test_unretrieved_node_ids_and_their_facts_are_dropped(self):
        llms = ScriptedLlmFactory(
            LeveledPlan(levels=[PlannedLevel(objective="Answer", queries=["Question"])]),
            {
                "Question": ShallowAnswer(
                    facts=[
                        ReferencedFact(
                            text="This fact was invented.", node_ids=["node:invented"]
                        )
                    ],
                    enough=True,
                )
            },
        )
        events: list[dict] = []
        summary = RealtimePipeline(
            llm_factory=llms,
            search=lambda _query, _limit: [result("node:real", "Real evidence")],
        ).run(
            "Question",
            emit=events.append,
            options=RealtimeOptions(min_search_results=1, max_recovery_levels=0),
        )

        level = next(event for event in events if event["type"] == "level")
        self.assertEqual(level["facts"], [])
        self.assertEqual(level["reference_node_ids"], [])
        self.assertEqual(level["text"], "")
        self.assertEqual(summary["status"], "partial")


if __name__ == "__main__":
    unittest.main()
