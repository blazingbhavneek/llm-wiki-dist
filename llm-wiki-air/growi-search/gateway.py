"""Slim gateway: LlmClient (LangChain ChatOpenAI) + server-side Reranker.

Ported from llm-wiki-dist/graph/gateway.py with Embedder and all HuggingFace /
vector-index code removed. The reranker degrades to no-op when unconfigured so
callers preserve Elasticsearch order.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, Protocol

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


# --- Jev relevance classifier ----------------------------------------------
#
# Jev is a per-question relevance gate, not an answer generator. Two adapters
# share one interface so the researcher never parses free-form model text.

JEV_YES_KEYS = ("true", "はい", "yes", "Yes")
JEV_OPTIONS = {"yes": "はい", "no": "いいえ"}
JEV_REQUIRED_FILES = (
    "jev_style_decision.py",
    "config.json",
    "model.safetensors",
    "readout_config.json",
    "tokenizer.json",
)


@dataclass(frozen=True)
class JevQuestion:
    key: str                 # stable page/chunk/card key
    text: str                # Japanese yes/no question


class JevClassifier(Protocol):
    def score_many(
        self, state: dict[str, Any], questions: list["JevQuestion"]
    ) -> list[float]: ...


def jev_question_text(query: str, subject: str = "") -> str:
    """Canonical Japanese yes/no question. The renderer owns this wording.

    Strict by design: it asks whether the page *contains the answer*, not whether
    it is related. Asking about relatedness makes a domain corpus answer yes to
    everything (any page that happens to name a function), which drowns the sweep.
    """
    target = f"対象: {subject}\n" if subject else ""
    return (
        f"{target}"
        "上記のページ（およびそのエンティティ定義ページ）の本文は、次の質問に対する答えそのもの"
        "（要求された一覧・表・値・定義・手順が、このページ内で実際に述べられているもの）を"
        "含んでいますか？\n"
        f"質問: {query}\n"
        "同じ話題を一般論として述べているだけ、用語や関数名が略式的に現れるだけ、"
        "他のページへのリンクや目次になっているだけのページは いいえ と答えてください。\n"
        "選択肢: はい / いいえ"
    )


def render_jev_state(state: dict[str, Any]) -> str:
    """Render the structured state dict into the canonical prompt block."""
    lines = ["状態:"]
    page = state.get("page") or {}
    if page:
        lines += ["# 対象ページ", f"タイトル: {page.get('title', '')}", f"パス: {page.get('path', '')}"]
        lines += ["本文（または目次カードの要約・キーワード）:", str(page.get("text", ""))]
    for card in state.get("cards") or []:
        lines += [
            f"# カード: {card.get('title', '')}",
            f"パス: {card.get('path', '')}",
            str(card.get("summary", "")),
        ]
        if card.get("keywords"):
            lines.append("キーワード: " + "、".join(card["keywords"]))
        if card.get("entities"):
            lines.append("エンティティ: " + "、".join(card["entities"]))
    defs = state.get("entity_definitions") or []
    if defs:
        lines.append("# このページで使われているエンティティを定義している他のページ")
        for definition in defs:
            lines.append(
                f"- {definition.get('entity', '')} → {definition.get('title', '')}: {definition.get('summary', '')}"
            )
    return "\n".join(lines)


def _jev_yes_probability(container: Any) -> float:
    """Extract p(はい) from a probabilities mapping; reject bad values."""
    if isinstance(container, (int, float)) and not isinstance(container, bool):
        value = float(container)
    elif isinstance(container, dict):
        value = float("nan")
        for key in JEV_YES_KEYS:
            if key in container:
                value = float(container[key])
                break
    else:
        value = float("nan")
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise RuntimeError(f"invalid jev probability: {container!r}")
    return value


class HostedJevClassifier:
    """Jev-compatible /score sidecar (for example a jev-score service)."""

    def __init__(self, settings: Settings, transport: Any = None) -> None:
        base = (settings.jev_base_url or "").rstrip("/")
        self.url = base if base.endswith("/score") else f"{base}/score"
        self.model = settings.jev_model
        self.timeout = max(1, int(settings.jev_timeout or 60))
        self.api_key = settings.jev_api_key
        self._client = httpx.Client(timeout=self.timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def score_many(self, state: dict[str, Any], questions: list[JevQuestion]) -> list[float]:
        if not questions:
            return []
        payload = {
            "model": self.model,
            "state": state,
            "questions": [{"key": q.key, "text": q.text} for q in questions],
            "options": JEV_OPTIONS,
            "category": "noul",
            "many_mode": "batched",
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last: Exception | None = None
        for attempt in range(2):  # one bounded retry, transport/5xx only
            try:
                response = self._client.post(self.url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last = exc
                if attempt == 0:
                    continue
                raise RuntimeError(f"jev score request failed: {exc}") from exc
            if response.status_code >= 500:
                last = RuntimeError(f"HTTP {response.status_code}")
                if attempt == 0:
                    continue
                raise RuntimeError(f"jev score endpoint failed: HTTP {response.status_code}")
            if response.is_error:  # 4xx: never retry, fail closed
                raise RuntimeError(f"jev score endpoint rejected request: HTTP {response.status_code}")
            try:
                return self._parse(response.json(), questions)
            except (ValueError, RuntimeError) as exc:
                # Malformed body is a hard failure (retrying cannot fix shape).
                raise RuntimeError(f"jev score response invalid: {exc}") from exc
        raise RuntimeError(f"jev score request failed: {last}")

    @staticmethod
    def _parse(body: Any, questions: list[JevQuestion]) -> list[float]:
        count = len(questions)
        if isinstance(body, dict) and isinstance(body.get("probabilities"), list):
            raw = body["probabilities"]
            if len(raw) != count:
                raise RuntimeError(f"expected {count} probabilities, got {len(raw)}")
            return [
                _jev_yes_probability(item.get("probabilities", item) if isinstance(item, dict) else item)
                for item in raw
            ]
        if isinstance(body, dict) and isinstance(body.get("results"), list):
            raw = body["results"]
            keys = [item.get("key") if isinstance(item, dict) else None for item in raw]
            if all(k is not None for k in keys):
                if len(set(keys)) != len(keys):
                    raise RuntimeError("duplicate result keys")
                wanted = [q.key for q in questions]
                if set(keys) != set(wanted):
                    raise RuntimeError("result keys do not match request keys")
                by_key = {
                    item["key"]: _jev_yes_probability(item.get("probabilities", item))
                    for item in raw
                }
                return [by_key[key] for key in wanted]
            if len(raw) != count:
                raise RuntimeError(f"expected {count} results, got {len(raw)}")
            return [
                _jev_yes_probability(item.get("probabilities", item) if isinstance(item, dict) else item)
                for item in raw
            ]
        raise RuntimeError("unrecognized jev score response shape")


def _jev_runtime_options(settings: Settings) -> tuple[str | None, str]:
    """Device and dtype for the local runtime.

    `auto` lets the runtime pick. bfloat16 halves both memory and time against the
    runtime's float32 default, and quietly becomes float16 on a GPU that predates it.
    """
    device = (settings.jev_device or "auto").strip().lower()
    dtype = (settings.jev_dtype or "float32").strip().lower()
    resolved = None if device in ("", "auto") else device
    if dtype == "bfloat16":
        try:
            import torch

            on_gpu = resolved == "cuda" or (resolved is None and torch.cuda.is_available())
            if on_gpu and not torch.cuda.is_bf16_supported():
                dtype = "float16"
        except Exception:  # noqa: BLE001 - no torch/CUDA information, keep what was asked for
            pass
    return resolved, dtype


class LocalJevClassifier:
    """Wraps a local JevStyleDecision runtime loaded from jev_local_path."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._lock = Lock()

    @classmethod
    def build(cls, settings: Settings) -> "LocalJevClassifier | None":
        import importlib.util

        path = _jev_local_snapshot(settings)
        module_file = path / "jev_style_decision.py"
        try:
            spec = importlib.util.spec_from_file_location("jev_style_decision", module_file)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {module_file}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            device, dtype = _jev_runtime_options(settings)
            try:
                runtime = module.JevStyleDecision(str(path), device=device, dtype=dtype)
            except TypeError:  # runtime build without device/dtype arguments
                runtime = module.JevStyleDecision(str(path))
            print(f"[growi-search] Jev local runtime: device={getattr(runtime, 'device', device)} "
                  f"dtype={getattr(runtime, 'dtype', dtype)}", flush=True)
        except Exception as exc:  # noqa: BLE001 - missing optional ML deps etc.
            log.warning("jev local runtime failed to load: %s; jev disabled", exc)
            return None
        return cls(runtime)

    def score_many(self, state: dict[str, Any], questions: list[JevQuestion]) -> list[float]:
        with self._lock:
            typed = [{"t": "noul", "ins": question.text, "crit": None} for question in questions]
            try:
                if hasattr(self.runtime, "decide_many"):
                    results = self.runtime.decide_many(state, typed)
                else:
                    results = [self.runtime.decide(state, q.text, qtype="noul") for q in questions]
            except Exception as exc:  # noqa: BLE001 - surface as adapter failure
                raise RuntimeError(f"local jev runtime failed: {exc}") from exc
            if len(results) != len(questions):
                raise RuntimeError("local jev runtime returned misaligned results")
            out: list[float] = []
            for question, result in zip(questions, results):
                container = result.get("probabilities", result) if isinstance(result, dict) else result
                try:
                    out.append(_jev_yes_probability(container))
                except RuntimeError as exc:
                    raise RuntimeError(f"{exc} for {question.key}") from exc
            return out


