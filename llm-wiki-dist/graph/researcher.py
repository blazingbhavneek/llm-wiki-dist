# region Imports

from __future__ import annotations

import asyncio
import json
import logging
import threading
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from threading import Event
from typing import TYPE_CHECKING, Any, Callable

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from typing import Any, Callable

from .core import (
    GRAPH_SYSTEM_PROMPT,
    ROUTER_PROMPT,
    SHALLOW_ANSWER_PROMPT,
    SUBAGENT_SYSTEM_PROMPT,
    MAIN_AGENT_SYSTEM_PROMPT,
    AgentAnswer,
    Edge,
    EvidenceHit,
    GraphStats,
    Node,
    NodeStatus,
    NodeType,
    QueryResult,
    RouteDecision,
    Settings,
    Subrun,
    clean_node_ref,
    dedupe,
    evidence_why,
)
from .core import finish as FinishArgs
from .core import follow_link as FollowLinkArgs
from .core import (
    format_lead_candidate,
    format_node_full,
    item_vec_weight,
    mmr_order,
    node_ref,
    node_snippet,
    normalize_scores,
)
from .core import read as ReadArgs
from .core import repair_answer_mermaid
from .core import sanitize_messages as _sanitize_messages_for_llm
from .core import sanitize_text as _sanitize_string_for_llm
from .core import sanitize_tool_output as _sanitize_tool_output
from .core import search as SearchArgs
from .gateway import LlmClient as OpenAiLlmClient

if TYPE_CHECKING:
    from .gateway import ModelGateway
    from .store import GraphStore

# endregion Imports

# region Global vars/Helpers

log = logging.getLogger("graph_researcher")


class AgentStopped(Exception):
    """Raised when a client cancels an in-flight LangGraph run."""


# region stop checkers, Agent compilers, Sanitizer, message processors


# stop event checker, if user says stop, then it would raise error and stop langgraph execution
def _check_stop(stop_event: Event | None) -> None:
    if stop_event is not None and stop_event.is_set():
        raise AgentStopped("agent run cancelled")


# strip base url of /chat/completions if user accidently enters it
def _base_url(value: str) -> str:
    base = (value or "").rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base


# makes a simple llm client given setting object
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
    """Compile a LangGraph ReAct agent with message sanitization before each LLM call."""

    # Clean the prompt once before giving it to the agent.
    safe_prompt = _sanitize_string_for_llm(prompt or "")

    def before_model(state: dict[str, Any]) -> dict[str, Any]:
        # Stop the graph if the user/client cancelled the run.
        _check_stop(stop_event)

        # LangGraph stores conversation history in state["messages"].
        messages = state.get("messages", [])
        if not isinstance(messages, list):
            messages = []

        # "llm_input_messages" is what the model sees for this call.
        # This keeps the graph state intact, but sends sanitized messages to the LLM.
        return {"llm_input_messages": _sanitize_messages_for_llm(messages)}

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        return create_react_agent(
            # ChatOpenAI model configured from app settings.
            _model(settings),
            tools=tools,
            prompt=safe_prompt,
            # Runs before every model call.
            pre_model_hook=before_model,
            # Use LangGraph's newer ReAct agent version.
            version="v2",
        )


def _last_message_text(state: dict[str, Any]) -> str:
    # Walk backward to find the most recent non-empty message text.
    for message in reversed(state.get("messages", [])):
        content = getattr(message, "content", "")

        if isinstance(content, str):
            text = _sanitize_string_for_llm(content).strip()
            if text:
                return text

        if isinstance(content, list):
            # Extract text from mixed content parts.
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(_sanitize_string_for_llm(item))
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(_sanitize_string_for_llm(item["text"]))

            text = "\n".join(parts).strip()
            if text:
                return text

    # No usable message text found.
    return ""


def _count_steps(state: Any) -> int:
    # Count messages as a simple proxy for how many conversation steps happened.
    messages = state.get("messages", []) if isinstance(state, dict) else []

    # Always return at least 1 so downstream logic never gets a zero step count.
    return max(1, len(messages))


def _clean_ids(ids: list[Any] | None) -> list[str]:
    """Sanitize and normalize a model-supplied list of node IDs."""

    return [
        clean_node_ref(_sanitize_string_for_llm(str(nid)))
        for nid in (ids or [])
        # Ignore empty, None, or whitespace-only IDs.
        if str(nid or "").strip()
    ]


def _safe_dedupe(ids: list[str]) -> list[str]:
    # Prefer the shared dedupe helper when it works.
    try:
        return dedupe(ids)

    # Fall back to a simple stable dedupe if the helper fails for any reason.
    except Exception:
        seen: set[str] = set()
        out: list[str] = []

        for item in ids:
            # Keep the first occurrence and skip empty or repeated IDs.
            if item and item not in seen:
                seen.add(item)
                out.append(item)

        return out


def _format_search_results(results: list[dict[str, Any]]) -> str:
    # Return a clear message when search found nothing.
    if not results:
        return "no nodes found"

    blocks: list[str] = []

    for r in results:
        node = r["node"]
        ev_lines: list[str] = []

        # Include only the first few evidence snippets to keep output compact.
        for ev in r.get("evidence", [])[:4]:
            text = _sanitize_string_for_llm(" ".join((ev.get("text") or "").split()))

            if text:
                # Trim long evidence snippets so tool output stays manageable.
                ev_lines.append(f"    - {text[:500]}")

        evidence_text = (
            "\n".join(ev_lines) if ev_lines else "    - no evidence snippets"
        )

        # Format each node in a model-readable structure with a suggested action.
        blocks.append(
            f"- node_id: `{node.id}`\n"
            f"  title: {_sanitize_string_for_llm(str(node.title))}\n"
            f"  summary: {_sanitize_string_for_llm(str(node.summary))}\n"
            f"  evidence:\n{evidence_text}\n"
            f"  next_action: if relevant, call explore(node_ids=['{node.id}'])"
        )

    # Sanitize the final combined output before returning it to the LLM/tool layer.
    return _sanitize_tool_output("\n".join(blocks))


# endregion stop checkers, Agent compilers, Sanitizer, message processors

# The main Agent, after initial reading of contents present, spawns smaller agents to explore particular leads
# A query may have multiple occurence by name in various documents,
# so rather than forcing model to choose one, spawn smaller agent to read up on differnt paths
# For this we need to create leads first, rather than instructing agent to do it on its own and not get lost, for the first stage we will have it generate
# leads, all the different possible paths where our answer could be
# region Lead Explorer


# args for what we are searching the leads for
class LeadSearchArgs(BaseModel):
    """Search the knowledge graph for relevant nodes."""

    text: str = Field(..., description="Search query text")


# starting point for the subagents, which node to start looking as seed
class LeadExploreArgs(BaseModel):
    """Dispatch subagents to deeply explore specific node IDs."""

    node_ids: list[str] = Field(
        ...,
        description="Exact node IDs to explore. Use node IDs returned by search.",
    )


# Once a lead exploration is finished, report final answer
class LeadFinishArgs(BaseModel):
    """Finish with the final answer and cited node IDs."""

    answer: str = Field(..., description="Final answer to the user")
    cited_node_ids: list[str] | None = Field(
        default=None,
        description="Node IDs supporting the final answer",
    )


