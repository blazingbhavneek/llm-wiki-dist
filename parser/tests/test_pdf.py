from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from formats.base import ParseOptions, ParseProfile
from formats.pdf import MineruError, PdfParser, run_mineru

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeWorkers:
    def __init__(self) -> None:
        self.network_calls = 0

    async def run_gpu(self, fn, pdf_path: str, output_dir: str) -> str:
        import json

        self.asserted_pdf = Path(pdf_path).read_bytes()
        markdown_dir = Path(output_dir) / "document" / "auto"
        image_dir = markdown_dir / "images"
        image_dir.mkdir(parents=True)
        (image_dir / "chart.png").write_bytes(_PNG_BYTES)
        markdown_path = markdown_dir / "document.md"
        # Two-page document with content-list artifact and page-boundary
        # markers so both generic (content_list) and llm-wiki (marker) paths
        # receive deterministic, backend-honest page boundaries.
        markdown_path.write_text(
            "# Report\n\n"
            "## PDF ページ 1\n\n"
            "![Chart](images/chart.png)\n\n"
            "## PDF ページ 2\n\n"
            "![Chart](images/chart.png)\n",
            encoding="utf-8",
        )
        content = [
            {"type": "text", "text": "Report", "text_level": 1, "page_idx": 0},
            {
                "type": "image",
                "img_path": "images/chart.png",
                "img_caption": ["Chart"],
                "page_idx": 0,
            },
            {
                "type": "image",
                "img_path": "images/chart.png",
                "img_caption": ["Chart"],
                "page_idx": 1,
            },
        ]
        (markdown_dir / "document_content_list.json").write_text(
            json.dumps(content, ensure_ascii=False), encoding="utf-8"
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


class PdfParserTests(unittest.IsolatedAsyncioTestCase):
    def test_detects_pdf_header_near_start(self) -> None:
        self.assertTrue(PdfParser.detect(b"prefix\n%PDF-1.7\n"))
        self.assertFalse(PdfParser.detect(b"not a pdf"))

    def test_mineru_uses_configured_v4_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "document.pdf"
            output_dir = root / "output"
            markdown_path = output_dir / "document" / "auto" / "document.md"
            markdown_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"%PDF-1.7 fake")
            markdown_path.write_text("result", encoding="utf-8")

            environment = {
                "MINERU_API_URL": "http://mineru.example:8000",
                "MINERU_API_TIER": "flash",
            }
            with (
                patch.dict(os.environ, environment, clear=False),
                patch(
                    "formats.pdf._run_mineru_api_once",
                    return_value=str(markdown_path),
                ) as invoke,
            ):
                result = run_mineru(str(pdf_path), str(output_dir))

        self.assertEqual(result, str(markdown_path))
        invoke.assert_called_once_with(str(pdf_path), str(output_dir))

    def test_mineru_retries_once_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "document.pdf"
            output_dir = root / "output"
            markdown_path = output_dir / "document" / "auto" / "document.md"
            markdown_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"%PDF-1.7 fake")
            markdown_path.write_text("result", encoding="utf-8")

            with (
                patch.dict(
                    os.environ,
                    {"MINERU_API_URL": "http://mineru.example:8000"},
                    clear=False,
                ),
                patch(
                    "formats.pdf._run_mineru_api_once",
                    side_effect=[
                        MineruError("temporary failure"),
                        str(markdown_path),
                    ],
                ) as invoke,
                patch("formats.pdf.time.sleep") as sleep,
            ):
                result = run_mineru(str(pdf_path), str(output_dir))

        self.assertEqual(result, str(markdown_path))
        self.assertEqual(invoke.call_count, 2)
        sleep.assert_called_once_with(10)

    def test_mineru_requires_external_api_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"MINERU_API_URL": ""}, clear=False):
                with self.assertRaisesRegex(MineruError, "MINERU_API_URL"):
                    run_mineru(str(Path(directory) / "document.pdf"), directory)

    async def test_gpu_extract_then_describes_unique_images_in_parallel_stage(self) -> None:
        workers = FakeWorkers()
        parser = PdfParser()
        options = ParseOptions(
            images=True,
            describe_images=True,
            llm_base_url="http://override/v1",
            llm_api_key="override-key",
            llm_model="override-model",
            profile=ParseProfile.LLM_WIKI,
        )
        FakeLLMClient.configurations.clear()

        with patch("formats.pdf.LLMClient", FakeLLMClient):
            result = await parser.parse(b"%PDF-1.7 fake", options, workers)

        self.assertEqual(workers.asserted_pdf, b"%PDF-1.7 fake")
        self.assertEqual(workers.network_calls, 1)
        self.assertEqual(result.image_count, 2)
        self.assertEqual(result.markdown.count("<image-unit>"), 2)
        self.assertEqual(result.markdown.count("data:image/png;base64,"), 2)
        self.assertEqual(result.markdown.count("Detailed description for Chart."), 2)
        self.assertEqual(len(result.pages), 2)
        self.assertTrue(all("<image-unit>" in page for page in result.pages))
        self.assertTrue(
            all("Detailed description for Chart." in page for page in result.pages)
        )
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

    async def test_images_off_keeps_description_but_removes_base64(self) -> None:
        workers = FakeWorkers()
        parser = PdfParser()

        with patch("formats.pdf.LLMClient", FakeLLMClient):
            result = await parser.parse(
                b"%PDF-1.7 fake",
                ParseOptions(
                    images=False,
                    describe_images=True,
                    profile=ParseProfile.LLM_WIKI,
                ),
                workers,
            )

        self.assertNotIn("base64", result.markdown)
        self.assertIn("Detailed description for Chart.", result.markdown)


if __name__ == "__main__":
    unittest.main()
