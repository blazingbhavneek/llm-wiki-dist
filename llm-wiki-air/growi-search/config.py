"""Search-service settings: GROWI, LLM, reranker, limits. No DB/ingest settings."""

from __future__ import annotations

import configparser
import os
import shutil
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel


def _load_env_files() -> None:
    # load_dotenv does not overwrite already-set variables, so service-local wins.
    service_dir = Path(__file__).resolve().parent
    load_dotenv(service_dir / ".env", override=False)
    load_dotenv(service_dir.parent / ".env", override=False)


def _project_values() -> dict[str, str]:
    """Read the small subset this service needs from configs/<WIKI_PROJECT>.ini."""
    selector = (os.environ.get("WIKI_PROJECT") or "").strip()
    if not selector:
        return {}
    path = Path(selector).expanduser()
    if not path.is_absolute():
        if path.name != selector or path.suffix not in ("", ".ini"):
            raise ValueError("WIKI_PROJECT must be a configs/ name or an absolute .ini path")
        path = Path(__file__).resolve().parent.parent / "configs" / f"{path.stem}.ini"
    parser = configparser.ConfigParser(interpolation=None)
    if path.suffix != ".ini" or not parser.read(path, encoding="utf-8"):
        raise ValueError(f"project config not found: {path}")
    project = dict(parser.items("project"))
    if parser.has_section("settings"):
        project.update(parser.items("settings"))
    return project


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _clamp_float(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    # GROWI
    growi_url: str = ""
    growi_token: str = ""
    growi_attachment_token: str = ""
    growi_root_path: str = "/"
    growi_timeout: float = 30.0

    # Chat LLM
    chat_base_url: str = ""
    chat_api_key: str = "local"
    chat_model: str = "gemma-4-31B"
    chat_temperature: float = 0.2

    # Reranker (optional; ES order preserved when unavailable)
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = ""
    rerank_timeout: int = 15

    # Embeddings for the index map (optional; keyword overlap is used when absent)
    embed_base_url: str = ""
    embed_model: str = ""
    embed_api_key: str = "local"

    # Jev relevance gate (optional; ES/router path is used when disabled)
    jev_enabled: bool = False
    jev_backend: str = "auto"          # auto | local | hosted
    jev_local_path: str = ""           # directory containing jev_style_decision.py
    jev_device: str = "auto"           # auto | cuda | mps | cpu for the local runtime
    jev_dtype: str = "bfloat16"        # float32 | bfloat16 | float16 (bf16 falls back to fp16)
    jev_base_url: str = ""             # hosted Jev-compatible /score endpoint
    jev_api_key: str = ""
    jev_model: str = "chaoliangUNSW/Jev-Style-0.8B-Decision-v3"
    jev_timeout: int = 60
    jev_threshold: float = 0.50
    jev_seed_threshold: float = 0.80
    jev_chunk_tokens: int = 25600
    jev_chunk_overlap: int = 10000
    jev_batch_size: int = 64
    jev_max_page_reads: int = 0        # 0 = unlimited; independent of RunBudget
    jev_max_list_calls: int = 0        # 0 = unlimited
    jev_workers: int = 4               # concurrent fetch+classify threads in the sweep pipeline
    jev_subagent_group_size: int = 5   # max seeds one seed-group subagent explores
    jev_subagent_groups: int = 8       # max seed-group subagents per question
    jev_prefilter_min_overlap: int = 2  # shared keyword grams needed before an expensive body score; 0 = off

    # Index pages published by `main.py index`
    index_page_name: str = "00-目次"
    index_cache_ttl: int = 600
    index_map_top_k: int = 20
    index_map_embed_k: int = 60

    # Retrieval limits
    search_candidates: int = 30
    rerank_top_k: int = 8
    shallow_page_reads: int = 2
    evidence_per_page: int = 2
    link_expand_limit: int = 12
    max_page_fetches_per_run: int = 12
    max_search_calls_per_run: int = 6
    growi_concurrency: int = 6
    page_cache_ttl: int = 120
    page_cache_max: int = 256

    # Service concurrency / agents
    service_max_reads: int = 16
    service_max_agents: int = 4
    agent_max_steps: int = 40
    agent_patience: int = 20
    subagent_count: int = 2
    subagent_concurrency: int = 2
    subagent_max_steps: int = 20
    subagent_min_reads: int = 1
    subagent_max_reads: int = 4

    # Presentation / deployment
    enable_mermaid: bool = False
    doc_parser_url: str = "/agent/doc-parser/"
    prefix: str = "/growi-search"
    allowed_llm_hosts: str = ""
    usage_log_path: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        _load_env_files()
        env = os.environ.get
        project = _project_values()

        token = (project.get("growi_token") or env("GROWI_TOKEN") or "").strip()
        # Tolerate a pasted "API Token: ..." value.
        if token.lower().startswith("api token:"):
            token = token.split(":", 1)[1].strip()

        # ponytail: whole-instance access; set GROWI_ROOT_PATH to scope manually.
        root = (env("GROWI_ROOT_PATH") or "/").strip() or "/"
        if not root.startswith("/"):
            root = "/" + root
        if len(root) > 1:
            root = root.rstrip("/")

        return cls(
            growi_url=(project.get("growi_url") or env("GROWI_URL") or "").rstrip("/"),
            growi_token=token,
            growi_attachment_token=(env("GROWI_ATTACHMENT_TOKEN") or token).strip(),
            growi_root_path=root,
            growi_timeout=float(env("GROWI_TIMEOUT") or 30),
            chat_base_url=(
                env("WIKI_CHAT_BASE_URL")
                or project.get("chat_base_url")
                or env("OPENAI_BASE_URL")
                or ""
            ).rstrip("/"),
            chat_api_key=(
                env("WIKI_CHAT_API_KEY") or env("OPENAI_API_KEY") or "local"
            ),
            chat_model=(env("WIKI_CHAT_MODEL") or env("WIKI_MODEL") or "gemma-4-31B"),
            chat_temperature=float(env("WIKI_CHAT_TEMPERATURE") or 0.2),
            rerank_base_url=(env("WIKI_RERANK_BASE_URL") or "").rstrip("/"),
            rerank_api_key=env("WIKI_RERANK_API_KEY") or "",
            rerank_model=env("WIKI_RERANK_MODEL") or "",
            rerank_timeout=int(env("WIKI_RERANK_TIMEOUT") or 15),
            embed_base_url=(env("WIKI_EMBED_BASE_URL") or "").rstrip("/"),
            embed_model=env("WIKI_EMBED_MODEL") or "",
            embed_api_key=env("WIKI_EMBED_API_KEY") or "local",
            jev_enabled=_bool(env("WIKI_JEV_ENABLED")),
            jev_backend=(env("WIKI_JEV_BACKEND") or "auto").strip().lower(),
            jev_local_path=(env("WIKI_JEV_LOCAL_PATH") or "").strip(),
            jev_device=(env("WIKI_JEV_DEVICE") or "auto").strip().lower(),
            jev_dtype=(env("WIKI_JEV_DTYPE") or "bfloat16").strip().lower(),
    jev_base_url=(env("WIKI_JEV_BASE_URL") or "").rstrip("/"),
            jev_api_key=(env("WIKI_JEV_API_KEY") or "").strip(),
            jev_model=env("WIKI_JEV_MODEL") or "chaoliangUNSW/Jev-Style-0.8B-Decision-v3",
            jev_timeout=int(env("WIKI_JEV_TIMEOUT") or 60),
            jev_threshold=_clamp_float(float(env("WIKI_JEV_THRESHOLD") or 0.5), 0.0, 1.0),
            jev_seed_threshold=_clamp_float(float(env("WIKI_JEV_SEED_THRESHOLD") or 0.8), 0.0, 1.0),
            jev_chunk_tokens=int(env("WIKI_JEV_CHUNK_TOKENS") or 25600),
            jev_chunk_overlap=int(env("WIKI_JEV_CHUNK_OVERLAP") or 10000),
            jev_batch_size=int(env("WIKI_JEV_BATCH_SIZE") or 64),
            jev_max_page_reads=int(env("WIKI_JEV_MAX_PAGE_READS") or 0),
            jev_max_list_calls=int(env("WIKI_JEV_MAX_LIST_CALLS") or 0),
            jev_workers=max(1, int(env("WIKI_JEV_WORKERS") or 4)),
            jev_subagent_group_size=max(1, int(env("WIKI_JEV_SUBAGENT_GROUP_SIZE") or 5)),
            jev_subagent_groups=max(1, int(env("WIKI_JEV_SUBAGENT_GROUPS") or 8)),
            jev_prefilter_min_overlap=max(0, int(env("WIKI_JEV_PREFILTER_MIN_OVERLAP") or 2)),
            index_page_name=env("WIKI_INDEX_PAGE_NAME") or "00-目次",
            index_cache_ttl=int(env("WIKI_INDEX_CACHE_TTL") or 600),
            index_map_top_k=int(env("WIKI_INDEX_MAP_TOP_K") or 20),
            index_map_embed_k=int(env("WIKI_INDEX_MAP_EMBED_K") or 60),
            search_candidates=_clamp(int(env("WIKI_SEARCH_CANDIDATES") or 30), 1, 50),
            rerank_top_k=_clamp(int(env("WIKI_RERANK_TOP_K") or 8), 1, 40),
            shallow_page_reads=_clamp(int(env("WIKI_SHALLOW_PAGE_READS") or 2), 1, 3),
            evidence_per_page=_clamp(int(env("WIKI_EVIDENCE_PER_PAGE") or 2), 1, 8),
            link_expand_limit=_clamp(int(env("WIKI_LINK_EXPAND_LIMIT") or 12), 1, 50),
            max_page_fetches_per_run=int(env("WIKI_MAX_PAGE_FETCHES_PER_RUN") or 12),
            max_search_calls_per_run=int(env("WIKI_MAX_SEARCH_CALLS_PER_RUN") or 6),
            growi_concurrency=_clamp(int(env("WIKI_GROWI_CONCURRENCY") or 6), 1, 32),
            page_cache_ttl=int(env("WIKI_PAGE_CACHE_TTL") or 120),
            page_cache_max=_clamp(int(env("WIKI_PAGE_CACHE_MAX") or 256), 1, 4096),
            service_max_reads=_clamp(int(env("WIKI_SERVICE_MAX_READS") or 16), 1, 128),
            service_max_agents=_clamp(int(env("WIKI_SERVICE_MAX_AGENTS") or 4), 1, 32),
            agent_max_steps=_clamp(int(env("WIKI_AGENT_MAX_STEPS") or 40), 5, 200),
            agent_patience=_clamp(int(env("WIKI_AGENT_PATIENCE") or 20), 1, 100),
            subagent_count=_clamp(int(env("WIKI_SUBAGENT_COUNT") or 2), 1, 8),
            subagent_concurrency=_clamp(int(env("WIKI_SUBAGENT_CONCURRENCY") or 2), 1, 8),
            subagent_max_steps=_clamp(int(env("WIKI_SUBAGENT_MAX_STEPS") or 20), 4, 100),
            subagent_min_reads=_clamp(int(env("WIKI_SUBAGENT_MIN_READS") or 1), 0, 10),
            subagent_max_reads=_clamp(int(env("WIKI_SUBAGENT_MAX_READS") or 4), 1, 20),
            enable_mermaid=(
                env("WIKI_ENABLE_MERMAID") or ("1" if shutil.which("mmdc") else "")
            ).lower() in {"1", "true", "yes", "on"},
            doc_parser_url=env("WIKI_DOC_PARSER_URL") or "/agent/doc-parser/",
            prefix=(env("WIKI_PREFIX") or "/growi-search").strip().rstrip("/"),
            allowed_llm_hosts=env("WIKI_ALLOWED_LLM_HOSTS") or "",
            usage_log_path=env("WIKI_USAGE_LOG_PATH") or "",
        )

    # --- validation ---------------------------------------------------------

    def validate_strict(self) -> None:
        """Startup validation. Raises ValueError on unusable configuration."""
        if not self.growi_url:
            raise ValueError("GROWI_URL is required")
        if not self.growi_token:
            raise ValueError("GROWI_TOKEN is required")
        if self.jev_seed_threshold < self.jev_threshold:
            raise ValueError("WIKI_JEV_SEED_THRESHOLD must be >= WIKI_JEV_THRESHOLD")
        # The chunker reserves 512 tokens for the prompt/metadata.
        if self.jev_chunk_tokens <= 512:
            raise ValueError("WIKI_JEV_CHUNK_TOKENS must be > 512")
        if not 0 <= self.jev_chunk_overlap < self.jev_chunk_tokens - 512:
            raise ValueError("WIKI_JEV_CHUNK_OVERLAP must fit the body token budget")
        if self.jev_batch_size < 1:
            raise ValueError("WIKI_JEV_BATCH_SIZE must be >= 1")
        if self.jev_max_page_reads < 0 or self.jev_max_list_calls < 0:
            raise ValueError("Jev budgets must be >= 0 (0 = unlimited)")
        if self.jev_workers < 1:
            raise ValueError("WIKI_JEV_WORKERS must be >= 1")
        if self.jev_subagent_group_size < 1 or self.jev_subagent_groups < 1:
            raise ValueError("Jev seed-group size and count must be >= 1")
        if self.jev_prefilter_min_overlap < 0:
            raise ValueError("WIKI_JEV_PREFILTER_MIN_OVERLAP must be >= 0 (0 = no prefilter)")

    @property
    def llm_ready(self) -> bool:
        return bool(self.chat_base_url and self.chat_model)

    @property
    def reranker_configured(self) -> bool:
        return bool(self.rerank_base_url and self.rerank_model)

    @property
    def allowed_hosts(self) -> set[str]:
        from urllib.parse import urlparse

        hosts = {h.strip().lower() for h in self.allowed_llm_hosts.split(",") if h.strip()}
        own = urlparse(self.chat_base_url).hostname
        if own:
            hosts.add(own.lower())
        return hosts

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude={"growi_token", "growi_attachment_token", "chat_api_key", "rerank_api_key", "embed_api_key", "usage_log_path", "jev_api_key", "jev_local_path"})
