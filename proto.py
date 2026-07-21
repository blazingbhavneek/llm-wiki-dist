from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Literal

import httpx
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field


# ============================================================
# Config
# ============================================================

ASK_ENDPOINT = "http://10.160.152.38:8000/agent/llm-wiki/wiki_moove/api/ask"

LLM_BASE_URL = "http://10.160.144.101:51029/v1"
LLM_MODEL = "gemma-4-31B"
LLM_API_KEY = "sk-dummy"

ASK_OVERRIDES: dict[str, Any] | None = None

CONCURRENCY = 3

REQUEST_TIMEOUT_SECONDS = 1800
ASK_CALL_TOTAL_TIMEOUT_SECONDS = 1800
LLM_CALL_TOTAL_TIMEOUT_SECONDS = 1800
CANDIDATE_TOTAL_TIMEOUT_SECONDS = 4200

MAX_RETRIES = 2
RETRY_SLEEP_SECONDS = 2

MAX_CANDIDATE_QUESTIONS = 18
MAX_FINAL_QUESTIONS = 10

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
# Pydantic schemas
# ============================================================

class CitationRef(BaseModel):
    title: str | None = None
    url: str | None = None
    source_id: str | None = None
    quote: str | None = None


class GroundedClaim(BaseModel):
    claim: str
    refs: list[CitationRef] = Field(default_factory=list)


class CitationContextDocument(BaseModel):
    seed_summary: str
    key_terms: list[str] = Field(default_factory=list)
    grounded_claims: list[GroundedClaim] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    explicit_limitations: list[str] = Field(default_factory=list)
    nearby_topics_in_docs: list[str] = Field(default_factory=list)
    missing_context: list[str] = Field(default_factory=list)


class CandidateQuestion(BaseModel):
    question: str
    question_type: Literal[
        "descriptive",
        "causal",
        "diagnostic",
        "benchmark",
        "algorithmic",
        "design",
        "edge_case",
        "compatibility",
        "failure_mode",
        "limitation",
        "operational",
    ]
    rationale: str
    why_docs_should_reasonably_cover_it: str
    source_gap_or_tension: str
    expected_expert_value: str

    # Added anti-shallow fields
    why_not_answered_by_current_context: str = ""
    expected_evidence_needed: str = ""
    depth_score: int = Field(default=3, ge=1, le=5)
    novelty_score: int = Field(default=3, ge=1, le=5)

    plausibility_score: int = Field(ge=1, le=5)
    usefulness_score: int = Field(ge=1, le=5)
    weirdness_risk_score: int = Field(ge=1, le=5)


class CandidateQuestionList(BaseModel):
    questions: list[CandidateQuestion] = Field(default_factory=list)


class GapCheckResult(BaseModel):
    question: str
    status: Literal[
        "answered",
        "partially_answered",
        "unanswered",
        "out_of_scope",
        "nonsensical",
        "too_artificial",
    ]
    confidence: int = Field(ge=1, le=5)
    concise_answer_if_answered: str | None = None
    evidence_that_answers_it: list[GroundedClaim] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    why_not_answered_or_only_partial: str | None = None
    why_question_is_reasonable: str | None = None
    plausibility_score: int = Field(ge=1, le=5)
    usefulness_score: int = Field(ge=1, le=5)
    weirdness_risk_score: int = Field(ge=1, le=5)
    recommendation: Literal["keep", "reject"]


class AdversarialCheckResult(BaseModel):
    question: str
    best_attempted_answer_from_docs: str | None = None
    can_be_answered_from_docs: bool
    decisive_citations_found: bool
    decisive_evidence: list[GroundedClaim] = Field(default_factory=list)
    remaining_gap: str | None = None
    is_reasonable_question: bool
    reject_reason_if_any: str | None = None


class FinalGapQuestion(BaseModel):
    question: str
    question_type: str
    rationale: str
    source_gap_or_tension: str
    expected_expert_value: str
    why_docs_should_reasonably_cover_it: str
    why_it_is_not_answered: str
    missing_information: list[str]
    plausibility_score: int
    usefulness_score: int
    weirdness_risk_score: int
    confidence: int


# ============================================================
# LLM client
# ============================================================

def make_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=LLM_MODEL,
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY,
        temperature=0.7,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


LLM = make_llm()


