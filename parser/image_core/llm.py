"""Thin wrapper around langchain-openai.

Two client variants (thinking on/off as timeout fallback), one text ask, one
schema-enforced JSON ask. Structured output rides vLLM guided decoding via
response_format json_schema; if the server rejects that, a guided_json rescue
retry enforces the same schema at the sampler level.
"""

import json
import re

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from image_core.config import ImageConfig

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.DOTALL)


def normalize_base_url(url: str) -> str:
    url = (url or "").rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    return url


def normalize_invoke_url(url: str) -> str:
    """Full chat-completions endpoint, for raw requests.post callers (server.py)."""
    return normalize_base_url(url) + "/chat/completions"


def get_response_text(response) -> str:
    """Extracts plain text from an AIMessage, a raw response dict, or a string."""
    if response is None:
        return ""

    if isinstance(response, str):
        content = response
    elif isinstance(response, dict):
        choices = response.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
        else:
            content = response.get("content", "")
    elif hasattr(response, "content"):
        content = response.content
    else:
        return str(response).strip()

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif hasattr(item, "text"):
                parts.append(item.text)
        content = "\n".join(parts)

    return str(content).strip()


def make_llm(cfg: ImageConfig, *, thinking: bool, timeout: int) -> ChatOpenAI:
    return ChatOpenAI(
        model=cfg.model,
        base_url=normalize_base_url(cfg.base_url),
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        top_p=cfg.top_p,
        timeout=timeout,
        max_retries=1,
        extra_body={"chat_template_kwargs": {"enable_thinking": thinking}},
    )


class Llm:
    """Thinking client with a no-thinking fallback (thinking can outrun the timeout)."""

    def __init__(self, cfg: ImageConfig):
        self.cfg = cfg
        self.thinking = make_llm(cfg, thinking=True, timeout=cfg.thinking_timeout)
        self.plain = make_llm(cfg, thinking=False, timeout=cfg.fallback_timeout)

    async def ask_text(self, content) -> str:
        messages = [{"role": "user", "content": content}]
        chain = self.thinking.with_fallbacks([self.plain])
        return get_response_text(await chain.ainvoke(messages))

    async def ask_json(self, content, schema: type[BaseModel]):
        messages = [{"role": "user", "content": content}]
        try:
            chain = self.thinking.with_structured_output(
                schema, method="json_schema"
            ).with_fallbacks(
                [self.plain.with_structured_output(schema, method="json_schema")]
            )
            result = await chain.ainvoke(messages)
            return result if isinstance(result, schema) else schema.model_validate(result)
        except Exception as exc:
            print(f"[WARN] json_schema structured output failed ({exc}); trying guided_json.")

        # vLLM guided_json rescue: schema enforced by the sampler grammar. Thinking
        # stays on; with a reasoning parser configured the grammar applies after
        # </think>, without one the model just answers in schema directly.
        rescue = make_llm(self.cfg, thinking=True, timeout=self.cfg.thinking_timeout).bind(
            extra_body={
                "chat_template_kwargs": {"enable_thinking": True},
                "guided_json": schema.model_json_schema(),
            }
        )
        text = get_response_text(await rescue.ainvoke(messages))
        return schema.model_validate(json.loads(_FENCE_RE.sub("", text)))