@dataclass
class LeadContext:
    """Shared state for one lead-agent run: the session doing retrieval, the
    sanitized question, event emission, cancellation, and run accumulators."""

    session: Any
    question: str
    emit: Callable[[dict[str, Any]], None]
    stop_event: Event | None = None
    evidence: list[str] = field(default_factory=list)
    finished: dict[str, Any] = field(
        default_factory=lambda: {"answer": "", "cited_node_ids": []}
    )


# Tool for the Main agent to search with evidence
# Its supposed to be atomic? (the word i mean is independant of global variable/context)
# in my earlier design the tools given to llm used globally defined variables etc, so for sake of langchain, i need a tool maker function which can
# be independant of the global vars, so, this is the atomic version of it which takes all the context too, this will be wrapped by a tool maker which will
# give it the set context, and then it would only need the query, so it would work with llm
def _lead_search(ctx: LeadContext, text: str) -> str:
    _check_stop(ctx.stop_event)
    session = ctx.session
    query = _sanitize_string_for_llm(str(text or "")).strip()
    ctx.emit({"type": "search", "phase": "main", "query": query})

    try:
        # do an initial evidence based search for query
        results = session.search_with_evidence(
            query, limit=session.settings.rerank_top_k
        )
    except Exception as exc:
        log.info("lead evidence search failed; node-only query=%s: %s", query, exc)
        nodes = session.search(query, limit=session.settings.rerank_top_k)
        results = [{"node": n, "why": [], "evidence": []} for n in nodes]

    # Safety net: if the model generated a brittle narrow query and got 0
    # results, retry with the original full user question.
    if not results and query and query != ctx.question.strip():
        ctx.emit({"type": "search", "phase": "main", "query": ctx.question})
        try:
            results = session.search_with_evidence(
                ctx.question, limit=session.settings.rerank_top_k
            )
        except Exception as exc:
            log.info("lead full-question retry failed: %s", exc)

    log.debug("lead.search query=%s results=%s", query, [r["node"].id for r in results])
    return _format_search_results(results)


# same as above, atomic version of the tool which would be wrapped later with pre-determined context
def _lead_explore(ctx: LeadContext, node_ids: list[str]) -> str:
    _check_stop(ctx.stop_event)
    cleaned = _clean_ids(node_ids)
    if not cleaned:
        return "no valid node_ids supplied. Search first, then call explore with exact node IDs."

    result = ctx.session._run_subagents(
        cleaned, ctx.question, ctx.evidence, ctx.emit, stop_event=ctx.stop_event
    )
    log.debug("lead.explore node_ids=%s evidence=%s", cleaned, ctx.evidence)
    return _sanitize_tool_output(result)


# same as above, atomic version of the tool which would be wrapped later with pre-determined context
def _lead_finish(
    ctx: LeadContext, answer: str, cited_node_ids: list[str] | None = None
) -> str:
    _check_stop(ctx.stop_event)
    ctx.finished["answer"] = _sanitize_string_for_llm(str(answer or "")).strip()
    ctx.finished["cited_node_ids"] = _clean_ids(cited_node_ids)
    log.debug(
        "lead.finish answer_len=%s cited=%s",
        len(ctx.finished["answer"]),
        ctx.finished["cited_node_ids"],
    )
    return _sanitize_tool_output(json.dumps(ctx.finished, ensure_ascii=False))


