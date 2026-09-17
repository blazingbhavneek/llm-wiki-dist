"""Minimal async client for an OpenAI-compatible local vision LLM."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from typing import Any, Self

import httpx
from dotenv import load_dotenv

load_dotenv()

DEFAULT_BASE_URL = "http://10.160.144.101:51029/v1"
DEFAULT_API_KEY = "local"
DEFAULT_MODEL = "gemma-4-31B"

IMAGE_DESCRIPTION_PROMPT = (
    "このドキュメント内の画像を、詳細かつ事実的に日本語で説明してください。"
    "図、グラフ、表、スクリーンショットなどの意味のある視覚的な関係を解説し、"
    "判読可能なテキストは文字起こししてください。説明文のみを返してください。"
)

SLIDE_SYNTHESIS_PROMPT = (
    "この画像はプレゼンテーションのスライド全体です。以下には、このスライドから"
    "既に抽出したテキスト、表、各画像の位置と個別説明があります。既存テキストの"
    "機械的な全文転載や、個々の画像の独立した再説明はしないでください。ただし、"
    "関係を正確に説明するために必要な主要エンティティの名称とラベルは必ず明記して"
    "ください。スライド全体を見なければ分からない情報を中心に補足してください。"
    "具体的には、"
    "要素の空間的な配置、グループ化、視覚的な階層、矢印や近接関係、テキストと画像の"
    "対応、各要素の役割、そして組み合わせによって伝えようとしている主張・流れ・"
    "結論を漏れなく説明してください。単に「スライドである」「画像とテキストがある」"
    "などの自明な記述は禁止します。抽出内容と矛盾する推測は避けてください。通常は"
    "2〜4個の十分に具体的な段落で、スライドを見ていない読者でも構成と要素間の相互作用"
    "を再現できる説明文のみを返してください。"
)

SLIDE_JUDGE_PROMPT = (
    "あなたはプレゼンテーションのアクセシビリティ説明を厳格に評価する審査員です。"
    "まずスライド画像と抽出済みコンテキストから、主要エンティティを漏れなく確認して"
    "ください。主要エンティティには、名前付きの人物・組織・システム・データ・成果物・"
    "工程・施策・図表・グループ・目標が含まれます。装飾だけのアイコンは除外します。"
    "次に、それらの間の主要な関係を確認してください。関係には、矢印や線の方向、"
    "入力と出力、包含とグループ化、順序と時間軸、対応付け、比較、因果、依存関係、"
    "強調、および最終的な主張への収束が含まれます。その完全な確認結果と候補説明を"
    "照合し、エンティティ網羅性45点、関係網羅性45点、明瞭性10点で0〜100点を付けて"
    "ください。長いだけでは加点せず、重要なエンティティまたは関係が1つでも抜けて"
    "いれば具体的に列挙してください。JSON以外は出力せず、必ず次の形式にしてください: "
    '{"score": 0, "missing_entities": ["..."], '
    '"missing_relationships": ["..."], '
    '"feedback": "次稿で行う具体的な修正"}'
)


class LLMResponseError(RuntimeError):
    """The LLM returned a successful response without usable text."""


class LLMRequestError(RuntimeError):
    """The LLM rejected a request and returned a useful diagnostic."""


@dataclass(frozen=True, slots=True)
class SlideDescriptionReview:
    """A judge score and actionable feedback for one slide description."""

    score: float
    missing: str
    missing_entities: tuple[str, ...] = ()
    missing_relationships: tuple[str, ...] = ()


def _first_env(*names: str, default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None:
            return value
    return default


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _response_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""

    content = choices[0].get("message", {}).get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")).strip()
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return str(content).strip()


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Connection defaults loaded from `.env`, with per-client overrides."""

    base_url: str
    api_key: str
    model: str
    timeout_s: float = 120.0
    max_tokens: int = 1024
    temperature: float = 0.2

    @classmethod
    def from_env(cls) -> LLMConfig:
        return cls(
            base_url=_first_env(
                "LLM_BASE_URL",
                "OPENAI_BASE_URL",
                default=DEFAULT_BASE_URL,
            ),
            api_key=_first_env(
                "LLM_API_KEY",
                "OPENAI_API_KEY",
                default=DEFAULT_API_KEY,
            ),
            model=_first_env(
                "LLM_MODEL",
                "OPENAI_MODEL",
                "WIKI_MODEL",
                default=DEFAULT_MODEL,
            ),
            timeout_s=float(os.getenv("LLM_TIMEOUT_SECONDS", "120")),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1024")),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
        )

    def with_overrides(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> LLMConfig:
        return replace(
            self,
            base_url=self.base_url if base_url is None else base_url,
            api_key=self.api_key if api_key is None else api_key,
            model=self.model if model is None else model,
        )


class LLMClient:
    """Describe images through a local OpenAI-compatible endpoint."""

    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = (config or LLMConfig.from_env()).with_overrides(
            base_url=base_url,
            api_key=api_key,
            model=model,
        )
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_s)
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def describe_image(self, data_url: str, alt_text: str = "") -> str:
        prompt = IMAGE_DESCRIPTION_PROMPT
        if alt_text.strip():
            prompt += f"\nThe document's image alt text is: {alt_text.strip()}"

        return await self._describe_with_prompt(data_url, prompt)

    async def describe_slide(
        self,
        data_url: str,
        context: str,
        revision_history: list[tuple[str, str]] | None = None,
    ) -> str:
        """Explain a slide, continuing prior draft/critique turns when supplied."""
        max_context = max(1, int(os.getenv("PPTX_SLIDE_CONTEXT_MAX_CHARS", "20000")))
        trimmed_context = context.strip()[:max_context]
        prompt = (
            f"{SLIDE_SYNTHESIS_PROMPT}\n\n"
            "--- 抽出済みコンテキスト（繰り返さないこと） ---\n"
            f"{trimmed_context}\n"
            "--- コンテキスト終了 ---"
        )
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url, "detail": "high"},
                    },
                ],
            }
        ]
        for previous_draft, feedback in revision_history or []:
            messages.append({"role": "assistant", "content": previous_draft})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "前稿に対する審査結果は以下です。これは新規作成ではなく改稿です。"
                        "前稿の正確で有用な内容をすべて保持し、不足している主要エンティティ"
                        "と関係を追加して、完成した説明全文だけを返してください。\n"
                        f"{feedback.strip()}"
                    ),
                }
            )
        return await self._complete_messages(messages)

    async def judge_slide_description(
        self,
        data_url: str,
        context: str,
        candidate: str,
    ) -> SlideDescriptionReview:
        """Score whether a candidate captures the slide's visual relationships."""
        max_context = max(1, int(os.getenv("PPTX_SLIDE_CONTEXT_MAX_CHARS", "20000")))
        trimmed_context = context.strip()[:max_context]
        prompt = (
            f"{SLIDE_JUDGE_PROMPT}\n\n"
            "--- 抽出済みコンテキスト ---\n"
            f"{trimmed_context}\n"
            "--- 候補説明 ---\n"
            f"{candidate.strip()}\n"
            "--- 評価対象終了 ---"
        )
        response = await self._describe_with_prompt(data_url, prompt)
        return _parse_slide_review(response)

    async def _describe_with_prompt(self, data_url: str, prompt: str) -> str:
        """Send one image and its task-specific prompt to the vision endpoint."""

        return await self._complete_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "high"},
                        },
                    ],
                }
            ]
        )

    async def _complete_messages(self, messages: list[dict[str, Any]]) -> str:
        """Send a complete conversation to the configured chat endpoint."""

        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        response = await self._client.post(
            _chat_completions_url(self.config.base_url),
            headers=headers,
            json={
                "model": self.config.model,
                "messages": messages,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
                "stream": False,
            },
        )
        if response.is_error:
            detail = response.text.strip()[:2000] or "empty response body"
            raise LLMRequestError(
                f"LLM returned HTTP {response.status_code}: {detail}"
            )
        text = _response_text(response.json())
        if not text:
            raise LLMResponseError("LLM response did not contain a description")
        return text


