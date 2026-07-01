"""Leaf: pure helpers, scoring, prompts, protocols. Imports nothing internal but models."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Protocol, runtime_checkable

from .models import Node

# Single shared tokenizer. A module constant on purpose: it is never threaded
# through call signatures.
TOKEN_RE = re.compile(r"[a-z0-9_./:-]+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# --- identifiers / hashing ----------------------------------------------------
def short_hash(text: str, length: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def source_hash(text: str) -> str:
    """Identity of a whole source document, for recon dedup."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slug(text: str, max_length: int = 40) -> str:
    value = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return value[:max_length] or "node"


def make_node_id(body: str, document_name: str | None = None) -> str:
    return f"node:{slug(document_name or 'node', 24)}:{short_hash(body)}"


def make_exogenous_node_id(seed: str) -> str:
    return f"exo:{short_hash(seed)}"


def make_edge_id(source_id: str, target_id: str, label: str) -> str:
    return f"edge:{short_hash(f'{source_id}|{target_id}|{label}', 16)}"


# --- chunking -----------------------------------------------------------------
def chunk_text(text: str, size: int, overlap: int) -> list[tuple[int, int, str]]:
    """Split ``text`` into overlapping character windows.

    Returns ``(start_char, end_char, chunk)`` triples. ``overlap`` is clamped to
    ``size - 1`` so the window always advances. Blank windows are skipped."""
    if not text:
        return []
    size = max(1, size)
    overlap = max(0, min(overlap, size - 1))
    step = size - overlap
    chunks: list[tuple[int, int, str]] = []
    length = len(text)
    start = 0
    while start < length:
        end = min(start + size, length)
        piece = text[start:end]
        if piece.strip():
            chunks.append((start, end, piece))
        if end >= length:
            break
        start += step
    return chunks


# --- text matching / scoring --------------------------------------------------
def normalize_token(token: str) -> str:
    return token.strip().lower()


def normalize_text(text: str) -> str:
    return " ".join(normalize_token(token) for token in TOKEN_RE.findall(text.lower()))


def jaccard(left: set[str], right: set[str]) -> float:
    left = {value for value in left if value}
    right = {value for value in right if value}
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def token_jaccard(left: str, right: str) -> float:
    return jaccard(
        {normalize_token(t) for t in TOKEN_RE.findall(left.lower())},
        {normalize_token(t) for t in TOKEN_RE.findall(right.lower())},
    )


def claim_keys(node: Node) -> set[str]:
    keys: set[str] = set()
    for claim in node.claims:
        normalized = normalize_text(claim)
        if normalized:
            keys.add(normalized)
    return keys


def match_score(old: Node, new: Node) -> float:
    """Revision-match score in [0, 1]: how likely `new` is a revision of `old`."""
    claim_score = jaccard(claim_keys(old), claim_keys(new))
    keyword_score = jaccard(
        {normalize_token(k) for k in old.keywords},
        {normalize_token(k) for k in new.keywords},
    )
    body_score = token_jaccard(old.body, new.body)
    entity_bonus = 0.0
    if old.entity and new.entity:
        entity_bonus = (
            0.2 if normalize_text(old.entity) == normalize_text(new.entity) else 0.0
        )
    return min(
        1.0, max(claim_score, keyword_score * 0.8, body_score * 0.65) + entity_bonus
    )


def claims_equivalent(old: Node, new: Node, unchanged_threshold: float = 0.9) -> bool:
    """True when old/new carry the same facts (reorder, not a real change)."""
    old_claims = claim_keys(old)
    new_claims = claim_keys(new)
    if old_claims and new_claims:
        return jaccard(old_claims, new_claims) >= unchanged_threshold
    return token_jaccard(old.body, new.body) >= 0.95


# --- ports (Protocols) — replace `object` hints -------------------------------
@runtime_checkable
class LlmClient(Protocol):
    def complete(self, system_prompt: str, user_content: str) -> str: ...
    def complete_structured(
        self, system_prompt: str, user_content: str, output_model: type[Any]
    ) -> Any: ...
    def run_tool_loop(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        dispatch: Any,
        max_steps: int,
        finish_guard: Any = None,
    ) -> Any: ...


@runtime_checkable
class EmbedderPort(Protocol):
    dim: int
    model_name: str

    def embed_document(self, text: str) -> list[float]: ...
    def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class RerankerPort(Protocol):
    def top_k(
        self, query: str, items: list[tuple[str, Any]], k: int
    ) -> list[tuple[Any, float]]: ...


