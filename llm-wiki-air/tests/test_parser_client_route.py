from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

import graph.workspace.parser_client as parser_client
from graph.workspace.parser_client import LLM_WIKI_PARSE_PATH, parse_document


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _Settings:
    chat_base_url = "http://llm/v1"
    chat_api_key = "k"
    chat_model = "m"


class ParserClientTests(unittest.TestCase):
    def test_uses_the_llm_wiki_route(self) -> None:
        self.assertEqual(LLM_WIKI_PARSE_PATH, "/parse/llm-wiki")

        stream = io.BytesIO()
        workbook = Workbook()
        workbook.active["A1"] = 1
        workbook.save(stream)
        workbook.close()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.xlsx"
            path.write_bytes(stream.getvalue())
            with patch.object(
                parser_client.requests,
                "post",
                return_value=_Resp({"markdown": "# x", "pages": ["## foo"]}),
            ) as post:
                result = parse_document(
                    path,
                    base_url="http://parser/agent/doc-parser",
                    settings=_Settings(),
                )

            self.assertEqual(result, "# x")
        self.assertEqual(
            post.call_args.args[0],
            "http://parser/agent/doc-parser/parse/llm-wiki",
        )

    def test_rejects_malformed_pages(self) -> None:
        stream = io.BytesIO()
        Workbook().save(stream)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.xlsx"
            path.write_bytes(stream.getvalue())
            with patch.object(
                parser_client.requests,
                "post",
                return_value=_Resp({"markdown": "# x", "pages": "not-a-list"}),
            ):
                with self.assertRaisesRegex(RuntimeError, "pages must be a list"):
                    parse_document(
                        path,
                        base_url="http://parser",
                        settings=_Settings(),
                    )


if __name__ == "__main__":
    unittest.main()
