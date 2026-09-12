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
    LINK_PROMPT_VERSION,
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
        """Flatten system and user text for a CLI-agent turn."""

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


def artifact_instruction(artifact_path: str, schema: Any) -> str:
    """JSON artifactの場所と形式を指定する。標準出力は状態として扱わない。"""

    return (
        f"回答を作業ディレクトリの`{artifact_path}`ファイルへ単一のJSONオブジェクトとして書き込む。"
        "受理する出力はこのファイルだけである。説明文で囲まず、他のファイルを作成しない。\n"
        "必須JSON形式:\n" + _schema_hint(schema)
    )


def render_agent_prompt(prompt: Prompt, artifact_path: str) -> str:
    """Resolve the artifact placeholder for a CLI agent turn."""

    return prompt.render().replace("{artifact}", artifact_path)


def reference_selection_prompt(
    *,
    page_number: int,
    page_title: str,
    owner_ranges: str,
    numbered_original: str,
    page_summaries: str,
    output_language: str,
) -> Prompt:
    """Select a tiny set of references before reading their full text."""

    from .wire import ReferenceSelection

    return Prompt(
        kind="reference_selection",
        version=REWRITE_PROMPT_VERSION,
        system=(
            "あなたは技術Wikiの参照候補選定者である。記事は書かない。"
            "対象ページを単独で理解しやすくする事実を持つ可能性があるページだけを選ぶ。"
            "構造化JSONだけを返す。\n\nJSON形式:\n"
            + _schema_hint(ReferenceSelection)
        ),
        body=(
            f"対象ページ: {page_number:03d} {page_title}\n"
            f"対象の原文範囲: {owner_ranges}行\n"
            "必要な候補をすべて選ぶ。件数上限はない。対象ページ自身は選ばない。\n"
            "単に同じライブラリに属するだけのページは選ばない。前提、用語、相互作用、"
            "使用条件、制約、注意、または対象機能への具体的な言及がありそうなページを選ぶ。"
            "有用な候補がなければ空配列にする。\n"
            f"説明は{output_language}で考えること。\n\n"
            f"--- 対象ページの行番号付き原文 ---\n{numbered_original}\n\n"
            f"--- 候補ページ一覧 ---\n{page_summaries}"
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
            "- descriptionには追加する事実、reasonには必要な理由、insertion_pointには"
            "対象記事のどこへ入れるかを書く。\n"
            "- 有用な事実がなければuseful_factsを空にし、"
            "no_useful_information_reasonへ具体的な理由を書く。捏造して水増ししない。\n\n"
            f"--- 対象ページの行番号付き原文（全文） ---\n{target_source}\n\n"
            f"--- 参照ページの行番号付き原文（全文） ---\n{reference_source}"
        ),
    )


