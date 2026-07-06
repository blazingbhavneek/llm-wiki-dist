from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from wiki_new.llm import structured_ainvoke
from wiki_new.utils import (
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

    title: str = Field(description="Human-readable concept/topic title.")
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
    summary: str = Field(default="")  # temp: disabled to reduce tokens


class ConceptSplitResult(BaseModel):
    files: list[ConceptFilePlan] = Field(
        description=(
            "Ordered list of concept files. Must exactly cover the requested "
            "source range with no gaps and no overlaps."
        )
    )


class SectionJudgeDecision(BaseModel):
    action: Literal[
        "keep",
        "merge_into_previous",
        "merge_into_next",
        "merge_previous_target_next",
        "adjust_boundary_with_previous",
        "adjust_boundary_with_next",
    ] = "keep"
    new_boundary_line: int | None = None
    reason: str = ""


class StreamingBoundaryJudgeDecision(BaseModel):
    action: Literal[
        "keep",
        "merge_into_previous",
        "adjust_boundary_with_previous",
    ] = "keep"
    new_boundary_line: int | None = None
    reason: str = ""


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

    normalized = [normalize_plan_item(raw_item) for raw_item in files]

    expected_start = source_start

    for index, item in enumerate(normalized, start=1):
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
# Judge
# ---------------------------------------------------------------------


def _section_line_count(plan: ConceptFilePlan) -> int:
    return plan.source_end - plan.source_start + 1


def _section_excerpt(
    plan: ConceptFilePlan,
    source_lines: list[str],
    *,
    full_limit: int = 80,
    head_lines: int = 30,
    tail_lines: int = 20,
) -> str:
    lines = source_lines[plan.source_start - 1 : plan.source_end]
    if len(lines) <= full_limit:
        return numbered_source_lines(lines, plan.source_start)

    head_text = numbered_source_lines(lines[:head_lines], plan.source_start)
    tail_start = plan.source_end - tail_lines + 1
    tail_text = numbered_source_lines(lines[-tail_lines:], tail_start)
    return f"{head_text}\n...\n{tail_text}"


def _boundary_excerpt(
    *,
    files: list[ConceptFilePlan],
    source_lines: list[str],
    target_index: int,
    span: int = 6,
) -> str:
    snippets: list[str] = []

    if target_index > 0:
        previous = files[target_index - 1]
        start = max(previous.source_start, previous.source_end - span + 1)
        snippets.append(
            "previous_tail:\n"
            + numbered_source_lines(
                source_lines[start - 1 : previous.source_end],
                start,
            )
        )

    target = files[target_index]
    end = min(target.source_end, target.source_start + span - 1)
    snippets.append(
        "target_head:\n"
        + numbered_source_lines(
            source_lines[target.source_start - 1 : end],
            target.source_start,
        )
    )

    if target_index + 1 < len(files):
        next_item = files[target_index + 1]
        next_end = min(next_item.source_end, next_item.source_start + span - 1)
        snippets.append(
            "next_head:\n"
            + numbered_source_lines(
                source_lines[next_item.source_start - 1 : next_end],
                next_item.source_start,
            )
        )

    return "\n\n".join(snippets)


def _raw_source_preview(
    *,
    source_lines: list[str],
    start_line: int,
    max_lines: int = 100,
) -> str:
    if start_line > len(source_lines):
        return "<no further source lines>"

    end_line = min(len(source_lines), start_line + max_lines - 1)
    return numbered_source_lines(
        source_lines[start_line - 1 : end_line],
        start_line,
    )


def _section_needs_judgment(
    *,
    files: list[ConceptFilePlan],
    source_lines: list[str],
    index: int,
) -> bool:
    if len(files) < 2 or not (0 <= index < len(files)):
        return False

    del source_lines
    return index > 0 or index < len(files) - 1


def build_section_judge_prompt(
    *,
    files: list[ConceptFilePlan],
    source_lines: list[str],
    target_index: int,
) -> list[Any]:
    previous = files[target_index - 1] if target_index > 0 else None
    target = files[target_index]
    next_item = files[target_index + 1] if target_index + 1 < len(files) else None
    target_line_count = _section_line_count(target)
    previous_line_count = _section_line_count(previous) if previous is not None else 0
    next_line_count = _section_line_count(next_item) if next_item is not None else 0
    merge_previous_total = previous_line_count + target_line_count
    merge_next_total = target_line_count + next_line_count

    valid_actions = ["keep"]
    if previous is not None:
        valid_actions.append("merge_into_previous")
        valid_actions.append("adjust_boundary_with_previous")
    if next_item is not None:
        valid_actions.append("merge_into_next")
        valid_actions.append("adjust_boundary_with_next")
    if previous is not None and next_item is not None:
        valid_actions.append("merge_previous_target_next")

    def section_block(label: str, plan: ConceptFilePlan | None) -> str:
        if plan is None:
            return f"{label}: <missing>\n"
        return (
            f"{label}:\n"
            f"- title: {plan.title}\n"
            f"- filename: {plan.filename}\n"
            f"- range: {plan.source_start}-{plan.source_end}\n"
            f"- line_count: {_section_line_count(plan)}\n"
            f"- summary: {plan.summary}\n"
            f"- text:\n{_section_excerpt(plan, source_lines)}\n"
        )

    return [
        SystemMessage(
            content=(
                "You judge whether a candidate wiki section is useful as a "
                "standalone reader-facing page or should be merged with its "
                "adjacent sections. Prefer fewer, larger, coherent sections. "
                "Do not preserve tiny standalone sections unless they are "
                "clearly useful on their own. Also check boundary hygiene: a "
                "section boundary should be pristine, so one section should not "
                "end with the heading of the next section or begin in the middle "
                "of content whose heading was left behind. Return structured "
                "output only."
            )
        ),
        HumanMessage(
            content=(
                "Task: decide whether the TARGET section should stay separate "
                "or be merged with its immediate neighbors.\n\n"
                "Judging criteria:\n"
                "- A section should be a useful standalone page for a reader.\n"
                "- Tiny sections that are mostly headings, contents lines, API "
                "enumerations, glossary-style entries, or other structural "
                "scaffolding usually should be merged.\n"
                "- Keep a short section only if it contains a genuinely "
                "distinct, self-contained piece of prose or reference content.\n"
                "- Do not over-merge. If keeping the sections separate already "
                "yields coherent reader-facing pages, prefer keep.\n"
                "- Very large pages should be rare. If a merge would create a "
                "much larger page, prefer keep or adjust_boundary unless the "
                "boundary is clearly in the middle of one continuous section.\n"
                "- Boundary hygiene matters: the previous section should not "
                "end with an orphan heading for the target section, and the "
                "target section should not start as body text whose heading was "
                "left in the previous page.\n"
                "- If the boundary is wrong but the two sections are otherwise "
                "distinct, prefer an adjust_boundary action instead of a merge.\n"
                "- If the target begins with trailing continuation from the "
                "previous page but reaches a clear major heading later in its "
                "own range, prefer adjust_boundary so that the new section "
                "starts at that later heading instead of merging the whole "
                "target away.\n"
                "- Merge into previous or next when the target clearly belongs "
                "with that neighboring section.\n"
                "- Merge previous + target + next when the target is just a "
                "bridge between two parts of the same larger section.\n"
                "- Never reorder sections. Only merge contiguous ones.\n\n"
                f"Valid actions for this case: {', '.join(valid_actions)}\n\n"
                "Size snapshot:\n"
                f"- previous_line_count: {previous_line_count}\n"
                f"- target_line_count: {target_line_count}\n"
                f"- next_line_count: {next_line_count}\n"
                f"- merge_previous_total: {merge_previous_total}\n"
                f"- merge_next_total: {merge_next_total}\n\n"
                "For adjust_boundary_with_previous or adjust_boundary_with_next, "
                "set new_boundary_line to the exact source line where the target "
                "section should begin or the next section should begin. The line "
                "must stay within the contiguous previous/target/next neighborhood.\n"
                "- Return only the final decision. Do not narrate your internal "
                "debate. Keep reason to one or two sentences.\n\n"
                f"Boundary snapshot:\n{_boundary_excerpt(files=files, source_lines=source_lines, target_index=target_index)}\n\n"
                f"{section_block('PREVIOUS', previous)}\n"
                f"{section_block('TARGET', target)}\n"
                f"{section_block('NEXT', next_item)}"
            )
        ),
    ]


def build_streaming_boundary_judge_prompt(
    *,
    previous: ConceptFilePlan,
    target: ConceptFilePlan,
    source_lines: list[str],
    lookahead_lines: int = 100,
    target_is_provisional_tail: bool = False,
) -> list[Any]:
    previous_line_count = _section_line_count(previous)
    target_line_count = _section_line_count(target)
    combined_line_count = previous_line_count + target_line_count
    previous_tail_start = max(previous.source_start, previous.source_end - 11)
    target_head_end = min(target.source_end, target.source_start + 11)

    previous_tail = numbered_source_lines(
        source_lines[previous_tail_start - 1 : previous.source_end],
        previous_tail_start,
    )
    target_head = numbered_source_lines(
        source_lines[target.source_start - 1 : target_head_end],
        target.source_start,
    )
    next_preview = _raw_source_preview(
        source_lines=source_lines,
        start_line=target.source_end + 1,
        max_lines=lookahead_lines,
    )
    provisional_tail_text = ""
    if target_is_provisional_tail:
        provisional_tail_text = (
            "- The TARGET in this case is the carry-over tail for the current "
            "chunk. It is not finalized yet and may continue in later source "
            "lines. Do NOT require the TARGET itself to be a complete "
            "standalone page yet. Judge only whether this is the correct place "
            "for the next section to begin.\n"
        )

    return [
        SystemMessage(
            content=(
                "You judge one proposed boundary in a streaming wiki-section "
                "planner. You are checking whether the TARGET section is truly "
                "a fair standalone reader-facing page after the PREVIOUS "
                "accepted section, or whether the PREVIOUS section should keep "
                "absorbing the TARGET. Prefer fewer, larger, coherent sections. "
                "A boundary should be pristine. Return structured output only."
            )
        ),
        HumanMessage(
            content=(
                "Task: decide whether the PREVIOUS accepted section should stop "
                "where it does, or whether the TARGET still belongs with it.\n\n"
                "Judging criteria:\n"
                "- Review this boundary left-to-right as a first-pass streaming "
                "check.\n"
                "- The PREVIOUS section is already the accepted result of all "
                "earlier boundary decisions in this chunk.\n"
                "- Keep the TARGET separate when this boundary already yields "
                "two coherent reader-facing pages.\n"
                "- If the TARGET is still obviously part of the same larger "
                "section, list, TOC, reference block, API family, or contiguous "
                "discussion, merge it into the PREVIOUS section.\n"
                "- Do not over-merge. If PREVIOUS is already substantial, or "
                "if merging would create a much larger page, prefer keep or "
                "adjust_boundary_with_previous unless the current boundary is "
                "clearly in the middle of one continuous section.\n"
                "- Long sections are acceptable. Do not force a split just "
                "because the section has many internal headings or enumerated "
                "items.\n"
                "- If the boundary is wrong but the TARGET should still remain "
                "a distinct page, use adjust_boundary_with_previous with the "
                "exact line where the TARGET should begin.\n"
                "- If the TARGET begins with trailing continuation from the "
                "previous page but reaches a clear new chapter or major section "
                "later in its own range, prefer adjust_boundary_with_previous "
                "to that later heading instead of merging the whole TARGET.\n"
                f"{provisional_tail_text}"
                "- The NEXT RAW SOURCE LINES are only future context. They are "
                "not yet a vetted section plan. Use them to judge whether the "
                "TARGET still looks like it belongs with the PREVIOUS section "
                "or whether it clearly stands on its own.\n"
                "- Do not merge across a clear chapter or top-level section "
                "start merely because the TARGET is provisional.\n"
                "- Never reorder content.\n\n"
                "Valid actions: keep, merge_into_previous, "
                "adjust_boundary_with_previous\n"
                "- Return only the final decision. Do not narrate your internal "
                "debate. Keep reason to one or two sentences.\n\n"
                "Size snapshot:\n"
                f"- previous_line_count: {previous_line_count}\n"
                f"- target_line_count: {target_line_count}\n"
                f"- merged_line_count: {combined_line_count}\n\n"
                "Boundary snapshot:\n"
                f"previous_tail:\n{previous_tail}\n\n"
                f"target_head:\n{target_head}\n\n"
                "PREVIOUS accepted section:\n"
                f"- title: {previous.title}\n"
                f"- filename: {previous.filename}\n"
                f"- range: {previous.source_start}-{previous.source_end}\n"
                f"- line_count: {_section_line_count(previous)}\n"
                f"- summary: {previous.summary}\n"
                f"- text:\n{_section_excerpt(previous, source_lines)}\n\n"
                "TARGET candidate section:\n"
                f"- title: {target.title}\n"
                f"- filename: {target.filename}\n"
                f"- range: {target.source_start}-{target.source_end}\n"
                f"- line_count: {_section_line_count(target)}\n"
                f"- summary: {target.summary}\n"
                f"- text:\n{_section_excerpt(target, source_lines)}\n\n"
                f"NEXT {lookahead_lines} RAW SOURCE LINES:\n{next_preview}"
            )
        ),
    ]


def _apply_section_judge_decision(
    files: list[ConceptFilePlan],
    *,
    target_index: int,
    action: str,
    new_boundary_line: int | None = None,
) -> tuple[list[ConceptFilePlan], bool]:
    if action == "keep" or not (0 <= target_index < len(files)):
        return files, False

    plans = list(files)

    if action == "merge_into_previous":
        if target_index == 0:
            return plans, False
        previous = plans[target_index - 1]
        target = plans[target_index]
        merged = ConceptFilePlan(
            title=previous.title,
            filename=previous.filename,
            source_start=previous.source_start,
            source_end=target.source_end,
            summary=previous.summary or target.summary,
        )
        return plans[: target_index - 1] + [merged] + plans[target_index + 1 :], True

    if action == "merge_into_next":
        if target_index >= len(plans) - 1:
            return plans, False
        target = plans[target_index]
        next_item = plans[target_index + 1]
        merged = ConceptFilePlan(
            title=next_item.title,
            filename=next_item.filename,
            source_start=target.source_start,
            source_end=next_item.source_end,
            summary=next_item.summary or target.summary,
        )
        return plans[:target_index] + [merged] + plans[target_index + 2 :], True

    if action == "merge_previous_target_next":
        if target_index == 0 or target_index >= len(plans) - 1:
            return plans, False
        previous = plans[target_index - 1]
        target = plans[target_index]
        next_item = plans[target_index + 1]
        merged = ConceptFilePlan(
            title=previous.title,
            filename=previous.filename,
            source_start=previous.source_start,
            source_end=next_item.source_end,
            summary=previous.summary or target.summary or next_item.summary,
        )
        return plans[: target_index - 1] + [merged] + plans[target_index + 2 :], True

    if action == "adjust_boundary_with_previous":
        if target_index == 0 or new_boundary_line is None:
            return plans, False
        previous = plans[target_index - 1]
        target = plans[target_index]
        if not (previous.source_start < new_boundary_line <= target.source_end):
            return plans, False
        revised_previous = previous.model_copy(
            update={"source_end": new_boundary_line - 1}
        )
        revised_target = target.model_copy(update={"source_start": new_boundary_line})
        return (
            plans[: target_index - 1]
            + [revised_previous, revised_target]
            + plans[target_index + 1 :],
            True,
        )

    if action == "adjust_boundary_with_next":
        if target_index >= len(plans) - 1 or new_boundary_line is None:
            return plans, False
        target = plans[target_index]
        next_item = plans[target_index + 1]
        if not (target.source_start < new_boundary_line <= next_item.source_end):
            return plans, False
        revised_target = target.model_copy(update={"source_end": new_boundary_line - 1})
        revised_next = next_item.model_copy(update={"source_start": new_boundary_line})
        return (
            plans[:target_index]
            + [revised_target, revised_next]
            + plans[target_index + 2 :],
            True,
        )

    return plans, False


async def judge_concept_plan(
    *,
    llm: ChatOpenAI,
    source_lines: list[str],
    files: list[ConceptFilePlan],
    max_output_tokens: int = 200,
    label: str | None = None,
) -> list[ConceptFilePlan]:
    log_prefix = f"[Judge:{label}]" if label else "[Judge]"
    plans = [normalize_plan_item(item) for item in files]
    if len(plans) < 2:
        print(f"{log_prefix} skipped: fewer than 2 sections", flush=True)
        return plans

    source_start = plans[0].source_start
    source_end = plans[-1].source_end
    reviewed = 0
    kept = 0
    merged = 0
    rejected = 0

    print(
        f"{log_prefix} starting: {len(plans)} section(s), range {source_start}-{source_end}",
        flush=True,
    )

    target_index = 0
    while target_index < len(plans):
        if not _section_needs_judgment(
            files=plans,
            source_lines=source_lines,
            index=target_index,
        ):
            target_index += 1
            continue

        target = plans[target_index]
        reviewed += 1
        print(
            f"{log_prefix} review index={target_index + 1}/{len(plans)} "
            f"title={target.title!r} range={target.source_start}-{target.source_end} "
            f"lines={_section_line_count(target)}",
            flush=True,
        )

        messages = build_section_judge_prompt(
            files=plans,
            source_lines=source_lines,
            target_index=target_index,
        )

        decision_raw = await structured_ainvoke(
            llm,
            SectionJudgeDecision,
            messages,
            max_output_tokens=max_output_tokens,
        )
        decision = SectionJudgeDecision.model_validate(decision_raw)
        print(
            f"{log_prefix} decision action={decision.action} "
            f"new_boundary_line={decision.new_boundary_line} "
            f"reason={decision.reason}",
            flush=True,
        )

        if decision.action == "keep":
            kept += 1
            target_index += 1
            continue

        revised, applied = _apply_section_judge_decision(
            plans,
            target_index=target_index,
            action=decision.action,
            new_boundary_line=decision.new_boundary_line,
        )
        if not applied:
            rejected += 1
            print(
                f"{log_prefix} rejected action={decision.action}: not applicable "
                f"for index={target_index + 1}",
                flush=True,
            )
            target_index += 1
            continue

        accepted, error = validate_concept_partition(
            files=revised,
            source_lines=source_lines,
            source_start=source_start,
            source_end=source_end,
            label="section judge single pass",
        )
        if accepted is None:
            rejected += 1
            print(
                f"{log_prefix} rejected invalid merge action={decision.action} "
                f"at index={target_index}: {error}",
                flush=True,
            )
            target_index += 1
            continue

        merged += 1
        print(
            f"{log_prefix} merged action={decision.action} at index={target_index}: "
            f"{decision.reason}",
            flush=True,
        )
        plans = accepted

        if decision.action == "adjust_boundary_with_previous":
            target_index += 1
            continue

        if decision.action == "adjust_boundary_with_next":
            target_index += 1
            continue

        if target_index >= len(plans):
            break

    print(
        f"{log_prefix} done: reviewed={reviewed} kept={kept} merged={merged} "
        f"rejected={rejected} final_sections={len(plans)}",
        flush=True,
    )
    return plans


async def judge_streaming_boundaries_once(
    *,
    llm: ChatOpenAI,
    source_lines: list[str],
    files: list[ConceptFilePlan],
    max_output_tokens: int = 200,
    lookahead_lines: int = 100,
    provisional_last_section: bool = False,
    label: str | None = None,
) -> list[ConceptFilePlan]:
    log_prefix = f"[Judge:{label}]" if label else "[Judge]"
    plans = [normalize_plan_item(item) for item in files]
    if len(plans) < 2:
        print(f"{log_prefix} skipped: fewer than 2 sections", flush=True)
        return plans

    accepted_prefix: list[ConceptFilePlan] = [plans[0]]
    reviewed = 0
    kept = 0
    merged = 0
    rejected = 0

    print(
        f"{log_prefix} starting streaming boundary review: "
        f"{len(plans)} section(s), range {plans[0].source_start}-{plans[-1].source_end}",
        flush=True,
    )

    last_plan_index = len(plans) - 1

    for plan_index, raw_target in enumerate(plans[1:], start=1):
        previous = accepted_prefix[-1]
        target = normalize_plan_item(raw_target)
        reviewed += 1

        print(
            f"{log_prefix} boundary review previous={previous.source_start}-{previous.source_end} "
            f"target={target.source_start}-{target.source_end}",
            flush=True,
        )

        messages = build_streaming_boundary_judge_prompt(
            previous=previous,
            target=target,
            source_lines=source_lines,
            lookahead_lines=lookahead_lines,
            target_is_provisional_tail=(
                provisional_last_section and plan_index == last_plan_index
            ),
        )

        decision_raw = await structured_ainvoke(
            llm,
            StreamingBoundaryJudgeDecision,
            messages,
            max_output_tokens=max_output_tokens,
        )
        decision = StreamingBoundaryJudgeDecision.model_validate(decision_raw)
        print(
            f"{log_prefix} decision action={decision.action} "
            f"new_boundary_line={decision.new_boundary_line} "
            f"reason={decision.reason}",
            flush=True,
        )

        if decision.action == "keep":
            kept += 1
            accepted_prefix.append(target)
            continue

        revised_pair, applied = _apply_section_judge_decision(
            [previous, target],
            target_index=1,
            action=decision.action,
            new_boundary_line=decision.new_boundary_line,
        )
        if not applied:
            rejected += 1
            print(
                f"{log_prefix} rejected action={decision.action}: not applicable "
                f"for target={target.source_start}-{target.source_end}",
                flush=True,
            )
            accepted_prefix.append(target)
            continue

        pair_accepted, error = validate_concept_partition(
            files=revised_pair,
            source_lines=source_lines,
            source_start=previous.source_start,
            source_end=target.source_end,
            label="streaming boundary judge",
        )
        if pair_accepted is None:
            rejected += 1
            print(
                f"{log_prefix} rejected invalid action={decision.action}: {error}",
                flush=True,
            )
            accepted_prefix.append(target)
            continue

        merged += 1
        accepted_prefix = accepted_prefix[:-1] + pair_accepted
        print(
            f"{log_prefix} applied action={decision.action}: {decision.reason}",
            flush=True,
        )

    print(
        f"{log_prefix} done: reviewed={reviewed} kept={kept} merged={merged} "
        f"rejected={rejected} final_sections={len(accepted_prefix)}",
        flush=True,
    )
    return accepted_prefix


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
            "There is one pending section from the previous chunk.\n"
            "It has NOT been saved yet.\n"
            "You are allowed to continue it, rename it, keep it, or split it, "
            "but your returned ranges must still exactly cover the full requested "
            "range below.\n\n"
            f"Pending section title: {pending.title}\n"
            f"Pending section filename: {pending.filename}\n"
            f"Pending section source range: "
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
                "You split numbered source lines into logical reader-facing "
                "document sections. "
                "Do NOT atomize the document into every possible concept. "
                "Only identify strong section boundaries that would make sense "
                "as standalone pages for a reader. "
                "Think briefly — do not overthink. "
                "Return structured output only."
            )
        ),
        HumanMessage(
            content=(
                f"{correction_text}"
                "Task: split the requested numbered source lines into logical "
                "document sections as Markdown files.\n\n"
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
                "- Prefer major section or chapter boundaries over lower-level "
                "subheadings.\n"
                "- Prefer splits at strong reader-facing transitions such as a "
                "new major section, a clearly separate API family, or a new "
                "mode of the document.\n"
                "- Keep long contiguous ranges together when they are still one "
                "logical section, even if they contain many subheadings, lists, "
                "enumerations, references, glossary entries, or other internal "
                "structure.\n"
                "- Be conservative: each file must contain substantial content. "
                "Long ranges are acceptable. Aim for roughly 100-400+ lines when "
                "the material remains one coherent section.\n"
                "- Do not let one page grow without bound. A page much beyond "
                "roughly 500-700 lines should be rare and only happen when the "
                "material is still one exceptionally cohesive section.\n"
                "- Do NOT split into tiny fragments just because a new heading "
                "appears or because a local concept changes briefly.\n"
                "- If a carried-over section is already substantial, stop it at "
                "the next strong chapter or major-section boundary instead of "
                "continuing to absorb later material.\n"
                "- Do NOT create continuation pages merely because a chunk "
                "boundary was reached. Only make a part 2 / continued page when "
                "the section is genuinely too large or too heterogeneous to stay "
                "useful as one reader-facing page.\n"
                "- Fewer larger files is better than many tiny files.\n"
                "- You MAY return a single file covering the entire requested "
                "range if it still behaves like one section, including front "
                "matter, navigation-like material, appendices, lists, or other "
                "scaffolding that should stay grouped until substantive section "
                "boundaries appear.\n"
                "- Filename must be a plain Markdown filename only, for example "
                "function_mpf_mfs_open.md.\n"
                "- Do not include directories in filename.\n\n"
                "Carry-over rule:\n"
                "- If the last section looks incomplete and probably continues in "
                "the next chunk, still include it as the last returned file.\n"
                "- The caller will keep the last returned file pending and show it "
                "again with the next chunk.\n\n"
                "New source lines (lines before this are covered by the pending "
                "section above, if any):\n"
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
    display_start: int,
    pending: ConceptFilePlan | None,
    label: str,
    max_output_tokens: int = 3000,
) -> list[ConceptFilePlan]:
    """
    Calls the model until it returns exact coverage for source_start-source_end.

    display_start: first line to actually show in the prompt (always chunk_start).
    source_start:  validation range start (may be further back due to pending).
    Only new lines are fed to the model; pending metadata covers the prior range.
    """

    source_block = numbered_source_lines(
        source_lines[display_start - 1 : source_end],
        display_start,
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
    judge_llm: ChatOpenAI | None = None,
    source_lines: list[str],
    target_lines: int = 100,
    max_extra: int = 30,
    on_commit=None,
    resume_files: list[ConceptFilePlan] | None = None,
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

    committed: list[ConceptFilePlan] = list(resume_files) if resume_files else []
    last_covered = committed[-1].source_end if committed else 0
    pending: ConceptFilePlan | None = None
    resumed = False

    for chunk_index, (chunk_start, chunk_end) in enumerate(global_chunks, start=1):
        is_final_chunk = chunk_index == len(global_chunks)

        if chunk_end <= last_covered:
            print(
                f"[Planning] chunk {chunk_index}/{len(global_chunks)}: skipping (covered through line {last_covered})"
            )
            continue

        if not resumed and last_covered > 0:
            prompt_start = last_covered + 1
            resumed = True
        elif pending is not None:
            prompt_start = pending.source_start
        else:
            prompt_start = chunk_start

        prompt_end = chunk_end
        label = f"chunk {chunk_index}/{len(global_chunks)}"

        display_start = max(prompt_start, chunk_start - target_lines)

        split = await split_window_until_valid(
            llm=llm,
            source_lines=source_lines,
            source_start=prompt_start,
            source_end=prompt_end,
            display_start=display_start,
            pending=pending,
            label=label,
        )

        if not split:
            raise RuntimeError(f"{label}: valid split unexpectedly returned no files.")

        if judge_llm is not None and len(split) > 1:
            print(
                f"[Planning] {label}: running streaming boundary judge on {len(split)} section(s)",
                flush=True,
            )
            split = await judge_streaming_boundaries_once(
                llm=judge_llm,
                source_lines=source_lines,
                files=split,
                provisional_last_section=not is_final_chunk,
                label=f"{label} streaming",
            )

        if is_final_chunk:
            if on_commit:
                on_commit(split)
            committed.extend(split)
            pending = None
        else:
            new_committed = split[:-1]
            if on_commit and new_committed:
                on_commit(new_committed)
            committed.extend(new_committed)
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
    for existing in docs_dir.glob("*.md"):
        existing.unlink()

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

        output_path = docs_dir / final_filename
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


def build_first_chunk_prompt(
    original_filename: str, current: ConceptFilePlan
) -> list[Any]:
    return [
        SystemMessage(
            content="You are an expert technical writer assigning logical headings to document chunks."
        ),
        HumanMessage(
            content=(
                f"Original document name: '{original_filename}'\n\n"
                f"First chunk details:\n"
                f"- Filename: {current.filename}\n"
                f"- Title: {current.title}\n"
                f"- Summary: {current.summary}\n\n"
                "Task: Assign a logical heading (up to 2 levels) for this first chunk. "
                "Use descriptive names (e.g., 'Document Administration', 'API Reference'). "
                "Do not use sequential numbers like '1.' or '2.'."
            )
        ),
    ]


def build_subsequent_chunk_prompt(
    original_filename: str,
    current: ConceptFilePlan,
    prev: ConceptFilePlan,
    prev_header: str,
) -> list[Any]:
    return [
        SystemMessage(
            content="You are an expert technical writer assigning logical headings to document chunks sequentially."
        ),
        HumanMessage(
            content=(
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
            )
        ),
    ]


def build_global_name_prompt(
    original_filename: str, files: list[ConceptFilePlan]
) -> list[Any]:
    summaries = "\n".join(f"- {f.title}: {f.summary}" for f in files)
    return [
        SystemMessage(content="You are an expert technical writer."),
        HumanMessage(
            content=(
                f"The original uploaded file was named: '{original_filename}'.\n\n"
                "Below is a list of document chunks with their titles and summaries:\n"
                f"{summaries}\n\n"
                "Task: Infer a single, descriptive, slugified global filename for the ENTIRE document (must end in .md). "
                "Reflect the actual content, not just the original filename."
            )
        ),
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
            llm,
            GlobalName,
            build_global_name_prompt(original_filename, files),
            max_output_tokens=100,
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
        llm,
        ChunkHeader,
        build_first_chunk_prompt(original_filename, files[0]),
        max_output_tokens=100,
    )
    first_header = ChunkHeader.model_validate(first_raw).header
    inferred_headers.append(first_header)

    # Subsequent chunks
    for i in range(1, len(files)):
        print(f"[Enrichment] Inferring header for chunk {i+1}/{len(files)}...")
        prompt = build_subsequent_chunk_prompt(
            original_filename=original_filename,
            current=files[i],
            prev=files[i - 1],
            prev_header=inferred_headers[-1],
        )
        raw = await structured_ainvoke(llm, ChunkHeader, prompt, max_output_tokens=100)
        header = ChunkHeader.model_validate(raw).header
        inferred_headers.append(header)

    return EnrichmentResult(
        inferred_file_name=global_name,
        files=[FileHeader(header=h) for h in inferred_headers],
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
    original_source: str | None = None,
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

    payload = {
        "original_file_name": original_file_name,
        "inferred_file_name": inferred_file_name,
        "files": file_records,
    }
    if original_source:
        payload["original_source"] = original_source

    write_json(path, payload)
