"""Compare teacher and candidate evaluation run folders."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
from typing import Any, Callable

JUDGE_SYSTEM = """あなたは回答品質の比較者です。質問に対し、引用された事実だけを使ってより正確で十分に答えている方を選んでください。推測や引用にない事実を加えた回答は低く評価してください。出力は A、B、同等 のいずれか一つだけにしてください。"""


def _seed_map(record: dict[str, Any], threshold: float) -> dict[str, dict[str, Any]]:
    result = {}
    for seed in record.get("seeds", []):
        if isinstance(seed, dict) and float(seed.get("p", 0.0)) >= threshold and seed.get("id"):
            result[str(seed["id"])] = seed
    return result


def _recall(expected: set[str], actual: set[str]) -> float:
    return len(expected & actual) / len(expected) if expected else 1.0


def _docs(seeds: dict[str, dict[str, Any]]) -> set[str]:
    return {str(seed.get("document") or "").rstrip("/") for seed in seeds.values()
            if seed.get("document")}


def _route_docs(record: dict[str, Any]) -> set[str]:
    return {str(value).rstrip("/") for value in record.get("routed_documents", []) if value}


def _rescued_docs(record: dict[str, Any]) -> set[str]:
    return {str(value).rstrip("/") for value in record.get("rescued_documents", []) if value}


def citation_jaccard(left: list[str], right: list[str]) -> float:
    a, b = set(left), set(right)
    union = a | b
    return len(a & b) / len(union) if union else 1.0


def compare_pair(teacher: dict[str, Any], candidate: dict[str, Any],
                 seed_threshold: float = 0.8) -> dict[str, Any]:
    expected = _seed_map(teacher, seed_threshold)
    actual = _seed_map(candidate, seed_threshold)
    expected_docs = _docs(expected)
    actual_docs = _docs(actual)
    route_docs = _route_docs(candidate) | _rescued_docs(candidate)
    routed_seed_count = sum(seed.get("document", "").rstrip("/") in route_docs
                            for seed in expected.values())
    return {
        "id": teacher.get("id", candidate.get("id", "")),
        "seed_recall": _recall(set(expected), set(actual)),
        "document_recall": _recall(expected_docs, actual_docs),
        "route_recall": routed_seed_count / len(expected) if expected else 1.0,
        "citation_overlap": citation_jaccard(
            list(teacher.get("cited_ids", [])), list(candidate.get("cited_ids", []))),
        "teacher_seeds": len(expected),
        "candidate_seeds": len(actual),
        "teacher_documents": len(expected_docs),
    }


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * min(1.0, max(0.0, p))
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def parse_judge(text: str, candidate_is_a: bool) -> str:
    answer = (text or "").strip().upper()
    if answer == "同等":
        return "same"
    if answer not in {"A", "B"}:
        return "invalid"
    candidate_won = (answer == "A") == candidate_is_a
    return "better" if candidate_won else "worse"


def summarize(teacher_records: list[dict[str, Any]], candidate_records: list[dict[str, Any]],
              seed_threshold: float = 0.8) -> dict[str, Any]:
    def index(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
        indexed = {}
        for row in rows:
            item_id = str(row.get("id") or "")
            if not item_id or item_id in indexed:
                raise ValueError(f"{label} run has a missing or duplicate question id: {item_id!r}")
            indexed[item_id] = row
        return indexed

    teacher_by_id = index(teacher_records, "teacher")
    candidate_by_id = index(candidate_records, "candidate")
    if teacher_by_id.keys() != candidate_by_id.keys():
        missing = sorted(teacher_by_id.keys() - candidate_by_id.keys())
        extra = sorted(candidate_by_id.keys() - teacher_by_id.keys())
        raise ValueError(f"run question ids differ (missing candidate: {missing}; extra candidate: {extra})")
    per_question = []
    for item_id in sorted(teacher_by_id):
        teacher = teacher_by_id[item_id]
        candidate = candidate_by_id[item_id]
        per_question.append(compare_pair(teacher, candidate, seed_threshold))

    metrics = {}
    for name in ("seed_recall", "document_recall", "route_recall", "citation_overlap"):
        values = [float(row[name]) for row in per_question]
        metrics[name] = {"mean": statistics.fmean(values) if values else None,
                         "count": len(values)}

    all_times = [float(row["timings"]["total_ms"]) for row in candidate_records
                 if isinstance(row.get("timings"), dict) and row["timings"].get("total_ms") is not None]
    stages: dict[str, list[float]] = {}
    for row in candidate_records:
        for name, value in (row.get("timings", {}).get("stage_ms", {}) or {}).items():
            stages.setdefault(name, []).append(float(value))
    return {
        "questions": len(per_question),
        "seed_threshold": seed_threshold,
        "metrics": metrics,
        "timing_ms": {"total_p50": percentile(all_times, .50),
                      "total_p95": percentile(all_times, .95), "count": len(all_times)},
        "stage_timing_ms": {name: {"p50": percentile(values, .50),
                                   "p95": percentile(values, .95), "count": len(values)}
                            for name, values in sorted(stages.items())},
        "per_question": per_question,
    }


def load_run_dir(directory: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(directory.glob("*.json")):
        if path.name == "summary.json":
            continue
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read evaluation result {path}: {exc}") from exc
        if not isinstance(row, dict) or not row.get("id"):
            raise ValueError(f"evaluation result has no id: {path}")
        rows.append(row)
    return rows


def judge_pair(llm: Any, teacher: dict[str, Any], candidate: dict[str, Any],
               choose_a: Callable[[], bool] = lambda: bool(random.getrandbits(1))) -> str:
    candidate_is_a = choose_a()
    a, b = (candidate, teacher) if candidate_is_a else (teacher, candidate)
    payload = {
        "question": teacher.get("question", candidate.get("question", "")),
        "A": {"answer": a.get("answer", ""), "cited_ids": a.get("cited_ids", [])},
        "B": {"answer": b.get("answer", ""), "cited_ids": b.get("cited_ids", [])},
    }
    return parse_judge(llm.complete(JUDGE_SYSTEM, json.dumps(payload, ensure_ascii=False)), candidate_is_a)


def _markdown(summary: dict[str, Any], judge: dict[str, Any] | None = None) -> str:
    lines = ["| Metric | Mean / value |", "| --- | ---: |"]
    for name, item in summary["metrics"].items():
        value = item["mean"]
        lines.append(f"| {name.replace('_', ' ')} | {value:.3f} |" if value is not None
                     else f"| {name.replace('_', ' ')} | n/a |")
    timing = summary["timing_ms"]
    lines.append(f"| total time p50 / p95 (ms) | {timing['total_p50'] if timing['total_p50'] is not None else 'n/a'} / {timing['total_p95'] if timing['total_p95'] is not None else 'n/a'} |")
    for name, item in summary["stage_timing_ms"].items():
        lines.append(f"| {name} p50 / p95 (ms) | {item['p50']:.1f} / {item['p95']:.1f} |")
    if judge is not None:
        lines.append(f"| answer judge (better / same / worse / invalid) | {judge['better']} / {judge['same']} / {judge['worse']} / {judge['invalid']} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--teacher", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--seed-threshold", type=float,
                        default=float(os.environ.get("WIKI_JEV_SEED_THRESHOLD", "0.8")))
    args = parser.parse_args(argv)
    try:
        teacher = load_run_dir(args.teacher)
        candidate = load_run_dir(args.candidate)
        summary = summarize(teacher, candidate, args.seed_threshold)
        judge_summary = None
        if args.judge:
            from config import Settings
            from gateway import LlmClient
            settings = Settings.from_env()
            settings.validate_strict()
            llm = LlmClient(settings.chat_model, settings.chat_base_url, settings.chat_api_key,
                            temperature=0, max_concurrency=settings.llm_max_concurrency)
            left = {str(row["id"]): row for row in teacher}
            right = {str(row["id"]): row for row in candidate}
            counts = {name: 0 for name in ("better", "same", "worse", "invalid")}
            for item_id in sorted(left.keys() & right.keys()):
                try:
                    verdict = judge_pair(llm, left[item_id], right[item_id])
                except Exception as exc:  # an invalid judgment is reported, not hidden
                    print(f"judge failed for {item_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
                    verdict = "invalid"
                counts[verdict] += 1
            judge_summary = {**counts, "count": sum(counts.values()),
                             "worse_rate": counts["worse"] / sum(counts.values()) if sum(counts.values()) else None}
            summary["answer_judge"] = judge_summary
        rendered = _markdown(summary, judge_summary)
        print(rendered)
        args.candidate.mkdir(parents=True, exist_ok=True)
        (args.candidate / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
