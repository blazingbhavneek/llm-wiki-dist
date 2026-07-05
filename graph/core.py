"""Core shelf for the graph package: data models, settings, prompt constants,
and pure helper functions. Imports nothing from the other graph modules —
every actor (store / gateway / librarian / researcher) builds on this."""

from __future__ import annotations


import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#  settings 
@dataclass
class Settings:

    # For Chat UI
    chat_base_url: str = "http://localhost:8080/v1"
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

    # API service concurrency (semaphores in ReadGraphService)
    service_max_reads: int = 16
    service_max_agents: int = 16

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
            service_max_reads=int(
                env("WIKI_SERVICE_MAX_READS", cls.service_max_reads)
            ),
            service_max_agents=int(
                env("WIKI_SERVICE_MAX_AGENTS", cls.service_max_agents)
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

# Durable discoveries extracted from raw transcripts
class ExtractedDiscovery(BaseModel):
    title: str
    body: str

class DiscoveryExtraction(BaseModel):
    discoveries: list[ExtractedDiscovery] = Field(default_factory=list)

# Whether two entities are same or not
class EntityMatch(BaseModel):
    is_same: bool = False
    target_node_id: str | None = None


# Early-exit routing decision for ask(): reuse an existing agent note verbatim,
# answer shallowly from retrieved evidence, or run the deep research agent.
from enum import Enum
from pydantic import BaseModel, Field


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
    """テキストクエリに一致するノードをWikiから検索します。"""

    text: str = Field(description="検索するキーワード")


class read(BaseModel):
    """IDを指定してノードの本文全体とメタデータを読みます。"""

    node_id: str = Field(description="読むノードのID")


class follow_link(BaseModel):
    """ノードからエッジをたどり、隣接ノードへ移動します。"""

    node_id: str = Field(description="展開するノードのID")
    direction: str = Field(
        default="both", description="'incoming'、'outgoing'、または 'both'"
    )


class explore(BaseModel):
    """重複しない開始ノードIDを探索サブエージェントのチームに渡します。"""

    node_ids: list[str] = Field(
        default_factory=list, description="重複しない開始ノードID"
    )


class finish(BaseModel):
    """最終回答と、根拠として使用したノードIDを提出します。"""

    answer: str = Field(description="ノード本文に根拠を持つ最終回答")
    cited_node_ids: list[str] = Field(
        default_factory=list, description="回答を裏付けるノードID"
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


# =============================================================================
# Prompt constants
# =============================================================================

"""Prompt constants used by graph services and agents."""

GRAPH_SYSTEM_PROMPT = "あなたは、簡潔で事実に基づくナレッジグラフWikiを維持します。"

SUMMARY_PROMPT = (
    "このMarkdownノードをナレッジグラフ用に要約してください。本文に存在する事実のみを使用してください。"
    "1〜3文に収めてください。前置きは不要です。"
    "本文が質問への回答、手順、規則、方針、判断基準、変更方法、原因説明、条件、例外、"
    "期限、責任者、数値、対象範囲を含む場合は、それらの具体情報を優先して残してください。"
    "抽象的な言い換えで、文書名、節名、項目名、対象、条件、値、日付、役割、例外を失わないでください。"
    "本文に存在しない事実を推論したり追加したりしないでください。"
)

KEYWORD_PROMPT = (
    "グラフ検索用に、このテキストから重要なキーワードやエンティティを抽出してください："
    "文書名、制度名、規則名、項目名、見出し、対象名、組織名、役割名、担当者、場所、"
    "日付、期限、金額、数値、条件、例外、手順名、状態名、原因、結果、固有名詞、専門用語など。"
    "重複しないキーワードを、重要度の高い順に最大12個返してください。"
    "ユーザーの質問や本文が変更方法・対応方法・判断・原因・手順・条件に関する場合は、"
    "対象、必要な行動、条件、例外、判断基準、変更前後の状態を表す語を優先してください。"
    "頭字語または識別子でない限り、小文字にしてください。"
)

CLAIM_PROMPT = (
    "リビジョン照合のために、このMarkdownノードから安定した識別情報となる事実を抽出してください。"
    "1つの主要なエンティティまたはトピックと、最大20個の原子的な主張を返してください。"
    "主張は、本文によって直接裏付けられる短い事実文でなければなりません。"
    "元文書の順序が入れ替わっても認識可能な事実を優先してください。"
    "本文が規則、手順、方針、判断基準、変更方法、原因説明、条件、例外、期限、責任、"
    "数値、対象範囲を含む場合は、対象、条件、行動、結果、例外、日付、値、役割の関係を"
    "優先して抽出してください。"
    "本文に存在しない事実を推論したり追加したりしないでください。"
)

DISCOVERY_PROMPT = (
    "あなたは開発者のコーディングエージェントが残した生の作業記録（トランスクリプト）を読みます。"
    "恒久的で再利用可能な発見のみを抽出してください：根本原因、不変条件、落とし穴、"
    "理由を伴う自明でない意思決定。\n"
    "ルール：\n"
    "- ほとんどのトランスクリプトには恒久的な内容は何も含まれていません。その場合は空のリストを返してください。\n"
    "- 経過の説明、計画、TODOのような雑談、コードを読めば明らかな事実は決して抽出しないでください。\n"
    "- 各発見は単体で理解できなければなりません（タイトルと、関係するファイル・シンボル・コミットを"
    "本文中に明記した本文）。"
)

ROUTER_PROMPT = (
    "あなたはナレッジグラフWikiの質問ルーターです。質問と、検索で見つかった候補ノード"
    "（kind='agent_note' は過去にエージェントが作成した回答ノート、kind='source' は元資料）"
    "が与えられます。最も安価で十分な戦略を1つ選んでください。\n\n"

    "選択肢：\n"
    "- reuse: 候補内の agent_note が、質問と同じ範囲をカバーし、質問が求める具体性まで"
    "ほぼ完全に回答している場合。その候補の node_id を正確に返してください。"
    "回答はそのノート本文をそのまま再利用します。\n"
    "- shallow: 質問が狭く、候補の evidence 抜粋だけで正確かつ十分に回答できる場合。"
    "node_id は null にしてください。\n"
    "- deep: 質問が広い、複数箇所の文書確認が必要、候補同士が矛盾する、"
    "解釈・手順・変更・判断・原因・条件整理が必要、または証拠が不十分な場合。"
    "node_id は null にしてください。\n\n"

    "重要ルール：\n"
    "- 質問の短さだけで shallow を選んではいけません。短い質問でも、"
    "変更方法、対応方針、原因、理由、手順、例外条件、判断基準、複数条件の整理、"
    "文書間の関係確認が必要なら deep を選んでください。\n"
    "- ただし、質問が単一の事実を尋ねており、候補 evidence だけで直接回答できる場合は shallow を選んでください。"
    "例：期限、日付、担当者、金額、数値、名称、定義、単一条件、単一の記載箇所にある事実。\n"
    "- ユーザーが「何を変更」「どこを変更」「どうすれば」「なぜ」「どの手順」"
    "「どの条件」「何が必要」「何を確認」「どう対応」「どれを選ぶ」"
    "のように、行動・判断・解釈・変更・原因・手順を尋ねている場合は、原則 deep を選んでください。\n"
    "- そのような質問で shallow を選んでよいのは、候補 evidence だけで次のすべてが明確に分かる場合です："
    "対象、必要な行動または回答、根拠、条件、例外や制約がある場合はそれら。\n"
    "- agent_note の再利用は、単に関連しているだけでは選んではいけません。"
    "質問が求める範囲と具体性を満たしている場合のみ reuse を選んでください。\n"
    "- kind='agent_note' の最上位候補が質問に直接かつ十分な具体性で回答している場合は reuse を選んでください。"
    "ただし、回答が概念的・抽象的で、対象・条件・手順・根拠が不足している場合は reuse ではなく deep を選んでください。\n"
    "- reuse を選ぶ場合、node_id は候補に含まれる id をそのまま正確にコピーしてください。\n"
    "- reuse では、node_id は必須です。\n"
    "- shallow/deep では、node_id は null にしてください。\n"
    "- 部分的な一致だけなら reuse ではなく shallow または deep を選んでください。\n"
    "- 確信がない場合は deep を選んでください。不正確な近道より深い調査を優先します。\n"
    "- reason には判断理由を短い1文で必ず書いてください。空文字は禁止です。"
)

SHALLOW_ANSWER_PROMPT = (
    "与えられたノード抜粋のみを根拠として、質問に簡潔に回答してください。\n"
    "- 質問と同じ言語で回答してください。\n"
    "- 抜粋に存在しない事実を追加・推論しないでください。\n"
    "- 回答はMarkdown形式で書いてください。\n"
    "- 根拠に使ったノードのidを、本文中でバッククォート付きで引用してください（例：`node:...`）。\n"
    "- 抜粋だけでは回答できない場合は、その旨を明確に述べてください。\n\n"

    "shallow 回答に適したケース：\n"
    "- 質問が単一の事実を尋ねており、抜粋内に直接答えがある場合。"
    "例：期限、日付、担当者、金額、数値、名称、定義、単一条件、単一の記載事実。\n\n"

    "注意が必要なケース：\n"
    "- ユーザーが「何を変更」「どこを変更」「どうすれば」「なぜ」「どの手順」"
    "「どの条件」「何が必要」「何を確認」「どう対応」「どれを選ぶ」"
    "のように、行動・判断・解釈・変更・原因・手順を尋ねている場合、"
    "抜粋内の一文だけを一般化して答えてはいけません。\n"
    "- その場合、抜粋内に存在する範囲で、対象、必要な行動、条件、根拠、例外、注意点を明確にしてください。\n"
    "- 抜粋に十分な情報がない場合は、"
    "『抜粋だけでは具体的な回答に必要な情報が不足しています』と明確に述べ、"
    "分かっている事実だけを根拠付きで説明してください。\n"
    "- 抽象的な一言だけで終えてはいけません。"
)

REGENERATE_EXOGENOUS_PROMPT = (
    "その根拠となるソース資料が変更された後に、派生Wikiノードを再生成してください。"
    "以前の派生ノードは、意図されたトピックと構成を理解するためだけに使用してください。"
    "新しいノード本文は、現在のサポート資料のみによって裏付けられていなければなりません。"
    "もはや裏付けられていない古い主張は削除してください。"
    "結果は簡潔で、事実に基づき、Markdown形式にしてください。前置きは不要です。"
)

EDGE_PROMPT = (
    "あなたはWikiグラフを維持します。新しいノードと、既存ノードの候補リスト"
    "（意味的類似性によって事前にフィルタ済み）が与えられたら、"
    "新しいノードがどの候補にリンクすべきか、またその理由を判断してください。\n"
    "ルール：\n"
    "- 与えられた候補IDのみを使用してください。\n"
    "- ラベルは、ターゲットが新しいノードにどのように関連するかを説明する短い動詞句です"
    "（例：'uses', 'defines', 'example-of', 'prerequisite-for', 'contradicts'）。\n"
    "- 関係が明確に有用な場合のみエッジを提案してください。弱い関係は省略してください。\n"
    "- summary：リンクを説明する短い節を1つ書いてください。"
)

ENTITY_DEDUP_PROMPT = (
    "あなたはWikiグラフを維持します。新しいノードと既存ノードの候補リストが与えられたら、"
    "新しいノードが、候補のうち厳密に1つと同じ現実世界のエンティティまたはトピックを"
    "記述しているかどうかを判断してください。\n"
    "ルール：\n"
    "- 同じエンティティとは、同じ具体的なもの"
    "（同じAPI、同じツール、同じ概念）を意味し、単に関連している、または類似している"
    "トピックではありません。\n"
    "- 保守的に判断してください。不確かな場合は is_same=false と答えてください。"
    "異なるものを指す同音異義語を決して統合しないでください。\n"
    "- 一致するものがある場合は、is_same=true とその候補の target_node_id を返してください。"
    "それ以外の場合は is_same=false と target_node_id=null を返してください。"
)

CLUSTER_NAMER_SYSTEM = (
    "あなたはナレッジグラフ内の1つのトピッククラスタに名前を付けます。"
    "必ず日本語でトピック名を返してください。"
    "英語のクラスタ名は使用しないでください。ただし、API名、関数名、ライブラリ名、"
    "製品名、頭字語、識別子など、翻訳すべきでない技術用語はそのまま残してかまいません。"
    "すでに使用されている他の名前と区別できる、具体的な名前を選んでください。"
    "キーワードとサンプルのセクションタイトルに見られる、最も具体的な技術的サブトピックを"
    "優先してください。"
    "より狭いトピックが存在する場合は、CUDA、SYCL、OpenMP、oneAPI のような"
    "広範なソース名だけを名前にすることは避けてください。"
    "最大4語程度の簡潔な日本語トピック名を1つだけ返してください。"
    "引用符、句読点、説明は不要です。"
)

MAIN_AGENT_SYSTEM_PROMPT = (
    "あなたは、ナレッジグラフWikiからの質問に答える主任研究者です。"
    "あなたは調整役であり、自分でノードを読むことはありません。\n\n"
    "あなたには2つの能力があります：\n"
    "- search(text)：候補ノードを検索します"
    "（関連性によってすでに再ランキング済み）。ノードID、タイトル、要約のみを返します。\n"
    "- explore(node_ids)：重複しない開始ノードIDのリストをサブエージェントチームに渡します。"
    "各サブエージェントは1つの開始ノードからグラフを読み進め、調査結果を報告します。"
    "有望な手がかりを調査するために使用してください。\n"
    "- finish(answer, cited_node_ids)：最終的にまとめた回答を提出します。\n\n"

    "作業手順：\n"
    "1. まず主質問を検索し、その後、質問内の重要語、固有名詞、文書名、制度名、対象名、"
    "条件、役割、日付、数値、手順名、判断基準らしき語を検索してください。"
    "グラフの異なる部分を浮かび上がらせるために、いくつか検索を行ってください。\n"
    "2. すべての候補から、異なるサブトピックをカバーする最良の重複しない開始ノードを選んでください"
    "（近い重複を避け、サブエージェントが異なるサブグラフを探索できるようにしてください）。\n"
    "3. それらの開始ノードを指定して explore(node_ids) を1回呼び出してください。"
    "チームは並列に探索し、各サブエージェントの調査結果と読んだノード本文を返します。\n"
    "4. サブエージェントの報告を読んでください。明確な不足が残っている場合は、"
    "再度検索して explore をもう1回実行してもかまいません。そうでなければ回答をまとめてください。\n"
    "5. サブエージェントが報告した内容のみに基づいた十分な回答を、"
    "証拠として使用したノードIDを引用しながら finish で提出してください。\n\n"

    "深く調べるべき質問の方針：\n"
    "- ユーザーが行動、変更、対応、判断、原因、理由、手順、条件、例外、影響、比較、"
    "複数文書の整合性を尋ねている場合、概念的な答えだけで finish してはいけません。\n"
    "- その場合は、可能な限り次を特定してください："
    "対象、該当する文書・節・規則・項目、必要な行動、満たすべき条件、判断基準、"
    "変更前後の状態、例外、注意点、根拠。\n"
    "- 質問が単一の事実だけを尋ねている場合は、必要以上に広げず、直接回答してください。"
    "例：期限、日付、担当者、金額、数値、名称、定義、単一条件。\n"
    "- ただし、単一の事実に見えても、候補同士が矛盾する場合、条件付きの場合、"
    "文脈により答えが変わる場合は、追加検索や追加 explore を検討してください。\n"
    "- サブエージェントの報告に具体的な対象、条件、根拠が不足している場合は、"
    "追加検索や追加 explore を検討してください。\n"
    "- それでも具体的な情報が見つからない場合は、見つかった事実と不足している情報を明確に分けて回答してください。"
    "根拠がない文書名、項目名、条件、値、手順を推測してはいけません。\n\n"

    "ルール：\n"
    "- あなたはノード本文を直接読むことはできません。サブエージェントの報告に依存してください。\n"
    "- 'node:' プレフィックスを含め、ノードIDは表示されたとおり正確にコピーしてください。\n"
    "- 少なくとも1回 explore を実行する前に finish してはいけません。\n"
    "- 幅広さを優先してください。同じ内容に関する複数のIDではなく、"
    "異なる開始点を explore に渡してください。\n"
    "- 回答はユーザーの質問に直接答えてください。関連情報の要約だけで終えてはいけません。"
)

SUBAGENT_SYSTEM_PROMPT = (
    "あなたは、ナレッジグラフWikiの1つの領域を探索する研究サブエージェントです。"
    "主任研究者から開始ノードを与えられています。それを徹底的に調査し、"
    "具体的で根拠のある発見を報告してください。\n\n"

    "ツール：\n"
    "- read(node_id)：ノードの完全な本文を読みます。割り当てられたノードを読むことから開始してください。\n"
    "- follow_link(node_id, direction)：参照、例、前提条件、関連概念をたどるために、"
    "ノードの近傍へ移動します。\n"
    "- search(text)：自分の領域で必要な場合、キーワードで追加のノードを検索します。\n"
    "- finish(answer, cited_node_ids)：調査結果と読んだノードIDを報告します。\n\n"

    "ルール：\n"
    "1. 割り当てられた開始ノードを最初に読んでください。\n"
    "2. リンクをたどり、自分の領域内で2〜5個のノードを読んで、実際の証拠を集めてください。"
    "ただし、質問に答えるための対象・条件・根拠・例外・手順が不足している場合は、"
    "必要に応じて追加検索やリンク追跡を行ってください。\n"
    "3. 自分の担当範囲に留まってください。タスクに列挙された兄弟開始ノードは"
    "他のサブエージェントが担当します。それらを再探索せず、自分のサブグラフに集中して、"
    "チーム全体でより広い範囲をカバーできるようにしてください。\n"
    "4. 実際に読んだノード本文のみに基づいて報告してください。\n"
    "5. 'node:' プレフィックスを含め、ノードIDは正確にコピーしてください。\n"
    "6. 完了したら、この領域が質問について何を示しているかを焦点を絞って要約し、"
    "使用したノードIDを引用して finish を呼び出してください。\n\n"

    "調査時の観点：\n"
    "- 質問が単一の事実を尋ねている場合は、その事実と根拠を正確に報告してください。"
    "例：期限、日付、担当者、金額、数値、名称、定義、単一条件。\n"
    "- 質問が行動、変更、対応、判断、原因、理由、手順、条件、例外、影響、比較を尋ねている場合は、"
    "抽象的な概念だけで報告してはいけません。\n"
    "- その場合は、対象、該当する文書・節・規則・項目、必要な行動、満たすべき条件、"
    "判断基準、変更前後の状態、例外、注意点、根拠を探してください。\n"
    "- 読んだ本文にそれらが存在する場合は、具体名、条件、値、記述をそのまま報告してください。\n"
    "- 存在しない場合は、『この領域では具体的な情報は見つからなかった』と明確に報告し、"
    "見つかった関連事実だけを根拠付きで説明してください。\n"
    "- 根拠のない文書名、項目名、条件、値、手順を推測してはいけません。"
)

MERMAID_INSTRUCTION = (
    "\n\n図：\n"
    "- フローチャート、アーキテクチャ図/ブロック図、シーケンス図、またはデータフロー図が"
    "回答をより明確にする場合は、回答の一部として ```mermaid のフェンス付きブロック内に"
    "Mermaid図を1つ含めてください。\n"
    "- 本当に役立つ場合にのみ図を追加してください。そうでなければ省略してください。\n"
    "- シンプルなASCIIノードID（n1, n2, proc_a）を使用し、スペース、句読点、"
    '長いテキストは引用符付きラベル内に入れてください：n1["線形層"] --> n2["GEMM"]。\n'
    "- mermaid ブロックには Mermaid 構文のみを含めてください（説明文や箇条書きは不可）。"
)

MERMAID_FIX_SYSTEM = (
    "Mermaid図の構文を修正し、mermaid-cli（mmdc）で描画できるようにしてください。"
    "修正済みの ```mermaid フェンス付きコードブロックを1つだけ返し、それ以外は何も返さないでください。"
)

LEAD_AGENT_PROMPT = """
あなたは主任研究エージェントです。

使用できるツール：
- search(text)：候補ノードを検索します
- explore(node_ids)：指定したノードを調査するサブエージェントを派遣します
- finish(answer, cited_node_ids)：最終回答を提出します

ルール：
- 検索結果に含まれる正確なノードIDを優先して使用してください。
- ユーザーメッセージに候補ノードがすでに提示されている場合は、まずそれらを調査してください。
- 関連するノードIDがある場合は explore(...) を使用してください。
- キーワードをわずかに変えただけの検索を延々と繰り返さないでください。
- 簡潔な回答で finish し、根拠となるノードIDを引用してください。
""".strip()


# =============================================================================
# Pure helpers (formatters, hashing, matching, mermaid repair)
# =============================================================================


import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

import hashlib



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
        return f"ノードが見つかりません\nrequested_id: {requested_id}\ncleaned_id: {cleaned_id}"
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
        "\n".join(evidence_lines) if evidence_lines else "  - 証拠抜粋なし"
    )
    lines += (
        f"\n  evidence:\n{evidence_text}\n"
        f"  next_action: 関連する場合は explore(node_ids=['{node.id}']) を呼び出すか、"
        "他の候補と一緒にこのIDを含めてください"
    )
    return lines


# =============================================================================
# LLM input sanitizers (image/base64 stripping for model-bound text)
# =============================================================================


import copy
import re
from typing import Any


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
