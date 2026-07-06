"""
Top-level orchestration for the lossless Markdown wiki generator.

This module intentionally does NOT run semantic LLM verification or repair.

Current architecture:
- The LLM is used only to choose concept/file boundaries.
- Python validates exact line coverage.
- Python renders each concept file by copying the original source slice.
- Python verifies rendered docs deterministically.
- Metadata lives in manifest.json and _planning/*.json.
- Final Markdown docs contain only original source content.

Entrypoint:
    ../md.py calls wiki.phases.main
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from wiki.models import (
    API_KEY,
    BASE_URL,
    CLEAN_OUTPUT,
    FILE_CONCURRENCY,
    GEN_MODEL,
    MAX_CHUNK_EXTRA,
    OUTPUT_ROOT,
    PHASE,
    SOURCE_PATH,
    TEMPERATURE,
)

from wiki.llm import make_llm

from wiki.planning import *

from wiki.utils import (
    init_manifest,
    read_lines,
    utc_now_iso,
    write_json,
)


# ---------------------------------------------------------------------
# Deterministic rendered-output integrity check
# ---------------------------------------------------------------------


def assert_rendered_docs_match_source(
    *,
    source_lines: list[str],
    coverage: list[dict[str, Any]],
    output_root: Path,
) -> None:
    """
    Deterministically verify rendered docs.

    This replaces the old LLM verification/repair loop.

    Checks:
    - coverage records are contiguous from line 1 to N
    - no gaps
    - no overlaps
    - every referenced output file exists
    - every output file content exactly equals its assigned source slice

    If this passes, there is nothing to repair.
    """

    source_line_count = len(source_lines)

    if source_line_count == 0:
        if coverage:
            raise RuntimeError("Empty source cannot have rendered coverage records.")
        return

    ordered = sorted(
        coverage,
        key=lambda item: (
            int(item["source_start"]),
            int(item["source_end"]),
        ),
    )

    expected_start = 1

    for index, item in enumerate(ordered, start=1):
        source_start = int(item["source_start"])
        source_end = int(item["source_end"])
        filename = str(item["filename"])

        if source_start != expected_start:
            raise RuntimeError(
                f"Rendered coverage gap/overlap at record #{index}: "
                f"expected source_start={expected_start}, got {source_start}."
            )

        if source_end < source_start:
            raise RuntimeError(
                f"Rendered coverage invalid range at record #{index}: "
                f"{source_start}-{source_end}."
            )

        if source_end > source_line_count:
            raise RuntimeError(
                f"Rendered coverage out of bounds at record #{index}: "
                f"ends at {source_end}, but source has {source_line_count} lines."
            )

        output_path = output_root / filename

        if not output_path.exists():
            raise RuntimeError(
                f"Rendered file missing for source lines "
                f"{source_start}-{source_end}: {filename}"
            )

        expected_text = join_original_source_lines(
            source_lines[source_start - 1 : source_end]
        )

        actual_text = output_path.read_text(encoding="utf-8")

        if actual_text != expected_text:
            raise RuntimeError(
                f"Rendered file does not exactly match source slice: {filename} "
                f"for source lines {source_start}-{source_end}."
            )

        expected_start = source_end + 1

    if expected_start != source_line_count + 1:
        raise RuntimeError(
            f"Rendered coverage incomplete: stopped at line {expected_start - 1}; "
            f"expected coverage through line {source_line_count}."
        )


# ---------------------------------------------------------------------
# Generation phase
# ---------------------------------------------------------------------

async def phase_generate(args: argparse.Namespace) -> None:
    """
    Simplified concept-file generation.

    No summaries-first pass.
    No H1 plan.
    No H1 layout.
    No H2 folders.
    No hierarchy.
    No LLM verification.
    No LLM repair.

    Flow:
    1. Read source.
    2. Split source into approximately 100-line safe chunks.
    3. For each chunk, ask model to split into concept files.
    4. Carry the last concept into the next prompt.
    5. Retry until every requested window has exact line coverage.
    6. Validate full-source coverage.
    7. Render each final concept as one Markdown file under docs/.
    8. Store title/range/summary/order metadata in manifest.json only.
    9. Deterministically verify rendered docs match source slices exactly.
    10. Infer headers and global filename.
    11. Write coverage.json and metadata.json.
    """

    source_path = Path(args.source)
    out_dir = Path(args.out)
    docs_dir = out_dir / "docs"

    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)

    out_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)

    source_lines = read_lines(source_path)
    source_line_count = len(source_lines)

    # We still initialize the manifest in memory because render_concept_files mutates it
    manifest = init_manifest(source_path)

    planning_dir = out_dir / "_planning"
    planning_dir.mkdir(parents=True, exist_ok=True)

    plan_md_path = planning_dir / "concept-plan.md"
    coverage_json_path = planning_dir / "coverage.json"
    metadata_json_path = planning_dir / "metadata.json"

    # Force 30-second timeout for concept planning.
    llm = make_llm(
        model=args.gen_model,
        base_url=args.base_url,
        api_key=args.api_key,
        temperature=args.temperature,
        timeout=300,
    )

    if source_line_count == 0:
        concept_files = []
        coverage = []
        inferred_headers = []
        inferred_global_name = source_path.with_suffix(".md").name
    else:
        concept_files = await plan_concept_files_streaming(
            llm=llm,
            source_lines=source_lines,
            target_lines=100,
            max_extra=args.max_chunk_extra,
        )

        assert_concept_coverage(
            files=concept_files,
            source_line_count=source_line_count,
        )

        coverage = render_concept_files(
            docs_dir=docs_dir,
            source_lines=source_lines,
            files=concept_files,
            manifest=manifest,
            output_root=out_dir,
        )

        assert_rendered_docs_match_source(
            source_lines=source_lines,
            coverage=coverage,
            output_root=out_dir,
        )

        # --- Enrichment Phase ---
        enrichment_result = await enrich_concept_plan(
            llm=llm,
            original_filename=source_path.name,
            files=concept_files,
        )
        
        inferred_headers = [f.header for f in enrichment_result.files]
        inferred_global_name = enrichment_result.inferred_file_name

    ordered_concept_files = sorted(
        concept_files,
        key=lambda item: (
            item.source_start,
            item.source_end,
        ),
    )

    # Ensure headers and files are sorted together if we sorted the files
    if source_line_count > 0:
        # Re-sort headers to match the sorted concept_files
        paired = sorted(
            zip(concept_files, inferred_headers),
            key=lambda x: (x[0].source_start, x[0].source_end)
        )
        ordered_concept_files = [p[0] for p in paired]
        inferred_headers = [p[1] for p in paired]

    manifest.setdefault("planning", {})
    manifest["planning"]["strategy"] = "streaming_concept_file_split"
    manifest["planning"]["target_lines"] = 100
    manifest["planning"]["max_chunk_extra"] = args.max_chunk_extra
    manifest["planning"]["docs_dir"] = "docs"
    manifest["planning"]["file_count"] = len(ordered_concept_files)
    manifest["planning"]["coverage_verified_at"] = utc_now_iso()
    manifest["planning"]["render_integrity"] = "exact_source_slice_match"
    manifest["planning"]["inferred_global_name"] = inferred_global_name
    manifest["planning"]["files"] = [
        {
            "order": index,
            "header": header,
            **item.model_dump(),
        }
        for index, (item, header) in enumerate(
            zip(ordered_concept_files, inferred_headers),
            start=1,
        )
    ]

    manifest["coverage"] = coverage
    manifest["updated_at"] = utc_now_iso()

    write_concept_plan_document(
        path=plan_md_path,
        source_path=source_path,
        source_line_count=source_line_count,
        files=ordered_concept_files,
    )

    # --- Write the two specific JSONs ---
    write_coverage_json(
        path=coverage_json_path,
        source_line_count=source_line_count,
        files=ordered_concept_files,
        headers=inferred_headers,
    )

    write_metadata_json(
        path=metadata_json_path,
        original_file_name=source_path.name,
        inferred_file_name=inferred_global_name,
        files=ordered_concept_files,
        headers=inferred_headers,
    )

    print(
        f"[Generation] Done. Created {len(ordered_concept_files)} "
        f"concept files in {docs_dir}. Global name: {inferred_global_name}"
    )


# ---------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------


def collect_source_files(source_path: str) -> list[Path]:
    """
    Collect one Markdown source file or all top-level Markdown files
    in a source directory.
    """

    path = Path(source_path)

    if path.is_file():
        if path.suffix.lower() != ".md":
            raise RuntimeError(f"Source file is not Markdown: {path}")
        return [path]

    if path.is_dir():
        files = sorted(path.glob("*.md"))
        if not files:
            raise RuntimeError(f"No .md files found in folder: {path}")
        return files

    raise RuntimeError(f"Source path does not exist: {path}")


def output_dir_for_source(
    source_file: Path,
    output_root: str,
) -> Path:
    """
    Example:
        /input/manual.md + /output -> /output/manual
    """

    return Path(output_root) / source_file.stem


def make_config_for_source(source_file: Path) -> SimpleNamespace:
    """
    Build the per-source config object.

    Only fields needed by the simplified generation pipeline are included.
    """

    return SimpleNamespace(
        source=str(source_file),
        out=str(output_dir_for_source(source_file, OUTPUT_ROOT)),
        phase=PHASE,
        base_url=BASE_URL,
        api_key=API_KEY,
        gen_model=GEN_MODEL,
        max_chunk_extra=MAX_CHUNK_EXTRA,
        temperature=TEMPERATURE,
        clean=CLEAN_OUTPUT,
    )


# ---------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------


async def process_one_source(
    semaphore: asyncio.Semaphore,
    source_file: Path,
) -> None:
    async with semaphore:
        config = make_config_for_source(source_file)

        print("=" * 80)
        print(f"[Batch] Starting: {config.source}")
        print(f"[Batch] Output:   {config.out}")
        print("=" * 80)

        try:
            if config.phase in {"all", "generate"}:
                await phase_generate(config)
            elif config.phase in {"verify", "repair", "generate-flat"}:
                raise RuntimeError(
                    f"Phase '{config.phase}' is no longer supported in the "
                    "lossless concept-split pipeline. Use PHASE='generate' "
                    "or PHASE='all'."
                )
            else:
                raise RuntimeError(
                    f"Unknown PHASE={config.phase!r}. "
                    "Supported phases: 'generate', 'all'."
                )

            print(f"[Batch] Done: {config.source}")

        except Exception as exc:
            print(f"[Batch] FAILED: {config.source}")
            print(f"[Batch] Error: {exc}")


async def async_main() -> None:
    source_files = collect_source_files(SOURCE_PATH)

    print(f"[Batch] Found {len(source_files)} Markdown file(s).")
    print(f"[Batch] File concurrency: {FILE_CONCURRENCY}")

    semaphore = asyncio.Semaphore(FILE_CONCURRENCY)

    tasks = [
        process_one_source(
            semaphore=semaphore,
            source_file=source_file,
        )
        for source_file in source_files
    ]

    await asyncio.gather(*tasks)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