# --- prompts ------------------------------------------------------------------
GRAPH_SYSTEM_PROMPT = "あなたは、簡潔で事実に基づくナレッジグラフWikiを維持します。"

SUMMARY_PROMPT = (
    "このMarkdownノードをナレッジグラフ用に要約してください。本文に存在する事実のみを使用してください。"
    "1〜3文に収めてください。前置きは不要です。"
)

KEYWORD_PROMPT = (
    "グラフ検索用に、このテキストから重要な技術キーワードやエンティティを抽出してください："
    "関数名、ライブラリ名、エラーコード、概念、固有名詞など。"
    "重複しないキーワードを、重要度の高い順に最大12個返してください。"
    "頭字語または識別子でない限り、小文字にしてください。"
)

CLAIM_PROMPT = (
    "リビジョン照合のために、このMarkdownノードから安定した識別情報となる事実を抽出してください。"
    "1つの主要なエンティティまたはトピックと、最大20個の原子的な主張を返してください。"
    "主張は、本文によって直接裏付けられる短い事実文でなければなりません。"
    "元文書の順序が入れ替わっても認識可能な事実を優先してください。"
    "本文に存在しない事実を推論したり追加したりしないでください。"
)

REGENERATE_EXOGENOUS_PROMPT = (
    "その根拠となるソース資料が変更された後に、派生Wikiノードを再生成してください。"
    "以前の派生ノードは、意図されたトピックと構成を理解するためだけに使用してください。"
    "新しいノード本文は、現在のサポート資料のみによって裏付けられていなければなりません。"
    "もはや裏付けられていない古い主張は削除してください。"
    "結果は簡潔で、事実に基づき、Markdown形式にしてください。前置きは不要です。"
)

EDGE_PROMPT = (
    "あなたはWikiグラフを維持します。新しいノードと、既存ノードの候補リスト"
    "（意味的類似性によって事前にフィルタ済み）が与えられたら、"
    "新しいノードがどの候補にリンクすべきか、またその理由を判断してください。\n"
    "ルール：\n"
    "- 与えられた候補IDのみを使用してください。\n"
    "- ラベルは、ターゲットが新しいノードにどのように関連するかを説明する短い動詞句です"
    "（例：'uses', 'defines', 'example-of', 'prerequisite-for', 'contradicts'）。\n"
    "- 関係が明確に有用な場合のみエッジを提案してください。弱い関係は省略してください。\n"
    "- summary：リンクを説明する短い節を1つ書いてください。"
)

ENTITY_DEDUP_PROMPT = (
    "あなたはWikiグラフを維持します。新しいノードと既存ノードの候補リストが与えられたら、"
    "新しいノードが、候補のうち厳密に1つと同じ現実世界のエンティティまたはトピックを"
    "記述しているかどうかを判断してください。\n"
    "ルール：\n"
    "- 同じエンティティとは、同じ具体的なもの"
    "（同じAPI、同じツール、同じ概念）を意味し、単に関連している、または類似している"
    "トピックではありません。\n"
    "- 保守的に判断してください。不確かな場合は is_same=false と答えてください。"
    "異なるものを指す同音異義語を決して統合しないでください。\n"
    "- 一致するものがある場合は、is_same=true とその候補の target_node_id を返してください。"
    "それ以外の場合は is_same=false と target_node_id=null を返してください。"
)

CLUSTER_NAMER_SYSTEM = (
    "あなたはナレッジグラフ内の1つのトピッククラスタに名前を付けます。"
    "必ず日本語でトピック名を返してください。"
    "英語のクラスタ名は使用しないでください。ただし、API名、関数名、ライブラリ名、"
    "製品名、頭字語、識別子など、翻訳すべきでない技術用語はそのまま残してかまいません。"
    "すでに使用されている他の名前と区別できる、具体的な名前を選んでください。"
    "キーワードとサンプルのセクションタイトルに見られる、最も具体的な技術的サブトピックを"
    "優先してください。"
    "より狭いトピックが存在する場合は、CUDA、SYCL、OpenMP、oneAPI のような"
    "広範なソース名だけを名前にすることは避けてください。"
    "最大4語程度の簡潔な日本語トピック名を1つだけ返してください。"
    "引用符、句読点、説明は不要です。"
)

