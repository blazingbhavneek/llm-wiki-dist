import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from graph.workspace import writer as writers


class BuildWikiOutputTests(unittest.TestCase):
    def test_wiki_mode_runs_pipeline_then_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.md"
            source.write_text("# a\n", encoding="utf-8")
            with mock.patch.object(writers, "run_wiki", return_value=root / "run") as run_wiki, mock.patch(
                "graph.wiki.export.export_ingest_layout"
            ) as export:
                (root / "out" / "docs").mkdir(parents=True)
                (root / "out" / "docs" / "001-a.md").write_text("# a\n", encoding="utf-8")
                result = writers.build_wiki_output(
                    source_path=source,
                    document_name="a.md",
                    out_dir=root / "out",
                    mode="wiki",
                    settings=SimpleNamespace(),
                    llm=None,
                    embedder=None,
                    state_dir=root / "state",
                )
            run_wiki.assert_called_once()
            self.assertEqual(run_wiki.call_args.kwargs["run_dir"], root / "state")
            export.assert_called_once_with(root / "run", root / "out", document_name="a.md")
            self.assertEqual(result.file_count, 1)

    def test_wiki_config_maps_settings(self) -> None:
        settings = SimpleNamespace(
            chat_base_url="u",
            chat_api_key="k",
            chat_model="m",
            wiki_section_target_lines=50,
            wiki_write_attempts=2,
            wiki_rewrite_concurrency=1,
            wiki_output_language="ja",
        )
        config = writers.wiki_config(settings, run_dir=Path("/tmp/x"))
        self.assertEqual(
            (
                config.chat_model,
                config.section_target_lines,
                config.write_attempts,
                config.rewrite_concurrency,
                config.run_dir,
            ),
            ("m", 50, 2, 1, "/tmp/x"),
        )

    def test_write_wiki_publishes_flat_pages_and_planning(self) -> None:
        from graph.project import Project

        def fake_build(**kwargs):
            out = Path(kwargs["out_dir"])
            (out / "docs").mkdir(parents=True)
            (out / "_planning").mkdir()
            (out / "docs" / "001-a.md").write_text("# a\n", encoding="utf-8")
            (out / "_planning" / "metadata.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(out_dir=out, file_count=1)

        with tempfile.TemporaryDirectory() as directory:
            project = Project(Path(directory)).ensure()
            (project.raw / "f1").mkdir()
            (project.raw / "f1" / "a_docx.md").write_text("# a\n", encoding="utf-8")
            with mock.patch.object(writers, "build_wiki_output", side_effect=fake_build):
                result = writers.write_wiki(
                    project,
                    "f1/a_docx.md",
                    mode="chunks",
                    settings=SimpleNamespace(wiki_linker_enabled=False),
                    llm=None,
                    embedder=None,
                )
            target = result.target
            self.assertEqual(result.touched, [])
            self.assertEqual(target, project.wiki / "f1" / "a.docx")
            marker = json.loads((target / "_planning" / "linker.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["status"], "disabled")
            self.assertTrue((target / "001-a.md").exists())
            self.assertFalse((target / "docs").exists())
            self.assertFalse(project.work_dir("f1/a_docx.md").exists())
            writers.write_index(project)
            index = (project.wiki / "index.md").read_text(encoding="utf-8")
            self.assertIn("[001-a](f1/a.docx/001-a.md)", index)


class WriterLinkerIntegrationTests(unittest.TestCase):
    """write_wiki runs the linker between local publication and the source stamp."""

    def test_second_document_touches_the_first_through_write_wiki(self) -> None:
        from graph.workspace.project import Project
        from tests.test_linker import A_PAGE, B_PAGE, FakeEmbedder, FakeModel

        pages = {"f1/a_docx.md": ("001-a.md", A_PAGE), "f1/b_docx.md": ("001-b.md", B_PAGE)}

        def fake_build(**kwargs):
            out = Path(kwargs["out_dir"])
            name, text = pages[kwargs["document_name"]]
            (out / "docs").mkdir(parents=True)
            (out / "_planning").mkdir()
            (out / "docs" / name).write_text(text, encoding="utf-8")
            (out / "_planning" / "metadata.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(out_dir=out, file_count=1)

        settings = SimpleNamespace(wiki_linker_enabled=True, wiki_linker_mode="legacy", wiki_output_language="ja", wiki_rewrite_concurrency=1)
        with tempfile.TemporaryDirectory() as directory:
            project = Project(Path(directory)).ensure()
            (project.raw / "f1").mkdir()
            for rel in pages:
                (project.raw / rel).write_text("# raw\n", encoding="utf-8")
            model = FakeModel()
            with mock.patch.object(writers, "build_wiki_output", side_effect=fake_build):
                first = writers.write_wiki(project, "f1/a_docx.md", mode="wiki", settings=settings, llm=model, embedder=FakeEmbedder())
                second = writers.write_wiki(project, "f1/b_docx.md", mode="wiki", settings=settings, llm=model, embedder=FakeEmbedder())
            self.assertEqual(first.touched, [])
            self.assertEqual(second.touched, ["f1/a_docx.md"])
            for result in (first, second):
                self.assertTrue((result.target / "_planning" / "source.json").exists())
                self.assertTrue(writers.up_to_date(project, "f1/a_docx.md"))
            self.assertIn("<!-- llm-wiki-links:start -->", (first.target / "001-a.md").read_text(encoding="utf-8"))
            self.assertNotIn("<!-- llm-wiki-links", (first.target / "_planning" / "pages" / "001-a.md").read_text(encoding="utf-8"))
