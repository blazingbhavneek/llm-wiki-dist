from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from graph.workspace.project import Project, open_project
from graph.workspace.writer import wiki_config
from graph.config import Settings
from graph.growi.client import GrowiPage, GrowiPublisher, wrap_page
from publisher import pipeline
from publisher import queue
from publisher.ledger import Ledger, save_ledger


class PipelineScopeTest(unittest.TestCase):
    def test_absolute_project_config_selects_one_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mount = base / "mount"
            mount.mkdir()
            config = base / "sample.ini"
            config.write_text(
                f"[project]\nsource_mount = {mount}\ntarget_name = Moove\ndata_root = output\n"
                "growi_url = http://project-growi.invalid/\n"
                "growi_token = API Token:project-token\n",
                encoding="utf-8",
            )
            settings = Settings.from_env(str(config))
            self.assertEqual(settings.target_name, "Moove")
            self.assertEqual(settings.data_root, str(base / "output"))
            self.assertEqual(settings.mount_path, str(mount))
            self.assertEqual(settings.growi_url, "http://project-growi.invalid/")
            self.assertEqual(settings.growi_token, "project-token")
            with patch.dict("os.environ", {"GROWI_URL": "http://growi.invalid", "GROWI_TOKEN": "old-token"}):
                connection = pipeline._connection(settings)
            self.assertEqual(connection.write_path, "/Moove")
            self.assertEqual(open_project(settings).root, base / "output" / "Moove")

    def test_shared_concurrency_applies_to_all_pipeline_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mount = base / "mount"
            mount.mkdir()
            config = base / "sample.ini"
            config.write_text(
                f"[project]\nsource_mount = {mount}\ntarget_name = test\ndata_root = output\n",
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "WIKI_CONCURRENCY": "7",
                    "WIKI_CHAT_BASE_URL": "http://env-llm.invalid/v1",
                },
            ):
                settings = Settings.from_env(str(config))

            self.assertEqual(settings.chat_base_url, "http://env-llm.invalid/v1")
            self.assertEqual(settings.concurrency, 7)
            self.assertEqual(settings.wiki_rewrite_concurrency, 7)
            self.assertEqual(settings.wiki_linker_concurrency, 7)
            self.assertEqual(settings.ingest_concurrency, 7)
            self.assertEqual(settings.service_max_agents, 7)
            wiki = wiki_config(settings, run_dir=base / "state")
            self.assertEqual(wiki.planner_concurrency, 7)
            self.assertEqual(wiki.rewrite_concurrency, 7)

    def test_project_settings_override_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mount = base / "mount"
            mount.mkdir()
            config = base / "sample.ini"
            config.write_text(
                f"""[project]
source_mount = {mount}
target_name = test
data_root = output

[settings]
growi_url = http://project-growi.invalid/
growi_token = API Token:project-token
growi_mode = own
growi_timeout = 45
chat_base_url = http://project-llm.invalid/v1
chat_api_key = project-key
chat_model = project-model
embed_base_url = http://project-embed.invalid/v1
rerank_base_url = http://project-rerank.invalid/v1
parser_base_url = http://project-parser.invalid/
parser_timeout = 600
concurrency = 7
wiki_linker_enabled = false
""",
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "GROWI_URL": "http://env-growi.invalid/",
                    "WIKI_CHAT_BASE_URL": "http://env-llm.invalid/v1",
                    "WIKI_CONCURRENCY": "2",
                    "WIKI_LINKER_ENABLED": "true",
                },
            ):
                settings = Settings.from_env(str(config))

            self.assertEqual(settings.growi_url, "http://project-growi.invalid/")
            self.assertEqual(settings.growi_token, "project-token")
            self.assertEqual(settings.growi_mode, "own")
            self.assertEqual(settings.growi_timeout, 45)
            self.assertEqual(settings.chat_base_url, "http://project-llm.invalid/v1")
            self.assertEqual(settings.chat_api_key, "project-key")
            self.assertEqual(settings.chat_model, "project-model")
            self.assertEqual(settings.embed_base_url, "http://project-embed.invalid/v1")
            self.assertEqual(settings.rerank_base_url, "http://project-rerank.invalid/v1")
            self.assertEqual(settings.parser_base_url, "http://project-parser.invalid/")
            self.assertEqual(settings.parser_timeout, 600)
            self.assertEqual(settings.concurrency, 7)
            self.assertEqual(settings.wiki_rewrite_concurrency, 7)
            self.assertEqual(settings.wiki_linker_concurrency, 7)
            self.assertEqual(settings.ingest_concurrency, 7)
            self.assertFalse(settings.wiki_linker_enabled)

    def test_sync_only_scopes_generation_linking_and_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mount = base / "mount"
            mount.mkdir()
            (mount / "a.md").write_text("A", encoding="utf-8")
            (mount / "b.md").write_text("B", encoding="utf-8")
            settings = SimpleNamespace(
                data_root=str(base / "data"),
                target_name="test",
                mount_path=str(mount),
                ingest_mode="wiki",
                wiki_linker_enabled=True,
                wiki_linker_mode="neo",
            )
            parsed: list[str] = []
            pending: list[list[str] | None] = []
            linked: list[str] = []
            published: list[set[str] | None] = []

            def parse(item, path, _settings):
                parsed.append(item.rel)
                return path.read_text(encoding="utf-8")

            def write(project, rel, **_kwargs):
                planning = project.wiki_dir(rel) / "_planning"
                planning.mkdir(parents=True)
                (planning / "source.json").write_text(
                    json.dumps({"raw": rel}), encoding="utf-8"
                )
                return SimpleNamespace(touched=[])

            def pending_rels(_project, _settings, rels=None, force=False):
                pending.append(None if rels is None else list(rels))
                return list(rels or [])

            def link(_project, rels, **_kwargs):
                linked.extend(rels)
                return ["peer_md.md"]

            def publish(_project, _ledger, _publisher, _run_id, *, only=None, on_progress=None):
                published.append(only)
                return []

            with (
                patch.object(pipeline, "_publisher", return_value=object()),
                patch.object(pipeline, "_model", return_value=object()),
                patch.object(pipeline, "Embedder", return_value=object()),
                patch.object(pipeline, "_parse", side_effect=parse),
                patch.object(pipeline, "write_wiki_pages", side_effect=write),
                patch.object(pipeline, "_pending_link_rels", side_effect=pending_rels),
                patch.object(pipeline, "run_linkers", side_effect=link),
                patch.object(pipeline, "_publish_sweep", side_effect=publish),
            ):
                result = pipeline.sync_once(settings, only=["a.md"], force=True)

            self.assertFalse(result["failures"])
            self.assertEqual(parsed, ["a.md"])
            self.assertEqual(pending, [["a_md.md"], ["a_md.md"]])
            self.assertEqual(linked, ["a_md.md"])
            self.assertEqual(published, [{"a_md.md", "peer_md.md"}])

    def test_publish_scope_does_not_delete_other_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Project(Path(tmp), Path(tmp) / "mount").ensure()
            ledger = Ledger(
                {},
                {
                    "a.md": {"raw_rel": "a_md.md"},
                    "b.md": {"raw_rel": "b_md.md"},
                },
            )
            deleted: list[str] = []

            class Publisher:
                def discover_documents(self, *_args):
                    return {}

                def pull_changes(self, *_args):
                    return [], [], set()

                def publish_documents(self, *_args, **_kwargs):
                    return {}

                def delete_document(self, _project, rel):
                    deleted.append(rel)

            failures = pipeline._publish_sweep(
                project, ledger, Publisher(), "test", only={"a_md.md"}
            )

            self.assertFalse(failures)
            self.assertEqual(deleted, ["a_md.md"])
            self.assertNotIn("a.md", ledger.published_documents)
            self.assertIn("b.md", ledger.published_documents)

    def test_publish_uses_actual_wiki_folder_when_source_marker_is_stale(self) -> None:
        document = "Moove/manual.pdf"
        self.assertEqual(pipeline._document_raw_rel(document), "Moove/manual_pdf.md")

    def test_queue_delete_cancels_unpublished_add_and_prioritizes_known_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mount = base / "mount"
            mount.mkdir()
            settings = SimpleNamespace(data_root=str(base / "data"), target_name="test", mount_path=str(mount))
            project = Project(base / "data" / "test", mount).ensure()

            source = mount / "a.md"
            source.write_text("A", encoding="utf-8")
            self.assertEqual(queue.scan(settings, settle_seconds=0)["added"], ["a.md"])
            active = queue.claim(project, "slow")
            source.unlink()
            self.assertEqual(queue.scan(settings, settle_seconds=0)["cancelled"], ["a.md"])
            self.assertFalse(queue.current(project, active))
            self.assertEqual(queue.status(project), [])

            source.write_text("B", encoding="utf-8")
            queue.scan(settings, settle_seconds=0)
            queue.finish(project, queue.claim(project, "slow"))
            save_ledger(project.metadata / "pipeline.json", Ledger({"a.md": {"raw_rel": "a_md.md"}}, {}))
            source.unlink()
            self.assertEqual(queue.scan(settings, settle_seconds=0)["deleted"], ["a.md"])
            self.assertEqual(queue.status(project)[0]["lane"], "fast")

    def test_scan_requeues_seen_but_unprocessed_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mount = base / "mount"
            mount.mkdir()
            settings = SimpleNamespace(data_root=str(base / "data"), target_name="test", mount_path=str(mount))
            project = Project(base / "data" / "test", mount).ensure()
            (mount / "missed.md").write_text("missing", encoding="utf-8")

            queue.scan(settings, settle_seconds=0)
            queue.finish(project, queue.claim(project, "slow"))
            self.assertEqual(queue.status(project), [])

            result = queue.scan(settings, settle_seconds=0)
            self.assertEqual(result["added"], ["missed.md"])
            self.assertEqual(queue.status(project)[0]["rel"], "missed.md")

    def test_worker_never_drops_an_omitted_file_from_a_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mount = base / "mount"
            mount.mkdir()
            (mount / "a.md").write_text("A", encoding="utf-8")
            (mount / "b.md").write_text("B", encoding="utf-8")
            settings = SimpleNamespace(data_root=str(base / "data"), target_name="test", mount_path=str(mount))
            project = Project(base / "data" / "test", mount).ensure()
            queue.scan(settings, only=["a.md", "b.md"], settle_seconds=0)

            events: list[dict] = []
            with patch.object(pipeline, "sync_once", return_value={
                "run_id": "test", "scan": object(), "done": [{"path": "a.md", "status": "added"}],
                "failures": [], "cancelled": False,
            }) as sync:
                result = queue.work_once(settings, on_event=events.append)

            self.assertEqual(result["paths"], ["a.md", "b.md"])
            self.assertIsNotNone(sync.call_args.kwargs["on_progress"])
            self.assertEqual(events[0]["stage"], "queue-claim")
            self.assertIn("b.md", result["failures"][0])
            self.assertEqual({row["rel"] for row in queue.status(project)}, {"a.md", "b.md"})
            self.assertEqual({row["status"] for row in queue.status(project)}, {"failed"})

    def test_two_selected_files_reach_publish_in_one_watcher_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mount = base / "mount"
            mount.mkdir()
            (mount / "a.md").write_text("A", encoding="utf-8")
            (mount / "b.md").write_text("B", encoding="utf-8")
            settings = SimpleNamespace(
                data_root=str(base / "data"), target_name="test", mount_path=str(mount),
                ingest_mode="wiki", wiki_linker_enabled=True, wiki_linker_mode="neo",
            )
            project = Project(base / "data" / "test", mount).ensure()
            published: list[set[str] | None] = []

            def write(project, rel, **_kwargs):
                planning = project.wiki_dir(rel) / "_planning"
                planning.mkdir(parents=True)
                (planning / "source.json").write_text(json.dumps({"raw": rel}), encoding="utf-8")
                return SimpleNamespace(touched=[])

            queue.scan(settings, only=["a.md", "b.md"], settle_seconds=0)
            with (
                patch.object(pipeline, "_publisher", return_value=object()),
                patch.object(pipeline, "_model", return_value=object()),
                patch.object(pipeline, "Embedder", return_value=object()),
                patch.object(pipeline, "_parse", side_effect=lambda item, *_args: item.rel),
                patch.object(pipeline, "write_wiki_pages", side_effect=write),
                patch.object(pipeline, "_pending_link_rels", side_effect=[[], ["a_md.md", "b_md.md"]]),
                patch.object(pipeline, "run_linkers", return_value=[]),
                patch.object(pipeline, "_publish_sweep", side_effect=lambda *_args, only=None, **_kwargs: published.append(only) or []),
            ):
                result = queue.work_once(settings)

            self.assertEqual(result["paths"], ["a.md", "b.md"])
            self.assertEqual([row["path"] for row in result["done"][:2]], ["a.md", "b.md"])
            self.assertEqual(published, [{"a_md.md", "b_md.md"}])
            self.assertEqual(queue.status(project), [])

    def test_watcher_rejects_a_mistyped_selected_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mount = base / "mount"
            mount.mkdir()
            (mount / "good.md").write_text("A", encoding="utf-8")
            settings = SimpleNamespace(data_root=str(base / "data"), target_name="test", mount_path=str(mount))

            with self.assertRaisesRegex(FileNotFoundError, "bad.md"):
                queue.serve(settings, only=["bad.md"], growi_interval=0)

    def test_failed_generation_is_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mount = base / "mount"
            mount.mkdir()
            (mount / "a.md").write_text("A", encoding="utf-8")
            settings = SimpleNamespace(
                data_root=str(base / "data"), target_name="test", mount_path=str(mount),
                ingest_mode="wiki", wiki_linker_enabled=True, wiki_linker_mode="neo",
            )
            with (
                patch.object(pipeline, "_publisher", return_value=object()),
                patch.object(pipeline, "_model", return_value=object()),
                patch.object(pipeline, "Embedder", return_value=object()),
                patch.object(pipeline, "_parse", side_effect=RuntimeError("broken")),
                patch.object(pipeline, "_publish_sweep") as publish,
            ):
                result = pipeline.sync_once(settings)
            self.assertTrue(result["failures"])
            publish.assert_not_called()

    def test_superseded_batch_is_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mount = base / "mount"
            mount.mkdir()
            (mount / "a.md").write_text("A", encoding="utf-8")
            settings = SimpleNamespace(
                data_root=str(base / "data"), target_name="test", mount_path=str(mount),
                ingest_mode="wiki", wiki_linker_enabled=True, wiki_linker_mode="neo",
            )
            with (
                patch.object(pipeline, "_publisher", return_value=object()),
                patch.object(pipeline, "_model", return_value=object()),
                patch.object(pipeline, "Embedder", return_value=object()),
                patch.object(pipeline, "_publish_sweep") as publish,
            ):
                result = pipeline.sync_once(settings, should_continue=lambda: False)
            self.assertTrue(result["cancelled"])
            publish.assert_not_called()

    def test_growi_pull_updates_resumable_generator_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Project(Path(tmp), Path(tmp) / "mount").ensure()
            folder = project.wiki_dir("doc.md")
            pristine = folder / "_planning" / "pages" / "001.md"
            pristine.parent.mkdir(parents=True)
            pristine.write_text("generated\n", encoding="utf-8")
            (folder / "001.md").write_text("generated\n", encoding="utf-8")
            (folder / "_planning" / "source.json").write_text(json.dumps({"raw": "doc.md"}), encoding="utf-8")
            (folder / "_planning" / "linker.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
            state_root = project.state_dir("doc.md")
            (state_root / "wiki").mkdir(parents=True)
            (state_root / "state" / "pages").mkdir(parents=True)
            state_page = state_root / "wiki" / "001.md"
            state_page.write_text("generated\n", encoding="utf-8")
            sidecar = state_root / "state" / "pages" / "001.json"
            sidecar.write_text(json.dumps({"filename": "001.md", "content_sha256": "old"}), encoding="utf-8")

            marker_id = "page/docs/doc/001.md"

            class Client:
                async def get_page(self, **_kwargs):
                    return GrowiPage(page_id="p1", revision_id="new", path="/docs/doc/001.md", body=wrap_page("user edit", page_id=marker_id, ranges=[]))

            publisher = GrowiPublisher(Client(), SimpleNamespace(write_path="/docs"))
            pulled, conflicts, _blocked = publisher.pull_changes(
                project,
                {"doc/001.md": {"growi_path": "/docs/doc/001.md", "page_id": "p1", "revision_id": "old"}},
                {"doc"},
            )

            self.assertEqual(conflicts, [])
            self.assertEqual(pulled, ["doc/001.md"])
            self.assertEqual(state_page.read_text(encoding="utf-8"), "user edit\n")
            self.assertNotEqual(json.loads(sidecar.read_text(encoding="utf-8"))["content_sha256"], "old")


if __name__ == "__main__":
    unittest.main()
