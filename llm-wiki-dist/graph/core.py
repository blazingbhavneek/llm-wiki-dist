# region Imports

from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# endregion Imports


# region Models


#  settings
class Settings(BaseModel):

    # For Chat UI
    chat_base_url: str = "http://10.160.144.101:51029/v1"
    chat_api_key: str = "local"
    chat_model: str = "gemma-4-31B"
    chat_temperature: float = 0.4
    agent_max_steps: int = 40
    agent_patience: int = 20

    # ask() early-exit routing: reuse an existing agent note or answer with
    # shallow RAG when the graph already covers the question; else deep research
    agent_early_exit: bool = True
    early_exit_candidates: int = 20
    shallow_answer_max_nodes: int = 6
    # Ask the model for the separate lookups a question needs and merge their
    # retrievals, so multi-step questions seed the agent with the intermediate
    # nodes a single query embedding never surfaces. One extra completion.
    decompose_query: bool = True
    decompose_max_queries: int = 4

    # Compile time/ingest time

    # embeddings
    embed_backend: str = "server"
    embed_base_url: str = "http://10.160.144.101:51024/v1"
    embed_api_key: str = "local"
    embed_model: str = "cl-nagoya/ruri-v3-310m"
    hf_embed_model: str = "cl-nagoya/ruri-v3-310m"
    hf_device: str = "cpu"  # fallback device id if server fails
    embed_dim: int = 768

    # reranker
    rerank_backend: str = "server"
    rerank_base_url: str = "http://10.160.144.101:51025/v1"
    rerank_api_key: str = "local"
    rerank_model: str = "cl-nagoya/ruri-v3-reranker-310m"
    hf_rerank_model: str = "cl-nagoya/ruri-v3-reranker-310m"
    rerank_device: str = "cpu"

    # db
    database_path: str = ".wiki/moove_wiki.sqlite"

    # edge
    edge_candidate_k: int = 50
    vector_query_k: int = 50

    # on endogenenous node change
    cascade_max_hops: int = 3
    cascade_max_nodes: int = 100

    # search parameters
    search_rrf_k: int = 60
    search_candidate_pool: int = 50
    rerank_top_k: int = 20
    entity_dedup: bool = True

    #  evidence-first search: chunking (chars)
    search_big_chunk_size: int = 3000
    search_big_chunk_overlap: int = 300
    search_small_chunk_size: int = 512
    search_small_chunk_overlap: int = 80

    #  evidence-first search: retrieval pools -
    pool_node_bm25: int = 50
    pool_vec_body: int = 50
    pool_vec_summary: int = 50
    pool_item_bm25: int = 150
    pool_vec_item: int = 300

    #  evidence-first search: weighted RRF field weights
    weight_item_bm25: float = 1.35
    weight_title_vec: float = 1.30
    weight_claim_vec: float = 1.25
    weight_small_chunk_vec: float = 1.15
    weight_summary_vec: float = 1.00
    weight_big_chunk_vec: float = 0.95
    weight_node_bm25: float = 0.90
    weight_body_vec: float = 0.75

    #  evidence-first search: caps + rerank/MMR
    evidence_max_per_node: int = 3
    evidence_max_per_field: int = 2
    evidence_dedup_char_window: int = 200
    evidence_rerank_pool: int = 120
    evidence_mmr_lambda: float = 0.75
    subagent_count: int = 5
    subagent_concurrency: int = 3
    subagent_max_steps: int = 20
    subagent_min_reads: int = 8
    subagent_max_reads: int = 30

    # API service concurrency (semaphores in ReadGraphService)
    service_max_reads: int = 16
    service_max_agents: int = 4

    # Maximum number of independent ingestion/model requests in flight.  This
    # applies to short-document batches as well as every stage of the native
    # long-document chunk-and-ingest path.
    ingest_concurrency: int = 4

    # Recluster after this many source additions. Reclustering renames every
    # cluster, costing ~2 LLM calls per cluster, so it must not run per node
    # during batch ingestion. 0 disables automatic reclustering entirely; the
    # caller is then responsible for one explicit refresh_clusters() at the end.
    recluster_every: int = 10

    # TODO: Reactivate it when serving with docker container and pre installed chrome headless etc
    enable_mermaid: bool = True
    mermaid_repair_attempts: int = 3
    mermaid_cli_bin: str = "mmdc"
    mermaid_puppeteer_config: str = "./puppeteer-config.json"
    mermaid_render_timeout: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            chat_base_url=env("OPENAI_BASE_URL", cls.chat_base_url),
            chat_api_key=env("OPENAI_API_KEY", cls.chat_api_key),
            chat_model=env("WIKI_MODEL", cls.chat_model),
            chat_temperature=float(env("WIKI_TEMPERATURE", cls.chat_temperature)),
            embed_backend=env("WIKI_EMBED_BACKEND", cls.embed_backend),
            embed_base_url=env(
                "WIKI_EMBED_BASE_URL", env("OPENAI_EMBED_BASE_URL", cls.embed_base_url)
            ),
            embed_api_key=env("WIKI_EMBED_API_KEY", cls.embed_api_key),
            embed_model=env("WIKI_EMBED_MODEL", cls.embed_model),
            hf_embed_model=env("WIKI_HF_EMBED_MODEL", cls.hf_embed_model),
            hf_device=env("WIKI_HF_DEVICE", cls.hf_device),
            embed_dim=int(env("WIKI_EMBED_DIM", cls.embed_dim)),
            rerank_backend=env("WIKI_RERANK_BACKEND", cls.rerank_backend),
            rerank_base_url=env(
                "WIKI_RERANK_BASE_URL", env("WIKI_EMBED_BASE_URL", cls.rerank_base_url)
            ),
            rerank_api_key=env("WIKI_RERANK_API_KEY", cls.rerank_api_key),
            rerank_model=env("WIKI_RERANK_MODEL", cls.rerank_model),
            hf_rerank_model=env("WIKI_HF_RERANK_MODEL", cls.hf_rerank_model),
            rerank_device=env("WIKI_RERANK_DEVICE", cls.rerank_device),
            database_path=env("WIKI_DB", cls.database_path),
            edge_candidate_k=int(env("WIKI_EDGE_K", cls.edge_candidate_k)),
            vector_query_k=int(env("WIKI_VECTOR_K", cls.vector_query_k)),
            cascade_max_hops=int(env("WIKI_CASCADE_MAX_HOPS", cls.cascade_max_hops)),
            cascade_max_nodes=int(env("WIKI_CASCADE_MAX_NODES", cls.cascade_max_nodes)),
            agent_max_steps=int(env("WIKI_AGENT_MAX_STEPS", cls.agent_max_steps)),
            agent_patience=int(env("WIKI_AGENT_PATIENCE", cls.agent_patience)),
            agent_early_exit=env(
                "WIKI_AGENT_EARLY_EXIT", "1" if cls.agent_early_exit else "0"
            )
            not in {"0", "false", "False", ""},
            early_exit_candidates=int(
                env("WIKI_EARLY_EXIT_CANDIDATES", cls.early_exit_candidates)
            ),
            shallow_answer_max_nodes=int(
                env("WIKI_SHALLOW_MAX_NODES", cls.shallow_answer_max_nodes)
            ),
            decompose_query=env(
                "WIKI_DECOMPOSE_QUERY", "1" if cls.decompose_query else "0"
            )
            not in {"0", "false", "False", ""},
            decompose_max_queries=int(
                env("WIKI_DECOMPOSE_MAX_QUERIES", cls.decompose_max_queries)
            ),
            search_rrf_k=int(env("WIKI_SEARCH_RRF_K", cls.search_rrf_k)),
            entity_dedup=env("WIKI_ENTITY_DEDUP", "1" if cls.entity_dedup else "0")
            not in {"0", "false", "False", ""},
            search_candidate_pool=int(
                env("WIKI_SEARCH_POOL", cls.search_candidate_pool)
            ),
            rerank_top_k=int(env("WIKI_RERANK_TOP_K", cls.rerank_top_k)),
            search_big_chunk_size=int(
                env("WIKI_SEARCH_BIG_CHUNK_SIZE", cls.search_big_chunk_size)
            ),
            search_big_chunk_overlap=int(
                env("WIKI_SEARCH_BIG_CHUNK_OVERLAP", cls.search_big_chunk_overlap)
            ),
            search_small_chunk_size=int(
                env("WIKI_SEARCH_SMALL_CHUNK_SIZE", cls.search_small_chunk_size)
            ),
            search_small_chunk_overlap=int(
                env("WIKI_SEARCH_SMALL_CHUNK_OVERLAP", cls.search_small_chunk_overlap)
            ),
            pool_node_bm25=int(env("WIKI_POOL_NODE_BM25", cls.pool_node_bm25)),
            pool_vec_body=int(env("WIKI_POOL_VEC_BODY", cls.pool_vec_body)),
            pool_vec_summary=int(env("WIKI_POOL_VEC_SUMMARY", cls.pool_vec_summary)),
            pool_item_bm25=int(env("WIKI_POOL_ITEM_BM25", cls.pool_item_bm25)),
            pool_vec_item=int(env("WIKI_POOL_VEC_ITEM", cls.pool_vec_item)),
            weight_item_bm25=float(env("WIKI_W_ITEM_BM25", cls.weight_item_bm25)),
            weight_title_vec=float(env("WIKI_W_TITLE_VEC", cls.weight_title_vec)),
            weight_claim_vec=float(env("WIKI_W_CLAIM_VEC", cls.weight_claim_vec)),
            weight_small_chunk_vec=float(
                env("WIKI_W_SMALL_CHUNK_VEC", cls.weight_small_chunk_vec)
            ),
            weight_summary_vec=float(env("WIKI_W_SUMMARY_VEC", cls.weight_summary_vec)),
            weight_big_chunk_vec=float(
                env("WIKI_W_BIG_CHUNK_VEC", cls.weight_big_chunk_vec)
            ),
            weight_node_bm25=float(env("WIKI_W_NODE_BM25", cls.weight_node_bm25)),
            weight_body_vec=float(env("WIKI_W_BODY_VEC", cls.weight_body_vec)),
            evidence_max_per_node=int(
                env("WIKI_EVIDENCE_MAX_PER_NODE", cls.evidence_max_per_node)
            ),
            evidence_max_per_field=int(
                env("WIKI_EVIDENCE_MAX_PER_FIELD", cls.evidence_max_per_field)
            ),
            evidence_dedup_char_window=int(
                env("WIKI_EVIDENCE_DEDUP_CHAR_WINDOW", cls.evidence_dedup_char_window)
            ),
            evidence_rerank_pool=int(
                env("WIKI_EVIDENCE_RERANK_POOL", cls.evidence_rerank_pool)
            ),
            evidence_mmr_lambda=float(
                env("WIKI_EVIDENCE_MMR_LAMBDA", cls.evidence_mmr_lambda)
            ),
            subagent_count=int(env("WIKI_SUBAGENT_COUNT", cls.subagent_count)),
            subagent_concurrency=int(
                env("WIKI_SUBAGENT_CONCURRENCY", cls.subagent_concurrency)
            ),
            subagent_max_steps=int(
                env("WIKI_SUBAGENT_MAX_STEPS", cls.subagent_max_steps)
            ),
            subagent_min_reads=int(
                env("WIKI_SUBAGENT_MIN_READS", cls.subagent_min_reads)
            ),
            subagent_max_reads=int(
                env("WIKI_SUBAGENT_MAX_READS", cls.subagent_max_reads)
            ),
            service_max_reads=int(env("WIKI_SERVICE_MAX_READS", cls.service_max_reads)),
            service_max_agents=int(
                env("WIKI_SERVICE_MAX_AGENTS", cls.service_max_agents)
            ),
            ingest_concurrency=max(
                1,
                int(
                    env(
                        "WIKI_INGEST_CONCURRENCY",
                        env("WIKI_CHUNK_CONCURRENCY", cls.ingest_concurrency),
                    )
                ),
            ),
            recluster_every=int(env("WIKI_RECLUSTER_EVERY", cls.recluster_every)),
            enable_mermaid=env(
                "WIKI_ENABLE_MERMAID", "1" if cls.enable_mermaid else "0"
            )
            not in {"0", "false", "False", ""},
            mermaid_repair_attempts=int(
                env("WIKI_MERMAID_REPAIR_ATTEMPTS", cls.mermaid_repair_attempts)
            ),
            mermaid_cli_bin=env("WIKI_MERMAID_CLI_BIN", cls.mermaid_cli_bin),
            mermaid_puppeteer_config=env(
                "WIKI_MERMAID_PUPPETEER_CONFIG", cls.mermaid_puppeteer_config
            ),
            mermaid_render_timeout=int(
                env("WIKI_MERMAID_RENDER_TIMEOUT", cls.mermaid_render_timeout)
            ),
        )


