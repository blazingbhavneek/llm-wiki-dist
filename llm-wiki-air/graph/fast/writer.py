"""Fast-only document-wide writer pass -> judge pass scheduler."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from common.policy import policy_of
from graph.wiki.model import judge_model, writer_model
from graph.wiki.page import (
    PLACEHOLDER_RE,
    check_section,
    is_small_document,
    normalize_draft,
    split_sections,
)
from graph.wiki.storage import read_json, slice_text, write_text_atomic
from graph.wiki.wire import PageJudgeResult, ReferenceFact
from .passes import bounded_map


@dataclass
class SectionSession:
    page: Any
    start: int
    end: int
    index: int
    count: int
    facts: list[ReferenceFact]
    units: list[Any]
    placeholders: list[str]
    numbered: str
    source_text: str
    facts_text: str
    task_dir: Path
    context: str
    candidates: list[Any] = field(default_factory=list)
    feedback: list[str] = field(default_factory=list)
    attempt: int = 0
    pending_draft: str | None = None
    result: Any | None = None


def _prompt(session: SectionSession, *, lines: Sequence[str], config: Any, policy: Any) -> Any:
    from graph.wiki import pipeline as wiki

    return wiki.section_write_prompt(
        page_title=session.page.title,
        page_summary=session.page.summary,
        index=session.index,
        count=session.count,
        source_start=session.start,
        source_end=session.end,
        numbered_section=session.numbered,
        facts_text=session.facts_text,
        image_context=wiki._image_context(session.units, lines),
        output_language=config.output_language,
        feedback=session.feedback,
        context=session.context,
        code_identifiers=(
            []
            if config.source_kind in {"csv", "xlsx"}
            else sorted(policy.code_tokens(session.source_text))
        ),
        policy_rules=policy.wiki_prompt_rules("writer"),
    )


def _finish(
    session: SectionSession,
    *,
    lines: Sequence[str],
    policy: Any,
    on_progress: Any,
) -> Any:
    from graph.wiki import pipeline as wiki

    if session.result is not None:
        return session.result
    clean = [item for item in session.candidates if not item.errors]
    if clean:
        best = max(
            clean,
            key=lambda item: (
                not item.missing,
                not item.defects,
                item.score if item.score is not None else 0,
                item.attempt,
            ),
        )
        if policy.strict_judge and (best.missing or best.defects):
            wiki._emit(
                on_progress,
                "write",
                "section_verbatim",
                page=session.page.title,
                section=session.index,
                error="judge reported missing information or defects",
            )
            session.result = wiki._SectionResult(
                markdown=wiki._verbatim_section(
                    lines, session.start, session.end, session.units
                ),
                attempts=len(session.candidates),
                errors=best.missing + best.defects,
                verbatim=True,
            )
        else:
            session.result = wiki._SectionResult(
                markdown=best.markdown,
                attempts=len(session.candidates),
                score=best.score,
                missing=best.missing,
                defects=best.defects,
            )
        return session.result

    errors = session.candidates[-1].errors if session.candidates else list(session.feedback)
    wiki._emit(
        on_progress,
        "write",
        "section_verbatim",
        page=session.page.title,
        section=session.index,
        error="; ".join(errors)[:300],
    )
    session.result = wiki._SectionResult(
        markdown=wiki._verbatim_section(
            lines, session.start, session.end, session.units
        ),
        attempts=len(session.candidates),
        errors=errors,
        verbatim=True,
    )
    return session.result


async def _write_candidate(
    session: SectionSession,
    *,
    lines: Sequence[str],
    model: Any,
    config: Any,
    policy: Any,
    stop_check: Any,
    on_progress: Any,
) -> str:
    from graph.wiki import pipeline as wiki

    if stop_check and stop_check():
        raise asyncio.CancelledError("page writing cancelled")
    session.attempt += 1
    attempt = session.attempt
    stem = f"section-{session.index:02d}"
    prompt = _prompt(session, lines=lines, config=config, policy=policy)
    rendered_prompt = prompt.render()
    cached_prompt = session.task_dir / f"{stem}-attempt-{attempt:02d}-prompt.md"
    cached_draft = session.task_dir / f"{stem}-attempt-{attempt:02d}.md"
    cached_judge = session.task_dir / f"{stem}-judge-{attempt:02d}.json"
    if (
        attempt == 1
        and cached_prompt.exists()
        and cached_draft.exists()
        and cached_prompt.read_text(encoding="utf-8") == rendered_prompt
    ):
        draft = cached_draft.read_text(encoding="utf-8")
        errors = check_section(
            draft,
            lines=lines,
            source_text=session.source_text,
            block_ranges=(),
            placeholders=session.placeholders,
            facts=session.facts,
            check_identifiers=config.source_kind not in {"csv", "xlsx"},
            tokens=policy.code_tokens,
        )
        judgment = read_json(cached_judge, default={})
        if (
            not errors
            and judgment
            and not judgment.get("missing_important_information")
            and not judgment.get("defects")
        ):
            wiki._emit(
                on_progress,
                "write",
                "section_resumed",
                page=session.page.title,
                section=session.index,
            )
            session.result = wiki._SectionResult(
                markdown=draft,
                attempts=0,
                score=int(judgment.get("coverage_score", 0)),
            )
            return "done"

    write_text_atomic(cached_prompt, rendered_prompt)
    try:
        raw = await writer_model(model).text(
            prompt.messages(), max_output_tokens=config.write_max_output_tokens
        )
    except Exception as exc:
        session.feedback = [
            f"前回の呼び出しが失敗した: {type(exc).__name__}: {exc}"[:500]
        ]
        write_text_atomic(
            session.task_dir / f"{stem}-attempt-{attempt:02d}-error.txt",
            session.feedback[0] + "\n",
        )
        if attempt >= max(1, config.write_attempts):
            _finish(session, lines=lines, policy=policy, on_progress=on_progress)
            return "done"
        return "retry"

    draft = normalize_draft(raw)
    draft = re.sub(r"`+(\[\[NEO-IMAGE:[A-Za-z0-9_-]+\]\])`+", r"\1", draft)
    draft = PLACEHOLDER_RE.sub(
        lambda match: match.group(0)
        if match.group(0) in session.placeholders
        else "",
        draft,
    )
    draft = wiki._preserve_image_placeholders(draft, session.units, lines)
    write_text_atomic(cached_draft, draft)
    errors = check_section(
        draft,
        lines=lines,
        source_text=session.source_text,
        block_ranges=(),
        placeholders=session.placeholders,
        facts=session.facts,
        check_identifiers=config.source_kind not in {"csv", "xlsx"},
        tokens=policy.code_tokens,
    )
    if errors:
        session.candidates.append(
            wiki._SectionCandidate(draft, attempt, errors=errors)
        )
        session.feedback = errors
        wiki._emit(
            on_progress,
            "write",
            "section_retry",
            page=session.page.title,
            section=session.index,
            attempt=attempt,
            error="; ".join(errors)[:300],
        )
        if attempt >= max(1, config.write_attempts):
            _finish(session, lines=lines, policy=policy, on_progress=on_progress)
            return "done"
        return "retry"

    session.pending_draft = draft
    return "candidate"


async def _judge_candidate(
    session: SectionSession,
    *,
    lines: Sequence[str],
    model: Any,
    config: Any,
    policy: Any,
    stop_check: Any,
    on_progress: Any,
) -> str:
    from graph.wiki import pipeline as wiki

    draft = session.pending_draft
    if draft is None:
        raise wiki.PipelineError("writer pass completed without a section candidate")
    session.pending_draft = None
    original = session.numbered
    if session.facts:
        original += (
            "\n\n--- 他ページから追加した事実（必ず反映） ---\n"
            + session.facts_text
        )
    judgment, _attempts, error = await wiki._structured_with_artifacts(
        schema=PageJudgeResult,
        prompt=wiki.page_judge_prompt(
            page_title=f"{session.page.title}（節 {session.index}/{session.count}）",
            owner_ranges=f"{session.start}-{session.end}",
            numbered_original=original,
            candidate=draft,
            output_language=config.output_language,
            policy_rules=policy.wiki_prompt_rules("judge"),
        ),
        model=judge_model(model),
        output_dir=session.task_dir,
        stem=f"section-{session.index:02d}-judge-{session.attempt:02d}",
        attempts=config.judge_attempts,
        max_output_tokens=config.judge_max_output_tokens,
        stop_check=stop_check,
        retry_temperature=getattr(config, "retry_temperature", 0.7),
    )
    if judgment is None:
        session.candidates.append(
            wiki._SectionCandidate(
                draft,
                session.attempt,
                errors=[error or "judge unavailable"] if policy.strict_judge else [],
            )
        )
        wiki._emit(
            on_progress,
            "write",
            "judge_unavailable",
            page=session.page.title,
            section=session.index,
            attempt=session.attempt,
            error=error,
        )
        _finish(session, lines=lines, policy=policy, on_progress=on_progress)
        return "done"

    missing = wiki._judge_feedback(judgment)
    defects = [item.strip() for item in judgment.defects if item and item.strip()]
    session.candidates.append(
        wiki._SectionCandidate(
            draft,
            session.attempt,
            score=judgment.coverage_score,
            missing=missing,
            defects=defects,
        )
    )
    wiki._emit(
        on_progress,
        "write",
        "section_judged",
        page=session.page.title,
        section=session.index,
        attempt=session.attempt,
        score=judgment.coverage_score,
        missing=len(missing),
        defects=len(defects),
    )
    if not missing and not defects:
        _finish(session, lines=lines, policy=policy, on_progress=on_progress)
        return "done"
    session.feedback = missing + [f"品質: {item}" for item in defects]
    if session.attempt >= max(1, config.write_attempts):
        _finish(session, lines=lines, policy=policy, on_progress=on_progress)
        return "done"
    return "retry"


async def rewrite_pending_pages(
    *,
    pending: Sequence[Any],
    results: list[Any],
    pages: Sequence[Any],
    lines: list[str],
    units: Sequence[Any],
    model: Any,
    config: Any,
    work_root: Path,
    wiki_root: Path,
    state_root: Path,
    source_path: Path,
    source_snapshot_path: Path,
    source_sha256: str,
    source_line_count: int,
    rewrite_version: str,
    stop_check: Any,
    on_progress: Any,
    parents: dict[str, str] | None,
) -> None:
    """Write every pending section, then judge every candidate, until complete."""

    del source_line_count
    from graph.formats.context import context_block
    from graph.wiki import pipeline as wiki

    policy = policy_of(config)
    sessions_by_page: dict[int, list[SectionSession]] = {}
    queue: list[SectionSession] = []
    small = config.skip_excel_and_small and is_small_document("\n".join(lines))
    for page in pending:
        if len(page.owner_ranges) != 1:
            raise wiki.PipelineError(
                f"page {page.number} must own one contiguous range"
            )
        page_start, page_end = page.owner_ranges[0]
        task_dir = work_root / f"page-{page.number:03d}"
        task_dir.mkdir(parents=True, exist_ok=True)
        ranges = (
            [(page_start, page_end)]
            if small
            else split_sections(
                lines,
                page_start,
                page_end,
                target=config.section_target_lines,
                min_lines=config.section_min_lines,
            )
        )
        wiki._emit(
            on_progress,
            "writer",
            "page_start",
            page=page.title,
            current=0,
            total=len(pages),
        )
        page_sessions: list[SectionSession] = []
        for index, (start, end) in enumerate(ranges, start=1):
            section_units = [
                unit
                for unit in units
                if start <= unit.source_start and unit.source_end <= end
            ]
            session = SectionSession(
                page=page,
                start=start,
                end=end,
                index=index,
                count=len(ranges),
                facts=[],
                units=section_units,
                placeholders=[unit.placeholder for unit in section_units],
                numbered=wiki._numbered_source(lines, [(start, end)], section_units),
                source_text=wiki._prompt_safe(
                    slice_text(lines, start, end), section_units
                ),
                facts_text="なし",
                task_dir=task_dir,
                context=context_block(page, pages, parents or {}),
            )
            if model is None:
                session.result = wiki._SectionResult(
                    markdown=wiki._verbatim_section(lines, start, end, section_units),
                    attempts=0,
                    errors=["model unavailable"],
                    verbatim=True,
                )
            else:
                queue.append(session)
            page_sessions.append(session)
        sessions_by_page[page.number] = page_sessions

    committed = {item.page.number for item in results}

    def commit_ready() -> None:
        for page in pending:
            if page.number in committed:
                continue
            sessions = sessions_by_page[page.number]
            if any(session.result is None for session in sessions):
                continue
            section_results = [session.result for session in sessions]
            body = "\n\n".join(
                policy.section(item.markdown).rstrip()
                for item in section_results
                if item is not None
            )
            markdown = f"# {page.title}\n\n{body}\n"
            masked, unmask = policy.mask_for_linking(markdown)
            markdown = unmask(
                wiki.link_titles(
                    masked,
                    [
                        (other.title, other.filename)
                        for other in pages
                        if other.number != page.number
                    ],
                )
            )
            markdown += wiki._nav_footer(page, pages)
            restored, unresolved = wiki.restore_images(
                markdown, wiki._page_units(page, units)
            )
            if unresolved:
                raise wiki.PipelineError(
                    f"page {page.number} has unresolved image placeholders: {unresolved}"
                )
            page.reference_ranges = []
            completed = [item for item in section_results if item is not None]
            result = wiki.RewriteResult(
                page=page,
                markdown=wiki.strip_reader_references(restored),
                attempts=sum(item.attempts for item in completed),
                judge_score=min(
                    (item.score for item in completed if item.score is not None),
                    default=None,
                ),
                missing_important_information=[
                    f"原文 {session.start}-{session.end}行: {item}"
                    for session in sessions
                    for item in session.result.missing
                ],
                defects=[
                    f"原文 {session.start}-{session.end}行: {item}"
                    for session in sessions
                    for item in session.result.defects
                ],
                verbatim_sections=[
                    f"原文 {session.start}-{session.end}行: "
                    + "; ".join(session.result.errors)
                    for session in sessions
                    if session.result.verbatim
                ],
            )
            results.append(result)
            committed.add(page.number)
            write_text_atomic(wiki_root / page.filename, result.markdown)
            wiki._write_page_state(
                wiki._page_state_path(state_root, page),
                result,
                rewrite_version=rewrite_version,
                pages=pages,
                source_path=source_path,
                source_snapshot_path=source_snapshot_path,
                source_sha256=source_sha256,
            )
            wiki._emit(
                on_progress,
                "writer",
                "page_done",
                page=page.title,
                total=len(pages),
            )
            wiki._emit(
                on_progress,
                "rewrite",
                "page_done",
                current=len(results),
                total=len(pages),
                page=page.title,
                attempts=result.attempts,
                score=result.judge_score,
                verbatim_sections=len(result.verbatim_sections),
            )

    commit_ready()
    concurrency = max(1, int(config.rewrite_concurrency))
    pass_number = 1
    while queue:
        current = queue
        retry: list[SectionSession] = []
        candidates: list[SectionSession] = []
        wiki._emit(
            on_progress,
            "writer",
            "writer_pass_started",
            pass_number=pass_number,
            sections=len(current),
            concurrency=concurrency,
        )
        async def write_one(session: SectionSession) -> str:
            return await _write_candidate(
                session,
                lines=lines,
                model=model,
                config=config,
                policy=policy,
                stop_check=stop_check,
                on_progress=on_progress,
            )

        actions = await bounded_map(current, concurrency, write_one)
        for session, action in zip(current, actions):
            if action == "candidate":
                candidates.append(session)
            elif action == "retry":
                retry.append(session)
        wiki._emit(
            on_progress,
            "writer",
            "writer_pass_done",
            pass_number=pass_number,
            sections=len(current),
            candidates=len(candidates),
        )

        if candidates:
            wiki._emit(
                on_progress,
                "writer",
                "judge_pass_started",
                pass_number=pass_number,
                sections=len(candidates),
                concurrency=concurrency,
            )
            async def judge_one(session: SectionSession) -> str:
                return await _judge_candidate(
                    session,
                    lines=lines,
                    model=model,
                    config=config,
                    policy=policy,
                    stop_check=stop_check,
                    on_progress=on_progress,
                )

            actions = await bounded_map(candidates, concurrency, judge_one)
            retry.extend(
                session
                for session, action in zip(candidates, actions)
                if action == "retry"
            )
            wiki._emit(
                on_progress,
                "writer",
                "judge_pass_done",
                pass_number=pass_number,
                sections=len(candidates),
                retry=len(retry),
            )
        commit_ready()
        queue = retry
        pass_number += 1

    commit_ready()
    unfinished = [page.filename for page in pending if page.number not in committed]
    if unfinished:
        raise RuntimeError(
            "fast writer/judge passes left unfinished pages: " + ", ".join(unfinished)
        )


__all__ = ["rewrite_pending_pages"]
