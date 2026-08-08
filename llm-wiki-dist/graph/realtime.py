"""Low-latency, dependency-levelled RAG for realtime speaking clients.

The pipeline emits the complete plan before any answer text. Completed levels
are emitted immediately so a caller can enqueue their text for speech while the
following level is researched.

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


class ShallowResearchAnswer(BaseModel):
    """One source-backed answer section for a realtime level."""

    answer: str = ""
    node_ids: list[str] = Field(default_factory=list)


# Retained as transport-compatible schemas for callers that imported the
# earlier experimental reader pipeline. The latency path no longer instantiates
# them because those extra model turns were serialized by the provider.
class ResearchMove(BaseModel):
    read_node_ids: list[str] = Field(default_factory=list)
    search_query: str = ""


class ReaderAssignment(BaseModel):
    angle: str = ""
    node_ids: list[str] = Field(default_factory=list)


class ReaderReport(BaseModel):
    evidence: str = ""
    node_ids: list[str] = Field(default_factory=list)
    follow_up_query: str = ""


@dataclass(frozen=True)
class RealtimeOptions:
    # The fast default answers the original request in one level without a
    # planning round trip. Clients that explicitly request multiple levels get
    # the slower dependency planner.
    max_levels: int = 1
    max_queries_per_level: int = 1
    # Compatibility-only knobs retained so older callers do not break.
    max_recovery_levels: int = 0
    search_limit: int = 16
    # An unbounded collection of full node bodies is both slow and easy for a
    # model to lose in. Keep the default prompt bounded; callers can still set
    # zero when they intentionally prefer exhaustive context over latency.
    max_context_chars: int = 32_000
    min_search_results: int = 1
    # Wall-clock budget for one run. Retrieval and model calls cannot be
    # interrupted once started, so the budget bounds *new* work: past it the
    # pipeline stops scheduling levels and reports the run as partial.
    deadline_seconds: float = 90.0
    # Accepted for backwards compatibility. The fast path does not add an
    # unbounded research loop after the first retrieval.
    research_seconds_per_query: float = 0.0
    # A realtime client gives each emitted level fifteen seconds.  Reserve a
    # little transport margin and never begin new work after this deadline.
    stage_deadline_seconds: float = 12.0
    # Read a broad ranked set before synthesis. Context is shared fairly across
    # these nodes, so one long manual page cannot hide a concise prerequisite.
    min_initial_read_nodes: int = 16
    # Accepted answer sections are context for later levels. They are supplied
    # to generation only, never appended to the retrieval query.
    max_established_chars: int = 2_500
    max_search_context_chars: int = 300  # compatibility-only; unused


def _normalize_options(options: RealtimeOptions) -> RealtimeOptions:
    """Clamp caller-supplied options so a bad value degrades instead of raising."""
    return replace(
        options,
        max_levels=max(1, int(options.max_levels)),
        max_queries_per_level=max(1, int(options.max_queries_per_level)),
        max_recovery_levels=max(0, int(options.max_recovery_levels)),
        search_limit=max(1, int(options.search_limit)),
        max_context_chars=max(0, int(options.max_context_chars)),
        min_search_results=max(0, int(options.min_search_results)),
        deadline_seconds=max(0.0, float(options.deadline_seconds)),
        research_seconds_per_query=max(
            0.0, float(options.research_seconds_per_query)
        ),
        stage_deadline_seconds=max(0.0, float(options.stage_deadline_seconds)),
        min_initial_read_nodes=max(1, int(options.min_initial_read_nodes)),
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
- Queries in the same level must cover distinct answer sections. Do not split a
  single definition into paraphrases. For API documentation, prefer sections
  such as purpose/return value, signature/parameters, and option flags.
- When the requested maximum allows it, always create at least two levels. The
  second level must add concrete details, settings, prerequisites, or conditions
  rather than paraphrasing the first answer.
- The final level should answer the dependent/causal part of the user's request.
- Write queries in the user's language.
"""


SYNTHESIS_SYSTEM_PROMPT = """You are the final fast documentation writer in a realtime
assistant. Answer the current planned section using only ranked source evidence
and earlier completed sections. Return a complete, direct, useful answer in the
user's language; do not describe the research process, evidence quality, or
missing material. Each selected source contains its query-match snippets and
document text; combine complementary sources and do not stop after the first
plausible match. For questions asking for files, prerequisites, steps, choices,
or a list, enumerate the complete supported set and keep required items primary
rather than conditional asides. Include every exact supporting node ID in
node_ids and no IDs not supplied. Do not put IDs in the answer text. Use compact
Markdown and do not request further research.
"""