# from_env() reads Settings.<field> as a plain default value; pydantic v2 stores
# a FieldInfo there instead, so expose the resolved default (handling
# default_factory too, in case a future field uses one) as the class attribute.
for _settings_field, _settings_info in Settings.model_fields.items():
    if _settings_info.default_factory is not None:
        setattr(Settings, _settings_field, _settings_info.default_factory())
    else:
        setattr(Settings, _settings_field, _settings_info.default)


#  whether a node was created from source file or made by AI Agent
class NodeType(str, Enum):
    endogenous = "endogenous"
    exogenous = "exogenous"


# whether a node is overruled by newer version, with updated version
class NodeStatus(str, Enum):
    active = "active"
    stale = "stale"
    superseded = "superseded"
    deleted = "deleted"


# Contains the actual information
class Node(BaseModel):
    id: str
    body: str
    type: NodeType = NodeType.endogenous
    title: str = ""
    original_document_name: str | None = None
    source_path: str | None = None
    source_ranges: list[tuple[int, int]] = Field(default_factory=list)
    source_version: str | None = None
    source_material_hash: str | None = None
    entity: str = ""
    claims: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    summary: str = ""
    cluster: str | None = None
    # HyDE-style probe: "what broader concept/field does this connect to",
    # embedded separately (vec_bridge) to surface analogically related nodes
    # that plain body/summary embeddings would never rank as neighbors.
    bridge_probe: str = ""
    status: NodeStatus = NodeStatus.active
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


