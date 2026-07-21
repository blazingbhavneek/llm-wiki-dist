from __future__ import annotations

"""
Non-trivial knowledge-gap detection over the llm-wiki graph.

Prototype implementation of the architecture in RESEARCH.md. Pipeline stages
map 1:1 onto that document so the two can be read side by side:

  STAGE 0  region seeding          RESEARCH.md §1 (offline candidate generation)
  STAGE 1  specialist agents       RESEARCH.md §2 (coverage/conflict/bridge/boundary)
  STAGE 2  targeted validation     RESEARCH.md §3 (cheap disqualifying searches)
  STAGE 3  certificate + adversary RESEARCH.md §4 (structured gap certificate)
  STAGE 4  anti-triviality gate    RESEARCH.md "Anti-triviality requirements"
  STAGE 5  ranking                 RESEARCH.md §5 (multiplicative priority)

Design commitments taken from the papers:

  - LimAgents: small models do bounded specialist passes; nothing gets one
    giant "find all the gaps" prompt. Each specialist sees one lens and a
    small slice of the region.
  - GAPMAP: gaps are typed (explicit vs implicit) and the implicit categories
    are named, not left to model whim.
  - FirstResearch: every survivor is expressed as an auditable certificate with
    a falsification test, and a gate rejects generic/one-hop questions.
  - HypER: verification is a separate cheap auditor pass, not the same call
    that produced the claim.

Run: python proto.py
"""

import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

# region Wiring and config

# ============================================================
# Wire up the real graph stack, in-process (no HTTP hop to /ask).
# GraphStore opens the actual sqlite file; Embedder/Reranker/LLM hit the same
# internal endpoints app.py uses.
# ============================================================

WIKI_ROOT = Path(__file__).parent / "llm-wiki-dist"
sys.path.insert(0, str(WIKI_ROOT))

from graph.core import Settings
from graph.gateway import ModelGateway
from graph.researcher import Researcher
from graph.store import GraphStore
from langchain_openai import ChatOpenAI


# ============================================================
# Config
# ============================================================

WIKI_DB_PATH = str(WIKI_ROOT / ".wiki" / "moove_wiki.sqlite")

MAX_RETRIES = 2
RETRY_SLEEP_SECONDS = 2
LLM_TIMEOUT_SECONDS = 1800

# STAGE 0 — how much graph around the seed to pull in (one hop from each hit).
REGION_SEARCH_LIMIT = 8
REGION_MAX_NODES = 24

# STAGE 1 — each specialist sees at most this many nodes, so prompts stay small.
SPECIALIST_NODE_BUDGET = 8
SPECIALIST_BODY_CHARS = 1200
MAX_CANDIDATES_PER_SPECIALIST = 4

# STAGE 2 — cheap disqualifying searches before any expensive judge runs.
VALIDATION_QUERIES_PER_CANDIDATE = 3
VALIDATION_SEARCH_LIMIT = 5

# STAGE 4/5
MAX_FINAL_QUESTIONS = 10
MIN_GROUNDED_PREMISES = 2  # RESEARCH.md anti-triviality test #1

ASK_CACHE_PATH = Path("ask_cache.json")
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


# ============================================================
# Model stack — built once at import
# ============================================================

SETTINGS = Settings()
GATEWAY = ModelGateway(SETTINGS)
STORE = GraphStore(WIKI_DB_PATH, readonly=True)
RESEARCHER = Researcher(GATEWAY, STORE)

# Plain langchain handle for our own structured extraction. Kept separate from
# GATEWAY.llm (researcher.py's own wrapper, used by the deep ask() agent).
LLM = ChatOpenAI(
    model=SETTINGS.chat_model,
    base_url=SETTINGS.chat_base_url,
    api_key=SETTINGS.chat_api_key,
    temperature=0.7,
    timeout=LLM_TIMEOUT_SECONDS,
)


# endregion Wiring and config


# region Taxonomy and schemas

# ============================================================
# Taxonomy
#
# GapKind: GAPMAP's implicit-gap categories, retargeted from biomedical to
# software per RESEARCH.md "Important scope limitation" — behavioral
# contracts, lifecycle, version boundaries, concurrency, interactions.
# ============================================================

GapKind = Literal[
    "missing_link",           # two documented claims that are never connected
    "narrow_generalization",  # rule stated only for a narrow scope
    "unreconciled_conflict",  # two descriptions that disagree, never resolved
    "undocumented_contract",  # cross-module dependency/invariant left implicit
    "missing_boundary",       # threshold, limit, interaction, tradeoff absent
    "unclear_lifecycle",      # ownership, state transition, version semantics
    "weak_provenance",        # claim asserted with no traceable source
]

