"""
Pipeline driver: source markdown text in, ingestable output directory out.

Output directory layout (the exact contract graph.librarian's
_load_new_planning_docs_output already ingests — no graph changes needed):

    out_dir/
      docs/001-<slug>.md        exact source line slices, byte-verified
      _planning/coverage.json   {files: [{title, filename, source_start, ...}]}
      _planning/metadata.json   {original_file_name, inferred_file_name, files}
      _planning/ablation.json   what actually ran: config, signals, timings

The LLM (optional) is any object exposing:
    complete_structured(system, user, pydantic_model) -> model instance
    complete(system, user) -> str
Pass None to run fully deterministic (signals + DP only).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .adjudicate import adjudicate
from .assemble import PlannedChunk, assemble_partition
from .config import ChunkConfig
from .signals import build_lattice
from .skeleton import run_skeleton

log = logging.getLogger("chunker")

ProgressFn = Callable[[dict], None]


@dataclass
class ChunkResult:
    out_dir: str
    file_count: int
    source_line_count: int
    llm_calls: int
    ablation: dict = field(default_factory=dict)


# ---------------------------------------------------------------------
# naming helpers
# ---------------------------------------------------------------------

_ATX_RE = re.compile(r"^(#{1,6})\s+(.*\S)")


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s.-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-.")
    return text[:60] or "section"


def _title_for_slice(lines: list[str]) -> str:
    for line in lines:
        match = _ATX_RE.match(line)
        if match:
            return match.group(2).strip()
    for line in lines:
        stripped = line.strip()
        if stripped:
            return stripped[:60]
    return "Untitled"


def _headers_for_chunks(
    chunks: list[PlannedChunk],
    lines: list[str],
    top_level_boundaries: list[int],
) -> list[str]:
    """Cluster header per chunk = title of the enclosing top-level section
    (from the skeleton's picks; fallback: the first H1/H2 above the chunk).
    Deterministic — replaces the old per-chunk LLM header chain."""
    section_titles: list[tuple[int, str]] = []  # (start_line_1based, title)
    for boundary in sorted(top_level_boundaries):
        start = boundary + 1
        if start <= len(lines):
            section_titles.append((start, _title_for_slice(lines[start - 1 : start + 3])))

    if not section_titles:
        # fallback: every H1/H2 in the doc
        for index, line in enumerate(lines, start=1):
            match = _ATX_RE.match(line)
            if match and len(match.group(1)) <= 2:
                section_titles.append((index, match.group(2).strip()))

    # The document start is always a section start even though it is never a
    # boundary candidate; without this the first chunks fall into "General".
    if not section_titles or section_titles[0][0] > 1:
        section_titles.insert(0, (1, _title_for_slice(lines[:4])))

    headers = []
    for chunk in chunks:
        header = "General"
        for start, title in section_titles:
            if start <= chunk.source_start:
                header = title
            else:
                break
        headers.append(header)
    return headers


def _unique(name: str, seen: set[str]) -> str:
    if name not in seen:
        seen.add(name)
        return name
    counter = 2
    while f"{name}-{counter}" in seen:
        counter += 1
    final = f"{name}-{counter}"
    seen.add(final)
    return final


# ---------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------


def run_chunk_pipeline(
    source_text: str,
    document_name: str,
    out_dir: str | Path,
    config: ChunkConfig | None = None,
    llm=None,
    on_progress: ProgressFn | None = None,
) -> ChunkResult:
    config = config or ChunkConfig.from_env()
    out_path = Path(out_dir)

    def progress(stage: str, **extra) -> None:
        if on_progress:
            try:
                on_progress({"stage": stage, **extra})
            except Exception:
                pass

    started = time.time()
    timings: dict[str, float] = {}
    lines = source_text.splitlines()
    n = len(lines)
    if n == 0:
        raise ValueError("source document is empty")

    # --- stages 0+1: legality mask + fused boundary lattice (no LLM) ---
    progress("chunking:signals", lines=n)
    t0 = time.time()
    fused, legal, signal_report = build_lattice(lines, config)
    timings["signals"] = round(time.time() - t0, 2)

    # --- stage 2: skeleton + selection-only LLM labeling ---
    progress("chunking:skeleton")
    t0 = time.time()
    skeleton = run_skeleton(lines, fused, config, llm=llm)
    timings["skeleton"] = round(time.time() - t0, 2)

    # --- stage 3: global DP assembly (no LLM, valid by construction) ---
    progress("chunking:assemble")
    t0 = time.time()
    chunks = assemble_partition(n, fused, legal, skeleton.candidates, config)
    timings["assemble"] = round(time.time() - t0, 2)

    # --- stage 4: bounded yes/no adjudication of weak cuts ---
    progress("chunking:adjudicate", files=len(chunks))
    t0 = time.time()
    chunks, adjudication_report = adjudicate(chunks, lines, config, llm=llm)
    timings["adjudicate"] = round(time.time() - t0, 2)

    # --- stage 5: deterministic render + byte verification ---
    progress("chunking:render", files=len(chunks))
    if out_path.exists():
        shutil.rmtree(out_path)
    docs_dir = out_path / "docs"
    planning_dir = out_path / "_planning"
    docs_dir.mkdir(parents=True, exist_ok=True)
    planning_dir.mkdir(parents=True, exist_ok=True)

    top_level_boundaries = [c.boundary for c in skeleton.candidates if c.top_level]
    headers = _headers_for_chunks(chunks, lines, top_level_boundaries)

    seen_slugs: set[str] = set()
    coverage_files = []
    metadata_files = []
    total = len(chunks)

    for index, (chunk, header) in enumerate(zip(chunks, headers), start=1):
        slice_lines = lines[chunk.source_start - 1 : chunk.source_end]
        body = "\n".join(slice_lines)
        title = _title_for_slice(slice_lines)
        slug = _unique(_slugify(title), seen_slugs)
        canonical = f"{slug}.md"
        disk_name = f"{index:03d}-{slug}.md"

        file_path = docs_dir / disk_name
        file_path.write_text(body, encoding="utf-8")

        # byte-for-byte verification against the source slice
        if file_path.read_text(encoding="utf-8") != body:
            raise RuntimeError(f"rendered file does not match source slice: {disk_name}")

        coverage_files.append(
            {
                "title": title,
                "filename": canonical,
                "source_start": chunk.source_start,
                "source_end": chunk.source_end,
                "summary": "",
                "header": header,
            }
        )
        metadata_files.append({"name": canonical, "header": header})

    # exact-coverage assertion (guaranteed by the DP; assert anyway)
    expected = 1
    for record in coverage_files:
        if record["source_start"] != expected:
            raise RuntimeError("coverage gap/overlap in chunker output")
        expected = record["source_end"] + 1
    if expected != n + 1:
        raise RuntimeError("coverage incomplete in chunker output")

    (planning_dir / "coverage.json").write_text(
        json.dumps(
            {"source_line_count": n, "file_count": total, "files": coverage_files},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (planning_dir / "metadata.json").write_text(
        json.dumps(
            {
                "original_file_name": document_name,
                "inferred_file_name": document_name,
                "files": metadata_files,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ablation = {
        "config": config.to_dict(),
        "signals": signal_report,
        "skeleton_llm_calls": skeleton.llm_calls,
        "skeleton_notes": skeleton.notes,
        "adjudication": adjudication_report,
        "timings_seconds": timings,
        "total_seconds": round(time.time() - started, 2),
        "file_count": total,
        "source_line_count": n,
    }
    (planning_dir / "ablation.json").write_text(
        json.dumps(ablation, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    log.info(
        "chunker: %s -> %d files in %.1fs (llm calls: %d)",
        document_name,
        total,
        ablation["total_seconds"],
        skeleton.llm_calls,
    )
    return ChunkResult(
        out_dir=str(out_path),
        file_count=total,
        source_line_count=n,
        llm_calls=skeleton.llm_calls,
        ablation=ablation,
    )
