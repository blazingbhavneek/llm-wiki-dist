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
        self.assertIn("機械的な全文転載", prompt)
        self.assertIn("主要エンティティ", prompt)
        self.assertIn("Title\nImage description: a rising chart", prompt)

    async def test_slide_revision_continues_the_previous_conversation(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "Revised draft."}}]},
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
                "Slide context",
                [
                    ("First draft.", "Missing entity A and relationship A to B."),
                    ("Second draft.", "Missing relationship B to C."),
                ],
            )
        finally:
            await http_client.aclose()

        messages = captured["body"]["messages"]
        self.assertEqual(result, "Revised draft.")
        self.assertEqual(
            [message["role"] for message in messages],
            ["user", "assistant", "user", "assistant", "user"],
        )
        self.assertEqual(messages[1]["content"], "First draft.")
        self.assertIn("Missing entity A", messages[2]["content"])
        self.assertEqual(messages[3]["content"], "Second draft.")
        self.assertIn("Missing relationship B to C", messages[4]["content"])

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

    async def test_slide_judge_returns_structured_score_and_feedback(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '```json\n{"score": 87, '
                                    '"missing_entities": ["profit target"], '
                                    '"missing_relationships": ["A feeds B"], '
                                    '"feedback": "add the omitted path"}\n```'
                                )
                            }
                        }
                    ]
                },
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
            review = await client.judge_slide_description(
                "data:image/png;base64,AAAA",
                "Title and individual image descriptions",
                "Candidate description",
            )
        finally:
            await http_client.aclose()

        prompt = captured["body"]["messages"][0]["content"][0]["text"]
        self.assertEqual(review.score, 87)
        self.assertEqual(review.missing_entities, ("profit target",))
        self.assertEqual(review.missing_relationships, ("A feeds B",))
        self.assertIn("profit target", review.missing)
        self.assertIn("A feeds B", review.missing)
        self.assertIn("add the omitted path", review.missing)
        self.assertIn("Title and individual image descriptions", prompt)
        self.assertIn("Candidate description", prompt)
        self.assertIn("主要エンティティ", prompt)