async def structured_llm_call(
    output_schema: type[BaseModel],
    system_prompt: str,
    user_prompt: str,
) -> BaseModel:
    """
    langchain-openai with_structured_output を使う構造化LLM呼び出し。
    retry + hard wall-clock timeout 付き。
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(
                f"[LLM] structured call attempt={attempt}/{MAX_RETRIES}, "
                f"schema={output_schema.__name__}"
            )

            async def _do_call() -> BaseModel:
                structured = LLM.with_structured_output(output_schema)
                result = await structured.ainvoke(
                    [
                        ("system", system_prompt),
                        ("human", user_prompt),
                    ]
                )

                if isinstance(result, output_schema):
                    return result

                return output_schema.model_validate(result)

            return await asyncio.wait_for(
                _do_call(),
                timeout=LLM_CALL_TOTAL_TIMEOUT_SECONDS,
            )

        except Exception as exc:
            last_error = exc
            print(
                f"[LLM] failed attempt={attempt}/{MAX_RETRIES}, "
                f"schema={output_schema.__name__}, error={repr(exc)}"
            )

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_SLEEP_SECONDS)

    raise RuntimeError(
        f"Structured LLM call failed after {MAX_RETRIES} attempts. "
        f"schema={output_schema.__name__}, last_error={repr(last_error)}"
    )


# ============================================================
# /ask cache manager
# ============================================================

class AskCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.data: dict[str, Any] = {
            "version": 1,
            "entries": {},
        }
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                if "entries" not in self.data:
                    self.data["entries"] = {}
            except Exception:
                self.data = {"version": 1, "entries": {}}

    async def save(self) -> None:
        async with self.lock:
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    @staticmethod
    def query_hash(query: str) -> str:
        return hashlib.sha256(query.encode("utf-8")).hexdigest()

    async def get_exact(self, query: str) -> dict[str, Any] | None:
        h = self.query_hash(query)
        async with self.lock:
            entry = self.data["entries"].get(h)
            if entry and entry.get("query") == query:
                return entry.get("result")
        return None

    async def set(self, query: str, result: dict[str, Any], debug_label: str) -> None:
        h = self.query_hash(query)

        async with self.lock:
            self.data["entries"][h] = {
                "query": query,
                "debug_label": debug_label,
                "result": result,
                "created_at_unix": time.time(),
            }

        await self.save()


ASK_CACHE = AskCache(ASK_CACHE_PATH)


# ============================================================
# Helpers
# ============================================================

def response_to_text(response: Any) -> str:
    if isinstance(response, str):
        return response

    if isinstance(response, dict):
        for key in [
            "answer",
            "response",
            "result",
            "output",
            "content",
            "text",
            "raw_text",
            "message",
        ]:
            value = response.get(key)
            if isinstance(value, str):
                return value

        for key in ["data", "payload"]:
            value = response.get(key)
            if isinstance(value, dict):
                nested = response_to_text(value)
                if nested:
                    return nested

        return json.dumps(response, ensure_ascii=False, indent=2)

    return str(response)


def should_reject_locally(question: str) -> bool:
    q = question.strip().lower()

    absurd_terms = [
        "ディズニー",
        "ポケモン",
        "ドラゴン",
        "ユニコーン",
        "魔法",
        "星座",
        "占い",
        "好きな色",
        "有名人",
        "ジョーク",
        "meme",
        "disney",
        "pokemon",
        "dragon",
        "unicorn",
    ]

    too_generic = [
        "これは何ですか",
        "なぜ重要ですか",
        "メリットは何ですか",
        "どのように動作しますか",
    ]

    question_like_terms = [
        "何",
        "どの",
        "どのよう",
        "なぜ",
        "場合",
        "条件",
        "基準",
        "方法",
        "影響",
        "違い",
        "扱い",
        "可能",
        "必要",
        "されるか",
        "できるか",
        "なるか",
        "定義されているか",
        "使われるのか",
        "解釈されるのか",
    ]

    if len(q) < 12:
        return True

    if any(term in q for term in absurd_terms):
        return True

    if any(pattern in q for pattern in too_generic):
        return True

    # 日本語LLMは末尾の「？」を省略することがあるため、疑問形らしさでも許可する。
    if not (
        q.endswith("？")
        or q.endswith("?")
        or any(term in q for term in question_like_terms)
    ):
        return True

    return False


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# ============================================================
# Cached /ask caller
# ============================================================

async def call_ask_endpoint_uncached(question: str) -> dict[str, Any]:
    payload = {
        "question": question,
        "overrides": ASK_OVERRIDES,
    }

    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"[ASK] call attempt={attempt}/{MAX_RETRIES}")

            async def _do_request() -> dict[str, Any]:
                timeout = httpx.Timeout(
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    connect=30.0,
                    read=REQUEST_TIMEOUT_SECONDS,
                    write=30.0,
                    pool=30.0,
                )

                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(ASK_ENDPOINT, json=payload)

                if response.status_code >= 400:
                    raise RuntimeError(
                        f"/ask failed HTTP {response.status_code}: {response.text[:2000]}"
                    )

                try:
                    return response.json()
                except Exception:
                    return {"raw_text": response.text}

            return await asyncio.wait_for(
                _do_request(),
                timeout=ASK_CALL_TOTAL_TIMEOUT_SECONDS,
            )

        except Exception as exc:
            last_error = exc
            print(f"[ASK] failed attempt={attempt}/{MAX_RETRIES}, error={repr(exc)}")

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_SLEEP_SECONDS)

    raise RuntimeError(
        f"/ask failed after {MAX_RETRIES} attempts. last_error={repr(last_error)}"
    )


async def ask_cached(question: str, debug_label: str) -> dict[str, Any]:
    """
    Exact cache only.

    Important:
    Semantic cache reuse is intentionally disabled.
    While debugging, semantic reuse can accidentally reuse stale empty results.
    """
    print(f"[ASK CACHE] checking exact cache: {debug_label}")

    exact = await ASK_CACHE.get_exact(question)
    if exact is not None:
        print(f"[ASK CACHE HIT] {debug_label}")
        return exact

    print(f"[ASK CALL] {debug_label}")
    result = await call_ask_endpoint_uncached(question)

    print(f"[ASK CACHE SAVE] {debug_label}")
    await ASK_CACHE.set(question, result, debug_label=debug_label)

    return result


# ============================================================
# Stage 1: context compiler
# ============================================================

async def compile_citation_context(seed_chunk: str) -> CitationContextDocument:
    print("\n" + "=" * 90)
    print("[STAGE 1] /ask で citation/context document を作成中...")
    print("=" * 90)

    ask_prompt = f"""