SPECIALIST_LENSES: dict[str, str] = {
    # RESEARCH.md §2. One bounded lens each; deliberately narrow prompts.
    "coverage": (
        "この領域で、文書が当然扱うべき次元（前提条件、事後条件、所有権、"
        "ライフサイクル、エラー経路、設定の反映タイミング）のうち、"
        "実際には記述されていないものを探してください。"
    ),
    "conflict": (
        "この領域で、互いに矛盾する、または両立しない記述・条件・用語・"
        "バージョン差異を探してください。単なる言い換えは矛盾ではありません。"
    ),
    "bridge": (
        "この領域の複数ノードにまたがる因果・依存・呼び出し関係のうち、"
        "両端は文書化されているが、その間をつなぐ説明が欠けているものを探してください。"
    ),
    "boundary": (
        "この領域で、上限超過・境界値・設定間の相互作用・トレードオフ・"
        "異常系の遷移について、閾値や挙動が定義されていない箇所を探してください。"
    ),
}


# ============================================================
# Schemas
# ============================================================

class GroundedPremise(BaseModel):
    """One documented fact a candidate gap rests on. RESEARCH.md test #1."""

    node_id: str
    claim: str  # what this node actually says, in one sentence
    quote: str = ""  # short verbatim span, if the specialist could isolate one


class GapCandidate(BaseModel):
    """A specialist's proposed gap, before any validation."""

    question: str
    gap_kind: GapKind
    is_explicit: bool = False  # GAPMAP: did the doc itself signal the gap?
    premises: list[GroundedPremise] = Field(default_factory=list)
    missing_structure: str  # RESEARCH.md test #2 — what relation/boundary is absent
    why_it_matters: str  # test #3 — impact on a workflow/invariant/decision
    resolution_test: str  # test #7 — what evidence would settle it
    alternate_phrasings: list[str] = Field(default_factory=list)  # for stage 2


class GapCandidateList(BaseModel):
    candidates: list[GapCandidate] = Field(default_factory=list)


class ValidationVerdict(BaseModel):
    """STAGE 2 — cheap disqualification. RESEARCH.md §3."""

    already_answered: bool  # found under another term
    chunking_artifact: bool  # "gap" is really bad chunking / entity resolution
    scope_or_version_difference: bool  # apparent conflict is just scope difference
    unsupported_assumption: bool  # candidate rests on something not documented
    note: str = ""


class AdversarialResult(BaseModel):
    """STAGE 3 — the deep agent's best attempt to prove the gap isn't one."""

    can_be_answered_from_docs: bool
    decisive_citations_found: bool
    best_attempted_answer: str = ""
    remaining_gap: str = ""
    is_reasonable_question: bool = True
    reject_reason: str = ""


class TrivialityJudgement(BaseModel):
    """STAGE 4 — the eight anti-triviality tests, adjudicated explicitly."""

    multiple_grounded_premises: bool
    explicit_missing_structure: bool
    central_or_impactful: bool
    non_obvious_resolution: bool
    corpus_novel: bool
    mechanism_or_boundary: bool
    has_resolution_test: bool
    changes_understanding: bool
    verdict_note: str = ""

    def passed(self) -> int:
        return sum(
            [
                self.multiple_grounded_premises,
                self.explicit_missing_structure,
                self.central_or_impactful,
                self.non_obvious_resolution,
                self.corpus_novel,
                self.mechanism_or_boundary,
                self.has_resolution_test,
                self.changes_understanding,
            ]
        )


class PriorityScores(BaseModel):
    """STAGE 5 — factors for the multiplicative priority. RESEARCH.md §5."""

    impact: int = Field(ge=1, le=5)
    unresolvedness: int = Field(ge=1, le=5)
    structural_salience: int = Field(ge=1, le=5)
    cross_document_support: int = Field(ge=1, le=5)
    non_triviality: int = Field(ge=1, le=5)
    actionability: int = Field(ge=1, le=5)


class GapCertificate(BaseModel):
    """RESEARCH.md §4. The auditable record for one surviving gap."""

    question: str
    gap_kind: GapKind
    is_explicit: bool
    specialist: str  # which lens found it

    grounded_premises: list[GroundedPremise]
    missing_structure: str
    why_the_gap_matters: str
    corpus_search_performed: list[str]  # every query stage 2 actually ran
    evidence_of_absence: str  # what the adversary failed to find
    resolution_test: str

    triviality: TrivialityJudgement
    scores: PriorityScores

    @property
    def priority(self) -> float:
        s = self.scores
        # Multiplicative: a single weak factor should sink the candidate.
        return float(
            s.impact
            * s.unresolvedness
            * s.structural_salience
            * s.cross_document_support
            * s.non_triviality
            * s.actionability
        )


