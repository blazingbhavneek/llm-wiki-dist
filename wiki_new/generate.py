"""
Flat (legacy, non-hierarchical) generation phase plus the shared generation
rules. phase_generate_flat is dispatched from phases.make_config_for_source.
Imports from models: GenerationDecision, NewFileRef, CurrentFileState.
Imports from prompts: build_generation_prompt.
Imports from llm: make_llm, structured_ainvoke.
Imports from utils: chunking, manifest, filename and IO helpers.

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

import argparse
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from wiki_new.llm import make_llm, structured_ainvoke
from wiki_new.models import CurrentFileState, GenerationDecision, NewFileRef
from wiki_new.prompts import build_generation_prompt
from wiki_new.utils import (
    add_chunk_record,
    add_or_update_file_record,
    append_markdown,
    chunk_source_lines_preserving_tables,
    clamp_range_to_chunk,
    count_file_lines,
    create_markdown_file,
    find_file_record,
    full_chunk_range,
    init_manifest,
    last_n_lines_from_file,
    make_unique_filename,
    numbered_source_lines,
    range_to_markdown,
    read_lines,
    slugify,
    split_chunk_ranges,
    update_markdown_frontmatter,
    write_json,
)


def parse_part_number(title: str) -> tuple[str, int]:
    match = re.search(r"^(.*?)\s+-\s+Part\s+(\d+)$", title.strip(), flags=re.I)
    if match:
        base = match.group(1).strip()
        number = int(match.group(2))
        return base, number

    return title.strip(), 1


def forced_part_ref(current: CurrentFileState) -> NewFileRef:
    base, number = parse_part_number(current.title)
    next_number = number + 1

    title = f"{base} - Part {next_number}"
    filename = slugify(title) + ".md"
    summary = f"Continuation of {base}."

    return NewFileRef(title=title, filename=filename, summary=summary)


def enforce_generation_rules(
    decision: GenerationDecision,
    current: Optional[CurrentFileState],
    source_start: int,
    source_end: int,
) -> GenerationDecision:
    """
    Enforces that every line in [source_start, source_end] is assigned to a file.
    If not, creates a fallback new file for unassigned lines.
    """
    full_set = set(range(source_start, source_end + 1))
    assigned_set = set()

    # Clamp ranges first
    if decision.current_source_range:
        cr = clamp_range_to_chunk(
            decision.current_source_range, source_start, source_end
        )
        if cr:
            decision.current_source_range = cr
            assigned_set.update(range(cr[0], cr[1] + 1))

    if decision.new_source_range:
        nr = clamp_range_to_chunk(decision.new_source_range, source_start, source_end)
        if nr:
            decision.new_source_range = nr
            assigned_set.update(range(nr[0], nr[1] + 1))

    unassigned = full_set - assigned_set

    # Handle initial state
    if current is None:
        if decision.action in {"append", "split"} or not decision.new_file.title:
            decision.action = "new"
            decision.current_source_range = None
            decision.new_source_range = full_chunk_range(source_start, source_end)
            if not decision.new_file.title:
                decision.new_file.title = "Introduction"
            if not decision.new_file.filename:
                decision.new_file.filename = "introduction.md"
            if not decision.new_file.summary:
                decision.new_file.summary = "Opening section from the source document."
        assigned_set = full_set
        unassigned = set()

    # Enforce 500-line rule
    elif current and current.line_count >= 500:
        part_ref = forced_part_ref(current)
        decision.action = "new"
        decision.current_source_range = None
        decision.new_source_range = full_chunk_range(source_start, source_end)
        decision.new_file = part_ref
        decision.reason = (
            decision.reason.strip()
            + " Script enforced 500-line hard rule and created a new Part file."
        ).strip()
        assigned_set = full_set
        unassigned = set()

    # Fill missing ranges safely
    elif decision.action == "append":
        if not decision.current_source_range:
            decision.current_source_range = full_chunk_range(source_start, source_end)
        decision.new_source_range = None
        assigned_set = full_set
        unassigned = set()

    elif decision.action == "new":
        if not decision.new_source_range:
            decision.new_source_range = full_chunk_range(source_start, source_end)
        decision.current_source_range = None
        assigned_set = full_set
        unassigned = set()

    elif decision.action == "split":
        if not decision.current_source_range or not decision.new_source_range:
            cr, nr = split_chunk_ranges(source_start, source_end)
            decision.current_source_range = cr
            decision.new_source_range = nr
        assigned_set = full_set
        unassigned = set()

    elif decision.action == "ignore":
        decision.current_source_range = None
        decision.new_source_range = None
        assigned_set = set()
        unassigned = full_set

    # CRITICAL: If any lines remain unassigned, force a new file for them
    if unassigned:
        print(f"[WARNING] Unassigned lines detected: {sorted(unassigned)[:10]}...")
        # Create a minimal fallback file
        min_line, max_line = min(unassigned), max(unassigned)
        fallback_range = [min_line, max_line]

        # Force action to 'split' or handle via new file
        if decision.action == "new":
            # Extend new range
            existing = decision.new_source_range
            if existing:
                decision.new_source_range = [
                    min(existing[0], min_line),
                    max(existing[1], max_line),
                ]
            else:
                decision.new_source_range = fallback_range
        elif decision.action in {"append", "split"} and current:
            # Add fallback as new file
            decision.action = "split"
            decision.new_source_range = fallback_range
            if not decision.new_file.title:
                decision.new_file.title = f"Continuation after line {max_line}"
            if not decision.new_file.filename:
                decision.new_file.filename = f"continuation-{min_line}.md"
            if not decision.new_file.summary:
                decision.new_file.summary = "Auto-created for unassigned lines."

        # Rebuild assigned set
        assigned_set = set(range(source_start, source_end + 1))
        unassigned = set()

    return decision


async def phase_generate_flat(args: argparse.Namespace) -> None:
    source_path = Path(args.source)
    out_dir = Path(args.out)
    manifest_path = out_dir / "manifest.json"

    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)

    out_dir.mkdir(parents=True, exist_ok=True)

    if manifest_path.exists() and not args.clean:
        raise RuntimeError(
            f"{manifest_path} already exists. Use --clean to regenerate from scratch."
        )

    source_lines = read_lines(source_path)
    chunks = chunk_source_lines_preserving_tables(
        source_lines,
        target_size=args.generation_lines,
        max_extra=args.max_chunk_extra,
    )

    manifest = init_manifest(source_path)

    llm = make_llm(
        model=args.gen_model,
        base_url=args.base_url,
        api_key=args.api_key,
        temperature=args.temperature,
        timeout=args.timeout,
    )

    current: Optional[CurrentFileState] = None
    next_index = 1

    for chunk_number, (start_idx, end_idx) in enumerate(chunks, start=1):
        source_start = start_idx + 1
        source_end = end_idx

        print(
            f"[Generation] Chunk {chunk_number}/{len(chunks)} "
            f"source lines {source_start}-{source_end}"
        )

        chunk_lines = source_lines[start_idx:end_idx]
        source_block = numbered_source_lines(chunk_lines, source_start)

        if current is None:
            context_tail = ""
        else:
            context_tail = last_n_lines_from_file(out_dir / current.filename, 100)
            current.line_count = count_file_lines(out_dir / current.filename)

        messages = build_generation_prompt(current, context_tail, source_block)

        decision = await structured_ainvoke(llm, GenerationDecision, messages)

        # Enforce rules: ensures 100% line coverage and handles fallbacks
        decision = enforce_generation_rules(
            decision=decision,
            current=current,
            source_start=source_start,
            source_end=source_end,
        )

        targets: list[dict[str, Any]] = []

        if decision.action == "ignore":
            add_chunk_record(
                manifest=manifest,
                source_start=source_start,
                source_end=source_end,
                action="ignore",
                targets=[],
                reason=decision.reason,
            )
            write_json(manifest_path, manifest)
            continue

        # Helper to refresh the YAML frontmatter of a specific file
        def refresh_frontmatter(filename: str):
            file_record = find_file_record(manifest, filename)
            if file_record:
                merged_ranges = file_record.get(
                    "_merged_source_ranges", [[source_start, source_end]]
                )
                update_markdown_frontmatter(out_dir / filename, merged_ranges)

        if decision.action == "append":
            if current is None:
                raise RuntimeError(
                    "Internal error: append requested without current file."
                )

            current_range = decision.current_source_range
            body = range_to_markdown(source_lines, current_range)

            append_markdown(out_dir / current.filename, body)
            current.line_count = count_file_lines(out_dir / current.filename)

            add_or_update_file_record(
                manifest,
                current.filename,
                current.title,
                current.summary,
                current_range[0],
                current_range[1],
            )
            refresh_frontmatter(current.filename)

            targets.append(
                {
                    "filename": current.filename,
                    "source_start": current_range[0],
                    "source_end": current_range[1],
                }
            )

        elif decision.action == "new":
            new_range = decision.new_source_range
            body = range_to_markdown(source_lines, new_range)

            title = decision.new_file.title.strip() or "Untitled"
            summary = decision.new_file.summary.strip()
            filename = make_unique_filename(
                out_dir,
                next_index,
                decision.new_file.filename,
                title,
            )
            next_index += 1

            create_markdown_file(
                path=out_dir / filename,
                title=title,
                summary=summary,
                source_start=new_range[0],
                source_end=new_range[1],
                body=body,
            )

            # The new file becomes the current file
            current = CurrentFileState(
                title=title,
                filename=filename,
                summary=summary,
                line_count=count_file_lines(out_dir / filename),
            )

            add_or_update_file_record(
                manifest,
                filename,
                title,
                summary,
                new_range[0],
                new_range[1],
            )
            refresh_frontmatter(filename)

            targets.append(
                {
                    "filename": filename,
                    "source_start": new_range[0],
                    "source_end": new_range[1],
                }
            )

        elif decision.action == "split":
            if current is None:
                raise RuntimeError(
                    "Internal error: split requested without current file."
                )

            current_range = decision.current_source_range
            new_range = decision.new_source_range

            # 1. Append to the existing current file
            if current_range:
                current_body = range_to_markdown(source_lines, current_range)
                append_markdown(out_dir / current.filename, current_body)
                current.line_count = count_file_lines(out_dir / current.filename)

                add_or_update_file_record(
                    manifest,
                    current.filename,
                    current.title,
                    current.summary,
                    current_range[0],
                    current_range[1],
                )
                refresh_frontmatter(current.filename)

                targets.append(
                    {
                        "filename": current.filename,
                        "source_start": current_range[0],
                        "source_end": current_range[1],
                    }
                )

            # 2. Create the new file for the remaining lines
            if new_range:
                new_body = range_to_markdown(source_lines, new_range)

                title = decision.new_file.title.strip() or "Untitled"
                summary = decision.new_file.summary.strip()
                filename = make_unique_filename(
                    out_dir,
                    next_index,
                    decision.new_file.filename,
                    title,
                )
                next_index += 1

                create_markdown_file(
                    path=out_dir / filename,
                    title=title,
                    summary=summary,
                    source_start=new_range[0],
                    source_end=new_range[1],
                    body=new_body,
                )

                # The newly created file becomes the current file for the next chunk
                current = CurrentFileState(
                    title=title,
                    filename=filename,
                    summary=summary,
                    line_count=count_file_lines(out_dir / filename),
                )

                add_or_update_file_record(
                    manifest,
                    filename,
                    title,
                    summary,
                    new_range[0],
                    new_range[1],
                )
                refresh_frontmatter(filename)

                targets.append(
                    {
                        "filename": filename,
                        "source_start": new_range[0],
                        "source_end": new_range[1],
                    }
                )

        else:
            raise RuntimeError(f"Unknown action: {decision.action}")

        add_chunk_record(
            manifest=manifest,
            source_start=source_start,
            source_end=source_end,
            action=decision.action,
            targets=targets,
            reason=decision.reason,
        )

        write_json(manifest_path, manifest)

    print(f"[Generation] Done. Manifest written to {manifest_path}")
