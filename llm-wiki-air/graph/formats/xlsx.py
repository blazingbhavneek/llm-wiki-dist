"""Excel source pages followed by one workbook-wide wiki story."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .tabular import _letter, _slug, source_page_title, write_tables

SHEET_RE = re.compile(r"^## (?:Sheet|シート): (.+?)\s*$")
VBA_REFS_RE = re.compile(
    r"<!-- vba-references:start -->.*?<!-- vba-references:end -->",
    re.DOTALL,
)
VBA_ID_RE = re.compile(r"<!-- vba-id: ([^ ]+) -->")
VBA_LINK_RE = re.compile(r"vba://([^)\s]+)")
SHEET_CONTEXT_RE = re.compile(
    r"<!-- sheet-context:start -->.*?<!-- sheet-context:end -->",
    re.DOTALL,
)
CELL_RANGE_RE = re.compile(r"_元範囲:\s*([^_]+)_")
LINE_REFERENCE_RE = re.compile(r"(?:原文|ソース)\s*(\d+)\s*[-–]\s*(\d+)\s*行")


def split_sheets(lines: list[str]) -> list[tuple[str, str, tuple[int, int]]]:
    starts = [(n, match.group(1)) for n, line in enumerate(lines, 1) if (match := SHEET_RE.match(line))]
    output = []
    for index, (start, name) in enumerate(starts):
        end = starts[index + 1][0] - 1 if index + 1 < len(starts) else len(lines)
        block = "\n".join(lines[start:end])
        table = block[block.find("<table>"):block.rfind("</table>") + len("</table>")] if "<table>" in block and not name.startswith("VBA-") else block
        range_note = next((line for line in block.splitlines() if line.startswith("_元範囲:")), "")
        if range_note and table != block:
            table += f"\n\n{range_note}"
        if match := VBA_REFS_RE.search(block):
            table += f"\n\n{match.group(0)}"
        if match := SHEET_CONTEXT_RE.search(block):
            table += f"\n\n{match.group(0)}"
        output.append((name, table, (start, end)))
    targets = {
        unquote(match.group(1)): f"{number:03d}-{_slug(source_page_title(name, is_vba=True))}.md"
        for number, (name, block, _) in enumerate(output, 1)
        if (match := VBA_ID_RE.search(block))
    }
    return [
        (
            name,
            VBA_LINK_RE.sub(lambda match: targets.get(unquote(match.group(1)), match.group(0)), block),
            source_range,
        )
        for name, block, source_range in output
    ]


def _cell_sources(sheet: str, body: str) -> list[str]:
    base = re.sub(r"-part\d+$", "", sheet, flags=re.IGNORECASE)
    ranges = CELL_RANGE_RE.findall(body)
    if ranges:
        return [f"{base}!{cell_range.strip()}" for cell_range in ranges]
    referenced = list(dict.fromkeys(re.findall(r"`([^`]+![A-Z]+\d+(?::[A-Z]+\d+)?)`", body)))
    if referenced:
        return referenced
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", body, re.IGNORECASE | re.DOTALL)
    widths = []
    for row in rows:
        width = 0
        for tag in re.findall(r"<(?:td|th)\b[^>]*>", row, re.IGNORECASE):
            span = re.search(r"\bcolspan=[\"']?(\d+)", tag, re.IGNORECASE)
            width += int(span.group(1)) if span else 1
        widths.append(width)
    if rows and widths and max(widths) > 0:
        return [f"{base}!A1:{_letter(max(widths))}{len(rows)}"]
    return []


def _story_source(
    sheets: list[tuple[str, str, tuple[int, int]]],
) -> tuple[str, list[dict[str, Any]]]:
    lines: list[str] = []
    spans: list[dict[str, Any]] = []
    for sheet, body, _source_range in sheets:
        if VBA_ID_RE.search(body):
            continue
        cells = _cell_sources(sheet, body)
        content = body.replace("\n", "\u2028")
        lines.append(
            f"Excelシート部分 {len(lines) + 1} / シート: {sheet} / "
            f"参照セル範囲: {', '.join(cells) or '不明'} / 内容: {content}"
        )
        spans.append({"start": len(lines), "end": len(lines), "cells": cells})
    return "\n".join(lines).rstrip() + "\n", spans


def _cells_for_ranges(ranges: list[list[int]], spans: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(
        cell
        for start, end in ranges
        for span in spans
        if span["start"] <= end and start <= span["end"]
        for cell in span["cells"]
    ))


def _replace_line_references(body: str, spans: list[dict[str, Any]]) -> str:
    def replace(match: re.Match[str]) -> str:
        cells = _cells_for_ranges([[int(match.group(1)), int(match.group(2))]], spans)
        return "参照セル " + ("、".join(f"`{cell}`" for cell in cells) or "不明")

    return LINE_REFERENCE_RE.sub(replace, body)


def _write_planning(run_dir: Path, files: list[dict[str, Any]], *, source_name: str) -> None:
    from graph.wiki.storage import write_json_atomic

    planning = run_dir / "_planning"
    planning.mkdir(exist_ok=True)
    write_json_atomic(
        planning / "manifest.json",
        {
            "source": source_name,
            "planning": {
                "ingest_mode": "wiki",
                "strategy": "excel-workbook-story",
                "file_count": len(files),
            },
            "files": files,
        },
    )
    write_json_atomic(
        planning / "coverage.json",
        {
            "file_count": len(files),
            "files": [
                {
                    "title": item["title"],
                    "filename": re.sub(r"^\d+-", "", item["filename"]),
                    "summary": item.get("summary", ""),
                    "header": "マクロ" if item["kind"] == "vba" else "シート" if item["kind"] == "table" else "解説",
                    "source_cells": item.get("source_cells", []),
                }
                for item in files
            ],
        },
    )
    write_json_atomic(
        planning / "metadata.json",
        {
            "original_file_name": source_name,
            "inferred_file_name": source_name,
            "files": [
                {
                    "name": re.sub(r"^\d+-", "", item["filename"]),
                    "header": "マクロ" if item["kind"] == "vba" else "シート" if item["kind"] == "table" else "解説",
                }
                for item in files
            ],
        },
    )


async def _append_story(
    source_path: Path,
    sheets: list[tuple[str, str, tuple[int, int]]],
    files: list[dict[str, Any]],
    *,
    run_dir: Path,
    model: Any,
    config: Any,
    on_progress=None,
    stop_check=None,
) -> None:
    from graph.wiki.pipeline import run_pipeline
    from graph.wiki.storage import read_json, write_text_atomic

    story_text, spans = _story_source(sheets)
    if not spans:
        _write_planning(run_dir, files, source_name=source_path.name)
        return
    if on_progress:
        on_progress({
            "stage": "excel-story",
            "step": "start",
            "current": 0,
            "total": len(spans),
            "parts": len(spans),
        })
    for span, item in zip(spans, (item for item in files if item["kind"] == "table")):
        span["filename"] = item["filename"]
    state_root = Path(config.run_dir or (run_dir / "wiki-state"))
    story_source = state_root / "excel-story-source.md"
    story_run = state_root / "excel-story"
    write_text_atomic(story_source, story_text)
    story_config = config.model_copy(
        update={
            "run_dir": str(story_run),
            "source_kind": "xlsx",
            "document_slug": "ワークブック解説",
            "window_target_lines": 1,
            "window_overlap_lines": 0,
            "page_target_lines": 1,
        }
    )
    await run_pipeline(
        story_source,
        config=story_config,
        model=model,
        on_progress=on_progress,
        stop_check=stop_check,
    )
    plan = read_json(story_run / "state" / "plan.json")
    if on_progress:
        on_progress({
            "stage": "excel-story",
            "step": "planned",
            "parts": len(spans),
            "pages": len(plan["pages"]),
        })
    offset = len(files)
    titles = {
        page["filename"]: _replace_line_references(page["title"], spans)
        for page in plan["pages"]
    }
    renames = {
        page["filename"]: f"{offset + number:03d}-解説-{_slug(titles[page['filename']])}.md"
        for number, page in enumerate(plan["pages"], 1)
    }
    for story_number, page in enumerate(plan["pages"], 1):
        old_name = page["filename"]
        new_name = renames[old_name]
        body = (story_run / "wiki" / old_name).read_text(encoding="utf-8")
        ranges = page.get("owner_ranges", [])
        used_spans = [
            span
            for start, end in ranges
            for span in spans
            if span["start"] <= end and start <= span["end"]
        ]
        references = list(dict.fromkeys(
            f"- [{cell}]({span['filename']})"
            for span in used_spans
            for cell in span["cells"]
        ))
        if references:
            first_line, separator, rest = body.partition("\n")
            body = "\n".join([
                first_line,
                "",
                "## 参照セル",
                "",
                *references,
                "",
                rest if separator else "",
            ]).rstrip() + "\n"
        for old_link, new_link in renames.items():
            body = body.replace(f"({old_link})", f"({new_link})")
        body = _replace_line_references(body, spans)
        write_text_atomic(run_dir / "docs" / new_name, body)
        files.append(
            {
                "filename": new_name,
                "title": titles[old_name],
                "kind": "story",
                "source_cells": _cells_for_ranges(ranges, spans),
                "summary": _replace_line_references(page.get("summary", ""), spans),
            }
        )
        if on_progress:
            on_progress({
                "stage": "excel-story",
                "step": "page",
                "current": story_number,
                "total": len(plan["pages"]),
                "page": titles[old_name],
            })
    _write_planning(run_dir, files, source_name=source_path.name)
    if on_progress:
        on_progress({
            "stage": "excel-story",
            "step": "complete",
            "current": len(plan["pages"]),
            "total": len(plan["pages"]),
        })


async def run(source_path: Path, *, run_dir: Path, model: Any, config: Any, on_progress=None, stop_check=None):
    lines = source_path.read_text(encoding="utf-8").splitlines()
    sheets = split_sheets(lines)
    files = await write_tables(
        sheets=sheets,
        run_dir=run_dir,
        model=model,
        config=config,
        on_progress=on_progress,
        stop_check=stop_check,
        generate_analyses=False,
    )
    by_title = {sheet: body for sheet, body, _ in sheets}
    for item, (sheet, _body, _source_range) in zip(files, sheets):
        item.pop("source_ranges", None)
        item["source_cells"] = _cell_sources(sheet, by_title[sheet])
    await _append_story(
        source_path,
        sheets,
        files,
        run_dir=Path(run_dir),
        model=model,
        config=config,
        on_progress=on_progress,
        stop_check=stop_check,
    )
    return files
