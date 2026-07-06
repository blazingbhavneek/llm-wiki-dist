from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from wiki.llm import structured_ainvoke
from wiki.utils import (
    chunk_source_lines_preserving_tables,
    is_fence_line,
    is_tableish_line,
    numbered_source_lines,
    slugify,
    utc_now_iso,
    write_json,
)


# ---------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------


class ConceptFilePlan(BaseModel):
    """
    One concept/topic file assignment.

    source_start/source_end are 1-based inclusive source line numbers.
    """

    title: str = Field(
        description="Human-readable concept/topic title."
    )
    filename: str = Field(
        description=(
            "Markdown filename only, no directories. Example: "
            "function_mpf_mfs_open.md"
        )
    )
    source_start: int = Field(
        description="1-based inclusive first source line covered by this file."
    )
    source_end: int = Field(
        description="1-based inclusive last source line covered by this file."
    )
    summary: str = Field(
        default="",
        description="Short description of what this file contains.",
    )


class ConceptSplitResult(BaseModel):
    files: list[ConceptFilePlan] = Field(
        description=(
            "Ordered list of concept files. Must exactly cover the requested "
            "source range with no gaps and no overlaps."
        )
    )


# ---------------------------------------------------------------------
# Filename / text helpers
# ---------------------------------------------------------------------


def concept_range_text(plan: ConceptFilePlan) -> str:
    return f"{plan.source_start}-{plan.source_end}"


def normalize_filename(filename: str, title: str) -> str:
    """
    Normalize model-provided filename into a safe local Markdown filename.

    The model may emit Japanese names, spaces, paths, etc.
    We keep only the basename and slugify the stem.
    """

    raw_name = Path((filename or "").strip()).name
    raw_stem = Path(raw_name).stem

    if not raw_stem:
        raw_stem = title or "concept"

    clean_stem = slugify(raw_stem) or "concept"

    return f"{clean_stem}.md"


def numbered_output_filename(
    *,
    index: int,
    total: int,
    filename: str,
) -> str:
    """
    Add stable source-order prefix to final document filename.

    Example:
        001-scn-modedata-txt.md
        002-scn-condata-txt.md
    """

    clean_name = Path(filename).name
    stem = Path(clean_name).stem
    suffix = Path(clean_name).suffix or ".md"

    width = max(3, len(str(total)))

    return f"{index:0{width}d}-{stem}{suffix}"


def make_unique_output_path(out_dir: Path, filename: str) -> Path:
    """
    Avoid overwriting duplicate filenames.

    Usually the numeric prefix already prevents collisions, but this keeps the
    renderer safe if output exists for some reason.
    """

    out_dir.mkdir(parents=True, exist_ok=True)

    filename = Path(filename).name
    stem = Path(filename).stem
    suffix = Path(filename).suffix or ".md"

    candidate = out_dir / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate

    counter = 2

    while True:
        candidate = out_dir / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate

        counter += 1


def join_original_source_lines(lines: list[str]) -> str:
    """
    Preserve source text as closely as possible.

    Supports both:
    - read_lines() returning newline-terminated strings
    - read_lines() returning newline-stripped strings
    """

    if not lines:
        return ""

    if any(line.endswith("\n") for line in lines):
        text = "".join(lines)
    else:
        text = "\n".join(lines)

    if text and not text.endswith("\n"):
        text += "\n"

    return text


# ---------------------------------------------------------------------
# Boundary safety
# ---------------------------------------------------------------------


def cut_is_inside_fence(source_lines: list[str], split_line: int) -> bool:
    """
    split_line is the first line of the next concept.

    The cut is between split_line - 1 and split_line.
    Returns True if that cut is inside a fenced code block.
    """

    if split_line <= 1:
        return False

    in_fence = False

    for line in source_lines[: split_line - 1]:
        if is_fence_line(line):
            in_fence = not in_fence

    return in_fence