# Connecting nodes so agent knows what to read next
class Edge(BaseModel):
    id: str
    source_node_id: str
    target_node_id: str
    label: str
    summary: str = ""

    # In case new information comes existing relation is invalidated
    created_at: str = Field(default_factory=now_iso)
    valid_at: str | None = None
    invalid_at: str | None = None
    expired_at: str | None = None
    source_episode_ids: list[str] = Field(default_factory=list)


#  LLM Structured outputs


# singular edge
class EdgeSuggestion(BaseModel):
    target_node_id: str
    label: str = "related"
    summary: str = ""


# list of above edge model
class EdgeSuggestions(BaseModel):
    edges: list[EdgeSuggestion] = Field(default_factory=list)


# HyDE-style bridge probe: one short "what does this connect to" sentence
class BridgeProbe(BaseModel):
    probe: str = ""


# For metadata filtering
class Keywords(BaseModel):
    keywords: list[str] = Field(default_factory=list)


# Factual grounds extracted from a node
class ClaimExtraction(BaseModel):
    entity: str = ""
    claims: list[str] = Field(default_factory=list)


# Whether two entities are same or not
class EntityMatch(BaseModel):
    is_same: bool = False
    target_node_id: str | None = None


# Early-exit routing decision for ask(): reuse an existing agent note verbatim,
# answer shallowly from retrieved evidence, or run the deep research agent.


class RouteMode(str, Enum):
    """
    Strategy selected by the early-exit router.
    """

    reuse = "reuse"
    shallow = "shallow"
    deep = "deep"


class RouteDecision(BaseModel):
    """
    Routing decision for ask() early-exit behavior.

    The router chooses the cheapest strategy that can still answer accurately:
    - reuse: return an existing agent-created answer note verbatim
    - shallow: answer directly from retrieved evidence snippets
    - deep: run the full research agent
    """

    mode: RouteMode = Field(
        default=RouteMode.deep,
        description=(
            "The routing strategy to use. "
            "'reuse' means an existing agent_note already answers the question almost completely, "
            "so return that note verbatim. "
            "'shallow' means the question is narrow and can be answered accurately from the retrieved evidence snippets. "
            "'deep' means the question is broad, multi-topic, uncertain, or the retrieved evidence is insufficient, "
            "so the full research agent should run."
        ),
    )

    node_id: str | None = Field(
        default=None,
        description=(
            "The exact candidate node id to reuse when mode is 'reuse'. "
            "This must be copied exactly from one of the provided candidate node IDs. "
            "Required for mode='reuse'. "
            "Should be null for mode='shallow' or mode='deep'."
        ),
    )

    reason: str = Field(
        default="",
        description=(
            "A short one-sentence explanation for the routing decision. "
            "Explain why the selected mode is sufficient or why deeper research is needed. "
            "Do not leave this empty."
        ),
    )


#  Query response from the graph
class QueryResult(BaseModel):
    query_type: str
    value: str
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)


# Final agent answer
class AgentAnswer(BaseModel):
    question: str
    answer: str = ""
    cited_node_ids: list[str] = Field(default_factory=list)
    exogenous_node_id: str | None = None
    steps: int = 0


class GraphStats(BaseModel):
    total_nodes: int
    active_nodes: int
    endogenous_nodes: int
    exogenous_nodes: int
    total_edges: int
    isolated_nodes: int
    avg_degree: float
    density: float
    mean_neighbor_overlap: float
    clusters: dict[str, int] = Field(default_factory=dict)
    target_node_id: str | None = None


# In case model emits english name for cluster, had this problem earlier
class ClusterRename(BaseModel):
    original_name: str
    new_name: str


class ClusterRenamePlan(BaseModel):
    renames: list[ClusterRename] = []


# Interface describing the methods an LLM client must implement, independent of
# the underlying provider. @runtime_checkable allows isinstance(..., LlmClient) checks.
# Protocol defining the interface expected of an LLM client. Protocols are for
# structural type checking (what methods an object has), not shared implementation
# or inheritance, making them ideal when multiple unrelated classes should be interchangeable.
@runtime_checkable
class LlmClient(Protocol):
    def complete(self, system_prompt: str, user_content: str) -> str: ...
    def complete_structured(
        self, system_prompt: str, user_content: str, output_model: type[Any]
    ) -> Any: ...


@runtime_checkable
class EmbedderPort(Protocol):
    dim: int
    model_name: str

    def embed_document(self, text: str) -> list[float]: ...
    def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class RerankerPort(Protocol):
    def top_k(
        self, query: str, items: list[tuple[str, Any]], k: int
    ) -> list[tuple[Any, float]]: ...


# Tools Schemas
class search(BaseModel):
    """Search the wiki for nodes matching a text query."""

    text: str = Field(description="keywords to search for")


class read(BaseModel):
    """Read a node's full body and metadata by id."""

    node_id: str = Field(description="id of the node to read")


class follow_link(BaseModel):
    """Follow edges from a node to its neighboring nodes."""

    node_id: str = Field(description="id of the node to expand")
    direction: str = Field(
        default="both", description="'incoming', 'outgoing', or 'both'"
    )


class explore(BaseModel):
    """Hand distinct starting node ids to a team of exploration subagents."""

    node_ids: list[str] = Field(
        default_factory=list, description="distinct starting node ids"
    )


class finish(BaseModel):
    """Provide the final answer and the node ids used as evidence."""

    answer: str = Field(description="the final answer, grounded in node content")
    cited_node_ids: list[str] = Field(
        default_factory=list, description="ids that support the answer"
    )


LEAD_TOOLS = [search, explore, finish]
SUBAGENT_TOOLS = [search, read, follow_link, finish]


@dataclass
class Subrun:
    """Per-subagent run state. Created once, captured by the dispatch closure —
    never threaded through call signatures."""

    start_id: str
    index: int
    visited: list[str] = field(default_factory=list)
    read_ids: set[str] = field(default_factory=set)
    empty_streak: int = 0


@dataclass
class EvidenceHit:
    """One retrieval hit, normalized across every pool (node + search_item)."""

    node_id: str
    field: str
    item_id: str | None
    text: str
    rank: int
    weight: float
    start_char: int | None = None

    def contribution(self, rrf_k: int) -> float:
        return self.weight / (rrf_k + self.rank)


@runtime_checkable
class EnrichmentWorker(Protocol):
    """The subset of GraphWriteSession the queue needs. Kept tiny so the two
    modules stay decoupled and this file never imports graph.graph."""

    def enrich_summary(self, node_id: str) -> None: ...
    def enrich_entity_dedup(self, node_id: str) -> None: ...
    def enrich_cascade(
        self, replacements: dict[str, str], stale_sources: list[str]
    ) -> None: ...
    def refresh_clusters(self) -> None: ...

    # meta accessors (already on GraphWriteSession as _db_get_meta/_db_set_meta;
    # a thin public alias will be added when wiring)
    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...


@dataclass(frozen=True)
class EnrichJob:
    kind: str  # "summary" | "entity_dedup" | "cascade" | "maybe_recluster" | "cluster_bridge"
    node_id: str | None = None
    replacements: dict[str, str] = field(default_factory=dict)
    stale_sources: list[str] = field(default_factory=list)


# endregion Models


# region Prompts

GRAPH_SYSTEM_PROMPT = "You maintain a concise, factual knowledge-graph wiki."

