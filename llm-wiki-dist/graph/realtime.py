"""Realtime RAG for a speaking client: a coworker with a big book.

He does not know the answer by heart, he knows the book. Asked something, he
says what he is about to look up, gives a real answer fast, and keeps reading
while he talks — because he already knows what will be asked next.

That image is the whole design:

===============================  ===========================================
Behaviour                        Mechanism here
===============================  ===========================================
Says what he will look up        ``plan`` is emitted from retrieval alone,
                                 with no model call, at about 0.5 s.
Answers fast, not perfectly      Four shards read four slices of the ranked
                                 set in parallel and are harvested at a
                                 deadline; a slow shard shortens the answer
                                 instead of delaying it.
Keeps reading while talking      The structural subgraph is built from the
                                 database while those shards generate, so it
                                 costs no GPU and no wall clock.
Anticipates the next question    The last stage looks up the terms the first
                                 answer used but never explained.
Never says "not in my notes"     Disclaimer sentences are stripped before any
                                 text reaches speech.
Reads the rest of the page       Chain neighbours travel further than typed
                                 ones, because a list split across two chunks
                                 is invisible to meaning-based ranking.
===============================  ===========================================

Hard rules enforced here rather than in a prompt:

- a cited node ID that was not retrieved, walked to, or already accepted never
  reaches the stream;
- no stage waits for all of its branches: each one harvests what finished by
  its deadline and emits that;
- no generation for a later stage starts before the current level is emitted,
  so deep research cannot delay first audio.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Protocol, Sequence

from pydantic import BaseModel, Field

from .neighborhood import NeighborRef
from .vocab import PINNABLE_KINDS, Vocabulary, VocabularyMatch

log = logging.getLogger("graph_realtime")


# region ports


class RealtimeLlm(Protocol):
    def complete_structured(
        self, system_prompt: str, user_content: str, output_model: type[Any]
    ) -> Any: ...


class SearchPort(Protocol):
    """Hybrid retrieval. ``profile`` carries per-question RRF weight multipliers."""

    def __call__(
        self, text: str, limit: int, profile: dict[str, float] | None = None
    ) -> list[dict[str, Any]]: ...


class RerankPort(Protocol):
    """Cross-encoder over ``(document, payload)`` pairs, best first."""

    def __call__(
        self, query: str, items: Sequence[tuple[str, Any]], k: int
    ) -> list[tuple[Any, float]]: ...


class NeighborPort(Protocol):
    """Structural walk. Database only: it must never touch the GPU."""

    def __call__(
        self,
        node_ids: Sequence[str],
        *,
        chain_hops: int,
        typed_hops: int,
        siblings: bool,
        limit: int,
    ) -> dict[str, list[NeighborRef]]: ...


class LoadNodesPort(Protocol):
    def __call__(self, node_ids: Sequence[str]) -> list[Any]: ...


class DeepAgentPort(Protocol):
    """One bounded research agent over the subgraph, started at ``node_id``."""

    def __call__(
        self,
        *,
        question: str,
        node_id: str,
        sibling_ids: Sequence[str],
        index: int,
        stop_event: threading.Event | None,
        extra_instructions: str = "",
    ) -> dict[str, Any]: ...


# endregion ports

# region models


class PlannedLevel(BaseModel):
    objective: str = ""
    queries: list[str] = Field(default_factory=list)


class LeveledPlan(BaseModel):
    levels: list[PlannedLevel] = Field(default_factory=list)


class ReferencedFact(BaseModel):
    text: str = ""
    node_ids: list[str] = Field(default_factory=list)


class ShallowResearchAnswer(BaseModel):
    """One source-backed answer section. Every shard and stage returns this."""

    answer: str = ""
    node_ids: list[str] = Field(default_factory=list)


# Retained as transport-compatible schemas for callers that imported the
# earlier experimental reader pipeline.
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


class RealtimeStopped(Exception):
    """Raised between bounded pipeline operations after client cancellation."""


@dataclass(frozen=True)
class RealtimeOptions:
    """Every knob the client may set per request.

    The defaults are the demo configuration: first audio at about five seconds,
    then two more levels while the client is still speaking.
    """

    # --- stages -----------------------------------------------------------
    # 1 = fast answer only. 2 adds deep research over the subgraph. 3 adds the
    # anticipation stage.
    max_levels: int = 3

    # --- fast answer ------------------------------------------------------
    shard_count: int = 4
    shard_detail_nodes: int = 5
    shard_wide_nodes: int = 10
    shard_deadline_seconds: float = 90.0

    # --- how much exploring is allowed (speed <-> accuracy) ---------------
    # The minimum is the important one: it guarantees the fast answer always
    # sees some structure beyond flat ranking, even at the fastest setting.
    neighbor_min_admit: int = 2
    neighbor_max_admit: int = 8
    neighbor_hops_fast: int = 1
    neighbor_hops_deep: int = 3
    neighbor_hops_typed: int = 1

    # --- retrieval --------------------------------------------------------
    search_limit: int = 100
    rerank_top_k: int = 30
    max_context_chars: int = 32_000

    # --- deep stage -------------------------------------------------------
    # These are deliberately far below the documentation agent's budgets. That
    # path may spend twenty steps and five reads before it is allowed to
    # answer; here, a gate like that guarantees the stage misses its deadline
    # and contributes nothing.
    subagent_count: int = 3
    subagent_concurrency: int = 3
    subagent_max_steps: int = 5
    subagent_min_reads: int = 1
    subagent_max_reads: int = 4
    # Raised from 18 s for a slower generation endpoint, where all three deep
    # agents were being dropped at the harvest every run and only the plain
    # reader survived. NOTE: this exceeds the 20 s gap between `level` events
    # that the speaking client allows, so the client watchdog has to be raised
    # to match. If it cannot be, lower `subagent_max_steps` instead — making
    # the agents finish sooner keeps the contract that giving them more time
    # breaks. Raised again to the request-schema ceiling for a contended
    # generation endpoint where even the doubled value dropped shards; a
    # deadline is a ceiling, not a fixed wait, so this costs nothing once the
    # endpoint is fast again — put the ceiling back down once it's stable.
    deep_deadline_seconds: float = 120.0
    deep_node_limit: int = 24

    # --- anticipation stage ----------------------------------------------
    anticipation_deadline_seconds: float = 120.0
    anticipation_terms: int = 3

    # --- run --------------------------------------------------------------
    # Wall-clock budget for the whole run. Retrieval and model calls cannot be
    # interrupted once started, so the budget bounds *new* work.
    deadline_seconds: float = 600.0
    emit_discovery: bool = True

    # --- accepted for older clients, no longer meaningful ------------------
    max_queries_per_level: int = 1
    max_recovery_levels: int = 0
    min_search_results: int = 1
    research_seconds_per_query: float = 0.0
    stage_deadline_seconds: float = 12.0
    min_initial_read_nodes: int = 16
    max_established_chars: int = 2_500
    max_search_context_chars: int = 300


def _normalize_options(options: RealtimeOptions) -> RealtimeOptions:
    """Clamp caller-supplied options so a bad value degrades instead of raising."""
    max_admit = max(0, int(options.neighbor_max_admit))
    return replace(
        options,
        max_levels=max(1, min(3, int(options.max_levels))),
        shard_count=max(1, min(8, int(options.shard_count))),
        shard_detail_nodes=max(1, int(options.shard_detail_nodes)),
        shard_wide_nodes=max(1, int(options.shard_wide_nodes)),
        shard_deadline_seconds=max(0.5, float(options.shard_deadline_seconds)),
        neighbor_min_admit=max(0, min(int(options.neighbor_min_admit), max_admit)),
        neighbor_max_admit=max_admit,
        neighbor_hops_fast=max(0, int(options.neighbor_hops_fast)),
        neighbor_hops_deep=max(0, int(options.neighbor_hops_deep)),
        neighbor_hops_typed=max(0, int(options.neighbor_hops_typed)),
        search_limit=max(1, int(options.search_limit)),
        rerank_top_k=max(1, int(options.rerank_top_k)),
        max_context_chars=max(0, int(options.max_context_chars)),
        subagent_count=max(0, int(options.subagent_count)),
        subagent_concurrency=max(1, int(options.subagent_concurrency)),
        subagent_max_steps=max(1, int(options.subagent_max_steps)),
        subagent_min_reads=max(0, int(options.subagent_min_reads)),
        subagent_max_reads=max(1, int(options.subagent_max_reads)),
        deep_deadline_seconds=max(0.0, float(options.deep_deadline_seconds)),
        deep_node_limit=max(1, int(options.deep_node_limit)),
        anticipation_deadline_seconds=max(
            0.0, float(options.anticipation_deadline_seconds)
        ),
        anticipation_terms=max(0, int(options.anticipation_terms)),
        deadline_seconds=max(0.0, float(options.deadline_seconds)),
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


@dataclass
class Retrieval:
    """Everything one wide search produced, before any generation."""

    query: str
    match: VocabularyMatch
    kind: str
    results: list[dict[str, Any]] = field(default_factory=list)
    node_ids: list[str] = field(default_factory=list)
    error: str | None = None
    latency_ms: int = 0


@dataclass
class Shard:
    """One parallel reader over its own slice of the ranked set."""

    id: str
    mode: str  # detail | wide
    results: list[dict[str, Any]]
    neighbors: list[NeighborRef] = field(default_factory=list)


# endregion models

# region question shape

_LIST_HINTS = (
    "どのファイル", "どれが必要", "一覧", "すべて", "全て", "何が必要",
    "必要なファイル", "必要な設定", "リスト", "列挙", "項目は",
    # 「〜の種類」 asks for a set and is the commonest way to do so in Japanese.
    # Without it, "アドバイスの種類と影響" classified as `named` and returned no
    # enums at all, while the same question ending 「…をすべて教えてください」
    # classified as `list` and returned all three. A trailing politeness phrase
    # must not decide whether the answer contains the list that was asked for.
    "種類", "どんな", "どのような", "それぞれ",
    "which files", "what files", "list ", "enumerate", "all of the",
    "kinds of", "types of", "each of",
)
_CONCEPT_HINTS = (
    "とは", "何のため", "目的", "なぜ", "仕組み", "違い", "概要", "役割",
    "what is", "what are", "why ", "purpose", "overview", "difference",
    "how does",
)
_NAMED_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]{3,}|第\s*\d+\s*(?:引数|パラメータ)|\d+\s*番目"
)

# Multipliers applied to the configured RRF field weights. Deliberately not
# filters: a filter throws away material that the answer might still need,
# while a weight only changes what floats to the top.
PROFILE_MULTIPLIERS: dict[str, dict[str, float]] = {
    # A named thing is spelled, not described. Lexical channels and extracted
    # claims know exact spellings; body embeddings dilute them.
    "named": {
        "weight_node_bm25": 1.60,
        "weight_item_bm25": 1.25,
        "weight_claim_vec": 1.20,
        "weight_summary_vec": 0.80,
        "weight_body_vec": 0.70,
    },
    # A concept question wants the page that is *about* the thing.
    "concept": {
        "weight_summary_vec": 1.40,
        "weight_title_vec": 1.20,
        "weight_item_bm25": 0.85,
    },
    # A list question is answered by many small exact items, so widen the pool
    # that carries them as well as its weight.
    "list": {
        "weight_item_bm25": 1.30,
        "weight_claim_vec": 1.25,
        "pool_item_bm25": 1.50,
    },
}


def classify_question(question: str, pinned: Sequence[str] = ()) -> str:
    """named | concept | list, from the question alone. No model call."""
    text = str(question or "").casefold()
    if any(hint.casefold() in text for hint in _LIST_HINTS):
        return "list"
    if pinned or _NAMED_RE.search(str(question or "")):
        return "named"
    if any(hint.casefold() in text for hint in _CONCEPT_HINTS):
        return "concept"
    return "named" if re.search(r"[A-Za-z0-9]", text) else "concept"


def _is_japanese(text: str) -> bool:
    return bool(re.search(r"[぀-ヿ㐀-鿿]", str(text or "")))


# endregion question shape

# region prompts

_SCOPE_RULES = """Answer only what was asked:
- asked for the arguments of a function -> only that function's arguments;
- asked for the Nth argument -> only that argument, of that function;
- asked which files are needed -> as many items of that list as you can support;
- asked what something is -> that thing.
Never widen the subject to a similarly named one."""

_OUTPUT_RULES = """Write a short, direct answer in plain prose, in the language the
user asked in. A client speaks this out loud to someone who is already
listening — a few sentences carrying only the substance, never a document.
No headings, no tables, and no bullet lists unless the source material is
itself a list of items; turn everything else into plain sentences. Do not
pad, and do not restate a point you have already made in different words.

