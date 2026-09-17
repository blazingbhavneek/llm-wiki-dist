"""End-to-end linker behaviour with a fake model and a fake embedder.

Two documents in one team: A defines the term ``X-Term``, B uses it.  The
fake model describes every chunk deterministically and accepts every
candidate it is shown, so the assertions are about plumbing (chunking,
catalog, bilateral rendering, caching, removal), not model judgement.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from graph.common.async_tools import run_async_blocking
from graph.linker import link_document, remove_document
from graph.linker.chunks import describe_all, make_chunks, split_page, validate_meta
from graph.linker.render import FOOTER_END, FOOTER_START, parse_footer
from graph.linker.wire import ChunkMeta, EdgeSuggestion, EdgeSuggestions, NeoEdgeSuggestion, NeoEdgeSuggestions
from graph.workspace.project import Project

A_PAGE = """# 校正の手順

このページは X-Term の定義を含む。

## X-Term の定義

X-Term とは、クロックの補正係数である。校正 装置 が X-Term を測定する。

## 測定方法

校正 装置 を接続し、X-Term を読み取る。

---

前のページ: なし ｜ 次のページ: なし
"""

B_PAGE = """# タイムアウトの症状

要求が設定より早く期限切れになる。

## 早期満了

タイマー が X-Term の影響で早く満了する。X-Term は別ページで定義されている。

```
## not a heading inside a fence
```
"""

C_PAGE = """# 無関係なページ

セッションの有効期限について。

## 画面のログアウト

