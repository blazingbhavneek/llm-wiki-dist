"""Microsoft GraphRAG CLI standard ingest and native DRIFT queries."""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import benchmark as legacy

from .base import (
    ResultCallback,
    assert_compatible_state,
    client_config_fingerprint,
    load_complete_ingestion,
    write_state,
)


NAME = "graphrag"


def _new_log_text(workspace: Path, offsets: dict[Path, int]) -> str:
    parts: list[str] = []
    log_dir = workspace / "logs"
    if not log_dir.exists():
        return ""
    for path in sorted(log_dir.rglob("*.log")):
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            handle.seek(offsets.get(path, 0))
            parts.append(handle.read().decode("utf-8", errors="replace"))
    return "\n".join(parts)


def _ensure_workspace(
    workspace: Path,
    document_mapping: Sequence[dict[str, str]],
    args: SimpleNamespace,
) -> None:
    if not (workspace / "settings.yaml").is_file():
        legacy.prepare_graphrag_workspace(workspace, document_mapping, args)
        return
    input_dir = workspace / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    for record in document_mapping:
        target = input_dir / f"{legacy.safe_name(str(record['id']))}.txt"
        source = Path(record["path"])
        if not target.is_file() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)
    legacy.configure_generated_graphrag_settings(workspace / "settings.yaml", args)


def ingest(
    workspace: Path,
    bundle: legacy.DatasetBundle,
    document_mapping: Sequence[dict[str, str]],
    manifest: dict[str, Any],
    args: SimpleNamespace,
) -> dict[str, Any]:
    dataset_fingerprint = str(manifest["dataset_fingerprint"])
    config_fingerprint = client_config_fingerprint(NAME, args)
    complete = load_complete_ingestion(
        workspace,
        name=NAME,
        dataset_fingerprint=dataset_fingerprint,
        config_fingerprint=config_fingerprint,
    )
    if complete is not None:
        output = workspace / "output"
        if output.is_dir() and any(item.is_file() for item in output.rglob("*")):
            return complete
        raise legacy.BenchmarkError(
            f"GraphRAG completion metadata exists but its output is missing: {workspace}"
        )
    state = assert_compatible_state(
        workspace,
        name=NAME,
        dataset_fingerprint=dataset_fingerprint,
        config_fingerprint=config_fingerprint,
    )
    workspace.mkdir(parents=True, exist_ok=True)
    _ensure_workspace(workspace, document_mapping, args)
    prior_usage = state.get("token_usage") if isinstance(state, dict) else {}
    started = time.perf_counter()
    write_state(
        workspace,
        name=NAME,
        dataset_fingerprint=dataset_fingerprint,
        config_fingerprint=config_fingerprint,
        status="running",
        resume_strategy="GraphRAG workflow cache",
        token_usage=prior_usage,
    )
    try:
        # Keeping GraphRAG's workflow cache is what makes an interrupted native
        # index restart at unfinished workflows instead of discarding all work.
        legacy.run_logged(
            legacy.graphrag_command(
                args,
                "index",
                "--root",
                str(workspace.resolve()),
                "--method",
                "standard",
            ),
            cwd=legacy.ROOT,
            env=legacy.subprocess_environment(args),
            log_path=workspace / "index.log",
            timeout=legacy.remaining_timeout(args),
            live_label="graphrag:index",
        )
    except BaseException:
        attempt_usage = legacy.parse_graphrag_token_metrics(
            _new_log_text(workspace, {}), args
        )
        write_state(
            workspace,
            name=NAME,
            dataset_fingerprint=dataset_fingerprint,
            config_fingerprint=config_fingerprint,
            status="running",
            resume_strategy="GraphRAG workflow cache",
            token_usage=attempt_usage,
        )
        raise

    token_usage = legacy.parse_graphrag_token_metrics(
        _new_log_text(workspace, {}), args
    )
    result = {
        "client": NAME,
        "system": NAME,
        "status": "complete",
        "dataset_fingerprint": dataset_fingerprint,
        "config_fingerprint": config_fingerprint,
        "elapsed_seconds": time.perf_counter() - started,
        "documents": len(bundle.documents),
        "characters": sum(len(item.text) for item in bundle.documents),
        "index_bytes": legacy.directory_size(workspace / "output"),
        "method": "standard",
        "resume_strategy": "GraphRAG workflow cache",
        "token_usage": token_usage,
        "token_accounting_complete": any(
            int(value.get("total_tokens") or 0) > 0 for value in token_usage.values()
        ),
    }
    legacy.json_dump(workspace / "ingestion.json", result)
    write_state(
        workspace,
        name=NAME,
        dataset_fingerprint=dataset_fingerprint,
        config_fingerprint=config_fingerprint,
        status="complete",
        resume_strategy="GraphRAG workflow cache",
        token_usage=token_usage,
    )
    return result


