"""Low-latency, dependency-levelled RAG for realtime speaking clients.

The pipeline emits the complete plan before any answer text.  Queries inside a
level run concurrently; completed levels are emitted immediately so a caller
can enqueue their text for speech while the following level is researched.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from pydantic import BaseModel, Field


class RealtimeLlm(Protocol):
    def complete_structured(
        self, system_prompt: str, user_content: str, output_model: type[Any]
    ) -> Any: ...


class PlannedLevel(BaseModel):
    objective: str = ""
    queries: list[str] = Field(default_factory=list)


class LeveledPlan(BaseModel):
    levels: list[PlannedLevel] = Field(default_factory=list)


class ReferencedFact(BaseModel):
    text: str = ""
    node_ids: list[str] = Field(default_factory=list)


class ShallowAnswer(BaseModel):
    facts: list[ReferencedFact] = Field(default_factory=list)
    enough: bool = False
    missing_queries: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class RealtimeOptions:
    max_levels: int = 4
    max_queries_per_level: int = 4
    max_recovery_levels: int = 2
    search_limit: int = 8
    max_context_chars: int = 14_000
    min_search_results: int = 3


@dataclass
class RuntimeLevel:
    id: str
    objective: str
    queries: list[str]
    kind: str = "planned"
    recovery_for: str | None = None


@dataclass
class QueryOutcome:
    query: str
    facts: list[ReferencedFact] = field(default_factory=list)
    enough: bool = False
    missing_queries: list[str] = field(default_factory=list)
    retrieved_node_ids: list[str] = field(default_factory=list)
    search_result_count: int = 0
    latency_ms: int = 0
    error: str | None = None


class RealtimeStopped(Exception):
    """Raised between bounded pipeline operations after client cancellation."""


PLAN_SYSTEM_PROMPT = """You create a very small dependency-ordered research plan for a realtime
speaking assistant. Break the user's request into shallow, specific search
questions. Put independent questions in the same level. Put a question in a
later level only when it needs facts learned in an earlier level.

Rules:
- Use no more than the requested maximum levels and queries per level.
- Level 1 must produce useful, speakable facts as quickly as possible.
- Usually use 1-4 levels. Do not add ceremonial verification or summary levels.
- Every query must be standalone, concrete, and searchable in documentation.
- The final level should answer the dependent/causal part of the user's request.
- Write queries in the user's language.
"""


ANSWER_SYSTEM_PROMPT = """Answer one shallow documentation question for a realtime speaking
assistant using only the supplied evidence and established facts.

