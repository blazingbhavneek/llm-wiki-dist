"""
Stage 3: global DP assembly.

Dynamic program over candidate cut points: maximize
    sum(boundary score) - sum(size-band penalty)
subject to legality. Output is a contiguous exact partition of [1, n] by
construction — validation downstream is an assertion, not a retry gate.

No LLM here. The 100-400-line size rule lives in the cost function
(enforced), not in prompt prose (unenforceable).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import ChunkConfig
from .signals import NEG_INF
from .skeleton import Candidate

log = logging.getLogger("chunker")

HARD_OVER_PENALTY = 50.0


@dataclass
class PlannedChunk:
    source_start: int  # 1-based inclusive
    source_end: int  # 1-based inclusive
    cut_score: float = 0.0  # fused+boost score of the cut that *opened* this chunk


def _size_penalty(length: int, config: ChunkConfig) -> float:
    # Penalties are in the same [0, ~4] unit scale as normalized cut rewards
    # ([0, 1]), so a strong boundary can bend the band a little but a badly
    # undersized/oversized chunk always loses.
    if length < config.size_min:
        ratio = (config.size_min - length) / max(1, config.size_min)
        return 4.0 * ratio * ratio
    if length <= config.size_max:
        return 0.0
    if length <= config.size_hard_max:
        ratio = (length - config.size_max) / max(1, config.size_max)
        return 4.0 * ratio * ratio
    # beyond hard max: huge but finite so degenerate docs stay feasible
    return HARD_OVER_PENALTY + (length - config.size_hard_max) / 10.0


def _candidate_cut_set(
    fused: list[float],
    legal: list[bool],
    candidates: list[Candidate],
    n_lines: int,
    config: ChunkConfig,
) -> dict[int, float]:
    """Map boundary (cut after line b) -> effective score. Ensures no gap
    between consecutive cut options exceeds the hard size cap so the DP is
    always feasible when any legal boundary exists."""
    boost_by_boundary = {c.boundary: c.boost for c in candidates}

    cuts: dict[int, float] = {}
    for c in candidates:
        raw = fused[c.boundary - 1]
        if raw == NEG_INF:
            continue
        cuts[c.boundary] = raw + boost_by_boundary.get(c.boundary, 0.0)

    # Feasibility fill: walk the doc; wherever the gap between available cuts
    # exceeds size_max, add the best legal boundaries inside the gap.
    positions = sorted(cuts)
    anchors = [0] + positions + [n_lines]
    for a, b in zip(anchors, anchors[1:]):
        gap_start, gap_end = a, b
        while gap_end - gap_start > config.size_max:
            window_lo = gap_start + config.size_min
            window_hi = min(gap_start + config.size_max, gap_end - 1)
            best_boundary, best_score = None, NEG_INF
            for boundary in range(window_lo, window_hi + 1):
                idx = boundary - 1
                if 0 <= idx < len(fused) and legal[idx] and fused[idx] > best_score:
                    best_boundary, best_score = boundary, fused[idx]
            if best_boundary is None:
                # no legal cut in the preferred window: take any legal one
                for boundary in range(gap_start + 1, gap_end):
                    idx = boundary - 1
                    if 0 <= idx < len(fused) and legal[idx]:
                        best_boundary, best_score = boundary, fused[idx]
                        break
            if best_boundary is None:
                break  # entire gap is one illegal block; accept the oversize
            cuts[best_boundary] = best_score
            gap_start = best_boundary
    return cuts


def assemble_partition(
    n_lines: int,
    fused: list[float],
    legal: list[bool],
    candidates: list[Candidate],
    config: ChunkConfig,
) -> list[PlannedChunk]:
    """Optimal partition of lines [1, n] over the candidate cut set."""
    if n_lines <= 0:
        return []
    if n_lines <= config.size_max:
        return [PlannedChunk(source_start=1, source_end=n_lines)]

    cuts = _candidate_cut_set(fused, legal, candidates, n_lines, config)
    # Positions where a chunk may END (cut after line b), plus doc end.
    positions = sorted(set(cuts) | {n_lines})

    # Normalize cut rewards to [0, 1]: only above-average boundaries earn a
    # reward, and the strongest boundary in this document defines 1.0. Keeps
    # rewards comparable to the size penalties regardless of signal mix.
    positive = [s for s in cuts.values() if s != NEG_INF and s > 0]
    max_positive = max(positive) if positive else 1.0

    best: dict[int, float] = {0: 0.0}
    prev: dict[int, int] = {}

    for end in positions:
        reward = (
            max(0.0, cuts.get(end, 0.0)) / max_positive if end != n_lines else 0.0
        )
        best_val, best_prev = None, None
        for start in [0] + [p for p in positions if p < end]:
            if start not in best:
                continue
            length = end - start
            if length <= 0 or length > config.size_hard_max * 3:
                continue
            value = best[start] + reward - _size_penalty(length, config)
            if best_val is None or value > best_val:
                best_val, best_prev = value, start
        if best_val is not None:
            best[end] = best_val
            prev[end] = best_prev

    if n_lines not in best:
        log.warning("chunker: DP infeasible, falling back to fixed windows")
        chunks = []
        start = 1
        while start <= n_lines:
            end = min(start + config.size_target - 1, n_lines)
            chunks.append(PlannedChunk(source_start=start, source_end=end))
            start = end + 1
        return chunks

    # Backtrack.
    ends: list[int] = []
    cursor = n_lines
    while cursor != 0:
        ends.append(cursor)
        cursor = prev[cursor]
    ends.reverse()

    chunks: list[PlannedChunk] = []
    start = 1
    for end in ends:
        chunks.append(
            PlannedChunk(
                source_start=start,
                source_end=end,
                cut_score=cuts.get(start - 1, 0.0) if start > 1 else 0.0,
            )
        )
        start = end + 1

    assert chunks[0].source_start == 1 and chunks[-1].source_end == n_lines
    for a, b in zip(chunks, chunks[1:]):
        assert b.source_start == a.source_end + 1, "partition must be exact"
    return chunks
