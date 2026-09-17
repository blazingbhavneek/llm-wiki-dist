"""Slim gateway: LlmClient (LangChain ChatOpenAI) + server-side Reranker.

Ported from llm-wiki-dist/graph/gateway.py with Embedder and all HuggingFace /
vector-index code removed. The reranker degrades to no-op when unconfigured so
callers preserve Elasticsearch order.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
from typing import Any, Callable

import httpx
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_openai import ChatOpenAI

from config import Settings

log = logging.getLogger("growi_search_gateway")


def _normalize_base_url(value: str) -> str:
    base = (value or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base[: -len("/chat/completions")]
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


class LlmClient:
    """Minimal chat client with one-shot complete / complete_structured."""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        *,
        temperature: float = 0.0,
        timeout: int = 300,
        retry_attempts: int = 2,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self.model = model
        self.base_url = _normalize_base_url(base_url)
        self.api_key = api_key or "local"
        self.temperature = temperature
        self.timeout = timeout
        self.retry_attempts = max(0, retry_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        self.llm = self._make_llm()
        self.last_usage: dict[str, Any] = {}

    def _make_llm(self) -> ChatOpenAI:
        return ChatOpenAI(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            timeout=self.timeout,
            max_retries=self.retry_attempts,
            stream_usage=True,
        )

    def run_messages(self, messages: list[dict[str, Any]]) -> str:
        def operation() -> str:
            response = self.llm.invoke(self._norm(messages))
            self.last_usage = getattr(response, "usage_metadata", None) or {}
            text = _as_text(getattr(response, "content", ""))
            if not text:
                raise RuntimeError("LLM returned empty response")
            return text

        return self._with_retries(operation, "run_messages")

    def run_messages_structured(
        self, messages: list[dict[str, Any]], output_model: type[Any]
    ) -> Any:
        def operation() -> Any:
            structured = self.llm.with_structured_output(output_model, method="json_schema")
            usage_cb = UsageMetadataCallbackHandler()
            result = structured.invoke(self._norm(messages), config={"callbacks": [usage_cb]})
            self.last_usage = next(iter(usage_cb.usage_metadata.values()), {})
            if isinstance(result, output_model):
                return result
            if isinstance(result, dict):
                return output_model.model_validate(result)
            if isinstance(result, str):
                return output_model.model_validate_json(result)
            return output_model.model_validate(result)

        return self._with_retries(operation, f"structured:{output_model.__name__}")

    def complete(self, system_prompt: str, user_content: str) -> str:
        return self.run_messages(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
        )

    def complete_structured(
        self, system_prompt: str, user_content: str, output_model: type[Any]
    ) -> Any:
        return self.run_messages_structured(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            output_model,
        )

    def probe(self) -> bool:
        """Cheap availability probe (list models); no costly generation."""
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            return response.status_code < 500
        except Exception:  # noqa: BLE001 - probe failures are never fatal
            return False

    def _with_retries(self, operation: Callable[[], Any], label: str) -> Any:
        total = self.retry_attempts + 1
        last: Exception | None = None
        for attempt in range(1, total + 1):
            try:
                return operation()
            except Exception as exc:  # noqa: BLE001 - retried below
                last = exc
                log.warning("[LLM Retry] %s attempt %d/%d failed: %s", label, attempt, total, exc)
                if attempt >= total:
                    break
                self.llm = self._make_llm()
                if self.retry_delay_seconds > 0:
                    time.sleep(
                        self.retry_delay_seconds * (2 ** min(attempt - 1, 6))
                        + random.uniform(0, 0.25)
                    )
        raise RuntimeError(f"{label} failed after {total} attempt(s)") from last

    @staticmethod
    def _norm(messages: list[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, dict):
                out.append(dict(message))
                continue
            role = getattr(message, "type", None) or getattr(message, "role", None)
            if role in ("human", "user"):
                role = "user"
            elif role in ("ai", "assistant"):
                role = "assistant"
            out.append({"role": role or "user", "content": getattr(message, "content", "")})
        return out


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
            if isinstance(item, (str, dict))
        ]
        return "\n".join(p for p in parts if p).strip()
    return str(content or "").strip()


def normalize_scores(values: list[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [1.0 for _ in values]
    return [(v - low) / (high - low) for v in values]


class Reranker:
    """Server-side cross-encoder only. HF/local fallback intentionally removed."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = (settings.rerank_base_url or "").rstrip("/")
        self.model = settings.rerank_model
        self.timeout = max(1, int(settings.rerank_timeout or 15))

    @classmethod
    def build(cls, settings: Settings) -> "Reranker | None":
        if not settings.reranker_configured:
            return None
        reranker = cls(settings)
        try:
            reranker.score("availability probe", ["probe document"])
        except Exception as exc:  # noqa: BLE001 - degrade to ES order
            log.info("reranker unavailable: %s", exc)
            return None
        return reranker

    def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        payload = {
            "model": self.model,
            "query": query,
            "documents": documents,
        }
        headers = {"Content-Type": "application/json"}
        if self.settings.rerank_api_key:
            headers["Authorization"] = f"Bearer {self.settings.rerank_api_key}"

        if self.base_url.endswith("/v1"):
            urls = [f"{self.base_url}/rerank"]
        else:
            urls = [f"{self.base_url}/v1/rerank", f"{self.base_url}/rerank"]

        last: Exception | None = None
        for url in urls:
            request = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                return self._parse(body, len(documents))
            except (OSError, urllib.error.URLError, ValueError) as exc:
                last = exc
        raise RuntimeError(f"rerank server request failed: {last}")

    @staticmethod
    def _parse(body: Any, count: int) -> list[float]:
        if isinstance(body, dict) and isinstance(body.get("results"), list):
            scores = [0.0] * count
            for entry in body["results"]:
                index = int(entry.get("index", -1))
                value = entry.get("relevance_score", entry.get("score", 0.0))
                if 0 <= index < count:
                    scores[index] = float(value)
            return scores
        if isinstance(body, dict) and isinstance(body.get("scores"), list):
            return [float(v) for v in body["scores"]][:count]
        raise ValueError("unrecognized rerank server response shape")

    def top_k(
        self, query: str, items: list[tuple[str, Any]], k: int
    ) -> list[tuple[Any, float]]:
        """Rank (text, payload) pairs; return (payload, score) sorted desc."""
        if not items:
            return []
        documents = [_text for _text, _payload in items]
        scores = self.score(query, documents)
        scores = normalize_scores(scores)
        ranked = sorted(
            ((payload, score) for (_text, payload), score in zip(items, scores)),
            key=lambda pair: pair[1],
            reverse=True,
        )
        return ranked[: max(0, k)]
