"""Offline acceptance tests for human-sync rollout phases 2 through 7."""

from __future__ import annotations

import asyncio
import json
import os
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from graph import config
from graph.config import HumanSyncMode, HumanSyncPolicy, Settings
from graph.growi.client import GrowiActivity, GrowiClient, GrowiPage, GrowiPublisher, wrap_page
from graph.wiki.storage import read_json, write_json_atomic
from graph.workspace import writer
from graph.workspace.project import Project
from publisher.activity import ActivityDetector
from publisher.human_changes import HumanStore
from publisher.human_semantic import (
    JudgeResult,
    ProtectedSpan,
    SemanticAssistant,
    SpanDisposition,
    WriterRequest,
    WriterResponse,
    build_runtime_semantic_assistant,
    mechanical_validate,
    shortlist,
)
from publisher.live_verification import LiveVerificationReport, verify_boundary_confirmation
from publisher.legacy_recovery import recover_legacy_ancestor


BASE = "# Page\n\n## Limits\n\nMaximum is 40°C.\nMode is AUTO.\n"
HUMAN = BASE.replace("40°C", "60°C")


class GrowiClientContractTest(unittest.TestCase):
    def test_update_uses_view_origin_to_enforce_revision_compare_and_swap(self):
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(201, json={
                "page": {
                    "_id": "p1", "path": "/docs/001", "status": "published",
                    "revision": {"_id": "r2", "body": "new"},
                },
            })

        client = GrowiClient(
            "https://growi.invalid", "token", transport=httpx.MockTransport(handler),
        )
        page = asyncio.run(client.update_page("p1", "r1", "new"))
        self.assertEqual(requests, [{
            "pageId": "p1", "revisionId": "r1", "body": "new", "origin": "view",
        }])
        self.assertEqual((page.revision_id, page.status), ("r2", "published"))

    def test_deleted_status_is_retained_for_remote_delete_classification(self):
        page = GrowiClient._page_from_payload({
            "page": {
                "_id": "p1", "path": "/trash/docs/001", "status": "deleted",
                "revision": {"_id": "r2", "body": "old"},
            },
        })
        self.assertEqual(page.status, "deleted")


