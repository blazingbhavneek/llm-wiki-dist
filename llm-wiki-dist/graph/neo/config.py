"""Configuration for overlapping observation, seed planning, and rewriting."""

from __future__ import annotations

from pydantic import BaseModel

PROMPT_VERSION = "neo-overlap-plan-ja-6"
SEED_PLAN_VERSION = "neo-seed-plan-ja-8"
REWRITE_PROMPT_VERSION = "neo-sections-ja-1"


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

    # Section-wise page writing
    rewrite_concurrency: int = 4
    section_target_lines: int = 80
    section_min_lines: int = 8
    write_attempts: int = 3
    write_max_output_tokens: int = 8000
    intro_max_output_tokens: int = 1500
    reference_candidates: int = 3
    reference_attempts: int = 3
    reference_max_output_tokens: int = 4000
    judge_attempts: int = 2
    judge_max_output_tokens: int = 4000

    # Model and output
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