あなたは日本語コーパスに接続されたRAG研究支援エージェントです。

以下のシードチャンクを理解するために必要な文脈を、検索可能なドキュメントに基づいて整理してください。

シードチャンク:
{seed_chunk}

出力には以下を含めてください:
- シードチャンクの要約
- 重要用語
- ドキュメントに根拠のある主張
- 根拠となる引用、出典名、URL、チャンクIDなど
- 暗黙の前提
- 明示的な制限事項
- 関連する周辺トピック
- ドキュメント上まだ不足していそうな文脈

注意:
- 根拠がない内容は断定しないでください。
- 参照できる出典がある場合は必ず末尾にまとめてください。
- 日本語で回答してください。
"""

    raw = await ask_cached(ask_prompt, "context_compiler")
    rag_text = response_to_text(raw)

    print("[STAGE 1] /ask response received. Structuring with LLM...")

    system_prompt = """
あなたはRAG回答を CitationContextDocument に変換する高速な抽出器です。

重要:
- 長く考えないでください。
- 深い分析は不要です。
- 完璧な抽出を目指さないでください。
- 分かる情報だけ短く入れてください。
- 不明なら空配列にしてください。
- 推測しないでください。
- 引用、URL、チャンクIDを捏造しないでください。
- スキーマを満たしたらすぐ終了してください。
- できるだけ短い内容で返してください。
"""

    user_prompt = f"""
以下のRAG回答を CitationContextDocument に変換してください。

長く考えず、明確に分かる情報だけを短く抽出してください。
不明な項目は空配列にしてください。
スキーマを満たしたらすぐ終了してください。

RAG回答:
{rag_text}
"""

    result = await structured_llm_call(
        CitationContextDocument,
        system_prompt,
        user_prompt,
    )

    context_doc = result  # type: ignore[assignment]

    print("[STAGE 1] context document completed")
    print(f"[STAGE 1] key_terms={len(context_doc.key_terms)}")
    print(f"[STAGE 1] grounded_claims={len(context_doc.grounded_claims)}")
    print(f"[STAGE 1] missing_context={len(context_doc.missing_context)}")

    return context_doc


# ============================================================
# Stage 2: candidate question generation with LLM only
# ============================================================

async def generate_candidate_questions(
    seed_chunk: str,
    context_doc: CitationContextDocument,
) -> CandidateQuestionList:
    print("\n" + "=" * 90)
    print("[STAGE 2] LLMのみで候補質問を生成中... /ask は使いません")
    print("=" * 90)

    system_prompt = """
あなたは日本語技術文書に対する「深いドキュメントギャップ」発見エージェントです。

目的:
与えられたシードチャンクとRAG文脈情報から、後続のRAG確認で検証する価値がある
「深い・自然・専門家向けの未回答候補質問」だけを生成してください。

最重要方針:
- シードチャンクや文脈情報に既に書かれている内容を言い換えた質問は禁止です。
- 項目名だけを見て「具体的な違いは何か」「定義は何か」と聞く浅い質問は禁止です。
- 単なる一覧化、マトリクス化、用語説明、機能差分確認は禁止です。
- 「3.1.9を参照」など、参照先が明示されているだけの項目について、
  その参照先に普通に書いてありそうな表面的質問は避けてください。
- 質問は、仕様設計者・保守者・運用者が本当に困る可能性がある
  境界条件、異常系、設定間の相互作用、実行時挙動、障害時の結果に限定してください。

浅い質問として禁止する例:
- プロセス種別ごとの機能的な違いは何か？
- pp, ep, mp, sp, fp の使用可能機能マトリクスは何か？
- TSSプライオリティとは何か？
- 重故障と軽故障の違いは何か？
- ネットワーク上のロケーション指定の形式は何か？
- イベント／メッセージキューバッファサイズとは何か？
- この項目はどのように定義されているか？

良い質問の条件:
- 既存文脈だけでは答えられない理由が明確である。
- シードチャンク内の複数の設定項目・制約・状態が組み合わさっている。
- 異常系、上限超過、不整合設定、起動失敗、共有メモリ反映、再起動、タイムアウト、
  多重起動、親子関係、分散配置、監視、故障分類などの実務的な問題を扱う。
- 回答には、仕様本文・状態遷移・エラーコード・ログ・通知・復旧動作などの
  具体的根拠が必要になる。

