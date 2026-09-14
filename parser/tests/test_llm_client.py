from __future__ import annotations

import json
import unittest

import httpx

from client.llm import LLMClient, LLMConfig, LLMRequestError


class LLMClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_describe_image_uses_configured_openai_compatible_endpoint(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "A detailed chart."}}]},
            )

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(transport=transport)
        client = LLMClient(
            LLMConfig(
                base_url="http://llm.local/v1",
                api_key="secret",
                model="vision-model",
            ),
            http_client=http_client,
        )
        try:
            result = await client.describe_image("data:image/png;base64,AAAA", "chart")
        finally:
            await http_client.aclose()

        self.assertEqual(result, "A detailed chart.")
        self.assertEqual(captured["url"], "http://llm.local/v1/chat/completions")
        self.assertEqual(captured["authorization"], "Bearer secret")
        self.assertEqual(captured["body"]["model"], "vision-model")
        self.assertEqual(
            captured["body"]["messages"][0]["content"][1]["image_url"]["url"],
            "data:image/png;base64,AAAA",
        )

    async def test_describe_slide_uses_extracted_context_and_synthesis_prompt(
        self,
    ) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "A relationship."}}]},
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = LLMClient(
            LLMConfig(
                base_url="http://llm.local/v1",
                api_key="secret",
                model="vision-model",
            ),
            http_client=http_client,
        )
        try:
            result = await client.describe_slide(
                "data:image/png;base64,AAAA",
                "Title\nImage description: a rising chart",
            )
        finally:
            await http_client.aclose()

        prompt = captured["body"]["messages"][0]["content"][0]["text"]
        self.assertEqual(result, "A relationship.")
        self.assertIn("既存テキストの文字起こし・言い換え・要約", prompt)
        self.assertIn("Title\nImage description: a rising chart", prompt)

    async def test_http_error_includes_safe_response_detail(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": {"message": "unsupported image format"}},
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = LLMClient(
            LLMConfig(
                base_url="http://llm.local/v1",
                api_key="secret",
                model="vision-model",
            ),
            http_client=http_client,
        )
        try:
            with self.assertRaisesRegex(
                LLMRequestError,
                "HTTP 400.*unsupported image format",
            ):
                await client.describe_image("data:image/png;base64,AAAA")
        finally:
            await http_client.aclose()
