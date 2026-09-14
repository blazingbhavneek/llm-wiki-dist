from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from formats import detect
from formats.base import ParseOptions
from formats.csv import CsvParser, CsvError, run_csv


class FakeWorkers:
    async def run_external(self, fn, csv_path: str, output_dir: str) -> str:
        return fn(csv_path, output_dir)


class CsvDetectionTests(unittest.TestCase):
    def test_accepts_delimited_grids(self) -> None:
        for payload in (
            b"name,age,city\nAlice,30,NYC\nBob,25,LA\n",
            b"id\tvalue\tnote\n1\t10\tok\n2\t20\tfine\n",
            b"a;b;c\n1;2;3\n4;5;6\n",
        ):
            self.assertTrue(CsvParser.detect(payload), payload)

    def test_rejects_non_tabular_text_and_binary(self) -> None:
        for payload in (
            b"",
            b'{"a": 1, "b": [2, 3]}',
            b"<?xml version='1.0'?><r/>",
            b"# Heading\n\nA paragraph, with commas, in it.\nAnother sentence here.\n",
            b"2024-01-01 INFO up\n2024-01-01 ERROR down\n",
            b"single\ncolumn\nvalues\n",
            b"\x89PNG\r\n\x00\x00binary",
        ):
            self.assertFalse(CsvParser.detect(payload), payload)

    def test_not_claimed_when_another_parser_matches(self) -> None:
        # a DOCX (zip) must never fall through to the CSV sniffer
        docx_like = b"PK\x03\x04" + b"\x00" * 40
        self.assertFalse(CsvParser.detect(docx_like))


class CsvRenderTests(unittest.TestCase):
    def _render(self, text: bytes) -> str:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src = root / "input.csv"
            src.write_bytes(text)
            return Path(run_csv(str(src), str(root / "out"))).read_text(encoding="utf-8")

    def test_first_row_is_html_header_and_content_is_escaped(self) -> None:
        md = self._render(b'a,b\n"<x>&|y",2\n3,4\n')
        self.assertIn("<tr><th>a</th><th>b</th></tr>", md)
        self.assertIn("<tr><td>&lt;x&gt;&amp;|y</td><td>2</td></tr>", md)
        self.assertNotIn("style=", md)
        self.assertNotIn("| ---", md)

    def test_occasional_short_row_is_padded(self) -> None:
        # one short row among many rectangular rows: detection still succeeds,
        # rendering pads the gap
        body = b"".join(b"r%d,x,y\n" % i for i in range(10))
        md = self._render(b"a,b,c\n" + body + b"last,only\n")
        self.assertIn("<tr><th>a</th><th>b</th><th>c</th></tr>", md)
        self.assertIn("<tr><td>last</td><td>only</td><td></td></tr>", md)

    def test_row_cap_adds_a_notice(self) -> None:
        import os

        rows = b"h1,h2\n" + b"\n".join(b"%d,x" % i for i in range(50)) + b"\n"
        os.environ["CSV_MAX_ROWS"] = "10"
        try:
            md = self._render(rows)
        finally:
            del os.environ["CSV_MAX_ROWS"]
        self.assertIn("Showing the first 10 of 50 data rows", md)

    def test_run_csv_rejects_non_tabular(self) -> None:
        with self.assertRaises(CsvError):
            self._render(b"just one sentence, nothing tabular about it really\n")


class CsvExtractTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_returns_html_table(self) -> None:
        data = b"product,qty\nwidget,10\ngadget,4\n"
        self.assertIs(detect(data), CsvParser)
        result = await CsvParser().parse(data, ParseOptions(), FakeWorkers())
        self.assertEqual(result.parser, "csv")
        self.assertEqual(result.image_count, 0)
        self.assertIn("<tr><th>product</th><th>qty</th></tr>", result.markdown)
        self.assertIn("<tr><td>widget</td><td>10</td></tr>", result.markdown)


if __name__ == "__main__":
    unittest.main()
