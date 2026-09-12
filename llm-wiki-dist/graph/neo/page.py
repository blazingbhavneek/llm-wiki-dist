"""Pure helpers for section-wise page writing.

No file IO and no model calls live here, so every function is testable with
plain strings.  Python decides section boundaries, what must survive a
rewrite verbatim, where cross-page facts go, and which titles get linked.
"""

from __future__ import annotations

import re
from typing import Sequence

from .markdown_blocks import atomic_windows, build_block_index
from .wire import ReferenceFact

HEADING_RE = re.compile(r"^#{1,4} \S")
CODE_TOKEN_RE = re.compile(r"0[xX][0-9A-Fa-f]+|[A-Za-z_][A-Za-z0-9_]{2,}")
WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}|[ァ-ヶー]{3,}|[一-龯]{2,}")
REFERENCE_MARKER_RE = re.compile(r"（参照元:\s*原文\s*(\d+)\s*(?:[-–—]\s*(\d+)\s*)?行）")
THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
PLACEHOLDER_RE = re.compile(r"\[\[NEO-IMAGE:[A-Za-z0-9_-]+\]\]")


def _nonblank(lines: Sequence[str], start: int, end: int) -> int:
    return sum(1 for line in lines[start - 1 : end] if line.strip())


def split_sections(
    lines: Sequence[str],
    start: int,
    end: int,
    *,
    target: int = 80,
    min_lines: int = 8,
) -> list[tuple[int, int]]:
    """Cut one page's owned range into writer-sized sections.

    Cuts happen before Markdown headings (never inside a fence, table or
    image unit); anything longer than ``target`` is split on atomic-block
    boundaries; anything with fewer than ``min_lines`` non-blank lines is
    merged into its neighbour.  The result tiles ``start..end`` exactly.
    """

    page = list(lines[start - 1 : end])
    total = len(page)
    if total == 0:
        return []
    index = build_block_index(page)
    cuts = [
        number
        for number, line in enumerate(page, start=1)
        if number > 1 and HEADING_RE.match(line) and index.cut_is_safe(number)
    ]
    bounds = [1, *cuts, total + 1]
    ranges = [(bounds[i], bounds[i + 1] - 1) for i in range(len(bounds) - 1)]

    split: list[tuple[int, int]] = []
    for s, e in ranges:
        if e - s + 1 > target:
            for ws, we in atomic_windows(page[s - 1 : e], target=target):
                split.append((s + ws - 1, s + we - 1))
        else:
            split.append((s, e))

    merged: list[list[int]] = []
    for s, e in split:
        if merged and _nonblank(page, merged[-1][0], merged[-1][1]) < min_lines:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    if len(merged) > 1 and _nonblank(page, merged[-1][0], merged[-1][1]) < min_lines:
        merged[-2][1] = merged[-1][1]
        merged.pop()

    result = [(start + s - 1, start + e - 1) for s, e in merged]
    assert result[0][0] == start and result[-1][1] == end
    assert all(result[i][1] + 1 == result[i + 1][0] for i in range(len(result) - 1))
    return result


def code_tokens(text: str) -> set[str]:
    """Identifier-like tokens that a lossless rewrite must keep verbatim."""

    # ponytail: bare numbers are too noisy; add units-aware numeric checks if needed.
    found: set[str] = set()
    for token in CODE_TOKEN_RE.findall(PLACEHOLDER_RE.sub(" ", text or "")):
        if (
            token[:2].lower() == "0x"
            or "_" in token
            or any(char.isdigit() for char in token)
            or any(char.isupper() for char in token[1:])
        ):
            found.add(token)
    return found


def word_tokens(text: str) -> set[str]:
    """Coarse vocabulary used only to rank reference candidates."""

    return set(WORD_RE.findall(PLACEHOLDER_RE.sub(" ", text or "")))


def verbatim_blocks(
    lines: Sequence[str], start: int, end: int
) -> list[tuple[str, int, int]]:
    """Fences and tables inside ``start..end`` as (kind, abs_start, abs_end)."""

    page = list(lines[start - 1 : end])
    index = build_block_index(page)
    return [
        (block.kind, start + block.start - 1, start + block.end - 1)
        for block in index.blocks
        if block.kind in ("fence", "table")
    ]


def _fence_flags(lines: Sequence[str]) -> list[bool]:
    """True for every line that is a fence delimiter or inside a fence."""

    flags: list[bool] = []
    inside = False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            flags.append(True)
            inside = not inside
            continue
        flags.append(inside)
    return flags


def demote_h1(text: str) -> str:
    """Turn ``# `` into ``## `` outside fences; the page owns the only H1."""

    lines = text.splitlines()
    flags = _fence_flags(lines)
    return "\n".join(
        ("## " + line[2:]) if not flags[i] and line.startswith("# ") else line
        for i, line in enumerate(lines)
    )


