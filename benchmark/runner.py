"""Two-phase benchmark CLI with stable, restartable dataset paths.

Defaults to the whole dataset; pass --sample N to build a separate, smaller
<dataset>-sampleN datastore capped at N questions and the corpus material
they reference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import benchmark as legacy

from .clients import NAMES as CLIENT_NAMES
from .clients import get_client
from .datasets import NAMES as DATASET_NAMES
from .datasets import get_dataset
from .datasets.common import load_canonical
from .reporting import generate_report


RESULTS_ROOT = legacy.ROOT / "benchmark-results"
# --debug trades statistical power for a turnaround short enough to check that
# all three clients still answer end to end. The seed is fixed so every client,
# dataset, and rerun sees the identical subset.
DEBUG_QUESTION_SAMPLE = 100
DEBUG_SAMPLE_SEED = 0


def datastore_path(dataset: str, *, sample: int | None = None) -> Path:
    # A --sample run gets its own datastore tree: it ingests a different,
    # smaller set of documents/questions than the full dataset, so sharing a
    # directory would either trip the manifest guard or silently shrink the
    # full corpus underneath already-completed work.
    name = f"{dataset}-sample{sample}" if sample else dataset
    return RESULTS_ROOT / "datastores" / name


def benchmark_path(
    dataset: str, *, debug: bool = False, sample: int | None = None
) -> Path:
    # A debug run keeps its own results tree: its accuracy is not comparable to
    # a full run, and sharing a directory would either trip the manifest guard
    # or quietly mix subset predictions into the real ones. Same reasoning for
    # a --sample run, which scores a different question set than the full one.
    name = f"{dataset}-sample{sample}" if sample else dataset
    if debug:
        name = f"{name}-debug"
    return RESULTS_ROOT / "benchmark" / name


def debug_questions(
    questions: Sequence[legacy.Question],
) -> list[legacy.Question]:
    """Draw the fixed debug subset, uniformly at random over question ids.

    Sampling ids rather than records keeps the choice stable if question
    fields ever change, and the result is returned in canonical order so
    predictions files stay diffable between runs.
    """

    if len(questions) <= DEBUG_QUESTION_SAMPLE:
        return list(questions)
    chosen = set(
        random.Random(DEBUG_SAMPLE_SEED).sample(
            sorted(question.id for question in questions),
            DEBUG_QUESTION_SAMPLE,
        )
    )
    return [question for question in questions if question.id in chosen]


def _clients(value: str) -> tuple[str, ...]:
    names = tuple(item.strip() for item in value.split(",") if item.strip())
    unknown = sorted(set(names) - set(CLIENT_NAMES))
    if not names or unknown:
        detail = f"unknown clients: {', '.join(unknown)}" if unknown else "no clients"
        raise argparse.ArgumentTypeError(detail)
    return names


def build_args(
    dataset: str,
    *,
    chat_base_url: str | None = None,
    debug: bool = False,
    sample: int | None = None,
) -> SimpleNamespace:
    url = chat_base_url or os.environ.get(
        "BENCH_CHAT_BASE_URL", legacy.DEFAULT_CHAT_BASE_URL
    )
    args = legacy.fixed_args(url, dataset)
    # Unlike the fixed 100-question presets `fixed_args` computes for the
    # legacy single-shot CLI, this two-phase pipeline defaults to the whole
    # dataset. `sample` is only set when the caller opts into a smaller,
    # permanent datastore via `--sample`.
    args.sample = sample
    args.resume = True
    args.debug = debug
    args.debug_seed = DEBUG_SAMPLE_SEED
    args.corpus_scope = "combined"
    args.output = str(benchmark_path(dataset, debug=debug, sample=sample))
    args.chat_api_key = os.environ.get("BENCH_CHAT_API_KEY", "local")
    args.embed_api_key = os.environ.get("BENCH_EMBED_API_KEY", "local")
    args.judge_api_key = os.environ.get("BENCH_JUDGE_API_KEY", args.chat_api_key)
    args.chat_model = os.environ.get("BENCH_CHAT_MODEL", args.chat_model)
    args.judge_model = os.environ.get("BENCH_JUDGE_MODEL", args.chat_model)
    args.embed_base_url = os.environ.get("BENCH_EMBED_BASE_URL", args.embed_base_url)
    args.embed_model = os.environ.get("BENCH_EMBED_MODEL", args.embed_model)
    args.rerank_base_url = os.environ.get(
        "BENCH_RERANK_BASE_URL", args.rerank_base_url
    )
    args.rerank_model = os.environ.get("BENCH_RERANK_MODEL", args.rerank_model)
    args.ours_python = os.environ.get("BENCH_LLM_WIKI_PYTHON", args.ours_python)
    args.agent_max_steps = int(os.environ.get("BENCH_AGENT_MAX_STEPS", "8"))
    args.subagent_max_steps = int(
        os.environ.get("BENCH_SUBAGENT_MAX_STEPS", "8")
    )
    args.subagent_count = int(
        os.environ.get("BENCH_SUBAGENT_COUNT", str(args.subagent_count))
    )
    args.subagent_concurrency = int(
        os.environ.get("BENCH_SUBAGENT_CONCURRENCY", str(args.subagent_concurrency))
    )
    args.rerank_top_k = int(
        os.environ.get("BENCH_RERANK_TOP_K", str(args.rerank_top_k))
    )
    args.search_candidate_pool = int(
        os.environ.get("BENCH_SEARCH_POOL", str(args.search_candidate_pool))
    )
    # One question worker holds subagent_concurrency requests open, so raising
    # the fan-out has to lower the workers or the global cap is fiction.
    args.ours_question_workers = max(
        1, legacy.MAX_CONCURRENT_REQUESTS // args.subagent_concurrency
    )
    args.query_token_budget = int(
        os.environ.get("BENCH_QUERY_TOKEN_BUDGET", "16000")
    )
    args.hybrid_candidate_k = int(
        os.environ.get("BENCH_HYBRID_CANDIDATE_K", str(args.top_k * 10))
    )
    # Each GraphRAG question gets a private query root and accounting proxy;
    # those proxies share a MAX_CONCURRENT_REQUESTS semaphore. This can safely
    # keep the GPU busy without exceeding the benchmark-wide request budget.
    args.graphrag_query_workers = max(
        1,
        int(
            os.environ.get(
                "BENCH_GRAPHRAG_QUERY_WORKERS",
                str(legacy.MAX_CONCURRENT_REQUESTS),
            )
        ),
    )
    return args


def _manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise legacy.BenchmarkError(f"invalid or missing manifest: {path}") from exc
    if not isinstance(value, dict):
        raise legacy.BenchmarkError(f"invalid manifest: {path}")
    return value


def _mapping(datastore: Path) -> list[dict[str, str]]:
    value = json.loads(
        (datastore / "canonical" / "documents.json").read_text(encoding="utf-8")
    )
    if not isinstance(value, list):
        raise legacy.BenchmarkError("canonical document mapping is invalid")
    return [dict(item) for item in value if isinstance(item, dict)]


def _runtime_names(clients: Sequence[str]) -> list[str]:
    return ["ours" if item == "llm_wiki" else item for item in clients]


def command_ingest(
    args: SimpleNamespace,
    clients: Sequence[str] = CLIENT_NAMES,
    *,
    preflight: bool = True,
) -> int:
    datastore = datastore_path(args.dataset, sample=getattr(args, "sample", None))
    dataset = get_dataset(args.dataset)
    bundle = dataset.prepare(datastore, args)
    manifest = _manifest(datastore / "manifest.json")
    mapping = _mapping(datastore)
    if preflight:
        legacy.validate_runtime(args, _runtime_names(clients))

    state_path = datastore / "ingestion-state.json"
    completed: dict[str, Any] = {}
    if state_path.is_file():
        with state_path.open(encoding="utf-8") as handle:
            existing = json.load(handle)
        if isinstance(existing, dict):
            completed = dict(existing.get("clients") or {})
    for name in clients:
        client = get_client(name)
        legacy.log(f"ingesting {args.dataset} with {name}")
        metrics = client.ingest(
            datastore / "clients" / name,
            bundle,
            mapping,
            manifest,
            args,
        )
        completed[name] = {
            "status": metrics.get("status"),
            "documents": metrics.get("documents"),
            "elapsed_seconds": metrics.get("elapsed_seconds"),
        }
        legacy.json_dump(
            state_path,
            {
                "dataset": args.dataset,
                "dataset_fingerprint": manifest["dataset_fingerprint"],
                "status": (
                    "complete"
                    if all(
                        completed.get(item, {}).get("status") == "complete"
                        for item in clients
                    )
                    else "running"
                ),
                "clients": completed,
            },
        )
    legacy.log(f"datastore complete: {datastore}")
    return 0


def _benchmark_config(args: SimpleNamespace, clients: Sequence[str]) -> dict[str, Any]:
    value = {
        "dataset": args.dataset,
        "clients": list(clients),
        "chat_base_url": args.chat_base_url,
        "chat_model": args.chat_model,
        "embed_base_url": args.embed_base_url,
        "embed_model": args.embed_model,
        "rerank_base_url": args.rerank_base_url,
        "rerank_model": args.rerank_model,
        "judge": args.judge,
        "agent_max_steps": args.agent_max_steps,
        "subagent_max_steps": args.subagent_max_steps,
        "subagent_count": args.subagent_count,
        "subagent_concurrency": args.subagent_concurrency,
        "rerank_top_k": args.rerank_top_k,
        "search_candidate_pool": args.search_candidate_pool,
        "query_token_budget": args.query_token_budget,
        "graphrag_method": args.graphrag_method,
        "graphrag_query_workers": args.graphrag_query_workers,
        "debug_sample": (
            {"questions": DEBUG_QUESTION_SAMPLE, "seed": DEBUG_SAMPLE_SEED}
            if getattr(args, "debug", False)
            else None
        ),
    }
    value["fingerprint"] = hashlib.sha256(
        json.dumps(value, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return value


def _require_ingestion(datastore: Path, clients: Sequence[str]) -> dict[str, Any]:
    manifest = _manifest(datastore / "manifest.json")
    missing = []
    for name in clients:
        path = datastore / "clients" / name / "ingestion.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {}
        if (
            value.get("status") != "complete"
            or value.get("dataset_fingerprint") != manifest.get("dataset_fingerprint")
        ):
            missing.append(name)
    if missing:
        raise legacy.BenchmarkError(
            "ingestion is incomplete for "
            + ", ".join(missing)
            + f"; run `python -m benchmark.runner ingest {manifest.get('dataset')}`"
        )
    return manifest


def command_bench(
    args: SimpleNamespace,
    clients: Sequence[str] = CLIENT_NAMES,
    *,
    preflight: bool = True,
) -> int:
    datastore = datastore_path(args.dataset, sample=getattr(args, "sample", None))
    dataset_manifest = _require_ingestion(datastore, clients)
    bundle = load_canonical(datastore)
    debug = bool(getattr(args, "debug", False))
    if debug:
        # The datastore stays whole: only the questions asked of it shrink, so
        # no ingest is repeated and the corpus each client searches is intact.
        questions = debug_questions(bundle.questions)
        legacy.log(
            f"debug mode: {len(questions)} of {len(bundle.questions)} questions "
            f"sampled with seed {DEBUG_SAMPLE_SEED}"
        )
        bundle = legacy.DatasetBundle(bundle.documents, questions, bundle.corpus_scope)
    if preflight:
        legacy.validate_runtime(args, _runtime_names(clients))
    output = benchmark_path(args.dataset, debug=debug)
    config = _benchmark_config(args, clients)
    manifest_path = output / "manifest.json"
    if manifest_path.is_file():
        existing = _manifest(manifest_path)
        if (
            existing.get("dataset_fingerprint")
            != dataset_manifest.get("dataset_fingerprint")
            or existing.get("benchmark_config") != config
        ):
            raise legacy.BenchmarkError(
                f"{output} contains results for different data or settings; "
                "remove it explicitly before starting a new benchmark"
            )
    else:
        if output.exists() and any(output.iterdir()):
            raise legacy.BenchmarkError(
                f"refusing to use non-empty result directory without a manifest: {output}"
            )
        legacy.json_dump(
            manifest_path,
            {
                "dataset": args.dataset,
                "dataset_fingerprint": dataset_manifest["dataset_fingerprint"],
                "questions": len(bundle.questions),
                "sampling": (
                    {
                        "mode": "debug",
                        "questions": DEBUG_QUESTION_SAMPLE,
                        "seed": DEBUG_SAMPLE_SEED,
                    }
                    if debug
                    else None
                ),
                "datastore": str(datastore.resolve()),
                "benchmark_config": config,
            },
        )

    benchmark_state: dict[str, Any] = {}
    state_path = output / "benchmark-state.json"
    if state_path.is_file():
        benchmark_state = json.loads(state_path.read_text(encoding="utf-8"))
    client_states = dict(benchmark_state.get("clients") or {})

    for name in clients:
        predictions_path = output / "clients" / name / "predictions.jsonl"
        existing = legacy.jsonl_load(predictions_path)
        merged = {str(row.get("id")): row for row in existing}
        lock = threading.Lock()

        def checkpoint(row: dict[str, Any]) -> None:
            question_id = str(row.get("id") or "")
            if not question_id:
                return
            with lock:
                merged[question_id] = row
                legacy.jsonl_dump(
                    predictions_path,
                    (
                        merged[item.id]
                        for item in bundle.questions
                        if item.id in merged
                    ),
                )

        pending = [
            item
            for item in bundle.questions
            if item.id not in merged or merged[item.id].get("error")
        ]
        if pending:
            legacy.log(f"benchmarking {name}: {len(pending)} pending questions")
            client = get_client(name)
            generated = client.query(
                datastore / "clients" / name,
                pending,
                args,
                checkpoint,
            )
            for row in generated:
                checkpoint(row)
        else:
            legacy.log(f"benchmarking {name}: all questions already complete")

        legacy.score_predictions(predictions_path, args)
        scored = legacy.jsonl_load(predictions_path)
        completed_ids = {
            str(row.get("id")) for row in scored if not row.get("error")
        }
        client_states[name] = {
            "status": (
                "complete"
                if all(item.id in completed_ids for item in bundle.questions)
                else "incomplete"
            ),
            "completed_questions": len(completed_ids),
            "total_questions": len(bundle.questions),
        }
        legacy.json_dump(
            state_path,
            {
                "dataset": args.dataset,
                "status": (
                    "complete"
                    if all(
                        client_states.get(item, {}).get("status") == "complete"
                        for item in clients
                    )
                    else "running"
                ),
                "clients": client_states,
            },
        )

    generate_report(output, datastore, bundle.questions, clients, args)
    incomplete = [
        name
        for name in clients
        if client_states.get(name, {}).get("status") != "complete"
    ]
    if incomplete:
        raise legacy.BenchmarkError(
            "benchmark has failed questions for "
            + ", ".join(incomplete)
            + "; rerun the same command to retry only those questions"
        )
    legacy.log(f"benchmark complete: {output}")
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        prog="python -m benchmark.runner",
        description=(
            "Ingest one stable full-dataset datastore, then benchmark all native agents."
        ),
    )
    subparsers = value.add_subparsers(dest="phase", required=True)
    for phase in ("ingest", "bench"):
        command = subparsers.add_parser(phase)
        command.add_argument("dataset", choices=DATASET_NAMES)
        command.add_argument(
            "--chat-base-url",
            default=None,
            help="OpenAI-compatible base URL (or BENCH_CHAT_BASE_URL)",
        )
        command.add_argument(
            "--clients",
            type=_clients,
            default=CLIENT_NAMES,
            help="comma-separated subset; defaults to vanilla,llm_wiki,graphrag",
        )
        command.add_argument(
            "--sample",
            type=int,
            default=None,
            help=(
                "cap the dataset at N questions, keeping only the corpus "
                "material those questions reference, in a separate "
                "<dataset>-sampleN datastore; omit for the full dataset. "
                "musique and multihop shrink with N; novel and fanout's "
                "corpus is mostly fixed size regardless (fanout's evidence "
                "pages are large full Wikipedia articles either way, and "
                "novel's 20-novel corpus doesn't depend on question count), "
                "so N mainly cuts their query cost, not ingest size"
            ),
        )
        if phase == "bench":
            command.add_argument(
                "--debug",
                action="store_true",
                help=(
                    f"answer a fixed random {DEBUG_QUESTION_SAMPLE}-question "
                    f"sample (seed {DEBUG_SAMPLE_SEED}) and write to "
                    "<dataset>-debug; the ingested corpus is unchanged"
                ),
            )
    return value


def main(argv: Sequence[str] | None = None) -> int:
    try:
        options = parser().parse_args(argv)
        args = build_args(
            options.dataset,
            chat_base_url=options.chat_base_url,
            debug=getattr(options, "debug", False),
            sample=options.sample,
        )
        if options.phase == "ingest":
            return command_ingest(args, options.clients)
        return command_bench(args, options.clients)
    except KeyboardInterrupt:
        legacy.log("interrupted; rerun the same command to continue")
        return 130
    except legacy.BenchmarkError as exc:
        legacy.log(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
