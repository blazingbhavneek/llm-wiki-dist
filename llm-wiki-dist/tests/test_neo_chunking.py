"""Stage A: atomic-block awareness, exact coverage, and honest fallbacks."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence

from graph.neo import document_map, images, markdown_blocks, pipeline, prompts, windows
from graph.neo.agent import AgentReply, PiAgent
from graph.neo.schemas import (
    CompiledSeedPlan,
    ObservationSet,
    RegionalReport,
    WindowReport,
)
from graph.neo.wire import (
    ImportantOmission,
    ObservedRange,
    PageJudgeResult,
    ReferenceFact,
    ReferenceResearchResult,
    ReferenceSelection,
    RegionalPage,
    RegionalPlan,
    SeedPlan,
    SeedRange,
    SemanticPlan,
    WikiPlanJudgeResult,
    WindowInventory,
)


def fake_source() -> str:
    """A document with a long fence, a table, and a base64 image unit."""

    payload = "QUJD" * 90
    body: list[str] = ["# Deployment Handbook", ""]
    body += [f"Intro paragraph line {index}." for index in range(1, 21)]
    body += ["", "## Fenced section", "", "```python"]
    body += [
        f"def function_{index}(): ...  # padded line {index}" for index in range(1, 61)
    ]
    body += ["```", "", "## Table section", ""]
    body += ["| step | action |", "| --- | --- |"]
    body += [f"| {index} | do thing {index} |" for index in range(1, 41)]
    body += [
        "",
        "## Diagram",
        "",
        "<image-unit>",
        f'<img src="data:image/png;base64,{payload}" alt="pipeline diagram">',
        "<image-description>Shows the ingest flow.</image-description>",
        "</image-unit>",
        "",
    ]
    body += [f"Trailing note {index}." for index in range(1, 91)]
    return "\n".join(body)


def base64_run() -> str:
    return "QUJD" * 90


class FakeModel:
    """Stands in for a ModelPort; behaviour is decided by each test."""

    name = "fake-model"
    provider = "fake"

    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.calls: list[str] = []

    async def structured(self, schema, messages, *, max_output_tokens=None) -> Any:
        self.calls.append(schema.__name__)
        return await self.behaviour(schema, messages)


def numbered_range(messages: Sequence[Any]) -> tuple[int, int]:
    """The first and last real source line the prompt actually shows."""

    numbers = [
        int(line.split(":", 1)[0])
        for line in messages[-1].content.splitlines()
        if line.split(":", 1)[0].isdigit()
    ]
    if not numbers:
        raise AssertionError("prompt contains no numbered source lines")
    return min(numbers), max(numbers)


async def whole_window(schema, messages):
    start, end = numbered_range(messages)
    return WindowInventory(
        summary=f"Everything between {start} and {end}.",
        observations=[
            ObservedRange(
                title=f"Lines {start}-{end}",
                summary=f"Everything between {start} and {end}.",
                source_start=start,
                source_end=end,
            )
        ]
    )


class BlockIndexTests(unittest.TestCase):
    def test_fence_table_and_image_units_are_found(self) -> None:
        lines = fake_source().splitlines()
        index = markdown_blocks.build_block_index(lines)

        kinds = {block.kind for block in index.blocks}
        self.assertLessEqual({"fence", "table", "image-unit"}, kinds)

        fence = next(block for block in index.blocks if block.kind == "fence")
        self.assertTrue(index.cut_is_safe(fence.start))
        self.assertFalse(index.cut_is_safe((fence.start + fence.end) // 2))
        self.assertTrue(index.cut_is_safe(fence.end + 1))

    def test_nearest_safe_cut_traverses_an_oversized_block(self) -> None:
        lines = ["a", "b", "```"] + ["code"] * 300 + ["```", "c"]
        index = markdown_blocks.build_block_index(lines)

        # The fence is far longer than any target: atomicity wins over the size.
        self.assertEqual(index.nearest_safe_cut(150, backsearch=40), 305)

    def test_unclosed_block_is_reported_not_sliced(self) -> None:
        with self.assertRaises(markdown_blocks.MalformedBlockError):
            markdown_blocks.build_block_index(["a", "```"] + ["code"] * 50)

    def test_windows_tile_the_source_without_splitting_a_block(self) -> None:
        lines = fake_source().splitlines()
        index = markdown_blocks.build_block_index(lines)
        windows = markdown_blocks.atomic_windows(lines, target=100, backsearch=25)

        expected = 1
        for start, end in windows:
            self.assertEqual(start, expected)
            self.assertLessEqual(end, len(lines))
            expected = end + 1
        self.assertEqual(expected, len(lines) + 1)

        for block in index.blocks:
            home = [
                (start, end) for start, end in windows if start <= block.start <= end
            ]
            self.assertEqual(len(home), 1, f"block {block} in {home}")
            start, end = home[0]
            self.assertLessEqual(start, block.start)
            self.assertLessEqual(block.end, end)


class SeedPlanTests(unittest.TestCase):
    def test_compiled_ranges_become_numbered_seed_pages(self) -> None:
        plan = CompiledSeedPlan(
            pages=[
                SeedRange(
                    title="概要", summary="概要説明", source_start=1, source_end=10
                ),
                SeedRange(
                    title="API", summary="API説明", source_start=11, source_end=30
                ),
            ]
        )

        pages = pipeline._plan_pages(plan)

        self.assertEqual(
            [page.owner_ranges for page in pages], [[(1, 10)], [(11, 30)]]
        )
        self.assertEqual(
            [page.filename for page in pages], ["001-概要.md", "002-API.md"]
        )

    def test_seed_validation_rejects_line_leaks(self) -> None:
        index = markdown_blocks.build_block_index(["line"] * 30)
        plan = SeedPlan(
            pages=[
                SeedRange(
                    title="A", summary="A", source_start=1, source_end=10
                ),
                SeedRange(
                    title="B", summary="B", source_start=12, source_end=30
                ),
            ]
        )

        checked, error = document_map.validate_seed_plan(
            plan, source_line_count=30, block_index=index
        )

        self.assertIsNone(checked)
        self.assertIn("gap", error)
        self.assertIn("must be 11", error)

    def test_seed_validation_sends_under_twenty_lines_back_to_the_llm(self) -> None:
        lines = ["line"] * 50
        plan = SeedPlan(
            pages=[
                SeedRange(title="A", summary="A", source_start=1, source_end=10),
                SeedRange(title="B", summary="B", source_start=11, source_end=50),
            ]
        )

        checked, error = document_map.validate_seed_plan(
            plan,
            source_line_count=len(lines),
            block_index=markdown_blocks.build_block_index(lines),
        )

        self.assertIsNone(checked)
        self.assertIn("at least 20", error)
        self.assertIn("previous or next", error)

    def test_seed_validation_snaps_boundary_outside_atomic_block(self) -> None:
        lines = ["before", "```", "code", "```", "after"]
        plan = SeedPlan(
            pages=[
                SeedRange(title="A", summary="A", source_start=1, source_end=2),
                SeedRange(title="B", summary="B", source_start=3, source_end=5),
            ]
        )

        checked, error = document_map.validate_seed_plan(
            plan,
            source_line_count=len(lines),
            block_index=markdown_blocks.build_block_index(lines),
        )

        self.assertIsNone(error)
        self.assertEqual(
            [(page.source_start, page.source_end) for page in checked.pages],
            [(1, 1), (2, 5)],
        )

    def test_boundary_before_atomic_block_is_already_safe(self) -> None:
        lines = ["before", "<image-unit>", "image", "</image-unit>", "after"]
        plan = SeedPlan(
            pages=[
                SeedRange(title="A", summary="A", source_start=1, source_end=1),
                SeedRange(title="B", summary="B", source_start=2, source_end=5),
            ]
        )

        checked, error = document_map.validate_seed_plan(
            plan,
            source_line_count=len(lines),
            block_index=markdown_blocks.build_block_index(lines),
        )

        self.assertIsNone(error)
        self.assertEqual(checked.pages[1].source_start, 2)

    def test_reference_paths_are_absolute_and_include_summaries(self) -> None:
        pages = [
            pipeline.SeedPage(
                number=1,
                title="概要",
                chapter="導入",
                summary="文書の目的",
                owner_ranges=[(1, 10)],
                filename="001-概要.md",
                page_id="page-001",
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = pipeline._page_index(pages, root / "seeds", root / "source.md")

        self.assertIn(str((root / "seeds" / pages[0].filename).resolve()), view)
        self.assertIn(str((root / "source.md").resolve()), view)
        self.assertIn("文書の目的", view)

    def test_cross_page_markers_are_collected_without_rejecting_the_page(self) -> None:
        markdown = "説明。（参照元: 原文 40-45行）\n別件。（参照元: 原文 46-50行）"

        ranges = pipeline._reference_ranges_from_markdown(markdown, [(1, 20)], 100)

        self.assertEqual(ranges, [(40, 50)])

    def test_dropped_image_placeholder_is_restored_once(self) -> None:
        lines = fake_source().splitlines()
        unit = images.extract_image_units(lines)[0]

        repaired = pipeline._preserve_image_placeholders("# Wiki\n", [unit], lines)
        duplicated = pipeline._preserve_image_placeholders(
            f"{unit.placeholder}\n{unit.placeholder}\n", [unit], lines
        )
        restored, unresolved = images.restore_images(repaired, [unit])

        self.assertEqual(repaired.count(unit.placeholder), 1)
        self.assertEqual(duplicated.count(unit.placeholder), 1)
        self.assertIn(unit.raw, restored)
        self.assertEqual(unresolved, [])

    def test_dropped_image_is_reinserted_next_to_original_heading(self) -> None:
        source = "## 対象機能\n<image-unit>\n<img src=\"data:image/png;base64,QUJD\">\n</image-unit>\n説明"
        lines = source.splitlines()
        unit = images.extract_image_units(lines)[0]

        repaired = pipeline._preserve_image_placeholders(
            "# Wiki\n\n## 対象機能\n\n説明\n", [unit], lines
        )

        self.assertLess(repaired.index("## 対象機能"), repaired.index(unit.placeholder))
        self.assertLess(repaired.index(unit.placeholder), repaired.index("説明"))


class PublicationTests(unittest.TestCase):
    @staticmethod
    def page() -> pipeline.SeedPage:
        return pipeline.SeedPage(
            number=1,
            title="概要",
            chapter="導入",
            summary="説明",
            owner_ranges=[(1, 10)],
            filename="001-概要.md",
            page_id="page-001",
        )

    def test_rewritten_markdown_has_no_internal_frontmatter(self) -> None:
        rendered = pipeline._render_page(
            self.page(), "# 概要\n\n本文", status="rewritten", rewrite_version="v1"
        )

        self.assertEqual(rendered, "# 概要\n\n本文\n")

    def test_legacy_frontmatter_is_migrated_to_sidecar_state(self) -> None:
        page = self.page()
        legacy = (
            "---\n"
            'title: "概要"\n'
            "page_number: 1\n"
            'status: "rewritten"\n'
            'rewrite_version: "v1"\n'
            "source_ranges: [[1, 10]]\n"
            "reference_ranges: []\n"
            "---\n"
            "# 概要\n\n本文\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / page.filename
            state = root / "state" / "001.json"
            output.write_text(legacy, encoding="utf-8")

            resumed = pipeline._resume_rewritten_page(
                output,
                state,
                page,
                rewrite_version="v1",
                source_line_count=10,
            )

            self.assertIsNotNone(resumed)
            self.assertEqual(output.read_text(encoding="utf-8"), "# 概要\n\n本文\n")
            self.assertTrue(state.exists())

    def test_link_validation_allows_only_inline_links(self) -> None:
        original = "# API\n\nets_printfを使用する。\n"
        linked = "# API\n\n[ets_printf](003-ets-printf.md)を使用する。\n"
        appended = linked + "\n## 関連機能\n\n- [別ページ](004-other.md)\n"

        self.assertEqual(
            pipeline._validate_additive_links(
                original,
                linked,
                allowed_filenames={"003-ets-printf.md", "004-other.md"},
                own_filename="001-api.md",
            ),
            "",
        )
        self.assertIn(
            "only inline links",
            pipeline._validate_additive_links(
                original,
                appended,
                allowed_filenames={"003-ets-printf.md", "004-other.md"},
                own_filename="001-api.md",
            ),
        )

    def test_provenance_maps_imported_ranges_to_their_wiki_pages(self) -> None:
        page = self.page()
        page.reference_ranges = [(12, 14)]
        referenced = pipeline.SeedPage(
            number=2,
            title="参照API",
            chapter="API",
            summary="参照情報",
            owner_ranges=[(11, 20)],
            filename="002-参照api.md",
            page_id="page-002",
        )

        provenance = pipeline._page_provenance(
            page,
            [page, referenced],
            source_path=Path("/tmp/input.md"),
            source_snapshot_path=Path("/tmp/snapshot.md"),
            source_sha256="abc123",
        )

        self.assertEqual(provenance["owned_line_ranges"], [[1, 10]])
        self.assertEqual(provenance["imported_line_ranges"], [[12, 14]])
        self.assertEqual(
            provenance["imported_from_pages"],
            [
                {
                    "number": 2,
                    "title": "参照API",
                    "filename": "002-参照api.md",
                    "source_ranges": [[12, 14]],
                }
            ],
        )

    def test_rewrite_references_are_copied_inside_the_task_directory(self) -> None:
        page = self.page()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local_seeds, local_source = pipeline._stage_rewrite_references(
                root / "task",
                [page],
                lines=[f"line {number}" for number in range(1, 11)],
                units=[],
                seed_root=root / "missing-external-seeds",
            )
            index = pipeline._page_index([page], local_seeds, local_source)

            self.assertTrue((local_seeds / page.filename).exists())
            self.assertTrue(local_source.exists())
            self.assertIn(str((local_seeds / page.filename).resolve()), index)
            self.assertIn(str(local_source.resolve()), index)


class RewriteJudgeTests(unittest.IsolatedAsyncioTestCase):
    def test_reference_agent_cannot_finish_before_required_reads(self) -> None:
        self.assertIn(
            "002",
            pipeline._reference_finish_error([3, 4, 5], {2, 3}, 3, "report"),
        )
        self.assertIn(
            "少なくとも5ページ",
            pipeline._reference_finish_error([2, 3], {2, 3}, 5, "report"),
        )
        self.assertIn(
            "report",
            pipeline._reference_finish_error([2, 3], {2, 3}, 2, ""),
        )
        self.assertEqual(
            pipeline._reference_finish_error([2, 3], {2, 3}, 2, "done"), ""
        )

    async def test_reference_research_inlines_full_seeds_and_keeps_provenance(self) -> None:
        async def research(schema, messages):
            prompt = messages[-1].content
            if schema is ReferenceSelection:
                self.assertIn("1: 対象の説明", prompt)
                self.assertIn("件数上限はない", prompt)
                return ReferenceSelection(page_numbers=[2])
            self.assertIs(schema, ReferenceResearchResult)
            self.assertIn("3: 必須設定", prompt)
            self.assertIn("4: 有効化が必要", prompt)
            return ReferenceResearchResult(
                useful_facts=[
                    ReferenceFact(
                        description="機能の利用前に設定を有効化する。",
                        reason="利用条件を説明するため。",
                        insertion_point="前提条件",
                        source_start=3,
                        source_end=4,
                    )
                ]
            )

        target = pipeline.SeedPage(
            number=1,
            title="対象API",
            chapter="",
            summary="対象の説明",
            owner_ranges=[(1, 2)],
            filename="001-target.md",
            page_id="page-001",
        )
        reference = pipeline.SeedPage(
            number=2,
            title="設定",
            chapter="",
            summary="必須設定",
            owner_ranges=[(3, 4)],
            filename="002-settings.md",
            page_id="page-002",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence, markdown = await pipeline._research_references(
                target,
                pages=[target, reference],
                lines=["対象の説明", "対象の詳細", "必須設定", "有効化が必要"],
                units=[],
                model=FakeModel(research),
                config=pipeline.NeoConfig(reference_attempts=1),
                work_root=root,
                seed_root=root / "seeds",
                stop_check=None,
                on_progress=None,
            )

            self.assertEqual(pipeline._researched_ranges(evidence), [(3, 4)])
            self.assertIn(str((root / "seeds" / "002-settings.md").resolve()), markdown)
            self.assertIn("3: 必須設定", markdown)
            self.assertTrue((root / "research-001" / "reference-research.md").exists())

    async def test_rewrite_publishes_only_after_reference_enrichment_check(self) -> None:
        class EditingAgent:
            name = "fake-editor"

            async def run(self, prompt, schema, *, workdir, **kwargs):
                task = Path(workdir)
                if prompt.kind == "wiki_page_plan":
                    research = (task / "reference-research.md").read_text(encoding="utf-8")
                    self_outer.assertIn("機能の利用前に設定を有効化する", research)
                    (task / "wiki-plan.md").write_text(
                        "# 記事構成\n\n参照調査の必須設定を追加する。\n",
                        encoding="utf-8",
                    )
                else:
                    (task / "page.md").write_text(
                        "# 対象API\n\n対象の説明。\n\n"
                        "機能の利用前に設定を有効化する。"
                        "（参照元: 原文 3-4行）\n",
                        encoding="utf-8",
                    )
                return AgentReply(payload={})

        async def model_behaviour(schema, messages):
            prompt = messages[-1].content
            if schema is ReferenceSelection:
                return ReferenceSelection(page_numbers=[2])
            if schema is ReferenceResearchResult:
                return ReferenceResearchResult(
                    useful_facts=[
                        ReferenceFact(
                            description="機能の利用前に設定を有効化する。",
                            reason="利用条件を説明するため。",
                            insertion_point="前提条件",
                            source_start=3,
                            source_end=4,
                        )
                    ]
                )
            if schema is WikiPlanJudgeResult:
                return WikiPlanJudgeResult(acceptable=True)
            self_outer.assertIs(schema, PageJudgeResult)
            if "--- 参照調査結果 ---" in prompt:
                self_outer.assertIn("（参照元: 原文 3-4行）", prompt)
            return PageJudgeResult(coverage_score=100)

        self_outer = self
        target = pipeline.SeedPage(
            number=1,
            title="対象API",
            chapter="",
            summary="対象の説明",
            owner_ranges=[(1, 2)],
            filename="001-target.md",
            page_id="page-001",
        )
        reference = pipeline.SeedPage(
            number=2,
            title="設定",
            chapter="",
            summary="必須設定",
            owner_ranges=[(3, 4)],
            filename="002-settings.md",
            page_id="page-002",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await pipeline._rewrite_page(
                target,
                pages=[target, reference],
                lines=["対象の説明", "対象の詳細", "必須設定", "有効化が必要"],
                units=[],
                agent=EditingAgent(),
                judge_model=FakeModel(model_behaviour),
                config=pipeline.NeoConfig(
                    rewrite_attempts=1,
                    rewrite_versions=1,
                    reference_attempts=1,
                    judge_attempts=1,
                ),
                work_root=root,
                seed_root=root / "seeds",
                source_line_count=4,
                stop_check=None,
                on_progress=None,
            )

        self.assertEqual(result.page.reference_ranges, [(3, 4)])
        self.assertEqual(result.judge_score, 100)
        self.assertIn("機能の利用前に設定を有効化する", result.markdown)

    async def test_keeps_original_and_selects_repaired_second_version(self) -> None:
        class EditingAgent:
            name = "fake-editor"

            def __init__(self) -> None:
                self.writer_calls = 0

            async def run(self, prompt, schema, *, workdir, **kwargs):
                if prompt.kind == "wiki_page_plan":
                    (Path(workdir) / "wiki-plan.md").write_text(
                        "# 記事構成\n\n重要事項AとBを残す。\n", encoding="utf-8"
                    )
                    return AgentReply(payload={})
                self.writer_calls += 1
                page_path = Path(workdir) / "page.md"
                if self.writer_calls == 1:
                    page_path.write_text("# Wiki\n\n重要事項A\n", encoding="utf-8")
                else:
                    page_path.write_text(
                        "# Wiki\n\n重要事項A\n\n重要事項B\n", encoding="utf-8"
                    )
                return AgentReply(payload={})

        async def judge(schema, messages):
            if schema is WikiPlanJudgeResult:
                return WikiPlanJudgeResult(acceptable=True)
            candidate = messages[-1].content.split("--- Wiki候補 ---", 1)[-1]
            if "重要事項B" in candidate:
                return PageJudgeResult(coverage_score=100)
            return PageJudgeResult(
                coverage_score=60,
                missing_important_information=[
                    ImportantOmission(
                        description="重要事項Bがない",
                        source_start=2,
                        source_end=2,
                    )
                ],
            )

        page = pipeline.SeedPage(
            number=1,
            title="概要",
            chapter="",
            summary="重要事項",
            owner_ranges=[(1, 2)],
            filename="001-概要.md",
            page_id="page-001",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_file = root / "source.md"
            source_file.write_text("重要事項A\n重要事項B\n", encoding="utf-8")
            result = await pipeline._rewrite_page(
                page,
                pages=[page],
                lines=["重要事項A", "重要事項B"],
                units=[],
                agent=EditingAgent(),
                judge_model=FakeModel(judge),
                config=pipeline.NeoConfig(
                    rewrite_attempts=2,
                    rewrite_versions=2,
                    judge_attempts=1,
                ),
                work_root=root,
                seed_root=root / "seeds",
                source_line_count=2,
                stop_check=None,
                on_progress=None,
            )

            self.assertEqual(
                (root / "rewrite-001-attempt-02" / "original.md").read_text(
                    encoding="utf-8"
                ),
                "重要事項A\n重要事項B",
            )

        self.assertEqual(result.selected_version, 2)
        self.assertEqual(result.judge_score, 100)
        self.assertIn("重要事項B", result.markdown)
        self.assertFalse(result.markdown.startswith("---\n"))

    async def test_link_pass_adds_only_inline_links(self) -> None:
        class LinkingAgent:
            name = "fake-linker"

            async def run(self, prompt, schema, *, workdir, **kwargs):
                page_path = Path(workdir) / "page.md"
                page_path.write_text(
                    page_path.read_text(encoding="utf-8").replace(
                        "ets_printf", "[ets_printf](002-ets-printf.md)"
                    ),
                    encoding="utf-8",
                )
                return AgentReply(payload={})

        first = pipeline.SeedPage(
            number=1,
            title="利用方法",
            chapter="",
            summary="呼び出し方",
            owner_ranges=[(1, 1)],
            filename="001-利用方法.md",
            page_id="page-001",
        )
        second = pipeline.SeedPage(
            number=2,
            title="ets_printf",
            chapter="",
            summary="出力関数",
            owner_ranges=[(2, 2)],
            filename="002-ets-printf.md",
            page_id="page-002",
        )
        result = pipeline.RewriteResult(
            page=first,
            markdown="# 利用方法\n\nets_printfを呼び出す。\n",
            attempts=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            (snapshot / first.filename).write_text(result.markdown, encoding="utf-8")
            (snapshot / second.filename).write_text(
                "# ets_printf\n\n出力関数。\n", encoding="utf-8"
            )
            linked = await pipeline._link_page(
                result,
                pages=[first, second],
                units=[],
                agent=LinkingAgent(),
                config=pipeline.NeoConfig(link_attempts=1),
                work_root=root,
                snapshot_root=snapshot,
                stop_check=None,
            )

        self.assertTrue(linked.linked)
        self.assertIn("[ets_printf](002-ets-printf.md)", linked.markdown)


class PiAgentTests(unittest.TestCase):
    def test_prepare_pi_agent_writes_a_64k_isolated_model_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = pipeline.NeoConfig(agent_backend="pi")
            agent = pipeline._prepare_agent(config, FakeModel(whole_window), root, "doc")

            self.assertIsInstance(agent, PiAgent)
            model_config = (root / "pi-profile" / "models.json").read_text(
                encoding="utf-8"
            )
            self.assertIn('"maxTokens": 65536', model_config)
            self.assertEqual(
                agent.environment()["PI_CODING_AGENT_DIR"], str(root / "pi-profile")
            )

    def test_pi_command_is_noninteractive_and_file_capable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt_file = root / "prompt.md"
            prompt_file.write_text("ページを編集する", encoding="utf-8")
            agent = PiAgent(
                pipeline.NeoConfig(agent_backend="pi").pi.model_copy(
                    update={"config_dir": str(root / "config")}
                )
            )

            command = agent.command(
                prompt_file=prompt_file, workdir=root, session="ignored"
            )

            self.assertIn("--no-session", command)
            self.assertIn("--no-context-files", command)
            self.assertIn("read,write,edit,grep,find,ls", command)
            self.assertEqual(command[-2:], ["-p", "ページを編集する"])


class WindowObservationTests(unittest.IsolatedAsyncioTestCase):
    def config(self, **overrides) -> pipeline.NeoConfig:
        values = {
            "window_target_lines": 100,
            "window_overlap_lines": 20,
            "planner_concurrency": 4,
            "planner_attempts": 2,
        }
        values.update(overrides)
        return pipeline.NeoConfig(**values)

    def test_windows_overlap_by_exact_configured_amount(self) -> None:
        self.assertEqual(
            windows.overlapping_windows(250, target=100, overlap=20),
            [(1, 100), (81, 180), (161, 250)],
        )

    def test_inventory_ranges_may_overlap_and_nest(self) -> None:
        inventory = WindowInventory(
            summary="section and function",
            observations=[
                ObservedRange(
                    title="Section", source_start=1, source_end=100
                ),
                ObservedRange(
                    title="Function", source_start=20, source_end=40
                ),
            ],
        )

        checked, error = windows.validate_inventory(
            inventory, source_start=1, source_end=100
        )

        self.assertIsNone(error)
        self.assertEqual(len(checked.observations), 2)

    async def test_observations_are_not_merged_into_chunks(self) -> None:
        result = await windows.observe_document(
            fake_source(), model=FakeModel(whole_window), config=self.config()
        )

        self.assertGreater(len(result.windows), 1)
        self.assertEqual(result.windows[0].source_start, 1)
        self.assertEqual(
            result.windows[1].source_start,
            result.windows[0].source_end - 20 + 1,
        )
        self.assertTrue(all(len(item.observations) == 1 for item in result.windows))

    async def test_invalid_range_is_retried_with_feedback(self) -> None:
        prompts: list[str] = []

        async def invalid_then_valid(schema, messages):
            prompts.append(messages[-1].content)
            start, end = numbered_range(messages)
            if len(prompts) == 1:
                end += 1
            return WindowInventory(
                summary="inventory",
                observations=[
                    ObservedRange(
                        title="item", source_start=start, source_end=end
                    )
                ],
            )

        await windows.observe_document(
            "\n".join(["line"] * 50),
            model=FakeModel(invalid_then_valid),
            config=self.config(window_target_lines=50, window_overlap_lines=10),
        )

        self.assertEqual(len(prompts), 2)
        self.assertIn("検証エラー", prompts[1])
        self.assertIn("must stay inside", prompts[1])

    async def test_failing_observer_keeps_a_mechanical_inventory(self) -> None:
        async def broken(schema, messages):
            raise RuntimeError("provider exploded")

        result = await windows.observe_document(
            fake_source(), model=FakeModel(broken), config=self.config()
        )

        self.assertTrue(result.windows)
        self.assertTrue(all(item.mechanical for item in result.windows))

    async def test_image_bytes_never_enter_an_observation_prompt(self) -> None:
        seen: list[str] = []

        async def watching(schema, messages):
            seen.append(messages[-1].content)
            return await whole_window(schema, messages)

        result = await windows.observe_document(
            fake_source(), model=FakeModel(watching), config=self.config()
        )

        self.assertEqual(len(result.images), 1)
        self.assertTrue(any("[IMAGE img-" in content for content in seen))
        self.assertTrue(all(base64_run() not in content for content in seen))

    async def test_progress_stop_and_live_results(self) -> None:
        events: list[dict] = []
        with tempfile.TemporaryDirectory() as directory:
            live = Path(directory) / "live-observations"
            await windows.observe_document(
                fake_source(),
                model=FakeModel(whole_window),
                config=self.config(),
                live_output_dir=live,
                on_progress=events.append,
            )
            markdown = sorted(live.glob("window-*.md"))
            json_files = sorted(live.glob("window-*.json"))
            self.assertGreater(len(markdown), 1)
            self.assertEqual(len(markdown), len(json_files))
            self.assertIn("Observed ranges", markdown[0].read_text(encoding="utf-8"))

        self.assertEqual(events[-1]["stage"], "observe")
        self.assertGreater(events[-1]["observations"], 0)

        async def never_called(schema, messages):  # pragma: no cover
            raise AssertionError("observer ran after cancellation")

        with self.assertRaises(asyncio.CancelledError):
            await windows.observe_document(
                fake_source(),
                model=FakeModel(never_called),
                config=self.config(),
                stop_check=lambda: True,
            )

    async def test_empty_source_has_no_windows(self) -> None:
        async def never_called(schema, messages):  # pragma: no cover
            raise AssertionError("empty source should make no call")

        result = await windows.observe_document(
            "", model=FakeModel(never_called), config=self.config()
        )
        self.assertEqual(result.windows, [])


class PlanningPromptTests(unittest.TestCase):
    def test_prompts_allow_observation_overlap_and_require_entity_pages(self) -> None:
        inventory = prompts.window_inventory_prompt(
            window_start=1,
            window_end=250,
            block=images.SanitizedSource(text="1: source", line_count=1),
            output_language="Japanese (日本語)",
        ).render()
        semantic = prompts.semantic_plan_prompt(
            source_line_count=500,
            regional_reports="region",
            output_language="Japanese (日本語)",
        ).render()
        compiler = prompts.seed_plan_compile_prompt(
            source_line_count=500,
            semantic_plan="plan",
            regional_reports="region",
            output_language="Japanese (日本語)",
        ).render()

        self.assertIn("観察範囲は重複・包含してよい", inventory)
        self.assertIn("20行以上あるものは独立Wikiページ", semantic)
        self.assertIn("隣のシードへ行を漏らしたり", semantic)
        self.assertIn("全てのページを20行以上", semantic)
        self.assertIn("空行、見出しだけ", compiler)
        self.assertIn("直前と直後の両方", compiler)

    def test_link_prompt_requires_inline_links_only(self) -> None:
        prompt = prompts.link_page_prompt(
            page_title="API",
            working_page_path="/tmp/task/page.md",
            original_path="/tmp/task/original.md",
            page_index_path="/tmp/task/page-index.md",
            output_language="Japanese (日本語)",
        ).render()

        self.assertIn("既存本文中の言及へ直接付ける", prompt)
        self.assertIn("「関連機能」「関連項目」", prompt)
        self.assertIn("追加してはならない", prompt)

    def test_rewrite_prompt_defers_all_navigation_to_link_phase(self) -> None:
        prompt = prompts.simple_page_edit_prompt(
            page_number=1,
            page_title="API",
            final_page_path="001-api.md",
            working_page_path="/tmp/task/page.md",
            plan_path="/tmp/task/wiki-plan.md",
            original_path="/tmp/task/original.md",
            page_index_path="/tmp/task/page-index.md",
            references_path="/tmp/task/references.md",
            owner_ranges="1-20",
            image_context="（このページに画像はない）",
            output_language="Japanese (日本語)",
            reference_research_path="/tmp/task/reference-research.md",
            reference_research="# 参照調査結果\n\nuseful fact 1",
        ).render()

        self.assertIn("後段の専用工程", prompt)
        self.assertIn("新しい内部リンクを追加しない", prompt)
        self.assertIn("「関連ページ」「関連機能」", prompt)
        self.assertIn("原文の整形や言い換えだけで終わらない", prompt)
        self.assertIn("何のための機能か、いつ使うか、どう動くか", prompt)
        self.assertIn("`1-20`は、全原文における出典行番号", prompt)
        self.assertIn("`page.md`内の行番号ではない", prompt)
        self.assertIn("readツールのoffsetに使ったりしない", prompt)
        self.assertIn("必ずこの順番で作業する", prompt)
        self.assertIn("ページタイトルと関係が薄く見える項目も", prompt)
        self.assertIn("編集後の`page.md`を再度1行目から最後まで読み", prompt)
        self.assertIn("調査済みの`wiki-plan.md`に従って", prompt)
        self.assertIn("Pythonが選別した参照調査結果", prompt)
        self.assertIn("useful factを漏らさず", prompt)

    def test_wiki_planner_reads_evidence_and_writes_a_concrete_plan(self) -> None:
        prompt = prompts.wiki_page_plan_prompt(
            page_number=1,
            page_title="API",
            working_page_path="/tmp/task/page.md",
            plan_path="/tmp/task/wiki-plan.md",
            page_index_path="/tmp/task/page-index.md",
            references_path="/tmp/task/references.md",
            owner_ranges="100-120",
            image_context="（このページに画像はない）",
            output_language="Japanese (日本語)",
            reference_research_path="/tmp/task/reference-research.md",
            reference_research="# 参照調査結果\n\nuseful fact 1",
        ).render()

        self.assertIn("`page.md`内の行番号ではない", prompt)
        self.assertIn("readツールのoffsetに使ったりしない", prompt)
        self.assertIn("Pythonが参照ページの全文を比較", prompt)
        self.assertIn("useful factを勝手に「なし」にしない", prompt)
        self.assertIn("/tmp/task/reference-research.md", prompt)
        self.assertIn("# 記事構成", prompt)
        self.assertIn("原文の見出し一覧を言い換えて並べただけ", prompt)
        self.assertIn("読者が判断や作業を進められる順序", prompt)
        self.assertIn("読み取り用シードの絶対パス", prompt)

    def test_judge_rejects_a_merely_cleaned_source_excerpt(self) -> None:
        prompt = prompts.page_judge_prompt(
            page_title="API",
            owner_ranges="1-20",
            numbered_original="1: # API\n2: 説明",
            candidate="# API\n\n説明",
            output_language="Japanese (日本語)",
        ).render()

        self.assertIn("単なる原文の整形・言い換え", prompt)
        self.assertIn("前後の章なしでは理解しにくい場合は未完成", prompt)

    def test_plan_judge_rejects_an_original_outline_copy(self) -> None:
        prompt = prompts.wiki_plan_judge_prompt(
            page_title="API",
            numbered_original="1: # API\n2: 説明",
            reference_research="# 参照調査結果\n\nuseful fact 1",
            wiki_plan="# 記事構成\n\n原文と同じ順序",
            output_language="Japanese (日本語)",
        ).render()

        self.assertIn("原文の見出しを同じ順番で言い換えただけ", prompt)
        self.assertIn("目的、前提、関係", prompt)
        self.assertIn("useful fact", prompt)


class HierarchicalPlanningTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def report(number: int) -> WindowReport:
        start = (number - 1) * 10 + 1
        end = number * 10
        return WindowReport(
            id=f"win-{number}",
            ordinal=number,
            source_start=start,
            source_end=end,
            summary=f"window {number}",
            observations=[
                ObservedRange(
                    title=f"item {number}", source_start=start, source_end=end
                )
            ],
        )

    async def test_regional_planner_processes_ten_windows_at_a_time(self) -> None:
        import re

        async def region(schema, messages):
            match = re.search(r"地域 \d+: 原文 (\d+)-(\d+)行", messages[-1].content)
            start, end = map(int, match.groups())
            return RegionalPlan(
                summary="region",
                pages=[
                    RegionalPage(
                        title="region", scope="region", source_start=start, source_end=end
                    )
                ],
            )

        observations = ObservationSet(
            document_id="doc",
            source_sha256="x",
            normalized_source_sha256="x",
            source_line_count=210,
            windows=[self.report(number) for number in range(1, 22)],
        )
        regions = await document_map._build_regions(
            observations,
            model=FakeModel(region),
            config=pipeline.NeoConfig(regional_window_count=10),
            checkpoint_root=None,
            stop_check=None,
            on_progress=None,
        )

        self.assertEqual(len(regions), 3)
        self.assertEqual([(item.source_start, item.source_end) for item in regions], [(1, 100), (101, 200), (201, 210)])

    async def test_compiler_gets_exact_feedback_and_retries(self) -> None:
        prompts: list[str] = []

        async def compile_plan(schema, messages):
            prompts.append(messages[-1].content)
            if len(prompts) == 1:
                return SeedPlan(
                    pages=[
                        SeedRange(title="A", summary="A", source_start=1, source_end=4),
                        SeedRange(title="B", summary="B", source_start=6, source_end=10),
                    ]
                )
            return SeedPlan(
                pages=[
                    SeedRange(title="A", summary="A", source_start=1, source_end=5),
                    SeedRange(title="B", summary="B", source_start=6, source_end=10),
                ]
            )

        result = await document_map._compile_seed_plan(
            SemanticPlan(plan="A then B"),
            [
                RegionalReport(
                    ordinal=1,
                    source_start=1,
                    source_end=10,
                    summary="region",
                    pages=[
                        RegionalPage(
                            title="A", scope="A", source_start=1, source_end=5
                        ),
                        RegionalPage(
                            title="B", scope="B", source_start=6, source_end=10
                        ),
                    ],
                )
            ],
            lines=["line"] * 10,
            model=FakeModel(compile_plan),
            config=pipeline.NeoConfig(map_attempts=3),
            checkpoint_root=None,
            stop_check=None,
            on_progress=None,
        )

        self.assertEqual(len(prompts), 2)
        self.assertIn("line ownership gap", prompts[1])
        self.assertIn("no line may leak", prompts[1])
        self.assertEqual([(p.source_start, p.source_end) for p in result.pages], [(1, 5), (6, 10)])

    async def test_compiler_recovers_a_previous_response_without_another_call(self) -> None:
        semantic = SemanticPlan(plan="A then B")
        regions = [
            RegionalReport(
                ordinal=1,
                source_start=1,
                source_end=5,
                summary="region",
                pages=[
                    RegionalPage(
                        title="A", scope="A", source_start=1, source_end=2
                    ),
                    RegionalPage(
                        title="B", scope="B", source_start=3, source_end=5
                    ),
                ],
            )
        ]
        raw = SeedPlan(
            pages=[
                SeedRange(title="A", summary="A", source_start=1, source_end=2),
                SeedRange(title="B", summary="B", source_start=3, source_end=5),
            ]
        )

        async def first(schema, messages):
            return raw

        async def never(schema, messages):  # pragma: no cover
            raise AssertionError("a valid prior compiler response should be recovered")

        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory)
            config = pipeline.NeoConfig(map_attempts=2)
            await document_map._compile_seed_plan(
                semantic,
                regions,
                lines=["before", "```", "code", "```", "after"],
                model=FakeModel(first),
                config=config,
                checkpoint_root=checkpoint,
                stop_check=None,
                on_progress=None,
            )
            next(checkpoint.glob("compile/*/result.json")).unlink()
            recovered = await document_map._compile_seed_plan(
                semantic,
                regions,
                lines=["before", "```", "code", "```", "after"],
                model=FakeModel(never),
                config=config,
                checkpoint_root=checkpoint,
                stop_check=None,
                on_progress=None,
            )

        self.assertEqual(
            [(page.source_start, page.source_end) for page in recovered.pages],
            [(1, 1), (2, 5)],
        )

class ImageSafetyTests(unittest.TestCase):
    def test_payload_is_scrubbed_from_prompt_text(self) -> None:
        lines = fake_source().splitlines()
        units = images.extract_image_units(lines)
        self.assertEqual(len(units), 1)

        prompt = images.numbered_prompt_block(
            lines, units, units[0].source_start, units[0].source_end
        )
        self.assertNotIn(base64_run(), prompt.text)
        self.assertIn("[IMAGE img-", prompt.text)
        self.assertIn(units[0].image_id, prompt.text)

    def test_raw_unit_bytes_are_untouched(self) -> None:
        lines = fake_source().splitlines()
        unit = images.extract_image_units(lines)[0]
        self.assertIn(base64_run(), unit.raw)
        self.assertEqual(images.count_units(unit.raw, [unit])[unit.image_id], 1)
        self.assertTrue(images.unit_intact(unit.raw, unit))
        self.assertEqual(images.scrub_base64(unit.raw).count(base64_run()), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
