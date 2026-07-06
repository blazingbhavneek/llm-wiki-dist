"""
Pure helper layer: text/line-range, file & JSON IO, filename/slug, source
chunking, and manifest/markdown-file-record helpers. Leaf module; imports
nothing from wiki. Everything above depends on these primitives.

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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------

def range_to_markdown(
    source_lines: list[str],
    source_range: Optional[list[int]],
) -> str:
    """
    Convert an inclusive 1-based source range [start, end] to raw Markdown.

    Important:
    - Uses original source lines.
    - Does NOT include line number prefixes.
    """
    if not source_range or len(source_range) != 2:
        return ""

    start, end = int(source_range[0]), int(source_range[1])

    if start > end:
        return ""

    # Convert 1-based inclusive to Python slice.
    return "\n".join(source_lines[start - 1 : end])


def clamp_range_to_chunk(
    source_range: Optional[list[int]],
    chunk_start: int,
    chunk_end: int,
) -> Optional[list[int]]:
    """
    Clamp an LLM-provided inclusive source range to the current chunk.
    """
    if not source_range or len(source_range) != 2:
        return None

    start, end = int(source_range[0]), int(source_range[1])

    start = max(start, chunk_start)
    end = min(end, chunk_end)

    if start > end:
        return None

    return [start, end]


def full_chunk_range(source_start: int, source_end: int) -> list[int]:
    return [source_start, source_end]


def split_chunk_ranges(source_start: int, source_end: int) -> tuple[list[int], list[int]]:
    midpoint = source_start + ((source_end - source_start) // 2)
    return [source_start, midpoint], [midpoint + 1, source_end]

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def count_file_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def ensure_md_suffix(filename: str) -> str:
    filename = filename.strip()
    if not filename.endswith(".md"):
        filename += ".md"
    return filename


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s.-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    text = text.strip("-.")
    return text or "untitled"


def clean_filename_hint(filename: str, title: str) -> str:
    filename = filename.strip()
    if filename:
        stem = Path(filename).stem
    else:
        stem = title

    stem = re.sub(r"^\d{3,}-", "", stem)
    stem = slugify(stem)

    return ensure_md_suffix(stem)


def make_numbered_filename(index: int, filename_hint: str, title: str) -> str:
    clean = clean_filename_hint(filename_hint, title)
    stem = Path(clean).stem
    return f"{index:03d}-{stem}.md"


def get_next_file_index(out_dir: Path) -> int:
    max_index = 0
    for path in out_dir.glob("*.md"):
        match = re.match(r"^(\d{3,})-", path.name)
        if match:
            max_index = max(max_index, int(match.group(1)))
    return max_index + 1


def make_unique_filename(out_dir: Path, index: int, filename_hint: str, title: str) -> str:
    filename = make_numbered_filename(index, filename_hint, title)
    path = out_dir / filename

    if not path.exists():
        return filename

    stem = Path(filename).stem
    suffix = Path(filename).suffix

    counter = 2
    while True:
        candidate = f"{stem}-{counter}{suffix}"
        if not (out_dir / candidate).exists():
            return candidate
        counter += 1


def append_markdown(path: Path, markdown: str) -> int:
    markdown = markdown.strip()
    if not markdown:
        return 0

    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    separator = "\n\n" if existing and not existing.endswith("\n\n") else ""
    path.write_text(existing + separator + markdown + "\n", encoding="utf-8")

    return len(markdown.splitlines())


def numbered_source_lines(lines: list[str], start_line_number: int) -> str:
    cleaned_lines = []
    for line in lines:
        # Strip base64 data from img tags before sending to the LLM
        if "data:image/" in line:
            line = re.sub(r'src=["\']data:image/[^"\']+["\']', 'src=""', line)
        cleaned_lines.append(line)
        
    return "\n".join(
        f"{start_line_number + i}: {line}" for i, line in enumerate(cleaned_lines)
    )


def last_n_lines_from_file(path: Path, n: int = 100) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[-n:])


def extract_json_from_text(text: str) -> Any:
    """
    Fallback parser for models that return JSON wrapped in prose or fences.
    """

    text = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    first = text.find("{")
    last = text.rfind("}")

    if first != -1 and last != -1 and last > first:
        return json.loads(text[first : last + 1])

    raise ValueError("Could not extract valid JSON from LLM response.")

# ---------------------------------------------------------------------
# Markdown chunking
# ---------------------------------------------------------------------


def is_fence_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("```") or stripped.startswith("~~~")


def is_tableish_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False

    if stripped.startswith("|") and "|" in stripped[1:]:
        return True

    if re.match(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$", stripped):
        return True

    return False


def chunk_source_lines_preserving_tables(
    lines: list[str],
    target_size: int = 100,
    max_extra: int = 30,
) -> list[tuple[int, int]]:
    """
    Returns zero-based half-open ranges: [(start, end), ...]

    Tries to avoid cutting inside fenced code blocks, Markdown tables,
    and <image-unit> blocks.
    """

    chunks: list[tuple[int, int]] = []
    n = len(lines)
    start = 0

    # Precompute states to correctly handle blocks spanning across chunk boundaries
    fence_state = []
    in_f = False
    for line in lines:
        if is_fence_line(line):
            in_f = not in_f
        fence_state.append(in_f)

    img_state = []
    in_i = False
    for line in lines:
        if "<image-unit>" in line:
            in_i = True
        if "</image-unit>" in line:
            in_i = False
        img_state.append(in_i)

    while start < n:
        end = min(start + target_size, n)

        extra = 0

        while end < n and extra < max_extra:
            cut_inside_table = (
                end > start
                and (
                    is_tableish_line(lines[end - 1])
                    or is_tableish_line(lines[end])
                )
            )

            # Check if we are inside a fence or an image unit at the proposed cut point.
            # The cut point is after line `end - 1`.
            inside_block = fence_state[end - 1] or img_state[end - 1]

            if not inside_block and not cut_inside_table:
                break

            end += 1
            extra += 1

        chunks.append((start, end))
        start = end

    return chunks


def fixed_windows(lines: list[str], window_size: int = 25) -> list[tuple[int, int]]:
    return [(i, min(i + window_size, len(lines))) for i in range(0, len(lines), window_size)]

# ---------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------


def init_manifest(source_path: Path) -> dict[str, Any]:
    return {
        "source": str(source_path),
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "files": [],
        "chunks": [],
        "planning": {},
        "coverage": [],
        "verification_flags": [],
    }


def find_file_record(manifest: dict[str, Any], filename: str) -> Optional[dict[str, Any]]:
    for record in manifest["files"]:
        if record["filename"] == filename:
            return record
    return None


def add_or_update_file_record(
    manifest: dict[str, Any],
    filename: str,
    title: str,
    summary: str,
    source_start: int,
    source_end: int,
) -> None:
    record = find_file_record(manifest, filename)
    new_range = [source_start, source_end]

    if record is None:
        manifest["files"].append(
            {
                "filename": filename,
                "title": title,
                "summary": summary,
                "source_ranges": [new_range],
                "_merged_source_ranges": [new_range],
            }
        )
    else:
        record["source_ranges"].append(new_range)
        record["source_ranges"].sort(key=lambda r: r[0])

        # Merge overlapping or adjacent ranges
        merged = []
        for start, end in record["source_ranges"]:
            if not merged:
                merged.append([start, end])
            else:
                last = merged[-1]
                if start <= last[1] + 1:
                    last[1] = max(last[1], end)
                else:
                    merged.append([start, end])
                    print(f"[WARNING] Discontinuous source ranges in {filename}: {record['source_ranges']}")
        
        record["_merged_source_ranges"] = merged


def update_markdown_frontmatter(path: Path, new_merged_ranges: list[list[int]]) -> None:
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return  # unexpected format

    # Split into frontmatter and body
    parts = content.split("---", 2)
    if len(parts) < 3:
        return

    _, front_str, body = parts

    # Parse existing frontmatter (simple YAML-like)
    lines = front_str.strip().splitlines()
    new_front_lines = []
    for line in lines:
        if line.startswith("source_lines:"):
            # Replace this line
            new_front_lines.append(f"source_lines: {json.dumps(new_merged_ranges)}")
        else:
            new_front_lines.append(line)

    new_front_str = "\n".join(new_front_lines)
    new_content = f"---\n{new_front_str}\n---\n{body}"

    path.write_text(new_content, encoding="utf-8")


def create_markdown_file(
    path: Path,
    title: str,
    summary: str,
    source_start: int,
    source_end: int,
    body: str,
) -> None:
    title = title.strip() or "Untitled"
    summary = summary.strip()
    # Keep `body` unchanged: leading and trailing blank source lines are owned
    # by this page just as much as non-blank source lines are.

    content = f"""---
title: {yaml_quote(title)}
summary: {yaml_quote(summary)}
source_lines: [[{source_start}, {source_end}]]
---

# {title}

{body}
"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def add_chunk_record(
    manifest: dict[str, Any],
    source_start: int,
    source_end: int,
    action: str,
    targets: list[dict[str, Any]],
    reason: str,
) -> None:
    manifest["chunks"].append(
        {
            "source_start": source_start,
            "source_end": source_end,
            "action": action,
            "targets": targets,
            "reason": reason,
        }
    )
    manifest["updated_at"] = utc_now_iso()


def overlap_size(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start) + 1)


def find_best_target_for_source_window(
    manifest: dict[str, Any],
    source_start: int,
    source_end: int,
) -> Optional[str]:
    """
    Returns the filename with the largest overlap for this source window.
    """

    best_filename: Optional[str] = None
    best_overlap = 0

    for chunk in manifest.get("chunks", []):
        for target in chunk.get("targets", []):
            t_start = int(target["source_start"])
            t_end = int(target["source_end"])
            size = overlap_size(source_start, source_end, t_start, t_end)

            if size > best_overlap:
                best_overlap = size
                best_filename = target["filename"]

    return best_filename