The sources are usually written in another language and translating what
they say is part of the answer, not optional — a client reads this out loud
and cannot switch languages mid-sentence.

Every identifier, API name, enum value, argument name and version number must
appear verbatim in the sources you were given. Copy them; never complete a set
by analogy, and never adjust one to look like its neighbours. If a list of
names is only partly present in your sources, give the part that is there. An
invented name is worse than a short answer, because it cannot be told apart
from a real one.

Do not describe the research process or the evidence. Put every supporting node
ID in node_ids and no ID that was not supplied to you. Never put IDs in the
answer text."""

# Only for the stages whose evidence is on-topic by construction: they were
# handed the results of a search for the user's own question, so "the material
# does not state this" is a shard admitting it drew the wrong slice, not a
# finding. The anticipation stage is different and must not inherit this.
_NO_GAP_TALK = """Do not report what is missing: a sentence saying the material does
not state something is never an acceptable answer and is discarded. If your
slice cannot answer, return the part it does support and nothing else."""

_COMMON_RULES = f"{_OUTPUT_RULES}\n\n{_NO_GAP_TALK}"

DETAIL_SHARD_SYSTEM_PROMPT = f"""You are one of several readers answering a documentation
question at the same time. You were given the highest-ranked sources in full.
Read them properly rather than skimming: exact names, arguments, order,
values, conditions and exceptions are the answer.

{_SCOPE_RULES}

Related nodes are listed with their IDs and summaries. They are context you
have not read, so do not state their contents as fact; cite one only when its
summary itself carries the point.

{_COMMON_RULES}"""

WIDE_SHARD_SYSTEM_PROMPT = f"""You are one of several readers answering a documentation
question at the same time. You were given a wide, shallow view of lower-ranked
sources: summaries, extracted claims, matched snippets and titles. Your job is
coverage — the required item, exception or prerequisite that the top-ranked
pages do not mention.

{_SCOPE_RULES}

{_COMMON_RULES}"""

DEEP_SYSTEM_PROMPT = f"""You are reading the rest of the document while the client is
already speaking the first answer. The nodes below are the structural
neighbourhood of the sources that answer was built from: the chunks before and
after them, their typed links, and the rest of their page.

Report what these add that a first pass over the ranked results would have
missed — the continuation of a list, a condition stated a page later, a
related setting. Do not restate what is already established below.

{_SCOPE_RULES}

{_COMMON_RULES}"""

ANTICIPATION_SYSTEM_PROMPT = f"""The first answer used a term without explaining it, and
that is what will be asked next. Explain that one term from the sources given:
what it is, what values or arguments it takes, and where it is used.

The term was taken out of an answer automatically and the sources were found by
searching for it alone, so both may be wrong. Return an empty answer and no
node IDs when the sources describe a different subject than the user's
question, when they use the term in an unrelated sense, or when they add
nothing to what the term already meant in context. An empty answer is the
correct output here and costs nothing; a plausible-sounding connection between
two unrelated subjects is the one failure this stage can produce.

{_SCOPE_RULES}

