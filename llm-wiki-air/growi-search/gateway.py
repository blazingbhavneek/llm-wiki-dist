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
from dataclasses import dataclass, replace
from threading import Lock, local
from typing import Any, Callable

import httpx
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_openai import ChatOpenAI

from config import Settings

log = logging.getLogger("growi_search_gateway")
_LLM_HTTP_CLIENT: httpx.Client | None = None
_LLM_HTTP_CLIENT_LOCK = Lock()


def llm_http_client(max_concurrency: int) -> httpx.Client:
    """Return the shared process-wide pool; its size is fixed by the first call."""
    global _LLM_HTTP_CLIENT
    if _LLM_HTTP_CLIENT is None:
        with _LLM_HTTP_CLIENT_LOCK:
            if _LLM_HTTP_CLIENT is None:
                size = max(1, int(max_concurrency))
                _LLM_HTTP_CLIENT = httpx.Client(
                    limits=httpx.Limits(max_connections=size, max_keepalive_connections=size),
                    timeout=httpx.Timeout(300, pool=None),
                )
    return _LLM_HTTP_CLIENT


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
        max_tokens: int = 0,
        max_concurrency: int = 4,
        retry_attempts: int = 2,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self.model = model
        self.base_url = _normalize_base_url(base_url)
        self.api_key = api_key or "local"
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = max(0, int(max_tokens))
        self.max_concurrency = max(1, int(max_concurrency))
        self.retry_attempts = max(0, retry_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        self.llm = self._make_llm()
        self._call_state = local()
        self.last_usage: dict[str, Any] = {}
        self.last_finish_reason = ""

    @property
    def last_usage(self) -> dict[str, Any]:
        return getattr(self._call_state, "usage", {})

    @last_usage.setter
    def last_usage(self, value: dict[str, Any]) -> None:
        self._call_state.usage = value

    @property
    def last_finish_reason(self) -> str:
        return getattr(self._call_state, "finish_reason", "")

    @last_finish_reason.setter
    def last_finish_reason(self, value: str) -> None:
        self._call_state.finish_reason = value

    def _make_llm(self) -> ChatOpenAI:
        return ChatOpenAI(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            timeout=httpx.Timeout(self.timeout, pool=None),
            http_client=llm_http_client(self.max_concurrency),
            max_retries=self.retry_attempts,
            stream_usage=True,
            max_tokens=self.max_tokens or None,
        )

    def _for_output_tokens(self, max_tokens: int | None) -> Any:
        """Bind a per-call output budget without changing this client's default."""
        if max_tokens is None or int(max_tokens) <= 0:
            return self.llm
        return self.llm.bind(max_tokens=int(max_tokens))

    def run_messages(self, messages: list[dict[str, Any]], max_tokens: int | None = None) -> str:
        def operation() -> str:
            self.last_finish_reason = ""
            response = self._for_output_tokens(max_tokens).invoke(self._norm(messages))
            self.last_usage = getattr(response, "usage_metadata", None) or {}
            self.last_finish_reason = (getattr(response, "response_metadata", None) or {}).get("finish_reason", "")
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

    def complete(self, system_prompt: str, user_content: str, max_tokens: int | None = None) -> str:
        return self.run_messages(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ], max_tokens=max_tokens
        )

    def stream(self, system: str, user: str, on_delta: Callable[[str], None],
               max_tokens: int | None = None) -> str:
        """Stream text chunks and return the same complete response."""
        text: list[str] = []
        usage: dict[str, Any] = {}
        self.last_finish_reason = ""
        for chunk in self._for_output_tokens(max_tokens).stream(self._norm([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])):
            usage = getattr(chunk, "usage_metadata", None) or usage
            self.last_finish_reason = ((getattr(chunk, "response_metadata", None) or {}).get("finish_reason")
                                       or self.last_finish_reason)
            content = getattr(chunk, "content", "")
            if isinstance(content, str):
                piece = content
            elif isinstance(content, list):
                piece = "".join(
                    item if isinstance(item, str) else str(item.get("text", ""))
                    for item in content if isinstance(item, (str, dict))
                )
            else:
                piece = ""
            if piece:
                text.append(piece)
                on_delta(piece)
        self.last_usage = usage
        return "".join(text)

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


