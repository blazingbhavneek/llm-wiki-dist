"""Configuration for overlapping observation, seed planning, and rewriting."""

from __future__ import annotations

from pydantic import BaseModel

PROMPT_VERSION = "wiki-overlap-plan-ja-6"
SEED_PLAN_VERSION = "wiki-seed-plan-ja-11"
REWRITE_PROMPT_VERSION = "wiki-sections-ja-4"

class WikiConfig(BaseModel):
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
    # Planner target per page; pages over twice this are split at headings by Python.
    page_target_lines: int = 100

    # Section-wise page writing
    rewrite_concurrency: int = 4
    section_target_lines: int = 80
    section_min_lines: int = 8
    write_attempts: int = 3
    write_max_output_tokens: int = 8000
    intro_max_output_tokens: int = 1500
    reference_candidates: int = 6
    reference_attempts: int = 3
    reference_max_output_tokens: int = 8000
    judge_attempts: int = 2
    judge_max_output_tokens: int = 4000

    # Model and output
    chat_base_url: str = "http://10.160.144.101:51029/v1"
    chat_api_key: str = "local"
    chat_model: str = "gemma-4-31B"
    temperature: float = 0.0
    request_timeout: int = 300
    # Reasoning on plain-text rewrite/intro calls (structured calls keep the
    # server default). Python's lossless checks gate the output either way.
    text_thinking: bool = False
    output_language: str = "Japanese (日本語)"
    prompt_version: str = PROMPT_VERSION
    resume: bool = True
    output_root: str = ".wiki/pages"
    document_slug: str = ""
    # Keep project runs stable so changed sources can resume incrementally.
    run_dir: str = ""

    # Format-aware structural planning.
    source_kind: str = "md"
    structure_target_lines: int = 250
    structure_min_lines: int = 40
    slide_delimiter: str = r"^## Slide (\d+)\s*$"
    slide_title: str = r"^### (.+?)\s*$"
    pdf_use_headings: bool = False
    tabular_slice_records: int = 40
    tabular_preview_rows: int = 12
    tabular_preview_cols: int = 12
