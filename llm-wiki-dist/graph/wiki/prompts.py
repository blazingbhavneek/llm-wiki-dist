"""Versioned prompt builders.

Resume must never combine decisions produced by an incompatible prompt, so
every builder is tagged with :data:`PROMPT_VERSION` and the run state records
it.  All builders are pure functions of their inputs: the same inputs always
render the same prompt, which is what makes a replay test meaningful.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from .config import (
    PROMPT_VERSION,
    REWRITE_PROMPT_VERSION,
    SEED_PLAN_VERSION,
)
from .images import SanitizedSource

COMMON_RULES = (
    "あなたは決定論的な文書Wiki化パイプラインの一作業者である。\n"
    "- 原文の意味を変えず、事実を捏造してはならない。\n"
    "- 判断できない場合は推測せず、注記またはレビュー対象として報告する。\n"
    "- 要求された構造化結果だけを返す。外側の文章は無視される。"
)


@dataclass(frozen=True)
class Prompt:
    kind: str
    system: str
    body: str
    version: str = PROMPT_VERSION

    def messages(self) -> list[BaseMessage]:
        return [SystemMessage(content=self.system), HumanMessage(content=self.body)]

    def render(self) -> str:
        """Flatten system and user text for artifact logging."""

        return f"{self.system}\n\n{self.body}"

    def fingerprint(self) -> str:
        from .storage import sha256_text

        return sha256_text(f"{self.version}\n{self.system}\n{self.body}")


def _schema_hint(model: Any) -> str:
    return json.dumps(model.model_json_schema(), ensure_ascii=False)


def _language_rule(config_language: str) -> str:
    return f"タイトル、要約、説明文は必ず{config_language}で書くこと。"


# --------------------------------------------------------------------------
# Stage A
# --------------------------------------------------------------------------


def window_inventory_prompt(
    *,
    window_start: int,
    window_end: int,
    block: SanitizedSource,
    output_language: str,
    last_error: str | None = None,
) -> Prompt:
    from .wire import WindowInventory

    correction = ""
    if last_error:
        correction = (
            "\n前回の回答は検証に失敗した。修正して有効な結果だけを返すこと。\n"
            f"検証エラー:\n{last_error}\n"
        )

    body = (
        "番号付きソースに何が書かれているかを観察し、範囲付きの目録を作る。"
        "これはページ分割ではなく、後段の計画者へ渡す証拠である。\n\n"
        f"観察ウィンドウ: 原文 {window_start}-{window_end}行（両端を含む）。\n"
        f"{_language_rule(output_language)}\n\n"
        "必須条件:\n"
        f"- 各source_start/source_endは必ず {window_start}-{window_end} の内側にする。\n"
        "- 観察範囲は重複・包含してよい。節全体と、その中の個別APIの両方を記録してよい。\n"
        "- 見えている範囲を無理に隙間なく分割しない。空行や装飾だけの範囲は不要である。\n"
        "- 見出し、説明、手順、制約、警告、表、画像、コード、API、関数、コマンド、"
        "エラーコード、メッセージ、設定項目などを具体的に記録する。\n"
        "- 同じ形式で列挙される具体的エンティティは1件ずつ記録し、kindと"
        "enumeration_familyを統一する。repeated_format=trueにする。\n"
        "- 普通の短い箇条書きや説明上の概念列挙は、個別エンティティとして水増ししない。\n"
        "- 対象がウィンドウの前から始まっていればcontinues_before、後へ続けばcontinues_afterをtrueにする。\n"
        "- 境界付近で途切れて見える内容を、完結した内容だと決めつけない。\n"
        "- 画像や表がどの説明・エンティティに属するかをsummaryまたはparentへ書く。\n"
        f"{correction}\n"
        f"番号付きソース:\n{block.text}\n"
    )

    return Prompt(
        kind="window_inventory",
        system=(
            "重複を許す範囲目録を作り、構造化結果だけを返す。ページ境界は決めない。\n"
            + COMMON_RULES
            + "\n\nJSON形式:\n"
            + _schema_hint(WindowInventory)
        ),
        body=body,
    )


# --------------------------------------------------------------------------
# Stage B
# --------------------------------------------------------------------------


def regional_plan_prompt(
    *,
    region_number: int,
    source_start: int,
    source_end: int,
    window_reports: str,
    previous_region: str,
    output_language: str,
    last_error: str | None = None,
) -> Prompt:
    from .wire import RegionalPlan

    correction = f"\n検証エラー:\n{last_error}\n" if last_error else ""
    return Prompt(
        kind="regional_plan",
        version=SEED_PLAN_VERSION,
        system=(
            "最大10個の重複ウィンドウ目録を、一つの地域的なWiki候補地図へ整理する。\n"
            + COMMON_RULES
            + "\n\nJSON形式:\n"
            + _schema_hint(RegionalPlan)
        ),
        body=(
            f"地域 {region_number}: 原文 {source_start}-{source_end}行。\n"
            f"{_language_rule(output_language)}\n"
            "重複ウィンドウによる同じ観察を統合し、ページ候補を返す。ここでは候補範囲の重複や"
            "前後地域への継続を許す。最終的な無重複分割は後段が行う。\n"
            f"各候補範囲は今回見えている {source_start}-{source_end}行の内側だけで示し、"
            "地域外へ続く場合はcontinues_before/continues_afterを使う。\n"
            "API関数、コマンド、メッセージ型、設定項目、実質的なエラー項目など、同じ書式で"
            "繰り返される具体的エンティティは個別に把握する。ただし20行未満の候補は作らず、"
            "短い隣接エンティティは同じ種類・目的のまとまりとしてグループ化する。"
            "列挙の導入・共通規則も最も適切なまとまりへ含める。\n"
            "ただし、普通の短いリスト、数段落だけの説明、抽象概念を機械的に細分化しない。"
            "行数を揃えるための切断や、エンティティ途中での切断を提案しない。\n"
            f"{correction}\n\n"
            f"前地域の末尾文脈:\n{previous_region or '（なし）'}\n\n"
            f"今回のウィンドウ目録:\n{window_reports}"
        ),
    )


def semantic_plan_prompt(
    *,
    source_line_count: int,
    regional_reports: str,
    output_language: str,
    last_error: str | None = None,
) -> Prompt:
    from .wire import SemanticPlan

    correction = f"\n前回の問題:\n{last_error}\n" if last_error else ""
    return Prompt(
        kind="semantic_plan",
        version=SEED_PLAN_VERSION,
        system=(
            "地域地図から文書全体のWiki分割方針を考える意味計画者である。"
            "厳密なJSON範囲表の作成は次の専用作業者が行うため、ここでは意味判断に集中する。\n"
            + COMMON_RULES
            + "\n\nJSON形式:\n"
            + _schema_hint(SemanticPlan)
        ),
        body=(
            f"原文は1-{source_line_count}行である。計画文は{output_language}で書くこと。\n"
            "自然なページ境界、各ページの題名とおおよその行範囲、章構造、境界を選ぶ理由を示す。\n"
            "最重要規則:\n"
            "- 原文の各行は最終的に必ず1個だけのシードに属する。隣のシードへ行を漏らしたり、"
            "同じ行を二重所有させたり、どこにも属させたりしない。\n"
            "- 見出し、導入、注記、表、画像、例を、それが説明する本文・エンティティから切り離さない。\n"
            "- ページの長さを揃えるために関数や項目の途中で切らない。\n"
            "- 空行、見出しだけ、導入文だけ、前後の続きだけを独立ページにしない。見出しは原則として"
            "その直後にある本文へ含める。全てのページを20行以上にする。短いエンティティは、"
            "意味的に近い隣接エンティティまたは共通説明と一緒に一つのページにする。\n"
            "- 同一書式で列挙されるAPI関数、コマンド、メッセージ型、設定項目、実質的な"
            "エラー項目は、20行以上あるものは独立Wikiページにする。20行未満のものは途中で切らず、"
            "同じ種類・目的の隣接項目と意味のある単位へまとめる。\n"
            "- 普通の短い箇条書き、説明の一部、単なる抽象概念は独立ページにしない。\n"
            "- 地域境界を越えて続くエンティティを一つに戻し、重複観察を一件として扱う。\n"
            f"{correction}\n\n地域地図:\n{regional_reports}"
        ),
    )


def seed_plan_compile_prompt(
    *,
    source_line_count: int,
    semantic_plan: str,
    regional_reports: str,
    output_language: str,
    last_error: str | None = None,
    previous_plan: str = "",
) -> Prompt:
    from .wire import SeedPlan

    correction = ""
    if last_error:
        correction = (
            "\n前回の構造化結果は無効だった。以下の検証結果を読み、全ページを再提出すること。\n"
            f"検証エラー: {last_error}\n"
            f"前回の結果:\n{previous_plan or '（取得できず）'}\n"
        )
    return Prompt(
        kind="seed_plan_compile",
        version=SEED_PLAN_VERSION,
        system=(
            "意味計画を、単純で厳密な連続シード範囲表へ変換する専用作業者である。"
            "新しい意味方針を考え直さず、構造と境界の整合性に集中する。\n"
            + COMMON_RULES
            + "\n\nJSON形式:\n"
            + _schema_hint(SeedPlan)
        ),
        body=(
            f"原文は1-{source_line_count}行である。タイトル等は{output_language}で書くこと。\n"
            "pagesは原文順に並べ、各ページは一つの連続したsource_start/source_endだけを持つ。\n"
            "絶対条件:\n"
            "- 先頭ページはsource_start=1。\n"
            "- 次ページのsource_startは直前ページのsource_end+1。\n"
            f"- 最終ページはsource_end={source_line_count}。\n"
            "- したがって全行をちょうど1回だけ所有し、漏れ・重複・飛び越しを一切作らない。\n"
            "- fenced code、Markdown表、<image-unit>、一つの具体的エンティティの途中を境界にしない。\n"
            "- 意味計画で独立指定された列挙エンティティを勝手に分断しない。\n"
            "- 空行、見出しだけ、導入文だけ、前後ページの断片だけを1ページにしない。見出しは"
            "通常、その直後の本文と同じページに入れる。\n"
            "- 原文全体が20行未満の場合を除き、全ページを必ず20行以上にする。短い項目は"
            "直前と直後の両方について題名、要約、章、種類を比較し、意味的により近い側とまとめる。"
            "先頭なら次、末尾なら前の候補だけを検討する。単に20行へ届かせるため、別エンティティの一部や"
            "空行だけを移動してはならない。\n"
            "- ページを空にせず、題名と短いsummaryを付ける。\n"
            f"{correction}\n\n意味計画:\n{semantic_plan}\n\n地域地図:\n{regional_reports}"
        ),
    )


def reference_research_prompt(
    *,
    target_number: int,
    target_title: str,
    target_ranges: str,
    target_source: str,
    reference_number: int,
    reference_title: str,
    reference_ranges: str,
    reference_source: str,
    output_language: str,
) -> Prompt:
    """Compare one complete reference seed with one complete target seed."""

    from .wire import ReferenceResearchResult

    return Prompt(
        kind="reference_research",
        version=REWRITE_PROMPT_VERSION,
        system=(
            "あなたは技術Wikiの資料調査者である。対象原文と参照原文はプロンプト内に"
            "全文が与えられているため、ファイル操作は不要である。記事や計画は書かず、"
            "参照原文から対象記事へ本当に追加すべき事実だけを構造化JSONで返す。\n\n"
            "JSON形式:\n" + _schema_hint(ReferenceResearchResult)
        ),
        body=(
            f"対象: {target_number:03d} {target_title}（原文 {target_ranges}行）\n"
            f"参照: {reference_number:03d} {reference_title}（原文 {reference_ranges}行）\n"
            f"説明は{output_language}で書くこと。\n\n"
            "判定規則:\n"
            "- 対象原文に既にある事実は追加候補にしない。\n"
            "- 対象ページを単独で理解、利用、実装、運用、障害対応するために有用な"
            "前提、用語、関係、使用条件、制約、注意だけを選ぶ。\n"
            "- 単なる関数一覧、章番号、同じ説明の言い換え、ナビゲーション用リンクは選ばない。\n"
            "- 各事実のsource_start/source_endは、必ず参照原文に表示された正確な行番号にする。\n"
            "- target_lineには、その事実を本文へ入れるべき対象原文の行番号"
            "（対象原文に表示された番号のうち、最も関係の深い行）を書く。\n"
            "- descriptionには追加する事実、reasonには必要な理由、insertion_pointには"
            "対象記事のどこへ入れるかを書く。\n"
            "- 有用な事実がなければuseful_factsを空にし、"
            "no_useful_information_reasonへ具体的な理由を書く。捏造して水増ししない。\n\n"
            f"--- 対象ページの行番号付き原文（全文） ---\n{target_source}\n\n"
            f"--- 参照ページの行番号付き原文（全文） ---\n{reference_source}"
        ),
    )


def page_judge_prompt(
    *,
    page_title: str,
    owner_ranges: str,
    numbered_original: str,
    candidate: str,
    output_language: str,
) -> Prompt:
    """Check both important factual coverage and standalone Wiki usefulness."""

    from .wire import PageJudgeResult

    return Prompt(
        kind="page_judge",
        system=(
            "あなたは技術Wikiの情報欠落と、独立したWiki記事としての完成度を判定する査読者である。"
            "文章を書き直してはならない。"
            "構造化JSONだけを返す。\n"
            "重要な欠落とは、利用者の理解・実装・運用・障害対応に影響する事実、条件、制約、"
            "手順、API挙動、引数、戻り値、警告、例外、エラーの意味である。"
            "章番号、節番号、目次、改訂履歴、装飾、重複文、言い回しなどの些細な差は欠落に含めない。"
            "要約や再構成で意味が保持されていれば欠落ではない。捏造された欠落を報告しない。\n"
            "候補は原文の一部（節）だけを書き直したものである。節の範囲外の情報、"
            "より詳しい説明、一般的な解説、構成の改善を要求してはならない。"
            "「他ページから追加した事実」が候補に含まれていなければ、それは欠落として報告する。\n\n"
            "JSON形式:\n" + _schema_hint(PageJudgeResult)
        ),
        body=(
            f"ページ: {page_title}\n"
            f"所有する原文範囲: {owner_ranges}行\n"
            f"判定文と欠落説明は{output_language}で書くこと。\n"
            "coverage_scoreは重要情報の保持率として0〜100で採点する。"
            "missing_important_informationには本当に重要な欠落だけを入れ、"
            "原文の正確な開始行と終了行を付ける。\n\n"
            f"--- 行番号付き原文 ---\n{numbered_original}\n\n"
            f"--- Wiki候補 ---\n{candidate}"
        ),
    )


# --------------------------------------------------------------------------
# Section writing (plain text output)
# --------------------------------------------------------------------------


def section_write_prompt(
    *,
    page_title: str,
    page_summary: str,
    index: int,
    count: int,
    source_start: int,
    source_end: int,
    numbered_section: str,
    facts_text: str,
    image_context: str,
    output_language: str,
    feedback: Sequence[str] = (),
) -> Prompt:
    """Rewrite one section losslessly; everything needed is in this prompt."""

    feedback_block = ""
    if feedback:
        feedback_block = (
            "# 前回の出力の不足（必ず全て直す）\n- "
            + "\n- ".join(item.strip() for item in feedback if item.strip())
            + "\n\n"
        )
    return Prompt(
        kind="section_write",
        version=REWRITE_PROMPT_VERSION,
        system=(
            "あなたは日本語技術Wikiの執筆者である。原文の一部（節）を、"
            "単独で読んで理解できるWikiの節に書き直す。\n"
            "- 出力はMarkdown本文だけ。前置き、説明、JSON、出力全体をコードフェンスで囲むことは禁止。\n"
            "- 事実を捏造しない。原文と追加事実にない引数、動作、例、一般論を書かない。\n"
            "- 原文の技術情報を一切落とさない。コードブロック、表、識別子、定数、数値、単位、"
            "エラーコード、警告文は一字も変えずにそのまま写す。"
        ),
        body=(
            "# 対象\n"
            f"- ページ: {page_title}\n"
            f"- ページ全体の要約: {page_summary or '要約なし'}\n"
            f"- この節: {index}/{count}（原文 {source_start}-{source_end}行）\n"
            f"- 本文は{output_language}で書く。\n\n"
            "# 書き方\n"
            "- 見出しは`##`以下を使う。`# `（H1）は書かない。\n"
            "- 原文の見出しは残してよいが、内容が分かる名前に変えてよい。\n"
            "- 段落や箇条書きに整理し、何のための機能か、いつ使うか、何に注意するかが"
            "原文から分かる範囲で伝わるようにする。文の意味、条件、順序に関わる情報は変えない。\n"
            "- 先頭の行番号は出典を示すためのもので、本文には書かない。\n"
            "- リンク（`[...](...)`）は書かない。「関連ページ」などの一覧も作らない。\n"
            "- 章番号、頁番号、目次など技術的な意味のない体裁だけは省いてよい。\n"
            "- 画像トークンは一字も変えず、元と同じ話題の直後に1回だけ置く。\n"
            f"{image_context}\n\n"
            "# 他ページから追加する事実\n"
            f"{facts_text}\n"
            "各事実は本文の該当箇所へ自然に組み込み、その段落の直後に"
            "`（参照元: 原文 S-E行）`（SとEは各事実の出典行）と書く。"
            f"原文 {source_start}-{source_end}行の情報にはこのマーカーを付けない。\n\n"
            f"{feedback_block}"
            "--- 行番号付き原文（この節） ---\n"
            f"{numbered_section}"
        ),
    )


def intro_prompt(
    *,
    page_title: str,
    page_summary: str,
    body: str,
    output_language: str,
) -> Prompt:
    """One additive lead paragraph written from the finished body only."""

    return Prompt(
        kind="intro",
        version=REWRITE_PROMPT_VERSION,
        system=(
            "あなたは日本語技術Wikiの編集者である。Markdown本文だけを出力する。"
            "前置き、見出し、リンク、箇条書き、コードフェンスは書かない。"
        ),
        body=(
            "次のWiki記事の冒頭に置く導入文を書く。2〜6文で、何のための機能・情報か、"
            "いつ使うか、この記事を読むと何が分かるかを説明する。"
            f"本文にない事実、識別子、数値は書かない。{output_language}で書く。\n\n"
            f"# タイトル\n{page_title}\n\n"
            f"# 要約\n{page_summary or '要約なし'}\n\n"
            f"# 本文\n{body}"
        ),
    )