良い質問の例:
- プログラム定義ファイルがバイナリ化され共有メモリ上に保持された後、定義変更はオンライン運転中に再読込・反映されるのか、それとも再起動が必要なのか？
- 最大起動可能プロセス数に達した状態で親プロセスがさらに子プロセス起動を要求した場合、返却エラー、ログ出力、既存プロセスへの影響はどう定義されているか？
- ネットワーク上のロケーション指定先が利用不能な場合、代替計算機への起動、リトライ、故障通知、起動失敗の扱いはどうなるか？
- 消費CPU時間監視と重故障／軽故障フラグが同時に関係する場合、WDT検出後の故障分類、通知、プロセス終了処理の順序はどう定義されているか？
- デーモン／非デーモン、常駐／非常駐、親子プロセス制約が矛盾する設定になった場合、定義ファイル変換時と実行時のどちらで検出されるのか？

スコア基準:
- depth_score:
  1 = 用語説明だけ
  2 = 単一項目の詳細確認
  3 = 通常仕様確認
  4 = 境界条件・異常系・設定相互作用を含む
  5 = 実行時影響、障害処理、運用判断に直結する深い問い
- novelty_score:
  1 = シードチャンクの言い換え
  2 = 既に書かれた項目の単純な詳細化
  3 = 自然だが普通の補足質問
  4 = 文脈から自然に生じる未記述の実務的論点
  5 = 複数仕様の交差点にある重要な未記述論点
- plausibility_score: 文脈から自然に出る質問か
- usefulness_score: 専門家にとって有用か
- weirdness_risk_score: 不自然・人工的・冗談っぽいリスク

必須条件:
- depth_score は 4 以上にしてください。
- novelty_score は 4 以上にしてください。
- plausibility_score は 4 以上にしてください。
- usefulness_score は 4 以上にしてください。
- weirdness_risk_score は 2 以下にしてください。
- 各候補には why_not_answered_by_current_context を必ず書いてください。
- 各候補には expected_evidence_needed を必ず書いてください。
- 本当に深い候補がなければ questions=[] を返してください。

出力:
- 質問文は必ず日本語。
- 最大件数まで無理に埋めないでください。
- 浅い候補を数で埋めるより、少数の深い候補を返してください。
"""

    user_prompt = f"""
シードチャンク:
{seed_chunk}

RAGから得られた文脈情報:
{context_doc.model_dump_json(indent=2)}

この情報から、後続のRAGチェックで検証すべき「深い未回答候補質問」だけを生成してください。

特に見るべきギャップ:
- missing_context
- explicit_limitations
- assumptions
- grounded_claims にあるが、実行時挙動・異常系・運用結果が不足している部分
- シードチャンクでは設定項目だけ列挙されているが、設定間の相互作用が不明な部分
- オンライン運転時にバイナリ形式へ変換され共有メモリに保持されることによる反映・整合性・障害時挙動
- 多重起動、親子関係、デーモン/非デーモン、常駐/非常駐、CPU時間監視、故障分類、ネットワーク配置、バッファサイズの相互作用

避けるべき候補:
- 単なる「具体的な違いは何か」
- 単なる「形式は何か」
- 単なる「定義は何か」
- 単なる「機能マトリクスは何か」
- シードチャンクの項目名をそのまま質問化したもの
- RAG文脈で既に回答されていそうなもの

