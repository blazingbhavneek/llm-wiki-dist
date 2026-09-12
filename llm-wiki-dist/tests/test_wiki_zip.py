import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from graph.librarian import Librarian
from graph.project import Project, zip_wiki


class WikiExportTests(unittest.TestCase):
    def test_flat_wiki_folder_is_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "a.docx"
            folder.mkdir()
            (folder / "001-a.md").write_text("# A\n\nbody\n", encoding="utf-8")
            planning = folder / "_planning"
            planning.mkdir()
            (planning / "metadata.json").write_text(
                '{"original_file_name":"a_docx.md","files":[{"name":"a.md","header":"H"}]}',
                encoding="utf-8",
            )
            (planning / "coverage.json").write_text(
                '{"files":[{"filename":"a.md","title":"A","source_start":1,"source_end":2}]}',
                encoding="utf-8",
            )
            (planning / "manifest.json").write_text(
                '{"planning":{"ingest_mode":"pages"}}', encoding="utf-8"
            )
            nodes, _edges = Librarian._load_md_output(Librarian.__new__(Librarian), folder)
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0].title, "A")

    def test_zip_omits_planning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Project(Path(directory)).ensure()
            page = project.wiki / "f1" / "a.docx"
            page.mkdir(parents=True)
            (page / "001-a.md").write_text("# a\n", encoding="utf-8")
            (page / "_planning").mkdir()
            (page / "_planning" / "x.json").write_text("{}", encoding="utf-8")
            (project.wiki / "index.md").write_text("# Wiki\n", encoding="utf-8")
            with zipfile.ZipFile(io.BytesIO(zip_wiki(project))) as archive:
                self.assertEqual(archive.namelist(), ["f1/a.docx/001-a.md", "index.md"])

    def test_growi_pages_from_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Project(Path(directory)).ensure()
            page = project.wiki / "f1" / "a.docx"
            page.mkdir(parents=True)
            (page / "001-a.md").write_text("# a\n", encoding="utf-8")
            (page / "_planning").mkdir()
            (page / "_planning" / "x.json").write_text("{}", encoding="utf-8")
            result = Librarian.growi_pages_from_wiki(
                object(), project.wiki, "/wiki"
            )
            self.assertEqual(result, [{"path": "/wiki/f1/a.docx/001-a", "body": "# a\n"}])
