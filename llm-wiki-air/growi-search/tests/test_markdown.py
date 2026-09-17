import unittest

import markdown as md


class StripHighlights(unittest.TestCase):
    def test_removes_highlight_keeps_japanese(self):
        raw = 'これは<em class="highlighted-keyword">系統制御</em>です &amp; OK'
        self.assertEqual(md.strip_search_highlights(raw), "これは系統制御です & OK")

    def test_bare_em(self):
        self.assertEqual(md.strip_search_highlights("<em>x</em>y"), "xy")


class PageTitle(unittest.TestCase):
    def test_first_heading(self):
        self.assertEqual(md.page_title("/a/b", "\n# 概要\ntext"), "概要")

    def test_falls_back_to_path(self):
        self.assertEqual(md.page_title("/a/b/MyPage", "no heading"), "MyPage")


class SplitSections(unittest.TestCase):
    BODY = "# 概要\nline2\nline3\n## 関連資料\nlink\nmore\n"

    def test_exact_line_numbers(self):
        sections = md.split_sections(self.BODY)
        top = sections[0]
        self.assertEqual(top.heading, "概要")
        self.assertEqual(top.level, 1)
        self.assertEqual((top.start_line, top.end_line), (1, 3))
        sub = sections[1]
        self.assertEqual(sub.heading, "関連資料")
        self.assertEqual(sub.start_line, 4)
        self.assertIn("more", sub.body)

    def test_preamble_kept(self):
        sections = md.split_sections("intro line\n# H\nbody")
        self.assertEqual(sections[0].heading, "")
        self.assertIn("intro line", sections[0].body)

    def test_no_split_inside_fence(self):
        body = "# A\n```\n# not a heading\n```\ndone\n"
        sections = md.split_sections(body)
        self.assertEqual([s.heading for s in sections], ["A"])
        self.assertIn("# not a heading", sections[0].body)


class ExtractLinks(unittest.TestCase):
    def test_id_link(self):
        links = md.extract_links("see [x](/507f1f77bcf86cd799439011)")
        self.assertEqual(links[0].raw_target, "/507f1f77bcf86cd799439011")

    def test_absolute_and_fragment(self):
        links = md.extract_links("[x](/Moove/Manual#節)")
        self.assertEqual(links[0].raw_target, "/Moove/Manual")
        self.assertEqual(links[0].fragment, "節")

    def test_em_dash_summary(self):
        links = md.extract_links("[x](/a) — 関連する資料です")
        self.assertEqual(links[0].summary, "関連する資料です")

    def test_heading_breadcrumb(self):
        body = "## 運用\n### 詳細\n[x](/a)\n"
        links = md.extract_links(body)
        self.assertEqual(links[0].heading, "運用 > 詳細")

    def test_ignores_image(self):
        self.assertEqual(md.extract_links("![alt](/image.png)"), [])

    def test_ignores_fenced_and_indented(self):
        self.assertEqual(md.extract_links("```\n[x](/a)\n```"), [])
        self.assertEqual(md.extract_links("    [x](/b)"), [])

    def test_ignores_external_and_attachment(self):
        self.assertEqual(md.extract_links("[a](https://x.com/y) [b](/attachment/z)"), [])

    def test_dedupes_same_target(self):
        links = md.extract_links("[a](/same) and [b](/same)")
        self.assertEqual(len(links), 1)

    def test_navigation_links_are_typed(self):
        body = "---\n\n前のページ: [a](/aaaaaaaaaaaaaaaaaaaaaaaa) ｜ 次のページ: [b](/bbbbbbbbbbbbbbbbbbbbbbbb)"
        self.assertEqual([link.kind for link in md.extract_links(body)], ["nav", "nav"])

    def test_index_footer_link_is_markdown(self):
        link = md.extract_links("- [t](/cccccccccccccccccccccccc) — s")[0]
        self.assertEqual(link.kind, "markdown")


class IndexPages(unittest.TestCase):
    BODY = """# Manual

<span hidden data-llm-wiki-index="document"></span>

- [概要](/6aab1ff0d4652631606ec418) — summary
  - 章: 第1章 はじめに
  - キーワード: 運転モード、状態遷移、構成制御
  - エンティティ: 運転モード管理、状態遷移
- [外部ページ](/Moove/Manual/001-概要) — other
  - ページ数: 4
"""

    def test_parse_index(self):
        cards = md.parse_index(self.BODY)
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0].target, "/6aab1ff0d4652631606ec418")
        self.assertEqual(cards[0].chapter, "第1章 はじめに")
        self.assertGreaterEqual(len(cards[0].keywords), 3)
        self.assertEqual(cards[0].entities, ["運転モード管理", "状態遷移"])
        self.assertEqual(cards[1].target, "/Moove/Manual/001-概要")
        self.assertEqual(cards[1].pages, 4)

    def test_marker_detection(self):
        self.assertTrue(md.is_index_page(self.BODY))
        self.assertFalse(md.is_index_page("# ordinary page\n"))


class ResolveTarget(unittest.TestCase):
    def test_id(self):
        t = md.resolve_target("/Moove/x", "/507f1f77bcf86cd799439011", "/Moove")
        self.assertEqual(t.kind, "id")
        self.assertEqual(t.page_id, "507f1f77bcf86cd799439011")

    def test_relative_and_md_suffix(self):
        t = md.resolve_target("/Moove/a/page", "sub.md", "/Moove")
        self.assertEqual(t.path, "/Moove/a/sub")

    def test_fragment_preserved(self):
        t = md.resolve_target("/Moove/a", "/Moove/b#sec", "/Moove")
        self.assertEqual(t.fragment, "sec")

    def test_outside_root_rejected(self):
        self.assertIsNone(md.resolve_target("/Moove/a", "/etc/passwd", "/Moove"))


class DeterministicEdgeIds(unittest.TestCase):
    def test_stable(self):
        from models import make_link_id

        a = make_link_id("s", "t", "anchor", "frag")
        b = make_link_id("s", "t", "anchor", "frag")
        c = make_link_id("s", "t", "anchor", "other")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


if __name__ == "__main__":
    unittest.main()