def cut_is_inside_image_unit(source_lines: list[str], split_line: int) -> bool:
    """
    Returns True if the cut before split_line is inside an <image-unit> block.
    """

    if split_line <= 1:
        return False

    in_image_unit = False

    for line in source_lines[: split_line - 1]:
        if "<image-unit>" in line:
            in_image_unit = True

        if "</image-unit>" in line:
            in_image_unit = False

    return in_image_unit


def cut_is_inside_table(source_lines: list[str], split_line: int) -> bool:
    """
    Returns True if the cut before split_line is inside a Markdown table.

    This rejects a cut where both sides look table-ish.
    """

    if split_line <= 1 or split_line > len(source_lines):
        return False

    previous_line = source_lines[split_line - 2]
    current_line = source_lines[split_line - 1]

    return is_tableish_line(previous_line) and is_tableish_line(current_line)


def validate_safe_internal_boundary(
    *,
    source_lines: list[str],
    split_line: int,
) -> str | None:
    """
    Validate that a concept boundary before split_line does not break a block.
    """

    if cut_is_inside_fence(source_lines, split_line):
        return f"Boundary before line {split_line} cuts inside a fenced code block."

    if cut_is_inside_image_unit(source_lines, split_line):
        return f"Boundary before line {split_line} cuts inside an <image-unit> block."

    if cut_is_inside_table(source_lines, split_line):
        return f"Boundary before line {split_line} cuts inside a Markdown table."

    return None


# ---------------------------------------------------------------------
# Coverage validation
# ---------------------------------------------------------------------


def normalize_plan_item(item: ConceptFilePlan) -> ConceptFilePlan:
    title = (item.title or "").strip()

    filename = normalize_filename(
        filename=item.filename,
        title=title or "concept",
    )

    if not title:
        title = Path(filename).stem.replace("-", " ").replace("_", " ").title()

    return ConceptFilePlan(
        title=title,
        filename=filename,
        source_start=int(item.source_start),
        source_end=int(item.source_end),
        summary=(item.summary or "").strip(),
    )


def validate_concept_partition(
    *,
    files: list[ConceptFilePlan],
    source_lines: list[str],
    source_start: int,
    source_end: int,
    label: str,
) -> tuple[list[ConceptFilePlan] | None, str | None]:
    """
    Validates that the model output covers exactly source_start-source_end.

    Rules:
    - first file starts exactly at source_start
    - last file ends exactly at source_end
    - no gaps
    - no overlaps
    - every line is covered, including blank lines
    - no invalid ranges
    - no cuts inside code fences, Markdown tables, or image blocks
    """

    if source_start > source_end:
        if files:
            return None, f"{label}: empty range cannot contain files."

        return [], None

    if not files:
        return None, (
            f"{label}: model returned no files, but it must cover "
            f"lines {source_start}-{source_end}."
        )

    normalized: list[ConceptFilePlan] = []
    expected_start = source_start

    for index, raw_item in enumerate(files, start=1):
        item = normalize_plan_item(raw_item)

        if item.source_start != expected_start:
            return None, (
                f"{label}: file #{index} starts at line {item.source_start}, "
                f"but expected line {expected_start}. This creates a gap or overlap."
            )

        if item.source_end < item.source_start:
            return None, (
                f"{label}: file #{index} has invalid range "
                f"{item.source_start}-{item.source_end}."
            )

        if item.source_start < source_start:
            return None, (
                f"{label}: file #{index} starts before requested range. "
                f"Got {item.source_start}, minimum is {source_start}."
            )

        if item.source_end > source_end:
            return None, (
                f"{label}: file #{index} ends after requested range. "
                f"Got {item.source_end}, maximum is {source_end}."
            )

        if index > 1:
            boundary_error = validate_safe_internal_boundary(
                source_lines=source_lines,
                split_line=item.source_start,
            )

            if boundary_error:
                return None, f"{label}: {boundary_error}"

        normalized.append(item)
        expected_start = item.source_end + 1

    if expected_start != source_end + 1:
        return None, (
            f"{label}: partition ended at line {expected_start - 1}, "
            f"but must end exactly at line {source_end}. Missing lines "
            f"{expected_start}-{source_end}."
        )

    return normalized, None


