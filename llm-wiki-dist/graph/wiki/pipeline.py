"""The wiki pipeline: deterministic partition, section-wise lossless rewriting.

1. Overlapping 250-line windows are described without assigning ownership.
2. Regional and document planners compile one exact sequential seed partition.
3. Python picks references (adjacent + shared vocabulary); one structured
   compare call per reference collects facts to import.
4. Python cuts each page into sections; the model rewrites one section at a
   time as plain Markdown; Python checks fences, tables, identifiers, images
   and imported facts survived before a judge looks for semantic omissions.
5. Python writes the title, one model-written intro, links and navigation.

There is no CLI agent and no tool-calling agent. Everything the model needs
is inside the prompt; every decision about what survives is made in Python.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import REWRITE_PROMPT_VERSION, SEED_PLAN_VERSION, WikiConfig
from .document_map import build_seed_plan
from .ids import document_id, slugify
from .images import ImageUnit, extract_image_units, restore_images
from .model import ChatModelPort, ModelPort
from .page import (
    PLACEHOLDER_RE,
    REFERENCE_MARKER_RE,
    assign_facts,
    check_section,
    code_tokens,
    demote_h1,
    link_titles,
    normalize_draft,
    split_sections,
    verbatim_blocks,
    word_tokens,
)
from .prompts import (
    intro_prompt,
    page_judge_prompt,
    reference_research_prompt,
    section_write_prompt,
)
from .schemas import CompiledSeedPlan
from .storage import (
    clean_workdir,
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
    judge_score: int | None = None
    missing_important_information: list[str] = field(default_factory=list)
    verbatim_sections: list[str] = field(default_factory=list)


@dataclass
class _SectionCandidate:
    markdown: str
    attempt: int
    score: int | None = None
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class _SectionResult:
    markdown: str
    attempts: int
    score: int | None = None
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    verbatim: bool = False


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


def _reference_ranges_from_markdown(
    markdown: str,
    owner_ranges: Sequence[tuple[int, int]],
    source_line_count: int,
) -> list[tuple[int, int]]:
    """Collect optional cross-page provenance markers without rejecting prose."""

    found: list[tuple[int, int]] = []
    for match in REFERENCE_MARKER_RE.finditer(markdown):
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
    facts: Sequence[ReferenceFact], page: SeedPage, target: SeedPage
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
        if not any(s <= fact.target_line <= e for s, e in target.owner_ranges):
            fact.target_line = 0
        valid.append(fact)
    return valid


def _render_reference_research(
    target: SeedPage,
    evidence: Sequence[_ReferenceEvidence],
    *,
    lines: Sequence[str],
    units: Sequence[ImageUnit],
    seed_root: Path,
) -> str:
    """Create the compact, auditable evidence pack consumed by plan and writer."""

    rendered = [
        "# 参照調査結果",
        "",
        f"対象: {target.number:03d} {target.title}",
        "",
    ]
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


def _select_references(
    page: SeedPage,
    pages: Sequence[SeedPage],
    tokens: dict[int, set[str]],
    *,
    limit: int,
) -> list[SeedPage]:
    """Previous and next page always, plus the pages sharing the most vocabulary."""

    # ponytail: lexical ranking; add an LLM shortlist only if smoke runs miss related pages.
    others = [item for item in pages if item.number != page.number]
    if not others:
        return []
    counts: dict[str, int] = {}
    for vocabulary in tokens.values():
        for token in vocabulary:
            counts[token] = counts.get(token, 0) + 1
    common = {token for token, count in counts.items() if count > len(pages) / 2}
    mine = tokens.get(page.number, set()) - common
    adjacent = [item for item in others if abs(item.number - page.number) == 1]
    scored = sorted(
        (
            (len(mine & (tokens.get(item.number, set()) - common)), -item.number, item)
            for item in others
            if item not in adjacent
        ),
        key=lambda entry: (entry[0], entry[1]),
        reverse=True,
    )
    picks = [item for score, _, item in scored[: max(0, limit)] if score > 0]
    return sorted(adjacent + picks, key=lambda item: item.number)


async def _research_references(
    page: SeedPage,
    *,
    pages: Sequence[SeedPage],
    lines: Sequence[str],
    units: Sequence[ImageUnit],
    tokens: dict[int, set[str]],
    model: ModelPort,
    config: WikiConfig,
    work_root: Path,
    seed_root: Path,
    stop_check: StopCheck,
    on_progress: Progress,
) -> tuple[list[_ReferenceEvidence], str]:
    """Python selects references; one structured compare call per reference."""

    selected = _select_references(page, pages, tokens, limit=config.reference_candidates)
    if not selected:
        return [], "# 参照調査結果\n\n他のWikiページはない。\n"

    research_dir = clean_workdir(work_root / f"research-{page.number:03d}")
    target_source = _numbered_source(
        lines, page.owner_ranges, _page_units(page, units)
    )
    _emit(
        on_progress,
        "research",
        "selected",
        page=page.title,
        candidates=len(selected),
        references=[item.number for item in selected],
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
        facts = (
            _valid_reference_facts(result.useful_facts, candidate, page)
            if result
            else []
        )
        reason = (
            result.no_useful_information_reason.strip()
            if result is not None
            else f"調査呼び出し失敗: {error}"
        )
        evidence.append(
            _ReferenceEvidence(
                page=candidate, facts=facts, no_useful_information_reason=reason
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
        page, evidence, lines=lines, units=units, seed_root=seed_root
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
            "attempts": result.attempts,
            "judge_score": result.judge_score,
            "missing_important_information": result.missing_important_information,
            "verbatim_sections": result.verbatim_sections,
        },
    )


def _resume_rewritten_page(
    output_path: Path,
    state_path: Path,
    page: SeedPage,
    *,
    rewrite_version: str,
    source_line_count: int,
) -> RewriteResult | None:
    """Reuse a page whose sidecar state still matches the file on disk."""

    if not output_path.exists() or not state_path.exists():
        return None
    try:
        markdown = output_path.read_text(encoding="utf-8")
        state = read_json(state_path)
        if (
            state.get("rewrite_version") != rewrite_version
            or state.get("filename") != page.filename
            or state.get("content_sha256") != sha256_text(markdown)
        ):
            return None
        page.reference_ranges = _valid_reference_ranges(
            state.get("reference_ranges", []), source_line_count
        )
        return RewriteResult(
            page=page,
            markdown=markdown,
            attempts=int(state.get("attempts", 0)),
            judge_score=state.get("judge_score"),
            missing_important_information=list(state.get("missing_important_information", [])),
            verbatim_sections=list(state.get("verbatim_sections", [])),
        )
    except (OSError, TypeError, ValueError):
        return None


def _facts_text(
    facts: Sequence[ReferenceFact],
    lines: Sequence[str],
    units: Sequence[ImageUnit],
) -> str:
    if not facts:
        return "なし"
    rendered: list[str] = []
    for number, fact in enumerate(facts, start=1):
        fact_units = [
            unit
            for unit in units
            if fact.source_start <= unit.source_start
            and unit.source_end <= fact.source_end
        ]
        excerpt = _numbered_source(
            lines, [(fact.source_start, fact.source_end)], fact_units
        ).rstrip()
        rendered.append(
            f"## 追加事実 {number}\n"
            f"- 事実: {fact.description.strip()}\n"
            f"- 理由: {fact.reason.strip() or '単独で理解するために必要'}\n"
            f"- 出典: 原文 {fact.source_start}-{fact.source_end}行\n"
            "- 根拠抜粋:\n```text\n"
            f"{excerpt}\n```"
        )
    return "\n\n".join(rendered)


def _verbatim_section(
    lines: Sequence[str], start: int, end: int, units: Sequence[ImageUnit]
) -> str:
    """Lossless by construction: the exact source lines, images as placeholders."""

    return demote_h1(_prompt_safe(slice_text(list(lines), start, end), units)).rstrip() + "\n"


def _nav_footer(page: SeedPage, pages: Sequence[SeedPage]) -> str:
    previous = next((item for item in pages if item.number == page.number - 1), None)
    following = next((item for item in pages if item.number == page.number + 1), None)
    parts = []
    if previous:
        parts.append(f"前のページ: [{previous.title}]({previous.filename})")
    if following:
        parts.append(f"次のページ: [{following.title}]({following.filename})")
    return ("\n---\n\n" + " ｜ ".join(parts) + "\n") if parts else ""


async def _write_section(
    page: SeedPage,
    start: int,
    end: int,
    *,
    index: int,
    count: int,
    facts: Sequence[ReferenceFact],
    lines: Sequence[str],
    units: Sequence[ImageUnit],
    model: ModelPort,
    config: WikiConfig,
    task_dir: Path,
    stop_check: StopCheck,
    on_progress: Progress,
) -> _SectionResult:
    """Write one section until Python's lossless checks and the judge are satisfied."""

    # ponytail: markers plus the section judge cover enrichment; add a dedicated judge if not.
    section_units = [
        unit for unit in units if start <= unit.source_start and unit.source_end <= end
    ]
    placeholders = [unit.placeholder for unit in section_units]
    numbered = _numbered_source(lines, [(start, end)], section_units)
    source_text = _prompt_safe(slice_text(list(lines), start, end), section_units)
    blocks = verbatim_blocks(lines, start, end)
    facts_text = _facts_text(facts, lines, units)
    stem = f"section-{index:02d}"
    candidates: list[_SectionCandidate] = []
    feedback: list[str] = []

    for attempt in range(1, max(1, config.write_attempts) + 1):
        if stop_check and stop_check():
            raise asyncio.CancelledError("page writing cancelled")
        prompt = section_write_prompt(
            page_title=page.title,
            page_summary=page.summary,
            index=index,
            count=count,
            source_start=start,
            source_end=end,
            numbered_section=numbered,
            facts_text=facts_text,
            image_context=_image_context(section_units, lines),
            output_language=config.output_language,
            feedback=feedback,
        )
        write_text_atomic(
            task_dir / f"{stem}-attempt-{attempt:02d}-prompt.md", prompt.render()
        )
        try:
            raw = await model.text(
                prompt.messages(), max_output_tokens=config.write_max_output_tokens
            )
        except Exception as exc:  # noqa: BLE001 - bounded retry with evidence
            feedback = [f"前回の呼び出しが失敗した: {type(exc).__name__}: {exc}"[:500]]
            write_text_atomic(
                task_dir / f"{stem}-attempt-{attempt:02d}-error.txt", feedback[0] + "\n"
            )
            continue
        draft = normalize_draft(raw)
        draft = PLACEHOLDER_RE.sub(
            lambda match: match.group(0) if match.group(0) in placeholders else "",
            draft,
        )
        draft = _preserve_image_placeholders(draft, section_units, lines)
        write_text_atomic(task_dir / f"{stem}-attempt-{attempt:02d}.md", draft)
        errors = check_section(
            draft,
            lines=lines,
            source_text=source_text,
            block_ranges=blocks,
            placeholders=placeholders,
            facts=facts,
        )
        if errors:
            candidates.append(_SectionCandidate(draft, attempt, errors=errors))
            feedback = errors
            _emit(
                on_progress, "write", "section_retry",
                page=page.title, section=index, attempt=attempt,
                error="; ".join(errors)[:300],
            )
            continue
        judge_original = numbered
        if facts:
            judge_original += "\n\n--- 他ページから追加した事実（必ず反映） ---\n" + facts_text
        judgment, _judge_attempts, judge_error = await _structured_with_artifacts(
            schema=PageJudgeResult,
            prompt=page_judge_prompt(
                page_title=f"{page.title}（節 {index}/{count}）",
                owner_ranges=f"{start}-{end}",
                numbered_original=judge_original,
                candidate=draft,
                output_language=config.output_language,
            ),
            model=model,
            output_dir=task_dir,
            stem=f"{stem}-judge-{attempt:02d}",
            attempts=config.judge_attempts,
            max_output_tokens=config.judge_max_output_tokens,
            stop_check=stop_check,
        )
        if judgment is None:
            candidates.append(_SectionCandidate(draft, attempt, errors=[]))
            _emit(on_progress, "write", "judge_unavailable",
                  page=page.title, section=index, attempt=attempt, error=judge_error)
            break
        missing = _judge_feedback(judgment)
        candidates.append(
            _SectionCandidate(draft, attempt, score=judgment.coverage_score, missing=missing)
        )
        _emit(
            on_progress, "write", "section_judged",
            page=page.title, section=index, attempt=attempt,
            score=judgment.coverage_score, missing=len(missing),
        )
        if not missing:
            break
        feedback = missing

    clean = [item for item in candidates if not item.errors]
    if clean:
        best = max(
            clean,
            key=lambda item: (
                not item.missing,
                item.score if item.score is not None else 0,
                item.attempt,
            ),
        )
        return _SectionResult(
            markdown=best.markdown,
            attempts=len(candidates),
            score=best.score,
            missing=best.missing,
        )
    errors = candidates[-1].errors if candidates else list(feedback)
    _emit(on_progress, "write", "section_verbatim",
          page=page.title, section=index, error="; ".join(errors)[:300])
    return _SectionResult(
        markdown=_verbatim_section(lines, start, end, section_units),
        attempts=len(candidates),
        errors=errors,
        verbatim=True,
    )


