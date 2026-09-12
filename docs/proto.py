from __future__ import annotations

"""Fast, conservative knowledge-gap question generation.

This is intentionally not a general research agent. It performs three bounded
model calls:

1. identify source fact-pairs that naturally interact;
2. turn only those pairs into a few focused questions;
3. review all proposals together after targeted retrieval.

The source text and graph do the expensive discovery work.  The model only
compares short passages.  In particular, this prototype never calls
``Researcher.ask()``: asking a multi-agent answerer to prove that an unanswered
question is unanswered was the main source of very long, timeout-prone runs.

Run: ``python proto.py``
"""

import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

# region Wiring and configuration

WIKI_ROOT = Path(__file__).parent / "llm-wiki-dist"
sys.path.insert(0, str(WIKI_ROOT))

from graph.core import Settings
from graph.gateway import ModelGateway
from graph.researcher import Researcher
from graph.store import GraphStore
from langchain_openai import ChatOpenAI


def env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() not in {"0", "false", "no", "off", ""}


WIKI_DB_PATH = os.environ.get(
    "WIKI_DB", str(WIKI_ROOT / ".wiki" / "moove_wiki.sqlite")
)

# These defaults deliberately put a hard upper bound on a bad model call.
LLM_TIMEOUT_SECONDS = env_int("GAP_LLM_TIMEOUT_SECONDS", 90)
OVERALL_TIMEOUT_SECONDS = env_int("GAP_OVERALL_TIMEOUT_SECONDS", 300)
LLM_MAX_OUTPUT_TOKENS = env_int("GAP_LLM_MAX_OUTPUT_TOKENS", 4096)
MAX_RETRIES = env_int("GAP_LLM_ATTEMPTS", 1)
ENABLE_THINKING = env_bool("GAP_ENABLE_THINKING", True)

REGION_SEARCH_LIMIT = env_int("GAP_REGION_SEARCH_LIMIT", 6)
REGION_MAX_NODES = env_int("GAP_REGION_MAX_NODES", 14)
REGION_NEIGHBORS_PER_SEED = env_int("GAP_REGION_NEIGHBORS_PER_SEED", 4)
NODE_BODY_CHARS = env_int("GAP_NODE_BODY_CHARS", 900)

MAX_CANDIDATES = env_int("GAP_MAX_CANDIDATES", 6)
MAX_FACT_PAIRS = env_int("GAP_MAX_FACT_PAIRS", 8)
MAX_FINAL_QUESTIONS = env_int("GAP_MAX_FINAL_QUESTIONS", 5)
VALIDATION_QUERIES_PER_CANDIDATE = env_int("GAP_VALIDATION_QUERIES", 2)
VALIDATION_SEARCH_LIMIT = env_int("GAP_VALIDATION_SEARCH_LIMIT", 4)
SEARCH_CONCURRENCY = env_int("GAP_SEARCH_CONCURRENCY", 3)

FINAL_TXT_PATH = Path("gap_questions_final.txt")
AUDIT_JSON_PATH = Path("gap_questions_audit.json")


SEED_CHUNK = """
3.6. プロセス起動／終了管理

本ソフトウェアは，以下に示すプロセス起動／終了管理を行う。

親プロセスは，本ソフトウェアの「子プロセス起動関数」，「子プロセス終了要求関数」を使用し，子プロセスの起動又は終了を要求することができる。
3.6.1 プログラムの定義

本ソフトウェアのプロセス起動管理機能により起動されるプロセスのプログラムはプログラム定義ファイルに定義されていなければならない。このファイルでは，プログラムごとに以下の定義内容を持つ。

• プログラム名称

• プロセス種別

親プロセス(pp)，イベント処理子プロセス(ep)，メッセージ受信子プロセス(mp)，

セマフォ処理プロセス(sp)，単純起動型子プロセス(fp)

※各種別の使用できる機能に関しては，3.1.9を参照のこと。

• デーモン／非デーモン

• 常駐／非常駐

• リアルタイムプライオリティ（0～31またはTSSプライオリティ）

• 最大起動可能プロセス数

親プロセス： この値が1よりも大きい場合にはコピープロセス生成可

子プロセス： 多重起動可能プロセス数

• ネットワーク上のロケーション指定（計算機種別）

• イベント／メッセージキューバッファサイズ

• エラー処理コード（現在未処理）

F

• プロセス異常終了時に重故障／軽故障のどちらで扱うかどうかのフラグ

• 消費CPU時間監視を行うかどうかのフラグ

プログラム定義ファイルは，オンライン運転時は，バイナリ形式のデータに変換され，共有メモリ上にテーブルとして保持される。
"""