SUMMARY_PROMPT = (
    "Summarize this Markdown node for a knowledge graph. Use only facts present in the text. "
    "Keep the summary to 1-3 sentences, with no preamble. "
    "If the text contains an answer to a question, a procedure, rule, policy, decision criterion, "
    "method for making a change, causal explanation, condition, exception, deadline, responsible "
    "party, numeric value, or scope, prioritize retaining those specific details. "
    "Do not lose document names, section names, item names, subjects, conditions, values, dates, "
    "roles, or exceptions through abstract paraphrasing. "
    "Do not infer or add facts that are not present in the text."
)

KEYWORD_PROMPT = (
    "Extract important keywords and entities from this text for graph search, including document "
    "names, system names, rule names, item names, headings, subject names, organization names, "
    "role names, responsible parties, places, dates, deadlines, monetary amounts, numeric values, "
    "conditions, exceptions, procedure names, state names, causes, effects, proper nouns, and "
    "technical terms. Return at most 12 unique keywords in descending order of importance. "
    "If the user's question or the text concerns how to make a change, how to respond, a decision, "
    "a cause, a procedure, or a condition, prioritize terms that express the subject, required "
    "action, conditions, exceptions, decision criteria, and the states before and after the change. "
    "Use lowercase unless the term is an acronym or identifier."
)

CLAIM_PROMPT = (
    "Extract facts from this Markdown node that provide stable identifying information for revision "
    "matching. Return one primary entity or topic and at most 20 atomic claims. Claims must be short "
    "factual statements directly supported by the text. Prefer facts that remain recognizable even "
    "if the source document is reordered. If the text contains rules, procedures, policies, decision "
    "criteria, methods for making changes, causal explanations, conditions, exceptions, deadlines, "
    "responsibilities, numeric values, or scope, prioritize relationships among the subject, "
    "conditions, actions, outcomes, exceptions, dates, values, and roles. Do not infer or add facts "
    "that are not present in the text."
)

ROUTER_PROMPT = (
    "You are the question router for a knowledge-graph wiki. You are given a question and candidate "
    "nodes found by search (kind='agent_note' is an answer note previously created by an agent, and "
    "kind='source' is source material). Choose the least expensive strategy that is sufficient.\n\n"
    "Options:\n"
    "- reuse: Choose this when an agent_note among the candidates covers the same scope as the "
    "question and answers it almost completely at the requested level of specificity. Return that "
    "candidate's exact node_id. The answer will reuse the note body verbatim.\n"
    "- shallow: Choose this when the question is narrow and the candidate evidence excerpts alone "
    "can answer it accurately and sufficiently. Set node_id to null.\n"
    "- deep: Choose this when the question is broad, requires checking multiple parts of the "
    "documentation, has conflicting candidates, requires interpretation or organizing procedures, "
    "changes, decisions, causes, or conditions, or has insufficient evidence. Set node_id to null.\n\n"
    "Important rules:\n"
    "- Do not choose shallow merely because the question is short. Even for a short question, choose "
    "deep if answering requires how to make a change, a response policy, a cause, reason, procedure, "
    "exception condition, decision criterion, organization of multiple conditions, or checking "
    "relationships across documents.\n"
    "- However, choose shallow when the question asks for a single fact that the candidate evidence "
    "directly answers, such as a deadline, date, responsible party, monetary amount, numeric value, "
    "name, definition, single condition, or fact stated in one place.\n"
    "- As a rule, choose deep when the user asks about an action, decision, interpretation, change, "
    "cause, or procedure with wording such as 'what should be changed,' 'where should it be changed,' "
    "'how should I do it,' 'why,' 'which procedure,' 'under what conditions,' 'what is required,' "
    "'what should be checked,' 'how should this be handled,' or 'which one should be chosen.'\n"
    "- For such questions, choose shallow only if the candidate evidence alone clearly establishes "
    "all of the following: the subject, the required action or answer, the basis, the conditions, "
    "and any exceptions or constraints.\n"
    "- Do not reuse an agent_note merely because it is relevant. Choose reuse only when it satisfies "
    "the scope and specificity requested by the question.\n"
    "- Choose reuse when the highest-ranked candidate with kind='agent_note' answers the question "
    "directly and with sufficient specificity. However, if its answer is conceptual or abstract and "
    "lacks the subject, conditions, procedure, or basis, choose deep instead of reuse.\n"
    "- When choosing reuse, copy the candidate id exactly into node_id.\n"
    "- node_id is required for reuse.\n"
    "- node_id must be null for shallow and deep.\n"
    "- For only a partial match, choose shallow or deep rather than reuse.\n"
    "- If uncertain, choose deep. Prefer thorough research to an inaccurate shortcut.\n"
    "- Always provide reason as one short sentence explaining the decision. It must not be empty."
)

SHALLOW_ANSWER_PROMPT = (
    "Answer the question concisely using only the provided node excerpts as evidence.\n"
    "- Answer in the same language as the question.\n"
    "- Do not add or infer facts that are not present in the excerpts.\n"
    "- Write the answer in Markdown.\n"
    "- Cite the ids of the nodes used as evidence inline in backticks (for example, `node:...`).\n"
    "- If the excerpts alone cannot answer the question, state that clearly.\n\n"
    "Cases suitable for a shallow answer:\n"
    "- The question asks for a single fact and the excerpts contain a direct answer, such as a "
    "deadline, date, responsible party, monetary amount, numeric value, name, definition, single "
    "condition, or single stated fact.\n\n"
    "Cases requiring caution:\n"
    "- When the user asks about an action, decision, interpretation, change, cause, or procedure with "
    "wording such as 'what should be changed,' 'where should it be changed,' 'how should I do it,' "
    "'why,' 'which procedure,' 'under what conditions,' 'what is required,' 'what should be checked,' "
    "'how should this be handled,' or 'which one should be chosen,' do not answer by generalizing a "
    "single sentence from an excerpt.\n"
    "- In such cases, identify the subject, required action, conditions, basis, exceptions, and "
    "cautions to the extent that they appear in the excerpts.\n"
    "- If the excerpts do not contain enough information, clearly state, 'The excerpts alone do not "
    "contain enough information for a specific answer,' and explain only the known facts with evidence.\n"
    "- Do not stop at a single abstract statement.\n"
    "Answer format:\n"
    "- Organize the answer with headings, bullet points, tables, and short paragraphs as appropriate, "
    "rather than as a series of long paragraphs.\n"
    "- When there are comparisons, conditions, procedures, item lists, or numeric information, use a "
    "table or bullet list whenever possible for clarity.\n"
    "- Clearly separate key points, assumptions, conclusions, and cautions so the reader can understand "
    "the answer quickly.\n"
    "- Do not add unsupported information or pad the content merely for appearance.\n\n"

    "Diagrams:\n"
    "- If the answer includes multiple interacting or related entities, procedures, systems, concepts, "
    "conditional branches, causes and effects, dependencies, or data flows, create a Mermaid diagram "
    "whenever practical to aid understanding.\n"
    "- Mermaid diagrams are strongly recommended. Omit one only for a single-fact answer or when a "
    "diagram would add no information.\n"
    "- If included, place exactly one fenced ```mermaid block immediately before the 'Citations:' section.\n"
    "- Use simple ASCII node IDs (n1, n2, proc_a), and put spaces, punctuation, and long text inside "
    "quoted labels: n1[\"Linear layer\"] --> n2[\"GEMM\"].\n"
    "- Include only Mermaid syntax in the mermaid block, with no prose or bullet points.\n"
    "- Do not invent unsupported document names, item names, conditions, values, or procedures in the diagram.\n\n"

    "Citation format:\n"
    "- The final answer must end with a 'Citations:' section.\n"
    "- 'Citations:' must be the final section of the answer. Do not add text after it.\n"
    "- List only node IDs actually used as sources for the answer.\n"
    "- Put each citation in its own entry with one blank line between entries; that is, separate entries "
    "with two newline characters. Never put multiple node IDs on the same line.\n"
    "- Do not combine node IDs using commas, enumeration commas, slashes, or adjacent entries in one bullet.\n"
    "- Every line must use this exact format: node_id : Title\n"
    "- Preserve node_id exactly as displayed. Use the Title exactly as shown in search results or "
    "subagent reports.\n"
    "- Do not omit or invent titles.\n"
    "- Pass cited_node_ids only the same IDs listed in the 'Citations:' section.\n"
    "- Follow this citation format exactly:\n"
    "\nCitations:\n"
    "\nnode:abc123 : Example Title\n"
    "\nnode:def456 : Another Title\n"
)