class Embedder:
    """OpenAI-compatible /v1/embeddings client; None when unavailable."""

    def __init__(self, settings: Settings) -> None:
        from langchain_openai import OpenAIEmbeddings

        self._client = OpenAIEmbeddings(
            model=settings.embed_model,
            base_url=_normalize_base_url(settings.embed_base_url),
            api_key=settings.embed_api_key or "local",
            timeout=60,
            max_retries=0,
            check_embedding_ctx_length=False,
        )

    @classmethod
    def build(cls, settings: Settings) -> "Embedder | None":
        if not (settings.embed_base_url and settings.embed_model):
            return None
        try:
            embedder = cls(settings)
            embedder.embed_query("availability probe")
            return embedder
        except Exception as exc:  # noqa: BLE001 - degrade to keyword overlap
            log.info("embedder unavailable: %s", exc)
            return None

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._client.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._client.embed_query(text)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


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


# --- Jev relevance question helpers ----------------------------------------

@dataclass(frozen=True)
class JevQuestion:
    key: str
    text: str


def jev_question_text(query: str, subject: str = "") -> str:
    """Canonical Japanese yes/no question, asking whether the page contains the scoped answer."""
    target = f"対象: {subject}\n" if subject else ""
    return (
        f"{target}"
        "上記のページ（およびそのエンティティ定義ページ）の本文は、次の質問に対する答えそのもの"
        "（要求された一覧・表・値・定義・手順が、このページ内で実際に述べられているもの）を"
        "含んでいますか？\n"
        f"質問: {query}\n"
        "質問の範囲に応じた必要な証拠（値、定義、複数項目、章など）を実際に含む必要があります。"
        "同じ話題を一般論として述べているだけ、用語や関数名が略式的に現れるだけ、"
        "他のページへのリンクや目次になっているだけのページは いいえ と答えてください。\n"
        "選択肢: はい / いいえ"
    )


def jev_route_question(description: str) -> str:
    return ("上記のフォルダまたは文書の配下に、次の内容を説明しているページ、またはその手がかりになる"
            "ページが含まれている可能性はありますか？\n"
            f"内容: {description}\n"
            "明らかに別の分野・別の種類の資料だけを含む場合のみ いいえ と答えてください。\n"
            "選択肢: はい / いいえ")


def jev_page_question(description: str) -> str:
    return ("上記のページは、次の内容そのもの（説明・定義・手順・一覧・値など）を実際に述べていますか？\n"
            f"内容: {description}\n"
            "関連する話題に触れているだけのページ、他のページへのリンクや目次だけのページは "
            "いいえ と答えてください。\n"
            "選択肢: はい / いいえ")


def jev_toc_question(query: str) -> str:
    return ("この文書の00-目次にある項目だけを根拠に、質問の検索意図に必要な証拠を含むページへ"
            "到達できる具体的な手がかりがあるか判定してください。単に同じ製品・分野に属するだけ、"
            "対象名が一度出るだけ、リンク集・目次・概要だけの場合は いいえ です。\n"
            f"検索意図: {query}\n"
            "質問の範囲が広い場合は、要求された複数項目・一覧・章の手がかりが必要です。"
            "範囲が狭い場合は、指定された対象と必要な詳細に一致する手がかりが必要です。\n"
            "選択肢: はい / いいえ")


def jev_cascade_list_question() -> str:
    return ("この質問は、該当する項目をすべて列挙することを求めていますか？\n"
            "選択肢: はい / いいえ")


def jev_cascade_fact_question() -> str:
    return ("この質問は、1つの事実や値だけで答えられますか？\n"
            "選択肢: はい / いいえ")


def jev_cascade_profile_questions() -> tuple[str, str]:
    return jev_cascade_list_question(), jev_cascade_fact_question()


def jev_cascade_section_question(query: str, list_profile: bool = False) -> str:
    if list_profile:
        lead = "上記の節は、次の質問が求める項目の一部（一覧の一項目など）を実際に含んでいますか？"
    else:
        lead = "上記の節は、次の質問に対する答えの全部または一部を実際に含んでいますか？"
    return f"{lead}\n質問: {query}\n選択肢: はい / いいえ"


def build_jev(settings: Settings):
    """Return the shared Jev engine, or None when disabled/unavailable."""
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
        if backend in {"hosted", "llm2jev", "systemone", "sglang", "vllm", "jpt"} and not settings.jev_base_url:
            return None
        config = replace(config, backend=backend, local_path=settings.jev_local_path,
                         device=settings.jev_device, dtype=settings.jev_dtype,
                         base_url=settings.jev_base_url, api_key=settings.jev_api_key,
                         model=settings.jev_model, timeout=settings.jev_timeout)
        return jev.get_engine(config)
    except Exception as exc:  # noqa: BLE001
        log.warning("jev engine construction failed: %s; jev disabled", exc)
        return None
