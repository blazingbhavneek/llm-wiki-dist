"""Search-service settings: GROWI, LLM, embedder/reranker, JEV, local index, research limits."""

from __future__ import annotations

import configparser
import logging
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel

log = logging.getLogger("growi_search_config")

# growi-search runs from its own directory; the shared jev and graph packages are adjacent.
_AIR_ROOT = str(Path(__file__).resolve().parent.parent)
if _AIR_ROOT not in sys.path:
    sys.path.append(_AIR_ROOT)


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


def _unit(value: str | None, default: float) -> float:
    return max(0.0, min(float(value or default), 1.0))


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    # GROWI
    growi_url: str = ""
    growi_token: str = ""
    growi_attachment_token: str = ""
    growi_root_path: str = "/"
    growi_timeout: float = 30.0
    growi_concurrency: int = 6

    # Chat LLM (shared server: llm_max_concurrency is the number of slots this service uses)
    chat_base_url: str = ""
    chat_api_key: str = "local"
    chat_model: str = "gemma-4-31B"
    chat_temperature: float = 0.2
    llm_max_output_tokens: int = 32768
    llm_context_tokens: int = 131072
    llm_timeout: int = 900
    llm_max_retries: int = 2
    llm_max_concurrency: int = 4

    # Embeddings and reranker (both optional: without them search is BM25-only / RRF order)
    embed_base_url: str = ""
    embed_model: str = ""
    embed_api_key: str = "local"
    embed_query_prefix: str = ""
    embed_doc_prefix: str = ""
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = ""
    rerank_timeout: int = 30

    # JEV yes/no classifier (the finder's judge)
    jev_enabled: bool = False
    jev_backend: str = "auto"          # auto | local | hosted (vanilla /v1/systemone) | llm2jev (custom batched /score)
    jev_local_path: str = ""
    jev_device: str = "auto"
    jev_dtype: str = "bfloat16"
    jev_base_url: str = ""
    jev_api_key: str = ""
    jev_model: str = "chaoliangUNSW/Jev-Style-0.8B-Decision-v3"
    jev_timeout: int = 60
    jev_help_threshold: float = 0.5    # question B: "does this section help answer it?"
    jev_direct_threshold: float = 0.8  # question A: "does this section answer it directly?"

    # Local index built from the 目次 data blocks
    index_page_name: str = "00-目次"
    store_dir: str = ""
    sync_seconds: int = 30
    revision_sweep_seconds: int = 300

    # Finder
    search_pool: int = 300             # sections pulled from Qdrant before rerank + JEV
    rerank_pool: int = 200
    wave_size: int = 100
    max_waves: int = 4

    # Research and compiler (16k/32k/64k cap OUTPUT only; inputs are never cut)
    quick_max_pages: int = 3
    max_threads: int = 12
    max_rounds: int = 3
    max_gaps: int = 6
    run_seconds: int = 1200            # soft: no new research round starts after it
    subagent_concurrency: int = 4      # researchers at once, never above llm_max_concurrency
    subagent_max_steps: int = 20
    subagent_report_tokens: int = 16384
    lead_check_tokens: int = 16384
    report_fold_tokens: int = 32768
    final_compiler_tokens: int = 65536
    service_max_agents: int = 4        # questions researched at the same time

    # Page cache for page views and researcher reads
    page_cache_ttl: int = 120
    page_cache_max: int = 256
    page_cache_mb: int = 64

    # Presentation / deployment
    doc_parser_url: str = "/agent/doc-parser/"
    prefix: str = "/growi-search"
    allowed_llm_hosts: str = ""
    usage_log_path: str = ""
    trace_dir: str = ""                # one JSON per question: every score and decision (for tuning)

    @classmethod
    def from_env(cls) -> "Settings":
        _load_env_files()
        env = os.environ.get
        project = _project_values()

        token = (project.get("growi_token") or env("GROWI_TOKEN") or "").strip()
        if token.lower().startswith("api token:"):  # tolerate a pasted "API Token: ..." value
            token = token.split(":", 1)[1].strip()

        root = (env("GROWI_ROOT_PATH") or "/").strip() or "/"
        if not root.startswith("/"):
            root = "/" + root
        if len(root) > 1:
            root = root.rstrip("/")

        embed_model = env("WIKI_EMBED_MODEL") or ""
        ruri = "ruri" in embed_model.lower()  # ruri-v3 is trained with these retrieval prefixes
        llm_slots = _clamp(int(env("WIKI_SEARCH_LLM_MAX_CONCURRENCY") or 4), 1, 256)

        return cls(
            growi_url=(project.get("growi_url") or env("GROWI_URL") or "").rstrip("/"),
            growi_token=token,
            growi_attachment_token=(env("GROWI_ATTACHMENT_TOKEN") or token).strip(),
            growi_root_path=root,
            growi_timeout=float(env("GROWI_TIMEOUT") or 30),
            growi_concurrency=_clamp(int(env("WIKI_GROWI_CONCURRENCY") or 6), 1, 32),
            chat_base_url=(env("WIKI_CHAT_BASE_URL") or project.get("chat_base_url")
                           or env("OPENAI_BASE_URL") or "").rstrip("/"),
            chat_api_key=env("WIKI_CHAT_API_KEY") or env("OPENAI_API_KEY") or "local",
            chat_model=env("WIKI_CHAT_MODEL") or env("WIKI_MODEL") or "gemma-4-31B",
            chat_temperature=float(env("WIKI_CHAT_TEMPERATURE") or 0.2),
            llm_max_output_tokens=max(0, int(env("WIKI_LLM_MAX_OUTPUT_TOKENS") or 32768)),
            llm_context_tokens=max(4096, int(env("WIKI_LLM_CONTEXT_TOKENS") or 131072)),
            llm_timeout=max(1, int(env("WIKI_REQUEST_TIMEOUT") or 900)),
            llm_max_retries=_clamp(int(env("WIKI_LLM_MAX_RETRIES") or 2), 0, 5),
            llm_max_concurrency=llm_slots,
            embed_base_url=(env("WIKI_EMBED_BASE_URL") or "").rstrip("/"),
            embed_model=embed_model,
            embed_api_key=env("WIKI_EMBED_API_KEY") or "local",
            embed_query_prefix=env("WIKI_EMBED_QUERY_PREFIX", "検索クエリ: " if ruri else ""),
            embed_doc_prefix=env("WIKI_EMBED_DOC_PREFIX", "検索文書: " if ruri else ""),
            rerank_base_url=(env("WIKI_RERANK_BASE_URL") or "").rstrip("/"),
            rerank_api_key=env("WIKI_RERANK_API_KEY") or "",
            rerank_model=env("WIKI_RERANK_MODEL") or "",
            rerank_timeout=int(env("WIKI_RERANK_TIMEOUT") or 30),
            jev_enabled=_bool(env("WIKI_JEV_ENABLED")),
            jev_backend=(env("WIKI_JEV_BACKEND") or "auto").strip().lower(),
            jev_local_path=(env("WIKI_JEV_LOCAL_PATH") or "").strip(),
            jev_device=(env("WIKI_JEV_DEVICE") or "auto").strip().lower(),
            jev_dtype=(env("WIKI_JEV_DTYPE") or "bfloat16").strip().lower(),
            jev_base_url=(env("WIKI_JEV_BASE_URL") or "").rstrip("/"),
            jev_api_key=(env("WIKI_JEV_API_KEY") or "").strip(),
            jev_model=env("WIKI_JEV_MODEL") or "chaoliangUNSW/Jev-Style-0.8B-Decision-v3",
            jev_timeout=int(env("WIKI_JEV_TIMEOUT") or 60),
            jev_help_threshold=_unit(env("WIKI_JEV_HELP_THRESHOLD") or env("WIKI_JEV_THRESHOLD"), 0.5),
            jev_direct_threshold=_unit(env("WIKI_JEV_DIRECT_THRESHOLD") or env("WIKI_JEV_SEED_THRESHOLD"), 0.8),
            index_page_name=env("WIKI_INDEX_PAGE_NAME") or "00-目次",
            store_dir=(env("WIKI_SEARCH_STORE_DIR") or str(Path(_AIR_ROOT) / "data" / "growi-search-store")).strip(),
            sync_seconds=max(5, int(env("WIKI_SEARCH_SYNC_SECONDS") or 30)),
            revision_sweep_seconds=max(30, int(env("WIKI_SEARCH_REVISION_SWEEP_SECONDS") or 300)),
            search_pool=_clamp(int(env("WIKI_SEARCH_POOL") or 300), 20, 5000),
            rerank_pool=_clamp(int(env("WIKI_RERANK_POOL") or 200), 1, 2000),
            wave_size=_clamp(int(env("WIKI_JEV_WAVE_SIZE") or 100), 10, 2000),
            max_waves=_clamp(int(env("WIKI_JEV_MAX_WAVES") or 4), 1, 50),
            quick_max_pages=_clamp(int(env("WIKI_QUICK_MAX_PAGES") or 3), 1, 20),
            max_threads=_clamp(int(env("WIKI_MAX_THREADS") or 12), 1, 64),
            max_rounds=_clamp(int(env("WIKI_MAX_ROUNDS") or 3), 1, 10),
            max_gaps=_clamp(int(env("WIKI_MAX_GAPS") or 6), 1, 32),
            run_seconds=max(60, int(env("WIKI_RUN_SECONDS") or 1200)),
            subagent_concurrency=min(_clamp(int(env("WIKI_SUBAGENT_CONCURRENCY") or llm_slots), 1, 256), llm_slots),
            subagent_max_steps=_clamp(int(env("WIKI_SUBAGENT_MAX_STEPS") or 20), 4, 100),
            subagent_report_tokens=_clamp(int(env("WIKI_SUBAGENT_REPORT_TOKENS") or 16384), 256, 131072),
            lead_check_tokens=_clamp(int(env("WIKI_LEAD_CHECK_TOKENS") or 16384), 256, 131072),
            report_fold_tokens=_clamp(int(env("WIKI_REPORT_FOLD_TOKENS") or 32768), 256, 131072),
            final_compiler_tokens=_clamp(int(env("WIKI_FINAL_COMPILER_TOKENS") or 65536), 256, 131072),
            service_max_agents=_clamp(int(env("WIKI_SERVICE_MAX_AGENTS") or 4), 1, 32),
            page_cache_ttl=int(env("WIKI_PAGE_CACHE_TTL") or 120),
            page_cache_max=_clamp(int(env("WIKI_PAGE_CACHE_MAX") or 256), 1, 4096),
            page_cache_mb=max(1, int(env("WIKI_PAGE_CACHE_MB") or 64)),
            doc_parser_url=env("WIKI_DOC_PARSER_URL") or "/agent/doc-parser/",
            prefix=(env("WIKI_PREFIX") or "/growi-search").strip().rstrip("/"),
            allowed_llm_hosts=env("WIKI_ALLOWED_LLM_HOSTS") or "",
            usage_log_path=env("WIKI_USAGE_LOG_PATH") or "",
            trace_dir=(env("WIKI_SEARCH_TRACE_DIR") or "").strip(),
        )

    def validate_strict(self) -> None:
        """Startup validation. Raises ValueError on unusable configuration."""
        if not self.growi_url:
            raise ValueError("GROWI_URL is required")
        if not self.growi_token:
            raise ValueError("GROWI_TOKEN is required")
        if self.jev_direct_threshold < self.jev_help_threshold:
            raise ValueError("WIKI_JEV_DIRECT_THRESHOLD must be >= WIKI_JEV_HELP_THRESHOLD")
        if max(self.subagent_report_tokens, self.report_fold_tokens, self.final_compiler_tokens) * 2 > self.llm_context_tokens:
            raise ValueError("an output budget must leave at least half of WIKI_LLM_CONTEXT_TOKENS for input")

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
        return self.model_dump(exclude={"growi_token", "growi_attachment_token", "chat_api_key", "rerank_api_key",
                                        "embed_api_key", "usage_log_path", "jev_api_key", "jev_local_path", "store_dir",
                                        "trace_dir"})
