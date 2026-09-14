"""End-to-end and unit tests for the pre-ingestion cross-document linker.

The deterministic smoke corpus is docs/LINKER.md section 20:

* A — symptom page (early timeout; request-expiry/retry vocabulary),
* B — hidden mechanism page (fast clock; oscillator/calibration vocabulary),
* C — lexical distractor (says "timeout" but is UI session expiration),
* D — hop endpoint already linked from B (validation of calibration drift).

Fake services deliberately omit B from retrieval and rank C first; only the
exhaustive map scout can bring B into research, which is the minimum proof
that this feature is not query-time RAG.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from graph.project import Project
from graph.wiki import linker
from graph.wiki.storage import sha256_text, write_json_atomic
from graph.wiki.wire import (
    BridgeProbeResult,
    DeepLinkProposal,
    DeepLinkResearchResult,
    LinkJudgeResult,
    MapLinkCandidate,
    MapLinkScanResult,
)

SETTINGS = SimpleNamespace(
    wiki_output_language="Japanese (日本語)",
    wiki_linker_map_concurrency=1,
    wiki_linker_research_concurrency=1,
    wiki_linker_enabled=True,
)

PAGE_A = "この運用では要求失効が設定値より早く到来し、再試行が連鎖する症状を扱う。"
PAGE_B = "制御クロックの発振器は数パーセント速く進むため校正係数による補正が必要である。"
PAGE_C = "セッションのタイムアウトは画面の待受け時間であり業務機構とは無関係な仕様である。"
PAGE_D = "実測された偏差は校正係数の妥当性を検証するため、観測値と照合して確認する。"

EVIDENCE = {
    "A": "要求失効が設定値より早く到来し",
    "B": "発振器は数パーセント速く進む",
    "D": "実測された偏差は校正係数の妥当性を検証",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_document(project: Project, raw_rel: str, pages: list[dict]) -> Path:
    wiki_dir = project.wiki_dir(raw_rel)
    planning = wiki_dir / "_planning"
    planning.mkdir(parents=True, exist_ok=True)
    raw = project.raw / raw_rel
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("# doc\n", encoding="utf-8")
    manifest_files, coverage_files = [], []
    for page in pages:
        (wiki_dir / page["filename"]).write_text(page["body"], encoding="utf-8")
        manifest_files.append(
            {
                "filename": page["filename"],
                "title": page["title"],
                "source_ranges": [list(r) for r in page["ranges"]],
                "reference_ranges": [],
            }
        )
        coverage_files.append(
            {
                "filename": page["filename"].split("-", 1)[1],
                "title": page["title"],
                "summary": page.get("summary", ""),
                "header": "1",
                "source_start": page["ranges"][0][0],
                "source_end": page["ranges"][0][1],
            }
        )
    write_json_atomic(
        planning / "manifest.json",
        {
            "source_sha256": hashlib.sha256(b"# doc\n").hexdigest(),
            "files": manifest_files,
        },
    )
    write_json_atomic(planning / "metadata.json", {"original_file_name": raw_rel})
    write_json_atomic(planning / "coverage.json", {"files": coverage_files})
    return wiki_dir


def smoke_pages() -> tuple[dict[str, dict], dict[str, dict]]:
    old = {
        "B": {
            "filename": "001-b-clock.md",
            "title": "クロック校正",
            "summary": "発振器の誤差と校正係数による補正を説明する。",
            "ranges": [(1, 40)],
            "body": "# クロック校正\n\n" + PAGE_B
            + "\n\n補正値は測定結果から導出する。\n\n[検証手順](002-d-validation.md)\n",
        },
        "D": {
            "filename": "002-d-validation.md",
            "title": "校正の検証",
            "summary": "実測偏差による校正の検証手順。",
            "ranges": [(41, 80)],
            "body": "# 校正の検証\n\n" + PAGE_D + "\n",
        },
        "C": {
            "filename": "003-c-session.md",
            "title": "セッション失効",
            "summary": "UIセッションの失効仕様。",
            "ranges": [(81, 120)],
            "body": "# セッション失効\n\n" + PAGE_C + "\n",
        },
    }
    new = {
        "A": {
            "filename": "001-a-symptom.md",
            "title": "早期満了の症状",
            "summary": "設定より早い要求失効と再試行の症状。",
            "ranges": [(1, 50)],
            "body": "# 早期満了の症状\n\n" + PAGE_A + "\n",
        },
    }
    return old, new


class FakeEmbedder:
    """embed_query fails: retrieval must degrade while maps keep working."""

    model_name = "fake-embed"
    dim = 0

    def embed_documents(self, texts):
        return [[float(len(text) % 7), 1.0, 0.5] for text in texts]

    def embed_query(self, text):
        raise RuntimeError("query endpoint down")


class FakeModel:
    """Schema-dispatching fake with the smoke-corpus decisions."""

    name = "fake"
    provider = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.prompts: dict[str, list[str]] = {}
        self.accept = True
        self.nominate_for: dict[str, list[str]] = {}
        self.page_tags: dict[str, str] = {}  # page_id -> A/B/C/D
        self.fail_scout = False
        self.fail_bridge = False
        self.invalid_research = False

    async def structured(self, schema, messages, *, max_output_tokens=None):
        await asyncio.sleep(0)
        body = messages[-1].content
        self.calls.append(schema.__name__)
        self.prompts.setdefault(schema.__name__, []).append(body)
        if schema is MapLinkScanResult:
            return self._scan(body)
        if schema is BridgeProbeResult:
            if self.fail_bridge:
                raise RuntimeError("bridge endpoint down")
            return BridgeProbeResult(
                probes=[
                    "この満了を早くする独立したクロックや発振器の挙動は何かあるか？",
                    "設定値と実測の差を説明する共有資源や寿命の制約は存在するか？",
                    "セッション、または満了に関する共有資源や寿命の制約はありますか？",
                ]
            )
        if schema is DeepLinkResearchResult:
            return self._research(body)
        if schema is LinkJudgeResult:
            return LinkJudgeResult(
                is_grounded_in_both_pages=True,
                is_specific_relationship=True,
                is_more_than_shared_topic=True,
                is_useful_to_target_reader=True,
                is_useful_to_endpoint_reader=True,
                bridge_text_adds_no_unsupported_claim=True,
                recommended=True,
            )
        raise AssertionError(f"unexpected schema {schema}")

    def _scan(self, body: str) -> MapLinkScanResult:
        if self.fail_scout:
            raise RuntimeError("scout endpoint down")
        segments = body.split("PAGE ID: ")
        target_card = segments[1]
        target_id = target_card.split("\n", 1)[0].strip()
        nominated = [
            pid for pid in self.nominate_for.get(target_id, []) if f"{pid}\n" in body
        ]
        if not nominated:
            return MapLinkScanResult(no_candidate_reason="具体的な橋が見つからない")
        candidates = []
        for page_id in nominated[:3]:
            card = next(s for s in segments if s.startswith(page_id))
            entry_ids = [
                token.strip("[]")
                for token in _split_bracket_ids(card)
            ][:1]
            target_entries = [
                token.strip("[]") for token in _split_bracket_ids(target_card)
            ][:1]
            candidates.append(
                MapLinkCandidate(
                    candidate_page_id=page_id,
                    relation_type="mechanism",
                    hypothesis="語彙は異なるが実クロックの誤差が早期満了の説明になり得る",
                    reader_value="症状ページ読者が校正の手順へ辿り原因対策を実行できる",
                    target_map_entry_ids=target_entries,
                    candidate_map_entry_ids=entry_ids,
                    bridge_questions=["実クロックのずれが早期満了を説明しないか？"],
                    priority=80,
                )
            )
        return MapLinkScanResult(candidates=candidates)

    def _research(self, body: str) -> DeepLinkResearchResult:
        notes = body.split("--- 発見経路の要約(手がかり) ---", 1)[1]
        path = _split_page_ids(notes.strip().split("\n", 1)[0])
        views = body.split("--- 精読候補ページ(全文) ---", 1)[1].split(
            "--- 発見経路", 1
        )[0]
        # Each research request now contains exactly one endpoint body; hop IDs
        # remain only in the discovery notes.
        endpoint_ids = _split_page_ids(views)[:1]
        target_id = path[0]
        entries: dict[str, list[str]] = {}
        for segment in body.split("PAGE ID: ")[1:]:
            page_id = segment.split("\n", 1)[0].strip()
            found = [t.strip("[]") for t in _split_bracket_ids(segment)]
            if found:
                entries.setdefault(page_id, found)
        proposals, rejected = [], []
        if self.invalid_research and endpoint_ids:
            return DeepLinkResearchResult(
                proposals=[
                    self._proposal(target_id, endpoint_ids[0], "B", path, entries)
                ],
                rejected_page_ids=[],
                notes="故意に無効な証拠",
            ).model_copy(
                update={"proposals": [
                    self._proposal(target_id, endpoint_ids[0], "B", path, entries)
                    .model_copy(update={"target_evidence": "これは本文に存在しない引用です"})
                ]}
            )
        for endpoint_id in endpoint_ids:
            tag = self.page_tags.get(endpoint_id)
            if not self.accept or tag not in EVIDENCE:
                rejected.append(endpoint_id)
                continue
            if tag == "D":  # accepted only with its own independent evidence
                if PAGE_D not in views:
                    rejected.append(endpoint_id)
                    continue
            proposals.append(self._proposal(target_id, endpoint_id, tag, path, entries))
        return DeepLinkResearchResult(
            proposals=proposals,
            rejected_page_ids=rejected,
            notes="共有トピックの候補は不採用",
        )

    def _proposal(
        self,
        target_id: str,
        endpoint_id: str,
        tag: str,
        path: list[str],
        entries: dict[str, list[str]],
    ) -> DeepLinkProposal:
        own = EVIDENCE["A"]
        peer = EVIDENCE[tag]
        route = path if path[-1] == endpoint_id else [target_id, endpoint_id]
        return DeepLinkProposal(
            endpoint_page_id=endpoint_id,
            relation_type="mechanism" if tag == "B" else "validation_of",
            discovery_path=route[:4],
            target_evidence=own,
            endpoint_evidence=peer,
            target_map_entry_ids=entries.get(target_id, ["lmap-" + "0" * 20]),
            endpoint_map_entry_ids=entries.get(endpoint_id, ["lmap-" + "0" * 20]),
            relationship_explanation=(
                "設定値より早い満了症状は発振器誤差が原因となり得るため、"
                "校正知識は症状解析の前提になる。"
            ),
            why_reader_needs_link="読者は症状ページから補正手順へ進み原因除去を実行できる",
            why_not_shared_topic_only="両ページは語彙が全く異なり機構の橋だけが根拠である",
            target_anchor_id="lead",
            endpoint_anchor_id="lead",
            target_bridge_template="背景の発振器誤差と補正手順は{link}で確認できる。",
            endpoint_bridge_template="この補正を要する早期満了の観測例は{link}に現れる。",
            target_footer_reason="発振器誤差と校正係数の前提を確認するため",
            endpoint_footer_reason="早期満了症状への影響を確認し検証根拠にするため",
            novelty_score=90,
            usefulness_score=88,
            confidence_score=85,
        )


def _split_bracket_ids(card: str) -> list[str]:
    import re

    return re.findall(r"\[lmap-[0-9a-f]{20}\]", card)


def _split_page_ids(text: str) -> list[str]:
    import re

    return re.findall(r"lpage-[0-9a-f]{20}", text)


class LinkerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Project(Path(self._tmp.name)).ensure()
        old, new = smoke_pages()
        self.old_dir = make_document(self.project, "team/old_md.md", list(old.values()))
        self.new_dir = make_document(self.project, "team/new_md.md", list(new.values()))
        self.model = FakeModel()
        self.embedder = FakeEmbedder()

    def ids(self) -> dict[str, str]:
        doc_new = linker.make_document_id("team/new_md.md")
        doc_old = linker.make_document_id("team/old_md.md")
        return {
            "A": linker.make_page_id(doc_new, "team/new.md/001-a-symptom.md"),
            "B": linker.make_page_id(doc_old, "team/old.md/001-b-clock.md"),
            "C": linker.make_page_id(doc_old, "team/old.md/003-c-session.md"),
            "D": linker.make_page_id(doc_old, "team/old.md/002-d-validation.md"),
        }

    def run_linker(self, raw_rel: str, reranker=None):
        return asyncio.run(
            linker.link_generated_document(
                self.project,
                raw_rel,
                model=self.model,
                settings=SETTINGS,
                embedder=self.embedder,
                reranker=reranker,
            )
        )


# ---------------------------------------------------------------------------
# Map / catalog tests
# ---------------------------------------------------------------------------


class MapCatalogTests(LinkerTestCase):
    def test_judge_cache_round_trip(self) -> None:
        catalog = linker.LinkCatalog.open(self.project.linker_database)
        self.addCleanup(catalog.close)
        result = LinkJudgeResult(
            is_grounded_in_both_pages=True,
            is_specific_relationship=True,
            is_more_than_shared_topic=True,
            is_useful_to_target_reader=True,
            is_useful_to_endpoint_reader=True,
            bridge_text_adds_no_unsupported_claim=True,
            recommended=True,
        )

        catalog.judge_put("target", "endpoint", "proposal", result)

        self.assertEqual(
            catalog.judge_get("target", "endpoint", "proposal"), result
        )

    def test_discovery_and_sync_builds_pages_entries_fts(self) -> None:
        documents = linker.discover_documents(self.project)
        self.assertEqual({d.raw_rel for d in documents},
                         {"team/old_md.md", "team/new_md.md"})
        catalog = linker.LinkCatalog.open(self.project.linker_database)
        self.addCleanup(catalog.close)
        catalog.sync_document(
            next(d for d in documents if d.raw_rel == "team/old_md.md")
        )
        rows = catalog.active_pages(
            linker.make_document_id("team/old_md.md")
        )
        self.assertEqual(len(rows), 3)
        entries = catalog.conn.execute("SELECT COUNT(*) FROM map_entries").fetchone()[0]
        self.assertGreaterEqual(entries, 3)
        fts = catalog.conn.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0]
        self.assertEqual(fts, 3)

    def test_second_sync_changes_nothing(self) -> None:
        documents = linker.discover_documents(self.project)
        catalog = linker.LinkCatalog.open(self.project.linker_database)
        self.addCleanup(catalog.close)
        document = next(d for d in documents if d.raw_rel == "team/old_md.md")
        catalog.sync_document(document)
        before = catalog.conn.execute(
            "SELECT page_id, updated_at, map_hash, body_hash FROM pages ORDER BY page_id"
        ).fetchall()
        catalog.sync_document(document)
        after = catalog.conn.execute(
            "SELECT page_id, updated_at, map_hash, body_hash FROM pages ORDER BY page_id"
        ).fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])

    def test_short_embedding_batch_degrades_without_marking_pages_ready(self) -> None:
        class ShortEmbedder:
            model_name = "short"
            dim = 3

            def embed_documents(self, texts):
                return [[1.0, 2.0, 3.0] for _ in texts[:-1]]

        catalog = linker.LinkCatalog.open(self.project.linker_database)
        self.addCleanup(catalog.close)
        documents = linker.discover_documents(self.project)
        for document in documents:
            catalog.sync_document(document)
        error = linker.sync_vectors(
            catalog,
            {document.document_id: document for document in documents},
            ShortEmbedder(),
        )
        self.assertIn("embedding count mismatch", error)
        states = {
            row[0]
            for row in catalog.conn.execute(
                "SELECT vector_state FROM pages WHERE active=1"
            )
        }
        self.assertEqual(states, {"pending"})

    def test_clips_cross_boundary_observations_and_dedupes(self) -> None:
        from graph.wiki.wire import ObservedRange

        pages = [
            linker.WikiPageRecord(
                page_id="p1", document_id="d", wiki_rel_path="t/d/1.md",
                filename="1.md", title="t1", chapter="", summary="",
                owner_ranges=[(1, 10)], reference_ranges=[], map_entries=[],
            ).finalize(),
            linker.WikiPageRecord(
                page_id="p2", document_id="d", wiki_rel_path="t/d/2.md",
                filename="2.md", title="t2", chapter="", summary="",
                owner_ranges=[(11, 20)], reference_ranges=[], map_entries=[],
            ).finalize(),
        ]
        observations = [
            ObservedRange(title="共通", kind="note", summary="x", source_start=5, source_end=15),
            ObservedRange(title="共通", kind="note", summary="x", source_start=5, source_end=15),
            ObservedRange(title="入れ子", kind="note", summary="sub", source_start=6, source_end=9),
        ]
        linker.assign_observations(observations, pages, 20)
        clipped = [e for p in pages for e in p.map_entries if e.title == "共通"]
        self.assertEqual(len(clipped), 2)  # one per intersected page, dupes collapsed
        self.assertTrue(any(e.source_end == 10 for e in clipped))
        self.assertTrue(any(e.source_start == 11 for e in clipped))
        self.assertTrue(any(e.title == "入れ子" for e in pages[0].map_entries))

    def test_ignores_index_and_unlisted_markdown(self) -> None:
        (self.old_dir / "index.md").write_text("# index\n", encoding="utf-8")
        (self.old_dir / "unlisted.md").write_text("# x\n", encoding="utf-8")
        documents = linker.discover_documents(self.project)
        old = next(d for d in documents if d.raw_rel == "team/old_md.md")
        self.assertEqual(len(old.pages), 3)

    def test_rejects_root_escape_and_cross_team(self) -> None:
        planning = self.old_dir / "_planning"
        write_json_atomic(
            planning / "metadata.json",
            {"original_file_name": "other/old_doc_md.md"},
        )
        documents = linker.discover_documents(self.project)
        self.assertNotIn("team/old_md.md", {d.raw_rel for d in documents})
        with self.assertRaises(linker.LinkerError):
            linker.normalize_rel_path("../escape.md")

    def test_strip_managed_links_roundtrip_and_malformed(self) -> None:
        base = "# 題\n\n本文。\n"
        rendered = linker.render_managed_page(
            base,
            [("lead", "llink-aa", "関連{link}。".replace("{link}", "[peer](x.md)"))],
            [("peer", "x.md", "理由")],
            linker.extract_heading_anchors(base),
        )
        self.assertEqual(linker.strip_managed_links(rendered), base)
        with self.assertRaises(linker.LinkerError):
            linker.strip_managed_links("<!-- llm-wiki-link:llink-aa:start -->\n", path="/x")

    def test_extract_local_page_links_skips_fence_image_code(self) -> None:
        text = (
            "[本物](002-x.md)\n画像 ![x](003-y.md)\n`[コード](004-z.md)`\n"
            "```\n[フェンス](005-w.md)\n```\n<div>[html](006-v.md)</div>\n"
        )
        self.assertEqual(linker.extract_local_page_links(text), ["002-x.md"])

    def test_ids_and_relative_links(self) -> None:
        self.assertEqual(
            linker.relative_link("t/d/sub/001-a.md", "t/d/002-b.md"), "../002-b.md"
        )
        self.assertEqual(
            linker.make_pair_id("lpage-b", "lpage-a"), linker.make_pair_id("lpage-a", "lpage-b")
        )

    def test_document_blocks_never_split_a_card(self) -> None:
        big = linker.WikiPageRecord(
            page_id="big", document_id="d", wiki_rel_path="t/d/big.md",
            filename="big.md", title="t", chapter="", summary="s" * 30000,
            owner_ranges=[(1, 5)], reference_ranges=[], map_entries=[],
        ).finalize()
        small = linker.WikiPageRecord(
            page_id="small", document_id="d", wiki_rel_path="t/d/small.md",
            filename="small.md", title="t", chapter="", summary="s",
            owner_ranges=[(6, 9)], reference_ranges=[], map_entries=[],
        ).finalize()
        blocks = linker.make_document_blocks([small, big, small])
        self.assertTrue(all(any(p.page_id == "big" for p in b) and len(b) == 1
                            for b in blocks if any(p.page_id == "big" for p in b)))
        # the oversized card owns its block; others pack around it
        self.assertGreaterEqual(len(blocks), 2)


# ---------------------------------------------------------------------------
# Renderer tests
# ---------------------------------------------------------------------------


class RendererTests(unittest.TestCase):
    BASE = (
        "# 題\n\n導入段落です。\n\n## 第二節\n\n| 表 | 头 |\n|---|---|\n| a | b |\n\n"
        "```\n## フェンス内\n```\n\n最終段落です。\n"
    )

    def test_lead_block_and_footer_roundtrip(self) -> None:
        anchors = linker.extract_heading_anchors(self.BASE)
        rendered = linker.render_managed_page(
            self.BASE,
            [("lead", "llink-01", "導入の補足[P](../d/002.md)")],
            [("Peer", "../d/002.md", "理由です。")],
            anchors,
        )
        self.assertIn("> 関連: 導入の補足[P](../d/002.md)", rendered)
        self.assertIn("## 関連資料", rendered)
        self.assertEqual(linker.strip_managed_links(rendered), self.BASE)
        again = linker.render_managed_page(
            linker.strip_managed_links(rendered),
            [("lead", "llink-01", "導入の補足[P](../d/002.md)")],
            [("Peer", "../d/002.md", "理由です。")],
            anchors,
        )
        self.assertEqual(again, rendered)

    def test_heading_anchor_inserts_after_first_paragraph(self) -> None:
        anchors = linker.extract_heading_anchors(self.BASE)
        h2 = next(a["anchor_id"] for a in anchors if a["heading"] == "第二節")
        # first ordinary content after the heading is a table -> no paragraph:
        # insertion must land before the next heading or EOF, never inside.
        rendered = linker.render_managed_page(
            self.BASE,
            [(h2, "llink-02", "補足[P](p.md)")],
            [],
            anchors,
        )
        self.assertEqual(linker.strip_managed_links(rendered), self.BASE)
        self.assertNotIn("```\n\n<!--", rendered)

    def test_missing_anchor_raises(self) -> None:
        anchors = linker.extract_heading_anchors("# 題\n")
        with self.assertRaises(linker.LinkerError):
            linker.render_managed_page("# 題\n", [("h9", "llink-03", "x")], [], anchors)

    def test_rrf_fuse(self) -> None:
        fused = linker.rrf_fuse([["a", "b"], ["b", "c"]])
        self.assertEqual(fused[0], "b")


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class ValidationTests(unittest.TestCase):
    def test_scan_rejects_unknown_ids(self) -> None:
        result = MapLinkScanResult(
            candidates=[
                MapLinkCandidate(
                    candidate_page_id="lpage-unknown",
                    relation_type="mechanism",
                    hypothesis="十分に長い仮説の説明テキストがここに入る",
                    reader_value="十分に長い読者価値の説明テキストがここに入る",
                    target_map_entry_ids=["lmap-nope"],
                    candidate_map_entry_ids=["lmap-nope"],
                    bridge_questions=["q"],
                    priority=50,
                )
            ]
        )
        errors = linker.validate_map_scan(
            result,
            block_page_ids={"lpage-known"},
            entries_by_page={"lpage-known": {"lmap-x"}},
            target_page_id="lpage-target",
        )
        self.assertTrue(errors)
        self.assertTrue(any("unknown" in e for e in errors))

    def test_scan_filter_keeps_valid_candidates(self) -> None:
        def candidate(page_id: str, entry_id: str) -> MapLinkCandidate:
            return MapLinkCandidate(
                candidate_page_id=page_id,
                relation_type="mechanism",
                hypothesis="十分に長い仮説の説明テキストがここに入る",
                reader_value="十分に長い読者価値の説明テキストがここに入る",
                target_map_entry_ids=["lmap-target"],
                candidate_map_entry_ids=[entry_id],
                bridge_questions=["この関係はなぜ必要ですか？"],
                priority=50,
            )

        filtered, dropped = linker.filter_valid_map_candidates(
            MapLinkScanResult(
                candidates=[
                    candidate("lpage-good", "lmap-good"),
                    candidate("lpage-bad", "lmap-good"),
                ]
            ),
            block_page_ids={"lpage-good", "lpage-bad"},
            entries_by_page={
                "lpage-target": {"lmap-target"},
                "lpage-good": {"lmap-good"},
                "lpage-bad": {"lmap-bad"},
            },
            target_page_id="lpage-target",
        )

        self.assertEqual(dropped, 1)
        self.assertEqual([item.candidate_page_id for item in filtered.candidates], ["lpage-good"])

    def test_bridge_probe_validation_rules(self) -> None:
        errors = linker.validate_bridge_probes(
            BridgeProbeResult(
                probes=[
                    "語句の羅列",  # short + no ?
                    "これは質問ですか？はい。これは質問ですか？はい。",
                    "これは質問ですか？はい。これは質問ですか？はい。",
                    "第三の質問で十分長い長さを持ちますか？はい持ちます。",
                ]
            )
        )
        self.assertTrue(any("not a question" in e for e in errors))
        self.assertTrue(any("duplicate" in e for e in errors))

    def _proposal_kwargs(self, **overrides):
        base = dict(
            endpoint_page_id="lpage-e",
            relation_type="mechanism",
            discovery_path=["lpage-t", "lpage-e"],
            target_evidence="対象側の正確な引用テキストです",
            endpoint_evidence="エンドポイント側の正確な引用です",
            target_map_entry_ids=["lmap-t1"],
            endpoint_map_entry_ids=["lmap-e1"],
            relationship_explanation="機構の橋を説明する十分に長い関係説明のテキストをここに配置する",
            why_reader_needs_link="読者が原因対策へ進めることが本リンクの目的である",
            why_not_shared_topic_only="語彙が全く異なり共有語以外の根拠しかないため",
            target_anchor_id="lead",
            endpoint_anchor_id="lead",
            target_bridge_template="背景は{link}で確認できる。",
            endpoint_bridge_template="影響例は{link}に現れる。",
            target_footer_reason="前提となる機構を確認するために関連ページを参照する",
            endpoint_footer_reason="観測例を確認の上で検証の根拠とするためです",
            novelty_score=90,
            usefulness_score=90,
            confidence_score=90,
        )
        base.update(overrides)
        return base

    def _validate(self, proposal, **overrides):
        import sqlite3

        target = {"page_id": "lpage-t", "document_id": "docA",
                  "wiki_rel_path": "t/a/p.md", "title": "T", "summary": ""}
        endpoint = {"page_id": "lpage-e", "document_id": "docB",
                    "wiki_rel_path": "t/b/p.md", "title": "E", "summary": ""}
        kwargs = dict(
            target=target,
            endpoint=endpoint,
            target_body="本文: 対象側の正確な引用テキストです",
            endpoint_body="本文: エンドポイント側の正確な引用です",
            allowed_endpoint_ids={"lpage-e"},
            seeds={"lpage-e"},
            edge_exists=lambda a, b: True,
            entries_by_page={"lpage-t": {"lmap-t1"}, "lpage-e": {"lmap-e1"}},
            anchors_by_page={"lpage-t": {"lead"}, "lpage-e": {"lead"}},
        )
        kwargs.update(overrides)
        return linker.validate_proposal(proposal, **kwargs)

    def test_valid_proposal_passes(self) -> None:
        self.assertEqual(self._validate(DeepLinkProposal(**self._proposal_kwargs())), [])

    def test_generic_shared_topic_rejected(self) -> None:
        proposal = DeepLinkProposal(
            **self._proposal_kwargs(
                relationship_explanation="両ページは関連があります、というこれ以上の説明になっていない関係説明テキストです。"
            )
        )
        self.assertTrue(any("generic" in e for e in self._validate(proposal)))

    def test_evidence_tolerates_markdown_decoration(self) -> None:
        proposal = DeepLinkProposal(
            **self._proposal_kwargs(
                target_evidence="対象側の正確な引用テキストです",  # body has it raw
            )
        )
        errors = self._validate(
            proposal,
            target_body="本文: `対象側の` **正確な** 引用テキストです\n\n次。",
        )
        self.assertFalse(any("target_evidence" in e for e in errors))
        # but a real content change still fails
        proposal = DeepLinkProposal(
            **self._proposal_kwargs(target_evidence="対象側の正確な引用です")
        )
        self.assertTrue(
            any("target_evidence" in e for e in self._validate(proposal))
        )

    def test_inexact_evidence_rejected_with_verbatim_hint(self) -> None:
        proposal = DeepLinkProposal(**self._proposal_kwargs(target_evidence="対象側のざっくりした要約テキスト"))
        errors = self._validate(proposal)
        self.assertTrue(any("target_evidence" in e for e in errors))
        self.assertTrue(any("対象側の正確な引用テキストです" in e for e in errors))

    def test_same_document_and_cross_team_rejected(self) -> None:
        proposal = DeepLinkProposal(**self._proposal_kwargs())
        errors = self._validate(
            proposal,
            endpoint={"page_id": "lpage-e", "document_id": "docA",
                      "wiki_rel_path": "t/a/q.md", "title": "E", "summary": ""},
        )
        self.assertTrue(any("same document" in e for e in errors))
        errors = self._validate(
            proposal,
            endpoint={"page_id": "lpage-e", "document_id": "docB",
                      "wiki_rel_path": "u/b/p.md", "title": "E", "summary": ""},
        )
        self.assertTrue(any("another team" in e for e in errors))

    def test_bad_path_and_unknown_anchor_rejected(self) -> None:
        proposal = DeepLinkProposal(
            **self._proposal_kwargs(discovery_path=["lpage-t", "lpage-x", "lpage-e"])
        )
        errors = self._validate(proposal, edge_exists=lambda a, b: False)
        self.assertTrue(errors)
        proposal = DeepLinkProposal(**self._proposal_kwargs(endpoint_anchor_id="h7"))
        self.assertTrue(any("anchor" in e for e in self._validate(proposal)))

    def test_third_hop_path_rejected(self) -> None:
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            DeepLinkProposal(
                **self._proposal_kwargs(
                    discovery_path=["lpage-t", "b", "c", "d", "e", "lpage-e"]
                )
            )
        # a legal-length path over unknown hops fails mechanical validation
        proposal = DeepLinkProposal(
            **self._proposal_kwargs(discovery_path=["lpage-t", "lpage-x", "lpage-e"])
        )
        errors = self._validate(proposal, edge_exists=lambda a, b: False,
                                seeds={"lpage-e"})
        self.assertTrue(any("discovery_path" in e for e in errors))

    def test_two_link_placeholders_rejected(self) -> None:
        proposal = DeepLinkProposal(
            **self._proposal_kwargs(target_bridge_template="{link}と{link}の両方を見る。")
        )
        self.assertTrue(any("target_bridge_template" in e for e in self._validate(proposal)))

    def test_unsupported_identifier_rejected(self) -> None:
        proposal = DeepLinkProposal(
            **self._proposal_kwargs(endpoint_bridge_template="エラーERR9ZZの原因は{link}に書く。")
        )
        self.assertTrue(any("identifier" in e for e in self._validate(proposal)))

    def test_low_scores_rejected(self) -> None:
        proposal = DeepLinkProposal(**self._proposal_kwargs(novelty_score=50))
        self.assertTrue(any("novelty_score" in e for e in self._validate(proposal)))


class EvidenceRepairTests(unittest.TestCase):
    def test_near_miss_repaired_far_miss_not(self) -> None:
        body = (
            "# 表\n\n#### `REPCPU`（通知対象計算機種別／略称）\n\n"
            "故障通知を受ける計算機種別、または計算機略称を指定します。\n"
        )
        proposal = DeepLinkProposal(**{
            **ValidationTests()._proposal_kwargs(),
            "target_evidence": "REPCPU 通知対象計算機種別/略称 故障通知を受ける計算機種別、または計算機略称を指定します",
        })
        proposal = proposal.model_copy(update={"endpoint_evidence": proposal.endpoint_evidence})
        # near miss against the heading+sentence region: repaired verbatim
        proposal = proposal.model_copy(
            update={"endpoint_evidence": "故障通知を受ける計算機 種別、または計算機略称を指定します"}
        )
        linker._repair_evidence(proposal, body, body)
        self.assertTrue(linker._evidence_matches(proposal.endpoint_evidence, body))
        # a genuinely different sentence stays broken
        other = DeepLinkProposal(
            **ValidationTests()._proposal_kwargs(endpoint_evidence="まったく内容の違う引用です")
        )
        linker._repair_evidence(other, body, body)
        self.assertFalse(linker._evidence_matches(other.endpoint_evidence, body))

    def test_combined_quote_reduces_to_exact_source_fragment(self) -> None:
        body = (
            "# 計算機情報\n\n#### (b) 計算機種別\n"
            "同じ機能を持つ計算機を総称するために使用されます。\n\n"
            "N重系計算機を構成する各計算機は、同一の種別として定義します。\n"
        )
        proposal = DeepLinkProposal(
            **ValidationTests()._proposal_kwargs(
                target_evidence=(
                    "計算機種別：同じ機能を持つ計算機を総称するために使用されます。"
                    "N重系計算機を構成する各計算機は、同一の種別として定義します"
                )
            )
        )

        linker._repair_evidence(proposal, body, body)

        self.assertTrue(linker._evidence_matches(proposal.target_evidence, body))

    def test_valid_line_with_hallucinated_suffix_reduces_to_exact_line(self) -> None:
        body = (
            "#### `REPCPU`（通知対象計算機種別／略称）\n"
            "故障通知を受ける計算機種別、または計算機略称を指定する。\n"
            '全計算機で通知を受ける場合は `"*"` を指定する。\n'
        )
        proposal = DeepLinkProposal(
            **ValidationTests()._proposal_kwargs(
                endpoint_evidence=(
                    "故障通知を受ける計算機種別、または計算機略称を指定する。"
                    "全計算機で通知を受ける場合は Perkenalkan を指定する。"
                )
            )
        )

        linker._repair_evidence(proposal, body, body)

        self.assertEqual(
            proposal.endpoint_evidence,
            "故障通知を受ける計算機種別、または計算機略称を指定する",
        )


class StructuredCallTests(unittest.TestCase):
    def test_timeout_retries(self) -> None:
        from unittest import mock

        class SlowModel:
            calls = 0

            async def structured(self, schema, messages):
                self.calls += 1
                await asyncio.sleep(1)

        model = SlowModel()
        prompt = SimpleNamespace(messages=lambda: [])
        events = []
        with mock.patch.object(linker, "MODEL_CALL_TIMEOUT_SECONDS", 0.001):
            with self.assertRaises(linker.LinkerIncomplete) as caught:
                asyncio.run(
                    linker._call_structured(
                        model,
                        MapLinkScanResult,
                        lambda _error: prompt,
                        None,
                        attempts=2,
                        artifacts=linker._Artifacts(None, events.append),
                    )
                )

        self.assertEqual(model.calls, 2)
        self.assertIn("TimeoutError", str(caught.exception))
        self.assertEqual(
            [event["step"] for event in events],
            ["request_start", "request_retry", "request_start", "request_retry"],
        )


class SelectionTests(unittest.TestCase):
    def test_quota_and_endpoint_capacity(self) -> None:
        class FakeCatalog:
            def links_for_page(self, page_id, statuses=("active",)):
                return []

        def link(pair, novelty=90):
            return linker.AcceptedLink(
                pair_id=pair, page_a_id="lpage-t", page_b_id="lpage-" + pair,
                relation_type="mechanism", discovery_path=["lpage-t", "lpage-x"],
                evidence={}, a_anchor_id="lead", b_anchor_id="lead",
                a_bridge_template="a{link}", b_bridge_template="b{link}",
                a_footer_reason="r", b_footer_reason="r",
                novelty_score=novelty, usefulness_score=90, confidence_score=90,
                target_hashes={},
            )

        catalog = FakeCatalog()
        selected, skipped = linker.select_final_links(
            catalog, "lpage-t", [link("p1"), link("p2", 80), link("p3", 70), link("p4", 95)]
        )
        self.assertEqual([l.pair_id for l in selected], ["p4", "p1", "p2"])
        self.assertTrue(any("target quota" in s for s in skipped))


# ---------------------------------------------------------------------------
# Smoke corpus: end-to-end linking
# ---------------------------------------------------------------------------


class SmokeCorpusTests(LinkerTestCase):
    def prepare(self) -> dict[str, str]:
        ids = self.ids()
        self.model.page_tags = {v: k for k, v in ids.items()}
        self.model.nominate_for = {ids["A"]: [ids["B"]]}
        return ids

    def test_first_document_catalogs_without_model_calls(self) -> None:
        import shutil

        shutil.rmtree(self.project.wiki / "team" / "new.md")
        marker = self.run_linker("team/old_md.md")
        self.assertEqual(marker["status"], "complete")
        self.assertEqual(marker["links_added"], 0)
        self.assertEqual(self.model.calls, [])
        marker_path = self.old_dir / "_planning" / "linker.json"
        self.assertEqual(json.loads(marker_path.read_text())["status"], "complete")

    def test_full_smoke_bilateral_edit(self) -> None:
        ids = self.prepare()

        class FakeReranker:
            def top_k(self, query, items, k):
                # rank the distractor first; B must still be researched
                c = [t for t in items if t[1] == ids["C"]]
                return ([(p, 1.0) for _t, p in c] + [(p, 0.5) for t, p in items if t not in [x[0] for x in c]])[:k]

        before_a = (self.new_dir / "001-a-symptom.md").read_text(encoding="utf-8")
        marker = self.run_linker("team/new_md.md", reranker=FakeReranker())
        self.assertEqual(marker["status"], "complete", marker)
        self.assertEqual(marker["links_added"], 2)

        page_a = (self.new_dir / "001-a-symptom.md").read_text(encoding="utf-8")
        page_b = (self.old_dir / "001-b-clock.md").read_text(encoding="utf-8")
        page_d = (self.old_dir / "002-d-validation.md").read_text(encoding="utf-8")
        page_c = (self.old_dir / "003-c-session.md").read_text(encoding="utf-8")

        # B reached research despite zero retrieval score; C reached and rejected
        research_prompts = self.model.prompts[DeepLinkResearchResult.__name__]
        self.assertTrue(any(ids["B"] in p for p in research_prompts))
        self.assertTrue(any(ids["C"] in p for p in research_prompts))

        # bilateral, direction-specific prose and footers
        self.assertIn("<!-- llm-wiki-link:", page_a)
        self.assertIn("<!-- llm-wiki-link:", page_b)
        self.assertIn("[クロック校正](../old.md/001-b-clock.md)", page_a)
        self.assertIn("[早期満了の症状](../new.md/001-a-symptom.md)", page_b)
        forward = [line for line in page_a.split("\n") if line.startswith("> 関連:")][0]
        reverse = [line for line in page_b.split("\n") if line.startswith("> 関連:")][0]
        self.assertNotEqual(forward, reverse)
        for page in (page_a, page_b, page_d):
            self.assertIn("## 関連資料", page)
        self.assertNotIn("## 関連資料", page_c)  # rejected distractor untouched

        # stripped base is byte-identical to the pre-run file
        self.assertEqual(linker.strip_managed_links(page_a), before_a)
        # hop link path had three IDs
        self.assertIn(
            ids["D"], [pid for p in self.model.prompts[DeepLinkResearchResult.__name__] for pid in [ids["D"]] if ids["B"] in p]
        )

        # rerun is byte-idempotent with zero model calls (page A now unchanged)
        files = {p: p.read_bytes() for p in [self.new_dir / "001-a-symptom.md",
                                             self.old_dir / "001-b-clock.md"]}
        self.model.calls.clear()
        marker = self.run_linker("team/new_md.md")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(marker["links_added"], 0)
        for path, blob in files.items():
            self.assertEqual(path.read_bytes(), blob)

    def test_discovery_checkpoint_skips_repeated_model_calls(self) -> None:
        ids = self.prepare()
        documents = linker.discover_documents(self.project)
        catalog = linker.LinkCatalog.open(self.project.linker_database)
        self.addCleanup(catalog.close)
        for document in documents:
            catalog.sync_document(document)
        current = next(d for d in documents if d.raw_rel == "team/new_md.md")
        old = [d for d in documents if d.raw_rel == "team/old_md.md"]

        def discover(events):
            return asyncio.run(
                linker.discover_candidates(
                    catalog,
                    self.model,
                    current.pages[0],
                    catalog.page(ids["A"]),
                    old,
                    embedder=None,
                    reranker=None,
                    output_language="Japanese (日本語)",
                    concurrency=1,
                    artifacts=linker._Artifacts(None),
                    progress=events.append,
                    stop_check=None,
                )
            )

        expected = discover([])
        self.model.calls.clear()
        events = []
        actual = discover(events)

        self.assertEqual(actual, expected)
        self.assertEqual(self.model.calls, [])
        self.assertTrue(any(event["step"] == "discovery_resumed" for event in events))

    def test_bridge_probe_failure_degrades_to_required_map_scout(self) -> None:
        self.prepare()
        self.model.fail_bridge = True
        marker = self.run_linker("team/new_md.md")
        self.assertEqual(marker["status"], "complete")
        self.assertIn(
            "llm-wiki-link",
            (self.new_dir / "001-a-symptom.md").read_text(encoding="utf-8"),
        )

    def test_reranker_can_drop_non_map_candidates(self) -> None:
        ids = self.prepare()

        class DropAllReranker:
            def top_k(self, query, items, k):
                return []

        documents = linker.discover_documents(self.project)
        catalog = linker.LinkCatalog.open(self.project.linker_database)
        self.addCleanup(catalog.close)
        for document in documents:
            catalog.sync_document(document)
        current = next(d for d in documents if d.raw_rel == "team/new_md.md")
        old = [d for d in documents if d.raw_rel == "team/old_md.md"]
        catalog.fts_search = lambda query, limit=linker.DIRECT_K: [ids["C"]]
        candidates, _overflow = asyncio.run(
            linker.discover_candidates(
                catalog,
                self.model,
                current.pages[0],
                catalog.page(ids["A"]),
                old,
                embedder=None,
                reranker=DropAllReranker(),
                output_language="Japanese (日本語)",
                concurrency=1,
                artifacts=linker._Artifacts(None),
                progress=None,
                stop_check=None,
            )
        )
        candidate_ids = {candidate.candidate_page_id for candidate in candidates}
        self.assertIn(ids["B"], candidate_ids)  # required map candidate survives
        self.assertNotIn(ids["C"], candidate_ids)

    def test_republish_restores_existing_managed_links_without_model_calls(self) -> None:
        self.prepare()
        self.run_linker("team/new_md.md")
        page_a = self.new_dir / "001-a-symptom.md"
        page_a.write_text(
            linker.strip_managed_links(page_a.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
        self.model.calls.clear()
        marker = self.run_linker("team/new_md.md")
        self.assertEqual(marker["status"], "complete")
        self.assertEqual(self.model.calls, [])
        self.assertIn("llm-wiki-link", page_a.read_text(encoding="utf-8"))

    def test_changed_peer_cleans_stale_backlink(self) -> None:
        self.prepare()
        self.run_linker("team/new_md.md")
        # regenerate B with new prose; the linker now declines every pair
        (self.old_dir / "001-b-clock.md").write_text(
            "# クロック校正\n\n" + PAGE_B + " 改訂: 温度依存の説明を追加した。\n\n[検証手順](002-d-validation.md)\n",
            encoding="utf-8",
        )
        self.model.accept = False
        marker = self.run_linker("team/old_md.md")
        self.assertEqual(marker["status"], "complete")
        self.assertEqual(marker["links_removed"], 1)  # A<->B stale; A<->D kept
        page_a = (self.new_dir / "001-a-symptom.md").read_text(encoding="utf-8")
        self.assertNotIn("001-b-clock.md", page_a.split("## 関連資料")[0])
        self.assertIn("002-d-validation.md", page_a)
        base_a = "# 早期満了の症状\n\n" + PAGE_A + "\n"
        self.assertEqual(
            linker.strip_managed_links(page_a), base_a
        )

    def test_removed_page_cleans_surviving_backlink(self) -> None:
        self.prepare()
        self.run_linker("team/new_md.md")
        manifest_path = self.old_dir / "_planning" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"] = [
            item
            for item in manifest["files"]
            if item["filename"] != "001-b-clock.md"
        ]
        write_json_atomic(manifest_path, manifest)
        (self.old_dir / "001-b-clock.md").unlink()

        marker = self.run_linker("team/old_md.md")
        self.assertEqual(marker["status"], "complete")
        self.assertEqual(marker["links_removed"], 1)
        page_a = (self.new_dir / "001-a-symptom.md").read_text(encoding="utf-8")
        self.assertNotIn("001-b-clock.md", page_a)

    def test_failed_research_candidate_is_skipped(self) -> None:
        self.prepare()
        self.model.invalid_research = True
        events = []
        marker = asyncio.run(
            linker.link_generated_document(
                self.project,
                "team/new_md.md",
                model=self.model,
                settings=SETTINGS,
                embedder=self.embedder,
                on_progress=events.append,
            )
        )
        self.assertEqual(marker["status"], "complete")
        self.assertEqual(marker["links_added"], 0)
        self.assertTrue(any(event["step"] == "research_skipped" for event in events))
        self.assertEqual(
            (self.new_dir / "001-a-symptom.md").read_text(encoding="utf-8"),
            "# 早期満了の症状\n\n" + PAGE_A + "\n",
        )

    def test_completed_page_linking_resumes_after_commit_failure(self) -> None:
        from unittest import mock

        self.prepare()
        with mock.patch.object(
            linker, "commit_relations", side_effect=RuntimeError("commit failed")
        ):
            with self.assertRaises(RuntimeError):
                self.run_linker("team/new_md.md")

        self.model.calls.clear()
        events = []
        marker = asyncio.run(
            linker.link_generated_document(
                self.project,
                "team/new_md.md",
                model=self.model,
                settings=SETTINGS,
                embedder=self.embedder,
                on_progress=events.append,
            )
        )

        self.assertEqual(marker["status"], "complete")
        self.assertEqual(self.model.calls, [])
        self.assertTrue(any(event["step"] == "page_resumed" for event in events))

    def test_scout_failure_is_incomplete_not_empty(self) -> None:
        self.prepare()
        self.model.fail_scout = True
        with self.assertRaises(linker.LinkerIncomplete):
            self.run_linker("team/new_md.md")
        marker = json.loads(
            (self.new_dir / "_planning" / "linker.json").read_text(encoding="utf-8")
        )
        self.assertEqual(marker["status"], "failed")
        catalog = linker.LinkCatalog.open(self.project.linker_database)
        self.addCleanup(catalog.close)
        self.assertEqual(
            catalog.conn.execute("SELECT COUNT(*) FROM map_comparisons").fetchone()[0], 0
        )
        # the target page was never edited
        self.assertEqual(
            (self.new_dir / "001-a-symptom.md").read_text(encoding="utf-8"),
            "# 早期満了の症状\n\n" + PAGE_A + "\n",
        )

    def test_only_declared_runtime_files(self) -> None:
        self.prepare()
        self.run_linker("team/new_md.md")
        metadata = {p.name for p in self.project.metadata.iterdir()}
        self.assertEqual(
            metadata, {"state", "wiki-linker.sqlite", "wiki-linker.lock"}
        )
        run_dirs = list(
            (self.project.state_dir("team/new_md.md") / "work" / "linker").iterdir()
        )
        self.assertEqual(len(run_dirs), 1)
        self.assertTrue((run_dirs[0] / "run.json").exists())

    def test_lock_prevents_overlap(self) -> None:
        handle = linker._acquire_lock(self.project)
        with self.assertRaises(linker.LinkerError):
            linker._acquire_lock(self.project)
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    def test_judge_prompt_has_no_retrieval_scores(self) -> None:
        self.prepare()
        self.run_linker("team/new_md.md")
        for prompt in self.model.prompts[LinkJudgeResult.__name__]:
            self.assertNotIn("priority", prompt)
            self.assertNotIn("retrieval", prompt)

    def test_research_pool_hands_free_slot_to_waiting_request(self) -> None:
        self.prepare()
        settings = SimpleNamespace(**{**SETTINGS.__dict__, "wiki_linker_research_concurrency": 1})
        asyncio.run(
            linker.link_generated_document(
                self.project,
                "team/new_md.md",
                model=self.model,
                settings=settings,
                embedder=self.embedder,
            )
        )
        research = [i for i, name in enumerate(self.model.calls) if name == "DeepLinkResearchResult"]
        judges = [i for i, name in enumerate(self.model.calls) if name == "LinkJudgeResult"]
        self.assertTrue(research and judges)
        self.assertLess(max(research), min(judges))

    def test_judge_view_keeps_local_context_not_whole_page(self) -> None:
        body = "prefix\n" + ("x" * 5_000) + "証拠となる文" + ("y" * 5_000) + "\nfar-tail"
        view = linker.build_judge_view(
            {"page_id": "p", "title": "title", "summary": "summary"},
            body,
            [{"anchor_id": "lead", "heading": "(lead)", "line": 1}],
            "証拠となる文",
        )
        self.assertIn("証拠となる文", view)
        self.assertNotIn("far-tail", view)
        self.assertLess(len(view), 3_000)

    def test_zero_proposals_zero_links(self) -> None:
        self.prepare()
        self.model.accept = False
        marker = self.run_linker("team/new_md.md")
        self.assertEqual(marker["status"], "complete")
        self.assertEqual(marker["links_added"], 0)
        self.assertNotIn(
            "llm-wiki", (self.new_dir / "001-a-symptom.md").read_text(encoding="utf-8")
        )


# ---------------------------------------------------------------------------
# Commit / recovery tests
# ---------------------------------------------------------------------------


class CommitRecoveryTests(LinkerTestCase):
    def test_commit_recover_and_rollback(self) -> None:
        make_document(
            self.project,
            "team/x_doc_md.md",
            [{"filename": "001-a.md", "title": "X", "summary": "x",
              "ranges": [(1, 5)], "body": "# X\n\n導入。\n"}],
        )
        make_document(
            self.project,
            "team/y_doc_md.md",
            [{"filename": "001-b.md", "title": "Y", "summary": "y",
              "ranges": [(1, 5)], "body": "# Y\n\n導入。\n"}],
        )
        catalog = linker.LinkCatalog.open(self.project.linker_database)
        self.addCleanup(catalog.close)
        documents = {d.raw_rel: d for d in linker.discover_documents(self.project)}
        for document in documents.values():
            catalog.sync_document(document)
        doc_a = documents["team/x_doc_md.md"]
        doc_b = documents["team/y_doc_md.md"]
        link = linker.AcceptedLink(
            pair_id=linker.make_pair_id(doc_a.pages[0].page_id, doc_b.pages[0].page_id),
            page_a_id=doc_a.pages[0].page_id,
            page_b_id=doc_b.pages[0].page_id,
            relation_type="mechanism",
            discovery_path=[doc_a.pages[0].page_id, doc_b.pages[0].page_id],
            evidence={},
            a_anchor_id="lead",
            b_anchor_id="lead",
            a_bridge_template="Xから{link}を見る。",
            b_bridge_template="Yから{link}を見る。",
            a_footer_reason="相互参照のために関連する",
            b_footer_reason="相互参照のために関連する",
            novelty_score=90,
            usefulness_score=90,
            confidence_score=90,
            target_hashes={},
        )
        written = linker.commit_relations(
            self.project, catalog, "lrun-t1", doc_a.document_id, [link], []
        )
        self.assertEqual(len(written), 2)
        for row in catalog.conn.execute("SELECT status FROM links"):
            self.assertEqual(row[0], "active")

        # ---- interrupted commit: pending link, only one side rendered ----
        doc_c_dir = make_document(
            self.project,
            "team/z_doc_md.md",
            [{"filename": "001-c.md", "title": "Z", "summary": "z",
              "ranges": [(1, 5)], "body": "# Z\n\n導入。\n"}],
        )
        catalog.sync_document(
            next(d for d in linker.discover_documents(self.project) if d.raw_rel == "team/z_doc_md.md")
        )
        doc_c = next(d for d in linker.discover_documents(self.project) if d.raw_rel == "team/z_doc_md.md")
        second = linker.AcceptedLink(
            pair_id=linker.make_pair_id(doc_a.pages[0].page_id, doc_c.pages[0].page_id),
            page_a_id=doc_a.pages[0].page_id,
            page_b_id=doc_c.pages[0].page_id,
            relation_type="mechanism",
            discovery_path=[], evidence={},
            a_anchor_id="lead", b_anchor_id="lead",
            a_bridge_template="Xから{link}を見る。",
            b_bridge_template="Zから{link}を見る。",
            a_footer_reason="相互参照のために関連する",
            b_footer_reason="相互参照のために関連する",
            novelty_score=90, usefulness_score=90, confidence_score=90,
            target_hashes={},
        )
        page_a_path = self.project.wiki / doc_a.pages[0].wiki_rel_path
        page_c_path = self.project.wiki / doc_c.pages[0].wiki_rel_path
        base_a = linker.strip_managed_links(page_a_path.read_text(encoding="utf-8"))
        catalog.begin_relation_commit(
            "lrun-crash", doc_a.document_id, [second], [],
            [
                {"page_id": doc_a.pages[0].page_id, "base_hash": sha256_text(base_a)},
                {"page_id": doc_c.pages[0].page_id,
                 "base_hash": sha256_text(
                     linker.strip_managed_links(page_c_path.read_text(encoding="utf-8"))
                 )},
            ],
        )
        anchors = linker.extract_heading_anchors(base_a)
        write_ok = linker.render_managed_page(
            base_a,
            [("lead", second.pair_id, "Xから[Z](../z.doc/001-c.md)を見る。")],
            [("Z", "../z.doc/001-c.md", "相互参照のために関連する")],
            anchors,
        )
        linker.write_text_atomic(page_a_path, write_ok)  # only side A written
        # simulate restart: a fresh catalog recovers the interrupted run
        recovered = linker.recover_pending_runs(self.project, catalog)
        self.assertEqual(recovered, 1)
        for path in (page_a_path, page_c_path):
            text = path.read_text(encoding="utf-8")
            self.assertIn(second.pair_id, text)
            self.assertIn("## 関連資料", text)
        statuses = {row[0] for row in catalog.conn.execute("SELECT status FROM links")}
        self.assertEqual(statuses, {"active"})

    def test_write_failure_rolls_back_both_sides(self) -> None:
        make_document(
            self.project, "team/x_doc_md.md",
            [{"filename": "001-a.md", "title": "X", "summary": "x",
              "ranges": [(1, 5)], "body": "# X\n\n導入。\n"}],
        )
        make_document(
            self.project, "team/y_doc_md.md",
            [{"filename": "001-b.md", "title": "Y", "summary": "y",
              "ranges": [(1, 5)], "body": "# Y\n\n導入。\n"}],
        )
        catalog = linker.LinkCatalog.open(self.project.linker_database)
        self.addCleanup(catalog.close)
        docs = {d.raw_rel: d for d in linker.discover_documents(self.project)}
        for document in docs.values():
            catalog.sync_document(document)
        a = docs["team/x_doc_md.md"].pages[0]
        b = docs["team/y_doc_md.md"].pages[0]
        link = linker.AcceptedLink(
            pair_id="llink-x1", page_a_id=a.page_id, page_b_id=b.page_id,
            relation_type="mechanism", discovery_path=[], evidence={},
            a_anchor_id="lead", b_anchor_id="lead",
            a_bridge_template="X{link}", b_bridge_template="Y{link}",
            a_footer_reason="相互参照のために関連する",
            b_footer_reason="相互参照のために関連する",
            novelty_score=90, usefulness_score=90, confidence_score=90,
            target_hashes={},
        )
        original = linker.write_text_atomic
        calls = {"n": 0}

        def flaky(path, text):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return original(path, text)

        import graph.wiki.storage as storage

        originals = {
            p: (self.project.wiki / p).read_text(encoding="utf-8")
            for p in (a.wiki_rel_path, b.wiki_rel_path)
        }
        # patch the reference used inside linker.commit_relations
        linker.write_text_atomic = flaky
        try:
            with self.assertRaises(OSError):
                linker.commit_relations(
                    self.project, catalog, "lrun-t2", "ldoc-x", [link], []
                )
        finally:
            linker.write_text_atomic = original
        for rel, text in originals.items():
            self.assertEqual((self.project.wiki / rel).read_text(encoding="utf-8"), text)
        self.assertEqual(
            catalog.conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 0
        )
        self.assertEqual(
            catalog.conn.execute(
                "SELECT status FROM link_runs WHERE run_id='lrun-t2'"
            ).fetchone()[0],
            "failed",
        )


# ---------------------------------------------------------------------------
# Writer integration (WP-8/WP-9)
# ---------------------------------------------------------------------------


class WriterIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        from graph import writers

        self.writers = writers
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Project(Path(self._tmp.name)).ensure()
        self.raw_rel = "team/doc_md.md"
        self.wiki_dir = make_document(
            self.project,
            self.raw_rel,
            [{"filename": "001-a.md", "title": "A", "summary": "a",
              "ranges": [(1, 9)], "body": "# A\n\n本文。\n"}],
        )
        self.settings = SimpleNamespace(
            wiki_linker_enabled=True,
            wiki_output_language="Japanese (日本語)",
            wiki_linker_map_concurrency=1,
            wiki_linker_research_concurrency=1,
        )

    def fake_llm(self):
        class Port:
            name = "p"
            provider = "p"

            async def structured(self, schema, messages, **kw):
                raise AssertionError("not called")

            async def text(self, messages, **kw):
                return ""

        return Port()

    def test_disabled_mode_constructs_no_services(self) -> None:
        from unittest import mock

        settings = SimpleNamespace(wiki_linker_enabled=False)
        with mock.patch("graph.wiki.model.ChatModelPort",
                        side_effect=AssertionError("service built")), \
             mock.patch("graph.gateway.Embedder",
                        side_effect=AssertionError("service built")), \
             mock.patch("graph.gateway.Reranker",
                        side_effect=AssertionError("service built")), \
             mock.patch("graph.wiki.linker.link_generated_document",
                        side_effect=AssertionError("linker ran")):
            self.writers.run_wiki_linker(
                self.project, self.raw_rel, settings=settings,
                llm=None, embedder=None,
            )
        marker = json.loads(
            (self.wiki_dir / "_planning" / "linker.json").read_text(encoding="utf-8")
        )
        self.assertEqual(marker["status"], "disabled")

    def test_pending_marker_then_complete_and_services_reused(self) -> None:
        from unittest import mock

        seen = {}

        async def stub(project, rel, *, model, settings, embedder, reranker,
                       on_progress=None, stop_check=None):
            seen["marker_during_run"] = json.loads(
                (self.wiki_dir / "_planning" / "linker.json").read_text(encoding="utf-8")
            )["status"]
            seen["model_is_port"] = model is llm
            seen["embedder"] = embedder
            return {"status": "complete"}

        llm = self.fake_llm()  # port-like: reused, no ChatModelPort built
        embedder = object()
        with mock.patch("graph.wiki.linker.link_generated_document", stub), \
             mock.patch("graph.wiki.model.ChatModelPort",
                        side_effect=AssertionError("should not build")):
            self.writers.run_wiki_linker(
                self.project, self.raw_rel, settings=self.settings,
                llm=llm, embedder=embedder,
            )
        self.assertEqual(seen["marker_during_run"], "pending")
        self.assertTrue(seen["model_is_port"])
        self.assertIs(seen["embedder"], embedder)

    def test_concurrency_mirrors_rewrite_setting(self) -> None:
        import os
        from unittest import mock

        seen = {}

        async def stub(project, rel, *, model, settings, embedder, reranker,
                       on_progress=None, stop_check=None):
            seen["map"] = settings.wiki_linker_map_concurrency
            seen["research"] = settings.wiki_linker_research_concurrency
            return {"status": "complete"}

        settings = SimpleNamespace(
            wiki_linker_enabled=True, wiki_rewrite_concurrency=7,
            wiki_output_language="ja",
        )
        with mock.patch("graph.wiki.linker.link_generated_document", stub):
            self.writers.run_wiki_linker(
                self.project, self.raw_rel, settings=settings,
                llm=self.fake_llm(), embedder=None,
            )
        self.assertEqual(seen, {"map": 7, "research": 7})
        os.environ["WIKI_LINKER_RESEARCH_CONCURRENCY"] = "9"
        try:
            with mock.patch("graph.wiki.linker.link_generated_document", stub):
                self.writers.run_wiki_linker(
                    self.project, self.raw_rel, settings=settings,
                    llm=self.fake_llm(), embedder=None,
                )
        finally:
            del os.environ["WIKI_LINKER_RESEARCH_CONCURRENCY"]
        self.assertEqual(seen["map"], 7)
        self.assertEqual(seen["research"], 9)

    def test_source_stamp_follows_linker_completion(self) -> None:
        from unittest import mock

        def fake_build(**kwargs):
            out = Path(kwargs["out_dir"])
            (out / "docs").mkdir(parents=True)
            (out / "docs" / "001-a.md").write_text("# A\n", encoding="utf-8")
            return SimpleNamespace(out_dir=out, file_count=1)

        planning = self.wiki_dir / "_planning"
        with mock.patch.object(self.writers, "build_wiki_output", side_effect=fake_build):
            with mock.patch.object(
                self.writers, "run_wiki_linker",
                side_effect=linker.LinkerIncomplete("boom"),
            ):
                with self.assertRaises(linker.LinkerIncomplete):
                    self.writers.write_wiki(
                        self.project, self.raw_rel, mode="wiki",
                        settings=self.settings, llm=None, embedder=None,
                    )
            self.assertFalse((planning / "source.json").exists())
            with mock.patch.object(self.writers, "run_wiki_linker",
                                   return_value=None):
                self.writers.write_wiki(
                    self.project, self.raw_rel, mode="wiki",
                    settings=self.settings, llm=None, embedder=None,
                )
            self.assertTrue((planning / "source.json").exists())

    def test_up_to_date_marker_semantics(self) -> None:
        planning = self.wiki_dir / "_planning"
        self.writers.write_source_stamp(
            self.wiki_dir, self.project.raw / self.raw_rel, self.raw_rel
        )
        # legacy output (no marker) stays valid
        self.assertTrue(self.writers.up_to_date(self.project, self.raw_rel))
        for status, expected in (
            ("pending", False), ("failed", False),
            ("complete", True), ("disabled", True),
        ):
            write_json_atomic(planning / "linker.json", {"status": status})
            self.assertIs(
                self.writers.up_to_date(self.project, self.raw_rel), expected, status
            )

    def test_remove_document_cleans_peer(self) -> None:
        other = make_document(
            self.project, "team/peer_md.md",
            [{"filename": "001-b.md", "title": "B", "summary": "b",
              "ranges": [(1, 9)], "body": "# B\n\n本文。\n"}],
        )
        catalog = linker.LinkCatalog.open(self.project.linker_database)
        self.addCleanup(catalog.close)
        docs = {d.raw_rel: d for d in linker.discover_documents(self.project)}
        for document in docs.values():
            catalog.sync_document(document)
        a = docs[self.raw_rel].pages[0]
        b = docs["team/peer_md.md"].pages[0]
        link = linker.AcceptedLink(
            pair_id=linker.make_pair_id(a.page_id, b.page_id),
            page_a_id=a.page_id, page_b_id=b.page_id,
            relation_type="useful_analogy", discovery_path=[], evidence={},
            a_anchor_id="lead", b_anchor_id="lead",
            a_bridge_template="向く{link}。", b_bridge_template="逆{link}。",
            a_footer_reason="相互参照のた為に関連する", b_footer_reason="相互参照のた為に関連する",
            novelty_score=90, usefulness_score=90, confidence_score=90,
            target_hashes={},
        )
        linker.commit_relations(
            self.project, catalog, "lrun-rm", docs[self.raw_rel].document_id, [link], []
        )
        base_a = (self.wiki_dir / "001-a.md").read_text(encoding="utf-8")
        self.assertIn("llm-wiki-link", base_a)
        linker.remove_document(self.project, "team/peer_md.md")
        a_text = (self.wiki_dir / "001-a.md").read_text(encoding="utf-8")
        self.assertEqual(a_text, "# A\n\n本文。\n")
        self.assertEqual(
            catalog.conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 0
        )
        self.assertNotIn("llm-wiki", (other / "001-b.md").read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
