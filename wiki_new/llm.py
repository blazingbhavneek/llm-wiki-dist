"""
LLM access layer. make_llm builds the ChatOpenAI client from config;
structured_ainvoke runs a structured-output call and falls back to JSON-only
prompting + manual validation.
Imports from utils: extract_json_from_text (parses the fallback JSON).

Package `wiki/` — lossless Markdown wiki generator. Module layout
(low-level to high-level; imports only ever point downward in this list):

- models.py    Runtime config constants (SOURCE_PATH, OUTPUT_ROOT, BASE_URL,
               GEN_MODEL, GENERATION_LINES, ... PARTITION_RETRY_ATTEMPTS) and all
               Pydantic schemas + the CurrentFileState dataclass (FileRef,
               NewFileRef, GenerationDecision, VerificationResult, RepairResult,
               ChunkSummary, TopicRange, H1Plan, H1Layout, LeafPagePlan).
               No wiki imports.
- utils.py     Pure stdlib helpers: line-range/markdown (range_to_markdown,
               clamp_range_to_chunk, split_chunk_ranges), file/JSON IO
               (read_lines, write_json, load_json), filenames/slugs
               (slugify, make_unique_filename), source chunking
               (chunk_source_lines_preserving_tables, fixed_windows), manifest +
               markdown-file records (init_manifest, add_or_update_file_record,
               create_markdown_file, add_chunk_record,
               find_best_target_for_source_window, overlap_size),
               extract_json_from_text. No wiki imports.
- llm.py       LLM client: make_llm, structured_ainvoke (structured-output call
               with JSON fallback). Imports: utils.
- prompts.py   All prompt builders build_*_prompt (chunk summary, H1 plan, H1
               layout, leaf page, generation, verification, repair).
               Imports: models.
- planning.py  Hierarchy planning + tree rendering: chunk-summary ledger
               (format_summary_ledger, summaries_for_range), exact-partition
               validation (validate_exact_partition,
               structured_partition_ainvoke_with_retries, partition_or_fallback,
               assert_exact_coverage) and the wiki-tree writers
               (render_hierarchical_wiki, write_navigation_index,
               write_topic_plan_document, hierarchy_to_manifest,
               planned_leaf_pages). Imports: models, utils, llm.
- generate.py  Flat (non-hierarchical) generation phase: enforce_generation_rules,
               parse_part_number, forced_part_ref, phase_generate_flat.
               Imports: models, utils, prompts, llm.
- phases.py    Hierarchical generation (phase_generate), verification
               (verify_one_window, phase_verify), repair (phase_repair) and the
               batch runner / entrypoint (collect_source_files,
               make_config_for_source, process_one_source, async_main, main).
               Imports: generate, planning, prompts, utils, llm, models.

Entrypoint: ../md.py is a thin shim that calls wiki.phases.main.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from wiki_new.utils import extract_json_from_text, utc_now_iso

# ---------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------


LLM_CACHE_VERSION = "structured-v1"
_CACHE_LOCKS: dict[str, asyncio.Lock] = {}
_CACHE_STATS = {
    "hits": 0,
    "misses": 0,
    "writes": 0,
    "disabled": 0,
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _cache_enabled() -> bool:
    return _env_bool("WIKI_LLM_CACHE", True)


def _cache_dir() -> Path:
    configured = os.environ.get("WIKI_LLM_CACHE_DIR")
    if configured:
        return Path(configured)
    return Path.cwd() / ".cache" / "wiki_llm"


def _thinking_enabled_default() -> bool:
    # Backward-compatible optional global override. Leave unset for the server's
    # default chat template; low-risk stages pass per-call overrides instead.
    return _env_bool("WIKI_QWEN_ENABLE_THINKING", True)


def _default_thinking_override() -> bool | None:
    if "WIKI_QWEN_ENABLE_THINKING" not in os.environ:
        return None
    return _thinking_enabled_default()


def _qwen_extra_body(enable_thinking: bool) -> dict[str, Any]:
    extra_body: dict[str, Any] = {
        "chat_template_kwargs": {"enable_thinking": enable_thinking}
    }
    top_k = _env_int("WIKI_LLM_TOP_K")
    if top_k is not None:
        extra_body["top_k"] = top_k
    return extra_body


def _should_send_qwen_controls(base_url: str) -> bool:
    if _env_bool("WIKI_QWEN_DISABLE_CONTROLS", False):
        return False
    if _env_bool("WIKI_QWEN_FORCE_CONTROLS", False):
        return True
    return "api.openai.com" not in base_url.lower()


def _normalize_base_url(base_url: str) -> str:
    value = base_url.strip()
    if value.startswith(("http://", "https://")):
        return value
    return f"http://{value}"


def _llm_fingerprint(llm: ChatOpenAI) -> dict[str, Any]:
    return {
        "model": getattr(llm, "model_name", None) or getattr(llm, "model", None),
        "base_url": str(getattr(llm, "openai_api_base", "") or ""),
        "temperature": getattr(llm, "temperature", None),
        "top_p": getattr(llm, "top_p", None),
        "presence_penalty": getattr(llm, "presence_penalty", None),
        "frequency_penalty": getattr(llm, "frequency_penalty", None),
        "extra_body": getattr(llm, "extra_body", None),
        "model_kwargs": getattr(llm, "model_kwargs", None),
    }


def _call_options_fingerprint(
    *,
    enable_thinking: bool | None,
) -> dict[str, Any]:
    return {
        "enable_thinking": enable_thinking,
    }


def _message_to_cache_dict(message: Any) -> dict[str, Any]:
    return {
        "type": message.__class__.__name__,
        "role": getattr(message, "type", None),
        "name": getattr(message, "name", None),
        "content": getattr(message, "content", str(message)),
    }


def _cache_key(
    *,
    llm: ChatOpenAI,
    schema_cls: type[BaseModel],
    messages: list[Any],
    max_output_tokens: int | None,
    phase: str,
    enable_thinking: bool | None,
) -> str:
    payload = {
        "cache_version": LLM_CACHE_VERSION,
        "prompt_version": os.environ.get("WIKI_PROMPT_VERSION", "default"),
        "structured_output_mode": _structured_output_mode(),
        "phase": phase,
        "schema_name": schema_cls.__name__,
        "schema": schema_cls.model_json_schema(),
        "llm": _llm_fingerprint(llm),
        "call_options": _call_options_fingerprint(enable_thinking=enable_thinking),
        "max_output_tokens": max_output_tokens,
        "messages": [_message_to_cache_dict(message) for message in messages],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> Path:
    return _cache_dir() / key[:2] / f"{key}.json"


def _read_cache(
    *,
    key: str,
    schema_cls: type[BaseModel],
) -> BaseModel | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("cache_version") != LLM_CACHE_VERSION:
            return None
        return schema_cls.model_validate(data["result"])
    except Exception:  # noqa: BLE001 - corrupt cache entries are misses
        return None


def _write_cache(
    *,
    key: str,
    phase: str,
    schema_cls: type[BaseModel],
    llm: ChatOpenAI,
    max_output_tokens: int | None,
    result: BaseModel,
) -> None:
    path = _cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cache_version": LLM_CACHE_VERSION,
        "created_at": utc_now_iso(),
        "key": key,
        "phase": phase,
        "schema": schema_cls.__name__,
        "llm": _llm_fingerprint(llm),
        "max_output_tokens": max_output_tokens,
        "result": result.model_dump(),
    }
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        tmp_name = handle.name
    Path(tmp_name).replace(path)


def get_llm_cache_stats() -> dict[str, int]:
    return dict(_CACHE_STATS)


def make_llm(
    model: str,
    base_url: str,
    api_key: str,
    temperature: float = 0.0,
    timeout: int = 300,
    enable_thinking: bool | None = None,
) -> ChatOpenAI:
    thinking = (
        _default_thinking_override() if enable_thinking is None else enable_thinking
    )
    normalized_base_url = _normalize_base_url(base_url)
    top_p = _env_float("WIKI_LLM_TOP_P")
    presence_penalty = _env_float("WIKI_LLM_PRESENCE_PENALTY")
    frequency_penalty = _env_float("WIKI_LLM_FREQUENCY_PENALTY")

    kwargs: dict[str, Any] = {
        "model": model,
        "base_url": normalized_base_url,
        "api_key": api_key,
        "temperature": temperature,
        "timeout": timeout,
    }
    if thinking is not None and _should_send_qwen_controls(normalized_base_url):
        kwargs["extra_body"] = _qwen_extra_body(thinking)
    if top_p is not None:
        kwargs["top_p"] = top_p
    if presence_penalty is not None:
        kwargs["presence_penalty"] = presence_penalty
    if frequency_penalty is not None:
        kwargs["frequency_penalty"] = frequency_penalty

    return ChatOpenAI(
        **kwargs,
    )


def _structured_output_mode() -> str:
    mode = os.environ.get("WIKI_STRUCTURED_OUTPUT_MODE", "auto").strip().lower()
    if mode in {"json", "json-only"}:
        return "json"
    if mode in {"structured", "structured-first"}:
        return "structured"
    return "auto"


def _should_use_json_first(llm: ChatOpenAI) -> bool:
    mode = _structured_output_mode()
    if mode == "json":
        return True
    # Default: prefer server-side structured output (response_format json_schema /
    # guided decoding). vLLM-style local servers support it and it returns valid
    # JSON even when the model emits reasoning tokens, so the brittle text-based
    # JSON extractor is only a last-resort fallback.
    return False


def _structured_output_method() -> str:
    # ChatOpenAI default is "function_calling"; local servers implement
    # response_format json_schema (guided decoding) far more reliably.
    return os.environ.get("WIKI_STRUCTURED_OUTPUT_METHOD", "json_schema").strip()


async def _json_schema_ainvoke(
    call_llm: Any,
    schema_cls: type[BaseModel],
    messages: list[Any],
) -> BaseModel:
    schema_json = json.dumps(schema_cls.model_json_schema(), indent=2)

    fallback_messages = list(messages)
    fallback_messages.append(
        HumanMessage(
            content=(
                "Return ONLY valid JSON matching this JSON Schema. "
                "Be concise. Do not include extra prose.\n\n"
                f"{schema_json}"
            )
        )
    )

    raw = await call_llm.ainvoke(fallback_messages)
    text = raw.content if hasattr(raw, "content") else str(raw)
    data = extract_json_from_text(text)
    return schema_cls.model_validate(data)


def _request_timeout_seconds() -> int:
    value = _env_int("WIKI_LLM_REQUEST_TIMEOUT")
    # Thinking on large chunks routinely exceeds 180s; default higher.
    return 420 if value is None else value


def _request_retries() -> int:
    value = _env_int("WIKI_LLM_REQUEST_RETRIES")
    return 3 if value is None else value


def _fallback_retries() -> int:
    value = _env_int("WIKI_LLM_FALLBACK_RETRIES")
    return 0 if value is None else value


def _llm_base_url(llm: ChatOpenAI) -> str:
    return str(getattr(llm, "openai_api_base", "") or "")


def _bind_for_structured_call(
    *,
    llm: ChatOpenAI,
    max_output_tokens: int | None,
    enable_thinking: bool | None,
) -> Any:
    bind_kwargs: dict[str, Any] = {}
    if max_output_tokens is not None:
        bind_kwargs["max_tokens"] = max_output_tokens

    base_url = _llm_base_url(llm)
    if enable_thinking is not None and _should_send_qwen_controls(base_url):
        bind_kwargs["extra_body"] = _qwen_extra_body(enable_thinking)

    if not bind_kwargs:
        return llm
    return llm.bind(**bind_kwargs)


async def _attempt_with_timeout(
    *,
    llm: ChatOpenAI,
    schema_cls: type[BaseModel],
    messages: list[Any],
    max_output_tokens: int | None,
    enable_thinking: bool | None,
    timeout_seconds: int,
) -> BaseModel:
    return await asyncio.wait_for(
        _structured_ainvoke_uncached(
            llm=llm,
            schema_cls=schema_cls,
            messages=messages,
            max_output_tokens=max_output_tokens,
            enable_thinking=enable_thinking,
        ),
        timeout=timeout_seconds,
    )


async def _structured_ainvoke_with_retry_policy(
    *,
    llm: ChatOpenAI,
    schema_cls: type[BaseModel],
    messages: list[Any],
    max_output_tokens: int | None,
    enable_thinking: bool | None,
    phase: str,
) -> BaseModel:
    timeout_seconds = _request_timeout_seconds()
    attempts = 1 + max(0, _request_retries())
    last_exc: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await _attempt_with_timeout(
                llm=llm,
                schema_cls=schema_cls,
                messages=messages,
                max_output_tokens=max_output_tokens,
                enable_thinking=enable_thinking,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - retry transient/loop failures
            last_exc = exc
            detail = str(exc) or type(exc).__name__
            if isinstance(exc, asyncio.TimeoutError):
                detail = f"TimeoutError after {timeout_seconds}s"
            print(
                f"[LLMRetry] phase={phase} attempt={attempt}/{attempts} "
                f"thinking={enable_thinking} failed: {detail}"
            )

    if enable_thinking is True:
        fallback_attempts = 1 + max(0, _fallback_retries())
        for attempt in range(1, fallback_attempts + 1):
            try:
                print(
                    f"[LLMFallback] phase={phase} switching to non-thinking "
                    f"after {attempts} failed thinking attempt(s)"
                )
                return await _attempt_with_timeout(
                    llm=llm,
                    schema_cls=schema_cls,
                    messages=messages,
                    max_output_tokens=max_output_tokens,
                    enable_thinking=True,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                print(
                    f"[LLMFallback] phase={phase} attempt={attempt}/{fallback_attempts} "
                    f"failed: {exc}"
                )

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"LLM call failed without exception for phase={phase}")


async def structured_ainvoke(
    llm: ChatOpenAI,
    schema_cls: type[BaseModel],
    messages: list[Any],
    max_output_tokens: int | None = None,
    phase: str | None = None,
    cache: bool | None = None,
    enable_thinking: bool | None = None,
) -> BaseModel:
    phase_name = phase or schema_cls.__name__
    use_cache = _cache_enabled() if cache is None else cache
    if not use_cache:
        _CACHE_STATS["disabled"] += 1
        return await _structured_ainvoke_with_retry_policy(
            llm=llm,
            schema_cls=schema_cls,
            messages=messages,
            max_output_tokens=max_output_tokens,
            enable_thinking=enable_thinking,
            phase=phase_name,
        )

    key = _cache_key(
        llm=llm,
        schema_cls=schema_cls,
        messages=messages,
        max_output_tokens=max_output_tokens,
        phase=phase_name,
        enable_thinking=enable_thinking,
    )
    lock = _CACHE_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _read_cache(key=key, schema_cls=schema_cls)
        if cached is not None:
            _CACHE_STATS["hits"] += 1
            if _env_bool("WIKI_LLM_CACHE_LOG", False):
                print(f"[LLMCache] hit phase={phase_name} key={key[:12]}")
            return cached

        _CACHE_STATS["misses"] += 1
        if _env_bool("WIKI_LLM_CACHE_LOG", False):
            print(f"[LLMCache] miss phase={phase_name} key={key[:12]}")
        result = await _structured_ainvoke_with_retry_policy(
            llm=llm,
            schema_cls=schema_cls,
            messages=messages,
            max_output_tokens=max_output_tokens,
            enable_thinking=enable_thinking,
            phase=phase_name,
        )
        _write_cache(
            key=key,
            phase=phase_name,
            schema_cls=schema_cls,
            llm=llm,
            max_output_tokens=max_output_tokens,
            result=result,
        )
        _CACHE_STATS["writes"] += 1
        return result


async def _structured_ainvoke_uncached(
    *,
    llm: ChatOpenAI,
    schema_cls: type[BaseModel],
    messages: list[Any],
    max_output_tokens: int | None = None,
    enable_thinking: bool | None = None,
) -> BaseModel:
    call_llm = _bind_for_structured_call(
        llm=llm,
        max_output_tokens=max_output_tokens,
        enable_thinking=enable_thinking,
    )

    if _should_use_json_first(llm):
        return await _json_schema_ainvoke(call_llm, schema_cls, messages)

    try:
        structured = call_llm.with_structured_output(
            schema_cls, method=_structured_output_method()
        )
        result = await structured.ainvoke(messages)

        if isinstance(result, schema_cls):
            return result

        return schema_cls.model_validate(result)

    except Exception:
        return await _json_schema_ainvoke(call_llm, schema_cls, messages)
