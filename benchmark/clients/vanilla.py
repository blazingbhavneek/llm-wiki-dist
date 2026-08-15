"""Autonomous baseline over a fully embedded hybrid-search vector store."""

from __future__ import annotations

import collections
import heapq
import json
import math
import re
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


NAME = "vanilla"
_TERM_RE = re.compile(r"\w+", re.UNICODE)


def _part_rows(path: Path) -> list[dict[str, Any]]:
    try:
        return legacy.jsonl_load(path)
    except legacy.BenchmarkError:
        return []


def ingest(
    workspace: Path,
    bundle: legacy.DatasetBundle,
    document_mapping: Sequence[dict[str, str]],
    manifest: dict[str, Any],
    args: SimpleNamespace,
) -> dict[str, Any]:
    del document_mapping
    dataset_fingerprint = str(manifest["dataset_fingerprint"])
    config_fingerprint = client_config_fingerprint(NAME, args)
    complete = load_complete_ingestion(
        workspace,
        name=NAME,
        dataset_fingerprint=dataset_fingerprint,
        config_fingerprint=config_fingerprint,
    )
    if complete is not None and (workspace / "index.jsonl").is_file():
        return complete
    assert_compatible_state(
        workspace,
        name=NAME,
        dataset_fingerprint=dataset_fingerprint,
        config_fingerprint=config_fingerprint,
    )
    workspace.mkdir(parents=True, exist_ok=True)

    chunks: list[dict[str, Any]] = []
    chunk_unit = ""
    for document in bundle.documents:
        texts, chunk_unit = legacy.fixed_chunks(
            document.text,
            size=args.chunk_tokens,
            overlap=args.chunk_overlap,
        )
        chunks.extend(
            {
                "id": f"{legacy.safe_name(document.id)}-{index:06d}",
                "source": document.id,
                "text": text,
            }
            for index, text in enumerate(texts)
        )
    if not chunks:
        raise legacy.BenchmarkError("vanilla chunking produced no chunks")

    parts_dir = workspace / "index-parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    client = legacy.OpenAICompatibleClient(
        args.embed_base_url,
        args.embed_api_key,
        timeout=args.timeout,
    )
    started = time.perf_counter()
    completed_chunks = 0
    for start in range(0, len(chunks), args.embed_batch_size):
        batch = chunks[start : start + args.embed_batch_size]
        part = parts_dir / f"{start:09d}.jsonl"
        metrics = parts_dir / f"{start:09d}.usage.json"
        prior = _part_rows(part) if metrics.is_file() else []
        if len(prior) == len(batch) and [row.get("id") for row in prior] == [
            row["id"] for row in batch
        ]:
            completed_chunks += len(batch)
            continue
        vectors, usage = client.embeddings(
            [item["text"] for item in batch],
            args.embed_model,
        )
        rows = []
        for item, vector in zip(batch, vectors):
            if args.embed_dim and len(vector) != args.embed_dim:
                raise legacy.BenchmarkError(
                    f"embedding dimension is {len(vector)}, expected {args.embed_dim}"
                )
            rows.append({**item, "vector": legacy.normalize_vector(vector)})
        legacy.jsonl_dump(part, rows)
        legacy.json_dump(metrics, usage)
        completed_chunks += len(rows)
        write_state(
            workspace,
            name=NAME,
            dataset_fingerprint=dataset_fingerprint,
            config_fingerprint=config_fingerprint,
            status="running",
            completed_chunks=completed_chunks,
            total_chunks=len(chunks),
        )
        legacy.log(f"vanilla embedded {completed_chunks}/{len(chunks)}")

    rows: list[dict[str, Any]] = []
    usages: list[dict[str, Any]] = []
    for start in range(0, len(chunks), args.embed_batch_size):
        rows.extend(legacy.jsonl_load(parts_dir / f"{start:09d}.jsonl"))
        usages.append(
            json.loads(
                (parts_dir / f"{start:09d}.usage.json").read_text(encoding="utf-8")
            )
        )
    if len(rows) != len(chunks):
        raise legacy.BenchmarkError("vanilla index parts are incomplete")
    legacy.jsonl_dump(workspace / "index.jsonl", rows)
    result = {
        "client": NAME,
        "system": NAME,
        "status": "complete",
        "dataset_fingerprint": dataset_fingerprint,
        "config_fingerprint": config_fingerprint,
        "elapsed_seconds": time.perf_counter() - started,
        "documents": len(bundle.documents),
        "characters": sum(len(item.text) for item in bundle.documents),
        "chunks": len(chunks),
        "chunk_unit": chunk_unit,
        "index_bytes": legacy.directory_size(workspace / "index.jsonl"),
        "token_usage": {"ingestion_embedding": legacy.summed_usage(usages)},
    }
    legacy.json_dump(workspace / "ingestion.json", result)
    write_state(
        workspace,
        name=NAME,
        dataset_fingerprint=dataset_fingerprint,
        config_fingerprint=config_fingerprint,
        status="complete",
        completed_chunks=len(chunks),
        total_chunks=len(chunks),
    )
    return result


