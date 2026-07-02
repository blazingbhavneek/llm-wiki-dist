from __future__ import annotations

import json
import logging
import warnings
from threading import Event
from typing import Any, Callable

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from .models import (
    AgentAnswer,
    Settings,
    Subrun,
    explore as ExploreArgs,
    finish as FinishArgs,
    follow_link as FollowLinkArgs,
    read as ReadArgs,
    search as SearchArgs,
)
from .utils import (
    MAIN_AGENT_SYSTEM_PROMPT,
    MERMAID_INSTRUCTION,
    SUBAGENT_SYSTEM_PROMPT,
    clean_node_ref,
    dedupe,
    format_lead_candidate,
    format_node_full,
    node_ref,
)

log = logging.getLogger("graph_langgraph_agent")


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


def _compile_agent(settings: Settings, tools: list[StructuredTool], prompt: str, stop_event: Event | None):
    def before_model(state: dict[str, Any]) -> dict[str, Any]:
        _check_stop(stop_event)
        return {"llm_input_messages": state["messages"]}

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return create_react_agent(
            _model(settings),
            tools=tools,
            prompt=prompt,
            pre_model_hook=before_model,
            version="v2",
        )


def _last_message_text(state: dict[str, Any]) -> str:
    for message in reversed(state.get("messages", [])):
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            text = "\n".join(parts).strip()
            if text:
                return text
    return ""


def _steps(state: dict[str, Any]) -> int:
    return sum(1 for message in state.get("messages", []) if isinstance(message, AIMessage))


def run_lead_agent(
    session: Any,
    question: str,
    emit: Callable[[dict[str, Any]], None],
    stop_event: Event | None = None,
) -> AgentAnswer:
    evidence: list[str] = []
    finished: dict[str, Any] = {}

    def search_tool(text: str) -> str:
        _check_stop(stop_event)
        query = str(text or "")
        emit({"type": "search", "phase": "main", "query": query})
        try:
            results = session.search_with_evidence(
                query, limit=session.settings.rerank_top_k
            )
        except Exception as exc:
            log.info("lead evidence search failed; node-only: %s", exc)
            results = [
                {"node": n, "why": [], "evidence": []}
                for n in session.search(query, limit=session.settings.rerank_top_k)
            ]
        candidate_nodes = [r["node"] for r in results]
        emit(
            {
                "type": "candidates",
                "count": len(candidate_nodes),
                "nodes": [node_ref(n) for n in candidate_nodes],
            }
        )
        if not candidate_nodes:
            return "no nodes found"
        return "\n".join(format_lead_candidate(r) for r in results)

    def explore_tool(node_ids: list[str]) -> str:
        _check_stop(stop_event)
        return session._run_subagents(
            node_ids, question, evidence, emit, stop_event=stop_event
        )

    def finish_tool(answer: str, cited_node_ids: list[str] | None = None) -> str:
        _check_stop(stop_event)
        finished["answer"] = str(answer or "").strip()
        finished["cited_node_ids"] = [nid for nid in (cited_node_ids or []) if nid]
        return json.dumps(finished, ensure_ascii=False)

    tools = [
        StructuredTool.from_function(
            search_tool,
            name="search",
            description=SearchArgs.__doc__ or "",
            args_schema=SearchArgs,
        ),
        StructuredTool.from_function(
            explore_tool,
            name="explore",
            description=ExploreArgs.__doc__ or "",
            args_schema=ExploreArgs,
        ),
        StructuredTool.from_function(
            finish_tool,
            name="finish",
            description=FinishArgs.__doc__ or "",
            args_schema=FinishArgs,
            return_direct=True,
        ),
    ]

    system_prompt = MAIN_AGENT_SYSTEM_PROMPT
    if session.settings.enable_mermaid:
        system_prompt += MERMAID_INSTRUCTION

    agent = _compile_agent(session.settings, tools, system_prompt, stop_event)
    state = agent.invoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"recursion_limit": max(8, session.settings.agent_max_steps * 2 + 4)},
    )
    emit({"type": "compiling"})

    answer_text = str(finished.get("answer") or "").strip() or _last_message_text(state)
    cited = [nid for nid in finished.get("cited_node_ids", []) if nid]
    return AgentAnswer(
        question=question,
        answer=answer_text,
        cited_node_ids=cited or dedupe(evidence),
        steps=_steps(state),
    )


