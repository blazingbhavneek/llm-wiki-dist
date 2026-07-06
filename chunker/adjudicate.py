"""
Stage 4: bounded yes/no adjudication of ambiguous cuts.

Only cuts whose fused score sits below a confidence band get an LLM look, as
a binary forced-choice ("does a new section start here?"), k votes, majority.
On "no" the chunk merges into its predecessor when the merged size stays
within the hard cap. Ties and failures keep the structural default (no flip).

Cost is hard-bounded by config.adjudicate_max_cuts * config.adjudicate_votes
plain completions. The `llm` argument needs only:
    complete(system_prompt: str, user_content: str) -> str
"""

from __future__ import annotations

import logging

from .assemble import PlannedChunk
from .config import ChunkConfig

log = logging.getLogger("chunker")

_SYSTEM = """You check one proposed section boundary inside a document.
You see the last lines of the previous section and the first lines of the
next section. Answer with exactly one word:
yes  - a new section genuinely starts here
no   - this is a continuation of the same section"""


def _context(lines: list[str], boundary: int, width: int = 12) -> str:
    above = lines[max(0, boundary - width) : boundary]
    below = lines[boundary : boundary + width]
    return (
        "----- end of previous section -----\n"
        + "\n".join(above)
        + "\n----- proposed boundary -----\n"
        + "\n".join(below)
    )


def _vote(llm, lines: list[str], boundary: int, votes: int) -> bool | None:
    """True = keep the cut, False = merge, None = no majority / failure."""
    yes, no = 0, 0
    for _ in range(votes):
        try:
            answer = llm.complete(_SYSTEM, _context(lines, boundary)).strip().lower()
        except Exception as exc:
            log.info("chunker: adjudication call failed: %s", exc)
            return None
        if answer.startswith("yes"):
            yes += 1
        elif answer.startswith("no"):
            no += 1
    if yes == no:
        return None
    return yes > no


def adjudicate(
    chunks: list[PlannedChunk],
    lines: list[str],
    config: ChunkConfig,
    llm=None,
) -> tuple[list[PlannedChunk], dict]:
    report = {"reviewed": 0, "merged": 0, "kept": 0, "skipped": 0}
    if not (config.use_adjudicator and llm is not None) or len(chunks) < 2:
        return chunks, report

    # Ambiguous = weakest cut scores first; hard cap on reviews.
    order = sorted(range(1, len(chunks)), key=lambda i: chunks[i].cut_score)
    ambiguous = [
        i
        for i in order
        if chunks[i].cut_score < config.adjudicate_zscore_band
    ][: config.adjudicate_max_cuts]

    merge_starts: set[int] = set()
    for i in ambiguous:
        report["reviewed"] += 1
        verdict = _vote(llm, lines, chunks[i].source_start - 1, config.adjudicate_votes)
        if verdict is None:
            report["skipped"] += 1
        elif verdict:
            report["kept"] += 1
        else:
            merge_starts.add(chunks[i].source_start)

    if not merge_starts:
        return chunks, report

    merged: list[PlannedChunk] = []
    for chunk in chunks:
        if (
            merged
            and chunk.source_start in merge_starts
            and (chunk.source_end - merged[-1].source_start + 1)
            <= config.size_hard_max
        ):
            merged[-1] = PlannedChunk(
                source_start=merged[-1].source_start,
                source_end=chunk.source_end,
                cut_score=merged[-1].cut_score,
            )
            report["merged"] += 1
        else:
            merged.append(chunk)
    return merged, report
