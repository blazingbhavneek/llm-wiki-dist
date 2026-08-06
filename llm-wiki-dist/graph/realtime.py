"""Low-latency, dependency-levelled RAG for realtime speaking clients.

The pipeline emits the complete plan before any answer text.  Queries inside a
level run concurrently; completed levels are emitted immediately so a caller
can enqueue their text for speech while the following level is researched.

Hard rules enforced here, not in the prompt:

- a fact without a node ID that was actually retrieved (or already accepted in
  an earlier level) never reaches the stream or a later level;
- a run that could not support every part of the question terminates with
  ``status="partial"`` and a machine-readable ``incomplete_reason``;
- the run stops planning new work once its wall-clock budget is spent.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Protocol

from pydantic import BaseModel, Field

log = logging.getLogger("graph_realtime")


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
    # Wall-clock budget for one run. Retrieval and model calls cannot be
    # interrupted once started, so the budget bounds *new* work: past it the
    # pipeline stops scheduling levels and reports the run as partial.
    deadline_seconds: float = 90.0
    # Accepted facts are context for generation. Only a short, ID-free digest
    # of them is appended to a retrieval string: node IDs and long fact lists
    # turn the BM25 side into a hundred-term OR query and blow up the embedded
    # query, which costs both latency and precision.
    max_established_chars: int = 2_500
    max_search_context_chars: int = 300


def _normalize_options(options: RealtimeOptions) -> RealtimeOptions:
    """Clamp caller-supplied options so a bad value degrades instead of raising."""
    return replace(
        options,
        max_levels=max(1, int(options.max_levels)),
        max_queries_per_level=max(1, int(options.max_queries_per_level)),
        max_recovery_levels=max(0, int(options.max_recovery_levels)),
        search_limit=max(1, int(options.search_limit)),
        max_context_chars=max(500, int(options.max_context_chars)),
        min_search_results=max(0, int(options.min_search_results)),
        deadline_seconds=max(0.0, float(options.deadline_seconds)),
        max_established_chars=max(0, int(options.max_established_chars)),
        max_search_context_chars=max(0, int(options.max_search_context_chars)),
    )


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
- Never repeat the same query in two levels.
- The final level should answer the dependent/causal part of the user's request.
- Write queries in the user's language.
"""


ANSWER_SYSTEM_PROMPT = """Answer one shallow documentation question for a realtime speaking
assistant using only the supplied evidence and established facts.

Return a few short, natural, speakable facts. Every fact must list the exact
node IDs that support it in the node_ids field. Copy node IDs exactly from the
supplied material. Never cite an unavailable node ID. Do not write node IDs
inside the spoken fact text. Do not use outside knowledge, do not guess, and do
not repeat an established fact unless it is needed to answer this query.

Set enough=false when the material cannot fully answer the query, and put
specific, differently-worded follow-up searches in missing_queries. Never repeat
the current question as a missing query. Keep the answer in the user's language.
"""


