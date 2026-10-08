from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common.storage import read_json, sha256_text, write_json_atomic
from graph.fast.model import (
    FastRoleGate,
    JUDGE_MAX_OUTPUT_TOKENS,
    model_pair,
)
from graph.fast.passes import bounded_map
from graph.fast.repair import (
    FastRepair,
    MAX_REWRITE_ATTEMPTS,
    RepairJudgeResult,
    _feedback,
    _judge_rank,
    _new_repair_session,
    _write_session_checkpoint,
)
from graph.workspace.project import Project
from graph.wiki.config import WikiConfig
from runner.cli import build_parser


IMAGE = (
    '<image-unit><image-media><img src="data:image/png;base64,aGVsbG8=" />'
    '></image-media><image-description>图片中的坏说明</image-description></image-unit>'
)


def settings(root: Path, *, concurrency: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        policy="fast",
        data_root=str(root.parent),
        target_name=root.name,
        mount_path=str(root / "mount"),
        chat_base_url="http://example.invalid/v1",
        chat_api_key="test",
        chat_model="test-model",
        concurrency=concurrency,
        wiki_rewrite_concurrency=concurrency,
        wiki_output_language="Japanese (日本語)",
        wiki_section_target_lines=80,
        wiki_write_attempts=3,
        wiki_planner_concurrency=concurrency,
        wiki_request_timeout=30,
        skip_excel_and_small=False,
        structure_target_lines=250,
        structure_min_lines=40,
        pdf_use_headings=False,
        growi_url="",
    )


def make_document(
    root: Path,
    pages: list[tuple[str, str, tuple[int, int]]],
    source_lines: list[str],
) -> tuple[Project, str, Path]:
    project = Project(root, root / "mount").ensure()
    project.mount.mkdir(parents=True, exist_ok=True)
    raw_rel = "team/document_pdf.md"
    raw = project.raw_file(raw_rel)
    raw.parent.mkdir(parents=True, exist_ok=True)
    source = "\n".join(source_lines) + "\n"
    raw.write_text(source, encoding="utf-8")
    state_root = project.state_dir(raw_rel)
    (state_root / "source").mkdir(parents=True, exist_ok=True)
    (state_root / "state" / "pages").mkdir(parents=True, exist_ok=True)
    (state_root / "wiki").mkdir(parents=True, exist_ok=True)
    (state_root / "source" / "original.md").write_text(source, encoding="utf-8")
    source_hash = sha256_text(source)
    plan_pages = []
    manifest_pages = []
    for number, (title, body, owned) in enumerate(pages, start=1):
        filename = f"{number:03d}-page-{number}.md"
        path = state_root / "wiki" / filename
        path.write_text(body, encoding="utf-8")
        page = {
            "number": number,
            "title": title,
            "chapter": "test",
            "summary": "",
            "path": [title],
            "filename": filename,
            "owner_ranges": [list(owned)],
            "reference_ranges": [],
        }
        plan_pages.append(page)
        manifest_pages.append({**page, "status": "rewritten"})
        write_json_atomic(state_root / "state" / "pages" / f"{number:03d}.json", {
            "number": number,
            "title": title,
            "filename": filename,
            "status": "rewritten",
            "rewrite_version": "existing-writer",
            "source_ranges": [list(owned)],
            "reference_ranges": [],
            "provenance": {"source_document": {"sha256": source_hash}},
            "content_sha256": sha256_text(body),
        })
    plan = {
        "source": str(raw),
        "source_snapshot": str(state_root / "source" / "original.md"),
        "source_sha256": source_hash,
        "source_line_count": len(source_lines),
        "prompt_version": "test",
        "pages": plan_pages,
    }
    write_json_atomic(state_root / "state" / "plan.json", plan)
    write_json_atomic(state_root / "state" / "manifest.json", {
        "source": str(raw), "source_sha256": source_hash, "pages": manifest_pages,
    })
    write_json_atomic(state_root / "state" / "run.json", {"published": True})
    return project, raw_rel, state_root