def wiki_page_plan_prompt(
    *,
    page_number: int,
    page_title: str,
    working_page_path: str,
    plan_path: str,
    page_index_path: str,
    references_path: str,
    owner_ranges: str,
    image_context: str,
    output_language: str,
    reference_research_path: str = "",
    reference_research: str = "",
    missing_information: Sequence[str] = (),
    last_error: str | None = None,
) -> Prompt:
    """Ask a small file-capable model to research and plan one Wiki page."""

    feedback = ""
    if missing_information:
        feedback += (
            "\n# 前版で不足した内容\n- "
            + "\n- ".join(missing_information)
            + "\nこれらを今回の計画で必ず解決する。\n"
        )
    if last_error:
        feedback += f"\n# 前回の実行エラー\n{last_error}\n"

    return Prompt(
        kind="wiki_page_plan",
        version=REWRITE_PROMPT_VERSION,
        system=(
            "あなたは日本語技術Wikiの構成設計者である。\n"
            "この工程で記事本文は書かない。原文の章立てを写すのではなく、読者が"
            "概念を理解し、目的に応じて機能を選び、正しく利用できる記事を設計する。"
            "次の執筆者が判断をやり直さなくてよい具体的なMarkdown計画を`wiki-plan.md`に書く。\n"
            "`wiki-plan.md`以外のファイルは変更しない。"
        ),
        body=(
            "# 計画対象\n"
            f"- ページ番号: {page_number:03d}\n"
            f"- タイトル: {page_title}\n"
            f"- 計画の書き込み先: `{plan_path}`\n\n"
            "# 行番号の意味（重要）\n"
            f"- `{owner_ranges}`は全原文における出典行番号である。\n"
            "- これは`page.md`内の行番号ではない。\n"
            "- `page.md`はその原文範囲を切り出したシードで、ローカル行番号は1から始まる。"
            "`page.md`が出典行番号より短くても正常であり、矛盾ではない。\n"
            f"- `page.md`の{owner_ranges}行目を探したり、この値をreadツールのoffsetに使ったりしない。\n\n"
            f"{feedback}"
            "# 必ずこの順番で作業する\n"
            f"1. `{working_page_path}`を1行目から最後まで全て読む。\n"
            f"2. `{reference_research_path}`を最後まで読む。これはPythonが参照ページの"
            "全文を比較して作成した調査結果である。\n"
            "3. 下にも同じ調査結果を掲載している。useful factを一件ずつ理解し、"
            "記事のどこで使うか決める。\n"
            f"4. 調査後に`{plan_path}`のみを編集する。\n\n"
            "# `wiki-plan.md`に必ず書く内容\n"
            "1. `# 読者と利用目的`: 誰が、どんな疑問や作業のために読む記事か。\n"
            "2. `# 読後に理解できること`: 個別項目だけでなく、目的、全体像、"
            "項目間の関係、選び方、利用の流れを具体的に書く。\n"
            "3. `# 必ず残す事実`: `page.md`の全機能、コマンド、API、引数、オプション、"
            "出力、手順、制約、注意、エラー、パス、環境変数。\n"
            "4. `# 参照から追加する内容`: 追加する事実、必要な理由、挿入場所、"
            "読み取り用シードの絶対パス、全原文の出典行範囲。"
            "下の調査結果にあるuseful factを勝手に「なし」にしない。"
            "調査結果自体が追加なしの場合だけ「なし」と書く。\n"
            "5. `# 原文からの再構成方針`: 原文順を変える箇所、まとめる重複、"
            "比較表にする項目、先に説明する前提、後で示す詳細を具体的に書く。\n"
            "6. `# 記事構成`: 完成記事の見出しを学習・利用しやすい順に並べる。"
            "各見出しには、その節で読者が得る理解、使う事実、説明する項目間の関係、"
            "使用する原文範囲または参照範囲を書く。\n"
            "7. `# 画像の配置`: 各画像トークンをどの見出しのどの説明の直後に置くか。\n"
            "8. `# 執筆後の確認`: 欠落、捏造、参照元行マーカー、画像、"
            "単独で理解できるかを確認するチェックリスト。\n\n"
            "原文の見出し一覧を言い換えて並べただけの計画は不合格である。"
            "冒頭で目的と使いどころを説明し、関連するAPIや概念を比較・接続し、"
            "読者が判断や作業を進められる順序にする。"
            "ただし、根拠にないチュートリアル手順や説明は作らない。\n"
            "ページタイトルと関係が薄く見える項目も、自分の判断で削除する計画にしない。"
            "必要なら独立した小見出しにする。\n"
            "根拠のない一般論、例、引数、動作を追加する計画にしない。\n\n"
            "# 画像\n"
            "下のトークンをそれぞれ必ず1回残す計画にする。\n"
            f"{image_context}\n\n"
            "# 読み取るファイル\n"
            f"- 編集対象のシード: `{working_page_path}`\n"
            f"- 全ページ一覧: `{page_index_path}`\n"
            f"- 他ページの要約と参照先: `{references_path}`\n"
            f"- 参照調査結果: `{reference_research_path}`\n"
            f"- 出力する計画: `{plan_path}`\n\n"
            f"# Pythonが本文を比較して確定した参照調査結果\n{reference_research}"
        ),
    )


