"""
Stage 2: extractive skeleton + selection-only LLM labeling.

The LLM never constructs a line number and never summarizes. It only:
  1. classifies recurring line *families* once (pattern-class labeling), and
  2. picks candidate IDs that start top-level sections (then recurses one
     level into big sections).

Both outputs translate into score boosts on the fused lattice; the DP
assembler makes every final decision, so a bad LLM answer can nudge a score
but can never produce a gap, overlap, or hallucinated line number.

The `llm` argument is any object with:
    complete_structured(system_prompt: str, user_content: str, model: type) -> model
(graph.gateway's chat client satisfies this; chunk.py's CLI shim does too).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .config import ChunkConfig
from .signals import NEG_INF

log = logging.getLogger("chunker")

# score boosts applied to the fused lattice
BOOST_FAMILY_MAJOR = 2.0
BOOST_FAMILY_MINOR = 1.0
PENALTY_FAMILY_NOT_BOUNDARY = -2.0
BOOST_TOP_LEVEL_PICK = 2.5
BOOST_RECURSIVE_PICK = 1.5


@dataclass
class Candidate:
    cid: str  # "C012"
    boundary: int  # cut after source line `boundary` (next section starts at boundary+1)
    line_text: str  # verbatim first line of the would-be section
    score: float
    family: str = ""  # regex family key, "" when unclassified
    boost: float = 0.0
    top_level: bool = False


@dataclass
class SkeletonResult:
    candidates: list[Candidate] = field(default_factory=list)
    llm_calls: int = 0
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------
# candidate extraction (pure python)
# ---------------------------------------------------------------------


def extract_candidates(
    lines: list[str], fused: list[float], config: ChunkConfig
) -> list[Candidate]:
    """Top-K lattice peaks, legal only, with a minimum gap between picks."""
    indexed = [
        (score, boundary_index)
        for boundary_index, score in enumerate(fused)
        if score != NEG_INF
    ]
    indexed.sort(reverse=True)

    chosen: list[int] = []
    taken: set[int] = set()
    for score, boundary_index in indexed:
        if len(chosen) >= config.skeleton_max_candidates:
            break
        if any(
            abs(boundary_index - other) < config.skeleton_min_gap for other in taken
        ):
            continue
        chosen.append(boundary_index)
        taken.add(boundary_index)

    chosen.sort()
    candidates = []
    for order, boundary_index in enumerate(chosen):
        boundary = boundary_index + 1  # boundary after line `boundary` (1-based)
        first_line = lines[boundary].strip() if boundary < len(lines) else ""
        candidates.append(
            Candidate(
                cid=f"C{order:03d}",
                boundary=boundary,
                line_text=first_line[:160],
                score=fused[boundary_index],
            )
        )
    return candidates


# ---------------------------------------------------------------------
# pattern families (pure python grouping; one LLM call to classify)
# ---------------------------------------------------------------------

_FAMILY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("atx_h1", re.compile(r"^#\s+\S")),
    ("atx_h2", re.compile(r"^##\s+\S")),
    ("atx_h3plus", re.compile(r"^#{3,6}\s+\S")),
    ("numbered", re.compile(r"^\d+(\.\d+)*[.)]?\s+\S")),
    ("cjk_section", re.compile(r"^第\s*[0-9０-９一二三四五六七八九十百]+\s*[章節条部編項]")),
    ("word_section", re.compile(r"^(Chapter|Section|Article|Part|Appendix)\s", re.IGNORECASE)),
    ("all_caps", re.compile(r"^[A-Z][A-Z0-9 \-:,.()&/]{2,59}$")),
]


def _family_of(text: str) -> str:
    for name, pattern in _FAMILY_PATTERNS:
        if pattern.match(text):
            return name
    return "other"


class FamilyVerdict(BaseModel):
    family: str = ""
    verdict: str = Field(
        default="section_start_minor",
        description="one of: section_start_major | section_start_minor | not_boundary",
    )


class FamilyClassification(BaseModel):
    verdicts: list[FamilyVerdict] = Field(default_factory=list)


_FAMILY_SYSTEM = """You label recurring line patterns found in one document.
For each pattern family you receive a few verbatim example lines. Decide what
the family is, for THIS document:
- "section_start_major": lines of this family begin top-level sections
- "section_start_minor": they begin subsections
- "not_boundary": they are not section starts (list items, captions, noise)
Return JSON with one verdict per family. Do not invent family names."""


class TopLevelSelection(BaseModel):
    top_level_ids: list[str] = Field(default_factory=list)


_TOPLEVEL_SYSTEM = """You receive a numbered list of candidate section-start
lines extracted from one document (ID, source line number, verbatim text).
Select the candidate IDs where a NEW TOP-LEVEL part of the document begins.
Return ONLY IDs from the list. Prefer fewer, clearly major boundaries."""

_RECURSE_SYSTEM = """You receive candidate section-start lines that all fall
inside ONE section of a document. Select the candidate IDs where a new
subsection clearly begins. Return ONLY IDs from the list."""


def _format_candidates(candidates: list[Candidate]) -> str:
    return "\n".join(
        f"{c.cid} | line {c.boundary + 1} | {c.line_text}" for c in candidates
    )


# ---------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------


def run_skeleton(
    lines: list[str],
    fused: list[float],
    config: ChunkConfig,
    llm=None,
) -> SkeletonResult:
    """Extract candidates and, when enabled and an LLM is provided, apply the
    two selection-only labeling passes. Mutates nothing; returns boosts on the
    Candidate objects for the assembler to fold into the lattice."""
    result = SkeletonResult(candidates=extract_candidates(lines, fused, config))
    candidates = result.candidates
    if not candidates:
        return result

    for candidate in candidates:
        candidate.family = _family_of(candidate.line_text)

    if not (config.use_skeleton_llm and llm is not None):
        result.notes.append("skeleton LLM disabled; using raw lattice peaks")
        return result

    # --- call 1: family classification (one call for the whole doc) ---
    families: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        families.setdefault(candidate.family, []).append(candidate)

    classifiable = {
        name: members
        for name, members in families.items()
        if name != "other" and len(members) >= 2
    }
    if classifiable:
        prompt_lines = []
        for name, members in classifiable.items():
            samples = "\n".join(f"    {m.line_text}" for m in members[:5])
            prompt_lines.append(f"family: {name} ({len(members)} lines)\n{samples}")
        try:
            parsed = llm.complete_structured(
                _FAMILY_SYSTEM, "\n\n".join(prompt_lines), FamilyClassification
            )
            result.llm_calls += 1
            verdict_by_family = {v.family: v.verdict for v in parsed.verdicts}
            for name, members in classifiable.items():
                verdict = verdict_by_family.get(name, "")
                boost = {
                    "section_start_major": BOOST_FAMILY_MAJOR,
                    "section_start_minor": BOOST_FAMILY_MINOR,
                    "not_boundary": PENALTY_FAMILY_NOT_BOUNDARY,
                }.get(verdict)
                if boost is None:
                    continue
                for member in members:
                    member.boost += boost
                result.notes.append(f"family {name}: {verdict}")
        except Exception as exc:
            result.notes.append(f"family classification failed: {exc}")
            log.info("chunker: family classification failed: %s", exc)

    # --- call 2: top-level selection ---
    shortlist = sorted(
        candidates, key=lambda c: c.score + c.boost, reverse=True
    )[:150]
    shortlist.sort(key=lambda c: c.boundary)
    try:
        parsed = llm.complete_structured(
            _TOPLEVEL_SYSTEM, _format_candidates(shortlist), TopLevelSelection
        )
        result.llm_calls += 1
        valid_ids = {c.cid for c in shortlist}
        picked = [cid for cid in parsed.top_level_ids if cid in valid_ids]
        by_id = {c.cid: c for c in candidates}
        for cid in picked:
            by_id[cid].boost += BOOST_TOP_LEVEL_PICK
            by_id[cid].top_level = True
        result.notes.append(f"top-level picks: {len(picked)}")
    except Exception as exc:
        result.notes.append(f"top-level selection failed: {exc}")
        log.info("chunker: top-level selection failed: %s", exc)

    # --- recursion: refine inside oversized top-level spans ---
    if config.skeleton_recursion_depth >= 2:
        top = [c for c in candidates if c.top_level]
        spans: list[tuple[int, int]] = []
        starts = [0] + [c.boundary for c in top] + [len(lines)]
        for a, b in zip(starts, starts[1:]):
            if b - a > 2 * config.size_max:
                spans.append((a, b))
        for span_start, span_end in spans[:10]:  # bound the extra calls
            inner = [
                c
                for c in candidates
                if span_start < c.boundary < span_end and not c.top_level
            ][:30]
            if len(inner) < 2:
                continue
            try:
                parsed = llm.complete_structured(
                    _RECURSE_SYSTEM, _format_candidates(inner), TopLevelSelection
                )
                result.llm_calls += 1
                valid_ids = {c.cid for c in inner}
                by_id = {c.cid: c for c in inner}
                for cid in parsed.top_level_ids:
                    if cid in valid_ids:
                        by_id[cid].boost += BOOST_RECURSIVE_PICK
            except Exception as exc:
                result.notes.append(f"recursive selection failed: {exc}")
                log.info("chunker: recursive selection failed: %s", exc)

    return result