Return a few short, natural, speakable facts. Every fact must list the exact
node IDs that support it. Copy node IDs exactly from the supplied material.
Never cite an unavailable node ID. Do not use outside knowledge, do not guess,
and do not repeat an established fact unless it is needed to answer this query.
Set enough=false and provide specific missing_queries when the material cannot
fully answer the query. Keep the answer in the user's language.
"""


class RealtimePipeline:
    """Executes a leveled plan and emits plan/level events synchronously."""

    def __init__(
        self,
        *,
        llm_factory: Callable[[], RealtimeLlm],
        search: Callable[[str, int], list[dict[str, Any]]],
    ) -> None:
        self._llm_factory = llm_factory
        self._search = search

    def run(
        self,
        question: str,
        *,
        emit: Callable[[dict[str, Any]], None],
        options: RealtimeOptions | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        opts = options or RealtimeOptions()
        started = time.perf_counter()
        self._check_stop(stop_event)

        levels, planning_fallback = self._plan(question, opts)
        plan_version = 1
        statuses = {level.id: "pending" for level in levels}
        emit(
            {
                "type": "plan",
                "version": plan_version,
                "question": question,
                "planning_fallback": planning_fallback,
                "levels": self._public_levels(levels, statuses),
            }
        )

        established_facts: list[ReferencedFact] = []
        seen_fact_texts: set[str] = set()
        recovery_count = 0
        unresolved = False
        position = 0

        while position < len(levels):
            self._check_stop(stop_event)
            level = levels[position]
            statuses[level.id] = "running"
            emit(
                {
                    "type": "level_start",
                    "plan_version": plan_version,
                    "level_id": level.id,
                    "position": position + 1,
                    "objective": level.objective,
                    "queries": list(level.queries),
                }
            )

            level_started = time.perf_counter()
            outcomes = self._run_level(
                question,
                level,
                established_facts,
                opts,
                stop_event,
            )

            new_facts: list[ReferencedFact] = []
            for outcome in outcomes:
                for fact in outcome.facts:
                    key = self._fact_key(fact.text)
                    if not key or key in seen_fact_texts:
                        continue
                    seen_fact_texts.add(key)
                    new_facts.append(fact)

            established_facts.extend(new_facts)
            complete = bool(outcomes) and all(outcome.enough for outcome in outcomes)
            statuses[level.id] = "complete" if complete else "partial"

            missing_queries = self._unique(
                query
                for outcome in outcomes
                for query in outcome.missing_queries
                if query.strip()
            )[: opts.max_queries_per_level]

            # Insert a targeted recovery level before any dependent level.  The
            # plan update is emitted before more speech content, so the caller
            # always knows the current order.
            if not complete and recovery_count < opts.max_recovery_levels:
                if not missing_queries:
                    missing_queries = self._unique(
                        outcome.query for outcome in outcomes if not outcome.enough
                    )[: opts.max_queries_per_level]
                if missing_queries:
                    recovery_count += 1
                    recovery = RuntimeLevel(
                        id=f"recovery_{recovery_count}",
                        objective=f"Find missing evidence for {level.objective}",
                        queries=missing_queries,
                        kind="recovery",
                        recovery_for=level.id,
                    )
                    levels.insert(position + 1, recovery)
                    statuses[recovery.id] = "pending"
                    plan_version += 1
                    emit(
                        {
                            "type": "plan_update",
                            "version": plan_version,
                            "reason": "insufficient_evidence",
                            "inserted_level_id": recovery.id,
                            "after_level_id": level.id,
                            "levels": self._public_levels(levels, statuses),
                        }
                    )
            elif not complete:
                unresolved = True

            reference_node_ids = self._unique(
                node_id for fact in new_facts for node_id in fact.node_ids
            )
            text = " ".join(fact.text.strip() for fact in new_facts if fact.text.strip())
            emit(
                {
                    "type": "level",
                    "plan_version": plan_version,
                    "level_id": level.id,
                    "position": position + 1,
                    "objective": level.objective,
                    "queries": [
                        {
                            "query": outcome.query,
                            "enough": outcome.enough,
                            "reference_node_ids": self._unique(
                                node_id
                                for fact in outcome.facts
                                for node_id in fact.node_ids
                            ),
                            "search_result_count": outcome.search_result_count,
                            "latency_ms": outcome.latency_ms,
                            "error": outcome.error,
                        }
                        for outcome in outcomes
                    ],
                    "text": text,
                    "facts": [fact.model_dump() for fact in new_facts],
                    "reference_node_ids": reference_node_ids,
                    "complete": complete,
                    "latency_ms": round((time.perf_counter() - level_started) * 1000),
                }
            )
            position += 1

        all_node_ids = self._unique(
            node_id for fact in established_facts for node_id in fact.node_ids
        )
        summary = {
            "status": "partial" if unresolved else "complete",
            "plan_version": plan_version,
            "levels_completed": len(levels),
            "facts": [fact.model_dump() for fact in established_facts],
            "reference_node_ids": all_node_ids,
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }
        emit({"type": "done", **summary})
        return summary

    def _plan(
        self, question: str, options: RealtimeOptions
    ) -> tuple[list[RuntimeLevel], bool]:
        prompt = (
            f"User question:\n{question}\n\n"
            f"Maximum levels: {options.max_levels}\n"
            f"Maximum queries in each level: {options.max_queries_per_level}"
        )
        fallback = False
        try:
            raw = self._llm_factory().complete_structured(
                PLAN_SYSTEM_PROMPT, prompt, LeveledPlan
            )
        except Exception:
            raw = LeveledPlan(
                levels=[PlannedLevel(objective="Answer the user", queries=[question])]
            )
            fallback = True

        levels: list[RuntimeLevel] = []
        for raw_level in raw.levels[: options.max_levels]:
            queries = self._unique(
                query.strip() for query in raw_level.queries if query.strip()
            )[: options.max_queries_per_level]
            if not queries:
                continue
            levels.append(
                RuntimeLevel(
                    id=f"level_{len(levels) + 1}",
                    objective=raw_level.objective.strip() or queries[0],
                    queries=queries,
                )
            )

        if not levels:
            fallback = True
            levels = [
                RuntimeLevel(
                    id="level_1", objective="Answer the user", queries=[question]
                )
            ]
        return levels, fallback

    def _run_level(
        self,
        original_question: str,
        level: RuntimeLevel,
        established_facts: list[ReferencedFact],
        options: RealtimeOptions,
        stop_event: threading.Event | None,
    ) -> list[QueryOutcome]:
        outcomes: list[QueryOutcome | None] = [None] * len(level.queries)
        workers = max(1, min(len(level.queries), options.max_queries_per_level))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="realtime-query"
        ) as executor:
            futures = {
                executor.submit(
                    self._answer_query,
                    original_question,
                    level,
                    query,
                    established_facts,
                    options,
                    stop_event,
                ): index
                for index, query in enumerate(level.queries)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    outcomes[index] = future.result()
                except RealtimeStopped:
                    raise
                except Exception as exc:
                    outcomes[index] = QueryOutcome(
                        query=level.queries[index],
                        enough=False,
                        missing_queries=[level.queries[index]],
                        error=f"{type(exc).__name__}: {exc}",
                    )
        return [outcome for outcome in outcomes if outcome is not None]

    def _answer_query(
        self,
        original_question: str,
        level: RuntimeLevel,
        query: str,
        established_facts: list[ReferencedFact],
        options: RealtimeOptions,
        stop_event: threading.Event | None,
    ) -> QueryOutcome:
        started = time.perf_counter()
        self._check_stop(stop_event)
        established_text = self._format_facts(established_facts, max_chars=2_500)
        search_text = query
        if established_text:
            search_text += f"\nEstablished context:\n{established_text}"

        results = self._search(search_text, options.search_limit)
        self._check_stop(stop_event)

        # Sparse results get one broader search.  Strong subqueries stop after
        # the first search, while weak ones automatically cast a wider net.
        if len(results) < options.min_search_results:
            expanded_query = (
                f"{original_question}\nCurrent objective: {level.objective}\n"
                f"Specific question: {query}"
            )
            expanded = self._search(
                expanded_query, min(options.search_limit * 2, 16)
            )
            results = self._merge_results(results, expanded)

        retrieved_ids = self._result_node_ids(results)
        allowed_ids = set(retrieved_ids)
        allowed_ids.update(
            node_id for fact in established_facts for node_id in fact.node_ids
        )
        evidence = self._format_evidence(results, options.max_context_chars)

        if not evidence and not established_text:
            return QueryOutcome(
                query=query,
                enough=False,
                missing_queries=[query],
                retrieved_node_ids=retrieved_ids,
                search_result_count=len(results),
                latency_ms=round((time.perf_counter() - started) * 1000),
            )

        user_content = (
            f"Original user question:\n{original_question}\n\n"
            f"Current level objective:\n{level.objective}\n\n"
            f"Current shallow question:\n{query}\n\n"
            f"Established facts from earlier levels:\n"
            f"{established_text or '(none)'}\n\n"
            f"Retrieved evidence:\n{evidence or '(none)'}"
        )
        answer = self._llm_factory().complete_structured(
            ANSWER_SYSTEM_PROMPT, user_content, ShallowAnswer
        )
        self._check_stop(stop_event)

        facts: list[ReferencedFact] = []
        for fact in answer.facts:
            text = " ".join(fact.text.split()).strip()
            node_ids = self._unique(
                node_id.strip()
                for node_id in fact.node_ids
                if node_id.strip() in allowed_ids
            )
            # Cheap hard guard: facts without a retrieved/established reference
            # never reach the speech stream or a later level.
            if text and node_ids:
                facts.append(ReferencedFact(text=text, node_ids=node_ids))

        enough = bool(answer.enough and facts)
        missing = self._unique(
            item.strip() for item in answer.missing_queries if item.strip()
        )[: options.max_queries_per_level]
        if not enough and not missing:
            missing = [query]

        return QueryOutcome(
            query=query,
            facts=facts,
            enough=enough,
            missing_queries=missing,
            retrieved_node_ids=retrieved_ids,
            search_result_count=len(results),
            latency_ms=round((time.perf_counter() - started) * 1000),
        )

    @staticmethod
    def _public_levels(
        levels: list[RuntimeLevel], statuses: dict[str, str]
    ) -> list[dict[str, Any]]:
        public: list[dict[str, Any]] = []
        for index, level in enumerate(levels):
            public.append(
                {
                    "id": level.id,
                    "position": index + 1,
                    "objective": level.objective,
                    "queries": list(level.queries),
                    "depends_on": [levels[index - 1].id] if index else [],
                    "kind": level.kind,
                    "recovery_for": level.recovery_for,
                    "status": statuses.get(level.id, "pending"),
                }
            )
        return public

    @staticmethod
    def _format_facts(facts: list[ReferencedFact], max_chars: int) -> str:
        lines: list[str] = []
        size = 0
        for fact in facts:
            line = f"- {fact.text} [{', '.join(fact.node_ids)}]"
            if size + len(line) > max_chars:
                break
            lines.append(line)
            size += len(line) + 1
        return "\n".join(lines)

    @staticmethod
    def _format_evidence(results: list[dict[str, Any]], max_chars: int) -> str:
        blocks: list[str] = []
        size = 0
        for result in results:
            node = result.get("node")
            node_id = str(getattr(node, "id", "") or "").strip()
            if not node_id:
                continue
            title = str(getattr(node, "title", "") or "").strip()
            snippets: list[str] = []
            for item in result.get("evidence", [])[:3]:
                text = " ".join(str(item.get("text") or "").split()).strip()
                if text:
                    snippets.append(text[:1_800])
            if not snippets:
                fallback = (
                    str(getattr(node, "summary", "") or "").strip()
                    or str(getattr(node, "body", "") or "").strip()
                )
                if fallback:
                    snippets.append(" ".join(fallback.split())[:2_000])
            if not snippets:
                continue
            block = f"node_id: {node_id}\ntitle: {title}\n" + "\n".join(
                f"evidence: {snippet}" for snippet in snippets
            )
            if size + len(block) > max_chars:
                break
            blocks.append(block)
            size += len(block) + 2
        return "\n\n".join(blocks)

    @staticmethod
    def _merge_results(
        first: list[dict[str, Any]], second: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for result in [*first, *second]:
            node = result.get("node")
            node_id = str(getattr(node, "id", "") or "").strip()
            if not node_id or node_id in seen:
                continue
            seen.add(node_id)
            merged.append(result)
        return merged

    @staticmethod
    def _result_node_ids(results: list[dict[str, Any]]) -> list[str]:
        return RealtimePipeline._unique(
            str(getattr(result.get("node"), "id", "") or "").strip()
            for result in results
            if str(getattr(result.get("node"), "id", "") or "").strip()
        )

    @staticmethod
    def _fact_key(text: str) -> str:
        return re.sub(r"\W+", " ", text.casefold()).strip()

    @staticmethod
    def _unique(values) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    @staticmethod
    def _check_stop(stop_event: threading.Event | None) -> None:
        if stop_event is not None and stop_event.is_set():
            raise RealtimeStopped("realtime research cancelled")