@dataclass
class Runtime:
    settings: Settings
    researcher: Researcher
    llm: ChatOpenAI


def build_runtime() -> Runtime:
    """Build clients lazily so importing this module stays cheap and testable."""
    settings = Settings.from_env()
    gateway = ModelGateway(settings)
    store = GraphStore(WIKI_DB_PATH, readonly=True)
    researcher = Researcher(gateway, store)
    llm = ChatOpenAI(
        model=settings.chat_model,
        base_url=settings.chat_base_url,
        api_key=settings.chat_api_key,
        temperature=0.1,
        timeout=LLM_TIMEOUT_SECONDS,
        max_retries=0,
        max_completion_tokens=LLM_MAX_OUTPUT_TOKENS,
        # Keep reasoning quality, while bounding each decomposed task by both
        # an output-token cap and a wall-clock deadline.
        extra_body={"chat_template_kwargs": {"enable_thinking": ENABLE_THINKING}},
    )
    return Runtime(settings=settings, researcher=researcher, llm=llm)


# endregion Wiring and configuration


# region Schemas

GapKind = Literal[
    "missing_link",
    "unreconciled_conflict",
    "undocumented_contract",
    "missing_boundary",
    "unclear_lifecycle",
    "version_or_scope_gap",
]


class GroundedPremise(BaseModel):
    node_id: str
    claim: str = Field(min_length=4, max_length=240)
    quote: str = Field(
        min_length=4,
        max_length=160,
        description="A short, verbatim quote copied from the supplied node",
    )


class FactPair(BaseModel):
    """Two documented facts that naturally meet in a real workflow."""

    premise_a: GroundedPremise
    premise_b: GroundedPremise
    relationship_to_check: Literal[
        "ordering",
        "state_transition",
        "ownership",
        "configuration_interaction",
        "limit_or_priority",
        "error_propagation",
        "version_or_scope",
        "other_contract",
    ]
    realistic_trigger: str = Field(min_length=4, max_length=240)
    reason_to_compare: str = Field(min_length=4, max_length=240)


class FactPairList(BaseModel):
    pairs: list[FactPair] = Field(default_factory=list)


class QuestionDraft(BaseModel):
    pair_index: int
    question: str = Field(min_length=8, max_length=240)
    gap_kind: GapKind
    missing_structure: str = Field(min_length=4, max_length=300)
    why_it_matters: str = Field(min_length=4, max_length=300)
    resolution_test: str = Field(min_length=4, max_length=300)
    alternate_phrasings: list[str] = Field(default_factory=list, max_length=2)


class QuestionDraftList(BaseModel):
    drafts: list[QuestionDraft] = Field(default_factory=list)


class GapCandidate(BaseModel):
    question: str = Field(min_length=8, max_length=240)
    gap_kind: GapKind
    premises: list[GroundedPremise] = Field(default_factory=list)
    missing_structure: str = Field(min_length=4, max_length=300)
    why_it_matters: str = Field(min_length=4, max_length=300)
    resolution_test: str = Field(min_length=4, max_length=300)
    alternate_phrasings: list[str] = Field(default_factory=list, max_length=2)


