"""
Stage 0 + Stage 1: legality mask and the boundary-strength lattice.

A "boundary" b (1 <= b < n_lines, 1-based) is the cut between source line b
and line b+1: the previous chunk ends at line b, the next starts at b+1.

Every signal returns a dense float array indexed by boundary position
(index 0 == boundary after line 1). Signals are z-normalized per document and
fused with config weights; illegal boundaries get -inf so no downstream stage
can ever cut inside a fence / table / <image-unit> block.

All required signals are pure stdlib. SaT (wtpsplit) and PPL (logprobs
endpoint) are optional and silently disable themselves when unavailable —
the pipeline records which signals actually ran in ablation.json.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

from .config import ChunkConfig

log = logging.getLogger("chunker")

NEG_INF = float("-inf")

# ---------------------------------------------------------------------
# Stage 0: legality mask
# ---------------------------------------------------------------------


def is_fence_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("```") or stripped.startswith("~~~")


def is_tableish_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("|") and "|" in stripped[1:]:
        return True
    if re.match(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$", stripped):
        return True
    return False


def legality_mask(lines: list[str]) -> list[bool]:
    """legal[b-1] == True when cutting between line b and b+1 is allowed."""
    n = len(lines)

    fence_state = []
    in_fence = False
    for line in lines:
        if is_fence_line(line):
            in_fence = not in_fence
        fence_state.append(in_fence)

    img_state = []
    in_img = False
    for line in lines:
        if "<image-unit>" in line:
            in_img = True
        if "</image-unit>" in line:
            in_img = False
        img_state.append(in_img)

    legal = []
    for b in range(1, n):  # boundary after line b (1-based)
        above = lines[b - 1]
        below = lines[b]
        inside_block = fence_state[b - 1] or img_state[b - 1]
        inside_table = is_tableish_line(above) or is_tableish_line(below)
        legal.append(not inside_block and not inside_table)
    return legal


def repeated_junk_lines(lines: list[str]) -> set[str]:
    """Short lines repeated many times = page headers/footers; they must not
    look like section starts."""
    counts = Counter(
        line.strip() for line in lines if line.strip() and len(line.strip()) < 80
    )
    return {text for text, count in counts.items() if count >= 5}


# ---------------------------------------------------------------------
# Structure prior
# ---------------------------------------------------------------------

_ATX_RE = re.compile(r"^(#{1,6})\s+\S")
_SETEXT_RE = re.compile(r"^(={3,}|-{3,})\s*$")
_NUMBERED_RE = re.compile(r"^\s{0,3}\d+(\.\d+)*[.)]?\s+\S")
_CJK_SECTION_RE = re.compile(r"^\s*第\s*[0-9０-９一二三四五六七八九十百]+\s*[章節条部編項]")
_WORD_SECTION_RE = re.compile(
    r"^\s*(Chapter|Section|Article|Part|Appendix|付録|別表)\s+[\dIVXivx]", re.IGNORECASE
)
_HR_RE = re.compile(r"^\s{0,3}([-*_])\s*(\1\s*){2,}$")
_CAPS_RE = re.compile(r"^[A-Z][A-Z0-9 \-:,.()&/]{2,59}$")


def structure_scores(lines: list[str], junk: set[str]) -> list[float]:
    """Score of boundary before line b+1 comes from what line b+1 *starts*."""
    n = len(lines)
    scores = [0.0] * max(0, n - 1)

    for b in range(1, n):
        below = lines[b]
        stripped = below.strip()
        score = 0.0

        if stripped and stripped not in junk:
            atx = _ATX_RE.match(below)
            if atx:
                level = len(atx.group(1))
                score += max(1.0, 6.0 - level)  # h1=5 ... h6=1
            elif b + 1 < n and _SETEXT_RE.match(lines[b + 1]) and stripped:
                # setext heading: line b+1 underlined by === or ---
                score += 4.0
            elif _CJK_SECTION_RE.match(below) or _WORD_SECTION_RE.match(below):
                score += 4.0
            elif _NUMBERED_RE.match(below) and len(stripped) < 100:
                score += 2.0
            elif _CAPS_RE.match(stripped):
                score += 2.0
            elif _HR_RE.match(below):
                score += 1.5

            # block starts are natural boundaries too
            if is_fence_line(below) or "<image-unit>" in below:
                score += 1.0
            if is_tableish_line(below) and not is_tableish_line(lines[b - 1]):
                score += 1.0

        # blank-run bonus: boundary preceded by blank lines
        blanks = 0
        k = b - 1
        while k >= 0 and not lines[k].strip():
            blanks += 1
            k -= 1
        if blanks >= 2:
            score += 1.5
        elif blanks == 1:
            score += 0.5

        scores[b - 1] = score

    return scores


# ---------------------------------------------------------------------
# TextTiling lexical cohesion
# ---------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _is_cjk_heavy(text: str) -> bool:
    if not text:
        return False
    cjk = sum(1 for ch in text if "぀" <= ch <= "鿿")
    return cjk > 0.2 * len(text)


def _tokens(text: str, cjk: bool) -> Counter:
    if cjk:
        chars = [ch for ch in text if not ch.isspace()]
        return Counter(a + b for a, b in zip(chars, chars[1:]))  # char bigrams
    return Counter(_TOKEN_RE.findall(text.lower()))


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in common)
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(
        sum(v * v for v in b.values())
    )
    return dot / norm if norm else 0.0


def texttiling_scores(lines: list[str], window: int = 20) -> list[float]:
    """Depth score at each boundary: how much lexical similarity dips there
    relative to the nearest peaks on both sides. Classic TextTiling."""
    n = len(lines)
    if n < 2:
        return []

    cjk = _is_cjk_heavy("\n".join(lines[: min(n, 400)]))

    sims = [0.0] * (n - 1)
    for b in range(1, n):
        lo, hi = max(0, b - window), min(n, b + window)
        left = _tokens("\n".join(lines[lo:b]), cjk)
        right = _tokens("\n".join(lines[b:hi]), cjk)
        sims[b - 1] = _cosine(left, right)

    depths = [0.0] * (n - 1)
    for i in range(n - 1):
        peak_left = sims[i]
        for j in range(i - 1, -1, -1):
            if sims[j] >= peak_left:
                peak_left = sims[j]
            else:
                break
        peak_right = sims[i]
        for j in range(i + 1, n - 1):
            if sims[j] >= peak_right:
                peak_right = sims[j]
            else:
                break
        depths[i] = (peak_left - sims[i]) + (peak_right - sims[i])
    return depths


# ---------------------------------------------------------------------
# Optional signals: SaT (wtpsplit) and PPL (logprobs endpoint)
# ---------------------------------------------------------------------


def sat_scores(lines: list[str]) -> list[float] | None:
    """Paragraph-boundary probability from wtpsplit's SaT model. Optional:
    returns None when the dependency is missing."""
    try:
        from wtpsplit import SaT  # type: ignore
    except Exception:
        log.info("chunker: wtpsplit not installed, SaT signal disabled")
        return None

    try:
        model = SaT("sat-3l")
        text = "\n".join(lines)
        probs = model.predict_proba(text)
        # Map char-level newline probabilities to line boundaries.
        scores = [0.0] * (len(lines) - 1)
        pos = 0
        for i, line in enumerate(lines[:-1]):
            pos += len(line)
            if pos < len(probs):
                scores[i] = float(probs[pos])
            pos += 1  # the newline itself
        return scores
    except Exception as exc:
        log.info("chunker: SaT signal failed (%s), disabled", exc)
        return None


def ppl_scores(lines: list[str], base_url: str, model: str) -> list[float] | None:
    """Perplexity-spike signal from a local OpenAI-compatible /completions
    endpoint that supports prompt logprobs (vLLM). Optional; returns None on
    any failure."""
    if not base_url or not model:
        return None
    try:
        import requests
    except Exception:
        return None

    try:
        n = len(lines)
        scores = [0.0] * (n - 1)
        # Score each boundary by the surprisal of the following line given
        # the preceding context window. Batched loosely to bound calls.
        step = 40
        for start in range(0, n - 1, step):
            end = min(n - 1, start + step)
            context = "\n".join(lines[max(0, start - 60) : end + 1])
            resp = requests.post(
                base_url.rstrip("/") + "/completions",
                json={
                    "model": model,
                    "prompt": context,
                    "max_tokens": 0,
                    "echo": True,
                    "logprobs": 0,
                },
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            lp = data["choices"][0].get("logprobs") or {}
            token_logprobs = lp.get("token_logprobs") or []
            tokens = lp.get("tokens") or []
            # Attribute mean surprisal to lines via newline tokens.
            line_idx = max(0, start - 60)
            acc, count = 0.0, 0
            for tok, logprob in zip(tokens, token_logprobs):
                if logprob is None:
                    continue
                acc += -float(logprob)
                count += 1
                if "\n" in tok:
                    if start <= line_idx < end and count:
                        scores[line_idx] = acc / count
                    line_idx += tok.count("\n")
                    acc, count = 0.0, 0
        return scores
    except Exception as exc:
        log.info("chunker: PPL signal failed (%s), disabled", exc)
        return None


# ---------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------


def _zscores(values: list[float]) -> list[float]:
    finite = [v for v in values if v != NEG_INF]
    if not finite:
        return values
    mean = sum(finite) / len(finite)
    var = sum((v - mean) ** 2 for v in finite) / max(1, len(finite))
    std = math.sqrt(var) or 1.0
    return [(v - mean) / std if v != NEG_INF else NEG_INF for v in values]


def build_lattice(
    lines: list[str], config: ChunkConfig
) -> tuple[list[float], list[bool], dict]:
    """Returns (fused z-score per boundary, legality mask, signal report)."""
    n = len(lines)
    legal = legality_mask(lines)
    junk = repeated_junk_lines(lines) if config.detect_repeated_lines else set()

    active: list[tuple[str, float, list[float]]] = []
    report: dict[str, str] = {}

    if config.use_structure:
        active.append(("structure", config.weight_structure, structure_scores(lines, junk)))
        report["structure"] = "on"
    else:
        report["structure"] = "off"

    if config.use_texttiling:
        active.append(("texttiling", config.weight_texttiling, texttiling_scores(lines)))
        report["texttiling"] = "on"
    else:
        report["texttiling"] = "off"

    if config.use_sat:
        sat = sat_scores(lines)
        if sat is not None:
            active.append(("sat", config.weight_sat, sat))
            report["sat"] = "on"
        else:
            report["sat"] = "unavailable"
    else:
        report["sat"] = "off"

    if config.use_ppl:
        ppl = ppl_scores(lines, config.ppl_base_url, config.ppl_model)
        if ppl is not None:
            active.append(("ppl", config.weight_ppl, ppl))
            report["ppl"] = "on"
        else:
            report["ppl"] = "unavailable"
    else:
        report["ppl"] = "off"

    fused = [0.0] * max(0, n - 1)
    if active:
        total_weight = sum(w for _, w, _ in active) or 1.0
        for _, weight, raw in active:
            z = _zscores(raw)
            for i in range(len(fused)):
                if i < len(z) and z[i] != NEG_INF:
                    fused[i] += (weight / total_weight) * z[i]

    for i in range(len(fused)):
        if not legal[i]:
            fused[i] = NEG_INF

    return fused, legal, report
