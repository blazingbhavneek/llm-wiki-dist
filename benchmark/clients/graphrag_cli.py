"""Microsoft GraphRAG CLI standard ingest and native DRIFT queries."""

from __future__ import annotations

import shutil
import time
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


def _log_offsets(workspace: Path) -> dict[Path, int]:
    log_dir = workspace / "logs"
    if not log_dir.exists():
        return {}
    return {
        path: path.stat().st_size
        for path in log_dir.rglob("*.log")
        if path.is_file()
    }


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
    results: list[dict[str, Any]] = []
    # GraphRAG's own metrics miss every streamed call, which is most of them.
    # Routing its traffic through a recording proxy counts each request from
    # the provider's own usage payload instead.
    with legacy.UsageRecordingProxy(
        args.chat_base_url,
        workspace / "provider-usage.jsonl",
        timeout=args.timeout,
    ) as proxy:
        env = legacy.subprocess_environment(args)
        legacy.point_graphrag_chat_at(workspace, proxy.base_url)
        usage_offset = proxy.usage_since(0)[1]
        # One CLI process at a time is deliberate: all hidden GraphRAG requests
        # report into one query.log, and byte-range attribution is exact only
        # when query lifetimes do not overlap.
        for index, question in enumerate(questions, start=1):
            result = question.result_base()
            started = time.perf_counter()
            try:
                offsets = _log_offsets(workspace)
                completed = legacy.run_logged(
                    legacy.graphrag_query_command(
                        args,
                        workspace,
                        question.question,
                        style=style,
                    ),
                    cwd=legacy.ROOT,
                    env=env,
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
                # The CLI can exit while its last responses are still draining.
                proxy.drain()
                usage, usage_offset = proxy.usage_since(usage_offset)
                log_text = _new_log_text(workspace, offsets)
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
                        # The proxy sees every request the CLI actually issued,
                        # so its own count is the authority when GraphRAG's log
                        # reports fewer.
                        "token_accounting_complete": counted >= max(attempted, 1),
                        "error": None,
                    }
                )
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
                usage_offset = proxy.usage_since(usage_offset)[1]
            result["latency_seconds"] = time.perf_counter() - started
            results.append(result)
            if on_result is not None:
                on_result(result)
        legacy.log(f"graphrag answered {index}/{len(questions)}")
    return results