def assert_concept_coverage(
    *,
    files: list[ConceptFilePlan],
    source_line_count: int,
) -> None:
    """
    Final hard assertion for the whole source document.
    """

    if source_line_count == 0:
        if files:
            raise RuntimeError("Empty source cannot have concept files.")

        return

    expected_start = 1

    for index, item in enumerate(files, start=1):
        if item.source_start != expected_start:
            raise RuntimeError(
                f"Coverage error at file #{index}: expected start line "
                f"{expected_start}, got {item.source_start}."
            )

        if item.source_end < item.source_start:
            raise RuntimeError(
                f"Coverage error at file #{index}: invalid range "
                f"{item.source_start}-{item.source_end}."
            )

        if item.source_end > source_line_count:
            raise RuntimeError(
                f"Coverage error at file #{index}: ends at line "
                f"{item.source_end}, but source has only {source_line_count} lines."
            )

        expected_start = item.source_end + 1

    if expected_start != source_line_count + 1:
        raise RuntimeError(
            f"Coverage incomplete: stopped at line {expected_start - 1}; "
            f"expected coverage through line {source_line_count}."
        )


# ---------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------


def build_concept_split_prompt(
    *,
    source_start: int,
    source_end: int,
    source_block: str,
    pending: ConceptFilePlan | None,
    last_error: str | None,
) -> list[Any]:
    pending_text = ""

    if pending is not None:
        pending_text = (
            "There is one pending concept from the previous chunk.\n"
            "It has NOT been saved yet.\n"
            "You are allowed to continue it, rename it, keep it, or split it, "
            "but your returned ranges must still exactly cover the full requested "
            "range below.\n\n"
            f"Pending concept title: {pending.title}\n"
            f"Pending concept filename: {pending.filename}\n"
            f"Pending concept source range: "
            f"{pending.source_start}-{pending.source_end}\n\n"
        )

    correction_text = ""

    if last_error:
        correction_text = (
            "Your previous answer was invalid.\n"
            "Fix it and return a valid partition.\n\n"
            f"Validation error:\n{last_error}\n\n"
        )

    return [
        SystemMessage(
            content=(
                "You split numbered Markdown/source text into concept files. "
                "Return structured output only."
            )
        ),
        HumanMessage(
            content=(
                f"{correction_text}"
                "Task: split the requested numbered source lines into independent "
                "concept/topic Markdown files.\n\n"
                f"Requested source range: {source_start}-{source_end}\n\n"
                f"{pending_text}"
                "Hard requirements:\n"
                "- Return one or more files.\n"
                "- Each file must have title, filename, source_start, source_end, "
                "and summary.\n"
                "- source_start/source_end are 1-based inclusive source line numbers.\n"
                "- The first returned file must start exactly at the requested "
                "source_start.\n"
                "- The last returned file must end exactly at the requested source_end.\n"
                "- Every line must be covered exactly once.\n"
                "- Blank lines count as real lines and must be covered.\n"
                "- Do not skip blank lines.\n"
                "- Do not create gaps.\n"
                "- Do not create overlaps.\n"
                "- Do not invent line numbers.\n"
                "- Do not split inside fenced code blocks.\n"
                "- Do not split inside Markdown tables.\n"
                "- Do not split inside <image-unit> blocks.\n"
                "- Prefer splits at headings, function/class/API boundaries, "
                "tables that belong together, or clear concept transitions.\n"
                "- Filename must be a plain Markdown filename only, for example "
                "function_mpf_mfs_open.md.\n"
                "- Do not include directories in filename.\n\n"
                "Carry-over rule:\n"
                "- If the last concept looks incomplete and probably continues in "
                "the next chunk, still include it as the last returned file.\n"
                "- The caller will keep the last returned file pending and show it "
                "again with the next chunk.\n\n"
                "Numbered source:\n"
                f"{source_block}"
            )
        ),
    ]


