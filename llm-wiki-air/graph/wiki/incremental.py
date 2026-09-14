"""Decide what a source edit invalidates in an existing wiki run."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence

from .storage import (
    normalize_source,
    read_json,
    sha256_text,
    split_source_lines,
    write_json_atomic,
)

Hunk = tuple[int, int, int, int]


def _overlaps(ranges: Sequence[Sequence[int]], start: int, end: int) -> bool:
    return any(int(first) <= end and start <= int(last) for first, last in ranges)


def invalidate_pages(
    run_root: Path, hunks: Sequence[Hunk], new_source_text: str
) -> str:
    """Invalidate touched pages, or remove the run when line numbers shift."""

    run_root = Path(run_root)
    plan_path = run_root / "state" / "plan.json"
    if not plan_path.exists() or not hunks:
        shutil.rmtree(run_root, ignore_errors=True)
        return "full"
    plan = read_json(plan_path)
    new_lines = split_source_lines(normalize_source(new_source_text))
    if len(new_lines) != int(plan.get("source_line_count", -1)) or any(
        old_len != new_len for _, old_len, _, new_len in hunks
    ):
        shutil.rmtree(run_root, ignore_errors=True)
        return "full"

    touched: list[dict] = []
    for page in plan["pages"]:
        ranges = list(page.get("owner_ranges", [])) + list(page.get("reference_ranges", []))
        if any(
            _overlaps(ranges, old_start, old_start + old_len - 1)
            for old_start, old_len, _, _ in hunks
        ):
            touched.append(page)
    for page in touched:
        (run_root / "wiki" / page["filename"]).unlink(missing_ok=True)
        (run_root / "state" / "pages" / f"{int(page['number']):03d}.json").unlink(
            missing_ok=True
        )
        shutil.rmtree(run_root / "work" / f"page-{int(page['number']):03d}", ignore_errors=True)
        shutil.rmtree(
            run_root / "work" / f"research-{int(page['number']):03d}",
            ignore_errors=True,
        )
    plan["source_sha256"] = sha256_text(new_source_text)
    write_json_atomic(plan_path, plan)
    return "resumed"