REGENERATE_EXOGENOUS_PROMPT = (
    "Regenerate the derived wiki node after its supporting source material has changed. Use the "
    "previous derived node only to understand the intended topic and structure. The new node body "
    "must be supported solely by the current supporting material. Remove stale claims that are no "
    "longer supported. Keep the result concise, factual, and in Markdown, with no preamble."
)

EDGE_PROMPT = (
    "You maintain a wiki graph. Given a new node and a small group of candidate existing nodes "
    "(found using multiple retrieval methods, not necessarily semantic similarity alone), determine "
    "which candidates the new node should link to and why.\n"
    "Each node's 'header' is the larger section or topic to which its chunk belongs. Use the header "
    "to judge chunks whose body alone provides little context.\n"
    "Rules:\n"
    "- Use only the provided candidate IDs.\n"
    "- A label is a short verb phrase explaining how the target relates to the new node, such as "
    "'uses', 'defines', 'example-of', 'prerequisite-for', or 'contradicts'.\n"
    "- Propose an edge only when the relationship is clearly useful. Omit weak relationships.\n"
    "- Even if only a few candidates (3-4) are provided, you do not need to force links to all of them.\n"
    "- summary: Write one short clause explaining the link."
)

BRIDGE_PROBE_PROMPT = (
    "You maintain a wiki graph. Read the following note, which may include surrounding context, and "
    "write 1-2 sentences about the fields, concepts, or applications to which it might connect.\n"
    "Rules:\n"
    "- Do not paraphrase the note body. Describe the broader context or related areas, fields, or "
    "applications suggested by the text.\n"
    "- If uncertain, do not force a broader connection; simply state the text's subject briefly."
)

CLUSTER_BRIDGE_PROMPT = (
    "You maintain a wiki graph. You are given two nodes representing different clusters (topics), a "
    "pair not found directly through semantic similarity or keywords. Determine whether they have a "
    "non-obvious but genuinely useful connection, such as a shared mechanism, cause, application, or analogy.\n"
    "Rules:\n"
    "- Use only the provided candidate IDs.\n"
    "- Do not force a connection if the relationship is unclear or contrived. Return an empty list in that case.\n"
    "- A label is a short verb phrase explaining how the target relates to the new node, such as "
    "'analogous-to', 'shares-mechanism-with', 'application-of', or 'inspired-by'.\n"
    "- summary: Explain the connection briefly and specifically."
)

ENTITY_DEDUP_PROMPT = (
    "You maintain a wiki graph. Given a new node and a list of candidate existing nodes, determine "
    "whether the new node describes the same real-world entity or topic as exactly one candidate.\n"
    "Rules:\n"
    "- The same entity means the same specific thing (the same API, tool, or concept), not merely a "
    "related or similar topic.\n"
    "- Be conservative. If uncertain, answer is_same=false. Never merge homonyms that refer to "
    "different things.\n"
    "- If there is a match, return is_same=true and that candidate's target_node_id. Otherwise, "
    "return is_same=false and target_node_id=null."
)

CLUSTER_NAMER_SYSTEM = (
    "You name a single topic cluster in a knowledge graph. Always return the topic name in English. "
    "Choose a specific name that can be distinguished from other names already in use. Prioritize "
    "the most specific technical subtopic evident in the keywords and sample section titles. Avoid "
    "using only a broad source name such as CUDA, SYCL, OpenMP, or oneAPI when a narrower topic is "
    "available. Return only one concise English topic name of about four words at most, with no "
    "quotation marks, punctuation, or explanation."
)

