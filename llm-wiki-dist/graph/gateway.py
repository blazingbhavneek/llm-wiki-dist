# region Imports

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from dotenv import load_dotenv
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_openai import ChatOpenAI

from .core import GRAPH_SYSTEM_PROMPT, Settings, strip_image_media

# endregion Imports

# region Global vars

load_dotenv()

log = logging.getLogger("graph_gateway")


INVOKE_URL = os.environ.get("OPENAI_BASE_URL", "http://10.160.144.101:51029/v1")
API_KEY = os.environ.get("OPENAI_API_KEY", "local")
MODEL = os.environ.get("WIKI_MODEL", "gemma-4-31B")


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


# endregion Global vars


class LlmClient:
    """Small chat wrapper with persistent history using LangChain ChatOpenAI."""

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
        # Store model/API settings, using defaults if empty values are passed.
        self.model = model or MODEL
        self.base_url = self._normalize_base_url(base_url or INVOKE_URL)
        self.api_key = api_key or API_KEY

        # Remove image/media content before storing prompts.
        self.system_prompt = strip_image_media(system_prompt)

        # Store generation and request settings.
        self.temperature = temperature
        self.timeout = timeout

        # LangChain/OpenAI handles retries internally via max_retries.
        # retry_delay_seconds is kept only so the public constructor stays the same.
        # We also use retry_delay_seconds for app-level retries around parsing,
        # structured output, empty responses, provider errors, and timeouts.
        self.retry_attempts = max(0, retry_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)

        # Create the LangChain OpenAI-compatible chat client.
        self.llm = ChatOpenAI(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            timeout=self.timeout,
            max_retries=self.retry_attempts,
        )

        # Persistent chat history used by invoke() and invoke_structured().
        self.message_history: list[dict[str, Any]] = []

        # Usage of the most recent run_messages()/run_messages_structured() call,
        # for callers that want to log token cost (see researcher.py _log_usage).
        self.last_usage: dict[str, Any] = {}

        # Add the system prompt at the start of the conversation, if provided.
        if self.system_prompt.strip():
            self.message_history.append(
                {"role": "system", "content": self.system_prompt.strip()}
            )

    def invoke(self, prompt: str) -> str:
        # Clean the prompt and reject empty input.
        prompt = strip_image_media(prompt).strip()
        if not prompt:
            raise ValueError("prompt must not be empty")

        # Add the new user message after existing history.
        user_message = {"role": "user", "content": prompt}
        messages = [*self.message_history, user_message]

        # Run the request, then save the user message and assistant reply.
        # History is only saved after a successful retry cycle.
        reply = self.run_messages(messages)
        self.message_history.append(user_message)
        self.message_history.append({"role": "assistant", "content": reply})

        return reply

    def invoke_structured(self, prompt: str, output_model: type[Any]) -> Any:
        # Clean the prompt and reject empty input.
        prompt = strip_image_media(prompt).strip()
        if not prompt:
            raise ValueError("prompt must not be empty")

        # Add the new user message after existing history.
        user_message = {"role": "user", "content": prompt}
        messages = [*self.message_history, user_message]

        # Let LangChain/OpenAI handle structured output parsing and validation.
        # App-level retry will rerun this if JSON/Pydantic parsing fails.
        result = self.run_messages_structured(messages, output_model)

        # Store the structured result as text in message history.
        # History is only saved after a successful retry cycle.
        self.message_history.append(user_message)
        self.message_history.append(
            {"role": "assistant", "content": self._stringify_output(result)}
        )

        return result

    def run_messages(self, messages: list[Any]) -> str:
        # Normalize message formats and call LangChain's chat model.
        normalized_messages = self._normalize_messages(messages)

        def operation() -> str:
            response = self.llm.invoke(normalized_messages)
            self.last_usage = getattr(response, "usage_metadata", None) or {}

            # LangChain returns an AIMessage; extract its text content.
            text = self._message_text(response)

            # Treat empty output as a failed LLM call so it can be retried.
            if not text:
                raise RuntimeError("LLM returned empty response")

            return text

        return self._run_with_retries(
            operation,
            label="run_messages",
        )

    def run_messages_structured(
        self,
        messages: list[Any],
        output_model: type[Any],
    ) -> Any:
        # Normalize messages once so every retry sends the same input.
        normalized_messages = self._normalize_messages(messages)

        def operation() -> Any:
            # Wrap the base model with LangChain's structured-output parser.
            # If output_model is a Pydantic model, this returns an instance of that model.
            structured_llm = self.llm.with_structured_output(
                output_model,
                method="json_schema",
            )

            # Fresh handler per call: cheap way to read this call's token usage
            # without changing structured_llm.invoke()'s return shape.
            usage_cb = UsageMetadataCallbackHandler()

            # No manual JSON extraction or model_validate_json needed.
            # If this raises JSON/Pydantic errors, app-level retry catches it.
            result = structured_llm.invoke(
                normalized_messages, config={"callbacks": [usage_cb]}
            )
            self.last_usage = next(iter(usage_cb.usage_metadata.values()), {})

            # Defensive validation in case a provider/LangChain version returns
            # a dict/string instead of the already-validated Pydantic object.
            if isinstance(result, output_model):
                return result

            if isinstance(result, dict):
                return output_model.model_validate(result)

            if isinstance(result, str):
                return output_model.model_validate_json(result)

            return output_model.model_validate(result)

        return self._run_with_retries(
            operation,
            label=f"run_messages_structured:{getattr(output_model, '__name__', str(output_model))}",
        )

    def complete(self, system_prompt: str, user_content: str) -> str:
        # One-shot completion that does not use persistent message_history.
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
        # One-shot structured completion using LangChain structured output.
        return self.run_messages_structured(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            output_model,
        )

    def reset_history(self) -> None:
        # Clear stored conversation history.
        self.message_history = []

        # Re-add the system prompt so future calls keep the same behavior.
        if self.system_prompt.strip():
            self.message_history.append(
                {"role": "system", "content": self.system_prompt.strip()}
            )

    def _make_llm(self) -> Any:
        # Create a fresh LangChain OpenAI-compatible chat client.
        return ChatOpenAI(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            timeout=self.timeout,
            max_retries=self.retry_attempts,
        )

    def _rebuild_llm(self) -> None:
        # Rebuild the client before retrying in case the underlying session is bad.
        self.llm = self._make_llm()

    def _run_with_retries(
        self,
        operation: Callable[[], Any],
        *,
        label: str,
    ) -> Any:
        # Run one initial attempt plus retry_attempts additional attempts.
        # Example: retry_attempts=3 means up to 4 total tries.
        max_total_attempts = self.retry_attempts + 1
        last_exc: Exception | None = None

        for attempt in range(1, max_total_attempts + 1):
            try:
                return operation()

            except Exception as exc:
                last_exc = exc

                log.warning(
                    "[LLM Retry] %s failed on attempt %d/%d: %s: %s",
                    label,
                    attempt,
                    max_total_attempts,
                    type(exc).__name__,
                    exc,
                )

                if attempt >= max_total_attempts:
                    break

                self._rebuild_llm()
                self._sleep_before_retry(attempt)

        raise RuntimeError(
            f"{label} failed after {max_total_attempts} attempt(s)"
        ) from last_exc

    def _sleep_before_retry(self, attempt: int) -> None:
        # Wait before retrying, using exponential backoff plus small jitter.
        if self.retry_delay_seconds <= 0:
            return

        delay = self.retry_delay_seconds * (2 ** min(max(0, attempt - 1), 6))
        delay += random.uniform(0.0, 0.25)

        time.sleep(delay)

    def _normalize_base_url(self, value: str) -> str:
        # LangChain/OpenAI wants the API base URL, usually ending in /v1.
        # Your old code used /v1/chat/completions, so convert that if provided.
        base = (value or INVOKE_URL).rstrip("/")

        if base.endswith("/chat/completions"):
            return base[: -len("/chat/completions")]

        if base.endswith("/v1"):
            return base

        return f"{base}/v1"

    def _normalize_messages(self, messages: list[Any]) -> list[dict[str, Any]]:
        # Convert common message shapes into OpenAI/LangChain-compatible dicts.
        normalized: list[dict[str, Any]] = []

        for message in messages:
            if isinstance(message, dict):
                entry = dict(message)

                if isinstance(entry.get("content"), str):
                    entry["content"] = strip_image_media(entry["content"])

                normalized.append(entry)
                continue

            # Support LangChain-style message objects.
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

            # Preserve tool call IDs if a tool message is passed in.
            tool_call_id = getattr(message, "tool_call_id", None)
            if tool_call_id:
                entry["tool_call_id"] = tool_call_id

            normalized.append(entry)

        return normalized

    def _message_text(self, response: Any) -> str:
        # LangChain usually returns an AIMessage with a .content field.
        content = getattr(response, "content", response)

        if isinstance(content, str):
            return content.strip()

        # Some providers return content as a list of content blocks.
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
        # Convert structured output into text before saving it in chat history.
        if hasattr(result, "model_dump_json"):
            return result.model_dump_json()

        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False)

        return str(result)


