"""Independent helpers for `Graph`: agent tool schemas, pure formatters, and the
mermaid validate/repair subsystem. Nothing here touches a `Graph` instance."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

import hashlib
import re
from typing import Any, Protocol, runtime_checkable

from .models import *


# Single shared tokenizer. A module constant on purpose: compiling a regex is
# relatively expensive, so reuse one compiled instance across the module.

# regex to find mermaid blocks
_MERMAID_BLOCK_RE = re.compile(
    r"```mermaid[ \t]*\r?\n(?P<code>.*?)```", re.IGNORECASE | re.DOTALL
)

# Matches tokens like "foo/bar:v1.2", "abc_123", or "2025-01-31".
TOKEN_RE = re.compile(r"[a-z0-9_./:-]+")

# Matches separators/punctuation like " ", "_", ".", or "!!!" for slugification.
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# identifiers / hashing
def short_hash(text: str, length: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]

# Identity of a whole source document, for recon dedup
def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

# remove all special symbols and whitespace and repalce with hyphen
def slug(text: str, max_length: int = 40) -> str:
    value = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return value[:max_length] or "node"


def make_node_id(body: str, document_name: str | None = None) -> str:
    return f"node:{slug(document_name or 'node', 24)}:{short_hash(body)}"


def make_exogenous_node_id(seed: str) -> str:
    return f"exo:{short_hash(seed)}"


def make_edge_id(source_id: str, target_id: str, label: str) -> str:
    return f"edge:{short_hash(f'{source_id}|{target_id}|{label}', 16)}"


# chunking
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


# text matching / scoring 
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


# Prompts for various OPS 
GRAPH_SYSTEM_PROMPT = "あなたは、簡潔で事実に基づくナレッジグラフWikiを維持します。"

SUMMARY_PROMPT = (
    "このMarkdownノードをナレッジグラフ用に要約してください。本文に存在する事実のみを使用してください。"
    "1〜3文に収めてください。前置きは不要です。"
    "本文が質問への回答、手順、規則、方針、判断基準、変更方法、原因説明、条件、例外、"
    "期限、責任者、数値、対象範囲を含む場合は、それらの具体情報を優先して残してください。"
    "抽象的な言い換えで、文書名、節名、項目名、対象、条件、値、日付、役割、例外を失わないでください。"
    "本文に存在しない事実を推論したり追加したりしないでください。"
)

KEYWORD_PROMPT = (
    "グラフ検索用に、このテキストから重要なキーワードやエンティティを抽出してください："
    "文書名、制度名、規則名、項目名、見出し、対象名、組織名、役割名、担当者、場所、"
    "日付、期限、金額、数値、条件、例外、手順名、状態名、原因、結果、固有名詞、専門用語など。"
    "重複しないキーワードを、重要度の高い順に最大12個返してください。"
    "ユーザーの質問や本文が変更方法・対応方法・判断・原因・手順・条件に関する場合は、"
    "対象、必要な行動、条件、例外、判断基準、変更前後の状態を表す語を優先してください。"
    "頭字語または識別子でない限り、小文字にしてください。"
)

CLAIM_PROMPT = (
    "リビジョン照合のために、このMarkdownノードから安定した識別情報となる事実を抽出してください。"
    "1つの主要なエンティティまたはトピックと、最大20個の原子的な主張を返してください。"
    "主張は、本文によって直接裏付けられる短い事実文でなければなりません。"
    "元文書の順序が入れ替わっても認識可能な事実を優先してください。"
    "本文が規則、手順、方針、判断基準、変更方法、原因説明、条件、例外、期限、責任、"
    "数値、対象範囲を含む場合は、対象、条件、行動、結果、例外、日付、値、役割の関係を"
    "優先して抽出してください。"
    "本文に存在しない事実を推論したり追加したりしないでください。"
)

ROUTER_PROMPT = (
    "あなたはナレッジグラフWikiの質問ルーターです。質問と、検索で見つかった候補ノード"
    "（kind='agent_note' は過去にエージェントが作成した回答ノート、kind='source' は元資料）"
    "が与えられます。最も安価で十分な戦略を1つ選んでください。\n\n"

    "選択肢：\n"
    "- reuse: 候補内の agent_note が、質問と同じ範囲をカバーし、質問が求める具体性まで"
    "ほぼ完全に回答している場合。その候補の node_id を正確に返してください。"
    "回答はそのノート本文をそのまま再利用します。\n"
    "- shallow: 質問が狭く、候補の evidence 抜粋だけで正確かつ十分に回答できる場合。"
    "node_id は null にしてください。\n"
    "- deep: 質問が広い、複数箇所の文書確認が必要、候補同士が矛盾する、"
    "解釈・手順・変更・判断・原因・条件整理が必要、または証拠が不十分な場合。"
    "node_id は null にしてください。\n\n"

    "重要ルール：\n"
    "- 質問の短さだけで shallow を選んではいけません。短い質問でも、"
    "変更方法、対応方針、原因、理由、手順、例外条件、判断基準、複数条件の整理、"
    "文書間の関係確認が必要なら deep を選んでください。\n"
    "- ただし、質問が単一の事実を尋ねており、候補 evidence だけで直接回答できる場合は shallow を選んでください。"
    "例：期限、日付、担当者、金額、数値、名称、定義、単一条件、単一の記載箇所にある事実。\n"
    "- ユーザーが「何を変更」「どこを変更」「どうすれば」「なぜ」「どの手順」"
    "「どの条件」「何が必要」「何を確認」「どう対応」「どれを選ぶ」"
    "のように、行動・判断・解釈・変更・原因・手順を尋ねている場合は、原則 deep を選んでください。\n"
    "- そのような質問で shallow を選んでよいのは、候補 evidence だけで次のすべてが明確に分かる場合です："
    "対象、必要な行動または回答、根拠、条件、例外や制約がある場合はそれら。\n"
    "- agent_note の再利用は、単に関連しているだけでは選んではいけません。"
    "質問が求める範囲と具体性を満たしている場合のみ reuse を選んでください。\n"
    "- kind='agent_note' の最上位候補が質問に直接かつ十分な具体性で回答している場合は reuse を選んでください。"
    "ただし、回答が概念的・抽象的で、対象・条件・手順・根拠が不足している場合は reuse ではなく deep を選んでください。\n"
    "- reuse を選ぶ場合、node_id は候補に含まれる id をそのまま正確にコピーしてください。\n"
    "- reuse では、node_id は必須です。\n"
    "- shallow/deep では、node_id は null にしてください。\n"
    "- 部分的な一致だけなら reuse ではなく shallow または deep を選んでください。\n"
    "- 確信がない場合は deep を選んでください。不正確な近道より深い調査を優先します。\n"
    "- reason には判断理由を短い1文で必ず書いてください。空文字は禁止です。"
)

SHALLOW_ANSWER_PROMPT = (
    "与えられたノード抜粋のみを根拠として、質問に簡潔に回答してください。\n"
    "- 質問と同じ言語で回答してください。\n"
    "- 抜粋に存在しない事実を追加・推論しないでください。\n"
    "- 回答はMarkdown形式で書いてください。\n"
    "- 根拠に使ったノードのidを、本文中でバッククォート付きで引用してください（例：`node:...`）。\n"
    "- 抜粋だけでは回答できない場合は、その旨を明確に述べてください。\n\n"

    "shallow 回答に適したケース：\n"
    "- 質問が単一の事実を尋ねており、抜粋内に直接答えがある場合。"
    "例：期限、日付、担当者、金額、数値、名称、定義、単一条件、単一の記載事実。\n\n"

    "注意が必要なケース：\n"
    "- ユーザーが「何を変更」「どこを変更」「どうすれば」「なぜ」「どの手順」"
    "「どの条件」「何が必要」「何を確認」「どう対応」「どれを選ぶ」"
    "のように、行動・判断・解釈・変更・原因・手順を尋ねている場合、"
    "抜粋内の一文だけを一般化して答えてはいけません。\n"
    "- その場合、抜粋内に存在する範囲で、対象、必要な行動、条件、根拠、例外、注意点を明確にしてください。\n"
    "- 抜粋に十分な情報がない場合は、"
    "『抜粋だけでは具体的な回答に必要な情報が不足しています』と明確に述べ、"
    "分かっている事実だけを根拠付きで説明してください。\n"
    "- 抽象的な一言だけで終えてはいけません。"
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
    "1. まず主質問を検索し、その後、質問内の重要語、固有名詞、文書名、制度名、対象名、"
    "条件、役割、日付、数値、手順名、判断基準らしき語を検索してください。"
    "グラフの異なる部分を浮かび上がらせるために、いくつか検索を行ってください。\n"
    "2. すべての候補から、異なるサブトピックをカバーする最良の重複しない開始ノードを選んでください"
    "（近い重複を避け、サブエージェントが異なるサブグラフを探索できるようにしてください）。\n"
    "3. それらの開始ノードを指定して explore(node_ids) を1回呼び出してください。"
    "チームは並列に探索し、各サブエージェントの調査結果と読んだノード本文を返します。\n"
    "4. サブエージェントの報告を読んでください。明確な不足が残っている場合は、"
    "再度検索して explore をもう1回実行してもかまいません。そうでなければ回答をまとめてください。\n"
    "5. サブエージェントが報告した内容のみに基づいた十分な回答を、"
    "証拠として使用したノードIDを引用しながら finish で提出してください。\n\n"

    "深く調べるべき質問の方針：\n"
    "- ユーザーが行動、変更、対応、判断、原因、理由、手順、条件、例外、影響、比較、"
    "複数文書の整合性を尋ねている場合、概念的な答えだけで finish してはいけません。\n"
    "- その場合は、可能な限り次を特定してください："
    "対象、該当する文書・節・規則・項目、必要な行動、満たすべき条件、判断基準、"
    "変更前後の状態、例外、注意点、根拠。\n"
    "- 質問が単一の事実だけを尋ねている場合は、必要以上に広げず、直接回答してください。"
    "例：期限、日付、担当者、金額、数値、名称、定義、単一条件。\n"
    "- ただし、単一の事実に見えても、候補同士が矛盾する場合、条件付きの場合、"
    "文脈により答えが変わる場合は、追加検索や追加 explore を検討してください。\n"
    "- サブエージェントの報告に具体的な対象、条件、根拠が不足している場合は、"
    "追加検索や追加 explore を検討してください。\n"
    "- それでも具体的な情報が見つからない場合は、見つかった事実と不足している情報を明確に分けて回答してください。"
    "根拠がない文書名、項目名、条件、値、手順を推測してはいけません。\n\n"

    "ルール：\n"
    "- あなたはノード本文を直接読むことはできません。サブエージェントの報告に依存してください。\n"
    "- 'node:' プレフィックスを含め、ノードIDは表示されたとおり正確にコピーしてください。\n"
    "- 少なくとも1回 explore を実行する前に finish してはいけません。\n"
    "- 幅広さを優先してください。同じ内容に関する複数のIDではなく、"
    "異なる開始点を explore に渡してください。\n"
    "- 回答はユーザーの質問に直接答えてください。関連情報の要約だけで終えてはいけません。"
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
    "2. リンクをたどり、自分の領域内で2〜5個のノードを読んで、実際の証拠を集めてください。"
    "ただし、質問に答えるための対象・条件・根拠・例外・手順が不足している場合は、"
    "必要に応じて追加検索やリンク追跡を行ってください。\n"
    "3. 自分の担当範囲に留まってください。タスクに列挙された兄弟開始ノードは"
    "他のサブエージェントが担当します。それらを再探索せず、自分のサブグラフに集中して、"
    "チーム全体でより広い範囲をカバーできるようにしてください。\n"
    "4. 実際に読んだノード本文のみに基づいて報告してください。\n"
    "5. 'node:' プレフィックスを含め、ノードIDは正確にコピーしてください。\n"
    "6. 完了したら、この領域が質問について何を示しているかを焦点を絞って要約し、"
    "使用したノードIDを引用して finish を呼び出してください。\n\n"

    "調査時の観点：\n"
    "- 質問が単一の事実を尋ねている場合は、その事実と根拠を正確に報告してください。"
    "例：期限、日付、担当者、金額、数値、名称、定義、単一条件。\n"
    "- 質問が行動、変更、対応、判断、原因、理由、手順、条件、例外、影響、比較を尋ねている場合は、"
    "抽象的な概念だけで報告してはいけません。\n"
    "- その場合は、対象、該当する文書・節・規則・項目、必要な行動、満たすべき条件、"
    "判断基準、変更前後の状態、例外、注意点、根拠を探してください。\n"
    "- 読んだ本文にそれらが存在する場合は、具体名、条件、値、記述をそのまま報告してください。\n"
    "- 存在しない場合は、『この領域では具体的な情報は見つからなかった』と明確に報告し、"
    "見つかった関連事実だけを根拠付きで説明してください。\n"
    "- 根拠のない文書名、項目名、条件、値、手順を推測してはいけません。"
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

# pure formatters 
def node_ref(node: Node) -> dict[str, str]:
    return {"id": node.id, "title": node.title or node.entity or node.id}


def dedupe(ids: list[str]) -> list[str]:
    seen: list[str] = []
    for node_id in ids:
        if node_id not in seen:
            seen.append(node_id)
    return seen


def clean_node_ref(value: str) -> str:
    """Strip the decoration an LLM tends to add around a node id (bullets,
    backticks, 'id:' prefix, a copied table row, a trailing '(Title)')."""
    text = str(value or "").strip()
    text = re.sub(r"^\s*[-*]\s*", "", text).strip()
    text = text.strip("`'\" \t\r\n")
    text = re.sub(r"^id\s*:\s*", "", text, flags=re.IGNORECASE).strip()
    if "|" in text:
        text = text.split("|", 1)[0].strip()
    text = re.sub(r"\s+\([^)]*\)\s*$", "", text).strip()
    return text.strip("`'\" \t\r\n,;")


def format_node_full(node: Node | None, requested_id: str, cleaned_id: str) -> str:
    if not node:
        return f"node not found\nrequested_id: {requested_id}\ncleaned_id: {cleaned_id}"
    note = ""
    if requested_id.strip() != cleaned_id:
        note = f"requested_id: {requested_id}\ncleaned_id: {cleaned_id}\n"
    return f"{note}id: {node.id}\ntitle: {node.title}\nsummary: {node.summary}\nbody:\n{node.body}"


# mermaid validate / repair (a real subsystem; not inlined) 
def validate_mermaid(code: str, settings: Settings) -> tuple[bool, str]:
    """Render the code to SVG with mmdc; True if it parses + renders."""
    mmdc = shutil.which(settings.mermaid_cli_bin)
    if mmdc is None:
        return False, f"mermaid CLI '{settings.mermaid_cli_bin}' not found in PATH"
    config = Path(settings.mermaid_puppeteer_config).expanduser()
    cmd_prefix = [mmdc] + (["-p", str(config)] if config.exists() else [])
    with tempfile.TemporaryDirectory() as tmp:
        in_file = Path(tmp) / "d.mmd"
        out_file = Path(tmp) / "d.svg"
        in_file.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [*cmd_prefix, "-i", str(in_file), "-o", str(out_file)],
                capture_output=True,
                timeout=settings.mermaid_render_timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"mmdc timed out after {settings.mermaid_render_timeout}s"
        except Exception as exc:  # noqa: BLE001 - mmdc failed to start
            return False, f"mmdc failed to start: {exc}"
        if proc.returncode == 0 and out_file.exists() and out_file.stat().st_size > 0:
            return True, ""
        return False, proc.stderr.decode("utf-8", errors="replace").strip()


def repair_answer_mermaid(
    answer: str, llm: LlmClient, settings: Settings, emit: Callable[[dict], None]
) -> str:
    """Validate + repair every mermaid block in `answer`, emitting progress events."""
    blocks = list(_MERMAID_BLOCK_RE.finditer(answer))
    if not blocks:
        return answer
    emit({"type": "diagram_pending"})

    new_answer = answer
    fixed_codes: list[str] = []
    all_ok = True
    for match in blocks:
        code = match.group("code").strip()
        ok, error = validate_mermaid(code, settings)
        attempt = 0
        while not ok and attempt < settings.mermaid_repair_attempts:
            attempt += 1
            user = (
                "The following Mermaid diagram does not render. Fix the syntax, preserving "
                "all nodes, edges, directions, and labels. Use simple ASCII node IDs and "
                "quoted labels for any spaces/punctuation/long text.\n\n"
                f"Render error:\n{error}\n\n```mermaid\n{code}\n```"
            )
            try:
                response = llm.complete(MERMAID_FIX_SYSTEM, user)
            except Exception:  # noqa: BLE001 - repair is best-effort
                break
            fix = _MERMAID_BLOCK_RE.search(response)
            repaired = (
                fix.group("code").strip()
                if fix
                else response.strip().strip("`").strip()
            )
            if not repaired:
                break
            code = repaired
            ok, error = validate_mermaid(code, settings)
        all_ok = all_ok and ok
        fixed_codes.append(code)
        new_answer = new_answer.replace(match.group(0), f"```mermaid\n{code}\n```", 1)

    emit(
        {
            "type": "diagram_ready" if all_ok else "diagram_failed",
            "answer": new_answer,
            **({"mermaid": fixed_codes} if all_ok else {}),
        }
    )
    return new_answer

# TODO: Make this inline
def item_vec_weight(settings: Settings, field: str) -> float:
    return {
        "title": settings.weight_title_vec,
        "claim": settings.weight_claim_vec,
        "small_chunk": settings.weight_small_chunk_vec,
        "summary": settings.weight_summary_vec,
        "big_chunk": settings.weight_big_chunk_vec,
    }.get(field, settings.weight_small_chunk_vec)

# For ranking purposes
def normalize_scores(values: list[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [1.0 for _ in values]
    return [(v - low) / (high - low) for v in values]

# Greedy Maximal Marginal Relevance (MMR) ordering.
# At each step, selects the next item that best balances:
# - relevance score (rel)
# - diversity from already selected items (penalized by token overlap)
# Produces an ordering that avoids redundancy while keeping highly relevant texts early.
# TODO: Make this inline
def mmr_order(texts: list[str], rel: list[float], lam: float) -> list[int]:
    """Greedy MMR ordering: lam*rel - (1-lam)*max_sim_to_selected (token overlap)."""
    remaining = set(range(len(texts)))
    order: list[int] = []
    while remaining:
        best_idx, best_score = None, float("-inf")
        for i in remaining:
            sim = max((token_jaccard(texts[i], texts[j]) for j in order), default=0.0)
            score = lam * rel[i] - (1.0 - lam) * sim
            if score > best_score:
                best_score, best_idx = score, i
        order.append(best_idx)
        remaining.discard(best_idx)
    return order


def node_snippet(node: Node) -> str:
    return (node.summary or node.title or "").strip()

# Aggregates evidence hits per field and keeps only the best (lowest) rank for each field.
# Returns a rank-ordered list showing which fields matched the node most strongly (earlier rank = stronger match).
def evidence_why(node_hits: list[EvidenceHit]) -> list[dict[str, Any]]:
    """Best (lowest) rank per field that matched this node, rank-ascending."""
    best: dict[str, int] = {}
    for hit in node_hits:
        if hit.field not in best or hit.rank < best[hit.field]:
            best[hit.field] = hit.rank
    return [
        {"field": field, "rank": rank}
        for field, rank in sorted(best.items(), key=lambda kv: kv[1])
    ]

# Formats a candidate node result into a compact, llm-readable summary string
# for inspection/debugging. Includes node metadata, top match reasons ("why"),
# a few evidence snippets, and a suggested next action for downstream exploration.
def format_lead_candidate(result: dict[str, Any]) -> str:
    node = result["node"]
    why = result.get("why", [])
    why_str = ", ".join(f"{w['field']}#{w['rank']}" for w in why[:4]) or "n/a"
    lines = (
        f"- node_id: `{node.id}`\n"
        f"  title: {node.title}\n"
        f"  summary: {node.summary}\n"
        f"  why_matched: {why_str}"
    )
    for ev in result.get("evidence", [])[:3]:
        snippet = " ".join((ev.get("text") or "").split())
        if len(snippet) > 240:
            snippet = snippet[:240] + "…"
        if snippet:
            lines += f"\n  evidence[{ev['field']}]: {snippet}"
    lines += (
        "\n  next_action: pass this id to explore(node_ids=[...]) if promising "
        "and distinct"
    )
    return lines

