"""Model clients: chat LLM (async LangChain), embedder, server-side reranker, JEV engine.

Each one degrades to None when unconfigured or unreachable; callers keep working without it.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Any

import httpx
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


def chat_model(settings: Settings, max_tokens: int = 0) -> ChatOpenAI:
    """One chat model per call site; max_tokens caps OUTPUT (thinking + answer), never input."""
    return ChatOpenAI(
        model=settings.chat_model,
        base_url=_normalize_base_url(settings.chat_base_url),
        api_key=settings.chat_api_key or "local",
        temperature=settings.chat_temperature,
        timeout=httpx.Timeout(settings.llm_timeout, pool=None),
        max_retries=settings.llm_max_retries,
        stream_usage=True,
        max_tokens=max_tokens or settings.llm_max_output_tokens or None,
    )


def normalize_scores(values: list[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [1.0 for _ in values]
    return [(v - low) / (high - low) for v in values]


EMBED_TOKEN_LIMIT = 8000  # the embed server's window is 8192 tokens; chars ~ tokens for CJK
EMBED_MAX_PARTS = 32


def _pieces(text: str, parts: int) -> list[str]:
    """Split text into exactly ``parts`` near-equal slices (empty text stays one slice)."""
    parts = max(1, min(parts, len(text) or 1))
    step = max(1, -(-len(text) // parts))
    return [text[start:start + step] for start in range(0, len(text), step)] or [""]


def _mean(vectors: list[list[float]]) -> list[float]:
    return [sum(values) / len(values) for values in zip(*vectors)]


class Embedder:
    """OpenAI-compatible /v1/embeddings client with the model's retrieval prefixes."""

    def __init__(self, settings: Settings) -> None:
        from langchain_openai import OpenAIEmbeddings

        self.model = settings.embed_model
        self.query_prefix = settings.embed_query_prefix
        self.doc_prefix = settings.embed_doc_prefix
        self._client = OpenAIEmbeddings(
            model=settings.embed_model,
            base_url=_normalize_base_url(settings.embed_base_url),
            api_key=settings.embed_api_key or "local",
            timeout=120,
            max_retries=2,
            check_embedding_ctx_length=False,
        )
        self.dim = 0

    @classmethod
    def build(cls, settings: Settings) -> "Embedder | None":
        if not (settings.embed_base_url and settings.embed_model):
            return None
        try:
            embedder = cls(settings)
            embedder.dim = len(embedder.embed_query("availability probe"))
            return embedder
        except Exception as exc:  # noqa: BLE001 - degrade to BM25-only search
            log.warning("embedder unavailable: %s", exc)
            return None

    @property
    def identity(self) -> str:
        """Changes whenever stored vectors stop being comparable with new ones."""
        return f"{self.model}|{self.dim}|{self.doc_prefix}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([self.doc_prefix + text for text in texts], 1)

    def _embed(self, texts: list[str], parts: int) -> list[list[float]]:
        """Embed a batch, splitting any text that does not fit the server's window into
        ``parts`` slices and averaging their vectors back into one vector per text.  The
        server's own tokenizer decides what fits, so a rejected batch is retried with
        parts + 1 (1 -> 2 -> 3 ...) until it is accepted."""
        limit = max(1, EMBED_TOKEN_LIMIT // parts)
        plan = [max(1, min(-(-len(text) // limit), len(text) or 1)) for text in texts]
        pieces = [piece for text, count in zip(texts, plan) for piece in _pieces(text, count)]
        try:
            vectors = self._client.embed_documents(pieces)
        except Exception as exc:  # noqa: BLE001 - the only reliable length signal is the reply
            if parts >= EMBED_MAX_PARTS:
                raise
            log.warning("embeddings rejected (%s); retrying with %d parts per %d-char slice",
                        exc, parts + 1, EMBED_TOKEN_LIMIT // (parts + 1))
            return self._embed(texts, parts + 1)
        out: list[list[float]] = []
        index = 0
        for count in plan:
            out.append(_mean(vectors[index:index + count]) if count > 1 else vectors[index])
            index += count
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._client.embed_query(self.query_prefix + text)


class Reranker:
    """Server-side cross-encoder (/v1/rerank)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = (settings.rerank_base_url or "").rstrip("/")
        self.model = settings.rerank_model
        self.timeout = max(1, int(settings.rerank_timeout or 30))

    @classmethod
    def build(cls, settings: Settings) -> "Reranker | None":
        if not settings.reranker_configured:
            return None
        reranker = cls(settings)
        try:
            reranker.score("availability probe", ["probe document"])
        except Exception as exc:  # noqa: BLE001 - degrade to fusion order
            log.warning("reranker unavailable: %s", exc)
            return None
        return reranker

    def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        payload = {"model": self.model, "query": query, "documents": documents}
        headers = {"Content-Type": "application/json"}
        if self.settings.rerank_api_key:
            headers["Authorization"] = f"Bearer {self.settings.rerank_api_key}"
        urls = ([f"{self.base_url}/rerank"] if self.base_url.endswith("/v1")
                else [f"{self.base_url}/v1/rerank", f"{self.base_url}/rerank"])
        last: Exception | None = None
        for url in urls:
            request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
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


def build_jev(settings: Settings):
    """Return the shared JEV engine, or None when disabled/unavailable."""
    if not settings.jev_enabled:
        return None
    try:
        import jev
        config = jev.JevConfig.from_env()
        backend = settings.jev_backend.strip().lower()
        if backend == "auto":
            backend = "hosted" if settings.jev_base_url and not settings.jev_local_path else "torch"
        elif backend == "local":
            backend = "torch"
        elif backend in {"systemone", "sglang", "vllm", "jpt"}:
            backend = "hosted"  # the vanilla /v1/systemone API
        if backend in {"hosted", "llm2jev"} and not settings.jev_base_url:
            return None
        config = replace(config, backend=backend, local_path=settings.jev_local_path,
                         device=settings.jev_device, dtype=settings.jev_dtype,
                         base_url=settings.jev_base_url, api_key=settings.jev_api_key,
                         model=settings.jev_model, timeout=settings.jev_timeout)
        return jev.get_engine(config)
    except Exception as exc:  # noqa: BLE001
        log.warning("jev engine construction failed: %s; jev disabled", exc)
        return None