最大 {MAX_CANDIDATE_QUESTIONS} 件まで。
ただし、深い候補が少なければ少数で構いません。
"""

    result = await structured_llm_call(
        CandidateQuestionList,
        system_prompt,
        user_prompt,
    )

    parsed = result  # type: ignore[assignment]

    print(f"[STAGE 2 DEBUG] raw structured candidates: {len(parsed.questions)}")

    for i, q in enumerate(parsed.questions, start=1):
        print(f"[STAGE 2 DEBUG] raw candidate {i}: {q.question}")
        print(
            f"  scores: plausibility={q.plausibility_score}, "
            f"usefulness={q.usefulness_score}, "
            f"weirdness={q.weirdness_risk_score}, "
            f"depth={q.depth_score}, "
            f"novelty={q.novelty_score}"
        )
        print(f"  why_not_answered={q.why_not_answered_by_current_context}")
        print(f"  evidence_needed={q.expected_evidence_needed}")

    filtered: list[CandidateQuestion] = []
    seen: set[str] = set()

    shallow_patterns = [
        "機能的な違い",
        "使用可能な機能のマトリクス",
        "とは何",
        "定義されているか",
        "具体的な記述形式",
        "どう使い分ける",
        "どのような指定形式",
        "具体的な機能",
    ]

    for q in parsed.questions:
        if q.question in seen:
            print(f"[LOCAL FILTER] rejected duplicate: {q.question}")
            continue
        seen.add(q.question)

        if should_reject_locally(q.question):
            print(f"[LOCAL FILTER] rejected question: {q.question}")
            continue

        if any(p in q.question for p in shallow_patterns):
            print(f"[LOCAL FILTER] rejected shallow pattern: {q.question}")
            continue

        if q.depth_score < 4:
            print(f"[LOCAL FILTER] rejected low depth: {q.question}")
            continue

        if q.novelty_score < 4:
            print(f"[LOCAL FILTER] rejected low novelty: {q.question}")
            continue

        if q.plausibility_score < 4:
            print(f"[LOCAL FILTER] rejected low plausibility: {q.question}")
            continue

        if q.usefulness_score < 4:
            print(f"[LOCAL FILTER] rejected low usefulness: {q.question}")
            continue

        if q.weirdness_risk_score > 2:
            print(f"[LOCAL FILTER] rejected high weirdness: {q.question}")
            continue

        if not q.why_not_answered_by_current_context.strip():
            print(f"[LOCAL FILTER] rejected missing why_not_answered: {q.question}")
            continue

        if not q.expected_evidence_needed.strip():
            print(f"[LOCAL FILTER] rejected missing expected evidence: {q.question}")
            continue

        filtered.append(q)

    print(f"[STAGE 2] candidate count after local filter: {len(filtered)}")

    return CandidateQuestionList(questions=filtered[:MAX_CANDIDATE_QUESTIONS])

# ============================================================
# Fallback candidate generation from context_doc.missing_context
# ============================================================

def fallback_candidates_from_context(
    context_doc: CitationContextDocument,
) -> CandidateQuestionList:
    print("[FALLBACK] missing_context から候補質問を生成します")

    fallback: list[CandidateQuestion] = []

    for item in context_doc.missing_context:
        item_lower = item.lower()

        if "重故障" in item or "軽故障" in item:
            fallback.append(
                CandidateQuestion(
                    question="プロセス異常終了時に重故障または軽故障として扱う具体的な判定基準と、その後続処理はどのように定義されているか？",
                    question_type="failure_mode",
                    rationale="プログラム定義ファイルには重故障／軽故障の扱いを示すフラグがあるが、具体的な判定基準と処理内容がシードチャンク内では説明されていない。",
                    why_docs_should_reasonably_cover_it="異常終了時の故障分類は運用・監視・復旧動作に直結するため、プロセス管理仕様として自然に説明されるべき内容である。",
                    source_gap_or_tension=item,
                    expected_expert_value="障害時の影響範囲、アラーム設計、復旧手順、運用判断の明確化に役立つ。",
                    plausibility_score=5,
                    usefulness_score=5,
                    weirdness_risk_score=1,
                )
            )

        if "tss" in item_lower or "プライオリティ" in item:
            fallback.append(
                CandidateQuestion(
                    question="リアルタイムプライオリティの値0〜31とTSSプライオリティは、スケジューリング上どのように解釈され、相互にどのような関係を持つのか？",
                    question_type="operational",
                    rationale="プログラム定義ファイルに優先度指定が存在するが、値の意味やTSS指定時の動作がシードチャンク内では明確ではない。",
                    why_docs_should_reasonably_cover_it="優先度はプロセス起動管理と実行時性能に直接影響するため、仕様文書に含まれていて自然な内容である。",
                    source_gap_or_tension=item,
                    expected_expert_value="性能設計、リアルタイム性評価、優先度設定ミスの防止に役立つ。",
                    plausibility_score=5,
                    usefulness_score=4,
                    weirdness_risk_score=1,
                )
            )

        if "計算機種別" in item or "ネットワーク" in item or "ロケーション" in item:
            fallback.append(
                CandidateQuestion(
                    question="プログラム定義ファイルのネットワーク上のロケーション指定における計算機種別は、どの形式で記述され、プロセス起動先の選択にどのように使われるのか？",
                    question_type="compatibility",
                    rationale="プログラム定義項目としてネットワーク上のロケーション指定が挙げられているが、具体的な記述形式や起動先決定ロジックが不明である。",
                    why_docs_should_reasonably_cover_it="分散環境でプロセスをどこに起動するかは運用・構成管理上重要であり、仕様として自然に説明されるべき内容である。",
                    source_gap_or_tension=item,
                    expected_expert_value="分散構成時の設定、配備、障害切り分け、互換性確認に役立つ。",
                    plausibility_score=5,
                    usefulness_score=5,
                    weirdness_risk_score=1,
                )
            )

        if "エラー処理コード" in item or "未処理" in item:
            fallback.append(
                CandidateQuestion(
                    question="プログラム定義ファイルのエラー処理コードが現在未処理である場合、設定値は無視されるのか、将来互換用として保持されるのか、また異常時の挙動に影響するのか？",
                    question_type="limitation",
                    rationale="シードチャンクではエラー処理コードが現在未処理とされているが、設定値の扱いや互換性上の意味が明確ではない。",
                    why_docs_should_reasonably_cover_it="未実装または未処理の設定項目は、設定ミス、将来互換性、運用時の期待値に影響するため、仕様として説明されるべき内容である。",
                    source_gap_or_tension=item,
                    expected_expert_value="設定ファイル作成時の判断、保守時の互換性確認、異常時の期待挙動の明確化に役立つ。",
                    plausibility_score=5,
                    usefulness_score=4,
                    weirdness_risk_score=1,
                )
            )

    # 重複排除
    seen: set[str] = set()
    unique: list[CandidateQuestion] = []

    for q in fallback:
        if q.question in seen:
            continue
        seen.add(q.question)
        unique.append(q)

    print(f"[FALLBACK] fallback candidate count: {len(unique)}")
    return CandidateQuestionList(questions=unique[:MAX_CANDIDATE_QUESTIONS])


# ============================================================
# Stage 3: direct gap check
# ============================================================

async def check_question_gap(
    seed_chunk: str,
    context_doc: CitationContextDocument,
    candidate: CandidateQuestion,
) -> GapCheckResult:
    ask_prompt = f"""