def query(
    workspace: Path,
    questions: Sequence[legacy.Question],
    args: SimpleNamespace,
    on_result: ResultCallback | None = None,
) -> list[dict[str, Any]]:
    legacy.ensure_graphrag_embedding_layout(workspace, args)
    style = legacy.detect_graphrag_query_style(args)
    # Each CLI gets a private root and journal, while every private proxy shares
    # this one cap. That lets questions run concurrently without either
    # interleaving their usage attribution or exceeding the provider budget.
    request_semaphore = threading.BoundedSemaphore(legacy.MAX_CONCURRENT_REQUESTS)

    def answer_one(question: legacy.Question) -> dict[str, Any]:
        result = question.result_base()
        started = time.perf_counter()
        try:
            with tempfile.TemporaryDirectory(
                prefix=f"graphrag-query-{legacy.safe_name(question.id)}-",
                dir=workspace,
            ) as temporary:
                query_root = Path(temporary)
                # Query uses the indexed output but requires a root-local
                # settings file. Isolating both settings and logs lets every
                # subprocess point at its own accounting proxy safely.
                (query_root / "output").symlink_to(
                    workspace / "output", target_is_directory=True
                )
                if (workspace / "input").exists():
                    (query_root / "input").symlink_to(
                        workspace / "input", target_is_directory=True
                    )
                if (workspace / "prompts").exists():
                    (query_root / "prompts").symlink_to(
                        workspace / "prompts", target_is_directory=True
                    )
                shutil.copy2(workspace / "settings.yaml", query_root / "settings.yaml")
                usage_path = query_root / "provider-usage.jsonl"
                with legacy.UsageRecordingProxy(
                    args.chat_base_url,
                    usage_path,
                    timeout=args.timeout,
                    embedding_upstream=args.embed_base_url,
                    request_semaphore=request_semaphore,
                ) as proxy:
                    legacy.point_graphrag_at(query_root, proxy.base_url)
                    completed = legacy.run_logged(
                        legacy.graphrag_query_command(
                            args,
                            query_root,
                            question.question,
                            style=style,
                        ),
                        cwd=legacy.ROOT,
                        env=legacy.subprocess_environment(args),
                        log_path=(
                            workspace
                            / "query-logs"
                            / f"{legacy.safe_name(question.id)}.log"
                        ),
                        timeout=legacy.remaining_timeout(args),
                    )
                    answer = legacy.parse_graphrag_answer(
                        completed.stdout.strip()
                        or "\n".join((completed.stdout, completed.stderr))
                    )
                    if not answer:
                        raise legacy.BenchmarkError(
                            "could not parse GraphRAG query response"
                        )
                    proxy.drain()
                    usage, _ = proxy.usage_since(0)
                    log_text = _new_log_text(query_root, {})
                    _measured, attempted = legacy.graphrag_metrics_coverage(
                        log_text, args
                    )
            counted = int(usage["retrieval_chat"].get("requests") or 0)
            result.update(
                {
                    "generated_answer": answer,
                    "retrieval_method": args.graphrag_method,
                    "token_usage": usage,
                    "chat_calls_measured": counted,
                    "chat_calls_attempted": attempted,
                    "token_accounting_complete": counted >= max(attempted, 1),
                    "error": None,
                }
            )
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["latency_seconds"] = time.perf_counter() - started
        return result

    results: list[dict[str, Any] | None] = [None] * len(questions)
    workers = min(args.graphrag_query_workers, len(questions))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(answer_one, question): index
            for index, question in enumerate(questions)
        }
        for completed_count, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results[futures[future]] = result
            if on_result is not None:
                on_result(result)
            legacy.log(f"graphrag answered {completed_count}/{len(questions)}")
    return [result for result in results if result is not None]
