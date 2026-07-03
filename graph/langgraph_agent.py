from __future__ import annotations

import copy
import json
import logging
import traceback
import warnings
from datetime import datetime
from threading import Event
from typing import Any, Callable

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

from .models import (
    AgentAnswer,
    Settings,
    Subrun,
    finish as FinishArgs,
    follow_link as FollowLinkArgs,
    read as ReadArgs,
    search as SearchArgs,
)
from .utils import (
    SUBAGENT_SYSTEM_PROMPT,
    clean_node_ref,
    dedupe,
    format_node_full,
    node_ref,
)

import re


_IMAGE_UNIT_RE = re.compile(
    r"<image-unit\b[^>]*>(?P<body>.*?)</image-unit>",
    re.IGNORECASE | re.DOTALL,
)

_IMAGE_DESCRIPTION_RE = re.compile(
    r"<image-description\b[^>]*>(?P<description>.*?)</image-description>",
    re.IGNORECASE | re.DOTALL,
)

_IMAGE_MEDIA_RE = re.compile(
    r"<image-media\b[^>]*>.*?</image-media>",
    re.IGNORECASE | re.DOTALL,
)


def strip_image_media(text: str) -> str:
    """Remove embedded image media payloads and keep image descriptions."""
    if not text:
        return text

    def replace_image_unit(match: re.Match[str]) -> str:
        body = match.group("body")
        description = _IMAGE_DESCRIPTION_RE.search(body)
        if description:
            return description.group("description").strip()
        return _IMAGE_MEDIA_RE.sub("", body).strip()

    return _IMAGE_UNIT_RE.sub(replace_image_unit, text).strip()


log = logging.getLogger("graph_langgraph_agent")


# =============================================================================
# Debug logging
# =============================================================================


def debug_print(label: str, data: Any = None) -> None:
    """
    Simple stdout debug logger.

    Uses print(..., flush=True) so logs appear immediately in Docker/server logs.
    """
    try:
        timestamp = datetime.now().isoformat()

        if data is None:
            print(f"[GRAPH_AGENT_DEBUG] {timestamp} | {label}", flush=True)
            return

        text = json.dumps(data, ensure_ascii=False, default=str, indent=2)
        print(
            f"[GRAPH_AGENT_DEBUG] {timestamp} | {label}\n{text[:12000]}",
            flush=True,
        )
    except Exception:
        print(
            f"[GRAPH_AGENT_DEBUG] {datetime.now().isoformat()} | {label} | <debug serialization failed>",
            flush=True,
        )


# =============================================================================
# Image/base64 filtering for LangGraph messages
# =============================================================================


_IMAGE_BLOCK_TYPES = {
    "image",
    "image_url",
    "input_image",
    "input_image_url",
}


_IMAGE_KEYS = {
    "image",
    "image_url",
    "input_image",
    "input_image_url",
    "b64_json",
}


def _looks_like_image_data_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip().lower()
    return stripped.startswith("data:image/") or stripped.startswith("data:application/octet-stream;base64,")


def _sanitize_string_for_llm(value: str) -> str:
    """
    Apply the same image/base64 stripping used by the shared LLM client.
    """
    return strip_image_media(value)


def _sanitize_content_for_llm(content: Any) -> Any:
    """
    Recursively sanitize LangChain/OpenAI message content.

    Handles:
    - plain strings
    - OpenAI multimodal list content
    - dict blocks containing image_url/input_image/b64_json
    - nested dict/list values
    """
    if isinstance(content, str):
        return _sanitize_string_for_llm(content)

    if isinstance(content, list):
        sanitized_items: list[Any] = []

        for item in content:
            # OpenAI-style content block:
            # {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
            if isinstance(item, dict):
                item_type = str(item.get("type") or "").strip().lower()

                if item_type in _IMAGE_BLOCK_TYPES:
                    continue

                # Some providers use blocks with image keys even without type.
                if any(key in item for key in _IMAGE_KEYS):
                    # Preserve text-only blocks; drop obvious image blocks.
                    text_value = item.get("text")
                    if isinstance(text_value, str):
                        sanitized_text = _sanitize_string_for_llm(text_value).strip()
                        if sanitized_text:
                            sanitized_items.append(
                                {
                                    "type": "text",
                                    "text": sanitized_text,
                                }
                            )
                    continue

                sanitized_item = _sanitize_content_for_llm(item)

                # Avoid empty dict/list noise.
                if sanitized_item not in ({}, [], "", None):
                    sanitized_items.append(sanitized_item)
                continue

            sanitized_item = _sanitize_content_for_llm(item)

            if sanitized_item not in ({}, [], "", None):
                sanitized_items.append(sanitized_item)

        return sanitized_items

    if isinstance(content, dict):
        item_type = str(content.get("type") or "").strip().lower()

        if item_type in _IMAGE_BLOCK_TYPES:
            return {}

        sanitized: dict[str, Any] = {}

        for key, value in content.items():
            key_lower = str(key).lower()

            # Drop explicit image fields.
            if key_lower in _IMAGE_KEYS:
                continue

            # Drop data:image URLs even if hidden under another key.
            if _looks_like_image_data_url(value):
                continue

            sanitized_value = _sanitize_content_for_llm(value)

            if sanitized_value not in ({}, [], "", None):
                sanitized[key] = sanitized_value

        return sanitized

    return content