class HybridStore:
    """Dense + BM25 retrieval combined with reciprocal-rank fusion."""

    def __init__(self, rows: Sequence[dict[str, Any]]) -> None:
        self.rows = list(rows)
        self.lengths: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = collections.defaultdict(list)
        for index, row in enumerate(self.rows):
            counts = collections.Counter(_terms(str(row.get("text") or "")))
            self.lengths.append(sum(counts.values()))
            for term, frequency in counts.items():
                self.postings[term].append((index, frequency))
        self.average_length = sum(self.lengths) / max(1, len(self.lengths))

    def search(
        self,
        query: str,
        query_vector: Sequence[float],
        *,
        top_k: int,
        candidate_k: int,
    ) -> list[dict[str, Any]]:
        candidate_k = max(top_k, candidate_k)
        dense = heapq.nlargest(
            candidate_k,
            range(len(self.rows)),
            key=lambda index: sum(
                left * right
                for left, right in zip(query_vector, self.rows[index]["vector"])
            ),
        )
        lexical_scores: dict[int, float] = collections.defaultdict(float)
        total = max(1, len(self.rows))
        for term in set(_terms(query)):
            posting = self.postings.get(term, [])
            if not posting:
                continue
            idf = math.log(1.0 + (total - len(posting) + 0.5) / (len(posting) + 0.5))
            for index, frequency in posting:
                norm = frequency + 1.2 * (
                    0.25 + 0.75 * self.lengths[index] / max(1.0, self.average_length)
                )
                lexical_scores[index] += idf * frequency * 2.2 / norm
        lexical = heapq.nlargest(
            candidate_k,
            lexical_scores,
            key=lexical_scores.__getitem__,
        )
        fused: dict[int, float] = collections.defaultdict(float)
        for rank, index in enumerate(dense, start=1):
            fused[index] += 1.0 / (60 + rank)
        for rank, index in enumerate(lexical, start=1):
            fused[index] += 1.0 / (60 + rank)
        best = heapq.nlargest(top_k, fused, key=fused.__getitem__)
        return [self.rows[index] for index in best]


def _terms(value: str) -> list[str]:
    return [item.casefold() for item in _TERM_RE.findall(value)]