# ---------------------------------------------------------------------
# LLM call with retry until valid
# ---------------------------------------------------------------------


async def split_window_until_valid(
    *,
    llm: ChatOpenAI,
    source_lines: list[str],
    source_start: int,
    source_end: int,
    pending: ConceptFilePlan | None,
    label: str,
    max_output_tokens: int = 8000,
) -> list[ConceptFilePlan]:
    """
    Calls the model until it returns exact coverage for source_start-source_end.

    This intentionally retries forever because correctness of coverage is more
    important than accepting a bad plan.
    """

    source_block = numbered_source_lines(
        source_lines[source_start - 1 : source_end],
        source_start,
    )

    attempt = 1
    last_error: str | None = None

    while True:
        print(
            f"[Planning] {label}: split attempt {attempt}, "
            f"source lines {source_start}-{source_end}"
        )

        messages = build_concept_split_prompt(
            source_start=source_start,
            source_end=source_end,
            source_block=source_block,
            pending=pending,
            last_error=last_error,
        )

        try:
            raw_result = await structured_ainvoke(
                llm,
                ConceptSplitResult,
                messages,
                max_output_tokens=max_output_tokens,
            )

            result = ConceptSplitResult.model_validate(raw_result)

            accepted, error = validate_concept_partition(
                files=result.files,
                source_lines=source_lines,
                source_start=source_start,
                source_end=source_end,
                label=label,
            )

            if accepted is not None:
                return accepted

            last_error = error

        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        print(
            f"[Planning] {label}: invalid split on attempt {attempt}; retrying. "
            f"Reason: {last_error}"
        )

        attempt += 1


# ---------------------------------------------------------------------
# Streaming concept planning
# ---------------------------------------------------------------------


async def plan_concept_files_streaming(
    *,
    llm: ChatOpenAI,
    source_lines: list[str],
    target_lines: int = 100,
    max_extra: int = 30,
) -> list[ConceptFilePlan]:
    """
    Main planner.

    Behavior:
    - Break source into approximately target_lines chunks.
    - Existing chunker avoids cutting tables/fences/image blocks.
    - For each chunk, ask the model to split into concept files.
    - Commit every returned file except the last one.
    - Keep the last one pending and include it in the next prompt.
    - At the final chunk, commit everything.
    - Validate complete 1-N coverage at the end.
    """

    source_line_count = len(source_lines)

    if source_line_count == 0:
        return []

    chunk_ranges = chunk_source_lines_preserving_tables(
        source_lines,
        target_size=target_lines,
        max_extra=max_extra,
    )

    global_chunks: list[tuple[int, int]] = [
        # Convert zero-based half-open ranges to one-based inclusive ranges.
        (start_idx + 1, end_idx)
        for start_idx, end_idx in chunk_ranges
    ]

    committed: list[ConceptFilePlan] = []
    pending: ConceptFilePlan | None = None

    for chunk_index, (chunk_start, chunk_end) in enumerate(global_chunks, start=1):
        is_final_chunk = chunk_index == len(global_chunks)

        if pending is not None:
            prompt_start = pending.source_start
        else:
            prompt_start = chunk_start

        prompt_end = chunk_end
        label = f"chunk {chunk_index}/{len(global_chunks)}"

        split = await split_window_until_valid(
            llm=llm,
            source_lines=source_lines,
            source_start=prompt_start,
            source_end=prompt_end,
            pending=pending,
            label=label,
        )

        if not split:
            raise RuntimeError(f"{label}: valid split unexpectedly returned no files.")

        if is_final_chunk:
            committed.extend(split)
            pending = None
        else:
            committed.extend(split[:-1])
            pending = split[-1]

            print(
                "[Planning] Carrying pending concept forward: "
                f"{pending.title} [{pending.source_start}-{pending.source_end}]"
            )

    if pending is not None:
        committed.append(pending)

    accepted, error = validate_concept_partition(
        files=committed,
        source_lines=source_lines,
        source_start=1,
        source_end=source_line_count,
        label="final full-source concept plan",
    )

    if accepted is None:
        raise RuntimeError(f"Final concept plan failed validation: {error}")

    assert_concept_coverage(
        files=accepted,
        source_line_count=source_line_count,
    )

    return accepted