def _jev_local_snapshot(settings: Settings):
    """Return a complete local model directory, downloading only at startup."""
    from pathlib import Path

    def complete(path: Path) -> bool:
        return path.is_dir() and all((path / name).is_file() for name in JEV_REQUIRED_FILES)

    configured = Path(settings.jev_local_path).expanduser() if settings.jev_local_path else None
    if configured is not None and complete(configured):
        print(f"[growi-search] Jev model: using local files at {configured}", flush=True)
        return configured

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError("huggingface-hub is required for local Jev startup download") from exc

    kwargs = {"repo_id": settings.jev_model}
    if configured is not None:
        print(f"[growi-search] Jev model: downloading {settings.jev_model} to {configured}", flush=True)
        kwargs["local_dir"] = str(configured)
        path = Path(snapshot_download(**kwargs))
    else:
        try:
            path = Path(snapshot_download(local_files_only=True, **kwargs))
            if not complete(path):
                raise FileNotFoundError("cached snapshot is incomplete")
            print(f"[growi-search] Jev model: using Hugging Face cache at {path}", flush=True)
            return path
        except Exception:  # no complete cached snapshot: download before readiness
            print(f"[growi-search] Jev model: downloading {settings.jev_model} to Hugging Face cache", flush=True)
            path = Path(snapshot_download(**kwargs))
    if not complete(path):
        raise RuntimeError(f"downloaded Jev snapshot is incomplete: {path}")
    print(f"[growi-search] Jev model: ready at {path}", flush=True)
    return path


def build_jev(settings: Settings) -> JevClassifier | None:
    """Factory: returns one reusable adapter, or None (jev disabled)."""
    if not settings.jev_enabled:
        return None
    backend = (settings.jev_backend or "auto").strip().lower()
    local, hosted = bool(settings.jev_local_path), bool(settings.jev_base_url)
    try:
        if backend == "hosted" or (backend == "auto" and hosted and not local):
            return HostedJevClassifier(settings) if hosted else _jev_none("base URL not set")
        if backend == "local" or backend == "auto":
            return LocalJevClassifier.build(settings)
    except Exception as exc:  # noqa: BLE001 - jev must never break search startup
        log.warning("jev adapter construction failed: %s; jev disabled", exc)
        return None
    return _jev_none(f"backend={backend!r} with no local path or base URL")


def _jev_none(reason: str) -> None:
    log.warning("jev disabled: %s", reason)
    return None
