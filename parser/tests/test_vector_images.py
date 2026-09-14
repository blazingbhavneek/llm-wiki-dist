from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.vector_images import (
    VectorConversionError,
    convert_document_vector_images,
    convert_vector_images,
    find_vector_images,
)

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeWorkers:
    def __init__(self) -> None:
        self.external_functions = []

    async def run_external(self, fn, *args):
        self.external_functions.append(fn)
        return fn(*args)


def _fake_libreoffice(converted_suffixes: set[str]):
    """Return a subprocess.run side effect emulating headless PNG output.

    Real LibreOffice writes ``<outdir>/<stem>.png`` for every input; this
    fake parses ``--outdir``/inputs from the argument list and does the same.
    """

    def run(args, **options):
        out_dir = Path(args[args.index("--outdir") + 1])
        inputs = [Path(value) for value in args[args.index("--outdir") + 2 :]]
        for source in inputs:
            if source.suffix.lower() in converted_suffixes:
                (out_dir / f"{source.stem}.png").write_bytes(_PNG_BYTES)
        return subprocess.CompletedProcess(args, 0, "convert ok", "")

    return run


class FindVectorImagesTests(unittest.TestCase):
    def test_finds_emf_and_wmf_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            nested = root / "media"
            nested.mkdir(parents=True)
            (nested / "image1.emf").write_bytes(b"emf")
            (nested / "image2.wmf").write_bytes(b"wmf")
            (nested / "image3.PNG").write_bytes(b"png")
            (root / "document.md").write_text("x", encoding="utf-8")

            found = find_vector_images(root)

        self.assertEqual([p.name for p in found], ["image1.emf", "image2.wmf"])


class ConvertVectorImagesTests(unittest.TestCase):
    def test_batch_conversion_uses_isolated_profile_and_renames_collisions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "media"
            media.mkdir()
            (media / "image1.emf").write_bytes(b"emf")
            (media / "image1.wmf").write_bytes(b"wmf")  # same stem -> unique name
            (media / "photo.png").write_bytes(_PNG_BYTES)

            completed = subprocess.CompletedProcess([], 0, "converted", "")
            with (
                patch(
                    "utils.vector_images.subprocess.run",
                    side_effect=_fake_libreoffice({".emf", ".wmf"}),
                ) as invoke,
            ):
                converted = convert_vector_images(str(media), ["libreoffice"])

        args, options = invoke.call_args
        self.assertIn("--headless", args[0])
        self.assertIn("png", args[0][args[0].index("--convert-to") + 1])
        self.assertTrue(
            any(value.startswith("-env:UserInstallation=file:") for value in args[0])
        )
        self.assertEqual(options["timeout"], 300.0)

        converted_by_source = {Path(a).name: Path(b).name for a, b in converted}
        self.assertEqual(
            converted_by_source,
            {"image1.emf": "image1.png", "image1.wmf": "image1.wmf.png"},
        )

    def test_missing_png_output_keeps_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "media"
            media.mkdir()
            (media / "broken.emf").write_bytes(b"emf")

            with patch(
                "utils.vector_images.subprocess.run",
                side_effect=_fake_libreoffice(set()),
            ):
                converted = convert_vector_images(str(media), ["libreoffice"])

        self.assertEqual(converted, [])

    def test_nonzero_exit_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "media"
            media.mkdir()
            (media / "image1.emf").write_bytes(b"emf")

            failed = subprocess.CompletedProcess([], 1, "", "boom")
            with patch("utils.vector_images.subprocess.run", return_value=failed):
                with self.assertRaises(VectorConversionError):
                    convert_vector_images(str(media), ["libreoffice"])