def _sanitize_message_for_llm(message: Any) -> Any:
    """
    Sanitize one LangGraph/LangChain message.

    Keeps the same message type where possible so LangGraph tool-call metadata
    remains intact.
    """
    if isinstance(message, dict):
        entry = dict(message)

        if "content" in entry:
            entry["content"] = _sanitize_content_for_llm(entry.get("content"))

        return entry

    if isinstance(message, BaseMessage):
        sanitized_content = _sanitize_content_for_llm(getattr(message, "content", ""))

        # LangChain messages are Pydantic models in modern versions.
        if hasattr(message, "model_copy"):
            return message.model_copy(update={"content": sanitized_content})

        # Fallback for older versions.
        cloned = copy.copy(message)
        try:
            cloned.content = sanitized_content
            return cloned
        except Exception:
            return message

    # Unknown message-like object.
    content = getattr(message, "content", None)
    if content is not None:
        try:
            cloned = copy.copy(message)
            cloned.content = _sanitize_content_for_llm(content)
            return cloned
        except Exception:
            return message

    return message


def _sanitize_messages_for_llm(messages: list[Any]) -> list[Any]:
    """
    Sanitize every message before the model sees it.
    """
    return [_sanitize_message_for_llm(message) for message in messages]


def _sanitize_tool_output(text: Any) -> str:
    """
    Tool outputs become ToolMessages and later go back to the LLM.
    Sanitize them immediately, and they will also be sanitized again by
    the pre_model_hook.
    """
    if not isinstance(text, str):
        text = str(text)
    return _sanitize_string_for_llm(text)


# =============================================================================
# Agent support utilities
# =============================================================================


class AgentStopped(Exception):
    """Raised when a client cancels an in-flight LangGraph run."""


def _check_stop(stop_event: Event | None) -> None:
    if stop_event is not None and stop_event.is_set():
        raise AgentStopped("agent run cancelled")


def _base_url(value: str) -> str:
    base = (value or "").rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base


def _model(settings: Settings) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.chat_model,
        base_url=_base_url(settings.chat_base_url),
        api_key=settings.chat_api_key or "local",
        temperature=settings.chat_temperature,
        timeout=300,
        max_retries=0,
        extra_body={"chat_template_kwargs": {"enable_thinking": True}},
    )


def _compile_agent(
    settings: Settings,
    tools: list[StructuredTool],
    prompt: str,
    stop_event: Event | None,
):
    """
    Compile a LangGraph ReAct agent with mandatory pre-model sanitization.

    This is the main protection point:
    LangGraph can build up messages from user inputs, assistant outputs,
    and tool observations. Before every LLM call, state["messages"] is cleaned.
    """
    safe_prompt = _sanitize_string_for_llm(prompt or "")

    def before_model(state: dict[str, Any]) -> dict[str, Any]:
        _check_stop(stop_event)

        messages = state.get("messages", [])
        if not isinstance(messages, list):
            messages = []

        sanitized_messages = _sanitize_messages_for_llm(messages)

        debug_print(
            "llm.pre_model_hook.sanitized",
            {
                "input_message_count": len(messages),
                "output_message_count": len(sanitized_messages),
            },
        )

        return {
            "llm_input_messages": sanitized_messages,
        }

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return create_react_agent(
            _model(settings),
            tools=tools,
            prompt=safe_prompt,
            pre_model_hook=before_model,
            version="v2",
        )