class Embedder:
    def __init__(self, settings: Settings) -> None:
        # Store settings and selected backend from config.
        self.settings = settings
        self._backend = settings.embed_backend

        # Embedding dimension is discovered from the first successful embed call.
        self._dim: int | None = None

        # Only these two backends are supported.
        if self._backend not in {"server", "hf"}:
            raise ValueError(
                f"unsupported embed_backend={self._backend!r}; expected 'server' or 'hf'"
            )

        # If local HF is explicitly configured, build it directly.
        if self._backend == "hf":
            log.info(
                "using local HF embedding backend: model=%s device=%s",
                self.settings.hf_embed_model,
                self.settings.hf_device,
            )
            self._client = self._build_client("hf")
            log.info("local HF embedding backend loaded")
            return

        # Otherwise, try the remote embedding server first.
        log.info(
            "probing embedding server: model=%s base_url=%s",
            self.settings.embed_model,
            self.settings.embed_base_url,
        )

        self._client = self._build_client("server")

        try:
            # Probe the server immediately so startup decides if fallback is needed.
            vector = self._client.embed_query("embedding server availability probe")
            self._dim = len(vector)

            log.info(
                "embedding server available: model=%s dim=%s base_url=%s",
                self.settings.embed_model,
                self._dim,
                self.settings.embed_base_url,
            )

        except Exception as err:
            # Server failed during startup, so switch once to local HF.
            log.warning(
                "embedding server unavailable during startup probe (%s); "
                "falling back to local HF backend: model=%s device=%s",
                err,
                self.settings.hf_embed_model,
                self.settings.hf_device,
            )

            self._backend = "hf"
            try:
                self._client = self._build_client("hf")

                # Probe HF too, so dimension is known and HF failures happen early.
                vector = self._client.embed_query("local HF embedding availability probe")
                self._dim = len(vector)
            except Exception as hf_err:
                raise RuntimeError(
                    "embedding server unavailable and local HF fallback failed: "
                    f"{hf_err}"
                ) from hf_err

            log.info(
                "local HF embedding backend ready: model=%s dim=%s device=%s",
                self.settings.hf_embed_model,
                self._dim,
                self.settings.hf_device,
            )

    def _build_client(self, backend: str):
        # Build the OpenAI-compatible remote embedding client.
        if backend == "server":
            from langchain_openai import OpenAIEmbeddings

            return OpenAIEmbeddings(
                model=self.settings.embed_model,
                base_url=self.settings.embed_base_url,
                api_key=self.settings.embed_api_key,
                # Disabled because we handle over-long stored documents ourselves.
                check_embedding_ctx_length=False,
            )

        # Build the local HuggingFace embedding client.
        from langchain_huggingface import HuggingFaceEmbeddings

        return HuggingFaceEmbeddings(
            model_name=self.settings.hf_embed_model,
            model_kwargs={"device": self.settings.hf_device},
            # Encode one text at a time to reduce peak GPU memory usage.
            encode_kwargs={"batch_size": 1},
        )

    @property
    def dim(self) -> int:
        # Return cached embedding dimension when available.
        # If unknown, run a tiny probe and cache the result.
        if self._dim is None:
            self._dim = len(self.embed_query("dimension probe"))

        return self._dim

    @property
    def model_name(self) -> str:
        # Return the active model name after possible fallback.
        # Backend itself is not included because same model via server/HF should
        # produce the same vectors and should not force a re-embed.
        if self._backend == "hf":
            return self.settings.hf_embed_model

        return self.settings.embed_model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        # Nothing to embed.
        if not texts:
            return []

        # Embed all documents using the active backend.
        vectors = self._client.embed_documents(texts)

        # Cache dimension from the first returned vector.
        if vectors:
            self._dim = len(vectors[0])

        return vectors

    def embed_query(self, text: str) -> list[float]:
        # Embed one query/text string using the active backend.
        vector = self._client.embed_query(text)

        # Keep dimension updated from the actual returned vector.
        self._dim = len(vector)

        return vector

    def embed_document(self, text: str) -> list[float]:
        # Stored documents may contain image markup/base64.
        # Clean that first, then fallback to chunking if text is too large.
        text = self._embed_safe_text(text)
        return self._embed_with_chunking(text)

    def _embed_safe_text(self, text: str) -> str:
        # Empty input stays empty.
        if not text:
            return ""

        def replace_image_unit(match: re.Match[str]) -> str:
            # Replace a full embedded image block with its description if available.
            image_unit = match.group(0)
            desc_match = _IMAGE_DESCRIPTION_RE.search(image_unit)

            if desc_match:
                description = desc_match.group(1).strip()
                if description:
                    return (
                        "\n\n[Embedded image description]\n"
                        f"{description}\n[/Embedded image description]\n\n"
                    )

            # Keep a small marker when no useful description exists.
            return "\n\n[Embedded image omitted: no description available]\n\n"

        # Replace structured image blocks with descriptions or omission markers.
        text = _IMAGE_UNIT_RE.sub(replace_image_unit, text)

        # Remove raw media payloads that are not useful for embeddings.
        text = _IMAGE_MEDIA_RE.sub("\n\n[Embedded image media omitted]\n\n", text)

        # Shorten data URI image blobs so huge base64 strings are not embedded.
        text = _DATA_IMAGE_URI_RE.sub("data:image;base64,[omitted]", text)

        return text

    def _embed_with_chunking(self, text: str) -> list[float]:
        try:
            # First try embedding the full text normally.
            return self.embed_query(text)

        except Exception as exc:
            # Only retry with chunks for errors that shorter input might fix.
            if not self._is_chunkable_error(exc):
                raise

            # If local HF hit CUDA memory issues, clear cache before retrying.
            if self._backend == "hf":
                try:
                    import torch

                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    # Cache cleanup is best-effort.
                    pass

        # Split by lines so chunks remain somewhat natural/readable.
        lines = text.splitlines()
        if not lines:
            raise RuntimeError("embedding failed and text could not be split")

        # Start with 2 chunks, then increase until embedding succeeds.
        chunk_count = 2

        while chunk_count <= max(2, len(lines)):
            # Clamp chunk count to the valid range.
            chunk_count = max(1, min(chunk_count, len(lines)))

            # Ceiling division gives roughly equal-sized chunks.
            chunk_size = max(1, (len(lines) + chunk_count - 1) // chunk_count)

            chunks = [
                "\n".join(lines[index : index + chunk_size])
                for index in range(0, len(lines), chunk_size)
            ]

            try:
                # Embed non-empty chunks.
                vectors = [self.embed_query(chunk) for chunk in chunks if chunk.strip()]

                if not vectors:
                    raise ValueError("cannot average empty embedding vector list")

                # Average chunk vectors into one document vector.
                dim = len(vectors[0])

                return [
                    sum(vector[index] for vector in vectors) / len(vectors)
                    for index in range(dim)
                ]

            except Exception as exc:
                # If splitting cannot fix this error, fail immediately.
                if not self._is_chunkable_error(exc):
                    raise

                # Clear CUDA cache before trying smaller chunks.
                if self._backend == "hf":
                    try:
                        import torch

                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    except Exception:
                        pass

                # Try again with more, smaller chunks.
                chunk_count += 1

        # Every chunk size failed.
        raise RuntimeError(
            f"embedding failed even after splitting into {chunk_count - 1} line chunks"
        )

    def _is_chunkable_error(self, exc: Exception) -> bool:
        # These errors are commonly fixed by using shorter input chunks.
        message = str(exc).lower()

        return (
            "maximum context length" in message
            or "context length" in message
            or "input_tokens" in message
            or "too many tokens" in message
            or "out of memory" in message
            or "cuda error" in message
        )


class Reranker:
    def __init__(self, settings: Settings) -> None:
        # Store settings and selected backend from config.
        self.settings = settings
        self._backend = settings.rerank_backend

        # Local HF CrossEncoder is only created if the active backend is HF.
        self._cross_encoder = None

        # Only these two backends are supported.
        if self._backend not in {"server", "hf"}:
            raise ValueError(
                f"unsupported rerank_backend={self._backend!r}; expected 'server' or 'hf'"
            )

        # If local HF is explicitly configured, load it directly.
        if self._backend == "hf":
            log.info(
                "using local HF reranker backend: model=%s device=%s",
                self.settings.hf_rerank_model,
                self.settings.rerank_device,
            )
            self._build_hf()
            log.info("local HF reranker backend loaded")
            return

        # Otherwise, try the remote rerank server first.
        log.info(
            "probing rerank server: model=%s base_url=%s",
            self.settings.rerank_model,
            self.settings.rerank_base_url,
        )

        try:
            # Probe during startup so we know immediately if the server is usable.
            self._server_scores("rerank server availability probe", ["probe document"])

            log.info(
                "rerank server available: model=%s base_url=%s",
                self.settings.rerank_model,
                self.settings.rerank_base_url,
            )

        except Exception as err:  # noqa: BLE001 - server down: degrade to local HF
            # If the server fails at startup, switch once to local HF.
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
        # Import lazily so sentence-transformers is only needed for the HF path.
        from sentence_transformers import CrossEncoder

        # CrossEncoder scores query/document pairs directly.
        self._cross_encoder = CrossEncoder(
            self.settings.hf_rerank_model,
            device=self.settings.rerank_device,
            trust_remote_code=True,
        )

    def score(self, query: str, documents: list[str]) -> list[float]:
        """Return one relevance score per document, in the input order."""
        # No documents means no scores.
        if not documents:
            return []

        # Local HF path: score query/document pairs with the CrossEncoder.
        if self._backend == "hf":
            pairs = [(query, document) for document in documents]
            raw_scores = self._cross_encoder.predict(pairs)
            return [float(value) for value in raw_scores]

        # Server path: send the query and documents to the rerank endpoint.
        return self._server_scores(query, documents)

    def top_k(
        self,
        query: str,
        items: list[tuple[str, object]],
        k: int,
    ) -> list[tuple[object, float]]:
        """Rank ``(document_text, payload)`` pairs and return ``(payload, score)``."""
        # Nothing to rank.
        if not items:
            return []

        # Extract document text only for reranking.
        documents = [text for text, _payload in items]

        # Scores come back in the same order as documents.
        scores = self.score(query, documents)

        # Attach scores back to payloads, then sort by score descending.
        ranked = sorted(
            ((payload, score) for (_text, payload), score in zip(items, scores)),
            key=lambda pair: pair[1],
            reverse=True,
        )

        # Negative k should return an empty list.
        return ranked[: max(0, k)]

    def _server_scores(self, query: str, documents: list[str]) -> list[float]:
        # Build the JSON body expected by common rerank endpoints.
        payload = json.dumps(
            {
                "model": self.settings.rerank_model,
                "query": query,
                "documents": documents,
            }
        ).encode("utf-8")

        # Basic JSON request headers.
        headers = {"Content-Type": "application/json"}

        # Add bearer auth only when configured.
        if self.settings.rerank_api_key:
            headers["Authorization"] = f"Bearer {self.settings.rerank_api_key}"

        # Support both common endpoint styles:
        #   base/v1/rerank
        #   base/rerank
        base_url = self.settings.rerank_base_url.rstrip("/")
        if base_url.endswith("/v1"):
            urls = [f"{base_url}/rerank"]
        else:
            urls = [f"{base_url}/v1/rerank", f"{base_url}/rerank"]

        # Try every supported URL variant before failing.
        last_err: Exception | None = None

        for url in urls:
            request = urllib.request.Request(
                url,
                data=payload,
                headers=headers,
                method="POST",
            )

            try:
                # Send the request and parse the JSON response.
                with urllib.request.urlopen(request, timeout=120) as response:
                    body = json.loads(response.read().decode("utf-8"))

                # Normalize response into scores matching the original document order.
                return self._parse_server_scores(body, len(documents))

            except (OSError, urllib.error.URLError, ValueError) as err:
                # Save the error and try the next endpoint variant.
                last_err = err

        # All endpoint variants failed.
        raise RuntimeError(f"rerank server request failed: {last_err}")

    def _parse_server_scores(self, body: object, count: int) -> list[float]:
        """Parse common rerank response shapes into input-order scores."""
        # Common shape used by TEI / Infinity / Jina-style servers:
        # {
        #   "results": [
        #       {"index": 0, "relevance_score": 0.91},
        #       {"index": 1, "relevance_score": 0.42}
        #   ]
        # }
        if isinstance(body, dict) and isinstance(body.get("results"), list):
            scores = [0.0] * count

            for entry in body["results"]:
                # Use the returned index to restore the original document order.
                index = int(entry.get("index", -1))

                # Some servers call it relevance_score, others just score.
                value = entry.get("relevance_score", entry.get("score", 0.0))

                if 0 <= index < count:
                    scores[index] = float(value)

            return scores

        # Simpler shape:
        # {
        #   "scores": [0.91, 0.42]
        # }
        if isinstance(body, dict) and isinstance(body.get("scores"), list):
            return [float(value) for value in body["scores"]][:count]

        # Anything else is not a response shape we understand.
        raise ValueError("unrecognized rerank server response shape")


class ModelGateway:

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

        old = self.settings
        new_llm = self.llm
        new_reranker = self.reranker
        new_embedder = self.embedder

        chat_keys = ("chat_model", "chat_base_url", "chat_api_key", "chat_temperature")
        if any(getattr(old, k) != getattr(settings, k) for k in chat_keys):
            new_llm = self._build_llm(settings)
        rerank_keys = (
            "rerank_backend",
            "rerank_base_url",
            "rerank_api_key",
            "rerank_model",
            "hf_rerank_model",
            "rerank_device",
        )
        if any(getattr(old, k) != getattr(settings, k) for k in rerank_keys):
            new_reranker = self._build_reranker(settings)

        # Keep gateway.embedder consistent with gateway.settings. Rebuilding at
        # runtime does NOT re-embed existing vectors (that happens only at the
        # next bootstrap, which detects the model change): new nodes get vectors
        # in the new model's space while stored vectors keep the old space, so
        # search quality can degrade until a restart re-embeds everything.
        embed_keys = (
            "embed_backend",
            "embed_base_url",
            "embed_api_key",
            "embed_model",
            "hf_embed_model",
            "hf_device",
            "embed_dim",
        )
        if any(getattr(old, k) != getattr(settings, k) for k in embed_keys):
            try:
                new_embedder = Embedder(settings)
                log.warning(
                    "embedder rebuilt for new embed settings; existing stored "
                    "vectors are only re-embedded at the next bootstrap — search "
                    "quality may degrade until restart"
                )
            except Exception as exc:
                log.error(
                    "failed to rebuild embedder for new settings; keeping previous: %s",
                    exc,
                )
                new_embedder = self.embedder

        self.settings = settings
        self.llm = new_llm
        self.reranker = new_reranker
        self.embedder = new_embedder

    def close(self) -> None:
        for obj in (self.embedder, self.reranker, self.llm):
            if hasattr(obj, "close"):
                try:
                    obj.close()
                except Exception:
                    pass