class ConvertDocumentVectorImagesTests(unittest.IsolatedAsyncioTestCase):
    def _make_media(self, root: Path) -> None:
        media = root / "media" / "media"
        media.mkdir(parents=True)
        (media / "image1.emf").write_bytes(b"emf")
        (media / "image2.wmf").write_bytes(b"wmf")

    async def test_converts_and_rewrites_markdown_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_media(root)
            markdown = (
                "![](media/media/image1.emf)\n\n"
                '<img src="media/media/image2.wmf" alt="diagram">'
            )
            workers = FakeWorkers()

            with (
                patch(
                    "utils.vector_images.find_libreoffice_command",
                    return_value=["libreoffice"],
                ),
                patch(
                    "utils.vector_images.subprocess.run",
                    side_effect=_fake_libreoffice({".emf", ".wmf"}),
                ),
            ):
                rewritten = await convert_document_vector_images(
                    markdown, root / "media", workers
                )

            self.assertEqual(workers.external_functions, [convert_vector_images])
            self.assertIn("media/media/image1.png", rewritten)
            self.assertIn("media/media/image2.png", rewritten)
            self.assertNotIn(".emf", rewritten)
            self.assertNotIn(".wmf", rewritten)
            self.assertTrue((root / "media/media/image1.png").is_file())
            self.assertTrue((root / "media/media/image2.png").is_file())

    async def test_auto_mode_without_libreoffice_keeps_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_media(root)
            markdown = "![](media/media/image1.emf)"

            with patch(
                "utils.vector_images.find_libreoffice_command", return_value=None
            ):
                rewritten = await convert_document_vector_images(
                    markdown, root / "media", FakeWorkers()
                )

        self.assertEqual(rewritten, markdown)

    async def test_required_mode_without_libreoffice_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_media(root)

            with (
                patch.dict(os.environ, {"VECTOR_IMAGE_CONVERSION": "required"}),
                patch(
                    "utils.vector_images.find_libreoffice_command", return_value=None
                ),
            ):
                with self.assertRaises(VectorConversionError):
                    await convert_document_vector_images(
                        "![](media/media/image1.emf)", root / "media", FakeWorkers()
                    )

    async def test_disabled_mode_skips_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workers = FakeWorkers()
            with patch.dict(os.environ, {"VECTOR_IMAGE_CONVERSION": "false"}):
                rewritten = await convert_document_vector_images(
                    "![](media/media/image1.emf)", Path(directory), workers
                )

        self.assertEqual(rewritten, "![](media/media/image1.emf)")
        self.assertEqual(workers.external_functions, [])

    async def test_auto_mode_tolerates_conversion_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_media(root)
            markdown = "![](media/media/image1.emf)"

            failed = subprocess.CompletedProcess([], 1, "", "crashed")
            with (
                patch(
                    "utils.vector_images.find_libreoffice_command",
                    return_value=["libreoffice"],
                ),
                patch("utils.vector_images.subprocess.run", return_value=failed),
            ):
                rewritten = await convert_document_vector_images(
                    markdown, root / "media", FakeWorkers()
                )

        self.assertEqual(rewritten, markdown)

    @unittest.skipIf(
        shutil.which("libreoffice") is None and shutil.which("soffice") is None,
        "LibreOffice is not installed",
    )
    async def test_real_libreoffice_round_trip(self) -> None:
        """Generate a WMF via LibreOffice, then convert it back to a real PNG."""
        executable = shutil.which("libreoffice") or shutil.which("soffice")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            svg = root / "sample.svg"
            svg.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
                '<rect width="64" height="64" fill="red"/></svg>',
                encoding="utf-8",
            )
            home = root / "home"
            home.mkdir()
            generated = subprocess.run(
                [executable, "--headless", f"-env:UserInstallation={(root / 'p1').as_uri()}",
                 "--convert-to", "wmf", "--outdir", str(root), str(svg)],
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "HOME": str(home)},
                check=False,
            )
            if generated.returncode or not (root / "sample.wmf").is_file():
                self.skipTest("could not generate a sample WMF")

            media = root / "media"
            media.mkdir()
            shutil.move(root / "sample.wmf", media / "image1.wmf")

            converted = convert_vector_images(str(media), [executable])

            self.assertEqual(len(converted), 1)
            png = Path(converted[0][1])
            self.assertEqual(png.suffix, ".png")

            from PIL import Image

            with Image.open(png) as image:
                image.verify()


if __name__ == "__main__":
    unittest.main()