class GapCandidateList(BaseModel):
    candidates: list[GapCandidate] = Field(default_factory=list)


class CandidateReview(BaseModel):
    candidate_index: int
    premises_supported: bool
    answer_found: bool
    contrived_or_unlikely: bool
    useful_in_normal_operation: bool
    requires_cross_source_reasoning: bool
    duplicate_of: int | None = None
    impact: int = Field(ge=1, le=5)
    non_triviality: int = Field(ge=1, le=5)
    actionability: int = Field(ge=1, le=5)
    note: str = Field(default="", max_length=300)


class CandidateReviewList(BaseModel):
    reviews: list[CandidateReview] = Field(default_factory=list)


class GapCertificate(BaseModel):
    question: str
    gap_kind: GapKind
    grounded_premises: list[GroundedPremise]
    missing_structure: str
    why_the_gap_matters: str
    corpus_search_performed: list[str]
    evidence_of_absence: str
    resolution_test: str
    review: CandidateReview

    @property
    def priority(self) -> int:
        # A multiplicative score prevents one impressive dimension from hiding
        # a useless or trivial question.
        return (
            self.review.impact
            * self.review.non_triviality
            * self.review.actionability
        )


@dataclass
class Region:
    nodes: list[Any]
    edges: list[Any]


@dataclass
class RetrievedCandidate:
    candidate: GapCandidate
    queries: list[str]
    hits: list[Any]


# endregion Schemas


# region Small helpers

