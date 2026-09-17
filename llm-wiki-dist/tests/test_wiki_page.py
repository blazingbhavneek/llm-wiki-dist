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
        # Title + A (19 lines) pack into one section; B is cut at its heading.
        self.assertEqual(sections[0], (1, 19))
        self.assertIn(20, [s for s, _ in sections])
        self.assertGreater(len(sections), 3)
        self.assertTrue(all(e - s + 1 <= 40 for s, e in sections))

    def test_adjacent_headings_pack_up_to_target(self) -> None:
        lines = (["## a"] + ["x"] * 9) * 6  # six 10-line subsections
        self.assertEqual(page.split_sections(lines, 1, 60, target=30, min_lines=2), [(1, 30), (31, 60)])
        self.assertEqual(page.split_sections(lines, 1, 60, target=10, min_lines=2), [(i, i + 9) for i in range(1, 60, 10)])

    def test_heading_inside_fence_is_not_a_cut(self) -> None:
        lines = ["## top", "text", "```sh", "# not a heading", "```", "tail"]
        self.assertEqual(page.split_sections(lines, 1, 6, target=80, min_lines=1), [(1, 6)])

    def test_tiny_sections_merge(self) -> None:
        lines = ["## a", "x", "## b", "y", "## c", "z"]
        self.assertEqual(page.split_sections(lines, 1, 6, target=80, min_lines=3), [(1, 6)])

    def test_offsets_are_absolute(self) -> None:
        lines = ["skip"] * 10 + ["## a"] + ["x"] * 10 + ["## b"] + ["y"] * 10
        sections = page.split_sections(lines, 11, 32, target=15, min_lines=2)
        self.assertEqual(sections, [(11, 21), (22, 32)])


class LosslessCheckTests(unittest.TestCase):
    def test_code_tokens_keep_identifiers_and_drop_prose(self) -> None:
        tokens = page.code_tokens("call mpf_mfs_open with E_MFS_ETIMEOUT at 0x1F; the file [[NEO-IMAGE:abc_1]]")
        self.assertEqual(tokens, {"mpf_mfs_open", "E_MFS_ETIMEOUT", "0x1F"})

    def test_code_tokens_compare_plural_acronyms_by_stem(self) -> None:
        self.assertEqual(page.code_tokens("uses GPUs, CPUs and VEs") - page.code_tokens("GPU と CPU と VE"), set())

    def test_code_tokens_ignore_doc_parser_backslash_escapes(self) -> None:
        # doc-parser/pandoc escapes underscores in prose ("mpi\_aware"); a
        # faithful rewrite naturally drops the escape, so it must not count
        # as a lost identifier (see graph/wiki/page.py MD_ESCAPE_RE).
        escaped = page.code_tokens(r"the mpi\_aware flag and nd\_range call")
        plain = page.code_tokens("the mpi_aware flag and nd_range call")
        self.assertEqual(escaped, plain)
        self.assertEqual(escaped, {"mpi_aware", "nd_range"})

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
        self.assertNotIn("参照元", joined)

    def test_check_section_passes_a_lossless_draft(self) -> None:
        lines = source()
        draft = (
            "## A\n\n```sh\n# comment\nx_y = 1\n```\n\n"
            "|a|b|\n|---|---|\n|1|mpf_open|\n\n[[NEO-IMAGE:x]]\n\n"
            "設定が必要である。\n"
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

    def test_entity_mentions_are_spread_and_reader_references_are_removed(self) -> None:
        markdown = "# X-Term\n\n" + "\n\n".join(f"段落{i} X-Term を使う。" for i in range(6))
        linked = page.link_entity_mentions(markdown, "X-Term", "definition.md")
        self.assertEqual(linked.count("[X-Term](definition.md)"), 3)
        self.assertEqual(page.link_entity_mentions(linked, "X-Term", "definition.md"), linked)
        cited = "説明。（参照元: [設定](002-settings.md) 原文 17-18行）\n"
        self.assertEqual(page.strip_reader_references(cited), "説明。\n")

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
