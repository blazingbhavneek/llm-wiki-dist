"""llm-wiki native ingestion, persistent graph store, and agent queries."""

from __future__ import annotations

import json
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


NAME = "llm_wiki"


def _valid_batch_result(value: Any, document_ids: Sequence[str]) -> bool:
    if not isinstance(value, dict):
        return False
    results = value.get("document_results")
    if not isinstance(results, list):
        return False
    by_id = {item.get("document"): item for item in results if isinstance(item, dict)}
    return all(
        doc_id in by_id and int(by_id[doc_id].get("ingested") or 0) > 0
        for doc_id in document_ids
    )


def _checkpoint_batch_results(
    workspace: Path,
    documents: Sequence[legacy.Document],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Split one batched worker result back into per-document checkpoints.

    One worker call now ingests a whole batch at once (so the LLM calls for
    different documents in the batch run concurrently), which means
    elapsed_seconds/token_usage on the result are batch totals rather than
    per-document ones. Crediting the full total to every document in the
    batch would multiply the reported cost by the batch size, so it is
    attributed to the first document only; the rest checkpoint with zero.
    Summing across all checkpoints still recovers the true total.
    """
    by_id = {
        item.get("document"): item
        for item in (result.get("document_results") or [])
        if isinstance(item, dict)
    }
    details: list[dict[str, Any]] = []
    for position, document in enumerate(documents):
        detail = {
            "document": document.id,
            "characters": len(document.text),
            "elapsed_seconds": (
                float(result.get("elapsed_seconds") or 0.0) if position == 0 else 0.0
            ),
            "token_usage": (result.get("token_usage") or {}) if position == 0 else {},
            "document_results": [by_id[document.id]] if document.id in by_id else [],
        }
        legacy.json_dump(
            workspace / "documents" / f"{legacy.safe_name(document.id)}.json",
            detail,
        )
        details.append(detail)
    return details


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
        if (workspace / "wiki.sqlite").is_file():
            return complete
        raise legacy.BenchmarkError(
            f"llm-wiki completion metadata exists but its database is missing: {workspace}"
        )
    state = assert_compatible_state(
        workspace,
        name=NAME,
        dataset_fingerprint=dataset_fingerprint,
        config_fingerprint=config_fingerprint,
    )
    workspace.mkdir(parents=True, exist_ok=True)
    mapping = {str(item["id"]): item for item in document_mapping}
    database = workspace / "wiki.sqlite"
    details: list[dict[str, Any]] = []
    started = time.perf_counter()

    # Recover the very narrow commit-to-checkpoint window from the worker's
    # atomic result file before issuing another ingestion request. Older
    # single-document state files wrote "current_document"; batched runs
    # write "current_documents".
    current_ids = [str(v) for v in (state.get("current_documents") or [])]
    if not current_ids and state.get("current_document"):
        current_ids = [str(state["current_document"])]
    result_path = workspace / "ingest-result.json"
    if current_ids and result_path.is_file():
        try:
            recovered = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            recovered = None
        by_id = {item.id: item for item in bundle.documents}
        recovered_documents = [
            by_id[doc_id]
            for doc_id in current_ids
            if doc_id in by_id
            and not (
                workspace / "documents" / f"{legacy.safe_name(doc_id)}.json"
            ).is_file()
        ]
        if recovered_documents and _valid_batch_result(
            recovered, [doc.id for doc in recovered_documents]
        ):
            _checkpoint_batch_results(workspace, recovered_documents, recovered)

    # Documents are grouped into batches of up to ingestion_concurrency and
    # handed to one worker subprocess call each, so the worker's own
    # concurrent prepare/link phases run across the whole batch instead of
    # one document ingesting fully before the next one starts -- the same
    # concurrency long documents already get across their internal chunks.
    batch_size = max(1, int(getattr(args, "ingestion_concurrency", 1)))
    pending_batch: list[legacy.Document] = []

    def flush_batch() -> None:
        nonlocal pending_batch
        if not pending_batch:
            return
        write_state(
            workspace,
            name=NAME,
            dataset_fingerprint=dataset_fingerprint,
            config_fingerprint=config_fingerprint,
            status="running",
            current_documents=[doc.id for doc in pending_batch],
            completed_documents=len(details),
            total_documents=len(bundle.documents),
        )
        result = legacy.run_ours_worker(
            "ingest",
            {
                "app_root": str(legacy.APP_ROOT.resolve()),
                "database": str(database.resolve()),
                "documents": [mapping[doc.id] for doc in pending_batch],
                "concurrency": args.ingestion_concurrency,
                "refresh_clusters": False,
            },
            workspace=workspace,
            args=args,
        )
        if not _valid_batch_result(result, [doc.id for doc in pending_batch]):
            raise legacy.BenchmarkError(
                "llm-wiki returned an invalid result for batch: "
                f"{[doc.id for doc in pending_batch]}"
            )
        details.extend(_checkpoint_batch_results(workspace, pending_batch, result))
        legacy.log(
            f"llm-wiki ingested {len(details)}/{len(bundle.documents)} documents "
            f"(batch of {len(pending_batch)})"
        )
        pending_batch = []

    for index, document in enumerate(bundle.documents, start=1):
        checkpoint = workspace / "documents" / f"{legacy.safe_name(document.id)}.json"
        if checkpoint.is_file():
            try:
                detail = json.loads(checkpoint.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                detail = None
            if (
                isinstance(detail, dict)
                and detail.get("document") == document.id
                and int(detail.get("characters") or -1) == len(document.text)
            ):
                details.append(detail)
                legacy.log(
                    f"llm-wiki resumed document {index}/{len(bundle.documents)}: "
                    f"{document.id}"
                )
                continue

        pending_batch.append(document)
        if len(pending_batch) >= batch_size:
            flush_batch()
    flush_batch()

    # Reclustering is intentionally one final checkpointed operation.  Running
    # it for every document would distort both ingestion time and token usage.
    finalize_checkpoint = workspace / "finalize.json"
    if (
        not finalize_checkpoint.is_file()
        and state.get("status") == "finalizing"
        and result_path.is_file()
    ):
        try:
            recovered_finalize = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            recovered_finalize = None
        if (
            isinstance(recovered_finalize, dict)
            and recovered_finalize.get("document_results") == []
            and isinstance(recovered_finalize.get("graph"), dict)
        ):
            legacy.json_dump(finalize_checkpoint, recovered_finalize)
    if not finalize_checkpoint.is_file():
        write_state(
            workspace,
            name=NAME,
            dataset_fingerprint=dataset_fingerprint,
            config_fingerprint=config_fingerprint,
            status="finalizing",
            completed_documents=len(details),
            total_documents=len(bundle.documents),
        )
        finalized = legacy.run_ours_worker(
            "ingest",
            {
                "app_root": str(legacy.APP_ROOT.resolve()),
                "database": str(database.resolve()),
                "documents": [],
                "concurrency": args.ingestion_concurrency,
                "finalize_only": True,
                "refresh_clusters": True,
            },
            workspace=workspace,
            args=args,
        )
        legacy.json_dump(finalize_checkpoint, finalized)
    else:
        finalized = json.loads(finalize_checkpoint.read_text(encoding="utf-8"))

    token_categories: dict[str, list[dict[str, Any]]] = {}
    for item in [*details, finalized]:
        categories = item.get("token_usage") if isinstance(item, dict) else None
        if not isinstance(categories, dict):
            continue
        for category, usage in categories.items():
            if isinstance(usage, dict):
                token_categories.setdefault(str(category), []).append(usage)
    database_bytes = sum(
        item.stat().st_size
        for item in workspace.glob(f"{database.name}*")
        if item.is_file()
    )
    result = {
        "client": NAME,
        "system": "ours",
        "status": "complete",
        "dataset_fingerprint": dataset_fingerprint,
        "config_fingerprint": config_fingerprint,
        "elapsed_seconds": sum(
            float(item.get("elapsed_seconds") or 0.0) for item in details
        )
        + float(finalized.get("elapsed_seconds") or 0.0),
        "process_elapsed_seconds": time.perf_counter() - started,
        "documents": len(bundle.documents),
        "characters": sum(len(item.text) for item in bundle.documents),
        "index_bytes": database_bytes + legacy.directory_size(workspace / "sources"),
        "token_usage": {
            category: legacy.summed_usage(usages)
            for category, usages in token_categories.items()
        },
        "document_checkpoints": len(details),
    }
    legacy.json_dump(workspace / "ingestion.json", result)
    write_state(
        workspace,
        name=NAME,
        dataset_fingerprint=dataset_fingerprint,
        config_fingerprint=config_fingerprint,
        status="complete",
        completed_documents=len(details),
        total_documents=len(bundle.documents),
    )
    return result


def _query_fingerprint(args: SimpleNamespace) -> dict[str, Any]:
    """Everything that changes what an answer would be, not just how fast."""
    return {
        name: getattr(args, name, None)
        for name in (
            "chat_model",
            "embed_model",
            "rerank_model",
            "agent_max_steps",
            "subagent_max_steps",
            "subagent_count",
            "subagent_concurrency",
            "rerank_top_k",
            "search_candidate_pool",
            "temperature",
            "max_answer_tokens",
        )
    }


def query(
    workspace: Path,
    questions: Sequence[legacy.Question],
    args: SimpleNamespace,
    on_result: ResultCallback | None = None,
) -> list[dict[str, Any]]:
    pending_by_id = {item.id: item for item in questions}
    results: dict[str, dict[str, Any]] = {}
    batch_path = workspace / "answer-batch.jsonl"

    # This journal lives in the shared datastore, so it outlives any one bench
    # run. Recovering an answer produced by a different agent configuration
    # would silently report the old settings as the new ones, so a changed
    # fingerprint retires the journal instead of resuming from it.
    fingerprint_path = workspace / "answer-config.json"
    fingerprint = _query_fingerprint(args)
    try:
        previous = json.loads(fingerprint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = None
    if previous != fingerprint:
        if batch_path.exists():
            legacy.log(
                "llm-wiki answer settings changed; discarding cached answers"
            )
            batch_path.unlink()
        legacy.json_dump(fingerprint_path, fingerprint)

    # The native worker atomically rewrites this file after every completed
    # question.  Recover those rows if the parent or worker was interrupted.
    for row in legacy.jsonl_load(batch_path):
        question_id = str(row.get("id") or "")
        if question_id in pending_by_id and not row.get("error"):
            results[question_id] = row
            if on_result is not None:
                on_result(row)
    remaining = [item for item in questions if item.id not in results]
    if remaining:
        try:
            generated = legacy.ours_answer(workspace, remaining, args)
        except Exception:
            for row in legacy.jsonl_load(batch_path):
                question_id = str(row.get("id") or "")
                if question_id in pending_by_id and question_id not in results:
                    results[question_id] = row
                    if on_result is not None:
                        on_result(row)
            raise
        for row in generated:
            question_id = str(row.get("id") or "")
            results[question_id] = row
            if on_result is not None:
                on_result(row)
    return [results[item.id] for item in questions if item.id in results]