# Citation shapes the model tends to emit around IDs: `**node:14**`, `[node:14]`,
# `"node:14".`  Matching is done on a normalized form so a cosmetic wrapper does
# not silently discard an otherwise supported fact.
_ID_EDGE_RE = re.compile(r"^[\s`'\"*<\[(]+|[\s`'\"*>\])]+$")
_ID_TAIL_RE = re.compile(r"[,.;:!?]+$")
_ID_SPLIT_RE = re.compile(r"[\s,;]+")
_BRACKET_RE = re.compile(r"[\[(]([^\[\]()]{1,200})[\])]")
_UNSUPPORTED_DISCLAIMER_RE = re.compile(
    r"(?:提供された|与えられた)(?:資料|情報|抜粋).{0,140}?"
    r"(?:記載|記述|情報|定義|説明).{0,80}?"
    r"(?:ありません|見つかりません|不足しています)(?:。|\.|$)|"
    r"(?:the )?(?:provided|supplied) (?:material|evidence|documentation).{0,140}?"
    r"(?:does not|doesn't|cannot).{0,100}?(?:state|describe|contain|provide).{0,80}?(?:\.|$)",
    re.IGNORECASE,
)


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
        levels_completed = 0
        unresolved = False
        incomplete_reason: str | None = None
        position = 0

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
            stage_deadline = level_started + opts.stage_deadline_seconds
            if deadline is not None:
                stage_deadline = min(stage_deadline, deadline)
            outcomes = self._run_level(
                question,
                level,
                established_facts,
                opts,
                stop_event,
                stage_deadline,
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
            # Do not turn sparse retrieval into recovery levels or user-facing
            # evidence warnings; completed sections remain independently useful
            # and later levels receive them as editorial context.
            complete = True
            statuses[level.id] = "complete"

            reference_node_ids = self._unique(
                node_id for fact in new_facts for node_id in fact.node_ids
            )
            text = "\n\n".join(
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
                            "answered": bool(outcome.facts),
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
            levels_completed += 1
            position += 1

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
        # For the default one-level request, the original question is already
        # the best retrieval query. Skipping a planning generation removes one
        # full model round trip from time-to-first-answer.
        if options.max_levels == 1:
            return [
                RuntimeLevel(
                    id="level_1", objective="Answer the user", queries=[question]
                )
            ], False

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
        if len(levels) == 1 and options.max_levels >= 2:
            japanese = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", question))
            detail_query = (
                f"{question} 詳細・記述項目・設定パラメータ・関連条件"
                if japanese
                else f"{question} details, configuration parameters, prerequisites, and conditions"
            )
            levels.append(
                RuntimeLevel(
                    id="level_2",
                    objective=(
                        "詳細・設定・関連条件を補足する"
                        if japanese
                        else "Add details, settings, and related conditions"
                    ),
                    queries=[detail_query],
                )
            )
        return levels, fallback

    # endregion planning

    # region execution

    def _run_level(
        self,
        original_question: str,
        level: RuntimeLevel,
        established_facts: list[ReferencedFact],
        options: RealtimeOptions,
        stop_event: threading.Event | None,
        deadline: float | None,
    ) -> list[QueryOutcome]:
        outcomes: list[QueryOutcome] = []
        for query in level.queries:
            try:
                outcomes.append(
                    self._answer_query(
                        original_question,
                        level,
                        query,
                        established_facts,
                        options,
                        stop_event,
                        deadline,
                    )
                )
            except RealtimeStopped:
                raise
            except Exception as exc:
                log.info("realtime query failed: %s", exc)
                outcomes.append(QueryOutcome(query=query, error=self._short_error(exc)))
        return outcomes

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
        results, search_error = self._safe_search(query, options.search_limit)
        self._check_stop(stop_event)
        if not results and not self._expired(deadline):
            expanded_query = (
                f"{original_question}\nCurrent objective: {level.objective}\n"
                f"Specific question: {query}"
            )
            expanded, expanded_error = self._safe_search(
                expanded_query, max(options.search_limit, 24)
            )
            results = self._merge_results(results, expanded)
            search_error = search_error or expanded_error
        primary_results = results[: options.min_initial_read_nodes]
        if not primary_results:
            return QueryOutcome(
                query=query, search_result_count=len(results),
                latency_ms=self._elapsed_ms(started), error=search_error,
            )
        retrieved_ids = self._result_node_ids(results)
        query_count = max(1, len(level.queries))
        evidence_chars = options.max_context_chars
        if evidence_chars > 0:
            evidence_chars = max(1, evidence_chars // query_count)
        evidence = self._format_evidence(primary_results, evidence_chars)
        # Keep a broad retrieval net without making synthesis ingest every
        # remaining node body. The compact notes preserve relevant exceptions
        # and prerequisites outside the highest-ranked full documents.
        catalog_chars = max(1_000, 6_000 // query_count)
        catalog = self._format_candidate_catalog(
            results[options.min_initial_read_nodes:], max_chars=catalog_chars
        )
        if catalog:
            evidence = f"{evidence}\n\nAdditional ranked matches:\n{catalog}".strip()
        if not evidence:
            return QueryOutcome(
                query=query, retrieved_node_ids=retrieved_ids,
                search_result_count=len(results),
                latency_ms=self._elapsed_ms(started), error=search_error,
            )
        allowed = self._allowed_ids(retrieved_ids, established_facts)
        established_text = self._format_facts(
            established_facts, max_chars=options.max_established_chars
        )
        try:
            answer = self._llm().complete_structured(
                SYNTHESIS_SYSTEM_PROMPT,
                f"Original user question:\n{original_question}\n\n"
                f"Current level objective:\n{level.objective}\n\n"
                f"Current shallow question:\n{query}\n\n"
                f"Established facts from earlier levels:\n"
                f"{established_text or '(none)'}\n\n"
                f"Source evidence:\n{evidence}",
                ShallowResearchAnswer,
            )
        except Exception as exc:
            log.info("realtime synthesis failed: %s", exc)
            return QueryOutcome(
                query=query,
                retrieved_node_ids=retrieved_ids,
                search_result_count=len(results),
                latency_ms=self._elapsed_ms(started),
                error=self._short_error(exc),
            )
        self._check_stop(stop_event)

        raw_node_ids = getattr(answer, "node_ids", []) or []
        node_ids = self._resolve_ids(raw_node_ids, allowed)
        text = self._clean_fact_text(getattr(answer, "answer", ""), allowed)
        if text and not raw_node_ids and retrieved_ids:
            node_ids = [retrieved_ids[0]]
        return QueryOutcome(
            query=query,
            facts=[ReferencedFact(text=text, node_ids=node_ids)] if text and node_ids else [],
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
    def _format_candidate_catalog(
        results: list[dict[str, Any]], max_chars: int = 0
    ) -> str:
        """Compact notes for ranked sources whose full body is not included."""
        blocks: list[str] = []
        size = 0
        for result in results:
            node = result.get("node")
            node_id = str(getattr(node, "id", "") or "").strip()
            if not node_id:
                continue
            title = " ".join(str(getattr(node, "title", "") or "").split())
            summary = " ".join(str(getattr(node, "summary", "") or "").split())
            source_path = " ".join(
                str(getattr(node, "source_path", "") or "").split()
            )
            source_ranges = getattr(node, "source_ranges", None) or []
            range_text = ", ".join(
                "-".join(str(part) for part in item)
                if isinstance(item, (tuple, list)) else str(item)
                for item in source_ranges
            )
            snippets = [
                " ".join(str(item.get("text") or "").split())
                for item in (result.get("evidence") or [])[:2]
                if str(item.get("text") or "").strip()
            ]
            block = (
                f"node_id: {node_id}\n"
                f"title: {title}\n"
                f"summary: {summary}\n"
                f"matches: {' | '.join(snippets)}\n"
                f"source_path: {source_path}\n"
                f"source_ranges: {range_text}"
            )
            if max_chars > 0 and size + len(block) > max_chars:
                break
            blocks.append(block)
            size += len(block) + 2
        return "\n\n".join(blocks)

    @staticmethod
    def _format_evidence(results: list[dict[str, Any]], max_chars: int) -> str:
        """Render every selected source before spending extra context on one.

        Search ranking frequently puts a long overview ahead of a short page
        containing a required filename or exception.  A first-fit whole-body
        formatter therefore makes the answer shallow: it fills the prompt with
        the first few documents and silently drops the rest.  Reserve an equal
        share of the evidence budget for each selected source, with its ranked
        match snippets first and a document excerpt second.
        """
        entries: list[tuple[str, str, str, list[str], str]] = []
        for result in results:
            node = result.get("node")
            node_id = str(getattr(node, "id", "") or "").strip()
            if not node_id:
                continue
            title = " ".join(str(getattr(node, "title", "") or "").split())
            summary = " ".join(str(getattr(node, "summary", "") or "").split())
            body = " ".join(str(getattr(node, "body", "") or "").split())
            matches = [
                " ".join(str(item.get("text") or "").split())
                for item in (result.get("evidence") or [])[:2]
                if str(item.get("text") or "").strip()
            ]
            if body or matches or summary:
                entries.append((node_id, title, summary, matches, body))

        if not entries:
            return ""

        blocks: list[str] = []
        per_node_chars = max_chars // len(entries) if max_chars > 0 else 0
        for node_id, title, summary, matches, body in entries:
            # Evidence chunks are generated by the retriever specifically for
            # this query, so protect them from being displaced by a long body.
            match_lines = [
                f"match: {snippet[:600]}" for snippet in matches
            ]
            header = f"node_id: {node_id}\ntitle: {title}"
            core = "\n".join([header, *match_lines])
            source = body or summary
            if per_node_chars > 0:
                source = source[: max(0, per_node_chars - len(core) - 12)]
            if source:
                block = f"{core}\ndocument: {source}"
            else:
                block = core
            blocks.append(block)
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
        # A model occasionally turns absent search context into a generic
        # "the supplied material does not say" sentence even when another
        # retrieved snippet does answer the question. This is neither a useful
        # answer section nor a safe inference, so do not stream it.
        cleaned = _UNSUPPORTED_DISCLAIMER_RE.sub("", cleaned)
        lines: list[str] = []
        previous_blank = False
        for raw_line in cleaned.splitlines():
            line = re.sub(r"[ \t]+", " ", raw_line).strip()
            if not line:
                if lines and not previous_blank:
                    lines.append("")
                previous_blank = True
                continue
            lines.append(line)
            previous_blank = False
        return "\n".join(lines).strip()

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
