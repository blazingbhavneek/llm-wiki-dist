import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from graph.convert import UnsupportedDocument, convert_mount, parse_document
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


class ConvertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Project(Path(self.tmp.name)).ensure()
        git(self.project.raw, "init", "-q")
        git(self.project.raw, "config", "user.name", "t")
        git(self.project.raw, "config", "user.email", "t@t")
        (self.project.mount / "f1").mkdir(parents=True)
        for name in ("a.pdf", "b.docx", "c.zip"):
            (self.project.mount / "f1" / name).write_bytes(name.encode())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_conversion_is_idempotent_and_records_unsupported(self) -> None:
        def fake_parse(path, **_kwargs):
            if path.suffix == ".zip":
                raise UnsupportedDocument("nope")
            return "# md\n"

        with mock.patch("graph.convert.parse_document", side_effect=fake_parse) as parse:
            result = convert_mount(
                self.project,
                parser_base_url="http://parser",
                settings=SimpleNamespace(),
            )
            self.assertEqual(result["unsupported"], ["f1/c.zip"])
            self.assertTrue((self.project.raw / "f1/a_pdf.md").exists())
            self.assertTrue((self.project.raw / "f1/b_docx.md").exists())
            self.assertEqual(parse.call_count, 3)
            convert_mount(
                self.project,
                parser_base_url="http://parser",
                settings=SimpleNamespace(),
            )
            self.assertEqual(parse.call_count, 3)

        (self.project.mount / "f1/a.pdf").unlink()
        convert_mount(
            self.project,
            parser_base_url="http://parser",
            settings=SimpleNamespace(),
        )
        self.assertFalse((self.project.raw / "f1/a_pdf.md").exists())

    def test_transient_failure_is_retried(self) -> None:
        calls = []

        def fake_parse(path, **_kwargs):
            calls.append(path.name)
            if path.name == "a.pdf" and calls.count("a.pdf") == 1:
                raise RuntimeError("temporary")
            return "# md\n"

        with mock.patch("graph.convert.parse_document", side_effect=fake_parse):
            result = convert_mount(
                self.project,
                parser_base_url="http://parser",
                settings=SimpleNamespace(),
            )
        self.assertIn("f1/a.pdf", result["failed"])
        self.assertTrue((self.project.raw / "f1/b_docx.md").exists())
        with mock.patch("graph.convert.parse_document", side_effect=fake_parse):
            convert_mount(
                self.project,
                parser_base_url="http://parser",
                settings=SimpleNamespace(),
            )
        self.assertEqual(calls.count("a.pdf"), 2)

    def test_parse_document_accepts_heartbeat_whitespace(self) -> None:
        reply = SimpleNamespace(
            status_code=200,
            content=b'\n\n{"markdown": "# x", "parser": "pdf"}',
            text="",
            raise_for_status=lambda: None,
        )
        with mock.patch("graph.convert.requests.post", return_value=reply):
            path = Path(self.tmp.name) / "x.pdf"
            path.write_bytes(b"x")
            self.assertEqual(
                parse_document(path, base_url="http://parser", settings=SimpleNamespace()),
                "# x",
            )

    def test_parse_document_maps_415(self) -> None:
        reply = SimpleNamespace(status_code=415, text="nope")
        with mock.patch("graph.convert.requests.post", return_value=reply):
            path = Path(self.tmp.name) / "x.zip"
            path.write_bytes(b"x")
            with self.assertRaises(UnsupportedDocument):
                parse_document(path, base_url="http://parser", settings=SimpleNamespace())