async def _write_intro(
    page: SeedPage,
    body: str,
    *,
    model: ModelPort,
    config: WikiConfig,
    task_dir: Path,
    stop_check: StopCheck,
) -> str:
    """One lead paragraph. Anything it cannot justify from the body is dropped."""

    if stop_check and stop_check():
        raise asyncio.CancelledError("page writing cancelled")
    prompt = intro_prompt(
        page_title=page.title,
        page_summary=page.summary,
        body=body,
        output_language=config.output_language,
    )
    write_text_atomic(task_dir / "intro-prompt.md", prompt.render())
    try:
        raw = await model.text(
            prompt.messages(), max_output_tokens=config.intro_max_output_tokens
        )
    except Exception as exc:  # noqa: BLE001 - the summary is an acceptable intro
        write_text_atomic(task_dir / "intro-error.txt", f"{type(exc).__name__}: {exc}\n")
        return page.summary
    text = PLACEHOLDER_RE.sub("", normalize_draft(raw))
    text = "\n".join(
        line for line in text.splitlines()
        if line.strip() and not line.startswith("#") and "](" not in line
    ).strip()
    if not text or code_tokens(text) - code_tokens(body):
        return page.summary
    write_text_atomic(task_dir / "intro.md", text + "\n")
    return text


async def _rewrite_page(
    page: SeedPage,
    *,
    pages: Sequence[SeedPage],
    lines: list[str],
    units: Sequence[ImageUnit],
    tokens: dict[int, set[str]],
    model: ModelPort,
    config: WikiConfig,
    work_root: Path,
    seed_root: Path,
    source_line_count: int,
    stop_check: StopCheck,
    on_progress: Progress,
) -> RewriteResult:
    if len(page.owner_ranges) != 1:
        raise PipelineError(f"page {page.number} must own one contiguous range")
    start, end = page.owner_ranges[0]
    page_units = _page_units(page, units)
    task_dir = clean_workdir(work_root / f"page-{page.number:03d}")

    evidence, _research = await _research_references(
        page, pages=pages, lines=lines, units=units, tokens=tokens, model=model,
        config=config, work_root=work_root, seed_root=seed_root,
        stop_check=stop_check, on_progress=on_progress,
    )
    facts = [fact for item in evidence for fact in item.facts]
    # ponytail: sections stay in source order; add reordering only if a smoke run needs it.
    sections = split_sections(
        lines, start, end,
        target=config.section_target_lines, min_lines=config.section_min_lines,
    )
    buckets = assign_facts(facts, sections)

    drafts: list[str] = []
    scores: list[int] = []
    missing: list[str] = []
    verbatim: list[str] = []
    attempts = 0
    for index, ((s, e), section_facts) in enumerate(zip(sections, buckets), start=1):
        result = await _write_section(
            page, s, e, index=index, count=len(sections), facts=section_facts,
            lines=lines, units=units, model=model, config=config, task_dir=task_dir,
            stop_check=stop_check, on_progress=on_progress,
        )
        drafts.append(result.markdown.rstrip())
        attempts += result.attempts
        if result.score is not None:
            scores.append(result.score)
        missing.extend(f"原文 {s}-{e}行: {item}" for item in result.missing)
        if result.verbatim:
            verbatim.append(f"原文 {s}-{e}行: " + "; ".join(result.errors))

    body = "\n\n".join(drafts)
    intro = await _write_intro(
        page, body, model=model, config=config, task_dir=task_dir, stop_check=stop_check
    )
    markdown = f"# {page.title}\n\n{intro.rstrip()}\n\n{body}\n"
    markdown = link_titles(
        markdown,
        [(item.title, item.filename) for item in pages if item.number != page.number],
    )
    markdown += _nav_footer(page, pages)
    restored, unresolved = restore_images(markdown, page_units)
    if unresolved:
        raise PipelineError(f"page {page.number} has unresolved image placeholders: {unresolved}")
    page.reference_ranges = _reference_ranges_from_markdown(
        restored, page.owner_ranges, source_line_count
    )
    return RewriteResult(
        page=page,
        markdown=restored,
        attempts=attempts,
        judge_score=min(scores) if scores else None,
        missing_important_information=missing,
        verbatim_sections=verbatim,
    )