def normalize_draft(raw: str) -> str:
    """Strip model noise (thinking, a whole-output fence) and demote H1."""

    text = THINK_RE.sub("", raw or "").strip()
    lines = text.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        first = lines[0].strip()
        fence_count = sum(1 for line in lines if line.lstrip().startswith("```"))
        if first in ("```markdown", "```md") or (first == "```" and fence_count % 2 == 1):
            lines = lines[1:-1]
    text = demote_h1("\n".join(line.rstrip() for line in lines))
    return text.strip() + "\n"


def table_row_key(line: str) -> str:
    return re.sub(r"\s*\|\s*", "|", line.strip())


def check_section(
    draft: str,
    *,
    lines: Sequence[str],
    source_text: str,
    block_ranges: Sequence[tuple[str, int, int]],
    placeholders: Sequence[str],
    facts: Sequence[ReferenceFact],
) -> list[str]:
    """Mechanical lossless checks. Every returned string is writer feedback."""

    errors: list[str] = []
    if not draft.strip():
        return ["出力が空である。節の本文をMarkdownで書くこと。"]
    draft_lines = draft.splitlines()
    draft_compact = "\n".join(line.rstrip() for line in draft_lines)
    draft_rows = {table_row_key(line) for line in draft_lines if line.strip().startswith("|")}

    for kind, s, e in block_ranges:
        block = [lines[number - 1] for number in range(s, e + 1)]
        if kind == "fence":
            inner = "\n".join(line.rstrip() for line in block[1:-1])
            if inner.strip() and inner not in draft_compact:
                errors.append(
                    f"原文 {s}-{e}行のコードブロックが一字一句同じ形で含まれていない。"
                    "中身を変えず、そのまま貼ること。"
                )
        else:
            missing = [
                line for line in block
                if line.strip().startswith("|") and table_row_key(line) not in draft_rows
            ]
            if missing:
                errors.append(
                    f"原文 {s}-{e}行の表から次の行が欠けている（表は全行そのまま写す）: "
                    + missing[0].strip()[:80]
                )

    for placeholder in placeholders:
        count = draft.count(placeholder)
        if count != 1:
            errors.append(f"画像トークン {placeholder} は必ず1回だけ置くこと（現在{count}回）。")

    missing_tokens = sorted(code_tokens(source_text) - code_tokens(draft))
    if missing_tokens:
        errors.append(
            "次の識別子・定数が本文から消えている。省略や言い換えをせず必ず書くこと: "
            + ", ".join(missing_tokens[:40])
        )

    marked = [
        (int(match.group(1)), int(match.group(2) or match.group(1)))
        for match in REFERENCE_MARKER_RE.finditer(draft)
    ]
    for fact in facts:
        if not any(ms <= fact.source_start and fact.source_end <= me for ms, me in marked):
            errors.append(
                f"参照事実「{fact.description.strip()[:60]}」を本文へ組み込み、"
                f"その直後に（参照元: 原文 {fact.source_start}-{fact.source_end}行）と書くこと。"
            )
    return errors


def assign_facts(
    facts: Sequence[ReferenceFact], sections: Sequence[tuple[int, int]]
) -> list[list[ReferenceFact]]:
    """Bucket each fact into the section that owns its ``target_line``."""

    buckets: list[list[ReferenceFact]] = [[] for _ in sections]
    if not sections:
        return buckets
    for fact in facts:
        index = next(
            (i for i, (s, e) in enumerate(sections) if s <= fact.target_line <= e), 0
        )
        buckets[index].append(fact)
    return buckets


def _link_once(line: str, title: str, filename: str) -> str | None:
    at = line.find(title)
    while at >= 0:
        before = line[:at]
        if before.count("[") == before.count("]") and before.count("`") % 2 == 0:
            return before + f"[{title}]({filename})" + line[at + len(title):]
        at = line.find(title, at + 1)
    return None


def link_titles(markdown: str, targets: Sequence[tuple[str, str]]) -> str:
    """Wrap the first plain occurrence of each other page's title in a link.

    Skips fences, headings, tables, HTML/image lines, inline code and existing
    link text.  Idempotent: a title already linked to its file is left alone.
    """

    # ponytail: exact-title links only; add identifier ownership when under-linking matters.
    lines = markdown.splitlines()
    flags = _fence_flags(lines)
    for title, filename in sorted(targets, key=lambda item: -len(item[0])):
        title = title.strip()
        if len(title) < 2 or f"]({filename})" in "\n".join(lines):
            continue
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if flags[i] or stripped.startswith(("#", "|", "<", "[[NEO-IMAGE", "![")):
                continue
            linked = _link_once(line, title, filename)
            if linked is not None:
                lines[i] = linked
                break
    return "\n".join(lines).rstrip() + "\n"