def _agent_answer(
    question: legacy.Question,
    store: HybridStore,
    chat_client: legacy.OpenAICompatibleClient,
    embed_client: legacy.OpenAICompatibleClient,
    args: SimpleNamespace,
) -> dict[str, Any]:
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "You are an autonomous retrieval agent with a hybrid_search tool. "
                "Search at least once, reformulate and search again whenever useful, "
                "then answer from evidence. Return only JSON: "
                '{"action":"search","query":"..."} or '
                '{"action":"answer","answer":"..."}.'
            ),
        },
        {"role": "user", "content": f"Question: {question.question}"},
    ]
    contexts: list[str] = []
    queries: list[str] = []
    chat_usages: list[dict[str, Any]] = []
    embedding_usages: list[dict[str, Any]] = []
    max_steps = max(1, int(getattr(args, "agent_max_steps", 8)))
    token_budget = max(1, int(getattr(args, "query_token_budget", 16_000)))
    answer = ""
    for step in range(1, max_steps + 1):
        raw, usage = chat_client.chat(
            messages,
            args.chat_model,
            temperature=args.temperature,
            max_tokens=args.max_answer_tokens,
            json_mode=True,
        )
        chat_usages.append(usage)
        try:
            decision = legacy.parse_json_object(raw)
        except Exception:
            decision = {"action": "answer", "answer": raw}
        action = str(decision.get("action") or "").strip().casefold()
        if action == "answer" and queries:
            answer = str(decision.get("answer") or "").strip()
            if answer:
                break
        query_text = str(decision.get("query") or question.question).strip()
        vectors, embed_usage = embed_client.embeddings([query_text], args.embed_model)
        embedding_usages.append(embed_usage)
        ranked = store.search(
            query_text,
            legacy.normalize_vector(vectors[0]),
            top_k=args.top_k,
            candidate_k=getattr(args, "hybrid_candidate_k", args.top_k * 10),
        )
        queries.append(query_text)
        contexts.extend(str(row["text"]) for row in ranked)
        evidence = "\n\n".join(
            f"[{row['id']}]\n{row['text']}" for row in ranked
        )[: legacy.AGENT_TOOL_RESULT_CHARS]
        messages.extend(
            [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"hybrid_search({query_text!r}) returned:\n{evidence}\n\n"
                        "Search again or provide the final JSON answer."
                    ),
                },
            ]
        )
        if legacy.summed_usage(chat_usages)["total_tokens"] >= token_budget:
            messages.append(
                {
                    "role": "user",
                    "content": "The query token budget is reached. Answer now from the evidence.",
                }
            )
            raw, usage = chat_client.chat(
                messages,
                args.chat_model,
                temperature=args.temperature,
                max_tokens=args.max_answer_tokens,
                json_mode=True,
            )
            chat_usages.append(usage)
            try:
                answer = str(legacy.parse_json_object(raw).get("answer") or "").strip()
            except Exception:
                answer = raw.strip()
            break
    if not answer:
        raise legacy.BenchmarkError(
            f"vanilla agent exhausted {max_steps} steps without a final answer"
        )
    agent_usage = legacy.summed_usage(chat_usages)
    embedding_usage = legacy.summed_usage(embedding_usages)
    return {
        "answer": answer,
        "context": contexts,
        "agent_queries": queries,
        "agent_steps": len(chat_usages),
        "token_usage": {
            "agent_chat": agent_usage,
            "retrieval_embedding": embedding_usage,
        },
        "token_accounting_complete": (
            agent_usage["total_tokens"] > 0
            and embedding_usage["total_tokens"] > 0
        ),
    }


def query(
    workspace: Path,
    questions: Sequence[legacy.Question],
    args: SimpleNamespace,
    on_result: ResultCallback | None = None,
) -> list[dict[str, Any]]:
    rows = legacy.jsonl_load(workspace / "index.jsonl")
    if not rows:
        raise legacy.BenchmarkError(f"vanilla index is empty: {workspace}")
    store = HybridStore(rows)
    chat_client = legacy.OpenAICompatibleClient(
        args.chat_base_url,
        args.chat_api_key,
        timeout=args.timeout,
    )
    embed_client = legacy.OpenAICompatibleClient(
        args.embed_base_url,
        args.embed_api_key,
        timeout=args.timeout,
    )

    def answer_one(question: legacy.Question) -> dict[str, Any]:
        result = question.result_base()
        started = time.perf_counter()
        try:
            answer = _agent_answer(question, store, chat_client, embed_client, args)
            result.update(
                {
                    "generated_answer": answer["answer"],
                    "context": answer["context"],
                    "agent_queries": answer["agent_queries"],
                    "agent_steps": answer["agent_steps"],
                    "retrieval_method": "dense+bm25-rrf",
                    "token_usage": answer["token_usage"],
                    "token_accounting_complete": answer[
                        "token_accounting_complete"
                    ],
                    "error": None,
                }
            )
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["latency_seconds"] = time.perf_counter() - started
        return result

    results: list[dict[str, Any] | None] = [None] * len(questions)
    workers = min(args.chat_concurrency, len(questions))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(answer_one, question): index
            for index, question in enumerate(questions)
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results[futures[future]] = result
            if on_result is not None:
                on_result(result)
            legacy.log(f"vanilla answered {completed}/{len(questions)}")
    return [item for item in results if item is not None]