MAIN_AGENT_SYSTEM_PROMPT = (
    "あなたは、ナレッジグラフWikiからの質問に答える主任研究者です。"
    "あなたは調整役であり、自分でノードを読むことはありません。\n\n"
    "あなたには2つの能力があります：\n"
    "- search(text)：候補ノードを検索します"
    "（関連性によってすでに再ランキング済み）。ノードID、タイトル、要約のみを返します。\n"
    "- explore(node_ids)：重複しない開始ノードIDのリストをサブエージェントチームに渡します。"
    "各サブエージェントは1つの開始ノードからグラフを読み進め、調査結果を報告します。"
    "有望な手がかりを調査するために使用してください。\n"
    "- finish(answer, cited_node_ids)：最終的にまとめた回答を提出します。\n\n"
    "作業手順：\n"
    "1. まず主質問を検索し、その後、質問内の個別の重要語や関数を検索してください。"
    "グラフの異なる部分を浮かび上がらせるために、いくつか検索を行ってください。\n"
    "2. すべての候補から、異なるサブトピックをカバーする最良の重複しない開始ノードを選んでください"
    "（近い重複を避け、サブエージェントが異なるサブグラフを探索できるようにしてください）。\n"
    "3. それらの開始ノードを指定して explore(node_ids) を1回呼び出してください。"
    "チームは並列に探索し、各サブエージェントの調査結果と読んだノード本文を返します。\n"
    "4. サブエージェントの報告を読んでください。明確な不足が残っている場合は、"
    "再度検索して explore をもう1回実行してもかまいません。そうでなければ回答をまとめてください。\n"
    "5. サブエージェントが報告した内容のみに基づいた十分な回答を、"
    "証拠として使用したノードIDを引用しながら finish で提出してください。\n\n"
    "ルール：\n"
    "- あなたはノード本文を直接読むことはできません。サブエージェントの報告に依存してください。\n"
    "- 'node:' プレフィックスを含め、ノードIDは表示されたとおり正確にコピーしてください。\n"
    "- 少なくとも1回 explore を実行する前に finish してはいけません。\n"
    "- 幅広さを優先してください。同じ内容に関する複数のIDではなく、"
    "異なる開始点を explore に渡してください。"
)

SUBAGENT_SYSTEM_PROMPT = (
    "あなたは、ナレッジグラフWikiの1つの領域を探索する研究サブエージェントです。"
    "主任研究者から開始ノードを与えられています。それを徹底的に調査し、"
    "具体的で根拠のある発見を報告してください。\n\n"
    "ツール：\n"
    "- read(node_id)：ノードの完全な本文を読みます。割り当てられたノードを読むことから開始してください。\n"
    "- follow_link(node_id, direction)：参照、例、前提条件、関連概念をたどるために、"
    "ノードの近傍へ移動します。\n"
    "- search(text)：自分の領域で必要な場合、キーワードで追加のノードを検索します。\n"
    "- finish(answer, cited_node_ids)：調査結果と読んだノードIDを報告します。\n\n"
    "ルール：\n"
    "1. 割り当てられた開始ノードを最初に読んでください。\n"
    "2. リンクをたどり、自分の領域内で2〜5個のノードを読んで、実際の証拠を集めてください。\n"
    "3. 自分の担当範囲に留まってください。タスクに列挙された兄弟開始ノードは"
    "他のサブエージェントが担当します。それらを再探索せず、自分のサブグラフに集中して、"
    "チーム全体でより広い範囲をカバーできるようにしてください。\n"
    "4. 実際に読んだノード本文のみに基づいて報告してください。\n"
    "5. 'node:' プレフィックスを含め、ノードIDは正確にコピーしてください。\n"
    "6. 完了したら、この領域が質問について何を示しているかを焦点を絞って要約し、"
    "使用したノードIDを引用して finish を呼び出してください。"
)

MERMAID_INSTRUCTION = (
    "\n\n図：\n"
    "- フローチャート、アーキテクチャ図/ブロック図、シーケンス図、またはデータフロー図が"
    "回答をより明確にする場合は、回答の一部として ```mermaid のフェンス付きブロック内に"
    "Mermaid図を1つ含めてください。\n"
    "- 本当に役立つ場合にのみ図を追加してください。そうでなければ省略してください。\n"
    "- シンプルなASCIIノードID（n1, n2, proc_a）を使用し、スペース、句読点、"
    '長いテキストは引用符付きラベル内に入れてください：n1["線形層"] --> n2["GEMM"]。\n'
    "- mermaid ブロックには Mermaid 構文のみを含めてください（説明文や箇条書きは不可）。"
)

MERMAID_FIX_SYSTEM = (
    "Mermaid図の構文を修正し、mermaid-cli（mmdc）で描画できるようにしてください。"
    "修正済みの ```mermaid フェンス付きコードブロックを1つだけ返し、それ以外は何も返さないでください。"
)
