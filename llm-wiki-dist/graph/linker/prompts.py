from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from graph.common.prompts import BRIDGE_PROBE_PROMPT, CLAIM_PROMPT, EDGE_PROMPT, KEYWORD_PROMPT, SUMMARY_PROMPT
from graph.wiki.prompts import COMMON_RULES, Prompt, _schema_hint, _language_rule

from .wire import ChunkMeta, NeoEdgeSuggestions

CHUNK_META_VERSION = "wiki-chunk-meta-1"
EDGE_VERSION_LEGACY = "wiki-link-edge-legacy-1"
EDGE_VERSION_NEO = "wiki-link-edge-neo-1"

NEO_EDGE_PROMPT = (
    "あなたはWiki横断リンクの判定者である。対象チャンクと、エンティティの振る舞いグラフを"
    "1〜3ホップたどって見つかった候補チャンクが渡される。各候補には、どのエンティティを"
    "経由して到達したか（via）と、候補側の entities / behaviours が付いている。\n"
    "ルール:\n"
    "- 与えられた候補IDのみを使う。\n"
    "- リンクを提案するのは、読者が対象チャンクを理解・実行する上で候補が具体的に役立つ"
    "場合だけ（前提、結果、制約、代替、矛盾、同じエンティティの別の振る舞い）。\n"
    "- 単に同じ語が出てくるだけの候補は提案しない。候補が少なくても無理につなげない。\n"
    "- label は列挙値から選ぶ。summary は、どのエンティティのどの振る舞いでつながるかを"
    "1文で書く。"
)


def chunk_meta_prompt(*, page_title: str, heading: str, document: str, text: str, output_language: str) -> Prompt:
    body = (
        f"# 対象\n- 文書: {document}\n- ページ: {page_title}\n"
        f"- 節: {heading or '(導入)'}\n- 出力は{output_language}で書く。\n\n"
        f"# summary\n{SUMMARY_PROMPT}\n\n# keywords\n{KEYWORD_PROMPT}\n\n"
        f"# entity / claims\n{CLAIM_PROMPT}\n\n# bridge_probe\n{BRIDGE_PROBE_PROMPT}\n\n"
        "# entities\nこの節に登場する固有のエンティティ（人物、組織、役割、製品、API、関数、"
        "パラメータ、エラーコード、文書名、規則名、手順名、概念、場所）を最大20件。\n"
        "- name は本文に書かれている表記をそのまま写す。\n"
        "- role は、この節が定義・宣言・仕様説明・初出解説している場合は defines、単に使用・"
        "言及している場合は uses。\n\n# behaviours\n"
        "この節で「誰が／何が、何をしているか」を最大20件。subject と object は entities の"
        "name と一致させる。object が無い場合は空文字。action は短い動詞句。\n\n"
        f"--- 本文 ---\n{text}"
    )
    return Prompt(
        kind="chunk_meta",
        version=CHUNK_META_VERSION,
        system=f"あなたはWiki横断リンク用のチャンク記述者である。与えられたWikiページの一節を読み、検索とリンク判定に必要な情報だけを構造化して返す。\n{COMMON_RULES}\n{_language_rule(output_language)}\nJSON形式:\n{_schema_hint(ChunkMeta)}",
        body=body,
    )


def legacy_edge_messages(target: dict[str, Any], candidates: list[dict[str, Any]], *, output_language: str = "") -> list[Any]:
    payload = {"new_node": target, "candidates": candidates}
    system = EDGE_PROMPT + (f"\n- summary は{output_language}で書く。" if output_language else "")
    return [SystemMessage(content=system), HumanMessage(content=json.dumps(payload, ensure_ascii=False))]


def neo_edge_messages(target: dict[str, Any], candidates: list[dict[str, Any]], *, output_language: str = "") -> list[Any]:
    payload = {"target": target, "candidates": candidates}
    system = NEO_EDGE_PROMPT + (f"\n- summary は{output_language}で書く。" if output_language else "")
    return [SystemMessage(content=system), HumanMessage(content=json.dumps(payload, ensure_ascii=False))]


__all__ = [
    "CHUNK_META_VERSION", "EDGE_VERSION_LEGACY", "EDGE_VERSION_NEO", "NEO_EDGE_PROMPT",
    "chunk_meta_prompt", "legacy_edge_messages", "neo_edge_messages",
]