# Citation shapes the model tends to emit around IDs: `**node:14**`, `[node:14]`,
# `"node:14".`  Matching is done on a normalized form so a cosmetic wrapper does
# not silently discard an otherwise supported fact.
_ID_EDGE_RE = re.compile(r"^[\s`'\"*<\[(]+|[\s`'\"*>\])]+$")
_ID_TAIL_RE = re.compile(r"[,.;:!?]+$")
_ID_SPLIT_RE = re.compile(r"[\s,;]+")
_BRACKET_RE = re.compile(r"[\[(]([^\[\]()]{1,200})[\])]")


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
        # One client per worker thread: building a chat client costs a fresh
        # HTTP connection, which is pure latency on the critical path. The
        # pipeline is per-request, so nothing leaks between runs.
        self._local = threading.local()

    def run(
        self,
        question: str,
        *,
        emit: Callable[[dict[str, Any]], None],
        options: RealtimeOptions | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        opts = _normalize_options(options or RealtimeOptions())
        question = (question or "").strip()
        if not question:
            raise ValueError("question must not be empty")

        started = time.perf_counter()
        deadline = started + opts.deadline_seconds if opts.deadline_seconds else None
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
        # Reserve every planned query so recovery does not insert duplicate
        # work ahead of a later level. Missing evidence is tracked separately
        # below; when that later query completes it closes the earlier gap.
        issued_queries = {
            self._query_key(query) for level in levels for query in level.queries
        }
        open_gap_queries: set[str] = set()
        recovery_count = 0
        levels_completed = 0
        unresolved = False
        incomplete_reason: str | None = None
        position = 0

        # One executor for the whole run: worker threads (and their cached chat
        # clients) survive across levels, and cancellation does not block on
        # in-flight model calls the way a per-level ``with`` block would.
        executor = ThreadPoolExecutor(
            max_workers=max(1, opts.max_queries_per_level),
            thread_name_prefix="realtime-query",
        )
        try:
            while position < len(levels):
                self._check_stop(stop_event)
                if self._expired(deadline):
                    unresolved = True
                    incomplete_reason = incomplete_reason or "deadline"
                    for pending in levels[position:]:
                        statuses[pending.id] = "skipped"
                    plan_version += 1
                    emit(
                        {
                            "type": "plan_update",
                            "version": plan_version,
                            "reason": "deadline",
                            "levels": self._public_levels(levels, statuses),
                        }
                    )
                    break

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
                    executor,
                    deadline,
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
                complete = bool(outcomes) and all(
                    outcome.enough for outcome in outcomes
                )
                statuses[level.id] = "complete" if complete else "partial"

                # Track missing evidence by normalized query, not as a sticky
                # level-wide boolean. A later planned query may be exactly the
                # follow-up an earlier level requested; if it succeeds, that
                # gap is resolved and the final run can still be complete.
                for outcome in outcomes:
                    query_key = self._query_key(outcome.query)
                    if outcome.enough:
                        open_gap_queries.discard(query_key)
                        continue
                    missing_keys = {
                        self._query_key(query)
                        for query in outcome.missing_queries
                        if self._query_key(query)
                    }
                    if missing_keys:
                        open_gap_queries.discard(query_key)
                        open_gap_queries.update(missing_keys)
                    elif query_key:
                        open_gap_queries.add(query_key)

                # Insert a targeted recovery level before any dependent level.
                # The plan update is emitted before more speech content, so the
                # caller always knows the current order.
                if not complete:
                    recovery_queries = self._recovery_queries(
                        outcomes, issued_queries, opts
                    )
                    can_recover = (
                        bool(recovery_queries)
                        and recovery_count < opts.max_recovery_levels
                        and not self._expired(deadline)
                    )
                    if can_recover:
                        recovery_count += 1
                        recovery = RuntimeLevel(
                            id=f"recovery_{recovery_count}",
                            objective=f"Find missing evidence for {level.objective}",
                            queries=recovery_queries,
                            kind="recovery",
                            recovery_for=level.id,
                        )
                        levels.insert(position + 1, recovery)
                        issued_queries.update(
                            self._query_key(query) for query in recovery_queries
                        )
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
                    else:
                        # A matching query may already exist in a later planned
                        # level. Leave its gap open and decide final completeness
                        # only after all planned work has had a chance to run.
                        if self._expired(deadline):
                            unresolved = True
                            incomplete_reason = incomplete_reason or "deadline"

                reference_node_ids = self._unique(
                    node_id for fact in new_facts for node_id in fact.node_ids
                )
                text = " ".join(
                    fact.text.strip() for fact in new_facts if fact.text.strip()
                )
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
                        "latency_ms": round(
                            (time.perf_counter() - level_started) * 1000
                        ),
                    }
                )
                levels_completed += 1
                position += 1
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if open_gap_queries:
            unresolved = True
            incomplete_reason = incomplete_reason or "evidence_missing"

        all_node_ids = self._unique(
            node_id for fact in established_facts for node_id in fact.node_ids
        )
        summary = {
            "status": "partial" if unresolved else "complete",
            "incomplete_reason": incomplete_reason,
            "plan_version": plan_version,
            "levels_completed": levels_completed,
            "levels_planned": len(levels),
            "facts": [fact.model_dump() for fact in established_facts],
            "reference_node_ids": all_node_ids,
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }
        emit({"type": "done", **summary})
        return summary

    # region planning

    def _plan(
        self, question: str, options: RealtimeOptions
    ) -> tuple[list[RuntimeLevel], bool]:
        prompt = (
            f"User question:\n{question}\n\n"
            f"Maximum levels: {options.max_levels}\n"
            f"Maximum queries in each level: {options.max_queries_per_level}"
        )
        fallback = False
        raw: LeveledPlan | None = None
        try:
            candidate = self._llm().complete_structured(
                PLAN_SYSTEM_PROMPT, prompt, LeveledPlan
            )
            raw = candidate if isinstance(candidate, LeveledPlan) else None
        except Exception as exc:
            log.info("realtime planning failed; single-level fallback: %s", exc)

        if raw is None:
            raw = LeveledPlan(
                levels=[PlannedLevel(objective="Answer the user", queries=[question])]
            )
            fallback = True

        levels: list[RuntimeLevel] = []
        planned_keys: set[str] = set()
        for raw_level in raw.levels[: options.max_levels]:
            queries: list[str] = []
            for query in raw_level.queries:
                query = " ".join(str(query or "").split()).strip()
                key = self._query_key(query)
                # A query already planned in an earlier level would spend a
                # whole LLM call re-deriving facts the run already has.
                if not key or key in planned_keys:
                    continue
                planned_keys.add(key)
                queries.append(query)
                if len(queries) >= options.max_queries_per_level:
                    break
            if not queries:
                continue
            levels.append(
                RuntimeLevel(
                    id=f"level_{len(levels) + 1}",
                    objective=(raw_level.objective or "").strip() or queries[0],
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

    def _recovery_queries(
        self,
        outcomes: list[QueryOutcome],
        issued_queries: set[str],
        options: RealtimeOptions,
    ) -> list[str]:
        """Pick searches worth another round trip.

        Model-proposed follow-ups only count when they are genuinely new. A
        query that failed with an error is allowed to repeat, because there the
        gap is a transient failure rather than missing evidence.
        """
        candidates: list[str] = []
        seen: set[str] = set()

        def add(query: str, *, allow_repeat: bool = False) -> None:
            text = " ".join(str(query or "").split()).strip()
            key = self._query_key(text)
            if not key or key in seen:
                return
            if key in issued_queries and not allow_repeat:
                return
            seen.add(key)
            candidates.append(text)

        for outcome in outcomes:
            if outcome.error:
                add(outcome.query, allow_repeat=True)
        for outcome in outcomes:
            for query in outcome.missing_queries:
                add(query)

        return candidates[: options.max_queries_per_level]

    # endregion planning

    # region execution

    def _run_level(
        self,
        original_question: str,
        level: RuntimeLevel,
        established_facts: list[ReferencedFact],
        options: RealtimeOptions,
        stop_event: threading.Event | None,
        executor: ThreadPoolExecutor,
        deadline: float | None,
    ) -> list[QueryOutcome]:
        outcomes: list[QueryOutcome | None] = [None] * len(level.queries)
        futures: dict[Future, int] = {
            executor.submit(
                self._answer_query,
                original_question,
                level,
                query,
                established_facts,
                options,
                stop_event,
                deadline,
            ): index
            for index, query in enumerate(level.queries)
        }

        stopped: RealtimeStopped | None = None
        for future in as_completed(futures):
            index = futures[future]
            try:
                outcomes[index] = future.result()
            except RealtimeStopped as exc:
                stopped = exc
                break
            except Exception as exc:
                log.info("realtime query failed: %s", exc)
                outcomes[index] = QueryOutcome(
                    query=level.queries[index],
                    enough=False,
                    error=self._short_error(exc),
                )

        if stopped is not None:
            # Cancel what has not started; the executor is torn down without
            # waiting, so a client cancel is not held hostage by a model call.
            for future in futures:
                future.cancel()
            raise stopped

        return [outcome for outcome in outcomes if outcome is not None]

    def _answer_query(
        self,
        original_question: str,
        level: RuntimeLevel,
        query: str,
        established_facts: list[ReferencedFact],
        options: RealtimeOptions,
        stop_event: threading.Event | None,
        deadline: float | None,
    ) -> QueryOutcome:
        started = time.perf_counter()
        self._check_stop(stop_event)

        established_text = self._format_facts(
            established_facts, max_chars=options.max_established_chars
        )
        search_context = self._search_context(
            established_facts, options.max_search_context_chars
        )
        search_text = f"{query} {search_context}".strip() if search_context else query

        results, search_error = self._safe_search(search_text, options.search_limit)
        self._check_stop(stop_event)

        # Sparse results get one broader search. Strong subqueries stop after
        # the first search, while weak ones automatically cast a wider net.
        if len(results) < options.min_search_results and not self._expired(deadline):
            expanded_query = (
                f"{original_question}\nCurrent objective: {level.objective}\n"
                f"Specific question: {query}"
            )
            expanded, expanded_error = self._safe_search(
                expanded_query, min(options.search_limit * 2, 16)
            )
            results = self._merge_results(results, expanded)
            search_error = search_error or expanded_error

        retrieved_ids = self._result_node_ids(results)
        allowed = self._allowed_ids(retrieved_ids, established_facts)
        evidence = self._format_evidence(results, options.max_context_chars)

        if not evidence and not established_text:
            return QueryOutcome(
                query=query,
                enough=False,
                missing_queries=[],
                retrieved_node_ids=retrieved_ids,
                search_result_count=len(results),
                latency_ms=self._elapsed_ms(started),
                error=search_error,
            )

        user_content = (
            f"Original user question:\n{original_question}\n\n"
            f"Current level objective:\n{level.objective}\n\n"
            f"Current shallow question:\n{query}\n\n"
            f"Established facts from earlier levels:\n"
            f"{established_text or '(none)'}\n\n"
            f"Retrieved evidence:\n{evidence or '(none)'}"
        )
        try:
            answer = self._llm().complete_structured(
                ANSWER_SYSTEM_PROMPT, user_content, ShallowAnswer
            )
        except Exception as exc:
            log.info("realtime answer call failed: %s", exc)
            return QueryOutcome(
                query=query,
                enough=False,
                retrieved_node_ids=retrieved_ids,
                search_result_count=len(results),
                latency_ms=self._elapsed_ms(started),
                error=self._short_error(exc),
            )
        self._check_stop(stop_event)

        facts: list[ReferencedFact] = []
        for fact in getattr(answer, "facts", None) or []:
            node_ids = self._resolve_ids(fact.node_ids, allowed)
            text = self._clean_fact_text(fact.text, allowed)
            # Cheap hard guard: facts without a retrieved/established reference
            # never reach the speech stream or a later level.
            if text and node_ids:
                facts.append(ReferencedFact(text=text, node_ids=node_ids))

        enough = bool(getattr(answer, "enough", False) and facts)
        current_key = self._query_key(query)
        missing = [
            item
            for item in self._unique(
                " ".join(str(item or "").split()).strip()
                for item in (getattr(answer, "missing_queries", None) or [])
            )
            if item and self._query_key(item) != current_key
        ][: options.max_queries_per_level]

        return QueryOutcome(
            query=query,
            facts=facts,
            enough=enough,
            missing_queries=[] if enough else missing,
            retrieved_node_ids=retrieved_ids,
            search_result_count=len(results),
            latency_ms=self._elapsed_ms(started),
            error=search_error,
        )

    def _safe_search(
        self, text: str, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Retrieval failure degrades one subquery instead of the whole run."""
        try:
            results = self._search(text, limit)
        except Exception as exc:
            log.info("realtime search failed: %s", exc)
            return [], self._short_error(exc)
        return list(results or []), None

    def _llm(self) -> RealtimeLlm:
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._llm_factory()
            self._local.client = client
        return client

    # endregion execution

    # region formatting/validation helpers

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
    def _search_context(facts: list[ReferencedFact], max_chars: int) -> str:
        """Short, ID-free digest of the most recent facts for retrieval only."""
        if max_chars <= 0:
            return ""
        parts: list[str] = []
        size = 0
        for fact in reversed(facts):
            text = " ".join(fact.text.split()).strip()
            if not text:
                continue
            if size + len(text) > max_chars:
                break
            parts.append(text)
            size += len(text) + 1
        return " ".join(reversed(parts))

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
            for item in (result.get("evidence") or [])[:3]:
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
    def _allowed_ids(
        retrieved_ids: list[str], established_facts: list[ReferencedFact]
    ) -> dict[str, str]:
        """Map normalized node ID -> canonical node ID for citation checking."""
        allowed: dict[str, str] = {}
        for node_id in retrieved_ids:
            allowed.setdefault(RealtimePipeline._normalize_id(node_id), node_id)
        for fact in established_facts:
            for node_id in fact.node_ids:
                allowed.setdefault(RealtimePipeline._normalize_id(node_id), node_id)
        allowed.pop("", None)
        return allowed

    @staticmethod
    def _normalize_id(value: str) -> str:
        text = _ID_EDGE_RE.sub("", str(value or "").strip())
        return _ID_TAIL_RE.sub("", text).strip().casefold()

    @staticmethod
    def _resolve_ids(values: Iterable[str], allowed: dict[str, str]) -> list[str]:
        """Keep only citations that resolve to a retrieved/established node ID.

        Matching is done on a normalized form, and a value holding several IDs
        (``"node:1, node:2"``) is split, so a formatting quirk does not throw
        away a fact the evidence actually supports. Anything that still does not
        resolve is dropped: an unverifiable reference is exactly what must never
        reach the speaker.
        """
        resolved: list[str] = []
        seen: set[str] = set()
        for value in values or []:
            raw = str(value or "")
            tokens = [raw, *(_ID_SPLIT_RE.split(raw) if raw else [])]
            for token in tokens:
                canonical = allowed.get(RealtimePipeline._normalize_id(token))
                if canonical and canonical not in seen:
                    seen.add(canonical)
                    resolved.append(canonical)
        return resolved

    @staticmethod
    def _clean_fact_text(text: str, allowed: dict[str, str]) -> str:
        """Normalize whitespace and drop bracketed citations from speech text.

        Only groups whose every token is a known node ID are removed, so real
        parenthetical content survives. A TTS client should never read a node ID
        out loud.
        """

        def replace(match: re.Match) -> str:
            tokens = [token for token in _ID_SPLIT_RE.split(match.group(1)) if token]
            if tokens and all(
                RealtimePipeline._normalize_id(token) in allowed for token in tokens
            ):
                return " "
            return match.group(0)

        cleaned = _BRACKET_RE.sub(replace, str(text or ""))
        return " ".join(cleaned.split()).strip()

    @staticmethod
    def _fact_key(text: str) -> str:
        return re.sub(r"\W+", " ", text.casefold()).strip()

    @staticmethod
    def _query_key(text: str) -> str:
        return re.sub(r"\W+", " ", str(text or "").casefold()).strip()

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
    def _short_error(exc: BaseException) -> str:
        # Bounded and type-prefixed: level events are client-facing, and a raw
        # provider error can be a wall of text containing endpoint details.
        return f"{type(exc).__name__}: {exc}".replace("\n", " ")[:200]

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return round((time.perf_counter() - started) * 1000)

    @staticmethod
    def _expired(deadline: float | None) -> bool:
        return deadline is not None and time.perf_counter() >= deadline

    @staticmethod
    def _check_stop(stop_event: threading.Event | None) -> None:
        if stop_event is not None and stop_event.is_set():
            raise RealtimeStopped("realtime research cancelled")

    # endregion formatting/validation helpers
