"""Pure section/lossless/link helpers used by the wiki page writer."""

from __future__ import annotations

import unittest

from graph.wiki import page
from graph.wiki.wire import ReferenceFact


def source() -> list[str]:
    lines = ["# Title", "", "intro a", "b", "c", "d", "e", "f", "g", "h"]
    lines += ["## A", "```sh", "# comment", "x_y = 1", "```"]
    lines += ["| a | b |", "|---|---|", "| 1 | mpf_open |", ""]
    lines += ["## B"] + [f"line {i} E_CODE_{i}" for i in range(100)]
    return lines


class SplitSectionsTests(unittest.TestCase):
    def test_sections_tile_the_range_and_cut_before_headings(self) -> None:
        lines = source()
        sections = page.split_sections(lines, 1, len(lines), target=40, min_lines=8)
        self.assertEqual(sections[0][0], 1)
        self.assertEqual(sections[-1][1], len(lines))
        for (_, left_end), (right_start, _) in zip(sections, sections[1:]):
            self.assertEqual(left_end + 1, right_start)
        self.assertIn(11, [s for s, _ in sections])
        self.assertIn(20, [s for s, _ in sections])
        self.assertGreater(len(sections), 3)

    def test_heading_inside_fence_is_not_a_cut(self) -> None:
        lines = ["## top", "text", "```sh", "# not a heading", "```", "tail"]
        self.assertEqual(page.split_sections(lines, 1, 6, target=80, min_lines=1), [(1, 6)])

    def test_tiny_sections_merge(self) -> None:
        lines = ["## a", "x", "## b", "y", "## c", "z"]
        self.assertEqual(page.split_sections(lines, 1, 6, target=80, min_lines=3), [(1, 6)])

    def test_offsets_are_absolute(self) -> None:
        lines = ["skip"] * 10 + ["## a"] + ["x"] * 10 + ["## b"] + ["y"] * 10
        sections = page.split_sections(lines, 11, 32, target=80, min_lines=2)
        self.assertEqual(sections, [(11, 21), (22, 32)])


class LosslessCheckTests(unittest.TestCase):
    def test_code_tokens_keep_identifiers_and_drop_prose(self) -> None:
        tokens = page.code_tokens("call mpf_mfs_open with E_MFS_ETIMEOUT at 0x1F; the file [[NEO-IMAGE:abc_1]]")
        self.assertEqual(tokens, {"mpf_mfs_open", "E_MFS_ETIMEOUT", "0x1F"})

    def test_verbatim_blocks_are_found_with_absolute_lines(self) -> None:
        self.assertEqual(page.verbatim_blocks(source(), 1, 20), [("fence", 12, 15), ("table", 16, 18)])

    def test_normalize_strips_wrapper_fence_and_demotes_h1_outside_fences(self) -> None:
        raw = "```markdown\n# Title\n\n```sh\n# comment\nx_y = 1\n```\n```"
        self.assertEqual(page.normalize_draft(raw), "## Title\n\n```sh\n# comment\nx_y = 1\n```\n")
        self.assertEqual(page.normalize_draft("<think>hmm</think>text"), "text\n")

    def test_check_section_reports_every_lost_item(self) -> None:
        lines = source()
        blocks = page.verbatim_blocks(lines, 1, 20)
        source_text = "\n".join(lines[:20])
        fact = ReferenceFact(description="設定が必要", source_start=300, source_end=301)
        errors = page.check_section(
            "## A\n\nsome prose without the code or table\n",
            lines=lines,
            source_text=source_text,
            block_ranges=blocks,
            placeholders=["[[NEO-IMAGE:x]]"],
            facts=[fact],
        )
        joined = "\n".join(errors)
        self.assertIn("12-15行のコードブロック", joined)
        self.assertIn("16-18行の表", joined)
        self.assertIn("[[NEO-IMAGE:x]]", joined)
        self.assertIn("mpf_open", joined)
        self.assertIn("x_y", joined)
        self.assertIn("300-301行", joined)

    def test_check_section_passes_a_lossless_draft(self) -> None:
        lines = source()
        draft = (
            "## A\n\n```sh\n# comment\nx_y = 1\n```\n\n"
            "|a|b|\n|---|---|\n|1|mpf_open|\n\n[[NEO-IMAGE:x]]\n\n"
            "設定が必要である。（参照元: 原文 300-301行）\n"
        )
        errors = page.check_section(
            draft,
            lines=lines,
            source_text="\n".join(lines[10:19]),
            block_ranges=page.verbatim_blocks(lines, 1, 20),
            placeholders=["[[NEO-IMAGE:x]]"],
            facts=[ReferenceFact(description="設定が必要", source_start=300, source_end=301)],
        )
        self.assertEqual(errors, [])

    def test_facts_land_in_the_section_owning_target_line(self) -> None:
        sections = [(1, 10), (11, 20)]
        facts = [ReferenceFact(target_line=15), ReferenceFact(target_line=0), ReferenceFact(target_line=99)]
        buckets = page.assign_facts(facts, sections)
        self.assertEqual([len(bucket) for bucket in buckets], [2, 1])


class LinkTitlesTests(unittest.TestCase):
    def test_links_first_plain_occurrence_longest_title_first(self) -> None:
        text = "# H\n\nsee ファイル管理API and ファイル管理 here\n```\nファイル管理\n```\n"
        linked = page.link_titles(text, [("ファイル管理", "1.md"), ("ファイル管理API", "2.md")])
        self.assertIn("[ファイル管理API](2.md)", linked)
        self.assertIn("[ファイル管理](1.md)", linked)
        self.assertIn("```\nファイル管理\n```", linked)
        self.assertEqual(page.link_titles(linked, [("ファイル管理", "1.md")]), linked)

    def test_skips_headings_tables_images_and_inline_code(self) -> None:
        text = "## 概要\n| 概要 | x |\n<img alt='概要'>\n`概要` and 概要\n"
        linked = page.link_titles(text, [("概要", "1.md")])
        self.assertEqual(linked, "## 概要\n| 概要 | x |\n<img alt='概要'>\n`概要` and [概要](1.md)\n")


if __name__ == "__main__":
    unittest.main()
