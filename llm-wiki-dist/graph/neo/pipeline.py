"""The small neo pipeline.

There are deliberately four small phases here:

1. Overlapping 250-line windows are described without assigning ownership.
2. Regional and document planners compile one exact sequential seed partition.
3. Small pairwise calls compare each seed with a few selected references.
4. One independent planner and writer pair turns the seed plus those findings
   into a readable wiki page, then two judges check preservation and enrichment.

The old organizer, reducer, mutable skill, draft wiki, and bidirectional audit
state machine are intentionally not part of this driver. Python owns source
ranges, filenames, provenance, retries, and publication; the selected CLI
agent only plans or edits one page at a time.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent

from .agent import AgentPort, clean_workdir, make_agent
from .config import (
    LINK_PROMPT_VERSION,
    REWRITE_PROMPT_VERSION,
    SEED_PLAN_VERSION,
    NeoConfig,
)
from .document_map import build_seed_plan
from .ids import document_id, slugify
from .images import ImageUnit, extract_image_units, restore_images
from .model import ChatModelPort, ModelPort
from .prompts import (
    link_page_prompt,
    page_judge_prompt,
    reference_enrichment_judge_prompt,
    reference_research_prompt,
    reference_selection_prompt,
    simple_page_edit_prompt,
    wiki_plan_judge_prompt,
    wiki_page_plan_prompt,
)
from .schemas import CompiledSeedPlan
from .storage import (
    normalize_source,
    read_json,
    sha256_text,
    slice_text,
    split_source_lines,
    write_json_atomic,
    write_text_atomic,
)
from .wire import (
    PageJudgeResult,
    ReferenceFact,
    ReferenceResearchResult,
    ReferenceSelection,
    WikiPlanJudgeResult,
)
from .windows import observe_document


Progress = Callable[[dict[str, Any]], None] | None
StopCheck = Callable[[], bool] | None


class PipelineError(RuntimeError):
    """A deterministic seed or publication invariant failed."""


@dataclass
class SeedPage:
    number: int
    title: str
    chapter: str
    summary: str
    owner_ranges: list[tuple[int, int]]
    filename: str
    page_id: str
    reference_ranges: list[tuple[int, int]] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.filename


@dataclass
class RewriteResult:
    page: SeedPage
    markdown: str
    attempts: int
    fallback: bool = False
    error: str = ""
    selected_version: int = 0
    judge_score: int | None = None
    missing_important_information: list[str] = field(default_factory=list)
    judge_notes: str = ""
    linked: bool = False
    link_attempts: int = 0
    link_error: str = ""


@dataclass
class _JudgedCandidate:
    markdown: str
    version: int
    score: int
    missing: list[str]
    notes: str = ""
    judged: bool = True
    reference_ranges: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class _ReferenceEvidence:
    page: SeedPage
    facts: list[ReferenceFact] = field(default_factory=list)
    no_useful_information_reason: str = ""


def _emit(callback: Progress, stage: str, step: str, **details: Any) -> None:
    if callback:
        callback({"stage": stage, "step": step, **details})


def _merge_ranges(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Collapse adjacent or overlapping source spans into simple ranges."""

    merged: list[tuple[int, int]] = []
    for start, end in sorted((int(start), int(end)) for start, end in ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _ranges_text(ranges: Sequence[tuple[int, int]]) -> str:
    return ", ".join(
        str(start) if start == end else f"{start}-{end}"
        for start, end in _merge_ranges(ranges)
    ) or "none"


def _ranges_json(ranges: Sequence[tuple[int, int]]) -> list[list[int]]:
    return [[start, end] for start, end in _merge_ranges(ranges)]


def _range_intersections(
    left: Sequence[tuple[int, int]], right: Sequence[tuple[int, int]]
) -> list[tuple[int, int]]:
    intersections: list[tuple[int, int]] = []
    for left_start, left_end in left:
        for right_start, right_end in right:
            start = max(left_start, right_start)
            end = min(left_end, right_end)
            if start <= end:
                intersections.append((start, end))
    return _merge_ranges(intersections)


def _referenced_page_records(
    page: SeedPage, pages: Sequence[SeedPage]
) -> list[dict[str, Any]]:
    """Map imported source ranges back to the seed pages that own them."""

    records: list[dict[str, Any]] = []
    for candidate in pages:
        if candidate.number == page.number:
            continue
        ranges = _range_intersections(page.reference_ranges, candidate.owner_ranges)
        if not ranges:
            continue
        records.append(
            {
                "number": candidate.number,
                "title": candidate.title,
                "filename": candidate.filename,
                "source_ranges": _ranges_json(ranges),
            }
        )
    return records


def _page_provenance(
    page: SeedPage,
    pages: Sequence[SeedPage],
    *,
    source_path: Path | None,
    source_snapshot_path: Path | None,
    source_sha256: str,
) -> dict[str, Any]:
    """Build durable dependencies for linking and later source-diff updates."""

    return {
        "source_document": {
            "path": str(source_path.resolve()) if source_path else "",
            "snapshot_path": (
                str(source_snapshot_path.resolve()) if source_snapshot_path else ""
            ),
            "sha256": source_sha256,
        },
        "owned_line_ranges": _ranges_json(page.owner_ranges),
        "imported_line_ranges": _ranges_json(page.reference_ranges),
        "imported_from_pages": _referenced_page_records(page, pages),
    }


def _plan_pages(plan: CompiledSeedPlan) -> list[SeedPage]:
    """Give the already validated sequential ranges stable numbered filenames."""

    pages: list[SeedPage] = []
    used_names: set[str] = set()
    for number, entry in enumerate(plan.pages, start=1):
        stem = slugify(entry.title or f"document-section-{number}", fallback=f"page-{number}")
        filename = f"{number:03d}-{stem}.md"
        if filename.casefold() in used_names:
            suffix = 2
            while f"{number:03d}-{stem}-{suffix}.md".casefold() in used_names:
                suffix += 1
            filename = f"{number:03d}-{stem}-{suffix}.md"
        used_names.add(filename.casefold())
        pages.append(
            SeedPage(
                number=number,
                title=(entry.title or f"Document section {number}").strip(),
                chapter=entry.chapter.strip(),
                summary=entry.summary.strip(),
                owner_ranges=[(entry.source_start, entry.source_end)],
                filename=filename,
                page_id=f"page-{number:03d}",
            )
        )
    return pages


def _slice_ranges(lines: Sequence[str], ranges: Sequence[tuple[int, int]]) -> str:
    return "\n\n".join(slice_text(list(lines), start, end) for start, end in ranges)


def _yaml_string(value: str) -> str:
    return json.dumps(value or "", ensure_ascii=False)


def _render_page(
    page: SeedPage,
    body: str,
    *,
    status: str,
    rewrite_version: str = "",
) -> str:
    if status == "rewritten":
        return body.rstrip() + "\n"
    refs = _ranges_json(page.reference_ranges)
    header = "\n".join(
        [
            "---",
            f"title: {_yaml_string(page.title)}",
            f"chapter: {_yaml_string(page.chapter)}",
            f"page_number: {page.number}",
            f"status: {_yaml_string(status)}",
            *(
                [f"rewrite_version: {_yaml_string(rewrite_version)}"]
                if rewrite_version
                else []
            ),
            f"source_ranges: {json.dumps(_ranges_json(page.owner_ranges), ensure_ascii=False)}",
            f"reference_ranges: {json.dumps(refs, ensure_ascii=False)}",
            "---",
            "",
        ]
    )
    return header + body.rstrip() + "\n"


def _page_units(page: SeedPage, units: Sequence[ImageUnit]) -> list[ImageUnit]:
    return [
        unit
        for unit in units
        if any(start <= unit.source_start and unit.source_end <= end for start, end in page.owner_ranges)
    ]


def _prompt_safe(text: str, units: Sequence[ImageUnit]) -> str:
    result = text
    for unit in units:
        result = result.replace(unit.raw, unit.placeholder)
    return result


def _image_neighbors(lines: Sequence[str], unit: ImageUnit) -> tuple[str, str]:
    def nearest(numbers) -> str:
        for number in numbers:
            text = lines[number - 1].strip()
            if text and not text.startswith(("<image-", "</image-", "<img ")):
                return text
        return ""

    before = nearest(range(unit.source_start - 1, 0, -1))
    after = nearest(range(unit.source_end + 1, len(lines) + 1))
    return before, after


def _insert_image_near_context(
    markdown: str,
    placeholder: str,
    before: str,
    after: str,
) -> str:
    """Best-effort placement next to surviving original context."""

    if before and before in markdown:
        position = markdown.find(before) + len(before)
        return markdown[:position] + f"\n\n{placeholder}" + markdown[position:]
    if after and after in markdown:
        position = markdown.find(after)
        return markdown[:position] + f"{placeholder}\n\n" + markdown[position:]
    return markdown.rstrip() + f"\n\n## 原文の図・画像\n\n{placeholder}\n"


def _preserve_image_placeholders(
    markdown: str,
    units: Sequence[ImageUnit],
    lines: Sequence[str],
) -> str:
    """Keep every owned image once and near its original surrounding text."""

    result = markdown.rstrip()
    for unit in units:
        first = result.find(unit.placeholder)
        if first < 0:
            before, after = _image_neighbors(lines, unit)
            result = _insert_image_near_context(
                result, unit.placeholder, before, after
            )
            continue
        tail = result[first + len(unit.placeholder) :].replace(unit.placeholder, "")
        result = result[: first + len(unit.placeholder)] + tail
    return result.rstrip() + "\n"


def _image_context(units: Sequence[ImageUnit], lines: Sequence[str]) -> str:
    if not units:
        return "（このページに画像はない）"
    entries: list[str] = []
    for unit in units:
        before, after = _image_neighbors(lines, unit)
        context = []
        if before:
            context.append(f"直前: {before}")
        if after:
            context.append(f"直後: {after}")
        if unit.description:
            context.append(f"説明: {unit.description}")
        entries.append(
            f"- `{unit.placeholder}` — 原文 {unit.source_start}-{unit.source_end}行"
            + ("、" + "、".join(context) if context else "")
        )
    return "\n".join(entries)


def _seed_path(seed_root: Path, page: SeedPage) -> Path:
    return (seed_root / page.filename).resolve()


def _page_index(pages: Sequence[SeedPage], seed_root: Path, source_file: Path) -> str:
    lines = ["# Wikiページ一覧", ""]
    for page in pages:
        chapter = f"［{page.chapter}］" if page.chapter else ""
        lines.append(
            f"- 最終Wiki: `{page.filename}` — {page.title}{chapter}\n"
            f"  - 参照用シード（絶対パス）: `{_seed_path(seed_root, page)}`\n"
            f"  - 全原文（絶対パス）: `{source_file.resolve()}`\n"
            f"  - 原文行: {_ranges_text(page.owner_ranges)}\n"
            f"  - 内容要約: {page.summary or '要約なし'}"
        )
    return "\n".join(lines) + "\n"


def _reference_view(
    page: SeedPage,
    pages: Sequence[SeedPage],
    seed_root: Path,
    source_file: Path,
) -> str:
    """Compact cross-page context; the full source remains owned by its page."""

    candidates = [item for item in pages if item.number != page.number]
    if not candidates:
        return ""
    lines = [
        "以下は他ページの参照用シードである。要約で候補を選び、内容を使う前に絶対パスのファイルを読むこと。",
        "",
    ]
    for item in candidates:
        lines.append(
            f"- 最終Wiki: `{item.filename}`\n"
            f"  - タイトル: {item.title}\n"
            f"  - 原文行: {_ranges_text(item.owner_ranges)}\n"
            f"  - 内容要約: {item.summary or '要約なし'}\n"
            f"  - 読み取り用シード絶対パス: `{_seed_path(seed_root, item)}`\n"
            f"  - 行番号確認用の全原文絶対パス: `{source_file.resolve()}`"
        )
    return "\n".join(lines)


_REFERENCE_MARKER = re.compile(
    r"（参照元:\s*原文\s*(\d+)\s*(?:[-–—]\s*(\d+)\s*)?行）"
)


def _reference_ranges_from_markdown(
    markdown: str,
    owner_ranges: Sequence[tuple[int, int]],
    source_line_count: int,
) -> list[tuple[int, int]]:
    """Collect optional cross-page provenance markers without rejecting prose."""

    found: list[tuple[int, int]] = []
    for match in _REFERENCE_MARKER.finditer(markdown):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if not 1 <= start <= end <= source_line_count:
            continue
        if any(owner_start <= start and end <= owner_end for owner_start, owner_end in owner_ranges):
            continue
        found.append((start, end))
    return _merge_ranges(found)


def _write_reference_seeds(
    pages: Sequence[SeedPage],
    lines: Sequence[str],
    units: Sequence[ImageUnit],
    seed_root: Path,
) -> None:
    """Write stable, readable source snapshots for cross-page agent reads."""

    seed_root.mkdir(parents=True, exist_ok=True)
    for page in pages:
        write_text_atomic(
            _seed_path(seed_root, page),
            _numbered_source(lines, page.owner_ranges, _page_units(page, units)),
        )


def _stage_rewrite_references(
    task_dir: Path,
    pages: Sequence[SeedPage],
    *,
    lines: Sequence[str],
    units: Sequence[ImageUnit],
    seed_root: Path,
) -> tuple[Path, Path]:
    """Copy all readable evidence inside one agent working directory."""

    local_seed_root = (task_dir / "reference-seeds").resolve()
    local_seed_root.mkdir(parents=True, exist_ok=True)
    for item in pages:
        source = _seed_path(seed_root, item)
        body = (
            source.read_text(encoding="utf-8")
            if source.exists()
            else _numbered_source(lines, item.owner_ranges, _page_units(item, units))
        )
        write_text_atomic(
            local_seed_root / item.filename,
            body,
        )
    local_source = (task_dir / "all-source-numbered.md").resolve()
    if lines:
        write_text_atomic(
            local_source,
            _numbered_source(lines, [(1, len(lines))], units),
        )
    else:
        write_text_atomic(local_source, "")
    return local_seed_root, local_source


def _numbered_source(
    lines: Sequence[str],
    ranges: Sequence[tuple[int, int]],
    units: Sequence[ImageUnit],
) -> str:
    """Render source with original line numbers while hiding image payloads."""

    units_by_start = {unit.source_start: unit for unit in units}
    rendered: list[str] = []
    for start, end in ranges:
        line_number = start
        while line_number <= end:
            unit = units_by_start.get(line_number)
            if unit is not None and unit.source_end <= end:
                rendered.append(
                    f"{unit.source_start}-{unit.source_end}: {unit.placeholder}"
                )
                line_number = unit.source_end + 1
                continue
            rendered.append(f"{line_number}: {lines[line_number - 1]}")
            line_number += 1
    return "\n".join(rendered) + "\n"


async def _structured_with_artifacts(
    *,
    schema: type[Any],
    prompt: Any,
    model: ModelPort,
    output_dir: Path,
    stem: str,
    attempts: int,
    max_output_tokens: int,
    stop_check: StopCheck,
) -> tuple[Any | None, int, str]:
    """Run one small structured task and retain every prompt and result."""

    last_error = ""
    for attempt in range(1, max(1, attempts) + 1):
        if stop_check and stop_check():
            raise asyncio.CancelledError("reference research cancelled")
        write_text_atomic(
            output_dir / f"{stem}-attempt-{attempt:02d}-prompt.md",
            prompt.render(),
        )
        try:
            raw = await model.structured(
                schema,
                prompt.messages(),
                max_output_tokens=max_output_tokens,
            )
            result = raw if isinstance(raw, schema) else schema.model_validate(raw)
            write_json_atomic(output_dir / f"{stem}.json", result)
            return result, attempt, ""
        except Exception as exc:  # noqa: BLE001 - bounded retry with evidence
            last_error = f"{type(exc).__name__}: {exc}"[:1000]
            write_text_atomic(
                output_dir / f"{stem}-attempt-{attempt:02d}-error.txt",
                last_error + "\n",
            )
    return None, max(1, attempts), last_error


def _valid_reference_facts(
    facts: Sequence[ReferenceFact], page: SeedPage
) -> list[ReferenceFact]:
    """Keep only cited facts that point inside the compared reference page."""

    valid: list[ReferenceFact] = []
    seen: set[tuple[str, int, int]] = set()
    for fact in facts:
        description = fact.description.strip()
        start, end = fact.source_start, fact.source_end
        if not description or not any(
            owner_start <= start <= end <= owner_end
            for owner_start, owner_end in page.owner_ranges
        ):
            continue
        key = (description, start, end)
        if key in seen:
            continue
        seen.add(key)
        valid.append(fact)
    return valid


def _render_reference_research(
    target: SeedPage,
    evidence: Sequence[_ReferenceEvidence],
    *,
    lines: Sequence[str],
    units: Sequence[ImageUnit],
    seed_root: Path,
    researcher_report: str = "",
) -> str:
    """Create the compact, auditable evidence pack consumed by plan and writer."""

    rendered = [
        "# 参照調査結果",
        "",
        f"対象: {target.number:03d} {target.title}",
        "",
    ]
    if researcher_report.strip():
        rendered.extend(
            ["## 調査エージェントの全体所見", researcher_report.strip(), ""]
        )
    useful_count = 0
    for item in evidence:
        rendered.extend(
            [
                f"## {item.page.number:03d} {item.page.title}",
                f"- 参照シード絶対パス: `{_seed_path(seed_root, item.page)}`",
                f"- 参照ページの原文範囲: {_ranges_text(item.page.owner_ranges)}行",
            ]
        )
        if not item.facts:
            rendered.extend(
                [
                    "- useful fact: なし",
                    "- 理由: "
                    + (item.no_useful_information_reason or "追加すべき事実は確認されなかった。"),
                    "",
                ]
            )
            continue
        for fact in item.facts:
            useful_count += 1
            fact_units = [
                unit
                for unit in units
                if fact.source_start <= unit.source_start
                and unit.source_end <= fact.source_end
            ]
            excerpt = _numbered_source(
                lines,
                [(fact.source_start, fact.source_end)],
                fact_units,
            ).rstrip()
            rendered.extend(
                [
                    f"### useful fact {useful_count}",
                    f"- 追加する事実: {fact.description.strip()}",
                    f"- 必要な理由: {fact.reason.strip() or '対象記事を単独で理解しやすくするため。'}",
                    f"- 挿入場所: {fact.insertion_point.strip() or '関連する説明の直後'}",
                    f"- 出典: 原文 {fact.source_start}-{fact.source_end}行",
                    "- 根拠抜粋:",
                    "```text",
                    excerpt,
                    "```",
                    "",
                ]
            )
    if useful_count == 0:
        rendered.extend(
            [
                "## 結論",
                "比較した参照ページから対象記事へ追加すべき技術情報は確認されなかった。",
                "",
            ]
        )
    return "\n".join(rendered).rstrip() + "\n"


def _researched_ranges(evidence: Sequence[_ReferenceEvidence]) -> list[tuple[int, int]]:
    return _merge_ranges(
        [
            (fact.source_start, fact.source_end)
            for item in evidence
            for fact in item.facts
        ]
    )


def _missing_research_markers(
    markdown: str,
    evidence: Sequence[_ReferenceEvidence],
    *,
    owner_ranges: Sequence[tuple[int, int]],
    source_line_count: int,
) -> list[str]:
    """Require one visible provenance marker for every accepted research fact."""

    marked = _reference_ranges_from_markdown(
        markdown, owner_ranges, source_line_count
    )
    missing: list[str] = []
    for item in evidence:
        for fact in item.facts:
            if any(
                marker_start <= fact.source_start
                and fact.source_end <= marker_end
                for marker_start, marker_end in marked
            ):
                continue
            missing.append(
                "参照情報の不足: "
                f"原文 {fact.source_start}-{fact.source_end}行の追加情報に"
                "参照元行マーカーがない"
            )
    return missing


def _reference_finish_error(
    read_page_numbers: Sequence[int],
    required_page_numbers: set[int],
    minimum_reads: int,
    report: str,
) -> str:
    """The same mechanical finish gate used by the general researcher."""

    unread_required = sorted(required_page_numbers.difference(read_page_numbers))
    if unread_required:
        return (
            "終了不可。境界確認のため次の隣接ページを先にread_referenceで読むこと: "
            + ", ".join(f"{number:03d}" for number in unread_required)
        )
    if len(set(read_page_numbers)) < minimum_reads:
        return (
            f"終了不可。{len(set(read_page_numbers))}ページしか読んでいない。"
            f"少なくとも{minimum_reads}ページを読み、関連情報をさらに調査すること。"
        )
    if not str(report or "").strip():
        return "終了不可。読んだ資料ごとの判断と根拠行をreportへ書くこと。"
    return ""


async def _run_reference_agent(
    page: SeedPage,
    *,
    pages: Sequence[SeedPage],
    lines: Sequence[str],
    units: Sequence[ImageUnit],
    model: ModelPort,
    config: NeoConfig,
    research_dir: Path,
    stop_check: StopCheck,
    on_progress: Progress,
) -> tuple[list[int], str, str, int]:
    """Run a ReAct reader whose finish tool rejects shallow research."""

    llm = getattr(model, "llm", None)
    others = [item for item in pages if item.number != page.number]
    if llm is None or not others:
        return [], "", "tool-capable model unavailable", 0

    by_number = {item.number: item for item in others}
    position = next(index for index, item in enumerate(pages) if item.number == page.number)
    required: set[int] = set()
    if position > 0:
        required.add(pages[position - 1].number)
    if position + 1 < len(pages):
        required.add(pages[position + 1].number)
    minimum_reads = min(
        len(others), max(len(required), max(1, config.reference_min_reads))
    )
    read_order: list[int] = []
    finished: dict[str, Any] = {"report": "", "cited": []}

    def search_references(text: str) -> str:
        """Search reference titles and summaries; then read promising page numbers."""

        query = str(text or "").casefold().strip()
        terms = [
            term
            for term in re.findall(r"[a-z_][a-z0-9_]{1,}|[ぁ-んァ-ヶ一-龯]{2,}", query)
            if len(term) > 1
        ]
        matches = [
            item
            for item in others
            if not terms
            or any(
                term in f"{item.title} {item.summary}".casefold()
                for term in terms
            )
        ]
        if not matches:
            return "該当なし。別の語で検索するか、一覧からページ番号を選んで読むこと。"
        return "\n".join(
            f"- {item.number:03d} | {item.title} | 原文 {_ranges_text(item.owner_ranges)}行 | "
            f"{item.summary or '要約なし'}"
            for item in matches
        )

    def read_reference(page_number: int) -> str:
        """Read the complete numbered source of one reference page."""

        if stop_check and stop_check():
            raise asyncio.CancelledError("reference research cancelled")
        candidate = by_number.get(int(page_number))
        if candidate is None:
            return "存在しないページ番号。ページ一覧から正確な番号を選ぶこと。"
        if candidate.number not in read_order:
            read_order.append(candidate.number)
            _emit(
                on_progress,
                "research",
                "read",
                page=page.title,
                reference=candidate.title,
                reads=len(read_order),
                minimum=minimum_reads,
            )
        return (
            f"ページ {candidate.number:03d}: {candidate.title}\n"
            f"原文範囲: {_ranges_text(candidate.owner_ranges)}行\n\n"
            + _numbered_source(
                lines, candidate.owner_ranges, _page_units(candidate, units)
            )
        )

    def finish_research(report: str, cited_page_numbers: list[int]) -> str:
        """Finish only after required pages and enough distinct references were read."""

        error = _reference_finish_error(
            read_order, required, minimum_reads, report
        )
        if error:
            return error
        cited = [number for number in cited_page_numbers if number in read_order]
        finished["report"] = str(report).strip()
        finished["cited"] = list(dict.fromkeys(cited))
        return "調査完了。これ以上ツールを呼ばず終了すること。"

    tools = [
        StructuredTool.from_function(
            search_references,
            name="search_references",
            description=search_references.__doc__ or "",
        ),
        StructuredTool.from_function(
            read_reference,
            name="read_reference",
            description=read_reference.__doc__ or "",
        ),
        StructuredTool.from_function(
            finish_research,
            name="finish_research",
            description=finish_research.__doc__ or "",
        ),
    ]
    system = (
        "あなたは日本語技術Wikiの参照調査エージェントである。"
        "対象ページ以外の本文は推測せず、必ずread_referenceで読んでから判断する。\n"
        "最初に隣接ページを読み、その後、一覧とsearch_referencesを使って必要なだけ調査する。"
        "対象の目的、前提、用語、関係、使い方、制約、注意、例外、境界から漏れた続きが"
        "他ページにないか確認する。同じ説明の重複や単なる一覧は追加情報にしない。\n"
        "十分に調査したらfinish_researchを呼ぶ。reportには、読んだ各ページについて"
        "『追加すべき事実と正確な原文行』または『追加不要と判断した具体的理由』を書く。"
        "finish_researchが終了不可を返したら指示されたページを読み、再度呼ぶ。"
    )
    catalog = "\n".join(
        f"- {item.number:03d} | {item.title} | 原文 {_ranges_text(item.owner_ranges)}行 | "
        f"{item.summary or '要約なし'}"
        for item in others
    )
    user_prompt = (
        f"対象ページ: {page.number:03d} {page.title}\n"
        f"対象原文範囲: {_ranges_text(page.owner_ranges)}行\n"
        f"終了前に最低{minimum_reads}ページを読む必要がある。件数の上限はない。\n"
        f"必ず読む隣接ページ: {', '.join(f'{number:03d}' for number in sorted(required)) or 'なし'}\n\n"
        "対象ページ本文:\n"
        + _numbered_source(lines, page.owner_ranges, _page_units(page, units))
        + "\n参照ページ一覧:\n"
        + catalog
    )
    last_error = ""
    for attempt in range(1, max(1, config.reference_attempts) + 1):
        if stop_check and stop_check():
            raise asyncio.CancelledError("reference research cancelled")
        prompt_path = research_dir / f"agent-attempt-{attempt:02d}-prompt.md"
        write_text_atomic(prompt_path, system + "\n\n" + user_prompt)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                graph = create_react_agent(
                    llm, tools=tools, prompt=system, version="v2"
                )
            state = await graph.ainvoke(
                {"messages": [{"role": "user", "content": user_prompt}]},
                config={
                    "recursion_limit": max(20, config.reference_max_steps * 2 + 4),
                    "max_concurrency": 1,
                },
            )
            trace = "\n\n".join(
                f"[{getattr(message, 'type', 'message')}]\n{getattr(message, 'content', '')}"
                for message in state.get("messages", [])
            )
            write_text_atomic(
                research_dir / f"agent-attempt-{attempt:02d}-trace.md", trace
            )
            if finished["report"]:
                write_json_atomic(
                    research_dir / "agent-result.json",
                    {
                        "read_page_numbers": read_order,
                        "cited_page_numbers": finished["cited"],
                        "report": finished["report"],
                    },
                )
                return read_order, finished["report"], "", attempt
            last_error = "research agent exited without calling finish_research"
        except Exception as exc:  # noqa: BLE001 - retry the bounded agent run
            last_error = f"{type(exc).__name__}: {exc}"[:1000]
        write_text_atomic(
            research_dir / f"agent-attempt-{attempt:02d}-error.txt",
            last_error + "\n",
        )
        user_prompt += (
            "\n\n前回はfinish_researchで完了しなかった。既に読んだページ: "
            + (", ".join(f"{number:03d}" for number in read_order) or "なし")
            + "。調査を続け、必ずfinish_researchを呼ぶこと。"
        )
    return (
        read_order,
        str(finished["report"]),
        last_error,
        max(1, config.reference_attempts),
    )


async def _research_references(
    page: SeedPage,
    *,
    pages: Sequence[SeedPage],
    lines: Sequence[str],
    units: Sequence[ImageUnit],
    model: ModelPort,
    config: NeoConfig,
    work_root: Path,
    seed_root: Path,
    stop_check: StopCheck,
    on_progress: Progress,
) -> tuple[list[_ReferenceEvidence], str]:
    """Force an agent to read references, then compare every read seed in code."""

    others = [item for item in pages if item.number != page.number]
    if not others:
        return [], "# 参照調査結果\n\n他のWikiページはない。\n"

    research_dir = clean_workdir(work_root / f"research-{page.number:03d}")
    target_source = _numbered_source(
        lines, page.owner_ranges, _page_units(page, units)
    )
    _emit(on_progress, "research", "agent_start", page=page.title)
    (
        selected_numbers,
        researcher_report,
        agent_error,
        agent_attempts,
    ) = await _run_reference_agent(
        page,
        pages=pages,
        lines=lines,
        units=units,
        model=model,
        config=config,
        research_dir=research_dir,
        stop_check=stop_check,
        on_progress=on_progress,
    )
    selection_attempts = 0
    selection_error = ""
    if agent_error:
        summaries = "\n".join(
            f"- {item.number:03d} | {item.title} | 原文 {_ranges_text(item.owner_ranges)}行 | "
            f"{item.summary or '要約なし'}"
            for item in others
        )
        selection_prompt = reference_selection_prompt(
            page_number=page.number,
            page_title=page.title,
            owner_ranges=_ranges_text(page.owner_ranges),
            numbered_original=target_source,
            page_summaries=summaries,
            output_language=config.output_language,
        )
        selection, selection_attempts, selection_error = (
            await _structured_with_artifacts(
                schema=ReferenceSelection,
                prompt=selection_prompt,
                model=model,
                output_dir=research_dir,
                stem="selection-fallback",
                attempts=config.reference_attempts,
                max_output_tokens=config.reference_max_output_tokens,
                stop_check=stop_check,
            )
        )
        page_position = next(
            index
            for index, candidate in enumerate(pages)
            if candidate.number == page.number
        )
        if page_position > 0:
            selected_numbers.append(pages[page_position - 1].number)
        if page_position + 1 < len(pages):
            selected_numbers.append(pages[page_position + 1].number)
        if selection is not None:
            selected_numbers.extend(selection.page_numbers)
        researcher_report = (
            "ツール型調査エージェントを利用できなかったため、候補選定を構造化呼び出しで補完した。"
        )
        selection_error = selection_error or agent_error
    candidate_by_number = {item.number: item for item in others}
    selected: list[SeedPage] = []
    for number in selected_numbers:
        candidate = candidate_by_number.get(number)
        if candidate is None or candidate in selected:
            continue
        selected.append(candidate)
    _emit(
        on_progress,
        "research",
        "agent_done",
        page=page.title,
        attempts=agent_attempts or selection_attempts,
        candidates=len(selected),
        error=selection_error,
    )

    evidence: list[_ReferenceEvidence] = []
    for current, candidate in enumerate(selected, start=1):
        reference_source = _numbered_source(
            lines, candidate.owner_ranges, _page_units(candidate, units)
        )
        prompt = reference_research_prompt(
            target_number=page.number,
            target_title=page.title,
            target_ranges=_ranges_text(page.owner_ranges),
            target_source=target_source,
            reference_number=candidate.number,
            reference_title=candidate.title,
            reference_ranges=_ranges_text(candidate.owner_ranges),
            reference_source=reference_source,
            output_language=config.output_language,
        )
        result, attempts, error = await _structured_with_artifacts(
            schema=ReferenceResearchResult,
            prompt=prompt,
            model=model,
            output_dir=research_dir,
            stem=f"reference-{candidate.number:03d}",
            attempts=config.reference_attempts,
            max_output_tokens=config.reference_max_output_tokens,
            stop_check=stop_check,
        )
        facts = _valid_reference_facts(result.useful_facts, candidate) if result else []
        reason = (
            result.no_useful_information_reason.strip()
            if result is not None
            else f"調査呼び出し失敗: {error}"
        )
        evidence.append(
            _ReferenceEvidence(
                page=candidate,
                facts=facts,
                no_useful_information_reason=reason,
            )
        )
        _emit(
            on_progress,
            "research",
            "reference_done",
            page=page.title,
            reference=candidate.title,
            current=current,
            total=len(selected),
            facts=len(facts),
            attempts=attempts,
            error=error,
        )

    research = _render_reference_research(
        page,
        evidence,
        lines=lines,
        units=units,
        seed_root=seed_root,
        researcher_report=researcher_report,
    )
    write_text_atomic(research_dir / "reference-research.md", research)
    return evidence, research


def _judge_feedback(result: PageJudgeResult) -> list[str]:
    feedback: list[str] = []
    for omission in result.missing_important_information:
        location = (
            f"原文 {omission.source_start}-{omission.source_end}行"
            if omission.source_start > 0 and omission.source_end >= omission.source_start
            else "原文範囲未指定"
        )
        feedback.append(f"{location}: {omission.description.strip()}")
    return feedback


async def _judge_wiki_plan(
    *,
    page: SeedPage,
    numbered_original: str,
    reference_research: str,
    wiki_plan: str,
    model: ModelPort,
    config: NeoConfig,
    task_dir: Path,
    stop_check: StopCheck,
) -> tuple[WikiPlanJudgeResult | None, str]:
    prompt = wiki_plan_judge_prompt(
        page_title=page.title,
        numbered_original=numbered_original,
        reference_research=reference_research,
        wiki_plan=wiki_plan,
        output_language=config.output_language,
    )
    result, _attempts, error = await _structured_with_artifacts(
        schema=WikiPlanJudgeResult,
        prompt=prompt,
        model=model,
        output_dir=task_dir,
        stem="plan-review",
        attempts=config.judge_attempts,
        max_output_tokens=config.judge_max_output_tokens,
        stop_check=stop_check,
    )
    return result, error


async def _judge_candidate(
    *,
    page: SeedPage,
    numbered_original: str,
    candidate: str,
    model: ModelPort,
    config: NeoConfig,
    task_dir: Path,
    version: int,
) -> tuple[PageJudgeResult | None, int, str]:
    """Judge important information coverage, with only bounded API retries."""

    prompt = page_judge_prompt(
        page_title=page.title,
        owner_ranges=_ranges_text(page.owner_ranges),
        numbered_original=numbered_original,
        candidate=candidate,
        output_language=config.output_language,
    )
    last_error = ""
    for attempt in range(1, max(1, config.judge_attempts) + 1):
        write_text_atomic(
            task_dir / f"judge-{version:02d}-attempt-{attempt:02d}-prompt.md",
            prompt.render(),
        )
        try:
            raw = await model.structured(
                PageJudgeResult,
                prompt.messages(),
                max_output_tokens=config.judge_max_output_tokens,
            )
            result = (
                raw
                if isinstance(raw, PageJudgeResult)
                else PageJudgeResult.model_validate(raw)
            )
            write_json_atomic(task_dir / f"judge-{version:02d}.json", result)
            return result, attempt, ""
        except Exception as exc:  # noqa: BLE001 - bounded retry, then keep the page
            last_error = f"{type(exc).__name__}: {exc}"[:1000]
            write_text_atomic(
                task_dir / f"judge-{version:02d}-attempt-{attempt:02d}-error.txt",
                last_error + "\n",
            )
    return None, max(1, config.judge_attempts), last_error


async def _judge_reference_enrichment(
    *,
    page: SeedPage,
    reference_research: str,
    reference_ranges: Sequence[tuple[int, int]],
    candidate: str,
    model: ModelPort,
    config: NeoConfig,
    task_dir: Path,
    version: int,
    stop_check: StopCheck,
) -> tuple[PageJudgeResult | None, int, str]:
    """Verify that researched cross-page facts and their citations were used."""

    if not reference_ranges:
        return PageJudgeResult(coverage_score=100), 0, ""
    prompt = reference_enrichment_judge_prompt(
        page_title=page.title,
        reference_research=reference_research,
        candidate=candidate,
        output_language=config.output_language,
    )
    result, attempts, error = await _structured_with_artifacts(
        schema=PageJudgeResult,
        prompt=prompt,
        model=model,
        output_dir=task_dir,
        stem=f"enrichment-judge-{version:02d}",
        attempts=config.judge_attempts,
        max_output_tokens=config.judge_max_output_tokens,
        stop_check=stop_check,
    )
    return result, attempts, error


def _valid_reference_ranges(value: Sequence[Sequence[int]], source_line_count: int) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for item in value:
        if len(item) != 2:
            continue
        start, end = int(item[0]), int(item[1])
        if 1 <= start <= end <= source_line_count:
            result.append((start, end))
    return _merge_ranges(result)


def _load_seed_plan(
    path: Path,
    *,
    source_sha256: str,
    source_line_count: int,
    prompt_version: str,
) -> list[SeedPage] | None:
    """Load the completed seed plan so a stopped run resumes at rewriting."""

    if not path.exists():
        return None
    try:
        raw = read_json(path)
        if raw.get("source_sha256") != source_sha256:
            return None
        if int(raw.get("source_line_count", 0)) != source_line_count:
            return None
        if raw.get("prompt_version") != prompt_version:
            return None
        pages = [
            SeedPage(
                number=int(item["number"]),
                title=str(item["title"]),
                chapter=str(item.get("chapter", "")),
                summary=str(item.get("summary", "")),
                owner_ranges=_merge_ranges(
                    [(int(start), int(end)) for start, end in item["owner_ranges"]]
                ),
                filename=str(item["filename"]),
                page_id=f"page-{int(item['number']):03d}",
                reference_ranges=_valid_reference_ranges(
                    item.get("reference_ranges", []), source_line_count
                ),
            )
            for item in raw["pages"]
        ]
        if not pages or [page.number for page in pages] != list(range(1, len(pages) + 1)):
            return None
        if len({page.filename for page in pages}) != len(pages):
            return None
        if any(Path(page.filename).name != page.filename for page in pages):
            return None
        _verify_ranges(pages, source_line_count)
        return pages
    except (KeyError, OSError, TypeError, ValueError, PipelineError):
        return None


_INTERNAL_FRONTMATTER = re.compile(
    r"\A---\r?\n(?P<header>.*?)\r?\n---\r?\n?", re.DOTALL
)


def _strip_internal_frontmatter(markdown: str) -> tuple[str, str]:
    """Remove only frontmatter created by this pipeline, not article content."""

    match = _INTERNAL_FRONTMATTER.match(markdown)
    if not match:
        return markdown, ""
    header = match.group("header")
    if "page_number:" not in header or "status:" not in header:
        return markdown, ""
    return markdown[match.end() :], header


def _page_state_path(state_root: Path, page: SeedPage) -> Path:
    return state_root / "pages" / f"{page.number:03d}.json"


def _write_page_state(
    path: Path,
    result: RewriteResult,
    *,
    rewrite_version: str,
    pages: Sequence[SeedPage] = (),
    source_path: Path | None = None,
    source_snapshot_path: Path | None = None,
    source_sha256: str = "",
) -> None:
    write_json_atomic(
        path,
        {
            "number": result.page.number,
            "title": result.page.title,
            "filename": result.page.filename,
            "status": "rewritten",
            "rewrite_version": rewrite_version,
            "source_ranges": _ranges_json(result.page.owner_ranges),
            "reference_ranges": _ranges_json(result.page.reference_ranges),
            "provenance": _page_provenance(
                result.page,
                pages,
                source_path=source_path,
                source_snapshot_path=source_snapshot_path,
                source_sha256=source_sha256,
            ),
            "content_sha256": sha256_text(result.markdown),
            "selected_version": result.selected_version,
            "judge_score": result.judge_score,
            "missing_important_information": result.missing_important_information,
            "judge_notes": result.judge_notes,
            "linked": result.linked,
            "link_version": LINK_PROMPT_VERSION if result.linked else "",
            "link_attempts": result.link_attempts,
            "link_error": result.link_error,
        },
    )


def _resume_rewritten_page(
    output_path: Path,
    state_path: Path,
    page: SeedPage,
    *,
    rewrite_version: str,
    source_line_count: int,
    pages: Sequence[SeedPage] = (),
    source_path: Path | None = None,
    source_snapshot_path: Path | None = None,
    source_sha256: str = "",
) -> RewriteResult | None:
    """Resume clean pages and migrate old frontmatter pages into sidecar state."""

    if not output_path.exists():
        return None
    try:
        markdown = output_path.read_text(encoding="utf-8")
        if state_path.exists():
            state = read_json(state_path)
            if (
                state.get("rewrite_version") == rewrite_version
                and state.get("filename") == page.filename
                and state.get("content_sha256") == sha256_text(markdown)
            ):
                page.reference_ranges = _valid_reference_ranges(
                    state.get("reference_ranges", []), source_line_count
                )
                result = RewriteResult(
                    page=page,
                    markdown=markdown,
                    attempts=0,
                    selected_version=int(state.get("selected_version", 0)),
                    judge_score=state.get("judge_score"),
                    missing_important_information=list(
                        state.get("missing_important_information", [])
                    ),
                    judge_notes=str(state.get("judge_notes", "")),
                    linked=(
                        bool(state.get("linked"))
                        and state.get("link_version") == LINK_PROMPT_VERSION
                    ),
                    link_attempts=int(state.get("link_attempts", 0)),
                    link_error=str(state.get("link_error", "")),
                )
                _write_page_state(
                    state_path,
                    result,
                    rewrite_version=rewrite_version,
                    pages=pages,
                    source_path=source_path,
                    source_snapshot_path=source_snapshot_path,
                    source_sha256=source_sha256,
                )
                return result

        body, header = _strip_internal_frontmatter(markdown)
        if (
            header
            and ('status: "rewritten"' in header or "status: rewritten" in header)
            and f"rewrite_version: {_yaml_string(rewrite_version)}" in header
        ):
            page.reference_ranges = _reference_ranges_from_markdown(
                body, page.owner_ranges, source_line_count
            )
            migrated = RewriteResult(page=page, markdown=body.rstrip() + "\n", attempts=0)
            write_text_atomic(output_path, migrated.markdown)
            _write_page_state(
                state_path,
                migrated,
                rewrite_version=rewrite_version,
                pages=pages,
                source_path=source_path,
                source_snapshot_path=source_snapshot_path,
                source_sha256=source_sha256,
            )
            return migrated
    except (OSError, TypeError, ValueError):
        return None
    return None


async def _rewrite_page(
    page: SeedPage,
    *,
    pages: Sequence[SeedPage],
    lines: list[str],
    units: Sequence[ImageUnit],
    agent: AgentPort,
    judge_model: ModelPort,
    config: NeoConfig,
    work_root: Path,
    seed_root: Path,
    source_line_count: int,
    stop_check: StopCheck,
    on_progress: Progress,
) -> RewriteResult:
    page_units = _page_units(page, units)
    source_body = _slice_ranges(lines, page.owner_ranges)
    seed_page = _render_page(page, source_body, status="seed")
    prompt_page = _prompt_safe(source_body, page_units)
    numbered_original = _numbered_source(lines, page.owner_ranges, page_units)
    last_error = ""
    max_attempts = max(1, config.rewrite_attempts)
    work_attempt = 0
    candidate_input = prompt_page
    missing_information: list[str] = []
    candidates: list[_JudgedCandidate] = []
    reference_evidence, reference_research = await _research_references(
        page,
        pages=pages,
        lines=lines,
        units=units,
        model=judge_model,
        config=config,
        work_root=work_root,
        seed_root=seed_root,
        stop_check=stop_check,
        on_progress=on_progress,
    )
    researched_ranges = _researched_ranges(reference_evidence)

    for version in range(1, max(1, config.rewrite_versions) + 1):
        edited_page = ""
        task_dir: Path | None = None
        while work_attempt < max_attempts:
            work_attempt += 1
            if stop_check and stop_check():
                raise asyncio.CancelledError("page rewriting cancelled")
            task_dir = clean_workdir(
                work_root / f"rewrite-{page.number:03d}-attempt-{work_attempt:02d}"
            )
            page_file = (task_dir / "page.md").resolve()
            original_file = (task_dir / "original.md").resolve()
            plan_file = (task_dir / "wiki-plan.md").resolve()
            index_file = (task_dir / "page-index.md").resolve()
            references_file = (task_dir / "references.md").resolve()
            research_file = (task_dir / "reference-research.md").resolve()
            local_seed_root, local_source = _stage_rewrite_references(
                task_dir,
                pages,
                lines=lines,
                units=units,
                seed_root=seed_root,
            )
            index_text = _page_index(pages, local_seed_root, local_source)
            references = _reference_view(
                page, pages, local_seed_root, local_source
            )
            write_text_atomic(original_file, prompt_page)
            write_text_atomic(page_file, candidate_input)
            write_text_atomic(index_file, index_text)
            write_text_atomic(references_file, references)
            write_text_atomic(research_file, reference_research)
            empty_plan = "# Wiki記事作成計画\n\n（調査後、この文を具体的な計画で置き換える）\n"
            write_text_atomic(plan_file, empty_plan)
            plan_prompt = wiki_page_plan_prompt(
                page_number=page.number,
                page_title=page.title,
                working_page_path=str(page_file),
                plan_path=str(plan_file),
                page_index_path=str(index_file),
                references_path=str(references_file),
                owner_ranges=_ranges_text(page.owner_ranges),
                image_context=_image_context(page_units, lines),
                output_language=config.output_language,
                reference_research_path=str(research_file),
                reference_research=reference_research,
                missing_information=missing_information,
                last_error=last_error,
            )
            _emit(
                on_progress,
                "rewrite",
                "plan_start",
                page=page.title,
                version=version,
                attempt=work_attempt,
            )
            plan_reply = await agent.run(
                plan_prompt,
                None,
                workdir=task_dir,
                artifact="plan",
                session=f"neo-page-{page.number:03d}-plan-{work_attempt:02d}",
                max_output_tokens=config.rewrite_max_output_tokens,
                stop_check=stop_check,
            )
            if not plan_reply.ok:
                last_error = plan_reply.error or "Wiki planning failed"
                _emit(
                    on_progress,
                    "rewrite",
                    "plan_retry",
                    page=page.title,
                    version=version,
                    attempt=work_attempt,
                    error=last_error,
                )
                continue
            try:
                wiki_plan = plan_file.read_text(encoding="utf-8")
            except OSError as exc:
                last_error = f"could not read wiki-plan.md: {exc}"
                _emit(
                    on_progress,
                    "rewrite",
                    "plan_retry",
                    page=page.title,
                    version=version,
                    attempt=work_attempt,
                    error=last_error,
                )
                continue
            if not wiki_plan.strip() or wiki_plan == empty_plan:
                last_error = "planner exited without writing wiki-plan.md"
                _emit(
                    on_progress,
                    "rewrite",
                    "plan_retry",
                    page=page.title,
                    version=version,
                    attempt=work_attempt,
                    error=last_error,
                )
                continue
            plan_review, plan_review_error = await _judge_wiki_plan(
                page=page,
                numbered_original=numbered_original,
                reference_research=reference_research,
                wiki_plan=wiki_plan,
                model=judge_model,
                config=config,
                task_dir=task_dir,
                stop_check=stop_check,
            )
            if plan_review is None or not plan_review.acceptable:
                issues = plan_review.issues if plan_review is not None else []
                last_error = (
                    "; ".join(item.strip() for item in issues if item.strip())
                    or plan_review_error
                    or "Wiki計画が原文章立ての整理に留まっている"
                )
                _emit(
                    on_progress,
                    "rewrite",
                    "plan_retry",
                    page=page.title,
                    version=version,
                    attempt=work_attempt,
                    error=last_error,
                )
                continue
            # Planning is read-only apart from wiki-plan.md. Restore the exact
            # writer input in case a small model edited page.md prematurely.
            write_text_atomic(page_file, candidate_input)
            _emit(
                on_progress,
                "rewrite",
                "plan_done",
                page=page.title,
                version=version,
                attempt=work_attempt,
                output=str(plan_file),
            )
            prompt = simple_page_edit_prompt(
                page_number=page.number,
                page_title=page.title,
                final_page_path=page.filename,
                working_page_path=str(page_file),
                plan_path=str(plan_file),
                original_path=str(original_file),
                page_index_path=str(index_file),
                references_path=str(references_file),
                owner_ranges=_ranges_text(page.owner_ranges),
                image_context=_image_context(page_units, lines),
                output_language=config.output_language,
                reference_research_path=str(research_file),
                reference_research=reference_research,
                missing_information=missing_information,
                last_error=last_error,
            )
            _emit(
                on_progress,
                "rewrite",
                "write_start",
                page=page.title,
                version=version,
                attempt=work_attempt,
            )
            reply = await agent.run(
                prompt,
                None,
                workdir=task_dir,
                artifact="rewrite",
                session=f"neo-page-{page.number:03d}-attempt-{work_attempt:02d}",
                max_output_tokens=config.rewrite_max_output_tokens,
                stop_check=stop_check,
            )
            if not reply.ok:
                last_error = reply.error or "page edit worker failed"
                _emit(
                    on_progress,
                    "rewrite",
                    "write_retry",
                    page=page.title,
                    version=version,
                    attempt=work_attempt,
                    error=last_error,
                )
                continue
            try:
                edited_page = page_file.read_text(encoding="utf-8")
            except OSError as exc:
                last_error = f"could not read edited page.md: {exc}"
                _emit(
                    on_progress,
                    "rewrite",
                    "write_retry",
                    page=page.title,
                    version=version,
                    attempt=work_attempt,
                    error=last_error,
                )
                continue
            if not edited_page.strip():
                last_error = "edited page.md is empty"
                _emit(
                    on_progress,
                    "rewrite",
                    "write_retry",
                    page=page.title,
                    version=version,
                    attempt=work_attempt,
                    error=last_error,
                )
                continue
            if edited_page == candidate_input:
                last_error = "writer exited without changing page.md"
                _emit(
                    on_progress,
                    "rewrite",
                    "write_retry",
                    page=page.title,
                    version=version,
                    attempt=work_attempt,
                    error=last_error,
                )
                continue
            edited_page = _preserve_image_placeholders(
                edited_page, page_units, lines
            )
            write_text_atomic(page_file, edited_page)
            break

        if not edited_page or task_dir is None:
            break

        judgment, judge_attempts, judge_error = await _judge_candidate(
            page=page,
            numbered_original=numbered_original,
            candidate=edited_page,
            model=judge_model,
            config=config,
            task_dir=task_dir,
            version=version,
        )
        if judgment is None:
            candidates.append(
                _JudgedCandidate(
                    markdown=edited_page,
                    version=version,
                    score=-1,
                    missing=[],
                    notes=judge_error,
                    judged=False,
                )
            )
            _emit(
                on_progress,
                "judge",
                "unavailable",
                page=page.title,
                version=version,
                attempts=judge_attempts,
                error=judge_error,
            )
            break

        enrichment, enrichment_attempts, enrichment_error = (
            await _judge_reference_enrichment(
                page=page,
                reference_research=reference_research,
                reference_ranges=researched_ranges,
                candidate=edited_page,
                model=judge_model,
                config=config,
                task_dir=task_dir,
                version=version,
                stop_check=stop_check,
            )
        )
        if enrichment is None:
            candidates.append(
                _JudgedCandidate(
                    markdown=edited_page,
                    version=version,
                    score=-1,
                    missing=[],
                    notes=enrichment_error,
                    judged=False,
                )
            )
            _emit(
                on_progress,
                "judge",
                "enrichment_unavailable",
                page=page.title,
                version=version,
                attempts=enrichment_attempts,
                error=enrichment_error,
            )
            break

        source_missing = _judge_feedback(judgment)
        reference_missing = [
            "参照情報の不足: " + item for item in _judge_feedback(enrichment)
        ]
        reference_missing.extend(
            _missing_research_markers(
                edited_page,
                reference_evidence,
                owner_ranges=page.owner_ranges,
                source_line_count=source_line_count,
            )
        )
        missing_information = source_missing + reference_missing
        combined_score = min(judgment.coverage_score, enrichment.coverage_score)
        candidates.append(
            _JudgedCandidate(
                markdown=edited_page,
                version=version,
                score=combined_score,
                missing=missing_information,
                notes=" / ".join(
                    item
                    for item in (judgment.notes, enrichment.notes)
                    if item.strip()
                ),
                reference_ranges=([] if reference_missing else researched_ranges),
            )
        )
        _emit(
            on_progress,
            "judge",
            "page_done",
            page=page.title,
            version=version,
            attempts=judge_attempts,
            score=combined_score,
            missing=len(missing_information),
            enrichment_score=enrichment.coverage_score,
        )
        if not missing_information:
            break
        candidate_input = edited_page
        last_error = ""

    if candidates:
        best = max(
            candidates,
            key=lambda item: (
                item.judged and not item.missing,
                item.judged,
                item.score,
                -len(item.missing),
                item.version,
            ),
        )
        restored, _ = restore_images(best.markdown, page_units)
        page.reference_ranges = _merge_ranges(
            [
                *best.reference_ranges,
                *_reference_ranges_from_markdown(
                    restored, page.owner_ranges, source_line_count
                ),
            ]
        )
        return RewriteResult(
            page=page,
            markdown=_render_page(
                page,
                restored,
                status="rewritten",
                rewrite_version=REWRITE_PROMPT_VERSION,
            ),
            attempts=work_attempt,
            selected_version=best.version,
            judge_score=None if best.score < 0 else best.score,
            missing_important_information=best.missing,
            judge_notes=best.notes,
        )

    return RewriteResult(
        page=page,
        markdown=seed_page,
        attempts=work_attempt,
        fallback=True,
        error=last_error or "edit unavailable; page was not published",
    )


async def _rewrite_all(
    pages: Sequence[SeedPage],
    *,
    lines: list[str],
    units: Sequence[ImageUnit],
    agent: AgentPort,
    judge_model: ModelPort,
    config: NeoConfig,
    work_root: Path,
    seed_root: Path,
    wiki_root: Path,
    state_root: Path,
    source_path: Path,
    source_snapshot_path: Path,
    source_sha256: str,
    source_line_count: int,
    stop_check: StopCheck,
    on_progress: Progress,
) -> list[RewriteResult]:
    semaphore = asyncio.Semaphore(max(1, config.rewrite_concurrency))

    results: list[RewriteResult] = []
    pending: list[SeedPage] = []
    for page in pages:
        output_path = wiki_root / page.filename
        state_path = _page_state_path(state_root, page)
        resumed = (
            _resume_rewritten_page(
                output_path,
                state_path,
                page,
                rewrite_version=REWRITE_PROMPT_VERSION,
                source_line_count=source_line_count,
                pages=pages,
                source_path=source_path,
                source_snapshot_path=source_snapshot_path,
                source_sha256=source_sha256,
            )
            if config.resume
            else None
        )
        if resumed is not None:
            results.append(resumed)
            _emit(
                on_progress,
                "rewrite",
                "page_resumed",
                current=len(results),
                total=len(pages),
                page=page.title,
            )
        else:
            # Seed pages are intermediate input, not publishable wiki pages.
            output_path.unlink(missing_ok=True)
            state_path.unlink(missing_ok=True)
            for old_attempt in work_root.glob(
                f"rewrite-{page.number:03d}-attempt-*"
            ):
                if old_attempt.is_dir():
                    shutil.rmtree(old_attempt)
            pending.append(page)

    async def one(page: SeedPage) -> RewriteResult:
        async with semaphore:
            return await _rewrite_page(
                page,
                pages=pages,
                lines=lines,
                units=units,
                agent=agent,
                judge_model=judge_model,
                config=config,
                work_root=work_root,
                seed_root=seed_root,
                source_line_count=source_line_count,
                stop_check=stop_check,
                on_progress=on_progress,
            )

    for completed, task in enumerate(
        asyncio.as_completed([one(page) for page in pending]), start=len(results) + 1
    ):
        result = await task
        results.append(result)
        if not result.fallback:
            write_text_atomic(wiki_root / result.page.filename, result.markdown)
            _write_page_state(
                _page_state_path(state_root, result.page),
                result,
                rewrite_version=REWRITE_PROMPT_VERSION,
                pages=pages,
                source_path=source_path,
                source_snapshot_path=source_snapshot_path,
                source_sha256=source_sha256,
            )
        _emit(
            on_progress,
            "rewrite",
            "page_done",
            current=completed,
            total=len(pages),
            page=result.page.title,
            attempts=result.attempts,
            fallback=result.fallback,
            version=result.selected_version,
            score=result.judge_score,
        )
    return sorted(results, key=lambda item: item.page.number)


_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")


def _without_link_markup(markdown: str) -> str:
    return _MARKDOWN_LINK.sub(lambda match: match.group(1), markdown)


def _validate_additive_links(
    original: str,
    candidate: str,
    *,
    allowed_filenames: set[str],
    own_filename: str,
) -> str:
    """Accept only inline link wrappers; visible article text must be identical."""

    from collections import Counter

    if _strip_internal_frontmatter(candidate)[1]:
        return "YAML frontmatter was added"
    if _without_link_markup(candidate) != _without_link_markup(original):
        return (
            "only inline links may be added; visible text, headings, ordering, images, "
            "and reference markers must remain byte-identical"
        )

    before = Counter(_MARKDOWN_LINK.findall(original))
    after = Counter(_MARKDOWN_LINK.findall(candidate))
    if any(after[item] < count for item, count in before.items()):
        return "an existing Markdown link was removed or changed"
    for (label, raw_target), count in (after - before).items():
        target = raw_target.strip().strip("<>").split("#", 1)[0]
        if count <= 0:
            continue
        if target == own_filename:
            return f"self-link is not allowed: {label} -> {raw_target}"
        if target not in allowed_filenames:
            return f"link target does not exist in this wiki: {raw_target}"
    return ""


def _stage_link_references(
    task_dir: Path,
    pages: Sequence[SeedPage],
    snapshot_root: Path,
) -> Path:
    """Place stable copies of all completed Wiki pages inside the link task."""

    local_root = (task_dir / "wiki-pages").resolve()
    local_root.mkdir(parents=True, exist_ok=True)
    index_lines = ["# 完成Wikiページ一覧", ""]
    for page in pages:
        target = local_root / page.filename
        write_text_atomic(
            target, (snapshot_root / page.filename).read_text(encoding="utf-8")
        )
        imported_from = _referenced_page_records(page, pages)
        imported_names = ", ".join(
            item["filename"] for item in imported_from
        ) or "なし"
        index_lines.append(
            f"- `{page.filename}` — {page.title}\n"
            f"  - 内容要約: {page.summary or '要約なし'}\n"
            f"  - 自身の原文範囲: {_ranges_text(page.owner_ranges)}\n"
            f"  - 追加利用した原文範囲: {_ranges_text(page.reference_ranges)}\n"
            f"  - 追加情報を所有するWiki: {imported_names}\n"
            f"  - 読み取り用絶対パス: `{target.resolve()}`"
        )
    index_path = (task_dir / "page-index.md").resolve()
    write_text_atomic(index_path, "\n".join(index_lines) + "\n")
    return index_path


async def _link_page(
    result: RewriteResult,
    *,
    pages: Sequence[SeedPage],
    units: Sequence[ImageUnit],
    agent: AgentPort,
    config: NeoConfig,
    work_root: Path,
    snapshot_root: Path,
    stop_check: StopCheck,
) -> RewriteResult:
    page = result.page
    original = result.markdown.rstrip() + "\n"
    page_units = _page_units(page, units)
    prompt_page = _prompt_safe(original, page_units)
    allowed = {item.filename for item in pages}
    last_error = ""

    for attempt in range(1, max(1, config.link_attempts) + 1):
        if stop_check and stop_check():
            raise asyncio.CancelledError("wiki linking cancelled")
        task_dir = clean_workdir(
            work_root / f"link-{page.number:03d}-attempt-{attempt:02d}"
        )
        page_file = (task_dir / "page.md").resolve()
        original_file = (task_dir / "original.md").resolve()
        index_file = _stage_link_references(task_dir, pages, snapshot_root)
        write_text_atomic(page_file, prompt_page)
        write_text_atomic(original_file, prompt_page)
        prompt = link_page_prompt(
            page_title=page.title,
            working_page_path=str(page_file),
            original_path=str(original_file),
            page_index_path=str(index_file),
            output_language=config.output_language,
            last_error=last_error or None,
        )
        reply = await agent.run(
            prompt,
            None,
            workdir=task_dir,
            artifact="link",
            session=f"neo-link-{page.number:03d}-attempt-{attempt:02d}",
            max_output_tokens=config.link_max_output_tokens,
            stop_check=stop_check,
        )
        if not reply.ok:
            last_error = reply.error or "link edit worker failed"
            continue
        try:
            candidate = page_file.read_text(encoding="utf-8")
        except OSError as exc:
            last_error = f"could not read linked page.md: {exc}"
            continue
        error = _validate_additive_links(
            prompt_page,
            candidate,
            allowed_filenames=allowed,
            own_filename=page.filename,
        )
        if error:
            last_error = error
            continue
        restored, unresolved = restore_images(candidate, page_units)
        if unresolved:
            last_error = "unresolved image placeholders: " + ", ".join(unresolved)
            continue
        result.markdown = restored.rstrip() + "\n"
        result.linked = True
        result.link_attempts = attempt
        result.link_error = ""
        return result

    result.link_attempts = max(1, config.link_attempts)
    result.link_error = last_error or "link pass unavailable; original article retained"
    return result


async def _link_all(
    results: Sequence[RewriteResult],
    *,
    pages: Sequence[SeedPage],
    units: Sequence[ImageUnit],
    agent: AgentPort,
    config: NeoConfig,
    work_root: Path,
    wiki_root: Path,
    state_root: Path,
    source_path: Path,
    source_snapshot_path: Path,
    source_sha256: str,
    stop_check: StopCheck,
    on_progress: Progress,
) -> list[RewriteResult]:
    """Run an additive inline-link pass over a stable snapshot in parallel."""

    _emit(on_progress, "link", "start", pages=len(results))
    snapshot_root = work_root / "link-source"
    if snapshot_root.exists():
        shutil.rmtree(snapshot_root)
    snapshot_root.mkdir(parents=True)
    for result in results:
        write_text_atomic(
            snapshot_root / result.page.filename,
            _prompt_safe(result.markdown, _page_units(result.page, units)),
        )

    completed_results = [item for item in results if item.linked]
    pending = [item for item in results if not item.linked]
    for current, item in enumerate(completed_results, start=1):
        _emit(
            on_progress,
            "link",
            "page_resumed",
            page=item.page.title,
            current=current,
            total=len(results),
        )
    for item in pending:
        for old_attempt in work_root.glob(f"link-{item.page.number:03d}-attempt-*"):
            if old_attempt.is_dir():
                shutil.rmtree(old_attempt)

    semaphore = asyncio.Semaphore(max(1, config.link_concurrency))

    async def one(item: RewriteResult) -> RewriteResult:
        async with semaphore:
            return await _link_page(
                item,
                pages=pages,
                units=units,
                agent=agent,
                config=config,
                work_root=work_root,
                snapshot_root=snapshot_root,
                stop_check=stop_check,
            )

    linked = list(completed_results)
    for current, task in enumerate(
        asyncio.as_completed([one(item) for item in pending]),
        start=len(completed_results) + 1,
    ):
        result = await task
        linked.append(result)
        write_text_atomic(wiki_root / result.page.filename, result.markdown)
        _write_page_state(
            _page_state_path(state_root, result.page),
            result,
            rewrite_version=REWRITE_PROMPT_VERSION,
            pages=pages,
            source_path=source_path,
            source_snapshot_path=source_snapshot_path,
            source_sha256=source_sha256,
        )
        _emit(
            on_progress,
            "link",
            "page_done",
            current=current,
            total=len(results),
            page=result.page.title,
            attempts=result.link_attempts,
            fallback=not result.linked,
            error=result.link_error,
        )
    return sorted(linked, key=lambda item: item.page.number)


def _index_text(title: str, pages: Sequence[SeedPage]) -> str:
    lines = [f"# {title}", "", "ページは原文での登場順に並んでいます。", ""]
    for page in pages:
        lines.append(
            f"- [{page.title}]({page.filename}) — "
            f"{page.summary or '要約なし'} "
            f"（原文 {_ranges_text(page.owner_ranges)}行）"
        )
    return "\n".join(lines) + "\n"


def _verify_ranges(pages: Sequence[SeedPage], source_line_count: int) -> None:
    expected = 1
    for page in sorted(pages, key=lambda item: item.number):
        for start, end in page.owner_ranges:
            if start != expected:
                raise PipelineError(
                    f"seed ownership gap/overlap before page {page.number}: "
                    f"expected line {expected}, got {start}"
                )
            if end < start or end > source_line_count:
                raise PipelineError(f"invalid owner range on page {page.number}: {start}-{end}")
            expected = end + 1
    if expected != source_line_count + 1:
        raise PipelineError(
            f"seed ownership stops at line {expected - 1}; source has {source_line_count} lines"
        )


def _manifest(
    *,
    source_path: Path,
    source_snapshot_path: Path,
    source_text: str,
    pages: Sequence[SeedPage],
    results: Sequence[RewriteResult],
) -> dict[str, Any]:
    by_number = {item.page.number: item for item in results}
    source_hash = sha256_text(source_text)
    return {
        "source": str(source_path.resolve()),
        "source_snapshot": str(source_snapshot_path.resolve()),
        "source_sha256": source_hash,
        "source_line_count": len(split_source_lines(normalize_source(source_text))),
        "pages": [
            {
                "number": page.number,
                "title": page.title,
                "chapter": page.chapter,
                "filename": page.filename,
                "owner_ranges": _ranges_json(page.owner_ranges),
                "reference_ranges": _ranges_json(page.reference_ranges),
                "provenance": _page_provenance(
                    page,
                    pages,
                    source_path=source_path,
                    source_snapshot_path=source_snapshot_path,
                    source_sha256=source_hash,
                ),
                "status": "failed" if by_number[page.number].fallback else "rewritten",
                "attempts": by_number[page.number].attempts,
                "error": by_number[page.number].error,
                "selected_version": by_number[page.number].selected_version,
                "judge_score": by_number[page.number].judge_score,
                "missing_important_information": by_number[
                    page.number
                ].missing_important_information,
                "judge_notes": by_number[page.number].judge_notes,
                "linked": by_number[page.number].linked,
                "link_attempts": by_number[page.number].link_attempts,
                "link_error": by_number[page.number].link_error,
            }
            for page in pages
        ],
    }


def _prepare_agent(config: NeoConfig, model: ModelPort, work_root: Path, slug: str) -> AgentPort:
    """Create an isolated CLI-agent profile without unrelated project context."""

    if config.agent_backend == "pi":
        profile_root = (work_root / "pi-profile").resolve()
        profile_root.mkdir(parents=True, exist_ok=True)
        pi = config.pi.model_copy(update={"config_dir": str(profile_root)})
        write_json_atomic(
            profile_root / "models.json",
            {
                "providers": {
                    pi.provider: {
                        "baseUrl": pi.base_url or config.chat_base_url,
                        "api": pi.api,
                        "apiKey": pi.api_key or config.chat_api_key,
                        "models": [
                            {
                                "id": pi.model or config.chat_model,
                                "name": pi.model or config.chat_model,
                                "reasoning": pi.reasoning,
                                "input": ["text", "image"],
                                "contextWindow": pi.context_window,
                                "maxTokens": pi.max_tokens,
                            }
                        ],
                    }
                }
            },
        )
        return make_agent(config.model_copy(update={"pi": pi}), model)
    if config.agent_backend != "hermes":
        return make_agent(config, model)
    profile_root = (work_root / "hermes-profile").resolve()
    profile_root.mkdir(parents=True, exist_ok=True)
    hermes = config.hermes.model_copy(update={"home": str(profile_root)})
    write_text_atomic(
        profile_root / "config.yaml",
        "model:\n"
        f"  provider: {hermes.provider}\n"
        f"  default: {hermes.model or config.chat_model}\n"
        f"  base_url: {hermes.base_url or config.chat_base_url}\n"
        f"  api_key: {hermes.api_key or config.chat_api_key}\n"
        f"  api_mode: {hermes.api_mode}\n",
    )
    write_text_atomic(profile_root / ".env", "")
    return make_agent(config.model_copy(update={"hermes": hermes}), model)


async def run_pipeline(
    source_path: Path | str,
    *,
    config: NeoConfig | None = None,
    model: ModelPort | None = None,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
) -> Path:
    """Build seed pages, then independently plan and rewrite each page."""

    source_path = Path(source_path).resolve()
    source_bytes = source_path.read_bytes()
    source_text = source_bytes.decode("utf-8")
    normalized = normalize_source(source_text)
    lines = split_source_lines(normalized)
    config = config or NeoConfig()
    model = model or ChatModelPort(config)
    slug = slugify(config.document_slug or source_path.stem, fallback="document").casefold()
    run_root = Path(config.output_root) / f"{slug}-{sha256_text(source_text)[:12]}"
    source_root = run_root / "source"
    state_root = run_root / "state"
    work_root = run_root / "work"
    wiki_root = run_root / "wiki"
    for directory in (source_root, state_root, work_root):
        directory.mkdir(parents=True, exist_ok=True)
    wiki_root.mkdir(parents=True, exist_ok=True)
    source_snapshot_path = (source_root / "original.md").resolve()
    write_text_atomic(source_snapshot_path, source_text)

    source_hash = sha256_text(source_text)
    plan_path = state_root / "plan.json"
    pages = _load_seed_plan(
        plan_path,
        source_sha256=source_hash,
        source_line_count=len(lines),
        prompt_version=SEED_PLAN_VERSION,
    ) if config.resume else None
    resumed_seed_plan = pages is not None
    if pages is not None:
        _emit(on_progress, "seed", "resumed", pages=len(pages), source_lines=len(lines))
    else:
        _emit(on_progress, "seed", "start", source_lines=len(lines))
        if wiki_root.exists():
            shutil.rmtree(wiki_root)
        wiki_root.mkdir(parents=True, exist_ok=True)
        observations = await observe_document(
            source_text,
            model=model,
            config=config,
            document=document_id(normalized),
            checkpoint_dir=work_root / "observations" / "checkpoints",
            live_output_dir=work_root / "observations" / "live",
            on_progress=on_progress,
            stop_check=stop_check,
        )
        seed_plan = await build_seed_plan(
            observations,
            lines=lines,
            model=model,
            config=config,
            checkpoint_dir=work_root / "planning",
            stop_check=stop_check,
            on_progress=on_progress,
        )
        pages = _plan_pages(seed_plan)
        _verify_ranges(pages, len(lines))
    units = extract_image_units(lines)
    seed_root = (work_root / "seeds").resolve()
    if not resumed_seed_plan:
        if seed_root.exists():
            shutil.rmtree(seed_root)
        page_state_root = state_root / "pages"
        if page_state_root.exists():
            shutil.rmtree(page_state_root)
        for obsolete in (
            work_root / "chunks",
            work_root / "map",
            source_root / "slices",
        ):
            if obsolete.exists():
                shutil.rmtree(obsolete)
        for old_attempt in work_root.glob("rewrite-*-attempt-*"):
            if old_attempt.is_dir():
                shutil.rmtree(old_attempt)
    _write_reference_seeds(pages, lines, units, seed_root)
    plan_json = {
        "source": str(source_path),
        "source_snapshot": str(source_snapshot_path),
        "source_sha256": sha256_text(source_text),
        "source_line_count": len(lines),
        "prompt_version": SEED_PLAN_VERSION,
        "pages": [
            {
                "number": page.number,
                "title": page.title,
                "chapter": page.chapter,
                "summary": page.summary,
                "filename": page.filename,
                "owner_ranges": _ranges_json(page.owner_ranges),
                "reference_ranges": _ranges_json(page.reference_ranges),
                "provenance": _page_provenance(
                    page,
                    pages,
                    source_path=source_path,
                    source_snapshot_path=source_snapshot_path,
                    source_sha256=source_hash,
                ),
            }
            for page in pages
        ],
    }
    write_json_atomic(plan_path, plan_json)
    write_text_atomic(wiki_root / "index.md", _index_text(source_path.stem, pages))
    _emit(on_progress, "seed", "done", pages=len(pages), images=len(units))

    agent = _prepare_agent(config, model, work_root, slug)
    results = await _rewrite_all(
        pages,
        lines=lines,
        units=units,
        agent=agent,
        judge_model=model,
        config=config,
        work_root=work_root,
        seed_root=seed_root,
        wiki_root=wiki_root,
        state_root=state_root,
        source_path=source_path,
        source_snapshot_path=source_snapshot_path,
        source_sha256=source_hash,
        source_line_count=len(lines),
        stop_check=stop_check,
        on_progress=on_progress,
    )
    fallback = [item for item in results if item.fallback]
    if not fallback:
        results = await _link_all(
            results,
            pages=pages,
            units=units,
            agent=agent,
            config=config,
            work_root=work_root,
            wiki_root=wiki_root,
            state_root=state_root,
            source_path=source_path,
            source_snapshot_path=source_snapshot_path,
            source_sha256=source_hash,
            stop_check=stop_check,
            on_progress=on_progress,
        )
    for item, page in zip(plan_json["pages"], pages):
        item["reference_ranges"] = _ranges_json(page.reference_ranges)
        item["provenance"] = _page_provenance(
            page,
            pages,
            source_path=source_path,
            source_snapshot_path=source_snapshot_path,
            source_sha256=source_hash,
        )
    write_json_atomic(plan_path, plan_json)
    write_text_atomic(wiki_root / "index.md", _index_text(source_path.stem, pages))
    manifest = _manifest(
        source_path=source_path,
        source_snapshot_path=source_snapshot_path,
        source_text=source_text,
        pages=pages,
        results=results,
    )
    write_json_atomic(state_root / "manifest.json", manifest)
    (wiki_root / "manifest.json").unlink(missing_ok=True)
    if fallback:
        review = [
            "# Human review required",
            "",
            "These pages were not published because rewriting exhausted all retries:",
            "",
        ]
        review.extend(
            f"- `{item.page.filename}`: {item.error or 'rewrite failed'}"
            for item in fallback
        )
        write_text_atomic(wiki_root / "_review.md", "\n".join(review) + "\n")
    else:
        (wiki_root / "_review.md").unlink(missing_ok=True)

    write_json_atomic(
        state_root / "run.json",
        {
            "source_sha256": sha256_text(source_text),
            "source_line_count": len(lines),
            "pages": len(pages),
            "rewritten": len(pages) - len(fallback),
            "fallback_pages": len(fallback),
            "linked": sum(item.linked for item in results),
            "link_failures": sum(
                bool(item.link_error) for item in results if not item.fallback
            ),
            "published": True,
        },
    )
    _emit(on_progress, "publish", "done", output=str(wiki_root), fallback_pages=len(fallback))
    return run_root
