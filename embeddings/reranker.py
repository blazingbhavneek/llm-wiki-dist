"""Cross-encoder reranker.

Same dual-backend shape as ``Embedder``: probe an OpenAI-style rerank server at
init, and if it is unavailable fall back once to a local HuggingFace
``CrossEncoder``. After init, scoring errors bubble up (no silent fallback).

A reranker scores (query, document) pairs jointly, so it is far more precise
than the bi-encoder used for retrieval. The pipeline uses it to trim a wide
candidate pool down to the few nodes actually worth exploring.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from graph.models import Settings

log = logging.getLogger(__name__)


class Reranker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._backend = settings.rerank_backend
        self._cross_encoder = None  # local HF CrossEncoder, built lazily on hf path
        self._initialize_backend()

    # region BACKEND
    def _initialize_backend(self) -> None:
        if self._backend == "hf":
            print(
                f"[reranker] LOADING LOCAL HF CrossEncoder (rerank_backend='hf'): "
                f"model={self.settings.hf_rerank_model} device={self.settings.rerank_device} "
                f"-- set WIKI_RERANK_BACKEND=server to use the vLLM server instead",
                flush=True,
            )
            log.info(
                "using local HF reranker backend: model=%s device=%s",
                self.settings.hf_rerank_model,
                self.settings.rerank_device,
            )
            self._build_hf()
            print("[reranker] local HF CrossEncoder loaded", flush=True)
            return

        print(
            f"[reranker] probing rerank server: model={self.settings.rerank_model} "
            f"base_url={self.settings.rerank_base_url}",
            flush=True,
        )

        if self._backend != "server":
            raise ValueError(
                f"unsupported rerank_backend={self._backend!r}; expected 'server' or 'hf'"
            )

        try:
            self._server_scores("rerank server availability probe", ["probe document"])
            log.info(
                "rerank server available: model=%s base_url=%s",
                self.settings.rerank_model,
                self.settings.rerank_base_url,
            )
        except Exception as err:  # noqa: BLE001 - server down: degrade to local HF
            print(
                f"[reranker] SERVER PROBE FAILED ({err}); FALLING BACK to local HF "
                f"CrossEncoder {self.settings.hf_rerank_model} on {self.settings.rerank_device}",
                flush=True,
            )
            log.warning(
                "rerank server unavailable during startup probe (%s); "
                "falling back to local HF CrossEncoder: model=%s device=%s",
                err,
                self.settings.hf_rerank_model,
                self.settings.rerank_device,
            )
            self._backend = "hf"
            self._build_hf()

    def _build_hf(self) -> None:
        from sentence_transformers import CrossEncoder

        self._cross_encoder = CrossEncoder(
            self.settings.hf_rerank_model,
            device=self.settings.rerank_device,
            trust_remote_code=True,
        )

    # endregion BACKEND

    # region PUBLIC API
    def score(self, query: str, documents: list[str]) -> list[float]:
        """Relevance score per document, in the input order."""
        if not documents:
            return []
        if self._backend == "hf":
            return self._hf_scores(query, documents)
        return self._server_scores(query, documents)

    def top_k(
        self,
        query: str,
        items: list[tuple[str, object]],
        k: int,
    ) -> list[tuple[object, float]]:
        """Rank ``(document_text, payload)`` pairs by relevance, keep the top k.

        Returns ``(payload, score)`` sorted by descending score.
        """
        if not items:
            return []
        documents = [text for text, _payload in items]
        scores = self.score(query, documents)
        ranked = sorted(
            ((payload, score) for (_text, payload), score in zip(items, scores)),
            key=lambda pair: pair[1],
            reverse=True,
        )
        return ranked[: max(0, k)]

    # endregion PUBLIC API

    # region BACKENDS
    def _hf_scores(self, query: str, documents: list[str]) -> list[float]:
        pairs = [(query, document) for document in documents]
        raw = self._cross_encoder.predict(pairs)
        return [float(value) for value in raw]

    def _server_scores(self, query: str, documents: list[str]) -> list[float]:
        payload = json.dumps(
            {
                "model": self.settings.rerank_model,
                "query": query,
                "documents": documents,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.settings.rerank_api_key:
            headers["Authorization"] = f"Bearer {self.settings.rerank_api_key}"

        last_err: Exception | None = None
        for url in self._rerank_urls():
            request = urllib.request.Request(
                url, data=payload, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    body = json.loads(response.read().decode("utf-8"))
                return self._parse_server_scores(body, len(documents))
            except (OSError, urllib.error.URLError, ValueError) as err:
                last_err = err
        raise RuntimeError(f"rerank server request failed: {last_err}")

    def _rerank_urls(self) -> list[str]:
        base = self.settings.rerank_base_url.rstrip("/")
        if base.endswith("/v1"):
            return [f"{base}/rerank"]
        return [f"{base}/v1/rerank", f"{base}/rerank"]

    def _parse_server_scores(self, body: object, count: int) -> list[float]:
        """Parse the common rerank response shape into input-order scores.

        Accepts ``{"results": [{"index": i, "relevance_score": s}, ...]}`` (TEI /
        infinity / jina style); falls back to a flat ``{"scores": [...]}`` list.
        """
        if isinstance(body, dict) and isinstance(body.get("results"), list):
            scores = [0.0] * count
            for entry in body["results"]:
                index = int(entry.get("index", -1))
                value = entry.get("relevance_score", entry.get("score", 0.0))
                if 0 <= index < count:
                    scores[index] = float(value)
            return scores
        if isinstance(body, dict) and isinstance(body.get("scores"), list):
            return [float(value) for value in body["scores"]][:count]
        raise ValueError("unrecognized rerank server response shape")

    # endregion BACKENDS
