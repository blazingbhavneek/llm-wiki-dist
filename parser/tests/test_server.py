from __future__ import annotations

import asyncio
import json
import re
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from formats.base import ParseResult
from server import _stream_json_job


class StreamJsonTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeats_leave_complete_body_as_valid_json(self) -> None:
        async def finish_later() -> ParseResult:
            await asyncio.sleep(0.02)
            return ParseResult(markdown="# Done", parser="pdf", image_count=0)

        task = asyncio.create_task(finish_later())
        with patch("server.HEARTBEAT_INTERVAL_S", 0.001):
            chunks = [chunk async for chunk in _stream_json_job(task)]

        body = "".join(chunks)
        self.assertTrue(body.startswith("\n"))
        self.assertEqual(json.loads(body)["markdown"], "# Done")
        self.assertNotIn(": ping", body)
        self.assertNotIn("event:", body)

    async def test_streamed_failure_is_valid_json(self) -> None:
        async def fail() -> ParseResult:
            raise RuntimeError("conversion broke")

        task = asyncio.create_task(fail())
        body = "".join([chunk async for chunk in _stream_json_job(task)])

        self.assertEqual(
            json.loads(body),
            {"error": "Parse failed: conversion broke"},
        )


class UrlPrefixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prefix_patch = patch("server.URL_PREFIX", "/agent/doc-parser")
        self.prefix_patch.start()
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        self.prefix_patch.stop()

    def test_unprefixed_application_paths_are_not_available(self) -> None:
        for path in ("/", "/random", "/parse", "/assets/index.js"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_prefixed_index_and_assets_are_available(self) -> None:
        response = self.client.get("/agent/doc-parser/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/agent/doc-parser/favicon.svg"', response.text)

        asset_path = re.search(
            r'(?:src|href)="(/agent/doc-parser/assets/[^"]+)"',
            response.text,
        )
        self.assertIsNotNone(asset_path)
        self.assertEqual(self.client.get(asset_path.group(1)).status_code, 200)
        self.assertEqual(
            self.client.get("/agent/doc-parser/favicon.svg").status_code,
            200,
        )

    def test_unknown_prefixed_path_is_not_spa_fallback(self) -> None:
        response = self.client.get("/agent/doc-parser/random")
        self.assertEqual(response.status_code, 404)

    def test_prefixed_parse_reaches_parse_handler(self) -> None:
        response = self.client.post(
            "/agent/doc-parser/parse",
            files={"file": ("unsupported.txt", b"plain text", "text/plain")},
        )
        self.assertEqual(response.status_code, 415)


if __name__ == "__main__":
    unittest.main()
