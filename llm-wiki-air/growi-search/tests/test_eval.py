"""Pure WP-15 evaluation comparison tests."""

import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from eval_compare import (compare_pair, judge_pair, load_run_dir, parse_judge,
                          percentile, summarize)
from eval_run import make_record


class RecallTests(unittest.TestCase):
    def test_seed_and_document_recall(self):
        teacher = {
            "id": "q1", "seeds": [
                {"id": "a", "p": 0.9, "document": "/team/A"},
                {"id": "b", "p": 0.79, "document": "/team/A"},
                {"id": "c", "p": 0.85, "document": "/team/B"},
            ],
            "cited_ids": ["a", "c"],
        }
        candidate = {
            "id": "q1", "seeds": [
                {"id": "a", "p": 0.95, "document": "/team/A"},
                {"id": "x", "p": 0.99, "document": "/team/C"},
            ],
            "cited_ids": ["a", "x"], "routed_documents": ["/team/A"],
        }
        result = compare_pair(teacher, candidate, seed_threshold=0.8)
        self.assertEqual(result["seed_recall"], 0.5)
        self.assertEqual(result["document_recall"], 0.5)
        self.assertEqual(result["citation_overlap"], 1 / 3)

    def test_route_recall_counts_rescues(self):
        teacher = {"id": "q1", "seeds": [
            {"id": "a", "p": 0.9, "document": "/team/A"},
            {"id": "b", "p": 0.9, "document": "/team/B"},
        ]}
        candidate = {"id": "q1", "seeds": [], "routed_documents": ["/team/A"],
                     "rescued_documents": ["/team/B"]}
        self.assertEqual(compare_pair(teacher, candidate)["route_recall"], 1.0)

    def test_route_recall_counts_each_seed_sharing_a_document(self):
        teacher = {"id": "q1", "seeds": [
            {"id": "a", "p": 0.9, "document": "/team/A"},
            {"id": "b", "p": 0.9, "document": "/team/A"},
            {"id": "c", "p": 0.9, "document": "/team/B"},
        ]}
        candidate = {"id": "q1", "routed_documents": ["/team/A"]}
        self.assertEqual(compare_pair(teacher, candidate)["route_recall"], 2 / 3)

    def test_empty_teacher_seed_set_has_full_recall(self):
        row = compare_pair({"id": "q1", "seeds": []}, {"id": "q1", "seeds": []})
        self.assertEqual(row["seed_recall"], 1.0)
        self.assertEqual(row["document_recall"], 1.0)
        self.assertEqual(row["route_recall"], 1.0)


class TimingTests(unittest.TestCase):
    def test_percentiles(self):
        values = [1, 2, 3, 4, 5]
        self.assertEqual(percentile(values, 0.5), 3)
        self.assertAlmostEqual(percentile(values, 0.95), 4.8)
        self.assertIsNone(percentile([], 0.5))

    def test_summarize_stage_and_total_timings(self):
        records = [{"id": str(i), "seeds": [], "cited_ids": [],
                    "timings": {"total_ms": total, "stage_ms": {"sweep": sweep}}}
                   for i, (total, sweep) in enumerate(((10, 4), (20, 6), (30, 8)))]
        summary = summarize(records, records)
        self.assertEqual(summary["timing_ms"]["total_p50"], 20)
        self.assertEqual(summary["stage_timing_ms"]["sweep"]["p50"], 6)

    def test_summarize_rejects_mismatched_ids(self):
        with self.assertRaisesRegex(ValueError, "ids differ"):
            summarize([{"id": "teacher"}], [{"id": "candidate"}])

    def test_load_run_dir_skips_summary_json(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "q1.json").write_text(json.dumps({"id": "q1"}), encoding="utf-8")
            (root / "summary.json").write_text(json.dumps({"questions": 1}), encoding="utf-8")
            self.assertEqual([row["id"] for row in load_run_dir(root)], ["q1"])


class JudgeTests(unittest.TestCase):
    def test_judge_parsing(self):
        self.assertEqual(parse_judge("A", True), "better")
        self.assertEqual(parse_judge("B", True), "worse")
        self.assertEqual(parse_judge("同等", False), "same")
        self.assertEqual(parse_judge("garbage", True), "invalid")

    def test_pair_randomizes_order_and_maps_candidate_verdict(self):
        class FakeLLM:
            def __init__(self, response):
                self.response = response
                self.payload = None

            def complete(self, _system, payload):
                self.payload = payload
                return self.response

        teacher = {"id": "q", "question": "Q", "answer": "teacher", "cited_ids": ["t"]}
        candidate = {"id": "q", "answer": "candidate", "cited_ids": ["c"]}
        llm = FakeLLM("A")
        self.assertEqual(judge_pair(llm, teacher, candidate, lambda: True), "better")
        self.assertIn('"answer": "candidate"', llm.payload)
        self.assertEqual(judge_pair(llm, teacher, candidate, lambda: False), "worse")


class RunRecordTests(unittest.TestCase):
    def test_collects_confirmed_seeds_routes_timings_and_event_counts(self):
        question = {"id": "q1", "question": "Q", "kind": "fact"}

        class Answer:
            answer = "answer"
            cited_node_ids = ["page1"]

        events = [
            {"type": "jev_gate", "stage": "full", "status": "confirmed",
             "node": {"id": "page1"}, "document": "/team/doc", "probability": .9},
            {"type": "route", "node": "/team/doc/00-目次", "kept": True},
            {"type": "timings", "stage_ms": {"sweep": 5}, "counts": {}, "total_ms": 8},
        ]
        record = make_record(question, "cascade", Answer(), events)
        self.assertEqual(record["seeds"], [{"id": "page1", "p": .9, "document": "/team/doc"}])
        self.assertEqual(record["routed_documents"], ["/team/doc"])
        self.assertEqual(record["timings"]["total_ms"], 8)
        self.assertEqual(record["events_count"], {"jev_gate": 1, "route": 1, "timings": 1})


if __name__ == "__main__":
    unittest.main()
