"""ModelGateway — the actor that talks to models.

Everything that leaves the process for a GPU lives here: the chat LLM client,
the embedder, and the reranker, each with a server backend and a one-shot
local-HuggingFace fallback probed at startup. Owns Settings and rebuilds
clients when settings change at runtime. No graph logic, no SQL.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from typing import TYPE_CHECKING, Any

import requests
from dotenv import load_dotenv
from pydantic import ValidationError

from .core import GRAPH_SYSTEM_PROMPT, Settings, strip_image_media

load_dotenv()

log = logging.getLogger("graph_gateway")


# =============================================================================
# Chat LLM client (OpenAI-compatible, retries, structured output)
# =============================================================================




INVOKE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:8080/v1")
API_KEY = os.environ.get("OPENAI_API_KEY", "local")
MODEL = os.environ.get("WIKI_MODEL", "openai/gpt-oss-120b")



class LlmClient:
    """Small chat wrapper with persistent history and simple retries."""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        *,
        system_prompt: str = "",
        temperature: float = 0.0,
        timeout: int = 300,
        retry_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self.model = model or MODEL
        self.base_url = self._normalize_invoke_url(base_url or INVOKE_URL)
        self.api_key = api_key or API_KEY
        self.system_prompt = strip_image_media(system_prompt)
        self.temperature = temperature
        self.timeout = timeout
        self.retry_attempts = max(0, retry_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        self.message_history: list[dict[str, Any]] = []
        if self.system_prompt.strip():
            self.message_history.append(
                {"role": "system", "content": self.system_prompt.strip()}
            )

    def invoke(self, prompt: str) -> str:
        prompt = strip_image_media(prompt).strip()
        if not prompt:
            raise ValueError("prompt must not be empty")

        user_message = {"role": "user", "content": prompt}
        messages = [*self.message_history, user_message]
        reply = self.run_messages(messages)
        self.message_history.append(user_message)
        self.message_history.append({"role": "assistant", "content": reply})
        return reply

    def invoke_structured(self, prompt: str, output_model: type[Any]) -> Any:
        prompt = strip_image_media(prompt).strip()
        if not prompt:
            raise ValueError("prompt must not be empty")

        user_message = {"role": "user", "content": prompt}
        messages = [*self.message_history, user_message]
        result = self.run_messages_structured(messages, output_model)
        self.message_history.append(user_message)
        self.message_history.append(
            {"role": "assistant", "content": self._stringify_output(result)}
        )
        return result

    def run_messages(self, messages: list[Any]) -> str:
        return self._run_with_retries(
            lambda: self._response_text(
                self._chat_completion(self._normalize_messages(messages))
            )
        )

    def run_messages_structured(
        self,
        messages: list[Any],
        output_model: type[Any],
    ) -> Any:
        normalized = self._normalize_messages(messages)
        schema = output_model.model_json_schema()
        constrained = self._with_json_schema_instruction(normalized, schema)
        return self._run_with_retries(
            lambda: self._parse_structured_response(
                self._chat_completion(constrained),
                output_model,
            )
        )

    def complete(self, system_prompt: str, user_content: str) -> str:
        return self.run_messages(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
        )

    def complete_structured(
        self,
        system_prompt: str,
        user_content: str,
        output_model: type[Any],
    ) -> Any:
        return self.run_messages_structured(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            output_model,
        )

    def reset_history(self) -> None:
        self.message_history = []
        if self.system_prompt.strip():
            self.message_history.append(
                {"role": "system", "content": self.system_prompt.strip()}
            )

    def _chat_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        enable_thinking: bool = True,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": enable_thinking},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        response = requests.post(
            self.base_url,
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        message = self._first_message(body)
        if not isinstance(message, dict):
            raise RuntimeError("chat completions response did not include a message")
        return message

    def _normalize_messages(self, messages: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, dict):
                entry = dict(message)
                if isinstance(entry.get("content"), str):
                    entry["content"] = strip_image_media(entry["content"])
                normalized.append(entry)
                continue

            role = getattr(message, "type", None) or getattr(message, "role", None)
            content = getattr(message, "content", None)
            if role == "human":
                role = "user"
            elif role == "ai":
                role = "assistant"

            if not role or content is None:
                raise TypeError(f"Unsupported message type: {type(message)!r}")

            if isinstance(content, str):
                content = strip_image_media(content)

            entry: dict[str, Any] = {"role": role, "content": content}
            tool_call_id = getattr(message, "tool_call_id", None)
            if tool_call_id:
                entry["tool_call_id"] = tool_call_id
            normalized.append(entry)
        return normalized

    def _with_json_schema_instruction(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
    ) -> list[dict[str, Any]]:
        instruction = (
            "Return only valid JSON matching this schema exactly.\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )
        updated = [dict(message) for message in messages]
        if updated and updated[0].get("role") == "system":
            content = str(updated[0].get("content", "")).strip()
            updated[0]["content"] = (
                f"{content}\n\n{instruction}" if content else instruction
            )
            return updated

        return [{"role": "system", "content": instruction}, *updated]

    def _parse_structured_response(
        self,
        message: dict[str, Any],
        output_model: type[Any],
    ) -> Any:
        raw = self._response_text(message)
        try:
            return output_model.model_validate_json(self._extract_json_text(raw))
        except (ValidationError, ValueError) as exc:
            raise RuntimeError(
                f"structured response validation failed: {exc}\nraw: {raw}"
            ) from exc

    def _extract_json_text(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            parts = text.split("```")
            if len(parts) >= 3:
                text = parts[1]
                if text.startswith("json"):
                    text = text[4:]
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end >= start:
            return text[start : end + 1]
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end >= start:
            return text[start : end + 1]
        raise ValueError("no JSON object or array found in model response")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _normalize_invoke_url(self, value: str) -> str:
        base = (value or INVOKE_URL).rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def _run_with_retries(self, fn) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts + 1):
            try:
                return fn()
            except Exception as exc:
                last_error = exc
                if attempt >= self.retry_attempts:
                    break
                time.sleep(self.retry_delay_seconds)
        assert last_error is not None
        raise last_error

    def _first_message(self, response_body: dict[str, Any]) -> dict[str, Any]:
        choices = response_body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError(
                f"chat completions response missing choices: {response_body}"
            )
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise RuntimeError(
                f"chat completions response missing message: {response_body}"
            )
        return message

    def _response_text(self, response: Any) -> str:
        content = (
            response.get("content", response)
            if isinstance(response, dict)
            else response
        )
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                else:
                    parts.append(str(item))
            return "\n".join(part for part in parts if part).strip()
        return str(content or "").strip()

    def _stringify_output(self, result: Any) -> str:
        if hasattr(result, "model_dump_json"):
            return result.model_dump_json()
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False)
        return str(result)

    def _tool_schema(self, tool: Any) -> dict[str, Any]:
        schema = tool.model_json_schema()
        properties = schema.get("properties", {})
        description = (tool.__doc__ or "").strip()
        return {
            "type": "function",
            "function": {
                "name": tool.__name__,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": schema.get("required", []),
                },
            },
        }

    def _assistant_message_with_tools(self, message: dict[str, Any]) -> dict[str, Any]:
        content = message.get("content", "")
        assistant: dict[str, Any] = {"role": "assistant", "content": content}
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            assistant["tool_calls"] = tool_calls
        return assistant

    def _tool_result_message(
        self, tool_call_id: str, observation: str
    ) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": observation,
        }

    def _parse_tool_args(self, raw_args: Any) -> dict[str, Any]:
        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def _make_tool_call_id(self) -> str:
        return f"call_{uuid.uuid4().hex}"


# =============================================================================
# Embedder (server backend with local HF fallback)
# =============================================================================




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
            log.info(
                "using local HF embedding backend: model=%s device=%s",
                self.settings.hf_embed_model,
                self.settings.hf_device,
            )
            self._client = self._build_hf()
            log.info("local HF embedding backend loaded")
            return

        log.info(
            "probing embedding server: model=%s base_url=%s",
            self.settings.embed_model,
            self.settings.embed_base_url,
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


# =============================================================================
# Reranker (server backend with local HF CrossEncoder fallback)
# =============================================================================





class Reranker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._backend = settings.rerank_backend
        self._cross_encoder = None  # local HF CrossEncoder, built lazily on hf path
        self._initialize_backend()

    # region BACKEND
    def _initialize_backend(self) -> None:
        if self._backend == "hf":
            log.info(
                "using local HF reranker backend: model=%s device=%s",
                self.settings.hf_rerank_model,
                self.settings.rerank_device,
            )
            self._build_hf()
            log.info("local HF reranker backend loaded")
            return

        log.info(
            "probing rerank server: model=%s base_url=%s",
            self.settings.rerank_model,
            self.settings.rerank_base_url,
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



class ModelGateway:
    """Process-wide model stack: chat LLM + embedder + reranker + settings."""

    def __init__(self, settings: Settings):
        log.info("gateway.building model stack")
        self.settings = settings
        log.info(
            "gateway.backends embed_backend=%s rerank_backend=%s",
            settings.embed_backend,
            settings.rerank_backend,
        )
        self.embedder = Embedder(settings)
        self.reranker = self._build_reranker(settings)
        self.llm = self._build_llm(settings)
        log.info("gateway.ready")

    def _build_llm(self, settings: Settings) -> LlmClient:
        return LlmClient(
            model=settings.chat_model,
            base_url=settings.chat_base_url,
            api_key=settings.chat_api_key,
            system_prompt=GRAPH_SYSTEM_PROMPT,
            temperature=settings.chat_temperature,
        )

    def _build_reranker(self, settings: Settings) -> Reranker | None:
        try:
            return Reranker(settings)
        except Exception as exc:
            log.info("reranker unavailable: %s", exc)
            return None

    def update_settings(self, settings: Settings) -> None:
        """Replace settings at runtime. Rebuilds the chat client / reranker
        when their config changed. The embedder is deliberately NOT rebuilt:
        changing the embed model invalidates every stored vector, and the
        bootstrap re-embed gate handles that on the next restart."""
        old = self.settings
        self.settings = settings
        chat_keys = ("chat_model", "chat_base_url", "chat_api_key", "chat_temperature")
        if any(getattr(old, k) != getattr(settings, k) for k in chat_keys):
            self.llm = self._build_llm(settings)
        rerank_keys = (
            "rerank_backend",
            "rerank_base_url",
            "rerank_api_key",
            "rerank_model",
            "hf_rerank_model",
            "rerank_device",
        )
        if any(getattr(old, k) != getattr(settings, k) for k in rerank_keys):
            self.reranker = self._build_reranker(settings)

    def close(self) -> None:
        for obj in (self.embedder, self.reranker, self.llm):
            if hasattr(obj, "close"):
                try:
                    obj.close()
                except Exception:
                    pass
