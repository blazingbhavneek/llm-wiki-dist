"""
Data layer: runtime configuration constants and every Pydantic schema
(plus the CurrentFileState dataclass) shared across the package. Leaf module;
imports nothing from wiki. Change a schema or a config default here and every
other module sees it.

Package `wiki/` — lossless Markdown wiki generator. Module layout
(low-level to high-level; imports only ever point downward in this list):

- models.py    Runtime config constants (SOURCE_PATH, OUTPUT_ROOT, BASE_URL,
               GEN_MODEL, GENERATION_LINES, ... PARTITION_RETRY_ATTEMPTS) and all
               Pydantic schemas + the CurrentFileState dataclass (FileRef,
               NewFileRef, GenerationDecision, VerificationResult, RepairResult,
               ChunkSummary, TopicRange, H1Plan, H1Layout, LeafPagePlan).
               No wiki imports.
- utils.py     Pure stdlib helpers: line-range/markdown (range_to_markdown,
               clamp_range_to_chunk, split_chunk_ranges), file/JSON IO
               (read_lines, write_json, load_json), filenames/slugs
               (slugify, make_unique_filename), source chunking
               (chunk_source_lines_preserving_tables, fixed_windows), manifest +
               markdown-file records (init_manifest, add_or_update_file_record,
               create_markdown_file, add_chunk_record,
               find_best_target_for_source_window, overlap_size),
               extract_json_from_text. No wiki imports.
- llm.py       LLM client: make_llm, structured_ainvoke (structured-output call
               with JSON fallback). Imports: utils.
- prompts.py   All prompt builders build_*_prompt (chunk summary, H1 plan, H1
               layout, leaf page, generation, verification, repair).
               Imports: models.
- planning.py  Hierarchy planning + tree rendering: chunk-summary ledger
               (format_summary_ledger, summaries_for_range), exact-partition
               validation (validate_exact_partition,
               structured_partition_ainvoke_with_retries, partition_or_fallback,
               assert_exact_coverage) and the wiki-tree writers
               (render_hierarchical_wiki, write_navigation_index,
               write_topic_plan_document, hierarchy_to_manifest,
               planned_leaf_pages). Imports: models, utils, llm.
- generate.py  Flat (non-hierarchical) generation phase: enforce_generation_rules,
               parse_part_number, forced_part_ref, phase_generate_flat.
               Imports: models, utils, prompts, llm.
- phases.py    Hierarchical generation (phase_generate), verification
               (verify_one_window, phase_verify), repair (phase_repair) and the
               batch runner / entrypoint (collect_source_files,
               make_config_for_source, process_one_source, async_main, main).
               Imports: generate, planning, prompts, utils, llm, models.

Entrypoint: ../md.py is a thin shim that calls wiki.phases.main.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------
# Runtime config
# ---------------------------------------------------------------------

SOURCE_PATH = "/home/seigyo/llm-wiki/input"
OUTPUT_ROOT = "/home/seigyo/llm-wiki/output4"

PHASE = "all"  # all | generate | generate-flat | verify | repair

BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://180.21.170.235:42383/v1")
API_KEY = os.environ.get("OPENAI_API_KEY", "local")

GEN_MODEL = os.environ.get("WIKI_GEN_MODEL", "nvidia/Qwen3.6-35B-A3B-NVFP4")
VERIFY_MODEL = os.environ.get("WIKI_VERIFY_MODEL", GEN_MODEL)

GENERATION_LINES = 100
VERIFICATION_LINES = 25
MAX_CHUNK_EXTRA = 50

# Concurrency inside verification for one file.
CONCURRENCY = 20

# Number of input markdown files processed at the same time.
FILE_CONCURRENCY = 4

TEMPERATURE = 0.7
TIMEOUT = 300

CLEAN_OUTPUT = True

PARTITION_RETRY_ATTEMPTS = 3

# ---------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------


class FileRef(BaseModel):
    title: str = ""
    filename: str = ""


class NewFileRef(BaseModel):
    title: str = ""
    filename: str = ""
    summary: str = ""


class GenerationDecision(BaseModel):
    action: Literal["append", "new", "split", "ignore"]
    current_file: FileRef = Field(default_factory=FileRef)
    new_file: NewFileRef = Field(default_factory=NewFileRef)

    # Inclusive source line ranges, using real source line numbers shown to the LLM.
    # Example: [101, 150]
    current_source_range: Optional[list[int]] = None
    new_source_range: Optional[list[int]] = None

    reason: str = ""


class VerificationResult(BaseModel):
    answer: Literal["YES", "NO"]
    missing_facts: list[str] = Field(default_factory=list)
    hallucinations: list[str] = Field(default_factory=list)
    reason: str = ""


class RepairResult(BaseModel):
    markdown_patch: str = ""
    reason: str = ""


class ChunkSummary(BaseModel):
    """A factual description of one source chunk; it never owns source text."""

    summary: str = ""
    topics: list[str] = Field(default_factory=list)
    suggested_heading: str = ""


class TopicRange(BaseModel):
    """A named, contiguous source range used at one level of the wiki tree."""

    title: str = ""
    summary: str = ""
    source_range: Optional[list[int]] = None


class H1Plan(BaseModel):
    sections: list[TopicRange] = Field(default_factory=list)


class H1Layout(BaseModel):
    """Sections are H2 folders when use_h2_folders is true, otherwise leaf pages."""

    use_h2_folders: bool = False
    sections: list[TopicRange] = Field(default_factory=list)


class LeafPagePlan(BaseModel):
    pages: list[TopicRange] = Field(default_factory=list)


@dataclass
class CurrentFileState:
    title: str
    filename: str
    summary: str
    line_count: int
