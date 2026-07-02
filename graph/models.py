# delayed type evaluation, helps with circular imports etc, or refering to a class before its definition is complete
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field
from typing import Callable, ContextManager, Protocol, runtime_checkable
from dataclasses import dataclass, field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#  settings 
@dataclass
class Settings:

    # For Chat UI
    chat_base_url: str = "http://10.160.144.101:51021/v1"
    chat_api_key: str = (
        "<API_KEY>"
    )
    chat_model: str = "openai/gpt-oss-120b"
    chat_temperature: float = 0.4
    agent_max_steps: int = 40
    agent_patience: int = 20

    # ask() early-exit routing: reuse an existing agent note or answer with
    # shallow RAG when the graph already covers the question; else deep research
    agent_early_exit: bool = True
    early_exit_candidates: int = 8
    shallow_answer_max_nodes: int = 6

    # Compile time/ingest time 

    # embeddings
    embed_backend: str = "server"
    embed_base_url: str = "http://localhost:8081/v1"
    embed_api_key: str = "local"
    embed_model: str = "cl-nagoya/ruri-v3-310m"
    hf_embed_model: str = "cl-nagoya/ruri-v3-310m"
    hf_device: str = "cuda:0" # fallback device id if server fails
    embed_dim: int = 768

    # reranker
    rerank_backend: str = "server"
    rerank_base_url: str = "http://localhost:8082/v1"
    rerank_api_key: str = "local"
    rerank_model: str = "cl-nagoya/ruri-v3-reranker-310m"
    hf_rerank_model: str = "cl-nagoya/ruri-v3-reranker-310m"
    rerank_device: str = "cuda:0"

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
    subagent_count: int = 3
    subagent_concurrency: int = 3
    subagent_max_steps: int = 20
    subagent_min_reads: int = 5
    subagent_max_reads: int = 10

    enable_mermaid: bool = True
    mermaid_repair_attempts: int = 3
    mermaid_cli_bin: str = "mmdc"
    mermaid_puppeteer_config: str = "/home/seigyo/llm-wiki/puppeteer-config.json"
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
class RouteDecision(BaseModel):
    mode: str = "deep"  # "reuse" | "shallow" | "deep"
    node_id: str | None = None
    reason: str = ""


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
    kind: str  # "summary" | "entity_dedup" | "cascade" | "maybe_recluster"
    node_id: str | None = None
    replacements: dict[str, str] = field(default_factory=dict)
    stale_sources: list[str] = field(default_factory=list)