def run_subagent(
    session: Any,
    run: Subrun,
    question: str,
    user_prompt: str,
    emit: Callable[[dict[str, Any]], None],
    stop_event: Event | None = None,
) -> dict[str, Any]:
    finished: dict[str, Any] = {}

    def search_tool(text: str) -> str:
        _check_stop(stop_event)
        query = str(text or "")
        emit({"type": "search", "phase": "sub", "agent": run.index, "query": query})
        nodes = session.search(query, limit=session.settings.rerank_top_k)
        run.visited.extend(n.id for n in nodes)
        if nodes:
            run.empty_streak = 0
            return "\n".join(
                f"- node_id: `{n.id}`\n  title: {n.title}\n  summary: {n.summary}\n"
                f"  next_action: read this node with read(node_id='{n.id}') if relevant"
                for n in nodes
            )
        run.empty_streak += 1
        if run.empty_streak >= session.settings.agent_patience:
            return (
                f"no nodes found ({run.empty_streak} consecutive empty searches). Stop "
                "searching now: call finish with the best answer supported by nodes you read."
            )
        return "no nodes found"

    def read_tool(node_id: str) -> str:
        _check_stop(stop_event)
        requested_id = str(node_id or "")
        cleaned_id = clean_node_ref(requested_id)
        node = session.read_node(cleaned_id)
        if node:
            if node.id in run.read_ids:
                return f"already read {node.id} ({node.title}). Pick a DIFFERENT node, follow a link, or finish."
            if len(run.read_ids) >= session.settings.subagent_max_reads:
                return (
                    f"read budget reached ({len(run.read_ids)}/{session.settings.subagent_max_reads} "
                    "nodes). Call finish now with what you have gathered."
                )
            run.empty_streak = 0
            run.read_ids.add(node.id)
            run.visited.append(node.id)
            emit({"type": "read", "agent": run.index, "node": node_ref(node)})
        return format_node_full(node, requested_id, cleaned_id)

    def follow_link_tool(node_id: str, direction: str = "both") -> str:
        _check_stop(stop_event)
        pairs = session.follow_link(node_id, direction=str(direction or "both"))
        if pairs:
            run.empty_streak = 0
        run.visited.extend(n.id for _edge, n in pairs)
        anchor = session.read_node(node_id)
        emit(
            {
                "type": "follow_link",
                "agent": run.index,
                "node": node_ref(anchor) if anchor else {"id": node_id, "title": node_id},
                "neighbors": len(pairs),
            }
        )
        if not pairs:
            return "no neighbors"
        return "\n".join(f"- [{e.label}] {n.id} | {n.title} | {n.summary}" for e, n in pairs)

    def finish_tool(answer: str, cited_node_ids: list[str] | None = None) -> str:
        _check_stop(stop_event)
        if len(run.read_ids) < session.settings.subagent_min_reads:
            return (
                f"You have read only {len(run.read_ids)} node(s); read at least "
                f"{session.settings.subagent_min_reads} before finishing. Read another now."
            )
        finished["answer"] = str(answer or "").strip()
        finished["cited_node_ids"] = [nid for nid in (cited_node_ids or []) if nid]
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

    agent = _compile_agent(session.settings, tools, SUBAGENT_SYSTEM_PROMPT, stop_event)
    state = agent.invoke(
        {"messages": [{"role": "user", "content": user_prompt}]},
        config={
            "recursion_limit": max(8, session.settings.subagent_max_steps * 2 + 6)
        },
    )

    answer = str(finished.get("answer") or "").strip() or _last_message_text(state)
    cited = [nid for nid in finished.get("cited_node_ids", []) if nid] or dedupe(
        run.visited
    )
    emit({"type": "subagent_done", "agent": run.index, "cited": cited})

    return {
        "start": run.start_id,
        "answer": answer or "(no findings)",
        "cited": cited,
    }
