"""
LLM access layer. make_llm builds the ChatOpenAI client from config; 
structured_ainvoke runs a structured-output call and falls back to JSON-only
prompting + manual validation.
Imports from utils: extract_json_from_text (parses the fallback JSON).

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

import json
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from wiki.utils import extract_json_from_text


# ---------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------


def make_llm(
    model: str,
    base_url: str,
    api_key: str,
    temperature: float = 0.0,
    timeout: int = 300,
) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        timeout=timeout,
        # Temporary: disable thinking for faster summaries. Comment out to re-enable.
        # model_kwargs={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    )

async def structured_ainvoke(
    llm: ChatOpenAI,
    schema_cls: type[BaseModel],
    messages: list[Any],
    max_output_tokens: int | None = None,
) -> BaseModel:
    call_llm = (
        llm.bind(max_tokens=max_output_tokens)
        if max_output_tokens is not None
        else llm
    )

    try:
        structured = call_llm.with_structured_output(schema_cls)
        result = await structured.ainvoke(messages)

        if isinstance(result, schema_cls):
            return result

        return schema_cls.model_validate(result)

    except Exception:
        schema_json = json.dumps(schema_cls.model_json_schema(), indent=2)

        fallback_messages = list(messages)
        fallback_messages.append(
            HumanMessage(
                content=(
                    "Return ONLY valid JSON matching this JSON Schema. "
                    "Be concise. Do not include extra prose.\n\n"
                    f"{schema_json}"
                )
            )
        )

        raw = await call_llm.ainvoke(fallback_messages)
        text = raw.content if hasattr(raw, "content") else str(raw)
        data = extract_json_from_text(text)
        return schema_cls.model_validate(data)
