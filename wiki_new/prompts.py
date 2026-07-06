"""
Prompt-building layer. Each build_*_prompt returns the message list for one
LLM call; no IO, no LLM invocation.
Imports from models: TopicRange, CurrentFileState (used in prompt signatures).

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
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from wiki_new.models import CurrentFileState, TopicRange


def build_chunk_summary_prompt(
    previous_source_block: str,
    prior_summary_ledger: str,
    source_block: str,
    source_start: int,
    source_end: int,
) -> list[Any]:
    return [
        SystemMessage(
            content=(
                "You summarize source chunks for a later wiki-planning pass. "
                "Do not rewrite or omit source content. Describe every meaningful "
                "topic, instruction, command, warning, and transition in the current "
                "range so a later planner can choose safe page boundaries."
                "Keep your answer very very short, 2-3 lines max per field"
            )
        ),
        HumanMessage(
            content=f"""
Current source range: {source_start}–{source_end}.

Previous 100 source lines, included only for context:
```text
{previous_source_block or "(This is the first source chunk.)"}
```

Line-range summary ledger accumulated before this chunk:
```markdown
{prior_summary_ledger}
```

Current numbered source lines to summarize:
```text
{source_block}
```

Return one concise factual summary of this exact range. `topics` should list the
important subjects in source order. `suggested_heading` should be a short label
for this range. Do not claim facts not present in the source.

Dont overthink it, answer quickly.
""",
        ),
    ]


def build_h1_plan_prompt(
    summary_ledger: str,
    source_line_count: int,
    chunk_ranges: list[list[int]],
) -> list[Any]:
    return [
        SystemMessage(
            content=(
                "You are the first pass of a lossless Markdown wiki planner. "
                "Plan only top-level H1 guide sections from the chunk summaries. "
                "Do not write Markdown or source content."
            )
        ),
        HumanMessage(
            content=f"""
Create H1 sections for source lines 1–{source_line_count}.

The returned `sections` source ranges MUST be a contiguous exact partition of
the whole document: first range starts at 1, each next range starts one line
after the previous range, and the final range ends at {source_line_count}.
There must be no gaps and no overlaps.

Choose boundaries only at these chunk ranges; each returned range must begin at
one listed range start and end at one listed range end:
{json.dumps(chunk_ranges)}

Chunk summary ledger:
```markdown
{summary_ledger}
```

Use a small number of reader-facing guide titles. Each section needs a concise
summary and an inclusive `source_range`.
""",
        ),
    ]


def build_h1_layout_prompt(
    h1: TopicRange,
    summary_ledger: str,
    chunk_ranges: list[list[int]],
) -> list[Any]:
    source_start, source_end = h1.source_range or [0, 0]
    return [
        SystemMessage(
            content=(
                "You decide whether one H1 wiki guide needs H2 folders. "
                "Plan ranges only; do not write Markdown or source text."
            )
        ),
        HumanMessage(
            content=f"""
H1 guide: {h1.title}
H1 source range: {source_start}–{source_end}
H1 summary: {h1.summary}

Decide whether this guide has enough distinct major subtopics to use H2 folders.
Set `use_h2_folders` to true only when H2 folders improve navigation. If false,
`sections` must be the final leaf pages directly inside the H1 folder. If true,
`sections` must be the H2 folder ranges; a later pass will make leaf pages.

In either case, `sections` must exactly and contiguously partition {source_start}–{source_end},
with no gap or overlap. Use only these chunk ranges:
{json.dumps(chunk_ranges)}

Relevant chunk summaries:
```markdown
{summary_ledger}
```
""",
        ),
    ]


def build_leaf_page_plan_prompt(
    parent: TopicRange,
    parent_label: str,
    summary_ledger: str,
    chunk_ranges: list[list[int]],
) -> list[Any]:
    source_start, source_end = parent.source_range or [0, 0]
    return [
        SystemMessage(
            content=(
                "You create the final page plan for one lossless wiki section. "
                "Plan only page titles, summaries, and exact source ranges."
            )
        ),
        HumanMessage(
            content=f"""
Parent {parent_label}: {parent.title}
Source range: {source_start}–{source_end}
Summary: {parent.summary}

Return reader-sized leaf pages. Their `pages` ranges MUST be an exact contiguous
partition of {source_start}–{source_end}; no source line may be skipped or
assigned twice. Boundaries must use only these complete chunk ranges:
{json.dumps(chunk_ranges)}

