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
    run_root: Path,
    hunks: Sequence[Hunk],
    new_source_text: str,
    *,
    touched_pages: set[str] | None = None,
    preserve_outputs: bool = False,
) -> str:
    """Remap unchanged ranges and invalidate pages touched by changed hunks."""

    run_root = Path(run_root)
    plan_path = run_root / "state" / "plan.json"
    if not plan_path.exists() or not hunks:
        shutil.rmtree(run_root, ignore_errors=True)
        return "full"
    plan = read_json(plan_path)
    old_count = int(plan.get("source_line_count", -1))
    new_lines = split_source_lines(normalize_source(new_source_text))
    if old_count < 0:
        shutil.rmtree(run_root, ignore_errors=True)
        return "full"

    changes = [
        (old_start - 1, old_start - 1 + old_len, new_start - 1, new_start - 1 + new_len)
        for old_start, old_len, new_start, new_len in hunks
    ]

    def map_boundary(value: int) -> int | None:
        old_cursor = new_cursor = 0
        for old_start, old_end, new_start, new_end in changes:
            if old_cursor <= value <= old_start:
                return new_cursor + value - old_cursor
            if value == old_end:
                return new_end
            if old_start < value < old_end:
                return None
            old_cursor, new_cursor = old_end, new_end
        if old_cursor <= value <= old_count:
            return new_cursor + value - old_cursor
        return None

    def map_ranges(ranges: Sequence[Sequence[int]]) -> list[list[int]] | None:
        mapped: list[list[int]] = []
        for first, last in ranges:
            start = map_boundary(int(first) - 1)
            end = map_boundary(int(last))
            if start is None or end is None or start >= end:
                return None
            mapped.append([start + 1, end])
        return mapped

    touched: list[dict] = []
    research_touched: set[int] = set()
    for page in plan["pages"]:
        owner_ranges = list(page.get("owner_ranges", []))
        reference_ranges = list(page.get("reference_ranges", []))
        owner_changed = any(
            _overlaps(owner_ranges, old_start, old_start + max(old_len, 1) - 1)
            for old_start, old_len, _, _ in hunks
        )
        references_changed = any(
            _overlaps(reference_ranges, old_start, old_start + max(old_len, 1) - 1)
            for old_start, old_len, _, _ in hunks
        )
        if owner_changed or references_changed:
            touched.append(page)
        if references_changed:
            research_touched.add(int(page["number"]))
        for key in ("owner_ranges", "reference_ranges"):
            mapped = map_ranges(page.get(key, []))
            if mapped is None:
                shutil.rmtree(run_root, ignore_errors=True)
                return "full"
            page[key] = mapped
    if not touched:
        shutil.rmtree(run_root, ignore_errors=True)
        return "full"
    if touched_pages is not None:
        touched_pages.update(str(page["filename"]) for page in touched)
    if not preserve_outputs:
        for page in touched:
            number = int(page["number"])
            (run_root / "wiki" / page["filename"]).unlink(missing_ok=True)
            (run_root / "state" / "pages" / f"{number:03d}.json").unlink(
                missing_ok=True
            )
            if number in research_touched:
                shutil.rmtree(run_root / "work" / f"research-{number:03d}", ignore_errors=True)

    for page in plan["pages"]:
        number = int(page["number"])
        if number in research_touched:
            continue
        cache_path = run_root / "work" / f"research-{number:03d}" / "references.json"
        cached = read_json(cache_path, default={})
        valid = isinstance(cached.get("useful_facts"), list)
        for fact in cached.get("useful_facts", []) if valid else []:
            mapped = map_ranges([[fact.get("source_start", 0), fact.get("source_end", 0)]])
            target = map_boundary(int(fact.get("target_line", 0)) - 1)
            if mapped is None or target is None:
                valid = False
                break
            fact["source_start"], fact["source_end"] = mapped[0]
            fact["target_line"] = target + 1
        if valid:
            write_json_atomic(cache_path, cached)
        elif cache_path.exists():
            shutil.rmtree(cache_path.parent, ignore_errors=True)
    touched_numbers = {int(page["number"]) for page in touched}
    new_hash = sha256_text(new_source_text)
    for page in plan["pages"]:
        if int(page["number"]) in touched_numbers and not preserve_outputs:
            continue
        state_path = run_root / "state" / "pages" / f"{int(page['number']):03d}.json"
        if not state_path.exists():
            continue
        state = read_json(state_path)
        state["source_ranges"] = page.get("owner_ranges", [])
        state["reference_ranges"] = page.get("reference_ranges", [])
        provenance = state.get("provenance")
        if isinstance(provenance, dict):
            provenance["owned_line_ranges"] = page.get("owner_ranges", [])
            provenance["imported_line_ranges"] = page.get("reference_ranges", [])
            for record in provenance.get("imported_from_pages", []):
                mapped = map_ranges(record.get("source_ranges", []))
                if mapped is None:
                    shutil.rmtree(run_root, ignore_errors=True)
                    return "full"
                record["source_ranges"] = mapped
            source = provenance.get("source_document")
            if isinstance(source, dict):
                source["sha256"] = new_hash
        write_json_atomic(state_path, state)
    plan["source_sha256"] = new_hash
    plan["source_line_count"] = len(new_lines)
    write_json_atomic(plan_path, plan)
    return "incremental"
