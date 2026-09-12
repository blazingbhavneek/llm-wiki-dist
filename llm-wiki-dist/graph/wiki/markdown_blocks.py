"""Atomic Markdown block parsing with hardened boundaries.

Plan section 2, gap 2: the legacy chunker only extended a window up to
``max_extra`` lines, so an unusually long fenced block, table, or
``<image-unit>`` could still be cut.  Here an atomic block is *indivisible*:
a boundary moves to the nearest safe point in either direction, with no soft
line limit, and a malformed block fails loudly instead of being sliced.

Line numbers are 1-based inclusive, matching the rest of the pipeline.  A
"cut" before line ``split_line`` sits between ``split_line - 1`` and
``split_line``; ``split_line == total + 1`` means "end of document".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from graph.chunk import is_tableish_line, scan_markdown_fences

IMAGE_UNIT_OPEN = "<image-unit>"
IMAGE_UNIT_CLOSE = "</image-unit>"

BlockKind = Literal["fence", "table", "image-unit"]


class MalformedBlockError(RuntimeError):
    """The source itself is broken; slicing it would silently lose content."""


@dataclass(frozen=True)
class AtomicBlock:
    kind: BlockKind
    start: int
    end: int

    @property
    def line_count(self) -> int:
        return self.end - self.start + 1

    def contains_cut(self, split_line: int) -> bool:
        # A cut is immediately *before* split_line.  A cut before the first
        # line of a block is therefore safe; cuts before later block lines are
        # inside the block.
        return self.start < split_line <= self.end


@dataclass
class BlockIndex:
    """Which atomic block (if any) a candidate cut would fall inside."""

    line_count: int
    blocks: list[AtomicBlock] = field(default_factory=list)
    owner: list[int] = field(default_factory=list)

    def block_at_cut(self, split_line: int) -> AtomicBlock | None:
        if split_line <= 1 or split_line > self.line_count:
            return None
        index = self.owner[split_line]
        if index < 0:
            return None
        block = self.blocks[index]
        return block if block.contains_cut(split_line) else None

    def cut_is_safe(self, split_line: int) -> bool:
        if split_line <= 1 or split_line > self.line_count + 1:
            return False
        return self.block_at_cut(split_line) is None

    def blocks_overlapping(self, start: int, end: int) -> list[AtomicBlock]:
        return [
            block
            for block in self.blocks
            if not (block.end < start or block.start > end)
        ]

    def nearest_safe_cut(
        self,
        candidate: int,
        *,
        backsearch: int = 0,
        forward_limit: int | None = None,
    ) -> int:
        """Closest safe cut to ``candidate``; forward search is unbounded.

        Atomicity always wins over the target size, so the forward search has
        no line budget: an image unit of any length is traversed whole.
        """

        candidate = max(2, min(candidate, self.line_count + 1))
        if self.cut_is_safe(candidate):
            return candidate

        best: tuple[int, int] | None = None  # (distance, cut)

        for delta in range(1, max(0, backsearch) + 1):
            cut = candidate - delta
            if cut <= 1:
                break
            if self.cut_is_safe(cut):
                best = (delta, cut)
                break

        ceiling = self.line_count + 1
        if forward_limit is not None:
            ceiling = min(ceiling, candidate + forward_limit)
        for delta in range(1, ceiling - candidate + 1):
            cut = candidate + delta
            if cut > ceiling:
                break
            if self.cut_is_safe(cut):
                if best is None or delta < best[0]:
                    best = (delta, cut)
                break

        if best is None:
            raise MalformedBlockError(
                f"no safe boundary near line {candidate}: the source contains "
                "an atomic block that never closes"
            )
        return best[1]


# --------------------------------------------------------------------------
# Scanners
# --------------------------------------------------------------------------


def _scan_image_unit_blocks(lines: list[str]) -> list[AtomicBlock]:
    blocks: list[AtomicBlock] = []
    open_line: int | None = None

    for number, line in enumerate(lines, start=1):
        if open_line is None:
            if IMAGE_UNIT_OPEN in line:
                tail = line.split(IMAGE_UNIT_OPEN, 1)[1]
                if IMAGE_UNIT_CLOSE in tail:
                    blocks.append(AtomicBlock("image-unit", number, number))
                else:
                    open_line = number
            continue

        if IMAGE_UNIT_CLOSE in line:
            blocks.append(AtomicBlock("image-unit", open_line, number))
            open_line = None

    if open_line is not None:
        raise MalformedBlockError(f"unclosed <image-unit> opened at line {open_line}")
    return blocks


def _scan_table_blocks(lines: list[str]) -> list[AtomicBlock]:
    blocks: list[AtomicBlock] = []
    start: int | None = None

    for number, line in enumerate(lines, start=1):
        if is_tableish_line(line):
            if start is None:
                start = number
            continue
        if start is not None:
            blocks.append(AtomicBlock("table", start, number - 1))
            start = None

    if start is not None:
        blocks.append(AtomicBlock("table", start, len(lines)))
    return blocks


def _scan_fence_blocks(lines: list[str]) -> list[AtomicBlock]:
    scan = scan_markdown_fences(lines)
    if scan.unclosed is not None:
        raise MalformedBlockError(
            "unclosed fenced code block opened at line "
            f"{scan.unclosed.line_number}: {scan.unclosed.raw_line}"
        )
    return [
        AtomicBlock("fence", opening.line_number, closing.line_number)
        for opening, closing in zip(scan.openings, scan.closings)
    ]


def build_block_index(lines: list[str]) -> BlockIndex:
    """One pass over the source: fences, tables, and image units (plan 5.1)."""

    blocks = [
        *_scan_fence_blocks(lines),
        *_scan_table_blocks(lines),
        *_scan_image_unit_blocks(lines),
    ]
    blocks.sort(key=lambda block: (block.start, block.end, block.kind))

    owner = [-1] * (len(lines) + 2)
    for index, block in enumerate(blocks):
        for line in range(block.start, min(block.end, len(lines)) + 1):
            owner[line] = index

    return BlockIndex(line_count=len(lines), blocks=blocks, owner=owner)


def atomic_windows(
    lines: list[str],
    *,
    target: int = 100,
    backsearch: int = 40,
) -> list[tuple[int, int]]:
    """Approximately ``target``-line windows on atomic-block-safe boundaries.

    Returns inclusive 1-based ``(start, end)`` ranges tiling ``1..len(lines)``.
    "100" is a target, never an integrity limit.
    """

    total = len(lines)
    if total == 0:
        return []

    index = build_block_index(lines)
    windows: list[tuple[int, int]] = []
    start = 1

    while start <= total:
        candidate = start + target
        if candidate > total:
            end = total
        else:
            cut = index.nearest_safe_cut(candidate, backsearch=backsearch)
            end = max(min(cut - 1, total), start)
        windows.append((start, end))
        start = end + 1

    return windows


def assert_no_unclosed_blocks(lines: list[str]) -> None:
    """Hard gate used by the verification stage (plan 15)."""

    build_block_index(lines)
