"""Stable benchmark reports with query and evaluation tokens kept separate."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

import benchmark as legacy


def _category_usage(
    row: dict[str, Any], *, include_judge: bool
) -> dict[str, int]:
    categories = row.get("token_usage")
    values = []
    if isinstance(categories, dict):
        values = [
            value
            for name, value in categories.items()
            if isinstance(value, dict) and (include_judge or name != "judge_chat")
        ]
    return legacy.summed_usage(values)


def _aggregate(
    rows: Iterable[dict[str, Any]], *, include_judge: bool
) -> dict[str, int | float]:
    values = [_category_usage(row, include_judge=include_judge) for row in rows]
    usage = legacy.summed_usage(values)
    count = len(values)
    return {
        "requests": usage["requests"],
        "input_tokens": usage["prompt_tokens"],
        "output_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "questions": count,
        "total_tokens_per_question": usage["total_tokens"] / count if count else 0.0,
    }


def _aggregate_judge(rows: Iterable[dict[str, Any]]) -> dict[str, int | float]:
    values = []
    for row in rows:
        categories = row.get("token_usage")
        judge = categories.get("judge_chat") if isinstance(categories, dict) else None
        values.append(legacy.summed_usage([judge] if isinstance(judge, dict) else []))
    usage = legacy.summed_usage(values)
    count = len(values)
    return {
        "requests": usage["requests"],
        "input_tokens": usage["prompt_tokens"],
        "output_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "questions": count,
        "total_tokens_per_question": usage["total_tokens"] / count if count else 0.0,
    }


def generate_report(
    output: Path,
    datastore: Path,
    questions: Sequence[legacy.Question],
    clients: Sequence[str],
    args: Any,
) -> dict[str, Any]:
    accuracy: dict[str, dict[str, Any]] = {}
    ingestion: dict[str, dict[str, Any]] = {}
    query_tokens: dict[str, dict[str, int | float]] = {}
    evaluation_tokens: dict[str, dict[str, int | float]] = {}
    accounting: dict[str, dict[str, Any]] = {}
    predictions_by_client: dict[str, list[dict[str, Any]]] = {}
    for name in clients:
        rows = legacy.jsonl_load(output / "clients" / name / "predictions.jsonl")
        predictions_by_client[name] = rows
        metrics = legacy.metric_summary(questions, rows, judge=args.judge)
        metrics.pop("token_usage", None)
        metrics.pop("_correct", None)
        accuracy[name] = metrics
        query_tokens[name] = _aggregate(rows, include_judge=False)
        evaluation_tokens[name] = _aggregate_judge(rows)
        ingestion_path = datastore / "clients" / name / "ingestion.json"
        ingestion[name] = json.loads(ingestion_path.read_text(encoding="utf-8"))
        incomplete_rows = [
            str(row.get("id") or "")
            for row in rows
            if row.get("token_accounting_complete") is False
        ]
        accounting[name] = {
            "complete": not incomplete_rows,
            "questions_without_provider_metrics": incomplete_rows,
        }

    comparison: dict[str, Any] = {}
    if "llm_wiki" in accuracy and "graphrag" in accuracy:
        comparison = {
            "llm_wiki_minus_graphrag_accuracy": (
                accuracy["llm_wiki"]["accuracy"]
                - accuracy["graphrag"]["accuracy"]
            ),
            "llm_wiki_minus_graphrag_tokens_per_question": (
                query_tokens["llm_wiki"]["total_tokens_per_question"]
                - query_tokens["graphrag"]["total_tokens_per_question"]
            ),
            "llm_wiki_token_ratio_vs_graphrag": (
                query_tokens["llm_wiki"]["total_tokens"]
                / query_tokens["graphrag"]["total_tokens"]
                if query_tokens["graphrag"]["total_tokens"]
                else None
            ),
        }

    summary = {
        "dataset": args.dataset,
        "questions": len(questions),
        "sampling": (
            {"mode": "debug", "seed": getattr(args, "debug_seed", None)}
            if getattr(args, "debug", False)
            else None
        ),
        "accuracy": accuracy,
        "query_token_usage": query_tokens,
        "evaluation_token_usage": evaluation_tokens,
        "token_accounting": accounting,
        "ingestion": ingestion,
        "comparison": comparison,
        "query_budget": {
            "target_tokens_per_question": args.query_token_budget,
            "vanilla_max_agent_steps": args.agent_max_steps,
            "llm_wiki_max_agent_steps": args.agent_max_steps,
            "llm_wiki_subagent_max_steps": args.subagent_max_steps,
            "graphrag_method": args.graphrag_method,
            "graphrag_drift_primer_folds": legacy.GRAPHRAG_DRIFT_PRIMER_FOLDS,
            "graphrag_drift_followups": legacy.GRAPHRAG_DRIFT_FOLLOWUPS,
            "graphrag_drift_depth": legacy.GRAPHRAG_DRIFT_DEPTH,
        },
    }
    legacy.json_dump(output / "summary.json", summary)
    _write_token_csv(output, clients, predictions_by_client)
    _write_markdown(output, summary, clients)
    return summary


def _write_token_csv(
    output: Path,
    clients: Sequence[str],
    predictions: dict[str, list[dict[str, Any]]],
) -> None:
    fields = [
        "client",
        "question_id",
        "source",
        "requests",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "agent_chat",
        "retrieval_chat",
        "retrieval_embedding",
        "judge_chat",
        "accounting_complete",
    ]
    path = output / "token-usage.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for name in clients:
            for row in predictions[name]:
                usage = _category_usage(row, include_judge=False)
                categories = row.get("token_usage")

                def total(category: str) -> int:
                    value = categories.get(category) if isinstance(categories, dict) else None
                    return int(value.get("total_tokens") or 0) if isinstance(value, dict) else 0

                writer.writerow(
                    {
                        "client": name,
                        "question_id": row.get("id"),
                        "source": row.get("source"),
                        "requests": usage["requests"],
                        "input_tokens": usage["prompt_tokens"],
                        "output_tokens": usage["completion_tokens"],
                        "total_tokens": usage["total_tokens"],
                        "agent_chat": total("agent_chat"),
                        "retrieval_chat": total("retrieval_chat"),
                        "retrieval_embedding": total("retrieval_embedding"),
                        "judge_chat": total("judge_chat"),
                        "accounting_complete": row.get(
                            "token_accounting_complete", True
                        ),
                    }
                )


def _scope_line(summary: dict[str, Any]) -> str:
    sampling = summary.get("sampling")
    if isinstance(sampling, dict):
        return (
            f"Debug sample: {summary['questions']:,} questions drawn at random "
            f"with seed {sampling.get('seed')}. Accuracy here is indicative "
            "only and is not comparable to a full run."
        )
    return f"Full dataset: {summary['questions']:,} questions. No sampling was used."


def _write_markdown(
    output: Path,
    summary: dict[str, Any],
    clients: Sequence[str],
) -> None:
    lines = [
        f"# Benchmark: {summary['dataset']}",
        "",
        _scope_line(summary),
        "",
        "## Query tokens",
        "",
        "Judge tokens are excluded from this table and reported separately in summary.json.",
        "",
        "| Client | Requests | Input | Output | Total | Total/question | Accounting |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for name in clients:
        usage = summary["query_token_usage"][name]
        complete = summary["token_accounting"][name]["complete"]
        lines.append(
            f"| {name} | {usage['requests']:,} | {usage['input_tokens']:,} | "
            f"{usage['output_tokens']:,} | {usage['total_tokens']:,} | "
            f"{usage['total_tokens_per_question']:,.1f} | "
            f"{'complete' if complete else 'missing provider metrics'} |"
        )
    lines.extend(
        [
            "",
            "## Answer quality",
            "",
            "| Client | Accuracy | Exact match | Token F1 | Failures | Median latency |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for name in clients:
        item = summary["accuracy"][name]
        latency = item["median_latency_seconds"]
        lines.append(
            f"| {name} | {item['accuracy']:.1%} | {item['exact_match']:.1%} | "
            f"{item['token_f1']:.3f} | {item['failures']} | "
            f"{legacy.format_duration(latency)} |"
        )
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
