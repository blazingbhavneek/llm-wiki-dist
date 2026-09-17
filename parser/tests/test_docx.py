from __future__ import annotations

import asyncio
import base64
import io
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from formats import detect
from formats.base import ParseOptions, ParseProfile
from formats.docx import DocxParser, run_pandoc

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def make_office_zip(*names: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, b"content")
    return buffer.getvalue()


class FakeWorkers:
    def __init__(self) -> None:
        self.external_calls = 0
        self.network_calls = 0

    async def run_external(self, fn, *args):
        self.external_calls += 1
        if fn is not run_pandoc:
            return fn(*args)
        docx_path, output_dir = args
        self.external_function = fn
        self.document_bytes = Path(docx_path).read_bytes()

        destination = Path(output_dir)
        media = destination / "media" / "media"
        media.mkdir(parents=True)
        (media / "image1.png").write_bytes(_PNG_BYTES)
        markdown_path = destination / "document.md"
        markdown_path.write_text(
            "# Notes\n\n![Architecture](media/media/image1.png)\n",
            encoding="utf-8",
        )
        return str(markdown_path)

    async def run_network(self, fn, *args, **kwargs):
        self.network_calls += 1
        return await fn(*args, **kwargs)


class FakeLLMClient:
    configurations: ClassVar[list[dict]] = []

    def __init__(self, **kwargs) -> None:
        self.configurations.append(kwargs)

    async def describe_image(self, data_url: str, alt_text: str) -> str:
        await asyncio.sleep(0)
        assert data_url.startswith("data:image/png;base64,")
        return f"Detailed description for {alt_text}."

    async def close(self) -> None:
        return None


class DocxParserTests(unittest.IsolatedAsyncioTestCase):
    def test_detects_docx_without_claiming_other_zip_formats(self) -> None:
        docx = make_office_zip("[Content_Types].xml", "word/document.xml")
        xlsx = make_office_zip("[Content_Types].xml", "xl/workbook.xml")

        self.assertTrue(DocxParser.detect(docx))
        self.assertIs(detect(docx), DocxParser)
        self.assertFalse(DocxParser.detect(xlsx))
        self.assertFalse(DocxParser.detect(b"PK not really a zip"))

    def test_pandoc_extracts_gfm_and_media_in_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docx_path = root / "source.docx"
            output_dir = root / "output"
            markdown_path = output_dir / "document.md"
            output_dir.mkdir()
            docx_path.write_bytes(b"fake docx")
            markdown_path.write_text("result", encoding="utf-8")

            completed = subprocess.CompletedProcess([], 0, "", "")
            with (
                patch.dict(os.environ, {"PANDOC_COMMAND": "pandoc"}, clear=False),
                patch("formats.docx.subprocess.run", return_value=completed) as invoke,
            ):
                result = run_pandoc(str(docx_path), str(output_dir))

        self.assertEqual(result, str(markdown_path))
        args, call_options = invoke.call_args
        self.assertIn("--from=docx", args[0])
        self.assertIn("--to=gfm", args[0])
        self.assertIn("--extract-media=media", args[0])
        self.assertEqual(call_options["cwd"], output_dir)
        self.assertEqual(call_options["timeout"], 300.0)

    async def test_external_conversion_then_description_and_embedding(self) -> None:
        document = make_office_zip("[Content_Types].xml", "word/document.xml")
        workers = FakeWorkers()
        options = ParseOptions(
            images=True,
            describe_images=True,
            llm_base_url="http://override/v1",
            llm_api_key="override-key",
            llm_model="override-model",
            profile=ParseProfile.LLM_WIKI,
        )
        FakeLLMClient.configurations.clear()

        with patch("formats.docx.LLMClient", FakeLLMClient):
            result = await DocxParser().parse(document, options, workers)

        self.assertEqual(workers.external_calls, 1)
        self.assertIs(workers.external_function, run_pandoc)
        self.assertEqual(workers.document_bytes, document)
        self.assertEqual(workers.network_calls, 1)
        self.assertEqual(result.parser, "docx")
        self.assertEqual(result.image_count, 1)
        self.assertIn("data:image/png;base64,", result.markdown)
        self.assertIn("Detailed description for Architecture.", result.markdown)
        self.assertEqual(
            FakeLLMClient.configurations,
            [
                {
                    "base_url": "http://override/v1",
                    "api_key": "override-key",
                    "model": "override-model",
                }
            ],
        )

    async def test_descriptions_can_be_disabled(self) -> None:
        document = make_office_zip("[Content_Types].xml", "word/document.xml")
        workers = FakeWorkers()

        with patch("formats.docx.LLMClient", side_effect=AssertionError):
            result = await DocxParser().parse(
                document,
                ParseOptions(
                    images=True,
                    describe_images=False,
                    profile=ParseProfile.LLM_WIKI,
                ),
                workers,
            )

        self.assertEqual(workers.network_calls, 0)
        self.assertEqual(result.image_count, 1)
        self.assertIn("data:image/png;base64,", result.markdown)
        self.assertIn("<image-description></image-description>", result.markdown)


if __name__ == "__main__":
    unittest.main()