# endregion Taxonomy and schemas


# region Shared infrastructure

# ============================================================
# Structured LLM helper
# ============================================================

async def structured_llm_call(
    output_schema: type[BaseModel],
    system_prompt: str,
    user_prompt: str,
) -> BaseModel:
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"[LLM] {output_schema.__name__} attempt={attempt}/{MAX_RETRIES}")

            async def _do_call() -> BaseModel:
                structured = LLM.with_structured_output(output_schema)
                result = await structured.ainvoke(
                    [("system", system_prompt), ("human", user_prompt)]
                )
                if isinstance(result, output_schema):
                    return result
                return output_schema.model_validate(result)

            return await asyncio.wait_for(_do_call(), timeout=LLM_TIMEOUT_SECONDS)

        except Exception as exc:
            last_error = exc
            print(f"[LLM] failed {output_schema.__name__} attempt={attempt}: {repr(exc)}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_SLEEP_SECONDS)

    raise RuntimeError(
        f"structured call failed after {MAX_RETRIES} attempts: "
        f"schema={output_schema.__name__}, last_error={repr(last_error)}"
    )


# ============================================================
# ask() cache — wraps the deep agent, used only by the adversary (stage 3)
# ============================================================

class AskCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.data: dict[str, Any] = {"version": 2, "entries": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
                self.data.setdefault("entries", {})
            except Exception:
                pass

    @staticmethod
    def key(query: str) -> str:
        return hashlib.sha256(query.encode("utf-8")).hexdigest()

    async def get(self, query: str) -> dict[str, Any] | None:
        async with self.lock:
            entry = self.data["entries"].get(self.key(query))
            return entry.get("result") if entry and entry.get("query") == query else None

    async def set(self, query: str, result: dict[str, Any], label: str) -> None:
        async with self.lock:
            self.data["entries"][self.key(query)] = {
                "query": query,
                "label": label,
                "result": result,
                "created_at_unix": time.time(),
            }
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )


ASK_CACHE = AskCache(ASK_CACHE_PATH)


async def ask_deep_agent(question: str, label: str) -> str:
    """Run the full multi-subagent ask(). Exact-match cache only."""
    cached = await ASK_CACHE.get(question)
    if cached is not None:
        print(f"[ASK CACHE HIT] {label}")
        return cached.get("answer", "")

    print(f"[ASK] {label}")
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            answer = await RESEARCHER.ask(question)
            result = {"answer": answer.answer, "cited_node_ids": answer.cited_node_ids}
            await ASK_CACHE.set(question, result, label)
            return answer.answer
        except Exception as exc:
            last_error = exc
            print(f"[ASK] failed attempt={attempt}: {repr(exc)}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_SLEEP_SECONDS)

    raise RuntimeError(f"ask() failed after {MAX_RETRIES} attempts: {repr(last_error)}")


# ============================================================
# Node formatting — keeps specialist prompts small and uniform
# ============================================================

def node_brief(node: Any, body_chars: int = SPECIALIST_BODY_CHARS) -> dict[str, Any]:
    return {
        "node_id": node.id,
        "title": node.title or node.entity or node.id,
        "summary": node.summary,
        "claims": node.claims[:6],
        "body": (node.body or "")[:body_chars],
    }


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# endregion Shared infrastructure


# region Stage 0 — region seeding

# ============================================================
# STAGE 0 — region seeding  (RESEARCH.md §1)
#
# Cheap and deterministic: no LLM. Pull the seed chunk's graph neighborhood so
# specialists reason over real structure instead of the seed text alone. The
# multi-node region is also what makes anti-triviality test #1 (multiple
# grounded premises) achievable at all.
# ============================================================

async def seed_region() -> list[Any]:
    print("\n" + "=" * 90)
    print("[STAGE 0] seeding graph region from seed chunk")
    print("=" * 90)

    seeds = await RESEARCHER.search(SEED_CHUNK[:1000], REGION_SEARCH_LIMIT)
    print(f"[STAGE 0] direct search hits: {len(seeds)}")

    region: dict[str, Any] = {n.id: n for n in seeds}

    # Expand one hop through real edges — RESEARCH.md §1 "multi-hop paths".
    for node in list(seeds):
        if len(region) >= REGION_MAX_NODES:
            break
        try:
            pairs = await RESEARCHER.follow_link(node.id, direction="both", limit=6)
        except Exception as exc:
            print(f"[STAGE 0] follow_link failed for {node.id}: {repr(exc)}")
            continue
        for _edge, neighbor in pairs:
            if neighbor.id not in region and len(region) < REGION_MAX_NODES:
                region[neighbor.id] = neighbor

    nodes = list(region.values())[:REGION_MAX_NODES]
    print(f"[STAGE 0] region size after 1-hop expansion: {len(nodes)}")
    for n in nodes:
        print(f"  - {n.id}  {n.title or n.entity}")
    return nodes


# endregion Stage 0 — region seeding


# region Stage 1 — specialist agents

# ============================================================
# STAGE 1 — specialist agents  (RESEARCH.md §2, LimAgents)
#
# Four bounded lenses, each run over small node batches in parallel. No
# specialist ever sees the whole region at once, so gemma-class models get a
# narrow task rather than one sprawling "find everything" prompt.
# ============================================================

SPECIALIST_SYSTEM = """
あなたは技術文書の「知識ギャップ」を探す専門エージェントです。
与えられた観点（レンズ）だけに集中してください。他の観点は無視してください。

厳守:
- 各候補は、必ず2件以上の異なるノードに根拠（premises）を持つこと。
  1つの文だけから思いつく質問は禁止です。
- premises には、実際にノードに書かれている内容だけを書くこと。捏造禁止。
- missing_structure には「何の関係・境界・条件・機構・出典が欠けているか」を具体的に書くこと。
- why_it_matters には、どの運用・実装・判断に影響するかを書くこと。
- resolution_test には「どんな記述が見つかればこのギャップは解消されるか」を書くこと。
- alternate_phrasings には、同じ内容を別の用語で検索するためのクエリを2〜3件書くこと。
- 用語説明、項目の言い換え、単純な一覧化は候補にしないでください。
- 良い候補がなければ candidates=[] を返してください。無理に埋めないこと。

gap_kind の選び方:
- missing_link: 両端は書かれているが、その間の関係が書かれていない
- narrow_generalization: 狭い範囲でのみ規定され、一般化条件が不明
- unreconciled_conflict: 矛盾する記述が併存し、解決されていない
- undocumented_contract: モジュール間の暗黙の前提・不変条件が未記述
- missing_boundary: 閾値・上限・相互作用・トレードオフが未定義
- unclear_lifecycle: 所有権・状態遷移・バージョン意味論が不明確
- weak_provenance: 主張の出典・根拠が追跡できない

is_explicit は、文書自身が「未定」「未処理」「別途規定」などと明示している場合のみ true。
"""


async def run_specialist(lens: str, nodes: list[Any]) -> list[tuple[str, GapCandidate]]:
    """One lens over one small batch of nodes."""
    user_prompt = f"""
観点（このレンズだけを見ること）:
{SPECIALIST_LENSES[lens]}

シードチャンク（この領域の出発点）:
{SEED_CHUNK}

グラフ領域のノード:
{json.dumps([node_brief(n) for n in nodes], ensure_ascii=False, indent=2)}

この観点から、最大 {MAX_CANDIDATES_PER_SPECIALIST} 件の候補ギャップを挙げてください。
根拠が2ノード未満しか無いものは挙げないでください。
"""

    try:
        result = await structured_llm_call(GapCandidateList, SPECIALIST_SYSTEM, user_prompt)
    except Exception as exc:
        print(f"[SPECIALIST {lens}] failed: {repr(exc)}")
        return []

    candidates = result.candidates[:MAX_CANDIDATES_PER_SPECIALIST]  # type: ignore[attr-defined]
    print(f"[SPECIALIST {lens}] proposed {len(candidates)}")
    for c in candidates:
        print(f"    [{c.gap_kind}] {c.question}  (premises={len(c.premises)})")
    return [(lens, c) for c in candidates]


async def run_all_specialists(nodes: list[Any]) -> list[tuple[str, GapCandidate]]:
    print("\n" + "=" * 90)
    print("[STAGE 1] specialist agents (coverage / conflict / bridge / boundary)")
    print("=" * 90)

    batches = chunked(nodes, SPECIALIST_NODE_BUDGET)
    tasks = [
        run_specialist(lens, batch)
        for lens in SPECIALIST_LENSES
        for batch in batches
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    found: list[tuple[str, GapCandidate]] = []
    seen_questions: set[str] = set()

    for result in results:
        if isinstance(result, Exception):
            print(f"[STAGE 1] specialist task error: {repr(result)}")
            continue
        for lens, candidate in result:
            if candidate.question in seen_questions:
                continue
            seen_questions.add(candidate.question)
            found.append((lens, candidate))

    print(f"[STAGE 1] total distinct candidates: {len(found)}")
    return found


# endregion Stage 1 — specialist agents


# region Stage 2 — targeted validation

# ============================================================
# STAGE 2 — targeted validation loop  (RESEARCH.md §3)
#
# Cheap disqualification before anything expensive runs. Each candidate's own
# alternate phrasings are searched: if a plain search under different wording
# answers it, it was never a gap. This is also anti-triviality test #5
# (corpus novelty under alternate wording).
# ============================================================

VALIDATION_SYSTEM = """
あなたは、候補ギャップが「本物のギャップ」かどうかを、検索結果だけで安く判定する監査器です。

以下を判定してください:
- already_answered: 別の用語・別ノードで、この質問に実質的に答えている記述がある
- chunking_artifact: 情報は存在するが、チャンク分割や表記ゆれのせいで見つけにくいだけ
- scope_or_version_difference: 矛盾に見えるが、実際は適用範囲やバージョンの違い
- unsupported_assumption: 候補が、文書に無い前提の上に成り立っている

いずれも該当しない場合は、すべて false にしてください。
検索結果に無い内容を推測しないでください。
"""


async def validate_candidate(
    lens: str, candidate: GapCandidate
) -> tuple[str, GapCandidate, ValidationVerdict, list[str]]:
    queries = ([candidate.question] + candidate.alternate_phrasings)[
        :VALIDATION_QUERIES_PER_CANDIDATE
    ]

    hits: list[Any] = []
    for query in queries:
        try:
            hits.extend(await RESEARCHER.search(query, VALIDATION_SEARCH_LIMIT))
        except Exception as exc:
            print(f"[STAGE 2] search failed ({query[:40]}): {repr(exc)}")

    # dedupe by node id, keep it small
    unique: dict[str, Any] = {}
    for node in hits:
        unique.setdefault(node.id, node)
    top = list(unique.values())[:6]

    if not top:
        # nothing found under any phrasing — strongest possible novelty signal
        return lens, candidate, ValidationVerdict(
            already_answered=False,
            chunking_artifact=False,
            scope_or_version_difference=False,
            unsupported_assumption=False,
            note="どの表現でも検索ヒットなし",
        ), queries

    user_prompt = f"""
候補質問:
{candidate.question}

この候補が主張する欠落:
{candidate.missing_structure}

別表現も含めて検索した結果:
{json.dumps([node_brief(n, 800) for n in top], ensure_ascii=False, indent=2)}
"""

    try:
        verdict = await structured_llm_call(
            ValidationVerdict, VALIDATION_SYSTEM, user_prompt
        )
    except Exception as exc:
        print(f"[STAGE 2] validation failed: {repr(exc)}")
        verdict = ValidationVerdict(
            already_answered=False,
            chunking_artifact=False,
            scope_or_version_difference=False,
            unsupported_assumption=False,
            note=f"validation error: {exc}",
        )

    return lens, candidate, verdict, queries  # type: ignore[return-value]


def survives_validation(verdict: ValidationVerdict) -> bool:
    return not (
        verdict.already_answered
        or verdict.chunking_artifact
        or verdict.scope_or_version_difference
        or verdict.unsupported_assumption
    )


async def run_validation(
    candidates: list[tuple[str, GapCandidate]]
) -> list[tuple[str, GapCandidate, list[str]]]:
    print("\n" + "=" * 90)
    print(f"[STAGE 2] targeted validation of {len(candidates)} candidates")
    print("=" * 90)

    results = await asyncio.gather(
        *(validate_candidate(lens, c) for lens, c in candidates),
        return_exceptions=True,
    )

    survivors: list[tuple[str, GapCandidate, list[str]]] = []
    for result in results:
        if isinstance(result, Exception):
            print(f"[STAGE 2] error: {repr(result)}")
            continue
        lens, candidate, verdict, queries = result
        if survives_validation(verdict):
            print(f"[SURVIVES] {candidate.question}")
            survivors.append((lens, candidate, queries))
        else:
            reasons = [
                name
                for name, flagged in [
                    ("already_answered", verdict.already_answered),
                    ("chunking_artifact", verdict.chunking_artifact),
                    ("scope_or_version", verdict.scope_or_version_difference),
                    ("unsupported_assumption", verdict.unsupported_assumption),
                ]
                if flagged
            ]
            print(f"[DROPPED: {','.join(reasons)}] {candidate.question}")

    print(f"[STAGE 2] survivors: {len(survivors)}/{len(candidates)}")
    return survivors


# endregion Stage 2 — targeted validation


# region Stage 3 — adversary

# ============================================================
# STAGE 3 — adversary  (RESEARCH.md §4)
#
# Only survivors reach the expensive judge. The deep multi-subagent agent gets
# one hard shot at proving the gap isn't real. Note the framing asks it to
# *prove coverage*, not to answer helpfully — a synthesized-but-ungrounded
# answer should not count as closing the gap.
# ============================================================

ADVERSARY_SYSTEM = """
あなたは、RAGの反証的回答を AdversarialResult に変換する抽出器です。

ルール:
- 決定的な直接記述がある場合のみ can_be_answered_from_docs=true
- 引用が曖昧、または周辺説明の組み合わせによる推論に留まる場合は
  decisive_citations_found=false
- 複数の記述から「推論すれば分かる」だけでは、回答済みとみなさないこと
- 質問が不自然・人工的なら is_reasonable_question=false
- 根拠や引用を捏造しないこと
"""


async def run_adversary(candidate: GapCandidate) -> AdversarialResult:
    ask_prompt = f"""
あなたは反証的な文書検証エージェントです。

目的:
次の質問が「実はドキュメント内で明確に回答済みである」ことを、
できるだけ強く証明しようとしてください。

質問:
{candidate.question}

この質問が指摘する欠落:
{candidate.missing_structure}

根拠として挙げられているノード:
{json.dumps([p.model_dump() for p in candidate.premises], ensure_ascii=False, indent=2)}

作業:
- ドキュメントから、この質問の核心に直接答える記述を探してください。
- 決定的な記述と引用が見つかった場合のみ「回答済み」と判断してください。
- 複数の記述をつなぎ合わせた推論は「回答済み」ではありません。
  その場合は、何が明示的に書かれていないかを述べてください。
- 関連しているだけの周辺説明は回答済みとみなしません。
- 引用や出典を捏造しないでください。
- 日本語で回答してください。
"""

    rag_text = await ask_deep_agent(ask_prompt, f"adversary: {candidate.question[:60]}")

    user_prompt = f"""
質問:
{candidate.question}

以下の反証的RAG回答を AdversarialResult に変換してください。

RAG回答:
{rag_text}
"""

    return await structured_llm_call(AdversarialResult, ADVERSARY_SYSTEM, user_prompt)  # type: ignore[return-value]


# endregion Stage 3 — adversary


# region Stage 4 — anti-triviality gate and adjudication

# ============================================================
# STAGE 4 — anti-triviality gate  (RESEARCH.md "Anti-triviality requirements")
#
# The eight tests are adjudicated one by one and recorded, so a rejection is
# auditable rather than a silent score threshold. Test #1 (multiple grounded
# premises) is checked deterministically first — no need to spend a model call
# on a candidate that structurally cannot pass.
# ============================================================

TRIVIALITY_SYSTEM = """
あなたは、候補ギャップが「非自明で価値がある」かどうかを判定する厳格な審査員です。

8つのテストを個別に判定してください:
1. multiple_grounded_premises: 2件以上の異なるノード/節/経路に根拠があるか
2. explicit_missing_structure: 欠けている関係・境界・条件・機構・出典が明確か
3. central_or_impactful: 重要なワークフロー・不変条件・依存・判断に影響するか
4. non_obvious_resolution: 単純な一箇所の参照では解決せず、統合や横断比較が必要か
5. corpus_novel: 別表現で検索しても十分な答えが見つからなかったか
6. mechanism_or_boundary: 矛盾・範囲遷移・相互作用・閾値・トレードオフ・
   バージョン差・モジュール間契約を扱っているか
7. has_resolution_test: 何が見つかれば解消/反証されるかが定義されているか
8. changes_understanding: 答えが理解・実装・保守・判断を変えるか

厳しく判定してください。「〜とは何か」「〜の違いは何か」のような一般的な質問は
ほとんどのテストで false になるはずです。

同時に、優先度スコア（1〜5）も付けてください:
- impact: 影響の大きさ
- unresolvedness: 未解決の度合い
- structural_salience: グラフ構造上の重要性
- cross_document_support: 複数文書にまたがる裏付けの強さ
- non_triviality: 非自明さ
- actionability: 実務で行動につながるか
"""


class TrivialityAndScores(BaseModel):
    triviality: TrivialityJudgement
    scores: PriorityScores


async def judge_triviality(
    candidate: GapCandidate,
    adversary: AdversarialResult,
    searched_queries: list[str],
) -> TrivialityAndScores:
    user_prompt = f"""
候補質問:
{candidate.question}

種別: {candidate.gap_kind}
文書自身が明示したギャップか: {candidate.is_explicit}

根拠ノード:
{json.dumps([p.model_dump() for p in candidate.premises], ensure_ascii=False, indent=2)}

欠けている構造:
{candidate.missing_structure}

なぜ重要か:
{candidate.why_it_matters}

解消テスト:
{candidate.resolution_test}

実際に検索した表現:
{json.dumps(searched_queries, ensure_ascii=False)}

反証エージェントの結果:
- 決定的に回答できたか: {adversary.can_be_answered_from_docs}
- 決定的な引用があったか: {adversary.decisive_citations_found}
- 残ったギャップ: {adversary.remaining_gap}
- 反証エージェントの最良回答: {adversary.best_attempted_answer[:800]}
"""

    return await structured_llm_call(TrivialityAndScores, TRIVIALITY_SYSTEM, user_prompt)  # type: ignore[return-value]


def gate(triviality: TrivialityJudgement, adversary: AdversarialResult) -> bool:
    """RESEARCH.md: reject unless it passes *most* tests, and the adversary failed."""
    if adversary.can_be_answered_from_docs or adversary.decisive_citations_found:
        return False
    if not adversary.is_reasonable_question or adversary.reject_reason:
        return False
    # Tests 1, 2 and 7 are structural preconditions — never waivable.
    if not (
        triviality.multiple_grounded_premises
        and triviality.explicit_missing_structure
        and triviality.has_resolution_test
    ):
        return False
    return triviality.passed() >= 6  # "most of these tests"


# ============================================================
# STAGE 3-4 driver, per candidate
# ============================================================

async def adjudicate(
    lens: str,
    candidate: GapCandidate,
    searched_queries: list[str],
) -> tuple[GapCertificate | None, dict[str, Any]]:
    record: dict[str, Any] = {
        "specialist": lens,
        "candidate": candidate.model_dump(),
        "adversary": None,
        "triviality": None,
        "scores": None,
        "kept": False,
        "error": None,
    }

    print("\n" + "-" * 90)
    print(f"[ADJUDICATE][{lens}] {candidate.question}")

    # Deterministic precondition — skip the expensive path when it cannot pass.
    if len({p.node_id for p in candidate.premises}) < MIN_GROUNDED_PREMISES:
        print(f"[REJECT] fewer than {MIN_GROUNDED_PREMISES} distinct grounded premises")
        record["error"] = "insufficient_premises"
        return None, record

    try:
        adversary = await run_adversary(candidate)
        record["adversary"] = adversary.model_dump()
        print(
            f"[ADVERSARY] answered={adversary.can_be_answered_from_docs} "
            f"decisive={adversary.decisive_citations_found}"
        )

        judged = await judge_triviality(candidate, adversary, searched_queries)
        record["triviality"] = judged.triviality.model_dump()
        record["scores"] = judged.scores.model_dump()
        print(f"[TRIVIALITY] passed {judged.triviality.passed()}/8")

        if not gate(judged.triviality, adversary):
            print("[REJECT] failed anti-triviality gate")
            return None, record

        certificate = GapCertificate(
            question=candidate.question,
            gap_kind=candidate.gap_kind,
            is_explicit=candidate.is_explicit,
            specialist=lens,
            grounded_premises=candidate.premises,
            missing_structure=candidate.missing_structure,
            why_the_gap_matters=candidate.why_it_matters,
            corpus_search_performed=searched_queries,
            evidence_of_absence=adversary.remaining_gap or adversary.best_attempted_answer,
            resolution_test=candidate.resolution_test,
            triviality=judged.triviality,
            scores=judged.scores,
        )
        record["kept"] = True
        record["certificate"] = certificate.model_dump()
        print(f"[KEEP] priority={certificate.priority:.0f}")
        return certificate, record

    except Exception as exc:
        record["error"] = repr(exc)
        print(f"[ERROR] {repr(exc)}")
        return None, record


# endregion Stage 4 — anti-triviality gate and adjudication


# region Stage 5 — ranking and output

# ============================================================
# Output
# ============================================================

def save_audit(audit: dict[str, Any]) -> None:
    AUDIT_JSON_PATH.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save_report(certificates: list[GapCertificate]) -> None:
    lines: list[str] = []
    lines.append("KNOWLEDGE GAP CERTIFICATES — 文書内で未解決と判定された論点")
    lines.append("=" * 90)
    lines.append("")
    lines.append(f"採用件数: {len(certificates)}")
    lines.append("")

    if not certificates:
        lines.append("非自明な未回答ギャップは見つかりませんでした。")
        lines.append("")
        lines.append("解釈:")
        lines.append("- 候補が反証エージェントによって回答済みと判定された、または")
        lines.append("- 反自明性ゲート（8テスト中6件以上）を通過しなかった可能性があります。")
        lines.append("- このプロトタイプは保守的に設計されています。")
    else:
        for i, cert in enumerate(certificates, start=1):
            lines.append(f"{i}. {cert.question}")
            lines.append("")
            lines.append(f"   種別: {cert.gap_kind}   検出レンズ: {cert.specialist}")
            lines.append(f"   文書が明示したギャップか: {'はい' if cert.is_explicit else 'いいえ（暗黙）'}")
            lines.append(f"   優先度: {cert.priority:.0f}   反自明性: {cert.triviality.passed()}/8")
            lines.append("")
            lines.append("   根拠となる記述:")
            for p in cert.grounded_premises:
                lines.append(f"   - [{p.node_id}] {p.claim}")
                if p.quote:
                    lines.append(f"       「{p.quote}」")
            lines.append("")
            lines.append(f"   欠けている構造: {cert.missing_structure}")
            lines.append("")
            lines.append(f"   なぜ重要か: {cert.why_the_gap_matters}")
            lines.append("")
            lines.append("   実施した検索:")
            for q in cert.corpus_search_performed:
                lines.append(f"   - {q}")
            lines.append("")
            lines.append(f"   不在の根拠: {cert.evidence_of_absence}")
            lines.append("")
            lines.append(f"   解消テスト: {cert.resolution_test}")
            lines.append("")
            s = cert.scores
            lines.append(
                f"   スコア: impact={s.impact} unresolved={s.unresolvedness} "
                f"salience={s.structural_salience} cross_doc={s.cross_document_support} "
                f"non_trivial={s.non_triviality} actionable={s.actionability}"
            )
            lines.append("")
            lines.append("-" * 90)
            lines.append("")

    lines.append("")
    lines.append("注記: 本結果は「索引済み資料の中で確認できなかった」ことを示すものであり、")
    lines.append("      世界的に未知であることを意味しません。")

    FINAL_TXT_PATH.write_text("\n".join(lines), encoding="utf-8")


# endregion Stage 5 — ranking and output


# region Orchestrator

# ============================================================
# Orchestrator
# ============================================================

async def run_gap_finder() -> None:
    audit: dict[str, Any] = {
        "wiki_db_path": WIKI_DB_PATH,
        "chat_model": SETTINGS.chat_model,
        "seed_chunk": SEED_CHUNK,
        "started_at_unix": time.time(),
        "region": [],
        "candidates": [],
        "survivors": [],
        "adjudications": [],
        "certificates": [],
        "errors": [],
    }

    try:
        # STAGE 0
        region = await seed_region()
        audit["region"] = [
            {"node_id": n.id, "title": n.title or n.entity} for n in region
        ]
        save_audit(audit)

        if not region:
            print("[FATAL] empty region — seed chunk matched nothing in the graph")
            audit["errors"].append({"stage": "seed_region", "error": "empty region"})
            save_audit(audit)
            save_report([])
            return

        # STAGE 1
        candidates = await run_all_specialists(region)
        audit["candidates"] = [
            {"specialist": lens, **c.model_dump()} for lens, c in candidates
        ]
        save_audit(audit)

        if not candidates:
            print("[STAGE 1] no candidates proposed — nothing to validate")
            save_report([])
            save_audit(audit)
            return

        # STAGE 2
        survivors = await run_validation(candidates)
        audit["survivors"] = [
            {"specialist": lens, "question": c.question, "queries": q}
            for lens, c, q in survivors
        ]
        save_audit(audit)

        # STAGE 3 + 4 — sequential: each runs a full deep ask(), which is
        # already internally parallel across subagents.
        print("\n" + "=" * 90)
        print(f"[STAGE 3/4] adversary + anti-triviality gate on {len(survivors)}")
        print("=" * 90)

        certificates: list[GapCertificate] = []
        for lens, candidate, queries in survivors:
            certificate, record = await adjudicate(lens, candidate, queries)
            audit["adjudications"].append(record)
            if certificate:
                certificates.append(certificate)
            save_audit(audit)

        # STAGE 5 — ranking
        certificates.sort(key=lambda c: c.priority, reverse=True)
        certificates = certificates[:MAX_FINAL_QUESTIONS]

        audit["certificates"] = [c.model_dump() for c in certificates]
        audit["finished_at_unix"] = time.time()

        save_report(certificates)
        save_audit(audit)

        print("\n" + "#" * 90)
        print("[DONE]")
        print(f"Report:     {FINAL_TXT_PATH.resolve()}")
        print(f"Audit JSON: {AUDIT_JSON_PATH.resolve()}")
        print(f"Ask cache:  {ASK_CACHE_PATH.resolve()}")
        print(f"Certificates kept: {len(certificates)}")
        print("#" * 90)

    except Exception as exc:
        audit["errors"].append({"stage": "top_level", "error": repr(exc)})
        audit["finished_at_unix"] = time.time()
        save_audit(audit)
        print(f"[FATAL] {repr(exc)}")
        raise


if __name__ == "__main__":
    asyncio.run(run_gap_finder())

# endregion Orchestrator