def _message_content(message: Any) -> str:
    content = getattr(message, "content", "")

    if isinstance(content, str):
        return _sanitize_string_for_llm(content)

    try:
        return _sanitize_string_for_llm(
            json.dumps(content, ensure_ascii=False, default=str)
        )
    except Exception:
        return _sanitize_string_for_llm(str(content))


def _last_message_text(state: dict[str, Any]) -> str:
    for message in reversed(state.get("messages", [])):
        content = getattr(message, "content", "")

        if isinstance(content, str):
            text = _sanitize_string_for_llm(content).strip()
            if text:
                return text

        if isinstance(content, list):
            parts: list[str] = []

            for item in content:
                if isinstance(item, str):
                    parts.append(_sanitize_string_for_llm(item))
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(_sanitize_string_for_llm(item["text"]))

            text = "\n".join(parts).strip()
            if text:
                return text

    return ""


def _steps(state: dict[str, Any]) -> int:
    return sum(
        1
        for message in state.get("messages", [])
        if isinstance(message, AIMessage)
    )


def format_lead_candidate(result: dict[str, Any]) -> str:
    """
    Formats one early-exit retrieval candidate so the LangGraph lead agent can reuse it.
    """
    node = result["node"]

    evidence_lines: list[str] = []

    for ev in result.get("evidence", [])[:5]:
        text = _sanitize_string_for_llm(
            " ".join((ev.get("text") or "").split())
        )
        if text:
            evidence_lines.append(f"  - {text[:700]}")

    evidence_text = (
        "\n".join(evidence_lines)
        if evidence_lines
        else "  - no evidence snippets"
    )

    return (
        f"- node_id: `{node.id}`\n"
        f"  title: {_sanitize_string_for_llm(str(node.title))}\n"
        f"  summary: {_sanitize_string_for_llm(str(node.summary))}\n"
        f"  evidence:\n{evidence_text}\n"
        f"  next_action: if relevant, call explore(node_ids=['{node.id}']) or include this id with other candidates"
    )


# =============================================================================
# Lead agent
# =============================================================================


