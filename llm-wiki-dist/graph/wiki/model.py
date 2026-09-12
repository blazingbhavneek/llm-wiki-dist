"""The single seam through which bounded structured model calls happen.

``ChatModelPort`` wraps the existing ``graph.chunk`` LLM plumbing, so the
pipeline needs no new model client.  Tests inject any object with the same
``structured`` coroutine, which is why nothing here imports a test helper.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Protocol, Sequence, runtime_checkable

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from .config import WikiConfig


@runtime_checkable
class ModelPort(Protocol):
    """One bounded structured call. Implementations must respect timeouts."""

    name: str
    provider: str

    async def structured(
        self,
        schema: type[BaseModel],
        messages: Sequence[BaseMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> BaseModel:  # pragma: no cover - protocol declaration
        ...

    async def text(
        self,
        messages: Sequence[BaseMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> str:  # pragma: no cover - protocol declaration
        ...


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


class ChatModelPort:
    """OpenAI-compatible chat endpoint via langchain structured output."""

    def __init__(self, config: WikiConfig, *, llm: Any | None = None) -> None:
        from graph.chunk import make_llm

        self.config = config
        self.llm = llm if llm is not None else make_llm(
            model=config.chat_model,
            base_url=config.chat_base_url,
            api_key=config.chat_api_key,
            temperature=config.temperature,
            timeout=config.request_timeout,
        )
        self.name = config.chat_model
        self.provider = config.chat_base_url

    async def structured(
        self,
        schema: type[BaseModel],
        messages: Sequence[BaseMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> BaseModel:
        from graph.chunk import structured_ainvoke

        return await structured_ainvoke(
            self.llm, schema, list(messages), max_output_tokens=max_output_tokens
        )

    async def text(
        self,
        messages: Sequence[BaseMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> str:
        """One bounded plain-text completion; the caller validates the content."""

        llm = (
            self.llm.bind(max_tokens=max_output_tokens)
            if max_output_tokens is not None
            else self.llm
        )
        reply = await asyncio.wait_for(
            llm.ainvoke(list(messages)), timeout=self.config.request_timeout
        )
        content = getattr(reply, "content", reply)
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return _THINK_RE.sub("", str(content))