def simple_page_edit_prompt(
    *,
    page_number: int,
    page_title: str,
    final_page_path: str,
    working_page_path: str,
    plan_path: str,
    original_path: str,
    page_index_path: str,
    references_path: str,
    owner_ranges: str,
    image_context: str,
    output_language: str,
    reference_research_path: str = "",
    reference_research: str = "",
    missing_information: Sequence[str] = (),
    last_error: str | None = None,
) -> Prompt:
    """Tell the writer to turn the staged seed into a Wiki page in place."""

    correction = ""
    if last_error:
        correction = (
            "## 前回の実行エラー\n"
            "`page.md`をもう一度編集し、以下を修正すること:\n- "
            + "\n- ".join(item.strip() for item in last_error.split("; ") if item.strip())
            + "\n\n"
        )

    repair = ""
    if missing_information:
        repair = (
            "## 判定で見つかった不足\n"
            "既存の有用な内容を削らず、以下を追加または再構成すること:\n- "
            + "\n- ".join(missing_information)
            + "\n\n"
        )

    return Prompt(
        kind="simple_page_edit",
        version=REWRITE_PROMPT_VERSION,
        system=(
            "あなたは日本語Wikiの執筆者である。\n"
            "調査済みの`wiki-plan.md`に従って`page.md`を直接編集し、"
            "それ単独で学習と検索に使えるWiki記事にする。\n"
            "事実を捏造せず、判断できない内容は推測しない。\n"
            "最終成果は編集済みの`page.md`である。JSONや別の成果ファイルは作らない。"
            "`page.md`以外は変更しない。"
        ),
        body=(
            "# 対象\n"
            f"- ページ番号: {page_number:03d}\n"
            f"- 最終ファイル名: `{final_page_path}`\n"
            f"- タイトル: {page_title}\n"
            f"- 編集対象: `{working_page_path}`\n\n"
            "# 行番号の意味（重要）\n"
            f"- `{owner_ranges}`は、全原文における出典行番号である。\n"
            "- これは`page.md`内の行番号ではない。\n"
            "- `page.md`は原文から切り出したシードで、ローカル行番号は1から始まる。"
            "`page.md`が出典行番号より短くても正常であり、矛盾ではない。\n"
            f"- `page.md`の{owner_ranges}行目を探したり、この値をreadツールのoffsetに使ったりしない。\n"
            "- 全原文の行番号が必要なときだけ、"
            "`page-index.md`または`references.md`に書かれた`all-source-numbered.md`の絶対パスを開く。\n\n"
            f"{repair}"
            f"{correction}"
            "# 必ずこの順番で作業する\n"
            f"1. `{working_page_path}`を1行目から最後まで全て読む。\n"
            f"2. `{plan_path}`を1行目から最後まで全て読む。\n"
            f"3. `{reference_research_path}`を最後まで読む。参照ページの必要な抜粋は"
            "Pythonがこのファイルへ既に入れているので、別ファイルを探す必要はない。\n"
            "4. 計画と参照調査結果を照合し、useful factを漏らさず、"
            "何をどの見出しに書くかを理解する。\n"
            f"5. `{working_page_path}`を直接編集する。新しい短文を別に作らない。\n"
            "6. 編集後の`page.md`を再度1行目から最後まで読み、下の確認項目を検査する。\n\n"
            "# Wiki記事の作り方\n"
            f"- タイトルと本文は{output_language}で書く。\n"
            "- `page.md`にはMarkdown本文だけを残し、H1見出しを置く。\n"
            "- 原文の整形や言い換えだけで終わらない。"
            "前後の章を読んでいない人にも、何のための機能か、いつ使うか、どう動くか、"
            "何に注意するかが分かる記事にする。\n"
            "- 原文の見出しや順序をそのまま複製しない。目的と使いどころから始め、"
            "前提、選択基準、操作の流れ、個別仕様、制約・失敗時の確認というように、"
            "読者の理解順へ再構成する。該当しない節を無理に作る必要はない。\n"
            "- 複数のAPI、コマンド、値がある場合は、最初に相互関係と使い分けを説明し、"
            "必要なら比較表や処理の流れを作ってから個別仕様を示す。\n"
            "- 参照調査のuseful factは付録や末尾一覧へ隔離せず、"
            "理解に必要な本文の位置へ自然に統合する。\n"
            "- 主題に応じて、概要、前提、用語、動作、使い方、制約、注意、例を読みやすい順序に再構成する。\n"
            "- APIやコマンドは、根拠がある範囲で目的、形式、引数、動作、出力、"
            "状態変化、失敗条件、制約を検索しやすく整理する。\n"
            "- 構成、順序、見出し、表現、表、箇条書き、注意書きは自由に変更してよい。\n"
            "- 根拠のない一般論、引数、戻り値、例、動作を作らない。\n\n"
            "# 内容を失わないためのルール\n"
            "- 編集前の`page.md`にある各機能、コマンド、API、オプション、手順、"
            "制約、注意、エラー、ファイルパス、環境変数を残す。\n"
            "- ページタイトルと関係が薄く見える項目も、自分の判断で削除しない。"
            "必要なら独立した小見出しとして整理する。\n"
            "- 削除してよいのは、重複した見出し、頁番号、章番号など、"
            "技術的な意味を持たない体裁上のノイズだけである。\n\n"
            "# 参照シードの使い方\n"
            "- Pythonが選別した参照調査結果を読まずに執筆しない。\n"
            "- このページを単独で理解するために必要な前提、用語定義、関係、"
            "使用方法、制約、注意事項だけを要約して本文へ統合する。\n"
            "- 参照ページ全体をコピーしない。関係しない情報を追加しない。\n"
            "- 他ページから追加した段落、表、または箇条書きの直後に、"
            "`（参照元: 原文 123-145行）`の形式で出典範囲を書く。\n"
            f"- 自分の出典範囲`{owner_ranges}`の情報には参照元マーカーを付けない。\n\n"
            "# リンクと末尾一覧\n"
            "- この工程では新しい内部リンクを追加しない。リンクは後段の専用工程で追加する。\n"
            "- ナビゲーション用の「関連ページ」「関連機能」「関連項目」「参考」"
            "「参照先」などの節や一覧を追加しない。\n"
            "- 必要な関係性は、ナビゲーション一覧ではなく通常の本文として説明する。\n\n"
            "# 画像\n"
            "下の画像トークンを一字も変えず、それぞれ必ず1回残す。"
            "元と同じ話題のすぐ近くに置く。末尾の画像集へ移動したり、削除したりしない。\n"
            f"{image_context}\n\n"
            "# ファイル\n"
            f"- 編集する: `{working_page_path}`\n"
            f"- 必ず読む調査・構成計画: `{plan_path}`\n"
            f"- 編集しない保存用コピー: `{original_path}`\n"
            f"- 不明点があるときの全ページ一覧: `{page_index_path}`\n"
            f"- 不明点があるときの参照一覧: `{references_path}`\n\n"
            f"# Pythonが本文を比較して確定した参照調査結果\n{reference_research}\n\n"
            "# 終了前の確認\n"
            "- `page.md`は原文の整形だけではなく、単独で理解できるWikiになっている。\n"
            "- 編集前の重要な技術情報を失っていない。\n"
            "- 参照から追加した情報には出典行マーカーがある。\n"
            "- 指定された画像トークンがそれぞれ1回ある。\n"
            "- `page.md`以外を変更していない。\n"
            "確認後、作業を終了する。"
        ),
    )


