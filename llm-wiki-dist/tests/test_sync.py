import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from graph import sync
from graph.project import Project


def git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, env=env
    )


class FakeLibrarian:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.ingested: list[str] = []

    def delete_document(self, name):
        self.deleted.append(name)
        return {"deleted": 1}

    def ingest_md_output(self, path, stop_check=None, raw_source_path=None, concurrency=None):
        self.ingested.append(Path(path).relative_to(Path(path).parents[2]).as_posix())
        return [object()]


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Project(Path(self.tmp.name)).ensure()
        git(self.project.raw, "init", "-q")
        (self.project.raw / "f1").mkdir()
        (self.project.raw / "f1" / "a_docx.md").write_text("# a\nline\n", encoding="utf-8")
        git(self.project.raw, "add", ".")
        git(self.project.raw, "commit", "-qm", "one")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def fake_write(project, rel, **kwargs):
        target = project.wiki_dir(rel)
        target.mkdir(parents=True, exist_ok=True)
        (target / "001-a.md").write_text("# a\n", encoding="utf-8")
        (target / "_planning").mkdir(exist_ok=True)
        (target / "_planning" / "metadata.json").write_text("{}", encoding="utf-8")
        return target

    def test_first_sync_adds_everything_and_records_head(self) -> None:
        self.assertEqual(sync.plan_changes(self.project), [sync.Change("A", "f1/a_docx.md")])
        librarian = FakeLibrarian()
        with mock.patch("graph.writers.write_wiki", side_effect=self.fake_write):
            result = sync.sync_project(
                self.project,
                librarian,
                mode="chunks",
                settings=SimpleNamespace(),
                llm=None,
                embedder=None,
            )
        self.assertEqual(librarian.ingested, ["wiki/f1/a.docx"])
        self.assertEqual(sync.last_sha(self.project), sync.head_sha(self.project))
        self.assertEqual(result["changes"][0]["status"], "A")
        self.assertTrue((self.project.wiki / "index.md").exists())
        self.assertEqual(sync.plan_changes(self.project), [])

    def test_modify_and_delete_are_detected_from_git(self) -> None:
        self.test_first_sync_adds_everything_and_records_head()
        (self.project.raw / "f1" / "a_docx.md").write_text("# a\nchanged\n", encoding="utf-8")
        (self.project.raw / "f1" / "b_pdf.md").write_text("# b\n", encoding="utf-8")
        git(self.project.raw, "add", ".")
        git(self.project.raw, "commit", "-qm", "two")
        self.assertEqual(
            sorted(c.status + " " + c.rel for c in sync.plan_changes(self.project)),
            ["A f1/b_pdf.md", "M f1/a_docx.md"],
        )
        self.assertEqual(sync.changed_hunks(self.project, "f1/a_docx.md"), [(2, 1, 2, 1)])
        git(self.project.raw, "rm", "-q", "f1/a_docx.md")
        git(self.project.raw, "commit", "-qm", "three")
        librarian = FakeLibrarian()
        with mock.patch("graph.writers.write_wiki", side_effect=self.fake_write):
            sync.sync_project(
                self.project,
                librarian,
                mode="chunks",
                settings=SimpleNamespace(),
                llm=None,
                embedder=None,
            )
        self.assertEqual(librarian.deleted, ["f1/a_docx.md"])
        self.assertFalse(self.project.wiki_dir("f1/a_docx.md").exists())
        self.assertEqual(librarian.ingested, ["wiki/f1/b.pdf"])

    def test_failed_write_does_not_move_last_sha(self) -> None:
        librarian = FakeLibrarian()
        with mock.patch("graph.writers.write_wiki", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                sync.sync_project(
                    self.project,
                    librarian,
                    mode="chunks",
                    settings=SimpleNamespace(),
                    llm=None,
                    embedder=None,
                )
        self.assertIsNone(sync.last_sha(self.project))
