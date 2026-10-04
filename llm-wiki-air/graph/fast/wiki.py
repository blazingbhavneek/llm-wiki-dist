"""Fast wiki behaviour: titles, identifiers, plans and context without extra model calls."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Sequence

from graph.wiki.storage import read_json, write_json_atomic

# "1.", "1.2 ", "（1）", "第3章": a section number ends in punctuation or a space,
# so "2024-04", "1.5倍" and "1:1" are left alone.
HEADING_NUMBER_RE = re.compile(
    r"^\s*(?:第\s*[0-9０-９一二三四五六七八九十百]+\s*[章節条項部編]"
    r"|[(（]?[0-9０-９]{1,3}(?:[.．][0-9０-９]{1,3})*(?:[.．)）、](?![0-9０-９])|(?=\s)))\s*"
)
ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# A translation rightly writes ``\beta_1`` as β₁ and "## 6.1 EXPERIMENT: LOGISTIC REGRESSION"
# in the output language; neither is an identifier to keep (every pdf retry was one of these).
TEX_MATH_RE = re.compile(r"\$\$.*?\$\$|\$[^$\n]+\$", re.DOTALL)
TEX_COMMAND_RE = re.compile(r"\\[A-Za-z]+")
SHOUTED_WORD_RE = re.compile(r"\b[A-Z]{2,}\b")


def strip_heading_number(text: str) -> str:
    """Remove a formatting prefix from a title without touching body text."""

    return HEADING_NUMBER_RE.sub("", text, count=1).strip()


def strip_heading_numbers(markdown: str) -> str:
    """strip_heading_number on every ATX heading outside code fences."""

    out: list[str] = []
    fenced = False
    for line in markdown.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and (match := ATX_HEADING_RE.match(line)):
            line = f"{match.group(1)} {strip_heading_number(match.group(2)) or match.group(2)}"
        out.append(line)
    return "\n".join(out)


def translatable(text: str) -> str:
    """Drop TeX command names inside math and all-caps title words on headings."""

    text = TEX_MATH_RE.sub(lambda match: TEX_COMMAND_RE.sub(" ", match.group(0)), text)
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("#") and not re.search(r"[a-z]", line):
            words = SHOUTED_WORD_RE.findall(line)
            # one short all-caps word in a heading is an acronym (## CIO の役割); keep it
            if len(words) >= 2 or any(len(word) >= 6 for word in words):
                line = SHOUTED_WORD_RE.sub(" ", line)
        lines.append(line)
    return "\n".join(lines)


def code_tokens(text: str) -> set[str]:
    from graph.wiki.page import code_tokens as tokens

    return tokens(text, prepare=translatable)


def merge_small_pages(plan: Any, *, target: int) -> Any:
    """Fold each page under half the target into its smaller neighbour (merged <= 2x target).

    The planner often returns 10-30 line pages, and every page costs at least one
    writer call, so a 373-line paper became nine pages instead of four.
    """

    from graph.wiki.document_map import _merge_seed_ranges
    from graph.wiki.schemas import CompiledSeedPlan

    pages = list(plan.pages)

    def size(page: Any) -> int:
        return page.source_end - page.source_start + 1

    while True:
        for _, i in sorted((size(page), i) for i, page in enumerate(pages) if size(page) < target // 2):
            neighbours = sorted(
                (size(pages[j]), j)
                for j in (i - 1, i + 1)
                if 0 <= j < len(pages) and size(pages[i]) + size(pages[j]) <= 2 * target
            )
            if neighbours:
                first, last = sorted((i, neighbours[0][1]))
                pages[first:last + 1] = [_merge_seed_ranges(pages[first:last + 1])]
                break
        else:
            return CompiledSeedPlan(summary=plan.summary, pages=pages)


def deterministic_seed_plan(lines: Sequence[str]) -> Any:
    """A safe single-page plan when no model client exists."""

    from graph.wiki.schemas import CompiledSeedPlan

    first = next((line.strip() for line in lines if line.strip()), "Document")
    title = re.sub(r"^#{1,6}\s+", "", first).strip() or "Document"
    return CompiledSeedPlan(
        summary="fast deterministic plan",
        pages=[{"title": title, "summary": title, "source_start": 1, "source_end": len(lines)}],
    )


def hierarchy(pages: Sequence[Any], lines: Sequence[str], *, checkpoint: Path, version: str) -> dict[str, str]:
    """Parent and page summaries from source leads and seed summaries; no model call."""

    from graph.formats.tree import lead

    cached = read_json(checkpoint, default={}) if Path(checkpoint).exists() else {}
    groups: dict[tuple[str, ...], list[Any]] = {}
    for page in pages:
        groups.setdefault(tuple(page.path[:-1]), []).append(page)
    parents: dict[str, str] = dict(cached.get("parents", {}))
    summaries: dict[str, str] = dict(cached.get("pages", {}))
    for chain, group in groups.items():
        key = " › ".join(chain)
        parents.setdefault(key, lead(lines, group[0].owner_ranges[0][0], group[-1].owner_ranges[-1][1], limit=400))
        for page in group:
            summaries.setdefault(str(page.number), page.summary or lead(lines, page.owner_ranges[0][0], page.owner_ranges[-1][1], limit=400))
    # "policy" marks the checkpoint so the standard path never reuses it as model output.
    write_json_atomic(checkpoint, {"parents": parents, "pages": summaries, "policy": version})
    for page in pages:
        page.summary = summaries.get(str(page.number), page.summary)
    return parents


MATH_RE = re.compile(r"\$\$.*?\$\$|\$[^$\n]+\$", re.DOTALL)


def mask_math(text: str) -> tuple[str, Callable[[str], str]]:
    """Swap $...$ and $$...$$ for tokens so link insertion never writes inside math."""

    spans: list[str] = []

    def hide(match: re.Match[str]) -> str:
        spans.append(match.group(0))
        return f"{len(spans) - 1}"

    masked = MATH_RE.sub(hide, text)
    if not spans:
        return text, _same

    def unmask(value: str) -> str:
        return re.sub("(\\d+)", lambda match: spans[int(match.group(1))], value)

    return masked, unmask


def _same(text: str) -> str:
    return text