あなたはRAGコーパスに基づく厳密なギャップ判定器です。

以下の候補質問が、利用可能なドキュメントによって回答済みかどうか判定してください。

シードチャンク:
{seed_chunk}

文脈情報:
{context_doc.model_dump_json(indent=2)}

候補質問:
{candidate.model_dump_json(indent=2)}

判定基準:
- ドキュメントが直接答えているなら answered
- 一部だけ答えているなら partially_answered
- 関連情報はあるが、この質問に直接答えていないなら unanswered または partially_answered
- 範囲外なら out_of_scope
- 不自然・冗談・人工的な質問なら nonsensical または too_artificial

必ず確認してください:
- 答えがある場合は、根拠となる出典・引用を示してください。
- 答えがない場合は、何が不足しているかを具体的に示してください。
- この質問が真面目な専門家にとって自然かどうかも評価してください。

重要:
- 「関連情報がある」だけでは回答済みとはしないでください。
- 質問の核心に直接答えているかを判定してください。
- 日本語で回答してください。
"""

    raw = await ask_cached(ask_prompt, f"gap_check: {candidate.question[:80]}")
    rag_text = response_to_text(raw)

    system_prompt = """
あなたはRAG回答を GapCheckResult に変換する厳密な構造化抽出器です。

ルール:
- ドキュメントが直接答えている場合は status='answered', recommendation='reject' にしてください。
- 質問が不自然、人工的、範囲外なら recommendation='reject' にしてください。
- 本当に有用で、未回答または部分未回答の場合だけ recommendation='keep' にしてください。
- 根拠や引用を捏造してはいけません。
- RAG回答が曖昧な場合は、保守的に判断してください。
"""

    user_prompt = f"""
候補質問:
{candidate.question}

以下のRAG回答を GapCheckResult に変換してください。

RAG回答:
{rag_text}
"""

    result = await structured_llm_call(
        GapCheckResult,
        system_prompt,
        user_prompt,
    )

    return result  # type: ignore[return-value]


# ============================================================
# Stage 4: adversarial check
# ============================================================

async def adversarial_answer_check(
    seed_chunk: str,
    context_doc: CitationContextDocument,
    candidate: CandidateQuestion,
) -> AdversarialCheckResult:
    ask_prompt = f"""
あなたは反証的なRAG検証エージェントです。

目的:
次の質問が「実はドキュメント内で回答済みである」ことを、できるだけ強く証明しようとしてください。

シードチャンク:
{seed_chunk}

文脈情報:
{context_doc.model_dump_json(indent=2)}

質問:
{candidate.question}

作業:
- ドキュメントから直接答えを探してください。
- 決定的な根拠があれば、回答済みと判断してください。
- 関連しているだけの曖昧な記述は、回答済みとはみなしません。
- 質問自体が不自然・人工的・範囲外なら、その理由を述べてください。
- 引用や出典を捏造しないでください。

重要:
- この質問の核心に対する直接回答があるかを確認してください。
- 周辺説明だけでは「回答済み」としないでください。
- 日本語で回答してください。
"""

    raw = await ask_cached(ask_prompt, f"adversarial_check: {candidate.question[:80]}")
    rag_text = response_to_text(raw)

    system_prompt = """
あなたは反証的RAG回答を AdversarialCheckResult に変換する抽出器です。

ルール:
- 決定的に回答できる場合だけ can_be_answered_from_docs=true。
- 直接の根拠がない場合は can_be_answered_from_docs=false。
- 根拠が曖昧な場合は decisive_citations_found=false。
- 質問が不自然なら is_reasonable_question=false にしてください。
- 根拠や引用を捏造してはいけません。
"""

    user_prompt = f"""
質問:
{candidate.question}

以下のRAG回答を AdversarialCheckResult に変換してください。