class RepairingModel:
    def __init__(self) -> None:
        self.writer_messages: list[str] = []
        self.judge_messages: list[str] = []
        self.writer_calls = 0

    async def structured(self, _schema, messages, **_kwargs):
        body = messages[-1].content
        self.judge_messages.append(body)
        title = body.split("現在の候補タイトル: ", 1)[1].splitlines()[0]
        if title == "项目内部标题":
            return RepairJudgeResult(
                approved=False,
                coverage_score=80,
                title_defects=["中国語の内部タイトルである"],
                body_defects=["同じ文を繰り返している"],
            )
        return RepairJudgeResult(approved=True, coverage_score=100)

    async def text(self, messages, **_kwargs):
        self.writer_calls += 1
        self.writer_messages.append(messages[-1].content)
        return "# 正しい機能\n\n## 説明\n\n正常な説明。\n\n[[WIKI_IMAGE_001]]\n"


class FastRepairTest(unittest.TestCase):
    def test_cli_repair_is_fast_operation_with_limit(self):
        args = build_parser().parse_args(["sync", "--fast", "--repair", "--limit", "7", "a.pdf"])
        self.assertTrue(args.fast_repair)
        self.assertEqual(args.limit, 7)
        self.assertEqual(args.items, ["a.pdf"])
        resumed = build_parser().parse_args(
            ["sync", "--fast", "--repair", "--force", "--continue"]
        )
        self.assertTrue(resumed.force)
        self.assertTrue(resumed.continue_run)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["sync", "--fast", "--repair", "--link"])

    def test_title_body_and_links_are_repaired_while_images_stay_opaque(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            page_one = (
                "# 项目内部标题\n\n同じ異常文章です。\n同じ異常文章です。\n同じ異常文章です。\n\n"
                + IMAGE
                + "\n\n---\n\n次のページ: [第二ページ](002-page-2.md)\n"
            )
            page_two = (
                "# 第二ページ\n\n[古いタイトル](001-page-1.md)を参照する。\n\n"
                "---\n\n前のページ: [古いタイトル](001-page-1.md)\n"
            )
            project, raw_rel, state_root = make_document(
                root,
                [
                    ("项目内部标题", page_one, (1, 3)),
                    ("第二ページ", page_two, (4, 5)),
                ],
                ["# 正しい機能", "正常な説明。", IMAGE, "# 第二ページ", "参照説明。"],
            )
            model = RepairingModel()
            repair = FastRepair(settings(root), project=project, model=model)
            result = repair.repair_document(raw_rel, promote=False)

            self.assertEqual(result["repaired"], 1)
            repaired = (state_root / "wiki" / "001-page-1.md").read_text(encoding="utf-8")
            self.assertTrue(repaired.startswith("# 正しい機能\n"))
            self.assertIn(IMAGE, repaired)
            self.assertIn("次のページ: [第二ページ](002-page-2.md)", repaired)
            sibling = (state_root / "wiki" / "002-page-2.md").read_text(encoding="utf-8")
            self.assertIn("[正しい機能](001-page-1.md)", sibling)
            self.assertNotIn("aGVsbG8=", "\n".join(model.judge_messages + model.writer_messages))
            self.assertNotIn("图片中的坏说明", "\n".join(model.judge_messages + model.writer_messages))
            self.assertIn("[[SOURCE_IMAGE_", model.judge_messages[0])
            self.assertIn("[[WIKI_IMAGE_001]]", model.judge_messages[0])
            self.assertIn("タイトル: 中国語の内部タイトルである", model.writer_messages[0])
            state = read_json(state_root / "state" / "pages" / "001.json")
            self.assertEqual(state["rewrite_version"], "existing-writer")
            self.assertEqual(state["repair_status"], "repaired")
            repaired_plan = read_json(state_root / "state" / "plan.json")["pages"][0]
            self.assertEqual(repaired_plan["title"], "正しい機能")
            self.assertEqual(repaired_plan["path"], ["正しい機能"])

    def test_clean_page_is_not_reformatted(self):
        class CleanModel:
            async def structured(self, _schema, _messages, **_kwargs):
                return RepairJudgeResult(approved=True, coverage_score=100)

            async def text(self, _messages, **_kwargs):
                raise AssertionError("clean pages must not call the writer")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            original = "# ページ\n\n\n説明。\n\n"
            project, raw_rel, state_root = make_document(
                root, [("ページ", original, (1, 1))], ["説明。"]
            )
            result = FastRepair(
                settings(root), project=project, model=CleanModel()
            ).repair_document(raw_rel, promote=False)

            self.assertEqual(result["clean"], 1)
            self.assertEqual(
                (state_root / "wiki" / "001-page-1.md").read_text(encoding="utf-8"),
                original,
            )

    def test_continue_routes_unjudged_to_judge_and_rejected_to_writer(self):
        class ResumeModel:
            def __init__(self):
                self.roles = []

            async def structured(self, _schema, messages, **_kwargs):
                prompt = messages[-1].content
                page = 2 if "ページ番号: 2" in prompt else 1
                self.roles.append(("judge", page))
                return RepairJudgeResult(approved=True, coverage_score=100)

            async def text(self, _messages, **_kwargs):
                self.roles.append(("writer", 1))
                return "# ページ1\n\n正常な説明。\n"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            project, raw_rel, state_root = make_document(
                root,
                [
                    ("ページ1", "# ページ1\n\n壊れた説明。\n", (1, 1)),
                    ("ページ2", "# ページ2\n\n正常な説明。\n", (2, 2)),
                ],
                ["正常な説明。", "正常な説明。"],
            )
            setup = FastRepair(settings(root), project=project, model=ResumeModel())
            loaded_root, _plan, _manifest, pages, _titles = setup._load_document(
                raw_rel, force=False, limit=None
            )

            rejected = _new_repair_session(pages[0])
            verdict = RepairJudgeResult(
                approved=False,
                coverage_score=80,
                body_defects=["修復が必要"],
            )
            rejected.feedback = _feedback(verdict)
            rejected.feedback_history = list(rejected.feedback)
            rejected.best_feedback = list(rejected.feedback)
            rejected.best_verdict = verdict
            rejected.best_rank = _judge_rank(
                verdict, mechanical_issues=0, attempt=0
            )
            _write_session_checkpoint(
                loaded_root,
                setup.version,
                rejected,
                stage="writer",
                model_name=setup.model_name,
                model_provider=setup.model_provider,
            )
            _write_session_checkpoint(
                loaded_root,
                setup.version,
                _new_repair_session(pages[1]),
                stage="initial-judge",
                model_name=setup.model_name,
                model_provider=setup.model_provider,
            )

            checkpoints = [
                (state_root / "state" / "pages" / f"{number:03d}.json")
                .relative_to(project.root)
                .as_posix()
                for number in (1, 2)
            ]
            model = ResumeModel()
            result = FastRepair(
                settings(root),
                project=project,
                model=model,
                resume_page_states=checkpoints,
            ).repair_document(raw_rel, force=True, promote=False)

            self.assertEqual(model.roles[0], ("judge", 2))
            self.assertEqual(model.roles[1], ("writer", 1))
            self.assertEqual(model.roles[2], ("judge", 1))
            self.assertEqual(result["clean"], 1)
            self.assertEqual(result["repaired"], 1)

    def test_each_judge_rejection_is_returned_to_the_same_page_task(self):
        class FourRoundModel:
            def __init__(self):
                self.judge_calls = 0
                self.writer_messages: list[str] = []

            async def structured(self, _schema, _messages, **_kwargs):
                self.judge_calls += 1
                if self.judge_calls < 5:
                    return RepairJudgeResult(
                        approved=False,
                        coverage_score=90,
                        body_defects=[f"欠陥{self.judge_calls}"],
                    )
                return RepairJudgeResult(approved=True, coverage_score=100)

            async def text(self, messages, **_kwargs):
                self.writer_messages.append(messages[-1].content)
                return f"# ページ\n\n正常な説明。候補{len(self.writer_messages)}\n"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            project, raw_rel, _state_root = make_document(
                root,
                [("ページ", "# ページ\n\n壊れた説明。\n", (1, 1))],
                ["正常な説明。"],
            )
            model = FourRoundModel()
            result = FastRepair(
                settings(root), project=project, model=model
            ).repair_document(raw_rel, promote=False)

            self.assertEqual(result["repaired"], 1)
            self.assertEqual(model.judge_calls, 5)
            self.assertEqual(len(model.writer_messages), MAX_REWRITE_ATTEMPTS)
            for attempt, message in enumerate(model.writer_messages, start=1):
                self.assertIn(f"本文: 欠陥{attempt}", message)
            self.assertIn("--- 直前の修復候補", model.writer_messages[1])
            self.assertIn("正常な説明。候補1", model.writer_messages[1])
            self.assertIn("それ以前の指摘", model.writer_messages[1])
            self.assertIn("本文: 欠陥1", model.writer_messages[1])

    def test_malformed_writer_candidate_is_retried_and_sibling_pages_continue(self):
        class MalformedWriterModel:
            def __init__(self):
                self.writer_calls = 0

            async def structured(self, _schema, messages, **_kwargs):
                prompt = messages[-1].content
                if "ページ番号: 1" in prompt:
                    return RepairJudgeResult(
                        approved=False,
                        coverage_score=90,
                        body_defects=["修復が必要"],
                    )
                return RepairJudgeResult(approved=True, coverage_score=100)

            async def text(self, messages, **_kwargs):
                self.writer_calls += 1
                if "ページ番号: 1" in messages[-1].content:
                    return "# ページ1\n\n候補本文。\n\n# 余分なH1\n"
                raise AssertionError("clean sibling pages must not call the writer")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            project, raw_rel, _state_root = make_document(
                root,
                [
                    ("ページ1", "# ページ1\n\n壊れた説明。\n", (1, 1)),
                    ("ページ2", "# ページ2\n\n正常な説明。\n", (2, 2)),
                ],
                ["壊れた説明。", "正常な説明。"],
            )
            model = MalformedWriterModel()
            result = FastRepair(
                settings(root, concurrency=2), project=project, model=model
            ).repair_document(raw_rel, promote=False)

            self.assertEqual(result["review"], 1)
            self.assertEqual(result["clean"], 1)
            self.assertEqual(model.writer_calls, MAX_REWRITE_ATTEMPTS)

    def test_unexpected_page_error_is_checkpointed_and_reported_without_stopping_siblings(self):
        class CleanModel:
            async def structured(self, _schema, _messages, **_kwargs):
                return RepairJudgeResult(approved=True, coverage_score=100)

            async def text(self, _messages, **_kwargs):
                raise AssertionError("clean pages must not call the writer")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            project, raw_rel, state_root = make_document(
                root,
                [
                    ("ページ1", "# ページ1\n\n壊れた説明。\n", (1, 1)),
                    ("ページ2", "# ページ2\n\n正常な説明。\n", (2, 2)),
                ],
                ["壊れた説明。", "正常な説明。"],
            )
            repair = FastRepair(
                settings(root, concurrency=2), project=project, model=CleanModel()
            )
            base_policy = repair.policy

            class SelectivePolicy:
                name = base_policy.name

                def code_tokens(self, text):
                    if "ページ1" in text:
                        raise RuntimeError("injected page-local guard failure")
                    return base_policy.code_tokens(text)

            repair.policy = SelectivePolicy()
            result = repair.repair_document(raw_rel, promote=False)

            self.assertEqual(result["review"], 1)
            self.assertEqual(result["clean"], 1)
            self.assertEqual(len(result["failures"]), 1)
            self.assertIn("injected page-local guard failure", result["failures"][0])
            self.assertEqual(
                read_json(state_root / "state" / "pages" / "001.json")["repair_status"],
                "review",
            )

    def test_best_scored_safe_rewrite_is_kept_for_review(self):
        class ScoredModel:
            def __init__(self):
                self.judge_calls = 0
                self.writer_calls = 0
                self.scores = [40, 70, 94, 80, 85]

            async def structured(self, _schema, _messages, **_kwargs):
                score = self.scores[self.judge_calls]
                self.judge_calls += 1
                return RepairJudgeResult(
                    approved=False,
                    coverage_score=score,
                    body_defects=[f"残る欠陥{self.judge_calls}"],
                )

            async def text(self, _messages, **_kwargs):
                self.writer_calls += 1
                return f"# ページ\n\n必要な仕様。候補{self.writer_calls}\n"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            original = "# ページ\n\n壊れた説明。\n"
            project, raw_rel, state_root = make_document(
                root, [("ページ", original, (1, 1))], ["必要な仕様。"]
            )
            result = FastRepair(
                settings(root), project=project, model=ScoredModel()
            ).repair_document(raw_rel, promote=False)

            self.assertEqual(result["review"], 1)
            selected = (state_root / "wiki" / "001-page-1.md").read_text(encoding="utf-8")
            self.assertIn("必要な仕様。候補2", selected)
            state = read_json(state_root / "state" / "pages" / "001.json")
            self.assertEqual(state["repair_status"], "review")
            self.assertEqual(state["repair_selected_attempt"], 2)
            self.assertEqual(state["repair_selection"], "best-judged")
            self.assertEqual(state["judge_score"], 94)

    def test_four_failed_rewrites_keep_original_and_create_review(self):
        class RejectingModel:
            def __init__(self):
                self.writer_calls = 0

            async def structured(self, _schema, _messages, **_kwargs):
                return RepairJudgeResult(
                    approved=False, coverage_score=50, body_defects=["壊れている"]
                )

            async def text(self, _messages, **_kwargs):
                self.writer_calls += 1
                return "# 修復候補\n\n画像タグを落とした本文。\n"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            original = "# 壊れたページ\n\n本文。\n\n" + IMAGE + "\n"
            project, raw_rel, state_root = make_document(
                root, [("壊れたページ", original, (1, 2))], ["# 原文", IMAGE]
            )
            model = RejectingModel()
            repair = FastRepair(settings(root), project=project, model=model)
            result = repair.repair_document(raw_rel, promote=False)

            self.assertEqual(model.writer_calls, MAX_REWRITE_ATTEMPTS)
            self.assertEqual(result["review"], 1)
            self.assertEqual((state_root / "wiki" / "001-page-1.md").read_text(encoding="utf-8"), original)
            self.assertIn("001-page-1.md", (state_root / "wiki" / "_review.md").read_text(encoding="utf-8"))
            self.assertEqual(
                read_json(state_root / "state" / "pages" / "001.json")["repair_status"],
                "review",
            )

    def test_configured_concurrency_runs_multiple_page_tasks(self):
        class SlowCleanModel:
            def __init__(self):
                self.active = 0
                self.maximum = 0

            async def structured(self, _schema, _messages, **_kwargs):
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                await asyncio.sleep(0.03)
                self.active -= 1
                return RepairJudgeResult(approved=True, coverage_score=100)

            async def text(self, _messages, **_kwargs):
                raise AssertionError("clean pages must not call the writer")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            pages = []
            source = []
            for number in range(1, 5):
                source.append(f"## 節{number}")
                pages.append((f"ページ{number}", f"# ページ{number}\n\n## 節{number}\n", (number, number)))
            project, raw_rel, _state_root = make_document(root, pages, source)
            model = SlowCleanModel()
            repair = FastRepair(settings(root, concurrency=3), project=project, model=model)
            result = repair.repair_document(raw_rel, promote=False)

            self.assertEqual(result["clean"], 4)
            self.assertEqual(model.maximum, 3)

    def test_repair_batches_writer_rounds_between_judge_rounds(self):
        class RoundModel:
            def __init__(self):
                self.active = 0
                self.maximum = 0
                self.phase_role = None
                self.phase_count = 0
                self.phase_sizes = []
                self.judge_calls = 0

            async def _enter(self, role):
                if self.active == 0:
                    self.phase_role = role
                    self.phase_count = 0
                elif self.phase_role != role:
                    raise AssertionError("writer and judge overlapped")
                self.active += 1
                self.phase_count += 1
                self.maximum = max(self.maximum, self.active)

            async def _leave(self):
                self.active -= 1
                if self.active == 0:
                    self.phase_sizes.append((self.phase_role, self.phase_count))
                    self.phase_role = None

            async def structured(self, _schema, messages, **_kwargs):
                await self._enter("judge")
                try:
                    await asyncio.sleep(0.005)
                    self.judge_calls += 1
                    if "現在候補と同一" in messages[-1].content:
                        return RepairJudgeResult(
                            approved=False,
                            coverage_score=80,
                            body_defects=["修復が必要"],
                        )
                    return RepairJudgeResult(approved=True, coverage_score=100)
                finally:
                    await self._leave()

            async def text(self, _messages, **_kwargs):
                await self._enter("writer")
                try:
                    await asyncio.sleep(0.005)
                    return "# 修復済み\n\n正常な説明。\n"
                finally:
                    await self._leave()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            pages = [
                (f"ページ{number}", f"# ページ{number}\n\n正常な説明。\n", (number, number))
                for number in range(1, 4)
            ]
            project, raw_rel, _state_root = make_document(
                root, pages, ["正常な説明。"] * 3
            )
            model = RoundModel()
            result = FastRepair(
                settings(root, concurrency=2), project=project, model=model
            ).repair_document(raw_rel, promote=False)

            self.assertEqual(result["repaired"], 3)
            self.assertEqual(model.maximum, 2)
            self.assertEqual(
                sum(size for role, size in model.phase_sizes if role == "writer"),
                3,
            )

    def test_bounded_pass_refills_each_finished_slot(self):
        async def run():
            first_five_started = asyncio.Event()
            release_first = asyncio.Event()
            release_slow = asyncio.Event()
            sixth_started = asyncio.Event()
            started = 0
            active = 0
            maximum = 0

            async def operation(number):
                nonlocal started, active, maximum
                started += 1
                active += 1
                maximum = max(maximum, active)
                if started == 5:
                    first_five_started.set()
                try:
                    if number == 0:
                        await release_first.wait()
                    elif number < 5:
                        await release_slow.wait()
                    else:
                        sixth_started.set()
                finally:
                    active -= 1

            task = asyncio.create_task(bounded_map(list(range(6)), 5, operation))
            await first_five_started.wait()
            release_first.set()
            await asyncio.wait_for(sixth_started.wait(), timeout=0.2)
            self.assertFalse(release_slow.is_set())
            release_slow.set()
            await task
            return maximum

        self.assertEqual(asyncio.run(run()), 5)

    def test_source_hash_mismatch_refuses_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            project, raw_rel, _state_root = make_document(
                root, [("ページ", "# ページ\n\n本文。\n", (1, 1))], ["本文。"]
            )
            project.raw_file(raw_rel).write_text("changed\n", encoding="utf-8")
            repair = FastRepair(settings(root), project=project, model=RepairingModel())
            with self.assertRaisesRegex(RuntimeError, "normal sync"):
                repair.repair_document(raw_rel, promote=False)

    def test_fast_model_pair_has_separate_roles_and_shared_gate(self):
        config = WikiConfig(
            chat_base_url="http://judge/v1",
            chat_model="large-judge",
            writer_base_url="http://writer/v1",
            writer_model="small-writer",
        )
        with patch("graph.clients.chat.make_llm", side_effect=[object(), object()]) as make:
            pair = model_pair(config)

        self.assertEqual(pair.writer.name, "small-writer")
        self.assertEqual(pair.judge.name, "large-judge")
        self.assertIs(pair.writer.gate, pair.judge.gate)
        self.assertEqual(
            [call.kwargs["model"] for call in make.call_args_list],
            ["small-writer", "large-judge"],
        )
        self.assertEqual(make.call_args_list[0].kwargs["temperature"], 0.7)

    def test_unset_writer_follows_judge(self):
        config = WikiConfig(
            chat_base_url="http://chat/v1",
            chat_model="chat",
            judge_base_url="http://judge/v1",
            judge_model="large-judge",
        )
        with patch("graph.clients.chat.make_llm", side_effect=[object(), object()]) as make:
            pair = model_pair(config)

        self.assertEqual(pair.writer.name, "large-judge")
        self.assertEqual(pair.judge.name, "large-judge")
        self.assertEqual(
            [call.kwargs["base_url"] for call in make.call_args_list],
            ["http://judge/v1", "http://judge/v1"],
        )

    def test_model_call_gate_respects_concurrency(self):
        async def run():
            gate = FastRoleGate(max_concurrency=3)
            active = 0
            maximum = 0

            async def operation():
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0.02)
                active -= 1

            await asyncio.gather(*(gate.run(operation) for _ in range(8)))
            return maximum

        self.assertEqual(asyncio.run(run()), 3)

    def test_model_call_gate_keeps_roles_isolated_until_batch_drains(self):
        async def run():
            gate = FastRoleGate(max_concurrency=2)
            both_started = asyncio.Event()
            release_fast = asyncio.Event()
            release_slow = asyncio.Event()
            judge_started = asyncio.Event()
            order = []
            active_writers = 0

            async def writer_operation(label, release):
                nonlocal active_writers
                active_writers += 1
                order.append(label)
                if active_writers == 2:
                    both_started.set()
                try:
                    await release.wait()
                finally:
                    active_writers -= 1

            async def judge_operation():
                self.assertEqual(active_writers, 0)
                order.append("judge")
                judge_started.set()

            fast = asyncio.create_task(gate.run(
                lambda: writer_operation("fast", release_fast), role="writer"
            ))
            slow = asyncio.create_task(gate.run(
                lambda: writer_operation("slow", release_slow), role="writer"
            ))
            await both_started.wait()
            judge = asyncio.create_task(gate.run(judge_operation, role="judge"))
            release_fast.set()
            await asyncio.sleep(0.01)
            self.assertFalse(judge_started.is_set())
            release_slow.set()
            await asyncio.gather(fast, slow, judge)
            return order

        order = asyncio.run(run())
        self.assertEqual(set(order[:2]), {"fast", "slow"})
        self.assertEqual(order[-1], "judge")

    def test_fast_judge_has_16k_output_ceiling(self):
        seen = []

        class Structured:
            async def ainvoke(self, _messages):
                return RepairJudgeResult(approved=True, coverage_score=100)

        class JudgeLlm:
            request_timeout = None

            def bind(self, **kwargs):
                seen.append(("bind", kwargs))
                return self

            def with_structured_output(self, _schema, **kwargs):
                seen.append(("structured", kwargs))
                return Structured()

        config = WikiConfig(
            policy="fast",
            chat_base_url="http://judge-cap/v1",
            writer_base_url="http://writer-cap/v1",
        )
        with patch(
            "graph.clients.chat.make_llm", side_effect=[object(), JudgeLlm()]
        ):
            pair = model_pair(config)
        result = asyncio.run(pair.judge.structured(RepairJudgeResult, []))

        self.assertTrue(result.approved)
        self.assertTrue(seen)
        self.assertTrue(
            all(call[1]["max_tokens"] == JUDGE_MAX_OUTPUT_TOKENS for call in seen)
        )


if __name__ == "__main__":
    unittest.main()