def run_lead_agent(
    session: Any,
    question: str,
    emit: Callable[[dict[str, Any]], None],
    stop_event: Event | None = None,
    seed_context: str = "",
    seed_node_ids: list[str] | None = None,
) -> AgentAnswer:
    """
    LangGraph lead agent.

    Important behavior:
    1. Sanitizes all input before LLM calls.
    2. Sanitizes all tool output before returning it to LangGraph.
    3. Sanitizes LangGraph message history in pre_model_hook.
    4. Supports seed_context and seed_node_ids from early retrieval.
    """

    question = _sanitize_string_for_llm(str(question or "")).strip()
    seed_context = _sanitize_string_for_llm(str(seed_context or "")).strip()
    seed_node_ids = seed_node_ids or []

    evidence: list[str] = []
    finished: dict[str, Any] = {
        "answer": "",
        "cited_node_ids": [],
    }

    debug_print(
        "lead.start",
        {
            "question": question,
            "seed_node_ids": seed_node_ids,
            "has_seed_context": bool(seed_context.strip()),
            "agent_max_steps": session.settings.agent_max_steps,
            "rerank_top_k": session.settings.rerank_top_k,
        },
    )

    class LeadSearchArgs(BaseModel):
        """Search the knowledge graph for relevant nodes."""

        text: str = Field(..., description="Search query text")

    class LeadExploreArgs(BaseModel):
        """Dispatch subagents to deeply explore specific node IDs."""

        node_ids: list[str] = Field(
            ...,
            description="Exact node IDs to explore. Use node IDs returned by search.",
        )

    class LeadFinishArgs(BaseModel):
        """Finish with the final answer and cited node IDs."""

        answer: str = Field(..., description="Final answer to the user")
        cited_node_ids: list[str] | None = Field(
            default=None,
            description="Node IDs supporting the final answer",
        )

    def _safe_dedupe(ids: list[str]) -> list[str]:
        try:
            return dedupe(ids)
        except Exception:
            seen: set[str] = set()
            out: list[str] = []

            for item in ids:
                if item and item not in seen:
                    seen.add(item)
                    out.append(item)

            return out

    def _count_steps(state: dict[str, Any]) -> int:
        messages = state.get("messages", []) if isinstance(state, dict) else []
        return max(1, len(messages))

    def _format_search_results(results: list[dict[str, Any]]) -> str:
        if not results:
            return "no nodes found"

        blocks: list[str] = []

        for r in results:
            node = r["node"]

            ev_lines: list[str] = []

            for ev in r.get("evidence", [])[:4]:
                text = _sanitize_string_for_llm(
                    " ".join((ev.get("text") or "").split())
                )
                if text:
                    ev_lines.append(f"    - {text[:500]}")

            evidence_text = (
                "\n".join(ev_lines)
                if ev_lines
                else "    - no evidence snippets"
            )

            blocks.append(
                f"- node_id: `{node.id}`\n"
                f"  title: {_sanitize_string_for_llm(str(node.title))}\n"
                f"  summary: {_sanitize_string_for_llm(str(node.summary))}\n"
                f"  evidence:\n{evidence_text}\n"
                f"  next_action: if relevant, call explore(node_ids=['{node.id}'])"
            )

        return _sanitize_tool_output("\n".join(blocks))

    def search_tool(text: str) -> str:
        _check_stop(stop_event)

        query = _sanitize_string_for_llm(str(text or "")).strip()

        debug_print(
            "lead.tool.search.start",
            {
                "query": query,
            },
        )

        emit({"type": "search", "phase": "main", "query": query})

        try:
            results = session.search_with_evidence(
                query,
                limit=session.settings.rerank_top_k,
            )

            debug_print(
                "lead.tool.search.results",
                {
                    "query": query,
                    "count": len(results),
                    "nodes": [
                        {
                            "id": r["node"].id,
                            "title": _sanitize_string_for_llm(str(r["node"].title)),
                            "summary": _sanitize_string_for_llm(str(r["node"].summary)),
                            "score": r.get("score"),
                            "evidence_count": len(r.get("evidence", [])),
                        }
                        for r in results
                    ],
                },
            )

        except Exception as exc:
            debug_print(
                "lead.tool.search.search_with_evidence_failed",
                {
                    "query": query,
                    "error": repr(exc),
                },
            )
            traceback.print_exc()

            log.info("lead evidence search failed; node-only: %s", exc)

            nodes = session.search(query, limit=session.settings.rerank_top_k)
            results = [
                {
                    "node": n,
                    "why": [],
                    "evidence": [],
                }
                for n in nodes
            ]

            debug_print(
                "lead.tool.search.node_only_results",
                {
                    "query": query,
                    "count": len(results),
                    "nodes": [
                        {
                            "id": r["node"].id,
                            "title": _sanitize_string_for_llm(str(r["node"].title)),
                        }
                        for r in results
                    ],
                },
            )

        # Safety net:
        # If the model generated a brittle narrow query and got 0 results,
        # retry with the original full user question.
        if not results and query and query != question.strip():
            debug_print(
                "lead.tool.search.retry_full_question.start",
                {
                    "failed_query": query,
                    "full_question": question,
                },
            )

            emit({"type": "search", "phase": "main", "query": question})

            try:
                results = session.search_with_evidence(
                    question,
                    limit=session.settings.rerank_top_k,
                )

                debug_print(
                    "lead.tool.search.retry_full_question.results",
                    {
                        "count": len(results),
                        "nodes": [
                            {
                                "id": r["node"].id,
                                "title": _sanitize_string_for_llm(str(r["node"].title)),
                                "summary": _sanitize_string_for_llm(str(r["node"].summary)),
                                "score": r.get("score"),
                                "evidence_count": len(r.get("evidence", [])),
                            }
                            for r in results
                        ],
                    },
                )

            except Exception as exc:
                debug_print(
                    "lead.tool.search.retry_full_question.failed",
                    {
                        "error": repr(exc),
                    },
                )
                traceback.print_exc()

        return _format_search_results(results)

    def explore_tool(node_ids: list[str]) -> str:
        _check_stop(stop_event)

        cleaned_node_ids = [
            clean_node_ref(_sanitize_string_for_llm(str(node_id)))
            for node_id in node_ids or []
            if str(node_id or "").strip()
        ]

        debug_print(
            "lead.tool.explore.start",
            {
                "raw_node_ids": node_ids,
                "cleaned_node_ids": cleaned_node_ids,
                "question": question,
            },
        )

        if not cleaned_node_ids:
            return "no valid node_ids supplied. Search first, then call explore with exact node IDs."

        result = session._run_subagents(
            cleaned_node_ids,
            question,
            evidence,
            emit,
            stop_event=stop_event,
        )

        result = _sanitize_tool_output(result)

        debug_print(
            "lead.tool.explore.done",
            {
                "evidence": evidence,
                "result_preview": result[:3000],
            },
        )

        return result

    def finish_tool(
        answer: str,
        cited_node_ids: list[str] | None = None,
    ) -> str:
        _check_stop(stop_event)

        finished["answer"] = _sanitize_string_for_llm(str(answer or "")).strip()
        finished["cited_node_ids"] = [
            clean_node_ref(_sanitize_string_for_llm(str(nid)))
            for nid in (cited_node_ids or [])
            if str(nid or "").strip()
        ]

        debug_print(
            "lead.tool.finish",
            {
                "answer_len": len(finished["answer"]),
                "answer_preview": finished["answer"][:1500],
                "cited_node_ids": finished["cited_node_ids"],
            },
        )

        return _sanitize_tool_output(json.dumps(finished, ensure_ascii=False))

    tools = [
        StructuredTool.from_function(
            search_tool,
            name="search",
            description=LeadSearchArgs.__doc__ or "Search for relevant nodes.",
            args_schema=LeadSearchArgs,
        ),
        StructuredTool.from_function(
            explore_tool,
            name="explore",
            description=LeadExploreArgs.__doc__ or "Explore selected node IDs using subagents.",
            args_schema=LeadExploreArgs,
        ),
        StructuredTool.from_function(
            finish_tool,
            name="finish",
            description=LeadFinishArgs.__doc__ or "Finish with the final answer.",
            args_schema=LeadFinishArgs,
            return_direct=True,
        ),
    ]

    lead_prompt = globals().get(
        "LEAD_AGENT_PROMPT",
        """
You are the lead research agent.

You can use these tools:
- search(text): search for candidate nodes
- explore(node_ids): dispatch subagents to inspect specific nodes
- finish(answer, cited_node_ids): provide final answer

Rules:
- Prefer exact node IDs from search results.
- If candidate nodes are already provided in the user message, inspect them first.
- Use explore(...) when you have relevant node IDs.
- Do not endlessly search with tiny keyword variations.
- Finish with a concise answer and cite supporting node IDs.
""".strip(),
    )

    lead_prompt = _sanitize_string_for_llm(str(lead_prompt or ""))

    agent = _compile_agent(
        session.settings,
        tools=tools,
        prompt=lead_prompt,
        stop_event=stop_event,
    )

    user_content = question

    if seed_context.strip():
        user_content = (
            f"{question}\n\n"
            "Important: initial retrieval already found candidate nodes for this question.\n"
            "Do not discard these candidates.\n"
            "If they appear relevant, call explore(node_ids=[...]) using the exact node IDs below.\n\n"
            "Initial candidate nodes:\n"
            f"{seed_context}\n\n"
            f"Candidate node IDs: {', '.join(seed_node_ids)}\n"
        )

    user_content = _sanitize_string_for_llm(user_content)

    debug_print(
        "lead.invoke.start",
        {
            "recursion_limit": max(8, session.settings.agent_max_steps * 2 + 4),
            "user_content_preview": user_content[:4000],
            "tool_names": [tool.name for tool in tools],
        },
    )

    emit({"type": "route", "mode": "deep", "reason": "lead agent started"})

    state: dict[str, Any] | Any

    try:
        state = agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": user_content,
                    }
                ]
            },
            config={
                "recursion_limit": max(
                    8,
                    session.settings.agent_max_steps * 2 + 4,
                ),
                # Optional but useful with SQLite/tool thread issues.
                "max_concurrency": 1,
            },
        )

        debug_print(
            "lead.invoke.done",
            {
                "message_count": len(state.get("messages", [])) if isinstance(state, dict) else None,
                "steps": _count_steps(state) if isinstance(state, dict) else None,
                "last_message_preview": _last_message_text(state)[:3000] if isinstance(state, dict) else str(state)[:3000],
                "finished": finished,
                "evidence": evidence,
            },
        )

    except Exception as exc:
        debug_print(
            "lead.invoke.failed",
            {
                "error": repr(exc),
            },
        )
        traceback.print_exc()
        raise

    # If finish_tool was return_direct=True, sometimes the final message is
    # the JSON returned by finish_tool. Parse it as a fallback.
    if not finished["answer"] and isinstance(state, dict):
        last_text = _last_message_text(state).strip()

        try:
            parsed = json.loads(last_text)
            if isinstance(parsed, dict):
                maybe_answer = _sanitize_string_for_llm(
                    str(parsed.get("answer") or "")
                ).strip()
                maybe_cited = parsed.get("cited_node_ids") or []

                if maybe_answer:
                    finished["answer"] = maybe_answer
                    finished["cited_node_ids"] = [
                        clean_node_ref(_sanitize_string_for_llm(str(nid)))
                        for nid in maybe_cited
                        if str(nid or "").strip()
                    ]

                    debug_print(
                        "lead.finish.parsed_from_last_message",
                        {
                            "answer_preview": finished["answer"][:1500],
                            "cited_node_ids": finished["cited_node_ids"],
                        },
                    )

        except Exception:
            pass

    # Last fallback: return the last model message as answer.
    if not finished["answer"] and isinstance(state, dict):
        fallback_text = _last_message_text(state).strip()

        debug_print(
            "lead.finish.fallback_last_message",
            {
                "fallback_len": len(fallback_text),
                "fallback_preview": fallback_text[:1500],
            },
        )

        finished["answer"] = _sanitize_string_for_llm(fallback_text)

    cited = _safe_dedupe(
        [
            *finished.get("cited_node_ids", []),
            *evidence,
        ]
    )

    answer = AgentAnswer(
        question=question,
        answer=_sanitize_string_for_llm(finished["answer"]),
        cited_node_ids=cited,
        steps=_count_steps(state) if isinstance(state, dict) else 1,
    )

    debug_print(
        "lead.return",
        {
            "answer_len": len(answer.answer),
            "answer_preview": answer.answer[:1500],
            "cited_node_ids": answer.cited_node_ids,
            "steps": answer.steps,
        },
    )

    return answer