# ---------------------------------------------------------------------
# Plan output
# ---------------------------------------------------------------------


def write_concept_plan_document(
    *,
    path: Path,
    source_path: Path,
    source_line_count: int,
    files: list[ConceptFilePlan],
) -> None:
    """
    Writes a human-readable Markdown planning file.

    This is stored in _planning only.
    It is not one of the final docs.
    """

    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        "# Concept File Plan",
        "",
        f"- Source: `{source_path}`",
        f"- Source lines: `{source_line_count}`",
        f"- Concept files: `{len(files)}`",
        "",
        "## Files",
        "",
    ]

    for index, item in enumerate(files, start=1):
        summary = f" — {item.summary}" if item.summary else ""

        lines.append(
            f"{index}. `{item.source_start}-{item.source_end}` → "
            f"`{item.filename}` — **{item.title}**{summary}"
        )

    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------


def manifest_add_file_record(
    *,
    manifest: dict[str, Any],
    filename: str,
    title: str,
    summary: str,
    source_start: int,
    source_end: int,
    order: int,
) -> None:
    """
    Store document metadata in manifest.json.

    This is where title, summary, range, and doc order live.
    The generated Markdown doc itself remains raw source content only.
    """

    record = {
        "order": order,
        "filename": filename,
        "title": title,
        "summary": summary,
        "source_start": source_start,
        "source_end": source_end,
        "updated_at": utc_now_iso(),
    }

    files = manifest.setdefault("files", [])

    if isinstance(files, list):
        files.append(record)
    elif isinstance(files, dict):
        files[filename] = record
    else:
        manifest["files"] = [record]


def manifest_add_chunk_record(
    *,
    manifest: dict[str, Any],
    filename: str,
    source_start: int,
    source_end: int,
    order: int,
) -> None:
    """
    Store source coverage assignment in manifest.json.
    """

    record = {
        "order": order,
        "source_start": source_start,
        "source_end": source_end,
        "action": "concept_file_created",
        "targets": [
            {
                "filename": filename,
                "source_start": source_start,
                "source_end": source_end,
            }
        ],
        "reason": "Streaming concept split.",
        "updated_at": utc_now_iso(),
    }

    chunks = manifest.setdefault("chunks", [])

    if isinstance(chunks, list):
        chunks.append(record)
    else:
        manifest["chunks"] = [record]


# ---------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------