ユーザー が 画面 からログアウトする。
"""


class FakeModel:
    name = "fake"
    provider = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def structured(self, schema: Any, messages: Any, *, max_output_tokens: int | None = None) -> Any:
        self.calls.append(schema.__name__)
        text = str(messages[-1].content)
        if schema is ChunkMeta:
            body = text.split("--- 本文 ---", 1)[-1]
            entities = []
            if "X-Term" in body:
                entities.append({"name": "X-Term", "kind": "parameter", "role": "defines" if "とは" in body else "uses"})
            for name in ("校正 装置", "タイマー", "ユーザー", "画面"):
                if name in body:
                    entities.append({"name": name, "kind": "concept", "role": "uses"})
            behaviours = []
            if "校正 装置" in body and "X-Term" in body:
                behaviours.append({"subject": "校正 装置", "action": "測定する", "object": "X-Term"})
            if "タイマー" in body:
                behaviours.append({"subject": "タイマー", "action": "満了する", "object": "X-Term"})
            return ChunkMeta(
                summary=body.strip().splitlines()[0][:80],
                keywords=["X-Term"] if "X-Term" in body else ["セッション"],
                entity="X-Term" if "X-Term" in body else "セッション",
                claims=["c1"],
                bridge_probe="clock",
                entities=entities,
                behaviours=behaviours,
            )
        payload = json.loads(text)
        if schema is EdgeSuggestions:
            return EdgeSuggestions(edges=[EdgeSuggestion(target_node_id=c["id"], label="relates-to", summary="fake edge") for c in payload["candidates"]])
        if schema is NeoEdgeSuggestions:
            return NeoEdgeSuggestions(edges=[NeoEdgeSuggestion(target_chunk_id=c["chunk_id"], label="interacts", summary="via entity") for c in payload["candidates"]])
        raise AssertionError(schema)


class FakeEmbedder:
    model_name = "fake-embed"
    dim = 3

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(t.count("X-Term")), float(t.count("セッション")), 1.0] for t in texts]


def settings(mode: str = "legacy") -> SimpleNamespace:
    return SimpleNamespace(wiki_linker_mode=mode, wiki_linker_concurrency=2, wiki_output_language="ja")


class LinkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Project(Path(self.tmp.name)).ensure()
        self.model = FakeModel()
        self.embedder = FakeEmbedder()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def publish(self, rel: str, pages: dict[str, str]) -> Path:
        (self.project.raw / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.project.raw / rel).write_text("# raw\n", encoding="utf-8")
        folder = self.project.wiki_dir(rel)
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in pages.items():
            (folder / name).write_text(text, encoding="utf-8")
        (folder / "_planning").mkdir(exist_ok=True)
        (folder / "_planning" / "metadata.json").write_text("{}", encoding="utf-8")
        return folder

    def link(self, rel: str, mode: str = "legacy"):
        return run_async_blocking(link_document(self.project, rel, model=self.model, embedder=self.embedder, settings=settings(mode)))

    # --- pure helpers -------------------------------------------------

    def test_split_page_ignores_headings_inside_fences(self) -> None:
        chunks = split_page(B_PAGE)
        self.assertEqual([c.heading for c in chunks], ["", "早期満了"])
        self.assertEqual(chunks[0].line_start, 1)

    def test_validate_meta_drops_entities_absent_from_text(self) -> None:
        meta = ChunkMeta(entities=[{"name": "ghost", "kind": "x"}, {"name": "X-Term", "kind": "p"}], behaviours=[{"subject": "ghost", "action": "a"}, {"subject": "X-Term", "action": "b", "object": "ghost"}])
        clean = validate_meta(meta, "X-Term appears here")
        self.assertEqual([e.name for e in clean.entities], ["X-Term"])
        self.assertEqual([(b.subject, b.object) for b in clean.behaviours], [("X-Term", "")])

    def test_entity_extraction_is_uncapped_and_later_chunks_can_split_names(self) -> None:
        names = [f"Entity-{i}" for i in range(25)]
        text = " ".join(names)
        meta = ChunkMeta(
            entities=[{"name": name, "kind": "concept"} for name in names],
            behaviours=[{"subject": name, "action": "appears"} for name in names],
        )
        clean = validate_meta(meta, text)
        self.assertEqual(len(clean.entities), 25)
        self.assertEqual(len(clean.behaviours), 25)

        class RollingModel:
            def __init__(self) -> None:
                self.prompts: list[str] = []

            async def structured(self, schema: Any, messages: Any) -> ChunkMeta:
                prompt = str(messages[-1].content)
                self.prompts.append(prompt)
                body = prompt.split("--- 本文 ---", 1)[-1]
                if "revealed separately" not in body:
                    return ChunkMeta(entities=[{"name": "Alpha-Beta", "kind": "component", "role": "defines"}])
                return ChunkMeta(entities=[
                    {"name": "Alpha", "kind": "component", "replaces": ["Alpha-Beta"]},
                    {"name": "Beta", "kind": "component", "replaces": ["Alpha-Beta"]},
                ])

        chunks = [
            *make_chunks("doc", "team", "001.md", "# First\n\nAlpha-Beta is introduced."),
            *make_chunks("doc", "team", "002.md", "# Second\n\nAlpha and Beta are revealed separately."),
        ]
        model = RollingModel()
        calls, fallbacks = run_async_blocking(describe_all(
            chunks, model=model, output_language="en", concurrency=4,
        ))
        self.assertEqual((calls, fallbacks), (2, 0))
        self.assertIn('"name": "Alpha-Beta"', model.prompts[1])
        self.assertEqual([entity.name for entity in chunks[0].entities], ["Alpha", "Beta"])
        self.assertEqual([entity.role for entity in chunks[0].entities], ["defines", "defines"])

    # --- end to end ---------------------------------------------------

    def test_first_document_links_nothing_and_writes_planning(self) -> None:
        folder = self.publish("t/a_docx.md", {"001-a.md": A_PAGE})
        result = self.link("t/a_docx.md")
        self.assertEqual(result.touched, [])
        self.assertEqual(result.edges_added, 0)
        self.assertEqual(self.model.calls.count("ChunkMeta"), 3)
        self.assertNotIn("EdgeSuggestions", self.model.calls)
        marker = json.loads((folder / "_planning" / "linker.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["status"], "complete")
        self.assertTrue((folder / "_planning" / "pages" / "001-a.md").exists())
        chunks = json.loads((folder / "_planning" / "chunks.json").read_text(encoding="utf-8"))
        self.assertEqual(len(chunks["pages"][0]["chunks"]), 3)
        self.assertEqual((folder / "001-a.md").read_text(encoding="utf-8"), A_PAGE)

    def test_second_document_links_both_ways_and_reruns_free(self) -> None:
        a = self.publish("t/a_docx.md", {"001-a.md": A_PAGE})
        self.link("t/a_docx.md")
        b = self.publish("t/b_docx.md", {"001-b.md": B_PAGE})
        result = self.link("t/b_docx.md")
        self.assertGreater(result.edges_added, 0)
        self.assertEqual(result.touched, ["t/a_docx.md"])
        a_text = (a / "001-a.md").read_text(encoding="utf-8")
        b_text = (b / "001-b.md").read_text(encoding="utf-8")
        for text in (a_text, b_text):
            self.assertIn(FOOTER_START, text)
            self.assertIn(FOOTER_END, text)
        self.assertIn("](../b.docx/001-b.md)", a_text)
        self.assertIn("](../a.docx/001-a.md)", b_text)
        self.assertTrue(a_text.startswith(A_PAGE.rstrip("\n")))
        links_a = json.loads((a / "_planning" / "links.json").read_text(encoding="utf-8"))
        links_b = json.loads((b / "_planning" / "links.json").read_text(encoding="utf-8"))
        self.assertEqual({e["edge_id"] for e in links_a["edges"]}, {e["edge_id"] for e in links_b["edges"]})
        parsed = parse_footer(b_text)
        self.assertTrue(parsed and all(link.label == "relates-to" for link in parsed))
        # unchanged rerun: no model calls, no byte changes
        before = (a_text, b_text)
        self.model.calls.clear()
        result = self.link("t/b_docx.md")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(result.touched, [])
        self.assertEqual(((a / "001-a.md").read_text(encoding="utf-8"), (b / "001-b.md").read_text(encoding="utf-8")), before)

    def test_removed_document_cleans_peer_footer(self) -> None:
        a = self.publish("t/a_docx.md", {"001-a.md": A_PAGE})
        self.link("t/a_docx.md")
        self.publish("t/b_docx.md", {"001-b.md": B_PAGE})
        self.link("t/b_docx.md")
        self.assertIn(FOOTER_START, (a / "001-a.md").read_text(encoding="utf-8"))
        touched = remove_document(self.project, "t/b_docx.md")
        self.assertEqual(touched, ["t/a_docx.md"])
        self.assertEqual((a / "001-a.md").read_text(encoding="utf-8"), A_PAGE)
        self.assertEqual(json.loads((a / "_planning" / "links.json").read_text(encoding="utf-8"))["edges"], [])

    def test_catalog_rebuild_from_planning_reuses_metadata(self) -> None:
        self.publish("t/a_docx.md", {"001-a.md": A_PAGE})
        self.link("t/a_docx.md")
        b = self.publish("t/b_docx.md", {"001-b.md": B_PAGE})
        self.link("t/b_docx.md")
        before = (b / "001-b.md").read_text(encoding="utf-8")
        self.project.linker_database.unlink()
        self.model.calls.clear()
        self.link("t/b_docx.md")
        self.assertNotIn("ChunkMeta", self.model.calls)
        self.assertEqual((b / "001-b.md").read_text(encoding="utf-8"), before)

    def test_mode_mismatch_is_an_error(self) -> None:
        from graph.linker import LinkerModeMismatch

        self.publish("t/a_docx.md", {"001-a.md": A_PAGE})
        self.link("t/a_docx.md", mode="legacy")
        with self.assertRaises(LinkerModeMismatch):
            self.link("t/a_docx.md", mode="neo")

    def test_neo_mode_inline_link_and_footer_groups(self) -> None:
        a = self.publish("t/a_docx.md", {"001-a.md": A_PAGE})
        self.link("t/a_docx.md", mode="neo")
        self.publish("t/c_docx.md", {"001-c.md": C_PAGE})
        self.link("t/c_docx.md", mode="neo")
        b = self.publish("t/b_docx.md", {"001-b.md": B_PAGE})
        result = self.link("t/b_docx.md", mode="neo")
        b_text = (b / "001-b.md").read_text(encoding="utf-8")
        a_text = (a / "001-a.md").read_text(encoding="utf-8")
        # Entity links wrap existing mentions two or three times and do not add footer prose.
        self.assertEqual(b_text.count("[X-Term](../a.docx/001-a.md)"), 2)
        self.assertNotIn("「X-Term」の定義", b_text)
        self.assertNotIn("「X-Term」を使用", a_text)
        # the unrelated page is never an inline target and its footer, if any, is similarity only
        c_text = (self.project.wiki / "t" / "c.docx" / "001-c.md").read_text(encoding="utf-8")
        self.assertNotIn("[X-Term](", c_text)
        links = parse_footer(c_text)
        self.assertTrue(all(link.label == "similar" for link in links))
        # one line per peer page for similarity, never one per chunk pair
        self.assertEqual(len(links), len({link.peer_path for link in links}))

    def test_publisher_splits_footer_into_its_own_block(self) -> None:
        from graph.growi.client import merge_marked_sections, split_footer, wrap_links, wrap_page

        page = "# t\n\nbody\n\n" + FOOTER_START + "\n## 関連リンク\n\n- [x](y.md) — uses: z\n" + FOOTER_END + "\n"
        main, footer = split_footer(page)
        self.assertEqual(main, "# t\n\nbody\n")
        self.assertTrue(footer.startswith(FOOTER_START) and footer.endswith(FOOTER_END))
        remote = wrap_page(main, page_id="p", ranges=[(1, 2)]) + "\n\n" + wrap_links(footer, page_id="p")
        self.assertIn("<!-- chunk: p-links", remote)
        # a footer-only change touches only the -links block
        updated = wrap_page(main, page_id="p", ranges=[(1, 2)]) + "\n\n" + wrap_links("", page_id="p")
        merged = merge_marked_sections(remote, updated)
        self.assertIn("body", merged)
        self.assertNotIn("uses: z", merged)


if __name__ == "__main__":
    unittest.main()