# =============================================================================
# Subagent
# =============================================================================


def run_subagent(
    session: Any,
    run: Subrun,
    question: str,
    user_prompt: str,
    emit: Callable[[dict[str, Any]], None],
    stop_event: Event | None = None,
) -> dict[str, Any]:
    """
    LangGraph subagent.

    Also uses the same pre_model_hook through _compile_agent, and all tool
    outputs are sanitized before returning to LangGraph.
    """
    question = _sanitize_string_for_llm(str(question or "")).strip()
    user_prompt = _sanitize_string_for_llm(str(user_prompt or "")).strip()

    finished: dict[str, Any] = {}

    def search_tool(text: str) -> str:
        _check_stop(stop_event)

        query = _sanitize_string_for_llm(str(text or "")).strip()

        emit(
            {
                "type": "search",
                "phase": "sub",
                "agent": run.index,
                "query": query,
            }
        )

        nodes = session.search(query, limit=session.settings.rerank_top_k)
        run.visited.extend(n.id for n in nodes)

        if nodes:
            run.empty_streak = 0

            result = "\n".join(
                f"- node_id: `{n.id}`\n"
                f"  title: {_sanitize_string_for_llm(str(n.title))}\n"
                f"  summary: {_sanitize_string_for_llm(str(n.summary))}\n"
                f"  next_action: read this node with read(node_id='{n.id}') if relevant"
                for n in nodes
            )

            return _sanitize_tool_output(result)

        run.empty_streak += 1

        if run.empty_streak >= session.settings.agent_patience:
            return (
                f"no nodes found ({run.empty_streak} consecutive empty searches). Stop "
                "searching now: call finish with the best answer supported by nodes you read."
            )

        return "no nodes found"

    def read_tool(node_id: str) -> str:
        _check_stop(stop_event)

        requested_id = _sanitize_string_for_llm(str(node_id or ""))
        cleaned_id = clean_node_ref(requested_id)

        node = session.read_node(cleaned_id)

        if node:
            if node.id in run.read_ids:
                return _sanitize_tool_output(
                    f"already read {node.id} ({node.title}). Pick a DIFFERENT node, follow a link, or finish."
                )

            if len(run.read_ids) >= session.settings.subagent_max_reads:
                return _sanitize_tool_output(
                    f"read budget reached ({len(run.read_ids)}/{session.settings.subagent_max_reads} "
                    "nodes). Call finish now with what you have gathered."
                )

            run.empty_streak = 0
            run.read_ids.add(node.id)
            run.visited.append(node.id)

            emit(
                {
                    "type": "read",
                    "agent": run.index,
                    "node": node_ref(node),
                }
            )

        result = format_node_full(node, requested_id, cleaned_id)
        return _sanitize_tool_output(result)

    def follow_link_tool(node_id: str, direction: str = "both") -> str:
        _check_stop(stop_event)

        cleaned_id = clean_node_ref(
            _sanitize_string_for_llm(str(node_id or ""))
        )
        safe_direction = _sanitize_string_for_llm(str(direction or "both"))

        pairs = session.follow_link(cleaned_id, direction=safe_direction)

        if pairs:
            run.empty_streak = 0

        run.visited.extend(n.id for _edge, n in pairs)

        anchor = session.read_node(cleaned_id)

        emit(
            {
                "type": "follow_link",
                "agent": run.index,
                "node": node_ref(anchor) if anchor else {"id": cleaned_id, "title": cleaned_id},
                "neighbors": len(pairs),
            }
        )

        if not pairs:
            return "no neighbors"

        result = "\n".join(
            f"- [{_sanitize_string_for_llm(str(e.label))}] "
            f"{n.id} | "
            f"{_sanitize_string_for_llm(str(n.title))} | "
            f"{_sanitize_string_for_llm(str(n.summary))}"
            for e, n in pairs
        )

        return _sanitize_tool_output(result)

    def finish_tool(
        answer: str,
        cited_node_ids: list[str] | None = None,
    ) -> str:
        _check_stop(stop_event)

        if len(run.read_ids) < session.settings.subagent_min_reads:
            return (
                f"You have read only {len(run.read_ids)} node(s); read at least "
                f"{session.settings.subagent_min_reads} before finishing. Read another now."
            )

        finished["answer"] = _sanitize_string_for_llm(str(answer or "")).strip()
        finished["cited_node_ids"] = [
            clean_node_ref(_sanitize_string_for_llm(str(nid)))
            for nid in (cited_node_ids or [])
            if str(nid or "").strip()
        ]

        return "finished; do not call more tools"

    tools = [
        StructuredTool.from_function(
            search_tool,
            name="search",
            description=SearchArgs.__doc__ or "",
            args_schema=SearchArgs,
        ),
        StructuredTool.from_function(
            read_tool,
            name="read",
            description=ReadArgs.__doc__ or "",
            args_schema=ReadArgs,
        ),
        StructuredTool.from_function(
            follow_link_tool,
            name="follow_link",
            description=FollowLinkArgs.__doc__ or "",
            args_schema=FollowLinkArgs,
        ),
        StructuredTool.from_function(
            finish_tool,
            name="finish",
            description=FinishArgs.__doc__ or "",
            args_schema=FinishArgs,
        ),
    ]

    agent = _compile_agent(
        session.settings,
        tools,
        _sanitize_string_for_llm(SUBAGENT_SYSTEM_PROMPT),
        stop_event,
    )

    debug_print(
        "subagent.invoke.start",
        {
            "agent": run.index,
            "start_id": run.start_id,
            "recursion_limit": max(8, session.settings.subagent_max_steps * 2 + 6),
            "user_prompt_preview": user_prompt[:3000],
        },
    )

    state = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": user_prompt,
                }
            ]
        },
        config={
            "recursion_limit": max(
                8,
                session.settings.subagent_max_steps * 2 + 6,
            ),
            # Optional but useful with SQLite/tool thread issues.
            "max_concurrency": 1,
        },
    )

    answer = (
        _sanitize_string_for_llm(str(finished.get("answer") or "")).strip()
        or _last_message_text(state)
    )

    cited = [
        clean_node_ref(_sanitize_string_for_llm(str(nid)))
        for nid in finished.get("cited_node_ids", [])
        if str(nid or "").strip()
    ] or dedupe(run.visited)

    emit(
        {
            "type": "subagent_done",
            "agent": run.index,
            "cited": cited,
        }
    )

    result = {
        "start": run.start_id,
        "answer": answer or "(no findings)",
        "cited": cited,
    }

    debug_print(
        "subagent.return",
        {
            "agent": run.index,
            "start": run.start_id,
            "answer_preview": result["answer"][:1500],
            "cited": cited,
        },
    )

    return result