def _parse_slide_review(value: str) -> SlideDescriptionReview:
    """Parse a judge response while tolerating fenced JSON from local models."""
    match = re.search(r"\{.*\}", value, re.DOTALL)
    if match is None:
        return SlideDescriptionReview(score=0.0, missing=value.strip())
    try:
        payload = json.loads(match.group(0))
        if not isinstance(payload, dict):
            raise ValueError("judge response was not an object")
        score = max(0.0, min(100.0, float(payload.get("score", 0))))
        missing_entities = _string_tuple(payload.get("missing_entities"))
        missing_relationships = _string_tuple(payload.get("missing_relationships"))
        feedback = str(payload.get("feedback", payload.get("missing", ""))).strip()
        feedback_parts = []
        if missing_entities:
            feedback_parts.append(
                "不足している主要エンティティ: " + "、".join(missing_entities)
            )
        if missing_relationships:
            feedback_parts.append(
                "不足している主要な関係: " + "、".join(missing_relationships)
            )
        if feedback:
            feedback_parts.append("修正指示: " + feedback)
        missing = "\n".join(feedback_parts)
    except (TypeError, ValueError, json.JSONDecodeError):
        return SlideDescriptionReview(score=0.0, missing=value.strip())
    return SlideDescriptionReview(
        score=score,
        missing=missing,
        missing_entities=missing_entities,
        missing_relationships=missing_relationships,
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())
