"""The single seam through which bounded structured model calls happen.

``ChatModelPort`` wraps the existing ``graph.chunk`` LLM plumbing, so the
pipeline needs no new model client.  Tests inject any object with the same
``structured`` coroutine, which is why nothing here imports a test helper.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from .config import NeoConfig


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


class ChatModelPort:
    """OpenAI-compatible chat endpoint via langchain structured output."""

    def __init__(self, config: NeoConfig, *, llm: Any | None = None) -> None:
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