async def _rewrite_all(
    pages: Sequence[SeedPage],
    *,
    lines: list[str],
    units: Sequence[ImageUnit],
    model: ModelPort,
    config: WikiConfig,
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
    tokens = {
        item.number: word_tokens(
            _prompt_safe(_slice_ranges(lines, item.owner_ranges), _page_units(item, units))
        )
        for item in pages
    }

    results: list[RewriteResult] = []
    pending: list[SeedPage] = []
    for page in pages:
        output_path = wiki_root / page.filename
        state_path = _page_state_path(state_root, page)
        resumed = (
            _resume_rewritten_page(
                output_path, state_path, page,
                rewrite_version=REWRITE_PROMPT_VERSION,
                source_line_count=source_line_count,
            )
            if config.resume
            else None
        )
        if resumed is not None:
            results.append(resumed)
            _emit(on_progress, "rewrite", "page_resumed",
                  current=len(results), total=len(pages), page=page.title)
        else:
            output_path.unlink(missing_ok=True)
            state_path.unlink(missing_ok=True)
            for old in (work_root / f"page-{page.number:03d}", work_root / f"research-{page.number:03d}"):
                if old.is_dir():
                    shutil.rmtree(old)
            pending.append(page)

    async def one(page: SeedPage) -> RewriteResult:
        async with semaphore:
            return await _rewrite_page(
                page, pages=pages, lines=lines, units=units, tokens=tokens,
                model=model, config=config, work_root=work_root, seed_root=seed_root,
                source_line_count=source_line_count,
                stop_check=stop_check, on_progress=on_progress,
            )

    for completed, task in enumerate(
        asyncio.as_completed([one(page) for page in pending]), start=len(results) + 1
    ):
        result = await task
        results.append(result)
        write_text_atomic(wiki_root / result.page.filename, result.markdown)
        _write_page_state(
            _page_state_path(state_root, result.page), result,
            rewrite_version=REWRITE_PROMPT_VERSION, pages=pages,
            source_path=source_path, source_snapshot_path=source_snapshot_path,
            source_sha256=source_sha256,
        )
        _emit(
            on_progress, "rewrite", "page_done",
            current=completed, total=len(pages), page=result.page.title,
            attempts=result.attempts, score=result.judge_score,
            verbatim_sections=len(result.verbatim_sections),
        )
    return sorted(results, key=lambda item: item.page.number)


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
                "status": "rewritten",
                "attempts": by_number[page.number].attempts,
                "judge_score": by_number[page.number].judge_score,
                "missing_important_information": by_number[page.number].missing_important_information,
                "verbatim_sections": by_number[page.number].verbatim_sections,
            }
            for page in pages
        ],
    }


