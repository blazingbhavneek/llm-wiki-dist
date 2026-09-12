import tempfile
import unittest
from pathlib import Path

from graph.project import Project, raw_name_for, wiki_folder_name


class ProjectPathTests(unittest.TestCase):
    def test_names_round_trip(self) -> None:
        self.assertEqual(wiki_folder_name("a_docx.md"), "a.docx")
        self.assertEqual(wiki_folder_name("my_file_pptx.md"), "my_file.pptx")
        self.assertEqual(wiki_folder_name("notes.md"), "notes")
        self.assertEqual(raw_name_for("a.docx"), "a_docx.md")
        self.assertEqual(raw_name_for("Report.PDF"), "Report_pdf.md")

    def test_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Project(Path(directory)).ensure()
            self.assertEqual(
                project.wiki_dir("f1/a_docx.md"), project.wiki / "f1" / "a.docx"
            )
            self.assertEqual(
                project.state_dir("f1/a_docx.md"),
                project.metadata / "state" / "f1" / "a.docx",
            )
            (project.raw / "f1").mkdir()
            (project.raw / "f1" / "a_docx.md").write_text("x", encoding="utf-8")
            (project.raw / ".git").mkdir()
            (project.raw / ".git" / "junk.md").write_text("x", encoding="utf-8")
            self.assertEqual(project.raw_files(), ["f1/a_docx.md"])