class ConfigPolicyTest(unittest.TestCase):
    def test_default_values_validation_and_policy(self):
        self.assertEqual(Settings().human_sync_mode, HumanSyncMode.off)
        for value in ("off", "observe", "apply"):
            settings = Settings.model_validate({"human_sync_mode": value})
            self.assertEqual(settings.human_sync_mode.value, value)
            self.assertEqual(HumanSyncPolicy.resolve(value).captures, value == "apply")
        with self.assertRaisesRegex(ValueError, "human_sync_mode"):
            Settings.model_validate({"human_sync_mode": "unsafe"})
        with self.assertRaisesRegex(ValueError, "off, observe, apply"):
            HumanSyncPolicy.resolve("unsafe")

    def test_ini_overrides_environment_and_legacy_defaults_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "mount"
            mount.mkdir()
            config.PROJECT_ROOT = root
            ini = root / "project.ini"
            ini.write_text(
                f"[project]\nsource_mount={mount}\ntarget_name=test\ndata_root=data\n"
                "[settings]\nhuman_sync_mode=apply\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"WIKI_HUMAN_SYNC_MODE": "observe"}, clear=False):
                self.assertEqual(Settings.from_env(str(ini)).human_sync_mode, HumanSyncMode.apply)
            ini.write_text(
                f"[project]\nsource_mount={mount}\ntarget_name=test\ndata_root=data\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("WIKI_HUMAN_SYNC_MODE", None)
                self.assertEqual(Settings.from_env(str(ini)).human_sync_mode, HumanSyncMode.off)


class ModePullTest(unittest.TestCase):
    def harness(self, mode: str, remote_body: str = HUMAN, *, revision: str = "r1"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project = Project(Path(temporary.name) / "project").ensure()
        project.raw_file("doc.md").write_text("source\n", encoding="utf-8")
        folder = project.wiki_dir("doc.md")
        folder.mkdir(parents=True)
        writer.write_source_stamp(folder, project.raw_file("doc.md"), "doc.md", identity_seed="source-1")
        write_json_atomic(folder / "_planning" / "linker.json", {"status": "complete"})
        store = HumanStore(project)
        store.generated("doc.md", {"001.md": BASE})
        store.render("doc.md")
        row = {"marker_id": "b1", "page_id": "p1", "growi_path": "/docs/doc/001", "revision_id": "r0"}
        store.remember_page("doc/001.md", row, BASE, BASE, published=True)

        class Client:
            page = GrowiPage(page_id="p1", revision_id=revision, path="/docs/doc/001",
                             body=wrap_page(remote_body, page_id="b1"))

            async def get_page(self, **_kwargs):
                return self.page

        client = Client()
        publisher = GrowiPublisher(
            client, SimpleNamespace(write_path="/docs", root_path="/docs", mode="attach"),
            human_sync_policy=HumanSyncPolicy.resolve(mode),
        )
        return project, store, row, client, publisher

    def test_same_remote_replacement_in_all_modes(self):
        for mode in ("off", "observe", "apply"):
            with self.subTest(mode=mode):
                project, store, row, _client, publisher = self.harness(mode)
                before = (project.wiki / "doc/001.md").read_bytes()
                pure = store.document("doc.md")["pages"]["001.md"]["body_blob"]
                pulled, failures, _blocked = publisher.pull_changes(project, {"doc/001.md": row}, {"doc"})
                if mode == "apply":
                    self.assertFalse(failures)
                    self.assertIn("60°C", (project.wiki / "doc/001.md").read_text(encoding="utf-8"))
                    self.assertEqual(publisher.human_sync_summary["captured"], 1)
                else:
                    self.assertFalse(pulled)
                    self.assertTrue(failures)
                    self.assertEqual((project.wiki / "doc/001.md").read_bytes(), before)
                    self.assertEqual(row["revision_id"], "r0")
                    self.assertEqual(store.document("doc.md")["pages"]["001.md"]["body_blob"], pure)
                    self.assertEqual(len(store.document("doc.md")["edits"]), 0)
                self.assertEqual(len(store.observations()), 0 if mode == "off" else 1)

    def test_safety_failures_block_in_every_mode(self):
        for mode in ("off", "observe", "apply"):
            for case in ("missing", "moved", "marker"):
                with self.subTest(mode=mode, case=case):
                    project, _store, row, client, publisher = self.harness(mode)
                    if case == "missing":
                        client.page = None
                    elif case == "moved":
                        client.page = client.page.model_copy(update={"path": "/elsewhere"})
                    else:
                        client.page = client.page.model_copy(update={"body": HUMAN})
                    _pulled, failures, blocked = publisher.pull_changes(project, {"doc/001.md": row}, {"doc"})
                    self.assertTrue(failures)
                    self.assertEqual(blocked, {"doc"})

    def test_observe_to_apply_refetches_same_revision_and_apply_off_apply_is_stable(self):
        project, store, row, client, observer = self.harness("observe")
        observer.pull_changes(project, {"doc/001.md": row}, {"doc"})
        self.assertEqual(len(store.observations()), 1)
        applier = GrowiPublisher(
            client, observer.connection, human_sync_policy=HumanSyncPolicy.resolve("apply")
        )
        pulled, failures, _ = applier.pull_changes(project, {"doc/001.md": row}, {"doc"})
        self.assertFalse(failures)
        self.assertTrue(pulled)
        self.assertEqual(len([row for row in store.document("doc.md")["edits"] if row["status"] != "deleted"]), 1)
        store.generated("doc.md", {"001.md": BASE.replace("AUTO", "MANUAL")})
        store.render("doc.md")
        off = GrowiPublisher(client, observer.connection, human_sync_policy=HumanSyncPolicy.resolve("off"))
        off.pull_changes(project, {"doc/001.md": row}, {"doc"})
        applier.pull_changes(project, {"doc/001.md": row}, {"doc"})
        text = (project.wiki / "doc/001.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("60°C"), 1)
        self.assertIn("MANUAL", text)

    def test_semantic_factory_runs_only_in_observe(self):
        for mode, expected in (("off", 0), ("observe", 1), ("apply", 0)):
            with self.subTest(mode=mode):
                project, _store, row, client, _publisher = self.harness(mode)
                calls = {"factory": 0, "proposal": 0}

                class Assistant:
                    def propose(self, **_kwargs):
                        calls["proposal"] += 1
                        return SimpleNamespace(status="fallback", reason_codes=["test"])

                def factory(_project):
                    calls["factory"] += 1
                    return Assistant()

                publisher = GrowiPublisher(
                    client, SimpleNamespace(write_path="/docs", root_path="/docs", mode="attach"),
                    human_sync_policy=HumanSyncPolicy.resolve(mode),
                    semantic_assistant_factory=factory,
                )
                publisher.pull_changes(project, {"doc/001.md": row}, {"doc"})
                self.assertEqual(calls["factory"], expected)
                self.assertEqual(calls["proposal"], expected)


class SemanticAssistanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Project(Path(self.tmp.name) / "project").ensure()
        self.store = HumanStore(self.project)

    def test_shortlist_is_stable_bounded_and_excludes_impossible_candidates(self):
        candidates = [
            {"candidate_id": "b", "source_id": "s1", "page_path": "doc/002.md", "heading_path": ["H"], "text": BASE},
            {"candidate_id": "a", "source_id": "s1", "page_path": "doc/001.md", "heading_path": ["H"], "text": BASE},
            {"candidate_id": "wrong-source", "source_id": "s2", "page_path": "doc/003.md", "heading_path": ["H"], "text": BASE},
            {"candidate_id": "wrong-type", "source_id": "s1", "page_path": "doc/004.md", "heading_path": ["H"], "block_kind": "code", "text": "```\nx\n```"},
        ]
        first = shortlist(source_id="s1", base=BASE, human=HUMAN,
                          anchor={"page_path": "doc/old.md", "heading_path": ["H"]}, candidates=candidates, cap=1)
        second = shortlist(source_id="s1", base=BASE, human=HUMAN,
                           anchor={"page_path": "doc/old.md", "heading_path": ["H"]}, candidates=list(reversed(candidates)), cap=1)
        self.assertEqual([row.candidate_id for row in first], ["a"])
        self.assertEqual(first, second)

    def test_mechanical_validation_is_authoritative(self):
        request = WriterRequest(
            base_g0="Limit 40°C.\n", human_h="Limit 60°C.\n", candidate_g1="Mode MANUAL.\n",
            source_id="s", page_path="doc/001.md", heading_path=["Limits"],
            protected_spans=[ProtectedSpan(span_id="h", kind="human", text="Limit 60°C.\n")], attempt=1,
        )
        valid = WriterResponse(
            merged_text="Limit 60°C.\nMode MANUAL.\n",
            dispositions=[SpanDisposition(span_id="h", disposition="preserved")],
        )
        self.assertTrue(mechanical_validate(request, valid).passed)
        missing = valid.model_copy(update={"merged_text": "Mode MANUAL.\n"})
        self.assertIn("protected_missing", mechanical_validate(request, missing).reason_codes)
        invented = valid.model_copy(update={"merged_text": valid.merged_text + "URL https://evil.invalid\n"})
        self.assertIn("critical_token_invented", mechanical_validate(request, invented).reason_codes)

    def test_bounded_feedback_cache_and_every_failure_falls_back(self):
        calls = {"score": 0, "write": 0, "judge": 0}

        def scorer(rows):
            calls["score"] += 1
            return {"schema_version": 1, "scorer_version": "fake", "scores": [
                {"schema_version": 1, "candidate_id": rows[0]["candidate_id"], "score": 0.95}
            ]}

        def writer_call(request):
            calls["write"] += 1
            merged = request["human_h"] if calls["write"] == 1 else request["human_h"] + request["candidate_g1"]
            return {"schema_version": 1, "merged_text": merged, "dispositions": [
                {"schema_version": 1, "span_id": "human-1", "disposition": "preserved"}
            ]}

        def judge(_request):
            calls["judge"] += 1
            return JudgeResult(passed=True, reason_codes=[]).model_dump()

        assistant = SemanticAssistant(self.store, scorer, writer_call, judge, model_version="fake-v1")
        args = dict(
            source_id="s1", base=BASE, human=HUMAN,
            anchor={"page_path": "doc/001.md", "heading_path": ["Limits"]},
            candidates=[{"candidate_id": "c1", "source_id": "s1", "page_path": "doc/001.md",
                         "heading_path": ["Limits"], "text": BASE.replace("AUTO", "MANUAL")}],
            edit_id="hedit-abc123", policy_mode="observe",
        )
        proposal = assistant.propose(**args)
        self.assertEqual(proposal.status, "proposed")
        self.assertEqual(calls, {"score": 1, "write": 2, "judge": 1})
        self.assertEqual(assistant.propose(**args).merged_blob, proposal.merged_blob)
        self.assertEqual(calls, {"score": 1, "write": 2, "judge": 1})
        failed = SemanticAssistant(self.store, lambda _rows: {"bad": True}, writer_call, judge)
        fallback = failed.propose(**{**args, "edit_id": "hedit-def456"})
        self.assertEqual(fallback.status, "fallback")
        self.assertEqual(self.store.get(fallback.merged_blob), self.store.get(fallback.fallback_blob))
        self.assertEqual(assistant.propose(**{**args, "policy_mode": "apply"}).reason_codes, ["observe_only"])

    def test_runtime_adapters_keep_page_text_in_bounded_data_messages(self):
        model_calls = []
        engine_requests = []

        class FakeModel:
            def __init__(self, config):
                self.name = "fake"
                self.provider = "fake://local"
                self.kind = "judge" if "judge" in str(config.run_dir) else "writer"

            async def structured(self, schema, messages, **_kwargs):
                model_calls.append((self.kind, messages))
                if schema is WriterResponse:
                    payload = json.loads(messages[1].content.split("<DATA>\n", 1)[1].rsplit("\n</DATA>", 1)[0])
                    request = WriterRequest.model_validate(payload)
                    return WriterResponse(
                        merged_text=request.human_h + request.candidate_g1,
                        dispositions=[SpanDisposition(span_id="human-1", disposition="preserved")],
                    )
                return JudgeResult(passed=True, reason_codes=[])

        class FakeEngine:
            def decide_batch(self, requests):
                engine_requests.extend(requests)
                return [SimpleNamespace(p_yes=0.95) for _request in requests]

        with patch("graph.wiki.model.ChatModelPort", FakeModel), patch("jev.get_engine_for", return_value=FakeEngine()):
            assistant = build_runtime_semantic_assistant(self.store, Settings())
            proposal = assistant.propose(
                source_id="s1", base=BASE, human=HUMAN,
                anchor={"page_path": "doc/001.md", "heading_path": ["Limits"]},
                candidates=[{"candidate_id": "c1", "source_id": "s1", "page_path": "doc/001.md",
                             "heading_path": ["Limits"], "text": BASE.replace("AUTO", "MANUAL")}],
                edit_id="hedit-abc999", policy_mode="observe",
            )
        self.assertEqual(proposal.status, "proposed")
        self.assertEqual(len(engine_requests), 1)
        self.assertEqual(engine_requests[0].state["H"], HUMAN)
        self.assertEqual([kind for kind, _messages in model_calls], ["writer", "judge"])
        self.assertNotIn(HUMAN, model_calls[0][1][0].content)


class ActivityDetectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Project(Path(self.tmp.name) / "project").ensure()

    def test_idle_poll_reads_no_page_bodies_and_same_timestamp_restart_is_idempotent(self):
        stamp = "2026-10-01T00:00:00+00:00"
        rows = [GrowiActivity(activity_id=f"a{i:03}", created_at=stamp, action="page.update", page_id="p1")
                for i in range(125)]

        class Client:
            body_reads = 0
            async def list_activities(self, *, offset=0, limit=100):
                return rows[offset:offset + limit], len(rows)
            async def get_page(self, **_kwargs):
                self.body_reads += 1
                raise AssertionError("detector hints must not fetch bodies")

        detector = ActivityDetector(self.project, endpoint="https://growi", boundary="/docs")
        batch = detector.poll(Client(), {"doc/001.md": {"page_id": "p1"}})
        self.assertEqual(len(batch.events), 125)
        self.assertEqual(batch.selected_page_ids, {"p1"})
        self.assertEqual(batch.metrics["pages_fetched"], 0)
        detector.commit(batch)
        repeated = detector.poll(Client(), {"doc/001.md": {"page_id": "p1"}})
        self.assertFalse(repeated.events)

    def test_official_activity_response_contract_is_parsed(self):
        payload = {"serializedPaginationResult": {"docs": [{
            "_id": "a1", "target": "p1", "targetModel": "Page", "action": "PAGE_UPDATE",
            "snapshot": {"pageId": "p1", "pagePath": "/docs/a"},
            "createdAt": "2026-10-01T00:00:00.000Z", "user": {"_id": "u1"},
        }], "totalDocs": 1}}

        class Response:
            def json(self): return payload

        client = GrowiClient("https://growi", "")
        async def request(*_args, **_kwargs): return Response()
        client._request = request
        rows, total = asyncio.run(client.list_activities())
        self.assertEqual(total, 1)
        self.assertEqual((rows[0].activity_id, rows[0].page_id, rows[0].path, rows[0].updated_by),
                         ("a1", "p1", "/docs/a", "u1"))

    def test_inventory_classifies_owned_foreign_and_duplicate_markers(self):
        pages = [
            GrowiPage(page_id="p1", revision_id="r2", path="/docs/a", body=""),
            GrowiPage(page_id="p2", revision_id="x", path="/docs/foreign", body="foreign"),
            GrowiPage(page_id="p3", revision_id="x", path="/docs/lost", body=wrap_page("ours", page_id="b1")),
        ]

        class Client:
            async def list_all_pages(self, _boundary): return pages
            async def get_page(self, *, page_id): return next(row for row in pages if row.page_id == page_id)

        detector = ActivityDetector(self.project, endpoint="https://growi", boundary="/docs")
        result = detector.inventory(Client(), {
            "doc/001.md": {"page_id": "p1", "revision_id": "r1", "growi_path": "/docs/a", "marker_id": "b1"}
        })
        kinds = [row["classification"] for row in result.classifications]
        self.assertIn("changed_owned", kinds)
        self.assertIn("unmanaged_foreign", kinds)
        self.assertIn("newly_discovered_owned", kinds)
        self.assertIn("duplicate_ownership", kinds)

    def test_reset_cursor_accepts_first_new_event_and_late_overlap_once(self):
        detector = ActivityDetector(self.project, endpoint="https://growi", boundary="/docs", overlap_seconds=60)
        write_json_atomic(detector.path, {
            "schema_version": 1, "endpoint_identity": detector.endpoint_identity,
            "last_processed_at": "2026-10-01T00:01:00+00:00", "ids_at_last_timestamp": [],
            "recent_ids": [], "last_sequence": None,
        })
        rows = [GrowiActivity(activity_id="late", created_at="2026-10-01T00:00:30+00:00",
                              action="page.update", page_id="p1")]

        class Client:
            async def list_activities(self, *, offset=0, limit=100): return rows[offset:offset + limit], len(rows)

        first = detector.poll(Client(), {"doc/001.md": {"page_id": "p1"}})
        self.assertEqual([row.activity_id for row in first.events], ["late"])
        detector.commit(first)
        self.assertFalse(detector.poll(Client(), {"doc/001.md": {"page_id": "p1"}}).events)

    def test_malformed_activity_and_permission_gap_force_inventory_without_advancing_cursor(self):
        detector = ActivityDetector(self.project, endpoint="https://growi", boundary="/docs")

        class Malformed:
            async def list_activities(self, **_kwargs): raise ValueError("bad payload")

        malformed = detector.poll(Malformed(), {})
        self.assertEqual(malformed.fallback_reason, "malformed_activity_response")
        detector.commit(malformed)
        self.assertFalse(detector.path.exists())

        class DeniedError(RuntimeError):
            status_code = 403

        class Denied:
            async def list_activities(self, **_kwargs): raise DeniedError("denied")

        self.assertEqual(detector.poll(Denied(), {}).fallback_reason, "activity_permission_gap")

    def test_inventory_rejects_results_outside_boundary(self):
        class Client:
            async def list_all_pages(self, _boundary):
                return [GrowiPage(page_id="p1", revision_id="r1", path="/other/page")]

        detector = ActivityDetector(self.project, endpoint="https://growi", boundary="/docs")
        with self.assertRaisesRegex(ValueError, "outside"):
            detector.inventory(Client(), {})


class OperatorAndLiveGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Project(Path(self.tmp.name) / "project").ensure()
        self.project.raw_file("doc.md").write_text("source\n", encoding="utf-8")
        folder = self.project.wiki_dir("doc.md")
        folder.mkdir(parents=True)
        writer.write_source_stamp(folder, self.project.raw_file("doc.md"), "doc.md", identity_seed="source-1")
        write_json_atomic(folder / "_planning" / "linker.json", {"status": "complete"})
        self.store = HumanStore(self.project)
        self.store.generated("doc.md", {"001.md": BASE})
        self.store.render("doc.md")
        row = {"marker_id": "b1", "page_id": "p1", "growi_path": "/docs/doc/001", "revision_id": "r0",
               "observed_revision_id": "r1"}
        self.store.remember_page("doc/001.md", row, BASE, BASE, published=True)
        row["observed_revision_id"] = "r1"
        self.store.capture("doc.md", "doc/001.md", row, BASE, HUMAN)
        self.store.render("doc.md")
        baseline = self.store.page("b1")
        baseline["observed_revision"] = "r1"
        self.store.save_page(baseline)

    def test_summary_is_redacted_and_resolution_is_checked_idempotent(self):
        edit = next(row for row in self.store.document("doc.md")["edits"] if row["status"] != "deleted")
        summary = self.store.project_summary()
        encoded = json.dumps(summary, ensure_ascii=False)
        self.assertIn(edit["edit_id"], encoded)
        self.assertNotIn("60°C", encoded)
        resolution = self.store.resolve(edit["edit_id"], action="keep-human", expected_revision="r1")
        self.assertEqual(self.store.resolve(edit["edit_id"], action="keep-human", expected_revision="r1"), resolution)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.resolve(edit["edit_id"], action="accept-source", expected_revision="old")

    def test_human_cli_checkpoints_summary_and_rejects_a_dirty_live_tree(self):
        from publisher.history import last_good
        from runner import cli

        with tempfile.TemporaryDirectory() as tmp:
            project = Project(Path(tmp) / "empty-project").ensure()
            settings = SimpleNamespace()
            args = SimpleNamespace(human_command="status")
            with patch.object(cli, "_settings", return_value=settings), \
                 patch.object(cli, "open_project", return_value=project), \
                 patch("builtins.print"):
                self.assertEqual(cli.cmd_human(args), 0)
            head = subprocess.run(
                ["git", "-C", str(project.root), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(last_good(project), head)
            summary_path = project.metadata / "human-sync" / "operator-summary.json"
            self.assertTrue(summary_path.is_file())

            # Stable summaries are a no-op; a second status must not create an
            # empty checkpoint.
            with patch.object(cli, "_settings", return_value=settings), \
                 patch.object(cli, "open_project", return_value=project), \
                 patch("builtins.print"):
                self.assertEqual(cli.cmd_human(args), 0)
            self.assertEqual(last_good(project), head)

            summary_path.write_text("dirty\n", encoding="utf-8")
            with patch.object(cli, "_settings", return_value=settings), \
                 patch.object(cli, "open_project", return_value=project), \
                 self.assertRaisesRegex(RuntimeError, "dirty last-good"):
                cli.cmd_human(args)
            self.assertEqual(summary_path.read_text(encoding="utf-8"), "dirty\n")

    def test_live_report_is_local_redacted_and_requires_exact_boundary_confirmation(self):
        settings = SimpleNamespace(target_name="docs", growi_url="https://secret-token@example.test/growi",
                                   growi_mode="attach", human_sync_mode=HumanSyncMode.off)
        report = LiveVerificationReport.create(self.project, settings, "/docs/disposable/human-sync")
        body = report.path.read_text(encoding="utf-8")
        self.assertNotIn("secret-token", body)
        with self.assertRaises(PermissionError):
            verify_boundary_confirmation(settings.growi_url, report.data["disposable_path"], "wrong")
        verify_boundary_confirmation(settings.growi_url, report.data["disposable_path"], report.data["confirmation_code"])
        self.assertTrue(all(row["status"] == "pending" for row in report.data["cases"].values()))

    def test_live_report_can_initialize_before_any_human_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Project(Path(tmp) / "fresh-project").ensure()
            settings = SimpleNamespace(target_name="docs", growi_url="https://example.test/growi",
                                       growi_mode="attach", human_sync_mode=HumanSyncMode.off)
            LiveVerificationReport.create(project, settings, "/docs/disposable/report-first")
            store = HumanStore(project)
            self.assertEqual(read_json(store.root / "schema.json"), {"schema_version": 1})

    def test_live_report_records_constraints_without_claiming_a_pass(self):
        settings = SimpleNamespace(target_name="docs", growi_url="https://example.test/growi",
                                   growi_mode="attach", human_sync_mode=HumanSyncMode.off)
        report = LiveVerificationReport.create(self.project, settings, "/docs/disposable/constrained")
        report.record_constraint("activity_cursor_restart", reason_codes=["global_feed_disallowed"])
        self.assertEqual(report.data["cases"]["activity_cursor_restart"]["status"], "constrained")
        final = report.finalize(cleanup_status="not_started", recovery_possible=True)
        self.assertEqual(final["status"], "incomplete")


class LegacyRecoveryTest(unittest.TestCase):
    def make_history(self, *, second_candidate: bool = False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project = Project(Path(temporary.name) / "project").ensure()
        raw = project.raw_file("doc.md")
        raw.write_text("source\n", encoding="utf-8")
        folder = project.wiki_dir("doc.md")
        folder.mkdir(parents=True)
        writer.write_source_stamp(folder, raw, "doc.md", identity_seed="source-1")
        state_wiki = project.state_dir("doc.md") / "wiki"
        state_pages = project.state_dir("doc.md") / "state" / "pages"
        state_wiki.mkdir(parents=True)
        state_pages.mkdir(parents=True)

        def write_candidate(text: str):
            (state_wiki / "001.md").write_text(text, encoding="utf-8")
            write_json_atomic(state_pages / "p1.json", {
                "filename": "001.md", "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "human_edited": False,
            })

        write_candidate(BASE)
        write_json_atomic(project.metadata / "pipeline.json", {
            "schema_version": 4,
            "sources": {"doc.md": {"raw_rel": "doc.md", "source_id": "source-1", "id_seed": "source-1"}},
            "published_documents": {}, "published_pages": {}, "endpoint": "", "marker_format": "",
        })
        subprocess.run(["git", "init", "-q", str(project.root)], check=True)
        subprocess.run(["git", "-C", str(project.root), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(project.root), "config", "user.email", "test@example.test"], check=True)
        subprocess.run(["git", "-C", str(project.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(project.root), "commit", "-q", "-m", "pure one"], check=True)
        if second_candidate:
            write_candidate(BASE.replace("40°C", "45°C"))
            subprocess.run(["git", "-C", str(project.root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(project.root), "commit", "-q", "-m", "pure two"], check=True)
        store = HumanStore(project)
        store.pin_legacy("doc.md", "doc/001.md", HUMAN, revision="r1")
        return project, store

    def test_unique_verified_git_ancestor_recovers_and_records_evidence(self):
        project, store = self.make_history()
        result = recover_legacy_ancestor(store, "doc.md")
        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["recovered"], 1)
        document = store.document("doc.md")
        self.assertTrue(document["legacy_recovery"])
        self.assertTrue(document["legacy_recovery"][0]["body_blob_ids"])
        self.assertTrue(document["legacy_recovery"][0]["raw_blob_ids"])
        self.assertFalse(any(row["status"] == "legacy_pinned" for row in document["edits"]))
        self.assertIn("60°C", (project.wiki / "doc/001.md").read_text(encoding="utf-8"))

    def test_multiple_verified_bodies_remain_pinned_without_partial_recovery(self):
        _project, store = self.make_history(second_candidate=True)
        before = json.dumps(store.document("doc.md"), sort_keys=True)
        result = recover_legacy_ancestor(store, "doc.md")
        self.assertEqual(result["status"], "legacy_pinned")
        self.assertEqual(json.dumps(store.document("doc.md"), sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