{_OUTPUT_RULES}"""

# endregion prompts

# Citation shapes the model tends to emit around IDs: `**node:14**`, `[node:14]`,
# `"node:14".`  Matching is done on a normalized form so a cosmetic wrapper does
# not silently discard an otherwise supported fact.
_ID_EDGE_RE = re.compile(r"^[\s`'\"*<\[(]+|[\s`'\"*>\])]+$")
_ID_TAIL_RE = re.compile(r"[,.;:!?]+$")
_ID_SPLIT_RE = re.compile(r"[\s,;]+")
_BRACKET_RE = re.compile(r"[\[(]([^\[\]()]{1,200})[\])]")
# One whole sentence that points at the material and then denies it. Both
# halves are required and both must sit in the same sentence: a bare negation
# is ordinary technical content ("6.0 未満はオンデマンド移行をサポートしていま
# せん") and must survive, while "資料には…言及されていません" must not reach a
# speaking client. Anchored to `。` so a match can never clip a neighbouring
# sentence, and the leading `提供された` is optional because models drop it.
_UNSUPPORTED_DISCLAIMER_RE = re.compile(
    # 提供された/与えられた is an anchor by itself: whatever noun follows it,
    # the sentence is about the evidence handed to the model rather than about
    # CUDA. Listing nouns alone was whack-a-mole — 資料 was covered and ソース
    # was not, so the same sentence shape leaked twice.
    # 情報 is deliberately not a standalone anchor: "…必要な情報をすべて持って
    # いません" is a real statement about Unified Memory. It is still covered
    # when it follows 提供された/与えられた, which is the disclaiming form.
    r"[^。\n]*?(?:提供された|与えられた|資料|抜粋|文書|ドキュメント"
    r"|コンテキスト|ソース|記載|記述|言及)"
    r"[^。\n]*?(?:ありません|いません|見つかりません|不足しています)。|"
    r"(?:the )?(?:provided|supplied) (?:material|evidence|documentation).{0,140}?"
    r"(?:does not|doesn't|cannot).{0,100}?(?:state|describe|contain|provide).{0,80}?(?:\.|$)",
    re.IGNORECASE,
)
_TERM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,60}|[A-Za-z0-9_]+\.[a-z]{1,4}")


class RealtimePipeline:
    """Executes the staged realtime plan and emits plan/level events."""

    def __init__(
        self,
        *,
        llm_factory: Callable[[], RealtimeLlm],
        search: SearchPort,
        rerank: RerankPort | None = None,
        neighbors: NeighborPort | None = None,
        load_nodes: LoadNodesPort | None = None,
        deep_agent: DeepAgentPort | None = None,
        vocabulary: Vocabulary | None = None,
    ) -> None:
        self._llm_factory = llm_factory
        self._search = search
        # Weight profiles are newer than this port. Decide once whether the
        # wired retrieval accepts one, instead of guessing from a TypeError
        # raised somewhere inside it.
        self._search_takes_profile = _accepts_profile(search)
        self._rerank = rerank
        self._neighbors = neighbors
        self._load_nodes = load_nodes
        self._deep_agent = deep_agent
        self._vocabulary = vocabulary or Vocabulary.empty()
        # One client per worker thread: building a chat client costs a fresh
        # HTTP connection, which is pure latency on the critical path. The
        # pipeline is per-request, so nothing leaks between runs.
        self._local = threading.local()

    # region run

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

        # Room for one stage's branches, the parallel subgraph build, and a
        # straggler that outlived its deadline and still holds a thread.
        workers = (
            max(opts.shard_count, opts.subagent_concurrency, opts.anticipation_terms, 2)
            + 2
        )
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="realtime")
        emit_lock = threading.Lock()

        def safe_emit(event: dict[str, Any]) -> None:
            # Stage threads never emit, but the subgraph builder shares this
            # pipeline, and a client callback is not required to be reentrant.
            with emit_lock:
                emit(event)

        state = _RunState(question=question, options=opts)

        try:
            retrieval = self._retrieve(question, opts, stop_event)
            levels = self._plan(retrieval, opts)
            statuses = {level.id: "pending" for level in levels}
            plan_version = 1
            safe_emit(
                {
                    "type": "plan",
                    "version": plan_version,
                    "question": question,
                    # The plan is data, not a sentence: the client's speaker
                    # model turns it into Japanese. Writing prose here would
                    # cost a generation and be paraphrased away anyway.
                    "planning_fallback": False,
                    "search_query": retrieval.query,
                    "question_type": retrieval.kind,
                    "vocabulary": retrieval.match.public(),
                    "pinned_identifier": retrieval.match.pinned,
                    "candidates": self._candidate_previews(retrieval.results),
                    "levels": self._public_levels(levels, statuses),
                }
            )

            state.allow(retrieval.node_ids)

            # The structural walk is pure SQLite, so it overlaps the fast
            # shards for free. Deep *generation* must not start here: it would
            # compete with those shards for the GPU and delay first audio.
            subgraph_future = pool.submit(
                self._build_subgraph, retrieval, opts, stop_event
            )

            for position, level in enumerate(levels, start=1):
                self._check_stop(stop_event)
                if self._expired(deadline):
                    state.unresolved = True
                    state.incomplete_reason = state.incomplete_reason or "deadline"
                    for pending in levels[position - 1 :]:
                        statuses[pending.id] = "skipped"
                    plan_version += 1
                    safe_emit(
                        {
                            "type": "plan_update",
                            "version": plan_version,
                            "reason": "deadline",
                            "levels": self._public_levels(levels, statuses),
                        }
                    )
                    break

                statuses[level.id] = "running"
                safe_emit(
                    {
                        "type": "level_start",
                        "plan_version": plan_version,
                        "level_id": level.id,
                        "position": position,
                        "objective": level.objective,
                        "queries": list(level.queries),
                    }
                )

                level_started = time.perf_counter()
                if level.kind == "fast":
                    outcomes = self._fast_answer(
                        retrieval, state, opts, pool, stop_event, deadline
                    )
                elif level.kind == "deep":
                    outcomes = self._deep_answer(
                        retrieval,
                        state,
                        opts,
                        pool,
                        subgraph_future,
                        safe_emit,
                        level,
                        stop_event,
                        deadline,
                    )
                else:
                    outcomes = self._anticipate(
                        retrieval, state, opts, pool, safe_emit, level, stop_event, deadline
                    )

                statuses[level.id] = "complete"
                new_facts = state.accept(outcomes)
                safe_emit(
                    self._level_event(
                        level, position, plan_version, outcomes, new_facts, level_started
                    )
                )
                state.levels_completed += 1

            summary = {
                "status": "partial" if state.unresolved else "complete",
                "incomplete_reason": state.incomplete_reason,
                "plan_version": plan_version,
                "levels_completed": state.levels_completed,
                "levels_planned": len(levels),
                "facts": [fact.model_dump() for fact in state.facts],
                "reference_node_ids": self._unique(
                    node_id for fact in state.facts for node_id in fact.node_ids
                ),
                "latency_ms": round((time.perf_counter() - started) * 1000),
            }
            safe_emit({"type": "done", **summary})
            return summary
        finally:
            # Never block on shutdown: a generation that outlived its stage is
            # already ignored, and its own HTTP timeout ends it.
            pool.shutdown(wait=False, cancel_futures=True)

    # endregion run

    # region retrieval and plan

    def _retrieve(
        self,
        question: str,
        options: RealtimeOptions,
        stop_event: threading.Event | None,
    ) -> Retrieval:
        """Repair the words, cast one wide net, rerank it down. No model call."""
        started = time.perf_counter()
        match = self._match_vocabulary(question)
        kind = classify_question(question, match.identifiers)
        profile = PROFILE_MULTIPLIERS.get(kind)

        results, error = self._safe_search(match.query, options.search_limit, profile)
        self._check_stop(stop_event)

        if not results and match.query != question:
            # The repaired spelling is usually better, but never let it be the
            # reason nothing was found.
            results, fallback_error = self._safe_search(
                question, options.search_limit, profile
            )
            error = error or fallback_error

        results = self._rerank_results(match.query, results, options)
        return Retrieval(
            query=match.query,
            match=match,
            kind=kind,
            results=results,
            node_ids=self._result_node_ids(results),
            error=error,
            latency_ms=self._elapsed_ms(started),
        )

    def _match_vocabulary(self, question: str) -> VocabularyMatch:
        try:
            return self._vocabulary.match(question)
        except Exception as exc:  # noqa: BLE001 - repair is an enhancement
            log.info("vocabulary match failed: %s", exc)
            return VocabularyMatch(query=question)

    def _rerank_results(
        self, query: str, results: list[dict[str, Any]], options: RealtimeOptions
    ) -> list[dict[str, Any]]:
        """Wide net in, working set out.

        Hybrid retrieval is tuned for recall and returns things that merely
        share vocabulary with the question. The cross-encoder reads the query
        and the candidate together, which is the only stage that can tell a
        page *about* the subject from a page that mentions it.
        """
        top_k = options.rerank_top_k
        if len(results) <= top_k or self._rerank is None:
            return results[:top_k]

        items: list[tuple[str, int]] = []
        for index, result in enumerate(results):
            text = self._rerank_document(result)
            if text:
                items.append((text, index))

        if not items:
            return results[:top_k]

        try:
            ranked = self._rerank(query, items, top_k)
        except Exception as exc:  # noqa: BLE001 - ranking already exists
            log.info("realtime node rerank failed; keeping RRF order: %s", exc)
            return results[:top_k]

        ordered = [
            results[index]
            for index, _score in ranked
            if isinstance(index, int) and 0 <= index < len(results)
        ]
        seen = {id(result) for result in ordered}
        for result in results:
            if len(ordered) >= top_k:
                break
            if id(result) not in seen:
                ordered.append(result)
        return ordered[:top_k]

    @staticmethod
    def _source_path(result: dict[str, Any]) -> str:
        return _text(getattr(result.get("node"), "source_path", ""))

    @classmethod
    def _source_paths(cls, results: list[dict[str, Any]]) -> set[str]:
        """The documents a set of results came out of."""
        return {path for path in map(cls._source_path, results) if path}

    @classmethod
    def _within(
        cls, results: list[dict[str, Any]], scope: set[str]
    ) -> list[dict[str, Any]]:
        """Results from the documents named by ``scope``.

        A corpus holds more than one book. The word an answer used in one of
        them is usually also defined, in an unrelated sense, in another —
        "System" is a CUDA topology term and an OpenMP memory space, and a
        one-word search cannot tell them apart. Returning nothing is correct
        when the term is not discussed where the answer came from; an empty
        scope (a corpus without source paths) disables the filter instead.
        """
        if not scope:
            return results
        return [result for result in results if cls._source_path(result) in scope]

    @staticmethod
    def _rerank_document(result: dict[str, Any]) -> str:
        node = result.get("node")
        parts = [
            _text(getattr(node, "title", "")),
            _text(getattr(node, "summary", "")),
        ]
        parts.extend(
            _text(item.get("text"))
            for item in (result.get("evidence") or [])[:2]
            if _text(item.get("text"))
        )
        if not any(parts):
            parts.append(_text(getattr(node, "body", ""))[:600])
        return " / ".join(part for part in parts if part)[:1_200]

    def _plan(self, retrieval: Retrieval, options: RealtimeOptions) -> list[RuntimeLevel]:
        """The three stages, fixed. Planning is retrieval, not a generation."""
        japanese = _is_japanese(retrieval.query)
        pinned = retrieval.match.pinned
        subject = pinned or retrieval.query

        stages = [
            (
                "fast",
                f"{subject} を直接答える" if japanese else f"Answer {subject} directly",
                retrieval.query,
            ),
            (
                "deep",
                "資料の続きと関連ノードを読み、詳細を補う"
                if japanese
                else "Read the rest of the document and add what it holds",
                f"{subject} 続き・関連ノード" if japanese else f"{subject} continuation and links",
            ),
            (
                "anticipation",
                "回答で触れた用語を先回りして調べる"
                if japanese
                else "Look up the terms the answer used but did not explain",
                f"{subject} 用語の詳細" if japanese else f"terms mentioned in the {subject} answer",
            ),
        ]

        return [
            RuntimeLevel(
                id=f"level_{index}",
                objective=objective,
                queries=[query],
                kind=kind,
            )
            for index, (kind, objective, query) in enumerate(
                stages[: options.max_levels], start=1
            )
        ]

    # endregion retrieval and plan

    # region fast answer

    def _fast_answer(
        self,
        retrieval: Retrieval,
        state: "_RunState",
        options: RealtimeOptions,
        pool: ThreadPoolExecutor,
        stop_event: threading.Event | None,
        deadline: float | None,
    ) -> list[QueryOutcome]:
        """Four readers over four slices, concatenated — never recompiled.

        One call over everything skims. Four calls each read their own slice
        properly: two go deep on the top ten, two go wide over the rest. The
        client's speaker model rewrites the result into speech anyway, so
        compiling the shards here would cost another generation and add
        nothing.
        """
        shards = self._build_shards(retrieval, options)
        if not shards:
            return []

        for shard in shards:
            state.allow(ref.node_id for ref in shard.neighbors)

        stage_deadline = self._stage_deadline(
            options.shard_deadline_seconds, deadline
        )
        futures = [
            (
                shard,
                pool.submit(
                    self._run_shard, shard, retrieval, state, options, stop_event
                ),
            )
            for shard in shards
        ]
        return self._harvest(futures, stage_deadline, label=lambda shard: shard.id)

    def _build_shards(
        self, retrieval: Retrieval, options: RealtimeOptions
    ) -> list[Shard]:
        results = retrieval.results
        if not results:
            return []

        detail_count = 1 if options.shard_count == 1 else min(2, options.shard_count - 1)
        detail_count = max(1, detail_count)
        specs: list[tuple[str, int]] = [
            ("detail", options.shard_detail_nodes) for _ in range(detail_count)
        ] + [
            ("wide", options.shard_wide_nodes)
            for _ in range(options.shard_count - detail_count)
        ]

        shards: list[Shard] = []
        cursor = 0
        for index, (mode, size) in enumerate(specs, start=1):
            slice_ = results[cursor : cursor + size]
            cursor += size
            if not slice_:
                # A corpus smaller than the plan is normal; do not send an
                # empty prompt just to keep the shard count.
                break
            shards.append(Shard(id=f"shard_{index}", mode=mode, results=slice_))

        if not shards:
            return []

        # The remainder past the last shard is still worth one shard's attention.
        leftover = results[cursor:]
        if leftover:
            shards[-1].results = shards[-1].results + leftover[: options.shard_wide_nodes]

        self._attach_neighbors(shards, options)
        return shards

    def _attach_neighbors(self, shards: list[Shard], options: RealtimeOptions) -> None:
        """Give each shard the best structural neighbours of its own nodes."""
        if self._neighbors is None or options.neighbor_max_admit <= 0:
            return

        seeds = self._unique(
            self._node_id(result) for shard in shards for result in shard.results
        )
        if not seeds:
            return

        walked = self._walk(
            seeds,
            options,
            chain_hops=options.neighbor_hops_fast,
            typed_hops=min(options.neighbor_hops_typed, options.neighbor_hops_fast),
            siblings=False,
            limit=options.neighbor_max_admit,
        )
        if walked is None:
            return

        for shard in shards:
            shard.neighbors = self._admit(shard, walked, options.neighbor_max_admit)

        # `neighbor_min_admit` is a floor, not a preference: a shard that ends
        # up with flat ranking and nothing else is the case this whole stage
        # exists to prevent. Where the cheap walk found too little, spend one
        # more lookup that also considers the rest of each source's page.
        starved = [
            shard for shard in shards if len(shard.neighbors) < options.neighbor_min_admit
        ]
        if not starved:
            return

        wider = self._walk(
            self._unique(
                self._node_id(result) for shard in starved for result in shard.results
            ),
            options,
            chain_hops=max(1, options.neighbor_hops_fast),
            typed_hops=max(1, options.neighbor_hops_typed),
            siblings=True,
            limit=options.neighbor_min_admit,
        )
        if wider is None:
            return

        for shard in starved:
            shard.neighbors = self._admit(
                shard,
                wider,
                options.neighbor_max_admit,
                already=shard.neighbors,
            )

    def _walk(
        self, seeds: list[str], options: RealtimeOptions, **kwargs: Any
    ) -> dict[str, list[NeighborRef]] | None:
        try:
            return self._neighbors(seeds, **kwargs)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - flat ranking still answers
            log.info("realtime neighbour lookup failed: %s", exc)
            return None

    @staticmethod
    def _admit(
        shard: Shard,
        walked: dict[str, list[NeighborRef]],
        ceiling: int,
        already: list[NeighborRef] | None = None,
    ) -> list[NeighborRef]:
        """Round-robin over the shard's own nodes, up to the ceiling.

        Rank by rank rather than seed by seed, so one heavily linked source
        cannot consume the whole admission budget.
        """
        admitted = list(already or [])
        seen = {RealtimePipeline._node_id(result) for result in shard.results}
        seen.update(ref.node_id for ref in admitted)
        per_seed = [
            walked.get(RealtimePipeline._node_id(result), []) for result in shard.results
        ]
        depth = max((len(refs) for refs in per_seed), default=0)
        for rank in range(depth):
            for refs in per_seed:
                if rank >= len(refs) or len(admitted) >= ceiling:
                    continue
                ref = refs[rank]
                if ref.node_id in seen:
                    continue
                seen.add(ref.node_id)
                admitted.append(ref)
            if len(admitted) >= ceiling:
                break
        return admitted

    def _run_shard(
        self,
        shard: Shard,
        retrieval: Retrieval,
        state: "_RunState",
        options: RealtimeOptions,
        stop_event: threading.Event | None,
    ) -> QueryOutcome:
        started = time.perf_counter()
        self._check_stop(stop_event)

        detail = shard.mode == "detail"
        budget = options.max_context_chars
        if budget:
            budget = budget // 2 if detail else budget // 4
        evidence = (
            self._format_evidence(shard.results, budget)
            if detail
            else self._format_wide(shard.results, budget)
        )
        if not evidence:
            return QueryOutcome(
                query=shard.id,
                search_result_count=len(shard.results),
                latency_ms=self._elapsed_ms(started),
            )

        prompt = "\n\n".join(
            part
            for part in (
                f"User question:\n{retrieval.query}",
                self._scope_line(retrieval),
                f"Source evidence:\n{evidence}",
                self._neighbor_block(shard.neighbors),
            )
            if part
        )

        try:
            answer = self._llm().complete_structured(
                DETAIL_SHARD_SYSTEM_PROMPT if detail else WIDE_SHARD_SYSTEM_PROMPT,
                prompt,
                ShallowResearchAnswer,
            )
        except Exception as exc:
            log.info("realtime shard %s failed: %s", shard.id, exc)
            return QueryOutcome(
                query=shard.id,
                retrieved_node_ids=[self._node_id(r) for r in shard.results],
                search_result_count=len(shard.results),
                latency_ms=self._elapsed_ms(started),
                error=self._short_error(exc),
            )

        shard_ids = [self._node_id(result) for result in shard.results]
        return self._outcome(
            shard.id, answer, state, started, shard_ids, len(shard.results)
        )

    # endregion fast answer

    # region deep stage

    def _build_subgraph(
        self,
        retrieval: Retrieval,
        options: RealtimeOptions,
        stop_event: threading.Event | None,
    ) -> list[NeighborRef]:
        """Database-only walk, running while the fast shards generate.

        Chain edges travel three hops and typed edges one. Two chunks of one
        printed page are ranked #3 and #19 by meaning; reading the rest of the
        page is the only thing that recovers the second.
        """
        if self._neighbors is None or options.max_levels < 2:
            return []

        seeds = retrieval.node_ids[: options.rerank_top_k]
        if not seeds:
            return []

        try:
            walked = self._neighbors(
                seeds,
                chain_hops=options.neighbor_hops_deep,
                typed_hops=options.neighbor_hops_typed,
                siblings=True,
                limit=options.neighbor_max_admit,
            )
        except Exception as exc:  # noqa: BLE001 - deep stage degrades to nothing
            log.info("realtime subgraph build failed: %s", exc)
            return []

        if stop_event is not None and stop_event.is_set():
            return []

        known = set(seeds)
        collected: list[NeighborRef] = []
        seen: set[str] = set()
        # Interleave by rank across seeds so the subgraph is not the complete
        # neighbourhood of the top node plus nothing else.
        ordered = list(walked.values())
        for rank in range(options.neighbor_max_admit):
            for refs in ordered:
                if rank >= len(refs):
                    continue
                ref = refs[rank]
                if ref.node_id in known or ref.node_id in seen:
                    continue
                seen.add(ref.node_id)
                collected.append(ref)
        return collected

    def _deep_answer(
        self,
        retrieval: Retrieval,
        state: "_RunState",
        options: RealtimeOptions,
        pool: ThreadPoolExecutor,
        subgraph_future: "Future[list[NeighborRef]]",
        emit: Callable[[dict[str, Any]], None],
        level: RuntimeLevel,
        stop_event: threading.Event | None,
        deadline: float | None,
    ) -> list[QueryOutcome]:
        """Research the subgraph while the client speaks level one."""
        stage_deadline = self._stage_deadline(options.deep_deadline_seconds, deadline)
        try:
            # The walk started before the fast shards and is pure SQLite, so it
            # is normally finished already. Spend at most a third of the stage
            # waiting for it: research over a partial subgraph beats a full
            # subgraph with no time left to read it.
            budget = max(0.0, stage_deadline - time.perf_counter()) / 3
            subgraph = subgraph_future.result(timeout=budget)
        except Exception as exc:  # noqa: BLE001 - includes TimeoutError
            log.info("realtime subgraph unavailable at deep stage: %s", exc)
            subgraph = []

        fresh = [ref for ref in subgraph if not state.is_seen(ref.node_id)][
            : options.deep_node_limit
        ]
        if not fresh:
            return []

        state.allow(ref.node_id for ref in fresh)
        if options.emit_discovery:
            self._emit_discovery(emit, level.id, fresh, state)

        # Agents decide what to read next, which is what makes this stage worth
        # having — but an agent loop is several model turns and can miss the
        # harvest entirely. One plain reader over the same subgraph always runs
        # alongside them: a single generation, reliably inside the stage, so
        # the level is never empty because the exploratory branch ran long.
        agents = (
            self._submit_deep_agents(retrieval, state, fresh, options, pool, stop_event)
            if self._deep_agent is not None
            else []
        )
        readers = self._submit_deep_readers(
            retrieval,
            state,
            fresh,
            options,
            pool,
            stop_event,
            count=1 if agents else options.subagent_count,
        )
        return self._harvest(
            agents + readers, stage_deadline, label=lambda name: str(name)
        )

    def _submit_deep_agents(
        self,
        retrieval: Retrieval,
        state: "_RunState",
        fresh: list[NeighborRef],
        options: RealtimeOptions,
        pool: ThreadPoolExecutor,
        stop_event: threading.Event | None,
    ) -> list[tuple[str, "Future[QueryOutcome]"]]:
        starts = [ref.node_id for ref in fresh[: options.subagent_count]]
        # Without this, each agent reconstructs the fast answer from scratch —
        # same comparison, same table, three times over — because none of them
        # knows level one already said it. `_submit_deep_readers` already gets
        # this; the agent path never did.
        established = self._format_facts(state.facts, 2_000)
        extra_instructions = (
            "既に他の調査で次の内容が確認済みです。重複を避け、"
            "まだ触れられていない具体的な事実だけを追加してください:\n"
            f"{established or '(まだ何も確認されていません)'}\n\n"
            "この回答は音声で読み上げられます。長い文書を作らないでください。"
            "見出し・表・Mermaid図・箇条書きは一切使わず、2〜3文の平易な地の文だけで、"
            "新しく分かった具体的な事実のみを短く述べてください。"
        )

        def run_one(index: int, node_id: str) -> QueryOutcome:
            started = time.perf_counter()
            self._check_stop(stop_event)
            report = self._deep_agent(  # type: ignore[misc]
                question=retrieval.query,
                node_id=node_id,
                sibling_ids=[other for other in starts if other != node_id],
                index=index,
                stop_event=stop_event,
                extra_instructions=extra_instructions,
            )
            # An agent only cites nodes it actually opened, so its citations
            # join the allowed set rather than being filtered against a set
            # built before it ran.
            cited = [str(value) for value in (report.get("cited") or []) if value]
            state.allow(cited)
            # The documentation-agent format mandates a trailing "引用:" section
            # listing node IDs and titles in the answer text itself. `cited`
            # already carries those IDs structurally; speaking the block too
            # would say every reference out loud twice.
            raw_answer = str(report.get("answer") or "")
            answer_text = re.split(r"\n引用[:：]", raw_answer, maxsplit=1)[0].strip()
            answer = ShallowResearchAnswer(answer=answer_text, node_ids=cited)
            return self._outcome(
                f"agent_{index}", answer, state, started, cited, len(cited)
            )

        return [
            (f"agent_{index}", pool.submit(run_one, index, node_id))
            for index, node_id in enumerate(starts, start=1)
        ]

    def _submit_deep_readers(
        self,
        retrieval: Retrieval,
        state: "_RunState",
        fresh: list[NeighborRef],
        options: RealtimeOptions,
        pool: ThreadPoolExecutor,
        stop_event: threading.Event | None,
        count: int | None = None,
    ) -> list[tuple[str, "Future[QueryOutcome]"]]:
        """Readers over slices of the subgraph: one generation each, no loop."""
        wanted = options.subagent_count if count is None else count
        count = max(1, min(wanted or 1, len(fresh)))
        size = max(1, (len(fresh) + count - 1) // count)
        slices = [fresh[start : start + size] for start in range(0, len(fresh), size)]
        established = self._format_facts(state.facts, 2_000)

        def run_one(index: int, refs: list[NeighborRef]) -> QueryOutcome:
            started = time.perf_counter()
            self._check_stop(stop_event)
            evidence = self._format_nodes(
                self._load(ref.node_id for ref in refs),
                refs,
                max(4_000, options.max_context_chars // 4),
            )
            if not evidence:
                return QueryOutcome(
                    query=f"deep_{index}", latency_ms=self._elapsed_ms(started)
                )
            prompt = "\n\n".join(
                part
                for part in (
                    f"User question:\n{retrieval.query}",
                    self._scope_line(retrieval),
                    f"Already established (do not repeat):\n{established or '(none)'}",
                    f"Neighbouring sources:\n{evidence}",
                )
                if part
            )
            try:
                answer = self._llm().complete_structured(
                    DEEP_SYSTEM_PROMPT, prompt, ShallowResearchAnswer
                )
            except Exception as exc:
                log.info("realtime deep reader %s failed: %s", index, exc)
                return QueryOutcome(
                    query=f"deep_{index}",
                    latency_ms=self._elapsed_ms(started),
                    error=self._short_error(exc),
                )
            return self._outcome(
                f"deep_{index}",
                answer,
                state,
                started,
                [ref.node_id for ref in refs],
                len(refs),
            )

        return [
            (f"deep_{index}", pool.submit(run_one, index, refs))
            for index, refs in enumerate(slices[: options.subagent_count or 1], start=1)
        ]

    # endregion deep stage

    # region anticipation

    def _anticipate(
        self,
        retrieval: Retrieval,
        state: "_RunState",
        options: RealtimeOptions,
        pool: ThreadPoolExecutor,
        emit: Callable[[dict[str, Any]], None],
        level: RuntimeLevel,
        stop_event: threading.Event | None,
        deadline: float | None,
    ) -> list[QueryOutcome]:
        """Look up what the answer mentioned but never explained.

        "the third argument is filenum" is not the end of the exchange, it is
        the setup for "what is filenum". The client is still speaking, so the
        answer to that can already exist by the time it is asked.
        """
        terms = self._unexplained_terms(retrieval, state, options.anticipation_terms)
        if not terms:
            return []

        stage_deadline = self._stage_deadline(
            options.anticipation_deadline_seconds, deadline
        )
        profile = PROFILE_MULTIPLIERS.get("named")
        # A bare term is the weakest query the pipeline ever issues: one word,
        # under a lexically weighted profile, with nothing tying it to the
        # subject. Both halves of the fix are here — the subject is appended to
        # the query, and the results are confined to the documents the answer
        # is already being built from.
        anchor = retrieval.match.pinned or (
            retrieval.match.clusters[0] if retrieval.match.clusters else ""
        )
        scope = self._source_paths(retrieval.results)

        def run_one(term: str) -> QueryOutcome:
            started = time.perf_counter()
            self._check_stop(stop_event)
            query = f"{term} {anchor}".strip() if anchor else term
            results, error = self._safe_search(query, options.rerank_top_k, profile)
            results = self._within(results, scope)
            if not results:
                return QueryOutcome(
                    query=term, latency_ms=self._elapsed_ms(started), error=error
                )
            results = results[: options.shard_detail_nodes]
            node_ids = self._result_node_ids(results)
            # A node an earlier stage already read is *not* a reason to skip
            # this generation. A detail shard given five full bodies answers
            # the question it was asked; it routinely leaves a term it used
            # unexplained even though the body defines it, which is the whole
            # premise of this stage. Whether a second focused pass adds
            # anything is a judgement about meaning, so it belongs to the model
            # and to the fact-text dedupe in `_RunState.accept`, not to a
            # node-identity test here. This stage also runs while the client is
            # still speaking, so the call it would have saved is free time.
            state.allow(node_ids)
            evidence = self._format_evidence(
                results, max(4_000, options.max_context_chars // 4)
            )
            if not evidence:
                return QueryOutcome(
                    query=term,
                    retrieved_node_ids=node_ids,
                    latency_ms=self._elapsed_ms(started),
                    error=error,
                )
            prompt = (
                f"Term to explain:\n{term}\n\n"
                f"Original user question:\n{retrieval.query}\n\n"
                f"Source evidence:\n{evidence}"
            )
            try:
                answer = self._llm().complete_structured(
                    ANTICIPATION_SYSTEM_PROMPT, prompt, ShallowResearchAnswer
                )
            except Exception as exc:
                log.info("realtime anticipation for %s failed: %s", term, exc)
                return QueryOutcome(
                    query=term,
                    retrieved_node_ids=node_ids,
                    latency_ms=self._elapsed_ms(started),
                    error=self._short_error(exc),
                )
            return self._outcome(
                term, answer, state, started, node_ids, len(results)
            )

        futures = [(term, pool.submit(run_one, term)) for term in terms]
        outcomes = self._harvest(futures, stage_deadline, label=lambda term: str(term))

        if options.emit_discovery:
            for outcome in outcomes:
                new_ids = [
                    node_id
                    for fact in outcome.facts
                    for node_id in fact.node_ids
                    if not state.is_seen(node_id)
                ]
                if new_ids:
                    emit(
                        {
                            "type": "discovery",
                            "level_id": level.id,
                            "text": self._discovery_text(outcome.query, retrieval),
                            "node_ids": new_ids[:3],
                            "speakable": True,
                        }
                    )
        return outcomes

    def _unexplained_terms(
        self, retrieval: Retrieval, state: "_RunState", limit: int
    ) -> list[str]:
        """Identifiers the answer used that the question did not ask about."""
        if limit <= 0:
            return []

        asked = {
            _fold_term(token)
            for token in _TERM_RE.findall(retrieval.query)
            + list(retrieval.match.identifiers)
        }
        # Membership in the corpus is not the test. Enrichment keywords put
        # ordinary nouns ("System", "Allocated") in the vocabulary alongside
        # `mpf_mfs_open`, and a stage spent looking up a common English word
        # retrieves whichever document in the corpus happens to define it —
        # which is how a CUDA answer acquires a paragraph about OpenMP. Only an
        # identifier or a filename is a thing the user asks a follow-up about.
        gated = bool(self._vocabulary)
        counts: dict[str, int] = {}
        surfaces: dict[str, str] = {}
        for fact in state.facts:
            for token in _TERM_RE.findall(fact.text):
                key = _fold_term(token)
                if len(key) < 4 or key in asked:
                    continue
                if gated and self._vocabulary.kind_of(token) not in PINNABLE_KINDS:
                    continue
                counts[key] = counts.get(key, 0) + 1
                surfaces.setdefault(key, token)

        # Frequency alone ranks backwards. The most repeated term in an answer
        # is its subject, which is already explained; the term mentioned once
        # in passing is the one the user is about to ask about. Shape decides
        # first, and frequency only breaks ties between equally specific names.
        ranked = sorted(
            counts.items(),
            key=lambda item: (_term_shape(surfaces[item[0]]), item[1]),
            reverse=True,
        )
        return [surfaces[key] for key, _count in ranked[:limit]]

    def _emit_discovery(
        self,
        emit: Callable[[dict[str, Any]], None],
        level_id: str,
        refs: list[NeighborRef],
        state: "_RunState",
    ) -> None:
        """Only ever sent for a node ID this run has not mentioned yet.

        Without that rule it is a progress bar, and a speaking client has
        nothing to say about a progress bar.
        """
        for ref in refs[:1]:
            if state.is_mentioned(ref.node_id):
                continue
            state.mention(ref.node_id)
            title = ref.title or ref.node_id
            emit(
                {
                    "type": "discovery",
                    "level_id": level_id,
                    "text": (
                        f"{title} を読んでいます"
                        if _is_japanese(title)
                        else f"Reading {title}"
                    ),
                    "node_ids": [ref.node_id],
                    "speakable": True,
                }
            )

    def _discovery_text(self, term: str, retrieval: Retrieval) -> str:
        return (
            f"{term} について調べています"
            if _is_japanese(retrieval.query)
            else f"Looking up {term}"
        )

    # endregion anticipation

    # region stage plumbing

    def _harvest(
        self,
        futures: list[tuple[Any, "Future[QueryOutcome]"]],
        deadline: float,
        *,
        label: Callable[[Any], str],
    ) -> list[QueryOutcome]:
        """Take what finished, drop what did not. Never wait for all branches.

        Fan-out is a tail-latency shape: four parallel calls finish at the
        slowest of four. A late shard makes the answer shorter, which is
        recoverable; a late shard that blocks the stream is not.
        """
        remaining = max(0.0, deadline - time.perf_counter())
        pending = [future for _key, future in futures]
        done, _unfinished = wait(pending, timeout=remaining)

        outcomes: list[QueryOutcome] = []
        for key, future in futures:
            if future not in done:
                future.cancel()
                log.info("realtime stage dropped %s at deadline", label(key))
                continue
            try:
                outcomes.append(future.result())
            except RealtimeStopped:
                raise
            except Exception as exc:  # noqa: BLE001 - one branch, not the run
                log.info("realtime stage branch %s failed: %s", label(key), exc)
                outcomes.append(
                    QueryOutcome(query=label(key), error=self._short_error(exc))
                )
        return outcomes

    def _outcome(
        self,
        query: str,
        answer: Any,
        state: "_RunState",
        started: float,
        retrieved_ids: list[str],
        result_count: int,
    ) -> QueryOutcome:
        allowed = state.allowed()
        raw_node_ids = getattr(answer, "node_ids", []) or []
        node_ids = self._resolve_ids(raw_node_ids, allowed)
        text = self._clean_fact_text(getattr(answer, "answer", ""), allowed)
        if text and not node_ids and retrieved_ids:
            # An answer written from evidence that forgot to cite is still
            # grounded; attribute it to the slice it was given.
            node_ids = [retrieved_ids[0]]
        return QueryOutcome(
            query=query,
            facts=[ReferencedFact(text=text, node_ids=node_ids)] if text and node_ids else [],
            retrieved_node_ids=retrieved_ids,
            search_result_count=result_count,
            latency_ms=self._elapsed_ms(started),
        )

    def _level_event(
        self,
        level: RuntimeLevel,
        position: int,
        plan_version: int,
        outcomes: list[QueryOutcome],
        new_facts: list[ReferencedFact],
        started: float,
    ) -> dict[str, Any]:
        return {
            "type": "level",
            "plan_version": plan_version,
            "level_id": level.id,
            "position": position,
            "objective": level.objective,
            "kind": level.kind,
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
            # Concatenated, not recompiled: the client's speaker model rewrites
            # this into speech, so a merge pass here would be paid for twice.
            "text": "\n\n".join(
                fact.text.strip() for fact in new_facts if fact.text.strip()
            ),
            "facts": [fact.model_dump() for fact in new_facts],
            "reference_node_ids": self._unique(
                node_id for fact in new_facts for node_id in fact.node_ids
            ),
            "complete": True,
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }

    def _stage_deadline(self, seconds: float, run_deadline: float | None) -> float:
        stage = time.perf_counter() + max(0.0, seconds)
        return min(stage, run_deadline) if run_deadline is not None else stage

    def _safe_search(
        self, text: str, limit: int, profile: dict[str, float] | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Retrieval failure degrades one stage instead of the whole run."""
        try:
            if self._search_takes_profile:
                results = self._search(text, limit, profile)
            else:
                results = self._search(text, limit)  # type: ignore[call-arg]
        except Exception as exc:  # noqa: BLE001
            log.info("realtime search failed: %s", exc)
            return [], self._short_error(exc)
        return list(results or []), None

    def _load(self, node_ids: Iterable[str]) -> list[Any]:
        ids = [node_id for node_id in dict.fromkeys(node_ids) if node_id]
        if not ids or self._load_nodes is None:
            return []
        try:
            return list(self._load_nodes(ids) or [])
        except Exception as exc:  # noqa: BLE001
            log.info("realtime node load failed: %s", exc)
            return []

    def _llm(self) -> RealtimeLlm:
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._llm_factory()
            self._local.client = client
        return client

    # endregion stage plumbing

    # region formatting

    def _scope_line(self, retrieval: Retrieval) -> str:
        """Name the subject in every shard prompt.

        Weight profiles lean retrieval toward the right sources; this is the
        other half. A shard holding a neighbouring function's documentation
        must know it is not allowed to answer with it.
        """
        parts: list[str] = [f"Question type: {retrieval.kind}"]
        if retrieval.match.identifiers:
            parts.append(
                "The question is about these exact identifiers, and no similarly "
                f"named ones: {', '.join(retrieval.match.identifiers[:3])}"
            )
        if retrieval.kind == "list":
            parts.append(
                "This is a list question: enumerate every supported item, and keep "
                "required items primary rather than conditional asides."
            )
        return "Scope:\n" + "\n".join(f"- {part}" for part in parts)

    @staticmethod
    def _neighbor_block(neighbors: list[NeighborRef]) -> str:
        if not neighbors:
            return ""
        lines = "\n".join(ref.line() for ref in neighbors)
        return (
            "Related nodes you have not read (structural neighbours of the sources "
            "above; cite one only if its own summary carries the point):\n" + lines
        )

    @staticmethod
    def _candidate_previews(results: list[dict[str, Any]], limit: int = 8) -> list[dict[str, str]]:
        previews: list[dict[str, str]] = []
        for result in results[:limit]:
            node = result.get("node")
            node_id = _text(getattr(node, "id", ""))
            if not node_id:
                continue
            previews.append(
                {
                    "node_id": node_id,
                    "title": _text(getattr(node, "title", "")),
                    "summary": _text(getattr(node, "summary", ""))[:200],
                }
            )
        return previews

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
    def _format_wide(results: list[dict[str, Any]], max_chars: int = 0) -> str:
        """Every field except the body: summary, claims, keywords, matches.

        A wide shard exists to notice that a source is relevant at all. Bodies
        would blow its prompt budget for material a detail shard already read.
        """
        blocks: list[str] = []
        size = 0
        for result in results:
            node = result.get("node")
            node_id = _text(getattr(node, "id", ""))
            if not node_id:
                continue
            claims = [
                _text(claim) for claim in (getattr(node, "claims", None) or [])[:4]
            ]
            keywords = [
                _text(keyword) for keyword in (getattr(node, "keywords", None) or [])[:8]
            ]
            snippets = [
                _text(item.get("text"))
                for item in (result.get("evidence") or [])[:3]
                if _text(item.get("text"))
            ]
            block = "\n".join(
                line
                for line in (
                    f"node_id: {node_id}",
                    f"title: {_text(getattr(node, 'title', ''))}",
                    f"summary: {_text(getattr(node, 'summary', ''))}",
                    f"claims: {' | '.join(claims)}" if claims else "",
                    f"keywords: {', '.join(keywords)}" if keywords else "",
                    f"matches: {' | '.join(snippet[:400] for snippet in snippets)}"
                    if snippets
                    else "",
                    f"source_path: {_text(getattr(node, 'source_path', ''))}",
                )
                if line
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
        containing a required filename or exception. A first-fit whole-body
        formatter therefore makes the answer shallow: it fills the prompt with
        the first few documents and silently drops the rest. Reserve an equal
        share of the evidence budget for each selected source, with its ranked
        match snippets first and a document excerpt second.
        """
        entries: list[tuple[str, str, str, list[str], str]] = []
        for result in results:
            node = result.get("node")
            node_id = _text(getattr(node, "id", ""))
            if not node_id:
                continue
            title = _text(getattr(node, "title", ""))
            summary = _text(getattr(node, "summary", ""))
            body = _text(getattr(node, "body", ""))
            matches = [
                _text(item.get("text"))
                for item in (result.get("evidence") or [])[:2]
                if _text(item.get("text"))
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
            match_lines = [f"match: {snippet[:600]}" for snippet in matches]
            header = f"node_id: {node_id}\ntitle: {title}"
            core = "\n".join([header, *match_lines])
            source = body or summary
            if per_node_chars > 0:
                source = source[: max(0, per_node_chars - len(core) - 12)]
            blocks.append(f"{core}\ndocument: {source}" if source else core)
        return "\n\n".join(blocks)

    @staticmethod
    def _format_nodes(
        nodes: list[Any], refs: list[NeighborRef], max_chars: int
    ) -> str:
        """Neighbour nodes as evidence, falling back to their stored summary."""
        by_id = {str(getattr(node, "id", "")): node for node in nodes}
        blocks: list[str] = []
        budget = max_chars // max(1, len(refs)) if max_chars > 0 else 0
        for ref in refs:
            node = by_id.get(ref.node_id)
            body = _text(getattr(node, "body", "")) if node is not None else ""
            summary = ref.summary or (
                _text(getattr(node, "summary", "")) if node is not None else ""
            )
            title = ref.title or (
                _text(getattr(node, "title", "")) if node is not None else ""
            )
            source = body or summary
            if not source:
                continue
            if budget > 0:
                source = source[:budget]
            blocks.append(
                f"node_id: {ref.node_id}\ntitle: {title}\n"
                f"relation: {ref.relation} ({ref.label}, {ref.distance} hop)\n"
                f"document: {source}"
            )
        return "\n\n".join(blocks)

    @staticmethod
    def _public_levels(
        levels: list[RuntimeLevel], statuses: dict[str, str]
    ) -> list[dict[str, Any]]:
        return [
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
            for index, level in enumerate(levels)
        ]

    # endregion formatting

    # region validation helpers

    @staticmethod
    def _node_id(result: dict[str, Any]) -> str:
        return _text(getattr(result.get("node"), "id", ""))

    @staticmethod
    def _result_node_ids(results: list[dict[str, Any]]) -> list[str]:
        return RealtimePipeline._unique(
            RealtimePipeline._node_id(result)
            for result in results
            if RealtimePipeline._node_id(result)
        )

    @staticmethod
    def _normalize_id(value: str) -> str:
        text = _ID_EDGE_RE.sub("", str(value or "").strip())
        return _ID_TAIL_RE.sub("", text).strip().casefold()

    @staticmethod
    def _resolve_ids(values: Iterable[str], allowed: dict[str, str]) -> list[str]:
        """Keep only citations that resolve to a node this run actually holds.

        Matching is done on a normalized form, and a value holding several IDs
        (``"node:1, node:2"``) is split, so a formatting quirk does not throw
        away a fact the evidence supports. Anything that still does not resolve
        is dropped: an unverifiable reference is exactly what must never reach
        the speaker.
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
        parenthetical content survives. A TTS client should never read a node
        ID out loud.
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
    def _unique(values) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value or value in seen:
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

    # endregion validation helpers


@dataclass
class _RunState:
    """Everything one run accumulates. Shared across stage threads."""

    question: str
    options: RealtimeOptions
    facts: list[ReferencedFact] = field(default_factory=list)
    levels_completed: int = 0
    unresolved: bool = False
    incomplete_reason: str | None = None
    _allowed: dict[str, str] = field(default_factory=dict)
    _seen: set[str] = field(default_factory=set)
    _mentioned: set[str] = field(default_factory=set)
    _fact_keys: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, node_ids: Iterable[str]) -> None:
        """Register nodes this run holds, so citing them can be verified."""
        with self._lock:
            for node_id in node_ids:
                node_id = str(node_id or "").strip()
                if node_id:
                    self._allowed.setdefault(
                        RealtimePipeline._normalize_id(node_id), node_id
                    )

    def allowed(self) -> dict[str, str]:
        with self._lock:
            return dict(self._allowed)

    def is_seen(self, node_id: str) -> bool:
        """True once a node has been read by an earlier stage."""
        with self._lock:
            return node_id in self._seen

    def is_mentioned(self, node_id: str) -> bool:
        with self._lock:
            return node_id in self._mentioned

    def mention(self, node_id: str) -> None:
        with self._lock:
            self._mentioned.add(node_id)

    def accept(self, outcomes: list[QueryOutcome]) -> list[ReferencedFact]:
        """Deduplicate, record, and return the facts a level actually adds."""
        new_facts: list[ReferencedFact] = []
        with self._lock:
            for outcome in outcomes:
                self._seen.update(outcome.retrieved_node_ids)
                for fact in outcome.facts:
                    key = RealtimePipeline._fact_key(fact.text)
                    if not key or key in self._fact_keys:
                        continue
                    self._fact_keys.add(key)
                    new_facts.append(fact)
                    self._seen.update(fact.node_ids)
                    self._mentioned.update(fact.node_ids)
        self.facts.extend(new_facts)
        return new_facts


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _fold_term(value: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", str(value or "").casefold())


def _term_shape(token: str) -> int:
    """How strongly a term's spelling marks it as a name rather than a word.

    Ranks anticipation candidates. `mpf_mfs_open` and `pmf_prg.txt` are things;
    `cudaMallocManaged` is a thing; `PCIe` is a word that happens to be
    capitalised. Higher is more specific.
    """
    if "_" in token or "." in token:
        return 3
    if re.search(r"[a-z][A-Z]", token):
        return 2
    if any(ch.isdigit() for ch in token):
        return 1
    return 0


def _accepts_profile(search: Any) -> bool:
    """Whether a wired search callable takes the weight-profile argument."""
    import inspect

    try:
        signature = inspect.signature(search)
    except (TypeError, ValueError):
        return True
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind is parameter.VAR_POSITIONAL or parameter.kind is parameter.VAR_KEYWORD:
            return True
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD):
            positional += 1
        elif parameter.kind is parameter.KEYWORD_ONLY and parameter.name == "profile":
            return True
    return positional >= 3