async def run_pipeline(
    source_path: Path | str,
    *,
    config: WikiConfig | None = None,
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
    config = config or WikiConfig()
    model = model or ChatModelPort(config)
    slug = slugify(config.document_slug or source_path.stem, fallback="document").casefold()
    run_root = (
        Path(config.run_dir).resolve()
        if config.run_dir
        else Path(config.output_root) / f"{slug}-{sha256_text(source_text)[:12]}"
    )
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
        for pattern in ("page-*", "research-*"):
            for old_attempt in work_root.glob(pattern):
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

    results = await _rewrite_all(
        pages,
        lines=lines,
        units=units,
        model=model,
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
    flagged = [item for item in results if item.verbatim_sections]
    if flagged:
        review = [
            "# Human review required",
            "",
            "These sections were published as the exact source text because every "
            "rewrite attempt lost content. Check them by hand:",
            "",
        ]
        for item in flagged:
            review.append(f"- `{item.page.filename}`")
            review.extend(f"  - {note}" for note in item.verbatim_sections)
        write_text_atomic(wiki_root / "_review.md", "\n".join(review) + "\n")
    else:
        (wiki_root / "_review.md").unlink(missing_ok=True)

    write_json_atomic(
        state_root / "run.json",
        {
            "source_sha256": sha256_text(source_text),
            "source_line_count": len(lines),
            "pages": len(pages),
            "rewritten": len(pages),
            "review_pages": len(flagged),
            "verbatim_sections": sum(len(item.verbatim_sections) for item in results),
            "published": True,
        },
    )
    _emit(on_progress, "publish", "done", output=str(wiki_root), review_pages=len(flagged))
    return run_root
