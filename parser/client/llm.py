"""Minimal async client for an OpenAI-compatible local vision LLM."""

from __future__ import annotations

import os
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
    "文字起こし・言い換え・要約や、個々の画像の再説明はしないでください。"
    "スライド全体を見なければ分からない情報だけを補足してください。具体的には、"
    "要素の空間的な配置、グループ化、視覚的な階層、矢印や近接関係、テキストと画像の"
    "対応、各要素の役割、そして組み合わせによって伝えようとしている主張・流れ・"
    "結論を説明してください。抽出内容と矛盾する推測は避け、簡潔な日本語の説明文のみ"
    "を返してください。"
)


class LLMResponseError(RuntimeError):
    """The LLM returned a successful response without usable text."""


class LLMRequestError(RuntimeError):
    """The LLM rejected a request and returned a useful diagnostic."""


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

    async def describe_slide(self, data_url: str, context: str) -> str:
        """Explain slide-level relationships without repeating extracted content."""
        max_context = max(1, int(os.getenv("PPTX_SLIDE_CONTEXT_MAX_CHARS", "20000")))
        trimmed_context = context.strip()[:max_context]
        prompt = (
            f"{SLIDE_SYNTHESIS_PROMPT}\n\n"
            "--- 抽出済みコンテキスト（繰り返さないこと） ---\n"
            f"{trimmed_context}\n"
            "--- コンテキスト終了 ---"
        )
        return await self._describe_with_prompt(data_url, prompt)

    async def _describe_with_prompt(self, data_url: str, prompt: str) -> str:
        """Send one image and its task-specific prompt to the vision endpoint."""

        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        response = await self._client.post(
            _chat_completions_url(self.config.base_url),
            headers=headers,
            json={
                "model": self.config.model,
                "messages": [
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
                ],
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