async def structured_llm_call(
    runtime: Runtime,
    output_schema: type[BaseModel],
    system_prompt: str,
    user_prompt: str,
) -> BaseModel:
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(
                f"[LLM] {output_schema.__name__} "
                f"attempt={attempt}/{MAX_RETRIES}, timeout={LLM_TIMEOUT_SECONDS}s"
            )

            async def invoke() -> BaseModel:
                structured = runtime.llm.with_structured_output(
                    output_schema, method="json_schema"
                )
                result = await structured.ainvoke(
                    [("system", system_prompt), ("human", user_prompt)]
                )
                if isinstance(result, output_schema):
                    return result
                return output_schema.model_validate(result)

            return await asyncio.wait_for(invoke(), timeout=LLM_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - transport/schema/timeout failures
            last_error = exc
            print(f"[LLM] {output_schema.__name__} failed: {exc!r}")

    raise RuntimeError(
        f"{output_schema.__name__} failed after {MAX_RETRIES} attempt(s): "
        f"{last_error!r}"
    )


def node_brief(node: Any, body_chars: int = NODE_BODY_CHARS) -> dict[str, Any]:
    return {
        "node_id": node.id,
        "title": node.title or node.entity or node.id,
        "document": node.original_document_name or node.source_path or "",
        "summary": node.summary,
        "claims": node.claims[:5],
        "body": (node.body or "")[:body_chars],
    }


def edge_brief(edge: Any) -> dict[str, str]:
    return {
        "source": edge.source_node_id,
        "relation": edge.label,
        "target": edge.target_node_id,
        "summary": edge.summary,
    }


def normalize_for_match(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[\s「」『』\"'`]+", "", value)


def node_text(node: Any) -> str:
    return "\n".join(
        [
            node.title or "",
            node.entity or "",
            node.summary or "",
            *(node.claims or []),
            node.body or "",
        ]
    )


def validate_premises(
    premises: list[GroundedPremise], nodes_by_id: dict[str, Any]
) -> tuple[list[GroundedPremise] | None, str]:
    """Reject invented IDs/quotes before spending more model time."""
    unique: list[GroundedPremise] = []
    seen: set[str] = set()
    for premise in premises:
        if premise.node_id in seen:
            continue
        node = nodes_by_id.get(premise.node_id)
        if node is None:
            return None, f"unknown premise node: {premise.node_id}"
        quote = normalize_for_match(premise.quote)
        if len(quote) < 4 or quote not in normalize_for_match(node_text(node)):
            return None, f"quote not found in {premise.node_id}: {premise.quote[:60]}"
        seen.add(premise.node_id)
        unique.append(premise)

    if len(unique) < 2:
        return None, "fewer than two distinct grounded nodes"
    return unique[:3], ""


def ground_candidate(
    candidate: GapCandidate, nodes_by_id: dict[str, Any]
) -> tuple[GapCandidate | None, str]:
    grounded, reason = validate_premises(candidate.premises, nodes_by_id)
    if grounded is None:
        return None, reason
    if not all(
        value.strip()
        for value in (
            candidate.question,
            candidate.missing_structure,
            candidate.why_it_matters,
            candidate.resolution_test,
        )
    ):
        return None, "required explanation is blank"

    return candidate.model_copy(update={"premises": grounded}), ""


def ground_pair(
    pair: FactPair, nodes_by_id: dict[str, Any]
) -> tuple[FactPair | None, str]:
    grounded, reason = validate_premises(
        [pair.premise_a, pair.premise_b], nodes_by_id
    )
    if grounded is None:
        return None, reason
    if not pair.realistic_trigger.strip() or not pair.reason_to_compare.strip():
        return None, "pair lacks a realistic trigger or comparison reason"
    return pair.model_copy(
        update={"premise_a": grounded[0], "premise_b": grounded[1]}
    ), ""


def review_passes(review: CandidateReview) -> bool:
    return (
        review.premises_supported
        and not review.answer_found
        and not review.contrived_or_unlikely
        and review.useful_in_normal_operation
        and review.requires_cross_source_reasoning
        and review.duplicate_of is None
        and review.impact >= 3
        and review.non_triviality >= 3
        and review.actionability >= 3
    )


def save_audit(audit: dict[str, Any]) -> None:
    AUDIT_JSON_PATH.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# endregion Small helpers


# region Stage 0: retrieve a compact connected region

async def seed_region(runtime: Runtime) -> Region:
    print("\n[STAGE 0] retrieving a compact graph region")
    seeds = await runtime.researcher.search(
        SEED_CHUNK[:1200], REGION_SEARCH_LIMIT
    )
    nodes_by_id: dict[str, Any] = {node.id: node for node in seeds}
    edges_by_id: dict[str, Any] = {}

    # One hop gives the generator real relationships without turning the prompt
    # into a whole-corpus research task.
    for seed in seeds:
        if len(nodes_by_id) >= REGION_MAX_NODES:
            break
        try:
            pairs = await runtime.researcher.follow_link(
                seed.id,
                direction="both",
                limit=REGION_NEIGHBORS_PER_SEED,
            )
        except Exception as exc:  # noqa: BLE001 - a broken edge must not kill seeding
            print(f"[STAGE 0] follow_link({seed.id}) failed: {exc!r}")
            continue
        for edge, neighbor in pairs:
            edges_by_id[edge.id] = edge
            if len(nodes_by_id) < REGION_MAX_NODES:
                nodes_by_id.setdefault(neighbor.id, neighbor)

    region = Region(
        nodes=list(nodes_by_id.values())[:REGION_MAX_NODES],
        edges=list(edges_by_id.values()),
    )
    print(
        f"[STAGE 0] {len(region.nodes)} nodes, {len(region.edges)} connecting edges"
    )
    return region


# endregion Stage 0


# region Stage 1: decompose discovery into two small reasoning calls

PAIR_SYSTEM = """
あなたの作業は「比較する価値がある事実のペアを選ぶ」ことだけです。
質問を作らず、ギャップがあるとも判定せず、答えも考えないでください。

良いペア:
- 異なる2ノードに実際に書かれた事実である。
- 同じ通常ワークフロー、設定反映、状態遷移、資源所有、優先順位、上限、
  エラー伝播、またはバージョン境界で自然に接触する。
- 両者を一緒に確認する現実的な契機を、提示文から説明できる。

悪いペア:
- 同じ話題というだけで、運用上は接触しない。
- 文書にない故障、入力、機能を発明しないと接触しない。
- 一方が単なる用語説明である。

premise_a.quote と premise_b.quote は提示された各ノードから短く正確にコピーし、
node_id は一字も変更しないでください。良いペアがなければ pairs=[] とします。
"""


QUESTION_SYSTEM = """
あなたの作業は、既に選別された事実ペアを「狭く答えられる実務質問」に変換する
ことだけです。新しい根拠やシナリオを追加せず、質問への回答も考えないでください。

各ペアについて:
- ペア内の記述だけで関係が直接回答済みなら、質問を作らない。
- 欠けている順序、状態遷移、所有権、設定の相互作用、優先順位、上限、
  エラー伝播、適用範囲のうち、一つだけを質問する。
- 通常運用または文書に明記された異常経路で自然に必要となる問いにする。
- 専門家が追加仕様または横断確認により1〜3段落で答えられる具体的な範囲にする。
- 答えが実装、設定、運用、保守の判断を変えないなら作らない。

禁止:
- 「書かれていない」だけの奇妙な仮定や、起こりそうにない端ケース。
- 用語説明、一覧、要約、製品への新機能要求。
- 全ケースの列挙、形式証明、原因の完全網羅など、範囲が無制限な問い。

pair_index は入力番号をそのまま使います。1ペアにつき最大1問、全体で指定上限まで。
alternate_phrasings は検索用の短い語句を最大2件にしてください。
"""


async def find_fact_pairs(
    runtime: Runtime, region: Region
) -> tuple[list[FactPair], list[dict[str, Any]]]:
    print("\n[STAGE 1A] finding facts that naturally interact (one LLM call)")
    user_prompt = f"""
出発点となる節:
{SEED_CHUNK[:1400]}

参照可能なノード:
{json.dumps([node_brief(node) for node in region.nodes], ensure_ascii=False)}

実在するグラフ接続:
{json.dumps([edge_brief(edge) for edge in region.edges], ensure_ascii=False)}

最大 {MAX_FACT_PAIRS} ペアを選んでください。件数を埋める必要はありません。
"""
    result = await structured_llm_call(
        runtime, FactPairList, PAIR_SYSTEM, user_prompt
    )

    nodes_by_id = {node.id: node for node in region.nodes}
    pairs: list[FactPair] = []
    rejected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for pair in result.pairs[:MAX_FACT_PAIRS]:  # type: ignore[attr-defined]
        grounded, reason = ground_pair(pair, nodes_by_id)
        pair_key = (
            min(pair.premise_a.node_id, pair.premise_b.node_id),
            max(pair.premise_a.node_id, pair.premise_b.node_id),
            pair.relationship_to_check,
        )
        if grounded is None:
            rejected.append({"pair": pair.model_dump(), "reason": reason})
            print(f"[STAGE 1A][DROP] {reason}")
        elif pair_key in seen:
            rejected.append({"pair": pair.model_dump(), "reason": "duplicate pair"})
        else:
            seen.add(pair_key)
            pairs.append(grounded)

    print(f"[STAGE 1A] grounded fact pairs: {len(pairs)}")
    return pairs, rejected


async def formulate_questions(
    runtime: Runtime, pairs: list[FactPair]
) -> tuple[list[GapCandidate], list[dict[str, Any]]]:
    print("[STAGE 1B] formulating focused questions (one LLM call)")
    numbered_pairs = [
        {"pair_index": index, **pair.model_dump()}
        for index, pair in enumerate(pairs, start=1)
    ]
    result = await structured_llm_call(
        runtime,
        QuestionDraftList,
        QUESTION_SYSTEM,
        (
            f"事実ペア:\n{json.dumps(numbered_pairs, ensure_ascii=False)}\n\n"
            f"最大 {MAX_CANDIDATES} 問を返してください。"
        ),
    )

    candidates: list[GapCandidate] = []
    rejected: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    seen_pairs: set[int] = set()
    for draft in result.drafts:  # type: ignore[attr-defined]
        if len(candidates) >= MAX_CANDIDATES:
            break
        if not 1 <= draft.pair_index <= len(pairs):
            rejected.append({"draft": draft.model_dump(), "reason": "invalid pair index"})
            continue
        if draft.pair_index in seen_pairs:
            rejected.append({"draft": draft.model_dump(), "reason": "second question for pair"})
            continue
        key = normalize_for_match(draft.question)
        if not key or key in seen_questions:
            rejected.append({"draft": draft.model_dump(), "reason": "duplicate or blank question"})
            continue

        pair = pairs[draft.pair_index - 1]
        candidate = GapCandidate(
            question=draft.question,
            gap_kind=draft.gap_kind,
            premises=[pair.premise_a, pair.premise_b],
            missing_structure=draft.missing_structure,
            why_it_matters=draft.why_it_matters,
            resolution_test=draft.resolution_test,
            alternate_phrasings=draft.alternate_phrasings[:2],
        )
        seen_pairs.add(draft.pair_index)
        seen_questions.add(key)
        candidates.append(candidate)
        print(f"[STAGE 1B][CANDIDATE] {candidate.question}")

    print(f"[STAGE 1B] candidates: {len(candidates)}")
    return candidates, rejected


async def generate_candidates(
    runtime: Runtime, region: Region
) -> tuple[list[GapCandidate], list[dict[str, Any]]]:
    pairs, pair_rejections = await find_fact_pairs(runtime, region)
    if not pairs:
        return [], pair_rejections
    candidates, draft_rejections = await formulate_questions(runtime, pairs)
    return candidates, [*pair_rejections, *draft_rejections]


# endregion Stage 1


# region Stage 2: retrieval plus one batch review call

async def retrieve_for_candidate(
    runtime: Runtime,
    candidate: GapCandidate,
    semaphore: asyncio.Semaphore,
) -> RetrievedCandidate:
    raw_queries = [candidate.question, *candidate.alternate_phrasings]
    queries: list[str] = []
    for query in raw_queries:
        query = query.strip()
        if query and query not in queries:
            queries.append(query)
        if len(queries) >= VALIDATION_QUERIES_PER_CANDIDATE:
            break

    hits_by_id: dict[str, Any] = {}
    for query in queries:
        try:
            async with semaphore:
                hits = await runtime.researcher.search(
                    query, VALIDATION_SEARCH_LIMIT
                )
        except Exception as exc:  # noqa: BLE001 - keep other validation searches useful
            print(f"[STAGE 2] search failed for {query[:50]!r}: {exc!r}")
            continue
        for hit in hits:
            hits_by_id.setdefault(hit.id, hit)

    return RetrievedCandidate(
        candidate=candidate,
        queries=queries,
        hits=list(hits_by_id.values())[:VALIDATION_SEARCH_LIMIT],
    )


REVIEW_SYSTEM = """
あなたは、技術文書から作られた質問候補をまとめて選別する保守的な編集者です。
質問への回答を長く考えたり、新しいシナリオを発明したりしないでください。
各候補と検索結果を比較する分類作業だけを行います。

判定規則:
- premises_supported: 引用された2つ以上の事実から、指摘された関係の確認が自然に必要か。
- answer_found: 検索結果に、質問の核心へ直接かつ十分に答える記述がある場合のみ true。
- contrived_or_unlikely: 文書にない任意の故障・入力・組合せを持ち込み、単に「その場合は
  書かれていない」と問うだけなら true。通常運用で自然に発生する組合せは false。
- useful_in_normal_operation: 答えが実装、設定、運用、障害対応、保守の具体的判断を変えるか。
- requires_cross_source_reasoning: 1ノードの単純参照ではなく、複数記述の関係確認が必要か。
- duplicate_of: 同じ核心の以前の候補番号。重複でなければ null。

重要:
- 検索で答えが無いことは、世界で未知という意味ではない。
- 難しいほど高得点ではない。具体的で狭く、答えられる質問を高く評価する。
- 「何が起きてもよい」「全ケースを列挙せよ」のように回答範囲が無制限なら低評価。
- note は採否の根拠を1文だけで書く。
- 入力された全候補について、candidate_index を変えずに1件ずつ返す。
"""


async def review_candidates(
    runtime: Runtime, candidates: list[GapCandidate]
) -> tuple[list[GapCertificate], list[dict[str, Any]]]:
    print("\n[STAGE 2] targeted retrieval")
    semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)
    retrieved = await asyncio.gather(
        *(
            retrieve_for_candidate(runtime, candidate, semaphore)
            for candidate in candidates
        )
    )

    review_input = []
    for index, item in enumerate(retrieved, start=1):
        review_input.append(
            {
                "candidate_index": index,
                "candidate": item.candidate.model_dump(),
                "search_queries": item.queries,
                "retrieved_passages": [
                    node_brief(hit, body_chars=550) for hit in item.hits
                ],
            }
        )

    print("[STAGE 2] reviewing every candidate together (one LLM call)")
    result = await structured_llm_call(
        runtime,
        CandidateReviewList,
        REVIEW_SYSTEM,
        json.dumps(review_input, ensure_ascii=False),
    )
    review_by_index = {
        review.candidate_index: review
        for review in result.reviews  # type: ignore[attr-defined]
        if 1 <= review.candidate_index <= len(retrieved)
    }

    certificates: list[GapCertificate] = []
    audit_rows: list[dict[str, Any]] = []
    for index, item in enumerate(retrieved, start=1):
        review = review_by_index.get(index)
        row: dict[str, Any] = {
            "candidate_index": index,
            "candidate": item.candidate.model_dump(),
            "queries": item.queries,
            "hit_node_ids": [hit.id for hit in item.hits],
            "review": review.model_dump() if review else None,
            "kept": False,
        }
        if review is None:
            row["reject_reason"] = "reviewer omitted candidate"
        elif not review_passes(review):
            row["reject_reason"] = review.note or "failed conservative gate"
            print(f"[STAGE 2][DROP] {item.candidate.question}")
        else:
            certificate = GapCertificate(
                question=item.candidate.question,
                gap_kind=item.candidate.gap_kind,
                grounded_premises=item.candidate.premises,
                missing_structure=item.candidate.missing_structure,
                why_the_gap_matters=item.candidate.why_it_matters,
                corpus_search_performed=item.queries,
                evidence_of_absence=(
                    "Targeted retrieval did not contain a direct answer. "
                    + review.note
                ).strip(),
                resolution_test=item.candidate.resolution_test,
                review=review,
            )
            certificates.append(certificate)
            row["kept"] = True
            row["certificate"] = certificate.model_dump()
            print(f"[STAGE 2][KEEP] {certificate.question}")
        audit_rows.append(row)

    return certificates, audit_rows


# endregion Stage 2


# region Output and orchestration

def save_report(certificates: list[GapCertificate]) -> None:
    lines = [
        "KNOWLEDGE-GAP QUESTIONS — 索引済み資料内で未解決の実務的な論点",
        "=" * 90,
        "",
        f"採用件数: {len(certificates)}",
        "",
    ]
    if not certificates:
        lines.extend(
            [
                "根拠があり、非自明で、現実的かつ未回答と確認できる質問は見つかりませんでした。",
                "候補数を埋めるための人工的な質問は出力していません。",
            ]
        )
    else:
        for index, cert in enumerate(certificates, start=1):
            lines.extend(
                [
                    f"{index}. {cert.question}",
                    "",
                    f"   種別: {cert.gap_kind}   優先度: {cert.priority}",
                    "   根拠:",
                ]
            )
            for premise in cert.grounded_premises:
                lines.append(
                    f"   - [{premise.node_id}] {premise.claim}「{premise.quote}」"
                )
            lines.extend(
                [
                    f"   欠けている関係: {cert.missing_structure}",
                    f"   実務上の意味: {cert.why_the_gap_matters}",
                    f"   検索結果: {cert.evidence_of_absence}",
                    f"   解消条件: {cert.resolution_test}",
                    "",
                ]
            )

    lines.extend(
        [
            "",
            "注記: 「索引済み資料内で直接回答を確認できなかった」という結果であり、",
            "      世界的に未知、または仕様上未定であることを断定するものではありません。",
        ]
    )
    FINAL_TXT_PATH.write_text("\n".join(lines), encoding="utf-8")


async def run_pipeline(runtime: Runtime, audit: dict[str, Any]) -> None:
    region = await seed_region(runtime)
    audit["region"] = {
        "nodes": [
            {"node_id": node.id, "title": node.title or node.entity}
            for node in region.nodes
        ],
        "edges": [edge_brief(edge) for edge in region.edges],
    }
    save_audit(audit)
    if len(region.nodes) < 2:
        raise RuntimeError("fewer than two source nodes were retrieved")

    candidates, grounding_rejections = await generate_candidates(runtime, region)
    audit["candidates"] = [candidate.model_dump() for candidate in candidates]
    audit["grounding_rejections"] = grounding_rejections
    save_audit(audit)
    if not candidates:
        save_report([])
        return

    certificates, reviews = await review_candidates(runtime, candidates)
    certificates.sort(key=lambda certificate: certificate.priority, reverse=True)
    certificates = certificates[:MAX_FINAL_QUESTIONS]
    audit["reviews"] = reviews
    audit["certificates"] = [certificate.model_dump() for certificate in certificates]
    save_report(certificates)


async def run_gap_finder(runtime: Runtime | None = None) -> None:
    runtime = runtime or build_runtime()
    started = time.time()
    audit: dict[str, Any] = {
        "wiki_db_path": WIKI_DB_PATH,
        "chat_model": runtime.settings.chat_model,
        "started_at_unix": started,
        "limits": {
            "llm_calls_in_normal_path": 3,
            "llm_timeout_seconds": LLM_TIMEOUT_SECONDS,
            "overall_timeout_seconds": OVERALL_TIMEOUT_SECONDS,
            "llm_max_output_tokens": LLM_MAX_OUTPUT_TOKENS,
            "thinking_enabled": ENABLE_THINKING,
            "max_fact_pairs": MAX_FACT_PAIRS,
            "max_candidates": MAX_CANDIDATES,
        },
        "region": {},
        "candidates": [],
        "grounding_rejections": [],
        "reviews": [],
        "certificates": [],
        "errors": [],
    }
    save_audit(audit)

    try:
        await asyncio.wait_for(
            run_pipeline(runtime, audit), timeout=OVERALL_TIMEOUT_SECONDS
        )
    except TimeoutError:
        message = f"whole run exceeded {OVERALL_TIMEOUT_SECONDS}s"
        audit["errors"].append({"stage": "overall", "error": message})
        save_report([])
        print(f"[TIMEOUT] {message}")
    except Exception as exc:  # noqa: BLE001 - persist a useful audit for any failure
        audit["errors"].append({"stage": "pipeline", "error": repr(exc)})
        save_report([])
        print(f"[ERROR] {exc!r}")
    finally:
        audit["finished_at_unix"] = time.time()
        audit["elapsed_seconds"] = round(time.time() - started, 3)
        save_audit(audit)

    print(f"\nReport: {FINAL_TXT_PATH.resolve()}")
    print(f"Audit:  {AUDIT_JSON_PATH.resolve()}")


if __name__ == "__main__":
    asyncio.run(run_gap_finder())


# endregion Output and orchestration
