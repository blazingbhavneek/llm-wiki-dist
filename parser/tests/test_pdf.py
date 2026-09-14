from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from formats.base import ParseOptions
from formats.pdf import PdfParser, run_mineru

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakePopen:
    """Minimal stand-in for subprocess.Popen used by run_mineru."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.pid = 4242

    def communicate(self, timeout=None):
        return self._stdout, self._stderr


class FakeWorkers:
    def __init__(self) -> None:
        self.network_calls = 0

    async def run_gpu(self, fn, pdf_path: str, output_dir: str) -> str:
        self.asserted_pdf = Path(pdf_path).read_bytes()
        markdown_dir = Path(output_dir) / "document" / "auto"
        image_dir = markdown_dir / "images"
        image_dir.mkdir(parents=True)
        (image_dir / "chart.png").write_bytes(_PNG_BYTES)
        markdown_path = markdown_dir / "document.md"
        markdown_path.write_text(
            "# Report\n\n![Chart](images/chart.png)\n\n"
            "Repeated: ![Chart again](images/chart.png)\n",
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


class PdfParserTests(unittest.IsolatedAsyncioTestCase):
    def test_detects_pdf_header_near_start(self) -> None:
        self.assertTrue(PdfParser.detect(b"prefix\n%PDF-1.7\n"))
        self.assertFalse(PdfParser.detect(b"not a pdf"))

    def test_mineru_uses_pipeline_backend_and_configured_gpu(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "document.pdf"
            output_dir = root / "output"
            markdown_path = output_dir / "document" / "auto" / "document.md"
            markdown_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"%PDF-1.7 fake")
            markdown_path.write_text("result", encoding="utf-8")

            environment = {
                "MINERU_COMMAND": "mineru",
                "MINERU_BACKEND": "pipeline",
                "MINERU_CUDA_VISIBLE_DEVICES": "1",
                "MINERU_GPU_MEMORY_UTILIZATION": "0.1",
                "MINERU_PROCESSING_WINDOW_SIZE": "4",
                "MINERU_API_URL": "",
            }
            popen = FakePopen(returncode=0, stdout="result", stderr="")
            with (
                patch.dict(os.environ, environment, clear=False),
                patch("formats.pdf.subprocess.Popen", return_value=popen) as invoke,
            ):
                result = run_mineru(str(pdf_path), str(output_dir))

        self.assertEqual(result, str(markdown_path))
        args, call_options = invoke.call_args
        self.assertIn("-b", args[0])
        self.assertIn("pipeline", args[0])
        # vLLM-only knob must not be passed to the pipeline backend
        self.assertNotIn("--gpu-memory-utilization", args[0])
        self.assertEqual(call_options["env"]["CUDA_VISIBLE_DEVICES"], "1")
        self.assertEqual(call_options["env"]["MINERU_PROCESSING_WINDOW_SIZE"], "4")

    async def test_gpu_extract_then_describes_unique_images_in_parallel_stage(self) -> None:
        workers = FakeWorkers()
        parser = PdfParser()
        options = ParseOptions(
            images=True,
            describe_images=True,
            llm_base_url="http://override/v1",
            llm_api_key="override-key",
            llm_model="override-model",
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
                ParseOptions(images=False, describe_images=True),
                workers,
            )

        self.assertNotIn("base64", result.markdown)
        self.assertIn("Detailed description for Chart.", result.markdown)


if __name__ == "__main__":
    unittest.main()