# this is the wrapper that wraps the above tools with pre-determined context so they can be called by langgraph with just queries
def _lead_tools(ctx: LeadContext) -> list[StructuredTool]:
    def search_tool(text: str) -> str:
        return _lead_search(ctx, text)

    def explore_tool(node_ids: list[str]) -> str:
        return _lead_explore(ctx, node_ids)

    def finish_tool(answer: str, cited_node_ids: list[str] | None = None) -> str:
        return _lead_finish(ctx, answer, cited_node_ids)

    return [
        StructuredTool.from_function(
            search_tool,
            name="search",
            description=LeadSearchArgs.__doc__ or "Search for relevant nodes.",
            args_schema=LeadSearchArgs,
        ),
        StructuredTool.from_function(
            explore_tool,
            name="explore",
            description=LeadExploreArgs.__doc__
            or "Explore selected node IDs using subagents.",
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


# this is the user prompt for when we start the "deep" mode of main agent
# an agent can have two modes, a shallow mode (answer directly exists) in a node (either exo or endo) in that case it quickly returns an answer
# if not, then it will start a deep agent, which will do actual exploration, so for that we would give it initial seed nodes to start looking into
# this is the prompt for that
def _seeded_user_content(
    question: str, seed_context: str, seed_node_ids: list[str]
) -> str:
    if not seed_context.strip():
        return question

    return (
        f"{question}\n\n"
        "重要: この質問に対して、初期検索ですでに候補ノードが見つかっています。\n"
        "これらのノードは有用な出発点として扱ってください。ただし、これらだけで十分だとは決めつけないでください。\n\n"
        "利用可能な進め方:\n"
        "- search(text=...) は、グラフ内から追加の関連ノードを探すために使います。\n"
        "- explore(node_ids=[...]) は、選択したノードIDについてサブエージェントによる詳しい調査を開始するために使います。\n"
        "- finish(answer=..., cited_node_ids=[...]) は、十分な調査と根拠確認が終わった後に最終回答を出すために使います。\n\n"
        "初期候補ノードを注意深く確認してください:\n"
        "- それらが関連性があり、質問全体に答えるために十分だと思われる場合は、"
        "下記の正確なノードIDを使って explore(node_ids=[...]) を呼び出してください。\n"
        "- それらが関連性はあるが不十分だと思われる場合は、explore を呼び出してサブエージェントを開始する前に、"
        "まず search(text=...) を使って追加の関連ノードを探してください。"
        "その後、関連する初期候補ノードIDと、新しく見つかった関連ノードIDの両方を使って "
        "explore(node_ids=[...]) を呼び出してください。\n"
        "- 候補ノードが関連していないように見える場合は、無理に使わないでください。"
        "explore を呼び出す前に、search(text=...) でより適切なノードを探してください。\n\n"
        "explore を呼び出す前に、候補ノードがユーザーの質問に完全に答えるために必要な"
        "重要なエンティティ、概念、制約、比較対象、期間、条件、例外、またはサブ質問をすべてカバーしているか確認してください。"
        "重要な情報が不足しているように見える場合は、必ず search(text=...) で追加検索してください。\n\n"
        "search を使う場合は、質問全体をそのまま検索するだけでなく、足りないと思われる観点ごとに検索してください。"
        "たとえば、不足している人物名、組織名、技術名、条件、時期、比較対象、原因、結果などを個別に検索してください。\n\n"
        "初期候補を確認せずに捨てないでください。"
        "初期候補を使う場合は、正確なノードIDを使用してください。"
        "ただし、初期候補だけで不十分な場合は、必ず追加検索してから explore を呼び出してください。\n\n"
        "初期候補ノード:\n"
        f"{seed_context}\n\n"
        f"候補ノードID: {', '.join(seed_node_ids)}\n"
    )


# the "deep" agent runner, it expects that previous "shallow" check is already done and now we need to start exploration from given seed nodes
def run_lead_agent(
    session: Any,
    question: str,
    emit: Callable[[dict[str, Any]], None],
    stop_event: Event | None = None,
    seed_context: str = "",
    seed_node_ids: list[str] | None = None,
) -> AgentAnswer:
    # Normalize user input and seed data before sending anything to the agent.
    question = _sanitize_string_for_llm(str(question or "")).strip()
    seed_context = _sanitize_string_for_llm(str(seed_context or "")).strip()
    seed_node_ids = seed_node_ids or []

    # Shared runtime context used by lead tools like search, explore, and finish.
    ctx = LeadContext(
        session=session, question=question, emit=emit, stop_event=stop_event
    )

    # Compile the lead agent with its tools and system prompt.
    agent = _compile_agent(
        session.settings,
        tools=_lead_tools(ctx),
        prompt=MAIN_AGENT_SYSTEM_PROMPT,
        stop_event=stop_event,
    )

    # Add seed retrieval context to the user message when available.
    user_content = _sanitize_string_for_llm(
        _seeded_user_content(question, seed_context, seed_node_ids)
    )

    log.debug(
        "lead.start seed_node_ids=%s max_steps=%s question=%s",
        seed_node_ids,
        session.settings.agent_max_steps,
        question,
    )

    # Notify callers/UI that the deep lead-agent route has started.
    emit({"type": "route", "mode": "deep", "reason": "lead agent started"})

    try:
        state = agent.invoke(
            {"messages": [{"role": "user", "content": user_content}]},
            config={
                "recursion_limit": max(50, session.settings.agent_max_steps * 2 + 4),
                # Keep tool execution serial to avoid SQLite/threading issues.
                "max_concurrency": 1,
            },
        )
    except Exception as exc:
        log.info("lead.invoke.failed error=%s", exc, exc_info=True)
        raise

    finished = ctx.finished

    # If finish returned JSON directly, parse it as a fallback source of truth.
    if not finished["answer"] and isinstance(state, dict):
        try:
            parsed = json.loads(_last_message_text(state).strip())
            if isinstance(parsed, dict):
                maybe_answer = _sanitize_string_for_llm(
                    str(parsed.get("answer") or "")
                ).strip()

                if maybe_answer:
                    finished["answer"] = maybe_answer
                    finished["cited_node_ids"] = _clean_ids(
                        parsed.get("cited_node_ids") or []
                    )
        except Exception:
            pass

    # Final fallback: use the last visible model message as the answer.
    if not finished["answer"] and isinstance(state, dict):
        finished["answer"] = _sanitize_string_for_llm(_last_message_text(state).strip())

    # Build the normalized final answer object.
    answer = AgentAnswer(
        question=question,
        answer=_sanitize_string_for_llm(finished["answer"]),
        cited_node_ids=_safe_dedupe(
            [*finished.get("cited_node_ids", []), *ctx.evidence]
        ),
        steps=_count_steps(state),
    )

    log.debug(
        "lead.return answer_len=%s cited=%s steps=%s",
        len(answer.answer),
        answer.cited_node_ids,
        answer.steps,
    )

    return answer


# endregion Lead Explorer

# region Subagent


@dataclass
class SubagentContext:
    """Shared state for one subagent run."""

    session: Any
    run: Subrun  #     start_id, index, visited, read_ids, empty_streak
    emit: Callable[[dict[str, Any]], None]
    stop_event: Event | None = None

    # Stores the subagent's final answer once finish() is called.
    finished: dict[str, Any] = field(default_factory=dict)


# same as above, atomic version of the tool which would be wrapped later with pre-determined context
def _sub_search(ctx: SubagentContext, text: str) -> str:
    # Stop early if cancellation was requested.
    _check_stop(ctx.stop_event)

    session, run = ctx.session, ctx.run

    # Clean the model-provided query before using it for graph search.
    query = _sanitize_string_for_llm(str(text or "")).strip()

    # Emit search event for logs/UI/debugging.
    ctx.emit({"type": "search", "phase": "sub", "agent": run.index, "query": query})

    # Search the graph for relevant nodes.
    nodes = session.search(query, limit=session.settings.rerank_top_k)

    # Track visited node IDs so the run has a memory of what it has seen.
    run.visited.extend(n.id for n in nodes)

    if nodes:
        # Reset empty-search counter once useful results are found.
        run.empty_streak = 0

        # Return compact node previews with clear next action guidance.
        return _sanitize_tool_output(
            "\n".join(
                f"- node_id: `{n.id}`\n"
                f"  title: {_sanitize_string_for_llm(str(n.title))}\n"
                f"  summary: {_sanitize_string_for_llm(str(n.summary))}\n"
                f"  next_action: read this node with read(node_id='{n.id}') if relevant"
                for n in nodes
            )
        )

    # Count consecutive failed searches to prevent endless searching.
    run.empty_streak += 1

    if run.empty_streak >= session.settings.agent_patience:
        return (
            f"no nodes found ({run.empty_streak} consecutive empty searches). Stop "
            "searching now: call finish with the best answer supported by nodes you read."
        )

    return "no nodes found"


# same as above, atomic version of the tool which would be wrapped later with pre-determined context
def _sub_read(ctx: SubagentContext, node_id: str) -> str:
    # Stop early if cancellation was requested.
    _check_stop(ctx.stop_event)

    session, run = ctx.session, ctx.run

    # Preserve requested_id for error messages, but use cleaned_id for lookup.
    requested_id = _sanitize_string_for_llm(str(node_id or ""))
    cleaned_id = clean_node_ref(requested_id)

    # Load the full node content from the graph store.
    node = session.read_node(cleaned_id)

    if node:
        # Avoid wasting read budget on the same node twice.
        if node.id in run.read_ids:
            return _sanitize_tool_output(
                f"already read {node.id} ({node.title}). Pick a DIFFERENT node, follow a link, or finish."
            )

        # Enforce subagent read budget.
        if len(run.read_ids) >= session.settings.subagent_max_reads:
            return _sanitize_tool_output(
                f"read budget reached ({len(run.read_ids)}/{session.settings.subagent_max_reads} "
                "nodes). Call finish now with what you have gathered."
            )

        # Successful read resets the empty-search streak.
        run.empty_streak = 0

        # Track this node as read and visited.
        run.read_ids.add(node.id)
        run.visited.append(node.id)

        # Emit read event for logs/UI/debugging.
        ctx.emit({"type": "read", "agent": run.index, "node": node_ref(node)})

    # Format full node details, including helpful info if node was missing.
    return _sanitize_tool_output(format_node_full(node, requested_id, cleaned_id))


# same as above, atomic version of the tool which would be wrapped later with pre-determined context
def _sub_follow_link(
    ctx: SubagentContext, node_id: str, direction: str = "both"
) -> str:
    # Stop early if cancellation was requested.
    _check_stop(ctx.stop_event)

    session, run = ctx.session, ctx.run

    # Clean model-provided node ID and direction before graph traversal.
    cleaned_id = clean_node_ref(_sanitize_string_for_llm(str(node_id or "")))
    safe_direction = _sanitize_string_for_llm(str(direction or "both"))

    # Fetch neighboring nodes connected by graph edges.
    pairs = session.follow_link(cleaned_id, direction=safe_direction)

    if pairs:
        # Finding neighbors means the agent made progress.
        run.empty_streak = 0

    # Track all neighbor nodes as visited.
    run.visited.extend(n.id for _edge, n in pairs)

    # Read anchor node only for nicer event metadata.
    anchor = session.read_node(cleaned_id)

    # Emit traversal event for logs/UI/debugging.
    ctx.emit(
        {
            "type": "follow_link",
            "agent": run.index,
            "node": (
                node_ref(anchor) if anchor else {"id": cleaned_id, "title": cleaned_id}
            ),
            "neighbors": len(pairs),
        }
    )

    if not pairs:
        return "no neighbors"

    # Return compact neighbor list with edge label, node ID, title, and summary.
    return _sanitize_tool_output(
        "\n".join(
            f"- [{_sanitize_string_for_llm(str(e.label))}] "
            f"{n.id} | "
            f"{_sanitize_string_for_llm(str(n.title))} | "
            f"{_sanitize_string_for_llm(str(n.summary))}"
            for e, n in pairs
        )
    )


# same as above, atomic version of the tool which would be wrapped later with pre-determined context
def _sub_finish(
    ctx: SubagentContext, answer: str, cited_node_ids: list[str] | None = None
) -> str:
    # Stop early if cancellation was requested.
    _check_stop(ctx.stop_event)

    run = ctx.run
    min_reads = ctx.session.settings.subagent_min_reads

    # Require enough node reads before allowing the subagent to finish.
    if len(run.read_ids) < min_reads:
        return (
            f"You have read only {len(run.read_ids)} node(s); read at least "
            f"{min_reads} before finishing. Read another now."
        )

    # Store final answer and cleaned citations in shared context.
    ctx.finished["answer"] = _sanitize_string_for_llm(str(answer or "")).strip()
    ctx.finished["cited_node_ids"] = _clean_ids(cited_node_ids)

    return "finished; do not call more tools"


# this is the wrapper that wraps the above tools with pre-determined context so they can be called by langgraph with just queries
def _sub_tools(ctx: SubagentContext) -> list[StructuredTool]:
    # Bind ctx into each tool so LangGraph only has to pass tool arguments.
    def search_tool(text: str) -> str:
        return _sub_search(ctx, text)

    def read_tool(node_id: str) -> str:
        return _sub_read(ctx, node_id)

    def follow_link_tool(node_id: str, direction: str = "both") -> str:
        return _sub_follow_link(ctx, node_id, direction)

    def finish_tool(answer: str, cited_node_ids: list[str] | None = None) -> str:
        return _sub_finish(ctx, answer, cited_node_ids)

    # Convert plain Python functions into LangChain StructuredTool objects.
    return [
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


def run_subagent(
    session: Any,
    run: Subrun,
    question: str,
    user_prompt: str,
    emit: Callable[[dict[str, Any]], None],
    stop_event: Event | None = None,
) -> dict[str, Any]:

    # Normalize inputs before passing them into the agent.
    question = _sanitize_string_for_llm(str(question or "")).strip()
    user_prompt = _sanitize_string_for_llm(str(user_prompt or "")).strip()

    # Shared runtime context used by subagent tools.
    ctx = SubagentContext(session=session, run=run, emit=emit, stop_event=stop_event)

    # Compile the subagent with its tools and system prompt.
    agent = _compile_agent(
        session.settings, _sub_tools(ctx), SUBAGENT_SYSTEM_PROMPT, stop_event
    )

    log.debug(
        "subagent.start agent=%s start_id=%s prompt_len=%s",
        run.index,
        run.start_id,
        len(user_prompt),
    )

    # Run the subagent with a bounded recursion limit and serial tool execution.
    state = agent.invoke(
        {"messages": [{"role": "user", "content": user_prompt}]},
        config={
            "recursion_limit": max(30, session.settings.subagent_max_steps * 2 + 6),
            # Optional but useful with SQLite/tool thread issues.
            "max_concurrency": 1,
        },
    )

    # Prefer the explicit finish() answer, otherwise fall back to the last message.
    answer = _sanitize_string_for_llm(
        str(ctx.finished.get("answer") or "")
    ).strip() or _last_message_text(state)

    # Prefer cited IDs from finish(); otherwise cite visited nodes as a fallback.
    cited = _clean_ids(ctx.finished.get("cited_node_ids", [])) or dedupe(run.visited)

    # Notify caller/UI that this subagent completed.
    emit({"type": "subagent_done", "agent": run.index, "cited": cited})

    log.debug(
        "subagent.return agent=%s start=%s answer_len=%s cited=%s",
        run.index,
        run.start_id,
        len(answer or "(no findings)"),
        cited,
    )

    # Return compact result for the lead agent to merge with other subagent results.
    return {
        "start": run.start_id,
        "answer": answer or "(no findings)",
        "cited": cited,
    }


# Compact router-facing view of one search_with_evidence result.
def _describe_candidate(result: dict[str, Any]) -> dict[str, Any]:
    node = result["node"]

    # Return only the fields needed for routing/preview, not the full node.
    return {
        "node_id": node.id,
        "kind": "agent_note" if node.type == NodeType.exogenous else "source",
        "title": node.title,
        "summary": node.summary,
        # Keep evidence snippets short and whitespace-normalized.
        "evidence": [
            " ".join((ev.get("text") or "").split())[:280]
            for ev in result.get("evidence", [])[:3]
        ],
    }


# One research session, seperate context, seperate parameters (set by user or default) for each session, so multiple users can do their own research variations
class ResearchSession:

    def __init__(self, gateway: ModelGateway, store: GraphStore):
        self.gateway = gateway  # GPU Related stuff: Model, Embedder, Reranker
        self.store = store  # DB connection

        # Defaults to the shared clients; apply_overrides may swap in
        # per-request replacements without touching the gateway.
        self.settings = gateway.settings
        self.llm = gateway.llm

        # Early-exit deep route preserves its candidates here so the lead
        # agent does not start blind.
        self._lead_seed_context = ""
        self._lead_seed_node_ids: list[str] = []

    # Per-request overrides: one /api/ask may carry a subset of tunables;
    # anything omitted falls back to the global runtime defaults. Applied to a
    # throwaway session only, so concurrent requests never share state.
    def apply_overrides(self, overrides: dict[str, Any] | None) -> None:
        if not overrides:
            return
        int_keys = {
            "edge_candidate_k",
            "vector_query_k",
            "cascade_max_hops",
            "cascade_max_nodes",
            "agent_max_steps",
            "agent_patience",
            "search_rrf_k",
            "search_candidate_pool",
            "rerank_top_k",
            "subagent_count",
            "subagent_concurrency",
            "subagent_max_steps",
            "subagent_min_reads",
            "subagent_max_reads",
            "early_exit_candidates",
            "shallow_answer_max_nodes",
        }
        float_keys = {"chat_temperature"}
        str_keys = {"chat_base_url", "chat_api_key", "chat_model"}
        bool_keys = {"entity_dedup", "enable_mermaid", "agent_early_exit"}
        clean: dict[str, Any] = {}
        for key, value in overrides.items():
            if value is None:
                continue
            try:
                if key in int_keys:
                    clean[key] = max(0, int(value))
                elif key in float_keys:
                    clean[key] = float(value)
                elif key in bool_keys:
                    clean[key] = bool(value)
                elif key in str_keys:
                    text = str(value).strip()
                    if text:
                        clean[key] = text
            except (TypeError, ValueError):
                continue
        if not clean:
            return
        self.settings = self.settings.model_copy(update=clean)
        if any(k in clean for k in str_keys) or "chat_temperature" in clean:
            self.llm = OpenAiLlmClient(
                model=self.settings.chat_model,
                base_url=self.settings.chat_base_url,
                api_key=self.settings.chat_api_key,
                system_prompt=GRAPH_SYSTEM_PROMPT,
                temperature=self.settings.chat_temperature,
            )

    # CRUD (Except only the R part lmao)

    # Get all nodes and edges for viewing the graph
    def get(self) -> tuple[list[Node], list[Edge]]:
        nodes = self.store.get_all_nodes()
        edges = self.store.get_all_edges()
        return nodes, edges

    # Read a particular node to get its content
    def read_node(self, node_id: str) -> Node | None:
        node = self.store.get_node(node_id)
        if node:
            return node
        if not node_id.startswith("node:"):
            node = self.store.get_node(f"node:{node_id}")
            if node:
                return node
        # Fuzzy fallback
        matches = self.store.keyword_search(node_id, 5)
        return matches[0] if matches else None

    # If asked to get a list of outgoing or incoming edges and their nodes
    def follow_link(
        self,
        node_id: str,
        label: str | None = None,
        direction: str = "both",
        limit: int | None = None,
    ) -> list[tuple[Edge, Node]]:
        normalized = direction.lower().strip()
        if normalized not in {"incoming", "outgoing", "both"}:
            raise ValueError("direction must be 'incoming', 'outgoing', or 'both'")

        pairs: list[tuple[Edge, Node]] = []
        if normalized in {"outgoing", "both"}:
            for edge in self.store.get_outgoing_edges(node_id, label):
                if edge.invalid_at or edge.expired_at:
                    continue
                target = self.store.get_node(edge.target_node_id)
                if target and target.status == NodeStatus.active:
                    pairs.append((edge, target))
        if normalized in {"incoming", "both"}:
            for edge in self.store.get_incoming_edges(node_id, label):
                if edge.invalid_at or edge.expired_at:
                    continue
                source = self.store.get_node(edge.source_node_id)
                if source and source.status == NodeStatus.active:
                    pairs.append((edge, source))
        return pairs[:limit] if limit is not None else pairs

    # Calculate graph stats for health, not too dense, not too sparse ideally
    def health(self, node_id: str | None = None) -> GraphStats:
        nodes = self.store.get_all_nodes()
        edges = self.store.get_all_edges()
        if node_id:
            nodes = [n for n in nodes if n.id == node_id]
            edges = [
                e for e in edges if node_id in (e.source_node_id, e.target_node_id)
            ]

        node_ids = {n.id for n in nodes}
        neighbors: dict[str, set[str]] = {nid: set() for nid in node_ids}
        for edge in edges:
            if edge.source_node_id in neighbors and edge.target_node_id in node_ids:
                neighbors[edge.source_node_id].add(edge.target_node_id)
            if edge.target_node_id in neighbors and edge.source_node_id in node_ids:
                neighbors[edge.target_node_id].add(edge.source_node_id)

        node_count = len(nodes)
        total_degree = sum(len(v) for v in neighbors.values())
        avg_degree = (total_degree / node_count) if node_count else 0.0
        max_edges = node_count * (node_count - 1) / 2
        density = ((total_degree / 2) / max_edges) if max_edges else 0.0

        overlap_total, overlap_pairs = 0.0, 0
        for nid, nid_neighbors in neighbors.items():
            for other_id in nid_neighbors:
                if other_id <= nid:
                    continue
                union = nid_neighbors | neighbors.get(other_id, set())
                if union:
                    overlap_total += len(
                        nid_neighbors & neighbors.get(other_id, set())
                    ) / len(union)
                    overlap_pairs += 1
        mean_overlap = (overlap_total / overlap_pairs) if overlap_pairs else 0.0

        clusters: dict[str, int] = {}
        for node in nodes:
            key = node.cluster or "Unclustered"
            clusters[key] = clusters.get(key, 0) + 1

        return GraphStats(
            total_nodes=node_count,
            active_nodes=sum(1 for n in nodes if n.status == NodeStatus.active),
            endogenous_nodes=sum(1 for n in nodes if n.type == NodeType.endogenous),
            exogenous_nodes=sum(1 for n in nodes if n.type == NodeType.exogenous),
            total_edges=len(edges),
            isolated_nodes=sum(1 for nid in node_ids if not neighbors[nid]),
            avg_degree=round(avg_degree, 3),
            density=round(density, 5),
            mean_neighbor_overlap=round(mean_overlap, 4),
            clusters=clusters,
            target_node_id=node_id,
        )

    # Query the graph
    # id: get by node id, and its edges
    # keyword:  search through LLM extracted keywords + title + summary and body of each node
    # vector: Embedding based search for similarity search (searches titles, main body, summaries, claims etc)
    def query(self, query_type: str, value: str) -> QueryResult:
        normalized = query_type.lower().strip()
        if normalized == "id":
            node = self.read_node(value)
            return QueryResult(
                query_type="id",
                value=value,
                nodes=[node] if node else [],
                edges=self.store.get_edges_for_node(value) if node else [],
            )
        if normalized == "keyword":
            nodes = self.store.keyword_search(value, self.settings.vector_query_k)
            edges: dict[str, Edge] = {}
            for node in nodes:
                for edge in self.store.get_edges_for_node(node.id):
                    edges[edge.id] = edge
            return QueryResult(
                query_type="keyword",
                value=value,
                nodes=nodes,
                edges=list(edges.values()),
            )
        if normalized == "vector":
            vector = self.gateway.embedder.embed_query(value)
            hits = self.store.vector_search(
                vector, "vec_body", self.settings.vector_query_k
            )
            seeds = [n for n in (self.store.get_node(nid) for nid, _ in hits) if n]
            nodes, edges_list = self._expand_neighborhood(seeds, hops=2)
            return QueryResult(
                query_type="vector", value=value, nodes=nodes, edges=edges_list
            )
        raise ValueError("query_type must be 'keyword', 'vector', or 'id'")

    # Calls the function that does all 3 Query modes then rank them, if that fails fallbacks to normal keyword search
    def search(self, text: str, limit: int | None = None) -> list[Node]:

        # server default limit or user defined limit
        limit = limit or self.settings.vector_query_k

        try:
            results = self.search_with_evidence(text, limit)
        except Exception as exc:
            log.info("evidence search failed; BM25-only fallback: %s", exc)
            try:
                nodes = self.store.keyword_search(text, limit)
            except Exception as exc2:
                log.info("bm25 fallback failed: %s", exc2)
                return []
            return nodes[:limit]
        return [r["node"] for r in results][:limit]

    # Evidence-first retrieval. Returns ``{node, score, why, evidence}`` dicts ranked by cross-encoder relevance (falling back to weighted RRF).
    # Combines BM25 keyword search, vector search, item search, reranking, deduping, and returns nodes with evidence snippets.
    def search_with_evidence(
        self, text: str, limit: int | None = None
    ) -> list[dict[str, Any]]:

        limit = limit or self.settings.vector_query_k
        s = self.settings

        # Rank normalizer, it flattens the contributions of ranks, as this parameter is increased, the ranked are
        # more flattened and more chance of lower ranks to be included
        rrf_k = s.search_rrf_k

        # It also makes evidences, not just plain ranks so LLM can choose based on context not just quantitative numbers
        hits: list[EvidenceHit] = []

        # node BM25
        node_bm25: list[Node] = []
        try:
            node_bm25 = self.store.keyword_search(text, s.pool_node_bm25)
        except Exception as exc:
            log.info("node bm25 failed: %s", exc)

        # Convert node keyword hits into evidence hits
        for rank, node in enumerate(node_bm25, start=1):
            hits.append(
                EvidenceHit(
                    node.id,
                    "node_bm25",
                    None,
                    node_snippet(node),
                    rank,
                    s.weight_node_bm25,
                )
            )

        # vector pools
        query_vec: list[float] | None = None
        try:
            query_vec = self.gateway.embedder.embed_query(text)
        except Exception as exc:
            log.info("query embed failed; BM25-only: %s", exc)

        if query_vec is not None:
            # Search node-level embeddings like body and summary
            for table, field, weight, pool in (
                ("vec_body", "body_vec", s.weight_body_vec, s.pool_vec_body),
                (
                    "vec_summary",
                    "summary_vec",
                    s.weight_summary_vec,
                    s.pool_vec_summary,
                ),
            ):
                try:
                    vhits = self.store.vector_search(query_vec, table, pool)
                except Exception as exc:
                    log.info("%s search failed: %s", table, exc)
                    continue

                # These hits know the node, text gets filled later
                for rank, (node_id, _dist) in enumerate(vhits, start=1):
                    hits.append(EvidenceHit(node_id, field, None, "", rank, weight))

            # Search smaller indexed items/chunks with vector similarity
            try:
                item_hits = self.store.vector_search(
                    query_vec, "vec_search_item", s.pool_vec_item
                )
            except Exception as exc:
                log.info("vec_search_item search failed: %s", exc)
                item_hits = []

            # Load item rows so we can attach actual text as evidence
            rows = self.store.get_search_items([iid for iid, _ in item_hits])

            for rank, (item_id, _dist) in enumerate(item_hits, start=1):
                row = rows.get(item_id)
                if not row:
                    continue
                hits.append(
                    EvidenceHit(
                        row["node_id"],
                        row["field"],
                        item_id,
                        row["text"] or "",
                        rank,
                        item_vec_weight(s, row["field"]),  # TODO: Make this inline
                        row.get("start_char"),
                    )
                )

        # item BM25 searches smaller indexed chunks/items inside nodes, not the whole node text
        # Different from node BM25, which finds broadly matching nodes using title/summary/body/keywords
        # This is mainly for finding exact evidence snippets that matched the query
        # So node BM25 finds candidate nodes, item BM25 explains why with precise text
        try:
            item_bm25 = self.store.search_items_fts_query(text, s.pool_item_bm25)
        except Exception as exc:
            log.info("item bm25 failed: %s", exc)
            item_bm25 = []

        # Add keyword matches from smaller items/chunks
        for rank, row in enumerate(item_bm25, start=1):
            hits.append(
                EvidenceHit(
                    row["node_id"],
                    row["field"],
                    row["item_id"],
                    row["text"] or "",
                    rank,
                    s.weight_item_bm25,
                    row.get("start_char"),
                )
            )

        # Nothing matched anywhere
        if not hits:
            return []

        # weighted RRF per node + load active nodes
        node_scores: dict[str, float] = defaultdict(float)

        # Same node can get points from multiple search methods
        for hit in hits:
            node_scores[hit.node_id] += hit.contribution(rrf_k)

        nodes: dict[str, Node] = {}

        # Load nodes and ignore inactive ones
        for node_id in node_scores:
            node = self.store.get_node(node_id)
            if node and node.status == NodeStatus.active:
                nodes[node_id] = node

        by_node: dict[str, list[EvidenceHit]] = defaultdict(list)

        # Group evidence under its node
        for hit in hits:
            if hit.node_id not in nodes:
                continue
            if not hit.text.strip():
                hit.text = node_snippet(nodes[hit.node_id])
            by_node[hit.node_id].append(hit)

        # --- per-node evidence selection with caps ----------------------------
        pool: list[EvidenceHit] = []

        # Pick limited good evidence from each node
        for node_id, node_hits in by_node.items():
            pool.extend(self._select_node_evidence(node_hits))

        pool.sort(key=lambda h: h.contribution(rrf_k), reverse=True)

        # Keep only non-empty snippets and cap rerank pool
        pool = [h for h in pool if h.text.strip()][: s.evidence_rerank_pool]

        # --- cross-encoder rerank of snippets (guarded) -----------------------
        rel: list[float] | None = None

        # Reranker scores actual query-snippet relevance if available
        if self.gateway.reranker and pool:
            try:
                ranked = self.gateway.reranker.top_k(
                    text, [(pool[i].text, i) for i in range(len(pool))], len(pool)
                )
                by_idx = {idx: score for idx, score in ranked}
                rel = [float(by_idx.get(i, 0.0)) for i in range(len(pool))]
            except Exception as exc:
                log.info("snippet rerank failed; RRF order: %s", exc)
                rel = None

        # If reranker fails, just use RRF score
        if rel is None:
            rel = [pool[i].contribution(rrf_k) for i in range(len(pool))]

        rel = normalize_scores(rel)

        # MMR dedup over the snippet pool
        order = mmr_order([h.text for h in pool], rel, s.evidence_mmr_lambda)
        rank_of = {idx: pos for pos, idx in enumerate(order)}

        node_pool_idx: dict[str, list[int]] = defaultdict(list)

        # Remember which snippets belong to which node
        for i, hit in enumerate(pool):
            node_pool_idx[hit.node_id].append(i)

        # aggregate evidence back to nodes
        results: list[dict[str, Any]] = []

        for node_id, node in nodes.items():
            snippet_idxs = node_pool_idx.get(node_id, [])

            if snippet_idxs:
                best_rel = max(rel[i] for i in snippet_idxs)

                # Choose best non-duplicate evidence snippets for this node
                chosen = sorted(snippet_idxs, key=lambda i: rank_of[i])[
                    : s.evidence_max_per_node
                ]

                evidence = [
                    {
                        "field": pool[i].field,
                        "text": pool[i].text,
                        "item_id": pool[i].item_id,
                        "rank": pool[i].rank,
                    }
                    for i in chosen
                ]
            else:
                best_rel, evidence = 0.0, []

            results.append(
                {
                    "node": node,
                    "score": best_rel + node_scores[node_id],
                    "why": evidence_why(by_node[node_id]),
                    "evidence": evidence,
                }
            )

        results.sort(key=lambda d: d["score"], reverse=True)
        return results[:limit]

    def _select_node_evidence(self, node_hits: list[EvidenceHit]) -> list[EvidenceHit]:
        """Caps per node: <=max_per_node snippets, <=max_per_field per field,
        <=1 per overlapping small/big-chunk region (start_char proximity)."""
        s = self.settings

        # Sort best evidence first using same RRF contribution logic
        ordered = sorted(
            node_hits, key=lambda h: h.contribution(s.search_rrf_k), reverse=True
        )

        selected: list[EvidenceHit] = []
        per_field: Counter = Counter()
        regions: list[int] = []
        seen: set[str] = set()

        for hit in ordered:
            # Stop once this node already has enough evidence
            if len(selected) >= s.evidence_max_per_node:
                break

            # Skip empty evidence
            if not hit.text.strip():
                continue

            # Dedup same item, or same-looking text if item_id is missing
            key = hit.item_id or hit.text[:80]
            if key in seen:
                continue

            # Avoid taking too many snippets from same field
            if per_field[hit.field] >= s.evidence_max_per_field:
                continue

            # Dedup nearby chunk regions so small/big chunks don't repeat same area
            if hit.field in ("small_chunk", "big_chunk") and hit.start_char is not None:
                if any(
                    abs(hit.start_char - r) < s.evidence_dedup_char_window
                    for r in regions
                ):
                    continue
                regions.append(hit.start_char)

            selected.append(hit)
            per_field[hit.field] += 1
            seen.add(key)

        return selected

    # Query agent, with live streaming events
    def ask(
        self,
        question: str,
        persist: bool = False,
        on_event: (
            Callable[[dict[str, Any]], None] | None
        ) = None,  # callback for event emission
        stop_event: threading.Event | None = None,
    ) -> AgentAnswer:
        if persist:
            raise ValueError("ReadSession.ask() must use persist=False")

        emit = on_event or (lambda event: None)
        emit({"type": "start", "question": question})

        # Early-exit routing: skip deep research when an existing agent note
        # already answers the question, or when shallow RAG over retrieved
        # evidence is enough for a narrow query.
        answer: AgentAnswer | None = None
        if self.settings.agent_early_exit:
            answer = self._try_early_exit(question, emit, stop_event=stop_event)
        if answer is None:
            answer = self._run_lead(question, emit, stop_event=stop_event)

        # if model was asked to generate mermaid, check if it was done right and fix it if any error
        if self.settings.enable_mermaid and answer.answer:
            # repair_answer_mermaid expects the llm as second arg
            answer.answer = repair_answer_mermaid(
                answer.answer, self.llm, self.settings, emit
            )

        emit({"type": "done"})
        return answer

    # Early-exit router. Three outcomes: reuse an existing agent note verbatim,
    # compose a shallow RAG answer from retrieved evidence, or return None to
    # fall through to deep multi-subagent research. Any failure falls through.
    def _try_early_exit(
        self,
        question: str,
        emit: Callable,
        stop_event: threading.Event | None = None,
    ) -> AgentAnswer | None:
        # Clear stale seed context from any previous call on the same session.
        self._lead_seed_context = ""
        self._lead_seed_node_ids = []

        if stop_event is not None and stop_event.is_set():
            raise AgentStopped("agent run cancelled")

        try:
            results = self.search_with_evidence(
                question, limit=self.settings.early_exit_candidates
            )
        except Exception as exc:
            log.info("early-exit retrieval failed; deep path: %s", exc, exc_info=True)
            return None

        if not results:
            emit({"type": "route", "mode": "deep", "reason": "no candidates"})
            return None

        emit(
            {
                "type": "candidates",
                "count": len(results),
                "nodes": [node_ref(r["node"]) for r in results],
            }
        )

        payload = {
            "question": question,
            "candidates": [_describe_candidate(r) for r in results],
        }
        try:
            raw = self.llm.complete_structured(
                ROUTER_PROMPT, json.dumps(payload, ensure_ascii=False), RouteDecision
            )
            decision = (
                raw
                if isinstance(raw, RouteDecision)
                else RouteDecision.model_validate(raw)
            )
        except Exception as exc:
            log.info("early-exit router failed; deep path: %s", exc, exc_info=True)
            return None

        mode = (decision.mode or "deep").strip().lower()
        log.debug(
            "early_exit.route mode=%s reason=%s node_id=%s",
            mode,
            decision.reason,
            decision.node_id,
        )
        emit(
            {
                "type": "route",
                "mode": mode,
                "reason": decision.reason,
                "node_id": decision.node_id,
            }
        )

        if stop_event is not None and stop_event.is_set():
            raise AgentStopped("agent run cancelled")

        if mode == "deep":
            # Preserve the candidates so LangGraph does not start blind.
            self._lead_seed_context = "\n\n".join(
                format_lead_candidate(r) for r in results
            )
            self._lead_seed_node_ids = [r["node"].id for r in results]
            return None

        if mode == "reuse" and decision.node_id:
            answer = self._answer_by_reuse(question, decision.node_id, emit)
            if answer is not None:
                return answer
            # Router pointed at a bad node; the evidence is still usable.
            mode = "shallow"

        if mode == "shallow":
            return self._answer_by_shallow(question, results, emit)

        log.debug("early_exit.unknown_mode mode=%s; deep path", mode)
        return None

    # Reuse an existing agent note verbatim. None when the node is missing,
    # inactive, or empty — caller falls back to the shallow path.
    def _answer_by_reuse(
        self, question: str, node_id: str, emit: Callable
    ) -> AgentAnswer | None:
        node = self.store.get_node(clean_node_ref(node_id))
        if not (node and node.status == NodeStatus.active and node.body.strip()):
            return None

        emit({"type": "read", "agent": 0, "node": node_ref(node)})
        # Cite the note plus the sources it was derived from so the UI
        # highlights the whole provenance path.
        cited = [node.id] + [
            e.target_node_id
            for e in self.store.get_outgoing_edges(node.id, "reference")
        ]
        return AgentAnswer(
            question=question,
            answer=node.body,
            cited_node_ids=dedupe(cited),
            steps=1,
            exogenous_node_id=node.id if node.type == NodeType.exogenous else None,
        )

    # Compose a shallow RAG answer from retrieved evidence. None on LLM
    # failure or empty output — caller falls through to the deep path.
    def _answer_by_shallow(
        self, question: str, results: list[dict[str, Any]], emit: Callable
    ) -> AgentAnswer | None:
        top = results[: self.settings.shallow_answer_max_nodes]
        context = {
            "question": question,
            "notes": [
                {
                    "node_id": r["node"].id,
                    "title": r["node"].title,
                    "summary": r["node"].summary,
                    "evidence": [
                        " ".join((ev.get("text") or "").split())
                        for ev in r.get("evidence", [])
                    ],
                    "body": r["node"].body[:2500],
                }
                for r in top
            ],
        }

        emit({"type": "compiling"})
        try:
            text = self.llm.complete(
                SHALLOW_ANSWER_PROMPT, json.dumps(context, ensure_ascii=False)
            ).strip()
        except Exception as exc:
            log.info("shallow answer failed; deep path: %s", exc, exc_info=True)
            return None
        if not text:
            return None

        return AgentAnswer(
            question=question,
            answer=text,
            cited_node_ids=[r["node"].id for r in top],
            steps=1,
        )

    def _run_lead(
        self,
        question: str,
        emit: Callable,
        stop_event: threading.Event | None = None,
    ) -> AgentAnswer:
        """Run the LangGraph lead agent. GraphStore hands every LangGraph
        worker thread its own SQLite connection, so this session object is
        safe to share with the agent's tool threads."""
        return run_lead_agent(
            self,
            question,
            emit,
            stop_event=stop_event,
            seed_context=self._lead_seed_context,
            seed_node_ids=self._lead_seed_node_ids,
        )

    # Run a subagent to explor multiple topics to avoid missing a path that might have been a better answer
    def _run_subagents(
        self,
        raw_node_ids: list[Any],
        question: str,
        evidence: list[str],
        emit: Callable,
        stop_event: threading.Event | None = None,
    ) -> str:
        starts = self._resolve_distinct_starts(raw_node_ids)
        if not starts:
            return (
                "no valid starting nodes resolved from those ids. Search again and pass "
                "exact node ids from the search results to explore."
            )

        start_refs = []
        for s in starts:
            node = self.read_node(s)
            if node:
                start_refs.append(node_ref(node))

        emit(
            {
                "type": "subagents_spawned",
                "starts": start_refs,
            }
        )

        assignments = [(start, [o for o in starts if o != start]) for start in starts]

        reports: list[dict[str, Any] | None] = [None] * len(assignments)

        max_workers = max(1, int(getattr(self.settings, "subagent_concurrency", 1) or 1))
        max_workers = min(max_workers, len(assignments))

        # emit may touch shared UI/websocket state, so serialize calls from worker threads.
        emit_lock = threading.Lock()

        def safe_emit(event: dict[str, Any]) -> None:
            with emit_lock:
                emit(event)

        def run_one(pos: int, start: str, siblings: list[str]) -> dict[str, Any]:
            if stop_event is not None and stop_event.is_set():
                raise AgentStopped("agent run cancelled")

            return self._run_single_subagent(
                start,
                siblings,
                question,
                pos + 1,
                safe_emit,
                stop_event=stop_event,
            )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(run_one, pos, start, siblings): (pos, start)
                for pos, (start, siblings) in enumerate(assignments)
            }

            for future in as_completed(futures):
                pos, start = futures[future]

                try:
                    reports[pos] = future.result()

                except AgentStopped:
                    for pending in futures:
                        pending.cancel()
                    raise

                except Exception as exc:
                    reports[pos] = {
                        "start": start,
                        "answer": f"(subagent failed: {exc})",
                        "cited": [],
                    }

        final_reports: list[dict[str, Any]] = [
            report
            for report in reports
            if report is not None
        ]

        # Aggregate cited evidence from all reports.
        for report in final_reports:
            evidence.extend(report.get("cited", []))

        blocks = ["Subagent reports (each explored a different region):"]

        for index, report in enumerate(final_reports, start=1):
            cited_str = ", ".join(report.get("cited", [])) or "(none)"
            blocks.append(
                f"\n### Subagent {index} — start node: {report.get('start')}\n"
                f"{report.get('answer', '').strip()}\nEvidence node ids: {cited_str}"
            )

        return "\n".join(blocks)

    # Dedup already seen nodes
    def _resolve_distinct_starts(self, raw_node_ids: list[Any]) -> list[str]:
        resolved: list[str] = []
        seen: set[str] = set()
        for raw in raw_node_ids or []:
            node = self.read_node(clean_node_ref(str(raw)))
            if node and node.id not in seen:
                seen.add(node.id)
                resolved.append(node.id)
            if len(resolved) >= self.settings.subagent_count:
                break
        return resolved

    # Run the subagent with same pattern, with explicit instructions to keep its exploration to its own region and report its finding
    # Explictly told to not read other nodes in case it ends up there somehow
    # TODO: Add exogenous node exploration case too, in case the exo nodes is enough, just verify the important facts from its source and report it as good
    def _run_single_subagent(
        self,
        start_id: str,
        sibling_ids: list[str],
        question: str,
        index: int,
        emit: Callable,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        run = Subrun(start_id=start_id, index=index)

        start_node = self.read_node(start_id)
        if start_node:
            emit(
                {"type": "subagent_start", "agent": index, "node": node_ref(start_node)}
            )

        siblings_str = ", ".join(sibling_ids) if sibling_ids else "(none)"
        user_prompt = (
            f"質問: {question}\n\n"
            f"あなたに割り当てられた開始ノード: {start_id}\n"
            f"他のエージェントが担当している領域（探索しないこと）: {siblings_str}\n\n"
            "まず開始ノードを読み、その後リンクをたどるか、あなたの担当領域内で検索してください。"
            "この領域がその質問について何を述べているかを報告してください。"
        )

        return run_subagent(
            self, run, question, user_prompt, emit, stop_event=stop_event
        )

    # Expands from the seed nodes through connected edges for the given number of hops
    # Collects active neighboring nodes and all edges seen on the way, while avoiding duplicates
    def _expand_neighborhood(
        self, seeds: list[Node], hops: int = 2
    ) -> tuple[list[Node], list[Edge]]:
        seen_nodes = {node.id: node for node in seeds}
        seen_edges: dict[str, Edge] = {}
        frontier = list(seen_nodes)
        for _hop in range(hops):
            next_frontier: list[str] = []
            for node_id in frontier:
                for edge in self.store.get_edges_for_node(node_id):
                    if edge.invalid_at or edge.expired_at:
                        continue
                    seen_edges[edge.id] = edge
                    other_id = (
                        edge.target_node_id
                        if edge.source_node_id == node_id
                        else edge.source_node_id
                    )
                    if other_id in seen_nodes:
                        continue
                    other = self.store.get_node(other_id)
                    if other and other.status == NodeStatus.active:
                        seen_nodes[other_id] = other
                        next_frontier.append(other_id)
            frontier = next_frontier
        return list(seen_nodes.values()), list(seen_edges.values())


# API for fastapi server, this will be created by an endpoint, and then it will invoke an independant research session in a new thread
# So the server's job is to create this object and make it run in another thread then wait for the results
class Researcher:
    """Concurrent, read-only question answering over the graph.

    Bounded concurrency via two semaphores: cheap reads and (expensive)
    agent runs. Every public method builds a fresh ResearchSession.
    """

    def __init__(
        self,
        gateway: ModelGateway,
        store: GraphStore,
        max_reads: int | None = None,
        max_agents: int | None = None,
    ):
        self.gateway = gateway
        self.store = store
        s = gateway.settings
        self.read_sem = asyncio.Semaphore(max_reads or s.service_max_reads)
        self.agent_sem = asyncio.Semaphore(max_agents or s.service_max_agents)

    def _session(self, overrides: dict[str, Any] | None = None) -> ResearchSession:
        session = ResearchSession(self.gateway, self.store)
        session.apply_overrides(overrides)
        return session

    async def _run(
        self, fn: Callable[[ResearchSession], Any], use_agent_sem: bool = False
    ) -> Any:
        sem = self.agent_sem if use_agent_sem else self.read_sem
        async with sem:
            return await asyncio.to_thread(lambda: fn(self._session()))

    async def get(self) -> tuple[list[Node], list[Edge]]:
        return await self._run(lambda s: s.get())

    async def health(self, node_id: str | None = None) -> GraphStats:
        return await self._run(lambda s: s.health(node_id))

    async def read_node(self, node_id: str) -> Node | None:
        return await self._run(lambda s: s.read_node(node_id))

    async def follow_link(
        self, node_id: str, label=None, direction: str = "both", limit=None
    ):
        return await self._run(
            lambda s: s.follow_link(
                node_id, label=label, direction=direction, limit=limit
            )
        )

    async def query(self, query_type: str, value: str) -> QueryResult:
        return await self._run(lambda s: s.query(query_type, value))

    async def search(self, q: str, limit: int | None = None) -> list[Node]:
        return await self._run(lambda s: s.search(q, limit))

    async def ask(
        self,
        question: str,
        on_event: Callable[[dict], None] | None = None,
        overrides: dict[str, Any] | None = None,
        stop_event: threading.Event | None = None,
    ) -> AgentAnswer:
        def work():
            return self._session(overrides).ask(
                question,
                persist=False,
                on_event=on_event,
                stop_event=stop_event,
            )

        if on_event and self.agent_sem.locked():
            on_event({"type": "queued_for_agent"})

        async with self.agent_sem:
            return await asyncio.to_thread(work)