MAIN_AGENT_SYSTEM_PROMPT = (
    "You are the lead researcher answering questions from a knowledge-graph wiki. You coordinate the "
    "research and do not read nodes yourself.\n\n"
    "You have three capabilities:\n"
    "- search(text): Search for candidate nodes that have already been reranked by relevance. It "
    "returns only node IDs, titles, and summaries.\n"
    "- explore(node_ids): Give a list of unique starting node IDs to a team of subagents. Each "
    "subagent reads through the graph from one starting node and reports its findings. Use this to "
    "investigate promising leads.\n"
    "- finish(answer, cited_node_ids): Submit the final synthesized answer.\n\n"

    "Workflow:\n"
    "1. Search for the main question first. Then search for important terms, proper nouns, document "
    "names, system names, subject names, conditions, roles, dates, numeric values, procedure names, "
    "and likely decision criteria from the question. Perform several searches to surface different "
    "parts of the graph.\n"
    "2. From all candidates, select the best unique starting nodes that cover distinct subtopics. "
    "Avoid near-duplicates so the subagents explore different subgraphs.\n"
    "3. Call explore(node_ids) once with those starting nodes. The team explores in parallel and "
    "returns each subagent's findings and the bodies of the nodes it read.\n"
    "4. Read the subagent reports. If clear gaps remain, you may search again and run explore one more "
    "time. Otherwise, synthesize the answer.\n"
    "5. Use finish to submit a sufficient answer based only on what the subagents reported. Do not put "
    "node IDs or citations in the middle of the answer body. Put all citations only in the final "
    "'Citations:' section.\n\n"

    "Guidance for questions requiring deep research:\n"
    "- If the user asks about actions, changes, responses, decisions, causes, reasons, procedures, "
    "conditions, exceptions, effects, comparisons, or consistency across documents, do not finish "
    "with only a conceptual answer.\n"
    "- In such cases, identify as much as possible of the following: the subject; applicable document, "
    "section, rule, or item; required action; conditions to satisfy; decision criteria; states before "
    "and after a change; exceptions; cautions; and evidence.\n"
    "- If the question asks only for a single fact, answer it directly without expanding the scope "
    "unnecessarily, such as a deadline, date, responsible party, monetary amount, numeric value, name, "
    "definition, or single condition.\n"
    "- However, even when a question appears to ask for one fact, consider additional search or explore "
    "calls if candidates conflict, the answer is conditional, or it varies by context.\n"
    "- If subagent reports lack a specific subject, conditions, or evidence, consider additional search "
    "or explore calls.\n"
    "- If specific information still cannot be found, clearly distinguish the facts found from the "
    "missing information. Do not invent unsupported document names, item names, conditions, values, "
    "or procedures.\n\n"

    "Answer format:\n"
    "- Organize the answer with headings, bullet points, tables, and short paragraphs as appropriate, "
    "rather than as a series of long paragraphs.\n"
    "- When there are comparisons, conditions, procedures, item lists, or numeric information, use a "
    "table or bullet list whenever possible for clarity.\n"
    "- Clearly separate key points, assumptions, conclusions, and cautions so the reader can understand "
    "the answer quickly.\n"
    "- Do not add unsupported information or pad the content merely for appearance.\n\n"

    "Rules:\n"
    "- You cannot read node bodies directly. Depend on subagent reports.\n"
    "- Do not call finish before running explore at least once.\n"
    "- Prefer breadth. Give explore distinct starting points rather than several IDs about the same content.\n"
    "- Answer the user's question directly. Do not stop at a summary of related information.\n"
    "- Do not place node IDs, citations, references, or parenthetical sources anywhere in the answer body.\n"
    "- Preserve node IDs exactly as displayed. Do not abbreviate, convert, normalize, reorder, or "
    "reformat them. Copy the `node:` prefix, symbols, letter case, delimiters, spaces, and all other "
    "details exactly as shown.\n\n"

    "Diagrams:\n"
    "- If the answer includes multiple interacting or related entities, procedures, systems, concepts, "
    "conditional branches, causes and effects, dependencies, or data flows, create a Mermaid diagram "
    "whenever practical to aid understanding.\n"
    "- Mermaid diagrams are strongly recommended. Omit one only for a single-fact answer or when a "
    "diagram would add no information.\n"
    "- If included, place exactly one fenced ```mermaid block immediately before the 'Citations:' section.\n"
    "- Use simple ASCII node IDs (n1, n2, proc_a), and put spaces, punctuation, and long text inside "
    "quoted labels: n1[\"Linear layer\"] --> n2[\"GEMM\"].\n"
    "- Include only Mermaid syntax in the mermaid block, with no prose or bullet points.\n"
    "- Do not invent unsupported document names, item names, conditions, values, or procedures in the diagram.\n\n"

    "Citation format:\n"
    "- The final answer must end with a 'Citations:' section.\n"
    "- 'Citations:' must be the final section of the answer. Do not add text after it.\n"
    "- List only node IDs actually used as sources for the answer.\n"
    "- Put each citation in its own entry with one blank line between entries; that is, separate entries "
    "with two newline characters. Never put multiple node IDs on the same line.\n"
    "- Do not combine node IDs using commas, enumeration commas, slashes, or adjacent entries in one bullet.\n"
    "- Every line must use this exact format: node_id : Title\n"
    "- Preserve node_id exactly as displayed. Use the Title exactly as shown in search results or "
    "subagent reports.\n"
    "- Do not omit or invent titles.\n"
    "- Pass cited_node_ids only the same IDs listed in the 'Citations:' section.\n"
    "- Follow this citation format exactly:\n"
    "\nCitations:\n"
    "\nnode:abc123 : Example Title\n"
    "\nnode:def456 : Another Title\n"
)

SUBAGENT_SYSTEM_PROMPT = (
    "You are a research subagent exploring one area of a knowledge-graph wiki. The lead researcher "
    "has given you a starting node. Investigate it thoroughly and report specific, well-supported findings.\n\n"

    "Tools:\n"
    "- read(node_id): Read a node's complete body. Begin by reading your assigned node.\n"
    "- follow_link(node_id, direction): Move to a neighboring node to follow references, examples, "
    "prerequisites, and related concepts.\n"
    "- search(text): Search by keyword for additional nodes when needed within your area.\n"
    "- finish(answer, cited_node_ids): Report your findings and the IDs of the nodes you read.\n\n"

    "Rules:\n"
    "1. Read your assigned starting node first.\n"
    "2. Follow links and read 2-5 nodes within your area to gather actual evidence. If the subject, "
    "conditions, basis, exceptions, or procedure needed to answer the question are missing, perform "
    "additional searches or follow links as needed.\n"
    "3. Stay within your assigned scope. Other subagents cover the sibling starting nodes listed in "
    "the task. Do not explore them again; focus on your own subgraph so the team covers more ground.\n"
    "4. Report only what is supported by node bodies you actually read.\n"
    "5. Preserve node IDs exactly as displayed. Do not abbreviate, convert, normalize, reorder, or "
    "reformat them. Copy the `node:` prefix, symbols, letter case, delimiters, spaces, and all other "
    "details exactly as shown.\n"
    "6. Record the title as well as the node ID for every node you read. Use the title exactly as "
    "shown in tool results, search results, or the node body; never invent it.\n"
    "7. When done, give a focused summary of what this area reveals about the question and call finish "
    "with the node IDs used in cited_node_ids.\n\n"

    "Research guidance:\n"
    "- If the question asks for a single fact, report that fact and its evidence precisely, such as a "
    "deadline, date, responsible party, monetary amount, numeric value, name, definition, or single condition.\n"
    "- If the question asks about an action, change, response, decision, cause, reason, procedure, "
    "condition, exception, effect, or comparison, do not report only abstract concepts.\n"
    "- In such cases, look for the subject; applicable document, section, rule, or item; required action; "
    "conditions to satisfy; decision criteria; states before and after a change; exceptions; cautions; "
    "and evidence.\n"
    "- If these appear in the text you read, report the specific names, conditions, values, and statements "
    "as written.\n"
    "- If they do not appear, clearly report, 'No specific information was found in this area,' and "
    "explain only the related facts found, with evidence.\n\n"

    "Diagrams:\n"
    "- If the answer includes multiple interacting or related entities, procedures, systems, concepts, "
    "conditional branches, causes and effects, dependencies, or data flows, create a Mermaid diagram "
    "whenever practical to aid understanding.\n"
    "- Mermaid diagrams are strongly recommended. Omit one only for a single-fact answer or when a "
    "diagram would add no information.\n"
    "- If included, place exactly one fenced ```mermaid block immediately before the 'Citations:' section.\n"
    "- Use simple ASCII node IDs (n1, n2, proc_a), and put spaces, punctuation, and long text inside "
    "quoted labels: n1[\"Linear layer\"] --> n2[\"GEMM\"].\n"
    "- Include only Mermaid syntax in the mermaid block, with no prose or bullet points.\n"
    "- Do not invent unsupported document names, item names, conditions, values, or procedures in the diagram.\n\n"

    "Citation format:\n"
    "- The final answer must end with a 'Citations:' section.\n"
    "- 'Citations:' must be the final section of the answer. Do not add text after it.\n"
    "- List only node IDs from which you actually obtained information.\n"
    "- Do not place node IDs, citations, references, or parenthetical sources anywhere in the answer body.\n"
    "- Put each citation in its own entry with one blank line between entries; that is, separate entries "
    "with two newline characters. Never put multiple node IDs on the same line.\n"
    "- Do not combine node IDs using commas, enumeration commas, slashes, or adjacent entries in one bullet.\n"
    "- Every line must use this exact format: node_id : Title\n"
    "- Preserve node_id exactly as displayed. Use the Title exactly as shown in tool results, search "
    "results, or the node body.\n"
    "- Do not omit or invent titles.\n"
    "- Pass cited_node_ids only the same IDs listed in the 'Citations:' section.\n"
    "- Follow this citation format exactly:\n"
    "\nCitations:\n"
    "\nnode:abc123 : Example Title\n"
    "\nnode:def456 : Another Title\n"
)

