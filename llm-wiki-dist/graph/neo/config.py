"""Configuration for overlapping observation, seed planning, and rewriting."""

from __future__ import annotations

from pydantic import BaseModel, Field

PROMPT_VERSION = "neo-overlap-plan-ja-6"
SEED_PLAN_VERSION = "neo-seed-plan-ja-8"
REWRITE_PROMPT_VERSION = "neo-agent-research-plan-write-ja-12"
LINK_PROMPT_VERSION = "neo-inline-links-ja-2"


class HermesConfig(BaseModel):
    binary: str = "hermes"
    model: str = "gemma-4-31B"
    provider: str = "custom"
    base_url: str = "http://10.160.144.101:51029/v1"
    api_key: str = "local"
    api_mode: str = "chat_completions"
    reasoning: str = ""
    toolsets: str = "file,skills"
    max_turns: int = 60
    timeout_seconds: int = 900
    profile: str = "neo-pipeline"
    home: str = ""
    disable_egress: bool = True
    persistent_session: bool = False
    extra_args: list[str] = Field(default_factory=list)


class PiConfig(BaseModel):
    binary: str = "pi"
    provider: str = "local-vllm"
    model: str = "gemma-4-31B"
    base_url: str = "http://10.160.144.101:51029/v1"
    api_key: str = "local"
    api: str = "openai-completions"
    reasoning: bool = True
    context_window: int = 262144
    max_tokens: int = Field(default=65536, ge=1, le=65536)
    thinking: str = ""
    tools: str = "read,write,edit,grep,find,ls"
    config_dir: str = ""
    timeout_seconds: int = 900
    extra_args: list[str] = Field(default_factory=list)


class NeoConfig(BaseModel):
    # Overlapping source observation
    window_target_lines: int = 250
    window_overlap_lines: int = 50
    planner_concurrency: int = 4
    planner_attempts: int = 5
    regional_window_count: int = 10
    # Zero means retry the final range compiler until it succeeds or is cancelled.
    map_attempts: int = 0
    planner_max_output_tokens: int = 4000
    map_max_output_tokens: int = 32000

    # Independent page rewriting
    rewrite_concurrency: int = 4
    # Total plan/write attempts per page, shared by the initial version and repair.
    rewrite_attempts: int = 4
    rewrite_max_output_tokens: int = 16000
    rewrite_versions: int = 2
    reference_min_reads: int = 5
    reference_max_steps: int = 30
    reference_attempts: int = 3
    reference_max_output_tokens: int = 4000
    judge_attempts: int = 2
    judge_max_output_tokens: int = 4000

    # Additive cross-linking after every page has been rewritten
    link_concurrency: int = 4
    link_attempts: int = 2
    link_max_output_tokens: int = 8000

    # Model and output
    agent_backend: str = "hermes"
    chat_base_url: str = "http://10.160.144.101:51029/v1"
    chat_api_key: str = "local"
    chat_model: str = "gemma-4-31B"
    temperature: float = 0.0
    request_timeout: int = 300
    output_language: str = "Japanese (日本語)"
    prompt_version: str = PROMPT_VERSION
    resume: bool = True
    output_root: str = ".wiki/neo"
    document_slug: str = ""

    hermes: HermesConfig = Field(default_factory=HermesConfig)
    pi: PiConfig = Field(default_factory=PiConfig)