Relevant chunk summaries:
```markdown
{summary_ledger}
```
""",
        ),
    ]


def build_generation_prompt(
    current: Optional[CurrentFileState],
    context_tail: str,
    source_block: str,
) -> list[Any]:
    system = SystemMessage(
        content=(
            "You are Markdown Wiki Maker. "
            "You are a routing and splitting assistant only. "
            "You do NOT rewrite, summarize, clean, or transform the source text. "
            "Your job is only to decide which numbered source line ranges go into which wiki file. "
            "The script will copy the original source lines into the final Markdown files. "
            "Ignore pure visual noise such as page numbers, repeated headers, and footers."
        )
    )

    if current is None:
        current_context = (
            "There is NO current file yet.\n"
            "You MUST use action 'new' unless all source lines are pure visual noise, "
            "in which case use 'ignore'."
        )
    else:
        current_context = f"""
Current file:
- title: {current.title}
- filename: {current.filename}
- current line count: {current.line_count}
- summary: {current.summary}

STRICT 500-LINE RULE:
If the current file line count is 500 or more, you MUST NOT use 'append'.
You MUST use 'new' and name the file '[Current Title] - Part X'.
"""

    human = HumanMessage(content=f"""
Process the next numbered source block.

Each source line is prefixed as:

<line number>: <line content>

Use the visible line numbers to choose exact inclusive source ranges.

Action definitions:
- append: The new lines continue the current topic. Put the full/selected range in current_source_range.
- new: The new lines are a new topic. Put the full/selected range in new_source_range.
- split: Some lines continue current topic, later lines start a new topic. Use both current_source_range and new_source_range.
- ignore: The lines are pure visual noise. Use null ranges.

Use this JSON schema exactly:

{{
  "action": "append | new | split | ignore",
  "current_file": {{
    "title": "Keep same or update slightly",
    "filename": "keep-same-filename.md"
  }},
  "new_file": {{
    "title": "New Topic Title",
    "filename": "new-topic.md",
    "summary": "1-2 sentence summary"
  }},
  "current_source_range": [101, 150],
  "new_source_range": [151, 200],
  "reason": "Brief explanation of the decision"
}}

Range rules:
1. Ranges are inclusive.
2. Use only line numbers that appear in the provided source block.
3. For append, set current_source_range and set new_source_range to null.
4. For new, set new_source_range and set current_source_range to null.
5. For split, set both ranges.
6. For ignore, set both ranges to null.
7. Do NOT return Markdown content.
8. Do NOT rewrite the source lines.
9. The script will copy original source text without line number prefixes.
10. If the current file line count is 500 or more, you MUST NOT use 'append'. You MUST use 'new' and name the file '[Current Title] - Part X'.

{current_context}

Context tail, last 100 lines of already generated current file:
```markdown
{context_tail}
```

New source lines:
```text
{source_block}
```
""")

    return [system, human]


def build_verification_prompt(
    source_block: str,
    target_filename: str,
    wiki_content: str,
) -> list[Any]:
    system = SystemMessage(
        content=(
            "You are a strict verification checker. "
            "Your job is to compare source document lines against the generated wiki page. "
            "Ignore page numbers, repeated headers, footers, and pure formatting artifacts. "
            "Do not flag harmless wording changes. "
            "Do flag lost facts, steps, rules, commands, warnings, numeric values, and table data. "
            "Also flag hallucinated claims that are not supported by the source."
        )
    )

    human = HumanMessage(content=f"""
Here are 25 source lines from the original document.

```text
{source_block}
```

Here is the generated wiki page they were assigned to.

Target file: {target_filename}

```markdown
{wiki_content}
```

Question:
Did any non-trivial information get lost or hallucinated?

Reply using structured JSON:
- answer: "YES" if information was lost or hallucinated.
- answer: "NO" if all important information is preserved.
- missing_facts: list missing facts, if any.
- hallucinations: list unsupported generated claims, if any.
- reason: brief explanation.
""")

    return [system, human]


def build_repair_prompt(
    source_block: str,
    target_filename: str,
    wiki_content: str,
    missing_facts: list[str],
    hallucinations: list[str],
) -> list[Any]:
    system = SystemMessage(
        content=(
            "You are a Markdown repair assistant. "
            "Your job is to create a concise Markdown patch to append to an existing wiki page. "
            "Do not rewrite the whole page. "
            "Do not duplicate facts already present. "
            "Preserve all non-trivial source information. "
            "If there is a hallucination, add a corrective clarification instead of deleting text."
        )
    )

    human = HumanMessage(content=f"""
Repair this wiki page by producing Markdown that can be appended to the file.

Target file: {target_filename}

Flagged source lines:
```text
{source_block}
```

Verifier missing facts:
{json.dumps(missing_facts, indent=2, ensure_ascii=False)}

Verifier hallucinations:
{json.dumps(hallucinations, indent=2, ensure_ascii=False)}

Current target wiki content:
```markdown
{wiki_content}
```

Return structured JSON:
- markdown_patch: Markdown text to append to the target file.
- reason: brief explanation.

The patch should:
1. Add missing facts, steps, rules, commands, warnings, numeric values, and table data.
2. Avoid duplicating content already present.
3. Be ready to append directly to the Markdown file.
""")

    return [system, human]
