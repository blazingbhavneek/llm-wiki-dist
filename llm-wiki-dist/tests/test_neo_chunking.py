"""Stage A: atomic-block awareness, exact coverage, and honest fallbacks."""

from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence

from graph.neo import document_map, images, markdown_blocks, pipeline, prompts, windows
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
    RegionalPage,
    RegionalPlan,
    SeedPlan,
    SeedRange,
    SemanticPlan,
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

    async def text(self, messages, *, max_output_tokens=None) -> str:
        self.calls.append("text")
        return await self.behaviour(None, messages)


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

class SectionWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_research_inlines_full_seeds_and_keeps_provenance(self) -> None:
        async def research(schema, messages):
            prompt = messages[-1].content
            self.assertIs(schema, ReferenceResearchResult)
            self.assertIn("1: 対象の説明", prompt)
            self.assertIn("3: 必須設定", prompt)
            self.assertIn("4: 有効化が必要", prompt)
            return ReferenceResearchResult(
                useful_facts=[
                    ReferenceFact(
                        description="機能の利用前に設定を有効化する。",
                        reason="利用条件を説明するため。",
                        source_start=3,
                        source_end=4,
                        target_line=99,  # outside the target page: clamped to 0
                    )
                ]
            )

        target = pipeline.SeedPage(
            number=1, title="対象API", chapter="", summary="対象の説明",
            owner_ranges=[(1, 2)], filename="001-target.md", page_id="page-001",
        )
        reference = pipeline.SeedPage(
            number=2, title="設定", chapter="", summary="必須設定",
            owner_ranges=[(3, 4)], filename="002-settings.md", page_id="page-002",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence, markdown = await pipeline._research_references(
                target,
                pages=[target, reference],
                lines=["対象の説明", "対象の詳細", "必須設定", "有効化が必要"],
                units=[],
                tokens={1: set(), 2: set()},
                model=FakeModel(research),
                config=pipeline.NeoConfig(reference_attempts=1),
                work_root=root,
                seed_root=root / "seeds",
                stop_check=None,
                on_progress=None,
            )

            facts = [fact for item in evidence for fact in item.facts]
            self.assertEqual([(f.source_start, f.source_end) for f in facts], [(3, 4)])
            self.assertEqual(facts[0].target_line, 0)
            self.assertIn(str((root / "seeds" / "002-settings.md").resolve()), markdown)
            self.assertIn("3: 必須設定", markdown)
            self.assertTrue((root / "research-001" / "reference-research.md").exists())

    def test_reference_selection_is_adjacent_plus_lexical_overlap(self) -> None:
        def seed(number: int) -> pipeline.SeedPage:
            return pipeline.SeedPage(
                number=number, title=f"p{number}", chapter="", summary="",
                owner_ranges=[(number, number)], filename=f"{number:03d}.md",
                page_id=f"page-{number:03d}",
            )

        pages = [seed(n) for n in range(1, 8)]
        tokens = {n: {"共通語"} for n in range(1, 8)}
        tokens[4] |= {"mpf_open", "ファイル"}
        tokens[1] |= {"mpf_open", "ファイル"}
        tokens[7] |= {"mpf_open"}
        tokens[6] |= {"無関係"}
        chosen = pipeline._select_references(pages[3], pages, tokens, limit=2)
        self.assertEqual([item.number for item in chosen], [1, 3, 5, 7])

    @staticmethod
    def lines() -> list[str]:
        return [
            "# 対象API", "", "mpf_open は開く。", "", "```c", "int mpf_open(int filenum);", "```",
            "", "| 引数 | 意味 |", "|---|---|", "| filenum | 番号 |", "",
            "注意:", "E_TIMEOUT が返ることがある。", "続き。",
            "# 設定", "有効化が必要。", "設定の詳細。",
        ]

    @staticmethod
    def pages() -> list[pipeline.SeedPage]:
        return [
            pipeline.SeedPage(number=1, title="対象API", chapter="", summary="開く",
                              owner_ranges=[(1, 15)], filename="001-api.md", page_id="page-001"),
            pipeline.SeedPage(number=2, title="設定", chapter="", summary="設定",
                              owner_ranges=[(16, 18)], filename="002-settings.md", page_id="page-002"),
        ]

    async def test_lost_code_block_is_fed_back_and_final_page_is_lossless(self) -> None:
        prompts_seen: list[str] = []

        async def behaviour(schema, messages):
            prompt = messages[-1].content
            if schema is ReferenceResearchResult:
                return ReferenceResearchResult(useful_facts=[ReferenceFact(
                    description="有効化が必要。", reason="前提", source_start=17,
                    source_end=17, target_line=3)])
            if schema is PageJudgeResult:
                return PageJudgeResult(coverage_score=100)
            if "導入文" in prompt:
                return "このページは mpf_open の使い方を説明する。"
            prompts_seen.append(prompt)
            if "前回の出力の不足" not in prompt:
                return "## 対象API\n\nmpf_open は開く。設定の有効化が必要。（参照元: 原文 17-17行）\n\n| 引数 | 意味 |\n|---|---|\n| filenum | 番号 |\n|\n\n## 注意\nE_TIMEOUT が返ることがある。続き。\n"
            return ("## 対象API\n\nmpf_open は開く。設定の有効化が必要。（参照元: 原文 17-17行）\n\n```c\nint mpf_open(int filenum);\n```\n\n"
                    "| 引数 | 意味 |\n|---|---|\n| filenum | 番号 |\n\n## 注意\nE_TIMEOUT が返ることがある。続き。\n")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await pipeline._rewrite_page(
                self.pages()[0], pages=self.pages(), lines=self.lines(), units=[],
                tokens={1: set(), 2: set()}, model=FakeModel(behaviour),
                config=pipeline.NeoConfig(write_attempts=3, section_target_lines=80, section_min_lines=1),
                work_root=root, seed_root=root / "seeds", source_line_count=18,
                stop_check=None, on_progress=None,
            )

        self.assertEqual(len(prompts_seen), 2)
        self.assertIn("コードブロック", prompts_seen[1])
        self.assertIn("int mpf_open(int filenum);", result.markdown)
        self.assertTrue(result.markdown.startswith("# 対象API\n\nこのページは"))
        self.assertIn("[設定](002-settings.md)の有効化が必要", result.markdown)
        self.assertIn("次のページ: [設定](002-settings.md)", result.markdown)
        self.assertEqual(result.verbatim_sections, [])
        self.assertEqual(result.page.reference_ranges, [(17, 17)])

    async def test_section_that_never_passes_is_published_verbatim_and_flagged(self) -> None:
        async def behaviour(schema, messages):
            if schema is ReferenceResearchResult:
                return ReferenceResearchResult(no_useful_information_reason="なし")
            if schema is PageJudgeResult:
                return PageJudgeResult(coverage_score=100)
            return "## 対象API\n\n何も書かない。\n"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await pipeline._rewrite_page(
                self.pages()[0], pages=self.pages(), lines=self.lines(), units=[],
                tokens={1: set(), 2: set()}, model=FakeModel(behaviour),
                config=pipeline.NeoConfig(write_attempts=2, section_min_lines=1),
                work_root=root, seed_root=root / "seeds", source_line_count=18,
                stop_check=None, on_progress=None,
            )

        self.assertEqual(len(result.verbatim_sections), 1)
        self.assertIn("mpf_open", result.verbatim_sections[0])
        self.assertIn("int mpf_open(int filenum);", result.markdown)
        self.assertIn("| filenum | 番号 |", result.markdown)

    async def test_judge_omissions_are_fed_back(self) -> None:
        judged = 0
        prompts_seen: list[str] = []

        async def behaviour(schema, messages):
            nonlocal judged
            prompt = messages[-1].content
            if schema is ReferenceResearchResult:
                return ReferenceResearchResult(no_useful_information_reason="なし")
            if schema is PageJudgeResult:
                judged += 1
                if judged == 1:
                    return PageJudgeResult(coverage_score=60, missing_important_information=[
                        ImportantOmission(description="タイムアウトの条件", source_start=14, source_end=14)])
                return PageJudgeResult(coverage_score=100)
            if "導入文" in prompt:
                return "導入。"
            prompts_seen.append(prompt)
            return ("## 対象API\n\nmpf_open は開く。\n\n```c\nint mpf_open(int filenum);\n```\n\n"
                    "| 引数 | 意味 |\n|---|---|\n| filenum | 番号 |\n\n## 注意\nE_TIMEOUT が返ることがある。続き。\n")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await pipeline._rewrite_page(
                self.pages()[0], pages=self.pages(), lines=self.lines(), units=[],
                tokens={1: set(), 2: set()}, model=FakeModel(behaviour),
                config=pipeline.NeoConfig(write_attempts=3, section_min_lines=1),
                work_root=root, seed_root=root / "seeds", source_line_count=18,
                stop_check=None, on_progress=None,
            )

        self.assertEqual(len(prompts_seen), 2)
        self.assertIn("タイムアウトの条件", prompts_seen[1])
        self.assertEqual(result.judge_score, 100)
        self.assertEqual(result.missing_important_information, [])

    async def test_run_pipeline_end_to_end_with_fake_model(self) -> None:
        async def behaviour(schema, messages):
            prompt = messages[-1].content
            if schema is WindowInventory:
                return await whole_window(schema, messages)
            if schema is RegionalPlan:
                match = re.search(r"地域 \d+: 原文 (\d+)-(\d+)行", prompt)
                start, end = map(int, match.groups())
                return RegionalPlan(pages=[RegionalPage(title="全体", scope="all", source_start=start, source_end=end)])
            if schema is SemanticPlan:
                return SemanticPlan(plan="one page")
            if schema is SeedPlan:
                end = int(re.search(r"原文は1-(\d+)行である", prompt).group(1))
                return SeedPlan(pages=[SeedRange(title="全体", summary="all", source_start=1, source_end=end)])
            if schema is ReferenceResearchResult:
                return ReferenceResearchResult(no_useful_information_reason="なし")
            if schema is PageJudgeResult:
                return PageJudgeResult(coverage_score=100)
            if "導入文" in prompt:
                return "導入。"
            body = prompt.split("--- 行番号付き原文（この節） ---\n", 1)[1]
            return "\n".join(line.split(": ", 1)[1] if ": " in line else line for line in body.splitlines())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "doc.md"
            source.write_text(fake_source(), encoding="utf-8")
            config = pipeline.NeoConfig(output_root=str(root / "out"), planner_attempts=1, map_attempts=1)
            run_root = await pipeline.run_pipeline(source, config=config, model=FakeModel(behaviour))

            wiki = run_root / "wiki"
            pages = sorted(p.name for p in wiki.glob("0*.md"))
            self.assertTrue(pages)
            self.assertTrue((wiki / "index.md").exists())
            self.assertFalse((wiki / "_review.md").exists())
            text = (wiki / pages[0]).read_text(encoding="utf-8")
            self.assertIn("def function_1(): ...", text)
            self.assertIn(base64_run(), text)
            self.assertNotIn("[[NEO-IMAGE:", text)


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