def write_concept_markdown_file(
    *,
    path: Path,
    source_body: str,
) -> None:
    """
    Writes one final Markdown doc.

    IMPORTANT:
    - No frontmatter.
    - No generated heading.
    - No source line text.
    - No summary.
    - Only the original source slice.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source_body, encoding="utf-8")


def render_concept_files(
    *,
    docs_dir: Path,
    source_lines: list[str],
    files: list[ConceptFilePlan],
    manifest: dict[str, Any],
    output_root: Path,
) -> list[dict[str, Any]]:
    """
    Render final concept files to docs/.

    Filenames get source-order numeric prefixes.

    Returns coverage records.
    """

    docs_dir.mkdir(parents=True, exist_ok=True)

    ordered_files = sorted(
        files,
        key=lambda item: (item.source_start, item.source_end),
    )

    total = len(ordered_files)
    coverage: list[dict[str, Any]] = []

    for index, item in enumerate(ordered_files, start=1):
        normalized = normalize_plan_item(item)

        base_filename = normalize_filename(
            filename=normalized.filename,
            title=normalized.title,
        )

        final_filename = numbered_output_filename(
            index=index,
            total=total,
            filename=base_filename,
        )

        output_path = make_unique_output_path(docs_dir, final_filename)
        relative_filename = output_path.relative_to(output_root).as_posix()

        selected_lines = source_lines[
            normalized.source_start - 1 : normalized.source_end
        ]

        source_body = join_original_source_lines(selected_lines)

        write_concept_markdown_file(
            path=output_path,
            source_body=source_body,
        )

        summary = (
            normalized.summary
            or f"Source lines {normalized.source_start}-{normalized.source_end}."
        )

        manifest_add_file_record(
            manifest=manifest,
            filename=relative_filename,
            title=normalized.title,
            summary=summary,
            source_start=normalized.source_start,
            source_end=normalized.source_end,
            order=index,
        )

        manifest_add_chunk_record(
            manifest=manifest,
            filename=relative_filename,
            source_start=normalized.source_start,
            source_end=normalized.source_end,
            order=index,
        )

        coverage.append(
            {
                "order": index,
                "source_start": normalized.source_start,
                "source_end": normalized.source_end,
                "filename": relative_filename,
                "title": normalized.title,
                "summary": summary,
            }
        )

    return coverage

# ---------------------------------------------------------------------
# Enrichment Schemas & Prompts (Sequential Headers & Global Filename)
# ---------------------------------------------------------------------


class FileHeader(BaseModel):
    header: str = Field(
        description="Inferred logical heading (up to 2 levels) for this chunk."
    )


class EnrichmentResult(BaseModel):
    inferred_file_name: str = Field(
        description="A descriptive, slugified global filename for the entire document (e.g., 'moove_configuration_control_manual.md')."
    )
    files: list[FileHeader] = Field(
        description="List of inferred headers in the EXACT same order as the input chunks."
    )


class ChunkHeader(BaseModel):
    header: str = Field(
        description="The logical heading for this chunk. If it continues the previous section, output the EXACT SAME header. Otherwise, output a new heading (up to 2 levels)."
    )


class GlobalName(BaseModel):
    inferred_file_name: str = Field(
        description="A descriptive, slugified global filename for the entire document (must end in .md)."
    )


def build_first_chunk_prompt(original_filename: str, current: ConceptFilePlan) -> list[Any]:
    return [
        SystemMessage(content="You are an expert technical writer assigning logical headings to document chunks."),
        HumanMessage(content=(
            f"Original document name: '{original_filename}'\n\n"
            f"First chunk details:\n"
            f"- Filename: {current.filename}\n"
            f"- Title: {current.title}\n"
            f"- Summary: {current.summary}\n\n"
            "Task: Assign a logical heading (up to 2 levels) for this first chunk. "
            "Use descriptive names (e.g., 'Document Administration', 'API Reference'). "
            "Do not use sequential numbers like '1.' or '2.'."
        ))
    ]


def build_subsequent_chunk_prompt(
    original_filename: str,
    current: ConceptFilePlan,
    prev: ConceptFilePlan,
    prev_header: str,
) -> list[Any]:
    return [
        SystemMessage(content="You are an expert technical writer assigning logical headings to document chunks sequentially."),
        HumanMessage(content=(
            f"Original document name: '{original_filename}'\n\n"
            f"Previous chunk details:\n"
            f"- Title: {prev.title}\n"
            f"- Summary: {prev.summary}\n"
            f"- Assigned Header: {prev_header}\n\n"
            f"Current chunk details:\n"
            f"- Filename: {current.filename}\n"
            f"- Title: {current.title}\n"
            f"- Summary: {current.summary}\n\n"
            "Task: Determine the logical heading for the current chunk.\n"
            "1. If the current chunk is a continuation of the same logical section as the previous chunk "
            "(e.g., another API function in the same API Reference section, or another definition file), "
            "you MUST output the EXACT SAME header as the previous chunk.\n"
            "2. If it starts a new logical section, output a new descriptive heading (up to 2 levels).\n"
            "Do not use sequential numbers."
        ))
    ]


def build_global_name_prompt(original_filename: str, files: list[ConceptFilePlan]) -> list[Any]:
    summaries = "\n".join(f"- {f.title}: {f.summary}" for f in files)
    return [
        SystemMessage(content="You are an expert technical writer."),
        HumanMessage(content=(
            f"The original uploaded file was named: '{original_filename}'.\n\n"
            "Below is a list of document chunks with their titles and summaries:\n"
            f"{summaries}\n\n"
            "Task: Infer a single, descriptive, slugified global filename for the ENTIRE document (must end in .md). "
            "Reflect the actual content, not just the original filename."
        ))
    ]


async def enrich_concept_plan(
    *,
    llm: ChatOpenAI,
    original_filename: str,
    files: list[ConceptFilePlan],
) -> EnrichmentResult:
    """
    Sequentially infers headers for each file and a global filename.
    """
    if not files:
        return EnrichmentResult(inferred_file_name="document.md", files=[])

    print(f"[Enrichment] Inferring global name...")
    try:
        global_name_raw = await structured_ainvoke(
            llm, GlobalName, build_global_name_prompt(original_filename, files), max_output_tokens=100
        )
        global_name = GlobalName.model_validate(global_name_raw).inferred_file_name
    except Exception as e:
        print(f"[Enrichment] Failed to infer global name: {e}. Using fallback.")
        global_name = "document.md"
        
    if not global_name.endswith(".md"):
        global_name += ".md"
    global_name = normalize_filename(global_name, "document")

    inferred_headers = []
    
    # First chunk
    print(f"[Enrichment] Inferring header for chunk 1/{len(files)}...")
    first_raw = await structured_ainvoke(
        llm, ChunkHeader, build_first_chunk_prompt(original_filename, files[0]), max_output_tokens=100
    )
    first_header = ChunkHeader.model_validate(first_raw).header
    inferred_headers.append(first_header)

    # Subsequent chunks
    for i in range(1, len(files)):
        print(f"[Enrichment] Inferring header for chunk {i+1}/{len(files)}...")
        prompt = build_subsequent_chunk_prompt(
            original_filename=original_filename,
            current=files[i],
            prev=files[i-1],
            prev_header=inferred_headers[-1],
        )
        raw = await structured_ainvoke(llm, ChunkHeader, prompt, max_output_tokens=100)
        header = ChunkHeader.model_validate(raw).header
        inferred_headers.append(header)

    return EnrichmentResult(
        inferred_file_name=global_name,
        files=[FileHeader(header=h) for h in inferred_headers]
    )


# ---------------------------------------------------------------------
# New JSON Writers
# ---------------------------------------------------------------------


def write_coverage_json(
    *,
    path: Path,
    source_line_count: int,
    files: list[ConceptFilePlan],
    headers: list[str],
) -> None:
    """
    Writes coverage.json.
    Contains detailed source mapping, summaries, and inferred headers.
    """
    file_records = []
    for item, header in zip(files, headers):
        record = item.model_dump()
        record["header"] = header
        file_records.append(record)

    write_json(
        path,
        {
            "source_line_count": source_line_count,
            "file_count": len(file_records),
            "files": file_records,
        },
    )


def write_metadata_json(
    *,
    path: Path,
    original_file_name: str,
    inferred_file_name: str,
    files: list[ConceptFilePlan],
    headers: list[str],
) -> None:
    """
    Writes metadata.json.
    Lightweight mapping for graphing/renaming.
    """
    file_records = [
        {
            "name": item.filename,
            "header": header,
        }
        for item, header in zip(files, headers)
    ]

    write_json(
        path,
        {
            "original_file_name": original_file_name,
            "inferred_file_name": inferred_file_name,
            "files": file_records,
        },
    )