MERMAID_FIX_SYSTEM = (
    "Fix the Mermaid diagram syntax so it can be rendered by mermaid-cli (mmdc). Return exactly one "
    "corrected fenced ```mermaid code block and nothing else."
)


# endregion Prompts


# region Helpers/Utils


# Single shared tokenizer. A module constant on purpose: compiling a regex is
# relatively expensive, so reuse one compiled instance across the module.

# regex to find mermaid blocks
_MERMAID_BLOCK_RE = re.compile(
    r"```mermaid[ \t]*\r?\n(?P<code>.*?)```", re.IGNORECASE | re.DOTALL
)

# Matches tokens like "foo/bar:v1.2", "abc_123", or "2025-01-31".
TOKEN_RE = re.compile(r"[a-z0-9_./:-]+")

# Matches separators/punctuation like " ", "_", ".", or "!!!" for slugification.
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# identifiers / hashing
def short_hash(text: str, length: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


# Identity of a whole source document, for recon dedup
def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# remove all special symbols and whitespace and repalce with hyphen
def slug(text: str, max_length: int = 40) -> str:
    value = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return value[:max_length] or "node"


def make_node_id(body: str, document_name: str | None = None) -> str:
    return f"node:{slug(document_name or 'node', 24)}:{short_hash(body)}"


def make_exogenous_node_id(seed: str) -> str:
    return f"exo:{short_hash(seed)}"


def make_edge_id(source_id: str, target_id: str, label: str) -> str:
    return f"edge:{short_hash(f'{source_id}|{target_id}|{label}', 16)}"


# chunking
def chunk_text(text: str, size: int, overlap: int) -> list[tuple[int, int, str]]:
    """Split ``text`` into overlapping character windows.

    Returns ``(start_char, end_char, chunk)`` triples. ``overlap`` is clamped to
    ``size - 1`` so the window always advances. Blank windows are skipped."""
    if not text:
        return []
    size = max(1, size)
    overlap = max(0, min(overlap, size - 1))
    step = size - overlap
    chunks: list[tuple[int, int, str]] = []
    length = len(text)
    start = 0
    while start < length:
        end = min(start + size, length)
        piece = text[start:end]
        if piece.strip():
            chunks.append((start, end, piece))
        if end >= length:
            break
        start += step
    return chunks


# text matching / scoring
def normalize_token(token: str) -> str:
    return token.strip().lower()


def normalize_text(text: str) -> str:
    return " ".join(normalize_token(token) for token in TOKEN_RE.findall(text.lower()))


def jaccard(left: set[str], right: set[str]) -> float:
    left = {value for value in left if value}
    right = {value for value in right if value}
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def token_jaccard(left: str, right: str) -> float:
    return jaccard(
        {normalize_token(t) for t in TOKEN_RE.findall(left.lower())},
        {normalize_token(t) for t in TOKEN_RE.findall(right.lower())},
    )


def claim_keys(node: Node) -> set[str]:
    keys: set[str] = set()
    for claim in node.claims:
        normalized = normalize_text(claim)
        if normalized:
            keys.add(normalized)
    return keys


def match_score(old: Node, new: Node) -> float:
    """Revision-match score in [0, 1]: how likely `new` is a revision of `old`."""
    claim_score = jaccard(claim_keys(old), claim_keys(new))
    keyword_score = jaccard(
        {normalize_token(k) for k in old.keywords},
        {normalize_token(k) for k in new.keywords},
    )
    body_score = token_jaccard(old.body, new.body)
    entity_bonus = 0.0
    if old.entity and new.entity:
        entity_bonus = (
            0.2 if normalize_text(old.entity) == normalize_text(new.entity) else 0.0
        )
    return min(
        1.0, max(claim_score, keyword_score * 0.8, body_score * 0.65) + entity_bonus
    )


def claims_equivalent(old: Node, new: Node, unchanged_threshold: float = 0.9) -> bool:
    """True when old/new carry the same facts (reorder, not a real change)."""
    old_claims = claim_keys(old)
    new_claims = claim_keys(new)
    if old_claims and new_claims:
        return jaccard(old_claims, new_claims) >= unchanged_threshold
    return token_jaccard(old.body, new.body) >= 0.95


# pure formatters
def node_ref(node: Node) -> dict[str, str]:
    return {"id": node.id, "title": node.title or node.entity or node.id}


def dedupe(ids: list[str]) -> list[str]:
    seen: list[str] = []
    for node_id in ids:
        if node_id not in seen:
            seen.append(node_id)
    return seen


def clean_node_ref(value: str) -> str:
    """Strip the decoration an LLM tends to add around a node id (bullets,
    backticks, 'id:' prefix, a copied table row, a trailing '(Title)')."""
    text = str(value or "").strip()
    text = re.sub(r"^\s*[-*]\s*", "", text).strip()
    text = text.strip("`'\" \t\r\n")
    text = re.sub(r"^id\s*:\s*", "", text, flags=re.IGNORECASE).strip()
    if "|" in text:
        text = text.split("|", 1)[0].strip()
    text = re.sub(r"\s+\([^)]*\)\s*$", "", text).strip()
    return text.strip("`'\" \t\r\n,;")


def format_node_full(node: Node | None, requested_id: str, cleaned_id: str) -> str:
    if not node:
        return f"node not found\nrequested_id: {requested_id}\ncleaned_id: {cleaned_id}"
    note = ""
    if requested_id.strip() != cleaned_id:
        note = f"requested_id: {requested_id}\ncleaned_id: {cleaned_id}\n"
    return f"{note}id: {node.id}\ntitle: {node.title}\nsummary: {node.summary}\nbody:\n{node.body}"


# mermaid validate / repair (a real subsystem; not inlined)
def validate_mermaid(code: str, settings: Settings) -> tuple[bool, str]:
    """Render the code to SVG with mmdc; True if it parses + renders."""
    mmdc = shutil.which(settings.mermaid_cli_bin)
    if mmdc is None:
        return False, f"mermaid CLI '{settings.mermaid_cli_bin}' not found in PATH"
    config = Path(settings.mermaid_puppeteer_config).expanduser()
    cmd_prefix = [mmdc] + (["-p", str(config)] if config.exists() else [])
    with tempfile.TemporaryDirectory() as tmp:
        in_file = Path(tmp) / "d.mmd"
        out_file = Path(tmp) / "d.svg"
        in_file.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [*cmd_prefix, "-i", str(in_file), "-o", str(out_file)],
                capture_output=True,
                timeout=settings.mermaid_render_timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"mmdc timed out after {settings.mermaid_render_timeout}s"
        except Exception as exc:  # noqa: BLE001 - mmdc failed to start
            return False, f"mmdc failed to start: {exc}"
        if proc.returncode == 0 and out_file.exists() and out_file.stat().st_size > 0:
            return True, ""
        return False, proc.stderr.decode("utf-8", errors="replace").strip()


def repair_answer_mermaid(
    answer: str, llm: LlmClient, settings: Settings, emit: Callable[[dict], None]
) -> str:
    """Validate + repair every mermaid block in `answer`, emitting progress events."""
    blocks = list(_MERMAID_BLOCK_RE.finditer(answer))
    if not blocks:
        return answer
    if shutil.which(settings.mermaid_cli_bin) is None:
        emit({"type": "diagram_skipped", "reason": "mermaid CLI not installed"})
        return answer
    emit({"type": "diagram_pending"})

    new_answer = answer
    fixed_codes: list[str] = []
    all_ok = True
    for match in blocks:
        code = match.group("code").strip()
        ok, error = validate_mermaid(code, settings)
        attempt = 0
        while not ok and attempt < settings.mermaid_repair_attempts:
            attempt += 1
            user = (
                "The following Mermaid diagram does not render. Fix the syntax, preserving "
                "all nodes, edges, directions, and labels. Use simple ASCII node IDs and "
                "quoted labels for any spaces/punctuation/long text.\n\n"
                f"Render error:\n{error}\n\n```mermaid\n{code}\n```"
            )
            try:
                response = llm.complete(MERMAID_FIX_SYSTEM, user)
            except Exception:  # noqa: BLE001 - repair is best-effort
                break
            fix = _MERMAID_BLOCK_RE.search(response)
            repaired = (
                fix.group("code").strip()
                if fix
                else response.strip().strip("`").strip()
            )
            if not repaired:
                break
            code = repaired
            ok, error = validate_mermaid(code, settings)
        all_ok = all_ok and ok
        fixed_codes.append(code)
        new_answer = new_answer.replace(match.group(0), f"```mermaid\n{code}\n```", 1)

    emit(
        {
            "type": "diagram_ready" if all_ok else "diagram_failed",
            "answer": new_answer,
            **({"mermaid": fixed_codes} if all_ok else {}),
        }
    )
    return new_answer


# TODO: Make this inline
def item_vec_weight(settings: Settings, field: str) -> float:
    return {
        "title": settings.weight_title_vec,
        "claim": settings.weight_claim_vec,
        "small_chunk": settings.weight_small_chunk_vec,
        "summary": settings.weight_summary_vec,
        "big_chunk": settings.weight_big_chunk_vec,
    }.get(field, settings.weight_small_chunk_vec)


# For ranking purposes
def normalize_scores(values: list[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [1.0 for _ in values]
    return [(v - low) / (high - low) for v in values]


# Greedy Maximal Marginal Relevance (MMR) ordering.
# At each step, selects the next item that best balances:
# - relevance score (rel)
# - diversity from already selected items (penalized by token overlap)
# Produces an ordering that avoids redundancy while keeping highly relevant texts early.
# TODO: Make this inline
def mmr_order(texts: list[str], rel: list[float], lam: float) -> list[int]:
    """Greedy MMR ordering: lam*rel - (1-lam)*max_sim_to_selected (token overlap)."""
    remaining = set(range(len(texts)))
    order: list[int] = []
    while remaining:
        best_idx, best_score = None, float("-inf")
        for i in remaining:
            sim = max((token_jaccard(texts[i], texts[j]) for j in order), default=0.0)
            score = lam * rel[i] - (1.0 - lam) * sim
            if score > best_score:
                best_score, best_idx = score, i
        order.append(best_idx)
        remaining.discard(best_idx)
    return order


def node_snippet(node: Node) -> str:
    return (node.summary or node.title or "").strip()


# Aggregates evidence hits per field and keeps only the best (lowest) rank for each field.
# Returns a rank-ordered list showing which fields matched the node most strongly (earlier rank = stronger match).
def evidence_why(node_hits: list[EvidenceHit]) -> list[dict[str, Any]]:
    """Best (lowest) rank per field that matched this node, rank-ascending."""
    best: dict[str, int] = {}
    for hit in node_hits:
        if hit.field not in best or hit.rank < best[hit.field]:
            best[hit.field] = hit.rank
    return [
        {"field": field, "rank": rank}
        for field, rank in sorted(best.items(), key=lambda kv: kv[1])
    ]


# Formats a candidate node result into a compact, llm-readable summary string
# for inspection/debugging. Includes node metadata, top match reasons ("why"),
# a few evidence snippets, and a suggested next action for downstream exploration.
def format_lead_candidate(result: dict[str, Any]) -> str:
    node = result["node"]
    why = result.get("why", [])
    why_str = ", ".join(f"{w['field']}#{w['rank']}" for w in why[:4]) or "n/a"
    lines = (
        f"- node_id: `{node.id}`\n"
        f"  title: {node.title}\n"
        f"  summary: {node.summary}\n"
        f"  why_matched: {why_str}"
    )
    evidence_lines: list[str] = []
    for ev in result.get("evidence", [])[:5]:
        snippet = " ".join((ev.get("text") or "").split())
        if snippet:
            evidence_lines.append(f"  - [{ev['field']}] {snippet[:700]}")
    evidence_text = (
        "\n".join(evidence_lines) if evidence_lines else "  - no evidence snippets"
    )
    lines += (
        f"\n  evidence:\n{evidence_text}\n"
        f"  next_action: if relevant, call explore(node_ids=['{node.id}']) "
        "or include this id with other candidates"
    )
    return lines


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


def sanitize_text(value: str) -> str:
    """Apply shared image/base64 stripping before text reaches an LLM."""
    return strip_image_media(value)


def sanitize_messages(messages: list[Any]) -> list[Any]:
    """Sanitize LangChain/OpenAI-style messages before model calls."""
    return [_sanitize_message(message) for message in messages]


def sanitize_tool_output(text: Any) -> str:
    """Sanitize tool observations before they become model-visible messages."""
    if not isinstance(text, str):
        text = str(text)
    return sanitize_text(text)


def _looks_like_image_data_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip().lower()
    return stripped.startswith("data:image/") or stripped.startswith(
        "data:application/octet-stream;base64,"
    )


def _sanitize_content(content: Any) -> Any:
    if isinstance(content, str):
        return sanitize_text(content)

    if isinstance(content, list):
        sanitized_items: list[Any] = []
        for item in content:
            if isinstance(item, dict):
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in _IMAGE_BLOCK_TYPES:
                    continue

                if any(key in item for key in _IMAGE_KEYS):
                    text_value = item.get("text")
                    if isinstance(text_value, str):
                        sanitized_text = sanitize_text(text_value).strip()
                        if sanitized_text:
                            sanitized_items.append(
                                {"type": "text", "text": sanitized_text}
                            )
                    continue

            sanitized_item = _sanitize_content(item)
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
            if key_lower in _IMAGE_KEYS or _looks_like_image_data_url(value):
                continue

            sanitized_value = _sanitize_content(value)
            if sanitized_value not in ({}, [], "", None):
                sanitized[key] = sanitized_value
        return sanitized

    return content


def _sanitize_message(message: Any) -> Any:
    if isinstance(message, dict):
        entry = dict(message)
        if "content" in entry:
            entry["content"] = _sanitize_content(entry.get("content"))
        return entry

    content = getattr(message, "content", None)
    if content is None:
        return message

    sanitized_content = _sanitize_content(content)
    if hasattr(message, "model_copy"):
        return message.model_copy(update={"content": sanitized_content})

    try:
        cloned = copy.copy(message)
        cloned.content = sanitized_content
        return cloned
    except Exception:
        return message


# endregion Helpers/Utils