RAG回答:
{rag_text}
"""

    result = await structured_llm_call(
        AdversarialCheckResult,
        system_prompt,
        user_prompt,
    )

    return result  # type: ignore[return-value]


# ============================================================
# Final decision
# ============================================================

def should_keep_final(
    candidate: CandidateQuestion,
    gap_check: GapCheckResult,
    adversarial: AdversarialCheckResult,
) -> bool:
    if should_reject_locally(candidate.question):
        return False

    if gap_check.recommendation != "keep":
        return False

    if gap_check.status not in {"unanswered", "partially_answered"}:
        return False

    if gap_check.plausibility_score < 4:
        return False

    if gap_check.usefulness_score < 4:
        return False

    if gap_check.weirdness_risk_score > 2:
        return False

    if not gap_check.why_question_is_reasonable:
        return False

    if adversarial.can_be_answered_from_docs:
        return False

    if adversarial.decisive_citations_found:
        return False

    if not adversarial.is_reasonable_question:
        return False

    if adversarial.reject_reason_if_any:
        return False

    if gap_check.status == "partially_answered" and not gap_check.missing_information:
        return False

    return True


def build_final_question(
    candidate: CandidateQuestion,
    gap_check: GapCheckResult,
    adversarial: AdversarialCheckResult,
) -> FinalGapQuestion:
    why = gap_check.why_not_answered_or_only_partial or ""

    if adversarial.remaining_gap:
        why = f"{why}\n反証チェック後に残ったギャップ: {adversarial.remaining_gap}".strip()

    return FinalGapQuestion(
        question=candidate.question,
        question_type=candidate.question_type,
        rationale=candidate.rationale,
        source_gap_or_tension=candidate.source_gap_or_tension,
        expected_expert_value=candidate.expected_expert_value,
        why_docs_should_reasonably_cover_it=candidate.why_docs_should_reasonably_cover_it,
        why_it_is_not_answered=why,
        missing_information=gap_check.missing_information,
        plausibility_score=gap_check.plausibility_score,
        usefulness_score=gap_check.usefulness_score,
        weirdness_risk_score=gap_check.weirdness_risk_score,
        confidence=gap_check.confidence,
    )


async def process_one_candidate(
    idx: int,
    total: int,
    seed_chunk: str,
    context_doc: CitationContextDocument,
    candidate: CandidateQuestion,
) -> dict[str, Any]:
    print("\n" + "=" * 90)
    print(f"[CANDIDATE {idx}/{total}] start")
    print(f"[QUESTION] {candidate.question}")
    print("=" * 90)

    record: dict[str, Any] = {
        "candidate": candidate.model_dump(),
        "gap_check": None,
        "adversarial_check": None,
        "kept": False,
        "final_question": None,
        "error": None,
    }

    try:
        print(f"[CANDIDATE {idx}/{total}] gap check...")
        gap_check = await check_question_gap(seed_chunk, context_doc, candidate)
        record["gap_check"] = gap_check.model_dump()

        print(
            f"[CANDIDATE {idx}/{total}] gap status={gap_check.status}, "
            f"recommendation={gap_check.recommendation}, "
            f"confidence={gap_check.confidence}/5"
        )

        print(f"[CANDIDATE {idx}/{total}] adversarial check...")
        adversarial = await adversarial_answer_check(seed_chunk, context_doc, candidate)
        record["adversarial_check"] = adversarial.model_dump()

        print(
            f"[CANDIDATE {idx}/{total}] adversarial "
            f"answered={adversarial.can_be_answered_from_docs}, "
            f"decisive={adversarial.decisive_citations_found}, "
            f"reasonable={adversarial.is_reasonable_question}"
        )

        keep = should_keep_final(candidate, gap_check, adversarial)
        record["kept"] = keep

        if keep:
            final_q = build_final_question(candidate, gap_check, adversarial)
            record["final_question"] = final_q.model_dump()
            print(f"[CANDIDATE {idx}/{total}] DECISION: KEEP")
        else:
            print(f"[CANDIDATE {idx}/{total}] DECISION: reject")

    except Exception as exc:
        record["error"] = repr(exc)
        print(f"[CANDIDATE {idx}/{total}] ERROR: {repr(exc)}")

    return record


# ============================================================
# Save outputs
# ============================================================

def save_final_txt(
    final_questions: list[FinalGapQuestion],
    context_doc: CitationContextDocument,
) -> None:
    lines: list[str] = []

    lines.append("RAG GAP FINDER — ドキュメント内で未回答と思われる質問")
    lines.append("=" * 90)
    lines.append("")
    lines.append("シード要約:")
    lines.append(context_doc.seed_summary.strip())
    lines.append("")
    lines.append(f"最終採用質問数: {len(final_questions)}")
    lines.append("")

    if not final_questions:
        lines.append("高信頼度の未回答質問は見つかりませんでした。")
        lines.append("")
        lines.append("解釈:")
        lines.append("- 候補質問が既にドキュメントで回答されていた可能性があります。")
        lines.append("- または、残った候補が弱い、不自然、範囲外、専門家価値が低いと判定されました。")
        lines.append("- この結果は正常です。このプロトタイプは保守的に設計されています。")
    else:
        for i, q in enumerate(final_questions, start=1):
            lines.append(f"{i}. {q.question}")
            lines.append("")
            lines.append(f"   種別: {q.question_type}")
            lines.append(f"   信頼度: {q.confidence}/5")
            lines.append(f"   妥当性: {q.plausibility_score}/5")
            lines.append(f"   有用性: {q.usefulness_score}/5")
            lines.append(f"   不自然さリスク: {q.weirdness_risk_score}/5")
            lines.append("")
            lines.append(f"   理由: {q.rationale}")
            lines.append("")
            lines.append(f"   元になったギャップ/緊張関係: {q.source_gap_or_tension}")
            lines.append("")
            lines.append(f"   専門家にとっての価値: {q.expected_expert_value}")
            lines.append("")
            lines.append(f"   なぜドキュメントが扱うべき自然な問いか: {q.why_docs_should_reasonably_cover_it}")
            lines.append("")
            lines.append(f"   なぜ未回答と思われるか: {q.why_it_is_not_answered}")
            lines.append("")
            if q.missing_information:
                lines.append("   不足している情報:")
                for item in q.missing_information:
                    lines.append(f"   - {item}")
                lines.append("")
            lines.append("-" * 90)
            lines.append("")

    FINAL_TXT_PATH.write_text("\n".join(lines), encoding="utf-8")


def save_audit_json(audit: dict[str, Any]) -> None:
    AUDIT_JSON_PATH.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ============================================================
# Main orchestrator
# ============================================================

async def run_gap_finder() -> None:
    audit: dict[str, Any] = {
        "endpoint": ASK_ENDPOINT,
        "llm_base_url": LLM_BASE_URL,
        "llm_model": LLM_MODEL,
        "concurrency": CONCURRENCY,
        "seed_chunk": SEED_CHUNK,
        "started_at_unix": time.time(),
        "context_document": None,
        "candidate_questions_raw_or_filtered": [],
        "candidate_questions": [],
        "candidate_records": [],
        "final_questions": [],
        "errors": [],
    }

    try:
        print("\n" + "#" * 90)
        print("[START] RAG Gap Finder")
        print("#" * 90)

        context_doc = await compile_citation_context(SEED_CHUNK)
        audit["context_document"] = context_doc.model_dump()
        save_audit_json(audit)

        candidate_list = await generate_candidate_questions(SEED_CHUNK, context_doc)
        candidates = candidate_list.questions

        if not candidates:
            print("\n" + "!" * 90)
            print("[FALLBACK] LLM candidate generation returned zero candidates.")
            print("[FALLBACK] Building candidates from context_doc.missing_context...")
            print("!" * 90)
            fallback_list = fallback_candidates_from_context(context_doc)
            candidates = fallback_list.questions

        audit["candidate_questions"] = [q.model_dump() for q in candidates]
        save_audit_json(audit)

        print("\n" + "#" * 90)
        print(f"[STAGE 3/4] 候補質問を concurrency={CONCURRENCY} で処理します")
        print(f"[STAGE 3/4] total candidates={len(candidates)}")
        print("#" * 90)

        all_records: list[dict[str, Any]] = []

        indexed_candidates = list(enumerate(candidates, start=1))
        batches = chunked(indexed_candidates, CONCURRENCY)

        for batch_no, batch in enumerate(batches, start=1):
            print("\n" + "=" * 90)
            print(f"[BATCH {batch_no}/{len(batches)}] processing {len(batch)} questions")
            print("=" * 90)

            tasks = [
                asyncio.wait_for(
                    process_one_candidate(
                        idx=idx,
                        total=len(candidates),
                        seed_chunk=SEED_CHUNK,
                        context_doc=context_doc,
                        candidate=candidate,
                    ),
                    timeout=CANDIDATE_TOTAL_TIMEOUT_SECONDS,
                )
                for idx, candidate in batch
            ]

            raw_batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            batch_records: list[dict[str, Any]] = []
            for result in raw_batch_results:
                if isinstance(result, Exception):
                    batch_records.append(
                        {
                            "candidate": None,
                            "gap_check": None,
                            "adversarial_check": None,
                            "kept": False,
                            "final_question": None,
                            "error": repr(result),
                        }
                    )
                else:
                    batch_records.append(result)

            all_records.extend(batch_records)

            audit["candidate_records"] = all_records
            save_audit_json(audit)

        final_questions: list[FinalGapQuestion] = []

        for record in all_records:
            if record.get("kept") and record.get("final_question"):
                final_questions.append(
                    FinalGapQuestion.model_validate(record["final_question"])
                )

        final_questions = sorted(
            final_questions,
            key=lambda q: (
                q.confidence,
                q.usefulness_score,
                q.plausibility_score,
                -q.weirdness_risk_score,
            ),
            reverse=True,
        )[:MAX_FINAL_QUESTIONS]

        audit["final_questions"] = [q.model_dump() for q in final_questions]
        audit["finished_at_unix"] = time.time()

        print("\n" + "#" * 90)
        print("[SAVE] final txt / audit json")
        print("#" * 90)

        save_final_txt(final_questions, context_doc)
        save_audit_json(audit)

        print("\n" + "#" * 90)
        print("[DONE]")
        print(f"Final TXT: {FINAL_TXT_PATH.resolve()}")
        print(f"Audit JSON: {AUDIT_JSON_PATH.resolve()}")
        print(f"Ask cache JSON: {ASK_CACHE_PATH.resolve()}")
        print(f"Final kept question count: {len(final_questions)}")
        print("#" * 90)

    except Exception as exc:
        audit["errors"].append(
            {
                "stage": "top_level",
                "error": repr(exc),
            }
        )
        audit["finished_at_unix"] = time.time()
        save_audit_json(audit)
        print(f"[FATAL ERROR] {repr(exc)}")
        raise


if __name__ == "__main__":
    asyncio.run(run_gap_finder())