def wiki_plan_judge_prompt(
    *,
    page_title: str,
    numbered_original: str,
    reference_research: str,
    wiki_plan: str,
    output_language: str,
) -> Prompt:
    """Reject plans that merely preserve the manual's original outline."""

    from .wire import WikiPlanJudgeResult

    return Prompt(
        kind="wiki_plan_judge",
        version=REWRITE_PROMPT_VERSION,
        system=(
            "あなたは技術Wikiの構成計画を査読する。記事本文は書かず、構造化JSONだけを返す。"
            "原文の情報保持と、独立した学習・参照記事としての再構成を両方評価する。\n\n"
            "JSON形式:\n" + _schema_hint(WikiPlanJudgeResult)
        ),
        body=(
            f"対象ページ: {page_title}\n"
            f"issuesは{output_language}で具体的に書くこと。\n\n"
            "acceptable=trueにできる条件:\n"
            "- 読者、利用目的、読後に理解できることが具体的である。\n"
            "- 原文の重要事実を保持する計画がある。\n"
            "- useful factがある場合、その内容、挿入場所、出典範囲が計画に入っている。\n"
            "- 原文の見出しを同じ順番で言い換えただけではなく、目的、前提、関係、"
            "選び方、利用の流れ、個別仕様、制約を読者の理解順に組み立てている。\n"
            "- 各見出しで何を説明し、どの事実を使うかが執筆者に明確である。\n"
            "満たさない場合はacceptable=falseにして、直すべき点だけをissuesへ書く。"
            "根拠のない内容追加や過剰な長文化は要求しない。\n\n"
            f"--- 所有原文 ---\n{numbered_original}\n\n"
            f"--- 参照調査結果 ---\n{reference_research}\n\n"
            f"--- Wiki計画 ---\n{wiki_plan}"
        ),
    )


