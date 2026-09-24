from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import formats
from formats.base import ExtractedDocument, ParseOptions
from formats.docx import DocxParser
from formats.legacy_office import LegacyOfficeParser, convert_legacy_office

SAMPLES = Path(__file__).parent / "samples"


class LegacyOfficeTests(unittest.TestCase):
    def test_detects_word_and_excel_97(self) -> None:
        word = (SAMPLES / "legacy_word97.doc").read_bytes()
        excel = (SAMPLES / "legacy_excel97.xls").read_bytes()
        docx = (SAMPLES / "docx_contains_pictures.docx").read_bytes()

        self.assertTrue(LegacyOfficeParser.detect(word))
        self.assertTrue(LegacyOfficeParser.detect(excel))
        self.assertFalse(LegacyOfficeParser.detect(docx))
        self.assertFalse(LegacyOfficeParser.detect(b"%PDF-1.7"))
        self.assertFalse(LegacyOfficeParser.detect(b"\xd0\xcf\x11\xe0" + b"\x00" * 600))
        self.assertIs(formats.detect(word), LegacyOfficeParser)
        self.assertIs(formats.detect(excel), LegacyOfficeParser)

    def test_extract_converts_then_delegates(self) -> None:
        fixture = SAMPLES / "docx_contains_pictures.docx"
        expected = fixture.read_bytes()

        class FakeWorkers:
            async def run_external(self, fn, *args):
                self_fn = fn
                if self_fn is not convert_legacy_office:
                    raise AssertionError(fn)
                source = Path(args[0])
                output = source.parent / "converted" / f"{source.stem}.docx"
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(fixture, output)
                return str(output)

        with tempfile.TemporaryDirectory() as tmp:
            parser = LegacyOfficeParser()
            with (
                patch("formats.legacy_office.find_libreoffice_command", return_value=["soffice"]),
                patch.object(DocxParser, "_extract", new_callable=AsyncMock, return_value=ExtractedDocument(markdown="# ok")) as extract,
            ):
                result = asyncio.run(parser._extract(
                    (SAMPLES / "legacy_word97.doc").read_bytes(),
                    tmp,
                    ParseOptions(filename="legacy_word97.doc"),
                    FakeWorkers(),
                ))

        self.assertEqual(result.markdown, "# ok")
        self.assertEqual(extract.await_args.args[0], expected)
        self.assertEqual(extract.await_args.args[2].filename, "legacy_word97.docx")

    @unittest.skipUnless(shutil.which("soffice") or shutil.which("libreoffice"), "LibreOffice is required")
    def test_real_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "legacy.xls"
            shutil.copyfile(SAMPLES / "legacy_excel97.xls", source)
            target = Path(convert_legacy_office(str(source), "xlsx", [shutil.which("soffice") or shutil.which("libreoffice")]))
            self.assertTrue(zipfile.is_zipfile(target))
            with zipfile.ZipFile(target) as archive:
                self.assertIn("xl/workbook.xml", archive.namelist())


if __name__ == "__main__":
    unittest.main()
