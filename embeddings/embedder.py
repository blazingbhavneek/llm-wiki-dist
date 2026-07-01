"""Real embeddings.

Default backend talks to an OpenAI-compatible embedding server.

At init time, if the configured embedding server is unavailable, this falls back
once to a local HuggingFace model on the configured device.

After init, embedding errors are not swallowed:
- context length errors bubble up
- bad requests bubble up
- runtime/model errors bubble up

There is no fake/hashed embedding path anywhere.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from graph.models import Settings

log = logging.getLogger(__name__)

# region EMBED-SAFE TEXT
_IMAGE_UNIT_RE = re.compile(
    r"<image-unit\b[^>]*>.*?</image-unit>",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_DESCRIPTION_RE = re.compile(
    r"<image-description\b[^>]*>(.*?)</image-description>",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_MEDIA_RE = re.compile(
    r"<image-media\b[^>]*>.*?</image-media>",
    re.IGNORECASE | re.DOTALL,
)
_DATA_IMAGE_URI_RE = re.compile(
    r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+",
    re.IGNORECASE,
)
# endregion EMBED-SAFE TEXT


class Embedder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._backend = settings.embed_backend
        self._client = None
        self._dim: int | None = None

        # Do availability decision once, up front.
        self._initialize_backend()

    def _build_server(self):
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(
            model=self.settings.embed_model,
            base_url=self.settings.embed_base_url,
            api_key=self.settings.embed_api_key,
            check_embedding_ctx_length=False,
        )

    def _build_hf(self):
        from langchain_huggingface import HuggingFaceEmbeddings

        return HuggingFaceEmbeddings(
            model_name=self.settings.hf_embed_model,
            model_kwargs={"device": self.settings.hf_device},
            # one text at a time — cap peak GPU memory per encode call
            encode_kwargs={"batch_size": 1},
        )

    def _initialize_backend(self) -> None:
        """Build the selected backend.

        If the configured backend is the embedding server, probe it immediately.
        If the probe fails, switch once to local HugFace.

        After this method completes, normal embedding calls do not fallback.
        """

        if self._backend == "hf":
            print(
                f"[embedder] LOADING LOCAL HF MODEL (embed_backend='hf'): "
                f"model={self.settings.hf_embed_model} device={self.settings.hf_device} "
                f"-- set WIKI_EMBED_BACKEND=server to use the vLLM server instead",
                flush=True,
            )
            log.info(
                "using local HF embedding backend: model=%s device=%s",
                self.settings.hf_embed_model,
                self.settings.hf_device,
            )
            self._client = self._build_hf()
            print("[embedder] local HF model loaded", flush=True)
            return

        print(
            f"[embedder] probing embedding server: model={self.settings.embed_model} "
            f"base_url={self.settings.embed_base_url}",
            flush=True,
        )

        if self._backend != "server":
            raise ValueError(
                f"unsupported embed_backend={self._backend!r}; expected 'server' or 'hf'"
            )

        self._client = self._build_server()

        try:
            vector = self._client.embed_query("embedding server availability probe")
            self._dim = len(vector)
            log.info(
                "embedding server available: model=%s dim=%s base_url=%s",
                self.settings.embed_model,
                self._dim,
                self.settings.embed_base_url,
            )
        except Exception as err:
            self._fallback_to_hf(err)

    def _fallback_to_hf(self, err: Exception) -> None:
        print(
            f"[embedder] SERVER PROBE FAILED ({err}); FALLING BACK to local HF model "
            f"{self.settings.hf_embed_model} on {self.settings.hf_device}",
            flush=True,
        )
        log.warning(
            "embedding server unavailable during startup probe (%s); "
            "falling back to local HF backend: model=%s device=%s",
            err,
            self.settings.hf_embed_model,
            self.settings.hf_device,
        )

        self._backend = "hf"
        self._client = self._build_hf()

        vector = self._client.embed_query("local HF embedding availability probe")
        self._dim = len(vector)

        log.info(
            "local HF embedding backend ready: model=%s dim=%s device=%s",
            self.settings.hf_embed_model,
            self._dim,
            self.settings.hf_device,
        )

    def _ensure_client(self):
        if self._client is None:
            self._initialize_backend()
        return self._client

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed_query("dimension probe"))
        return self._dim

    @property
    def model_name(self) -> str:
        """Identity of the active embedding model (post-fallback).

        Used to detect an embedding-model change against the value stored in the
        DB so vectors can be rebuilt. Keyed on the model ONLY, not the backend:
        the same model served locally (HF) or remotely (server) produces the same
        vectors, so switching backend must NOT trigger a re-embed.
        """
        if self._backend == "hf":
            return self.settings.hf_embed_model
        return self.settings.embed_model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        client = self._ensure_client()
        vectors = client.embed_documents(texts)

        if vectors:
            self._dim = len(vectors[0])

        return vectors

    def embed_query(self, text: str) -> list[float]:
        client = self._ensure_client()
        vector = client.embed_query(text)
        self._dim = len(vector)
        return vector

    def embed_document(self, text: str) -> list[float]:
        """Embed stored content: strip image markup, then embed with a
        context-length fallback that chunks and averages over-long input."""
        return self._embed_with_context_fallback(self._embed_safe_text(text))

    # region EMBED-SAFE TEXT
    def _embed_safe_text(self, text: str) -> str:
        """Return embedding-safe text; image blocks become descriptions/markers."""
        if not text:
            return ""

        def replace_image_unit(match: re.Match[str]) -> str:
            image_unit = match.group(0)
            desc_match = _IMAGE_DESCRIPTION_RE.search(image_unit)
            if desc_match:
                description = desc_match.group(1).strip()
                if description:
                    return (
                        "\n\n[Embedded image description]\n"
                        f"{description}\n[/Embedded image description]\n\n"
                    )
            return "\n\n[Embedded image omitted: no description available]\n\n"

        cleaned = _IMAGE_UNIT_RE.sub(replace_image_unit, text)
        cleaned = _IMAGE_MEDIA_RE.sub("\n\n[Embedded image media omitted]\n\n", cleaned)
        cleaned = _DATA_IMAGE_URI_RE.sub("data:image;base64,[omitted]", cleaned)
        return cleaned

    def _embed_with_context_fallback(self, text: str) -> list[float]:
        try:
            return self.embed_query(text)
        except Exception as exc:
            if not self._is_splittable_error(exc):
                raise
            self._free_gpu_cache()

        lines = text.splitlines()
        if not lines:
            raise RuntimeError("embedding failed and text could not be split")
        chunk_count = 2
        while chunk_count <= max(2, len(lines)):
            chunks = self._split_lines_into_chunks(lines, chunk_count)
            try:
                vectors = [self.embed_query(chunk) for chunk in chunks if chunk.strip()]
                return self._mean_vectors(vectors)
            except Exception as exc:
                if not self._is_splittable_error(exc):
                    raise
                self._free_gpu_cache()
                chunk_count += 1

        raise RuntimeError(
            f"embedding failed even after splitting into {chunk_count - 1} line chunks"
        )

    def _is_splittable_error(self, exc: Exception) -> bool:
        """Errors that a shorter input might fix: context length or GPU OOM."""
        message = str(exc).lower()
        return (
            "maximum context length" in message
            or "context length" in message
            or "input_tokens" in message
            or "too many tokens" in message
            or "out of memory" in message
            or "cuda error" in message
        )

    def _free_gpu_cache(self) -> None:
        if self._backend != "hf":
            return
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def _split_lines_into_chunks(self, lines: list[str], chunk_count: int) -> list[str]:
        chunk_count = max(1, min(chunk_count, len(lines)))
        size = max(1, (len(lines) + chunk_count - 1) // chunk_count)
        return [
            "\n".join(lines[index : index + size])
            for index in range(0, len(lines), size)
        ]

    def _mean_vectors(self, vectors: list[list[float]]) -> list[float]:
        if not vectors:
            raise ValueError("cannot average empty embedding vector list")
        dim = len(vectors[0])
        return [
            sum(vector[index] for vector in vectors) / len(vectors)
            for index in range(dim)
        ]

    # endregion EMBED-SAFE TEXT