def link_page_prompt(
    *,
    page_title: str,
    working_page_path: str,
    original_path: str,
    page_index_path: str,
    output_language: str,
    last_error: str | None = None,
) -> Prompt:
    """Ask the selected CLI agent for a final, additive-only link pass."""

    correction = ""
    if last_error:
        correction = f"\n前回の編集は無効だった。次の問題だけを修正すること:\n{last_error}\n"
    return Prompt(
        kind="link_page",
        version=LINK_PROMPT_VERSION,
        system=(
            "あなたは日本語Wikiの内部リンク編集者である。既に完成した記事へ、"
            "必要最小限のMarkdownリンクだけを追加する。\n"
            "記事の情報、語句、順序、見出し、画像、参照元表記を削除・変更・要約してはならない。"
            "新しい技術説明も追加してはならない。`page.md`以外を変更しないこと。"
        ),
        body=(
            f"対象記事: {page_title}\n"
            f"本文は{output_language}のまま維持すること。\n"
            f"最初に `{working_page_path}` と `{page_index_path}` を最後まで読むこと。"
            "page-indexにある要約から関連候補を選び、候補の絶対パスを実際に開いて確認すること。\n"
            "page-indexの「追加情報を所有するWiki」は、書き直し時に別の原文範囲から情報を"
            "取り込んだ履歴であり、本文中に該当語句があれば優先的なリンク候補にすること。"
            "本文中で、別Wikiページの主題である関数、コマンド、設定、エラー、機能、前提事項が"
            "既に言及されている箇所だけに、`[既存の語句](NNN-file.md)`形式のリンクを加える。"
            "リンクは必ず既存本文中の言及へ直接付ける。末尾や別節へ「関連機能」「関連項目」"
            "「参考」「参照先」などの一覧を追加してはならない。"
            "無関係なページ、同じページ自身、存在しないファイルへリンクしないこと。"
            "適切なリンクがなければpage.mdを変更せず終了してよい。\n"
            "これは純粋な追加編集である。元の文字を一文字も削除・置換・並べ替えないこと。"
            "YAMLフロントマターを追加しないこと。\n\n"
            "作業ファイル（すべて作業ディレクトリ内の絶対パス）:\n"
            f"- 編集対象: `{working_page_path}`\n"
            f"- 編集前コピー（変更禁止）: `{original_path}`\n"
            f"- 全完成Wikiの要約と絶対パス: `{page_index_path}`\n"
            f"{correction}"
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
            "単なる原文の整形・言い換えで、目的、前提、動作、使い方、制約、項目間の関係が"
            "原文から説明できるのに説明されず、前後の章なしでは理解しにくい場合は未完成である。"
            "その場合はmissing_important_informationへ必要な改善を具体的に1件以上入れ、"
            "coverage_scoreを85以下にする。短くても内容が完結したAPIやエラー項目に、"
            "根拠のない説明や不要な長文化を要求してはならない。\n\n"
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


def reference_enrichment_judge_prompt(
    *,
    page_title: str,
    reference_research: str,
    candidate: str,
    output_language: str,
) -> Prompt:
    """Check that every accepted cross-page fact reached the Wiki article."""

    from .wire import PageJudgeResult

    return Prompt(
        kind="reference_enrichment_judge",
        version=REWRITE_PROMPT_VERSION,
        system=(
            "あなたは技術Wikiの参照情報反映を判定する査読者である。文章を書き直さず、"
            "構造化JSONだけを返す。調査結果でuseful factとされた情報が、意味を保って"
            "Wiki候補へ統合されているかを確認する。\n\nJSON形式:\n"
            + _schema_hint(PageJudgeResult)
        ),
        body=(
            f"ページ: {page_title}\n"
            f"判定文は{output_language}で書くこと。\n"
            "全useful factが本文に反映され、各追加箇所に対応する"
            "`（参照元: 原文 ...行）`があればcoverage_score=100とする。"
            "欠落、意味の変化、または出典行マーカー不足があれば、その事実の正確な"
            "source_start/source_endをmissing_important_informationへ入れる。"
            "調査結果にuseful factがなければ新しい情報を要求しない。\n\n"
            f"--- 参照調査結果 ---\n{reference_research}\n\n"
            f"--- Wiki候補 ---\n{candidate}"
        ),
    )
