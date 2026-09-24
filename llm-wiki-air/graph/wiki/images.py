"""Images are immutable evidence (plan section 10).

An ``<image-unit>`` belongs to its owning source chunk; it is never optional
decoration.  Two representations exist and they are deliberately different
types so sanitized text cannot be mistaken for wiki source:

``ImageUnit.raw``
    Exact ``<image-unit>`` bytes, including the base64 media payload.  Only
    deterministic code (the materializer and the substitution step) reads it.

``SanitizedSource`` / ``ImageUnit.prompt_marker``
    Stable ID, mime, alt text and description with the payload omitted.  Only
    prompt builders consume it, and it is never persisted as content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Callable, Iterable, Sequence

from .ids import image_id
from .markdown_blocks import IMAGE_UNIT_CLOSE, IMAGE_UNIT_OPEN, build_block_index
from .schemas import ImageRecord
from .storage import sha256_text, slice_text

_MEDIA_RE = re.compile(
    r"""src=["']data:(?P<mime>[\w.+-]+/[\w.+-]+);base64,(?P<data>[A-Za-z0-9+/=]*)["']""",
    re.IGNORECASE,
)
_ALT_RE = re.compile(r"""alt=["'](?P<alt>[^"']*)["']""", re.IGNORECASE)
_DESC_RE = re.compile(
    r"<image-description>(?P<desc>.*?)</image-description>", re.IGNORECASE | re.DOTALL
)
_UNIT_RE = re.compile(r"<image-unit\b[^>]*>.*?</image-unit>", re.IGNORECASE | re.DOTALL)
_BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")


@dataclass(frozen=True)
class ImageUnit:
    """Raw form of one image block. ``raw`` is byte-authoritative."""

    image_id: str
    source_start: int
    source_end: int
    raw: str
    mime: str = ""
    alt: str = ""
    description: str = ""
    unit_sha256: str = ""
    media_sha256: str = ""

    @property
    def prompt_marker(self) -> str:
        parts = [self.image_id]
        if self.mime:
            parts.append(f"type: {self.mime}")
        if self.alt:
            parts.append(f"alt: {self.alt}")
        if self.description:
            parts.append(f"description: {' '.join(self.description.split())}")
        return "[IMAGE " + "; ".join(parts) + "]"

    @property
    def placeholder(self) -> str:
        """Deterministic stand-in used in drafts and rewrite prompts."""

        return f"[[NEO-IMAGE:{self.image_id}]]"


@dataclass(frozen=True)
class SanitizedSource:
    """Model-visible text. Never write this to a wiki page or a slice file."""

    text: str
    line_count: int

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"SanitizedSource({self.line_count} lines, base64 removed)"


def _build_unit(lines: Sequence[str], start: int, end: int) -> ImageUnit:
    raw = slice_text(lines, start, end)
    media = _MEDIA_RE.search(raw)
    payload = media.group("data") if media else ""
    unit_hash = sha256_text(raw)
    alt = _ALT_RE.search(raw)
    description = _DESC_RE.search(raw)
    return ImageUnit(
        image_id=image_id(unit_hash, f"{start}-{end}"),
        source_start=start,
        source_end=end,
        raw=raw,
        mime=media.group("mime") if media else "",
        alt=alt.group("alt") if alt else "",
        description=" ".join(description.group("desc").split()) if description else "",
        unit_sha256=unit_hash,
        media_sha256=sha256_text(payload) if payload else "",
    )


def extract_image_units(lines: Sequence[str]) -> list[ImageUnit]:
    """Find every complete ``<image-unit>`` block with its exact bytes."""

    units: list[ImageUnit] = []
    open_line: int | None = None

    for number, line in enumerate(lines, start=1):
        if open_line is None and IMAGE_UNIT_OPEN in line:
            if IMAGE_UNIT_CLOSE in line.split(IMAGE_UNIT_OPEN, 1)[1]:
                units.append(_build_unit(lines, number, number))
                open_line = None
            else:
                open_line = number
            continue
        if open_line is not None and IMAGE_UNIT_CLOSE in line:
            units.append(_build_unit(lines, open_line, number))
            open_line = None

    if open_line is not None:
        raise ValueError(f"unclosed <image-unit> opened at line {open_line}")

    return units


def reuse_image_descriptions(
    previous: str,
    current: str,
    describe: Callable[[str, str], str],
) -> str:
    """Reuse descriptions by media hash and describe only unseen image bytes."""

    cached: dict[str, str] = {}
    for match in _UNIT_RE.finditer(previous):
        media = _MEDIA_RE.search(match.group(0))
        description = _DESC_RE.search(match.group(0))
        if media and description:
            cached.setdefault(
                sha256_text(media.group("data")), description.group("desc")
            )

    generated: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        block = match.group(0)
        media = _MEDIA_RE.search(block)
        description = _DESC_RE.search(block)
        if not media or not description:
            return block
        key = sha256_text(media.group("data"))
        if key in cached:
            value = cached[key]
        else:
            if key not in generated:
                alt = _ALT_RE.search(block)
                data_url = f'data:{media.group("mime")};base64,{media.group("data")}'
                generated[key] = describe(
                    data_url,
                    unescape(alt.group("alt")) if alt else "",
                ).strip().replace(
                    "</image-description>", "&lt;/image-description&gt;"
                )
            value = generated[key]
        start = description.start("desc")
        end = description.end("desc")
        return block[:start] + value + block[end:]

    return _UNIT_RE.sub(replace, current)


def neutralize_image_descriptions(text: str) -> str:
    """Remove description wording from diffs without changing line numbers."""

    def replace(match: re.Match[str]) -> str:
        block = match.group(0)
        start = match.start("desc") - match.start()
        end = match.end("desc") - match.start()
        return block[:start] + ("\n" * match.group("desc").count("\n")) + block[end:]

    return _DESC_RE.sub(replace, text)


def block_units(lines: Sequence[str]) -> list[ImageUnit]:
    """Fences and tables as placeholder units: the writer places a token,
    Python restores the exact source lines, so code is never retyped."""

    return [
        ImageUnit(
            image_id=f"{block.kind}-{block.start}-{block.end}",
            source_start=block.start,
            source_end=block.end,
            raw=slice_text(lines, block.start, block.end),
            description=f"{block.kind}: {lines[block.start - 1].strip()[:80]}",
        )
        for block in build_block_index(list(lines)).blocks
        if block.kind in ("fence", "table")
    ]


# --------------------------------------------------------------------------
# Prompt-side helpers
# --------------------------------------------------------------------------


def image_records(units: Iterable[ImageUnit]) -> list[ImageRecord]:
    """JSON-safe ledger entries: hashes and ranges, never the payload."""

    return [
        ImageRecord(
            image_id=unit.image_id,
            source_start=unit.source_start,
            source_end=unit.source_end,
            unit_sha256=unit.unit_sha256,
            media_sha256=unit.media_sha256,
            mime=unit.mime,
            alt=unit.alt,
            description=unit.description,
            prompt_marker=unit.prompt_marker,
        )
        for unit in units
    ]


def sanitize_lines(lines: Sequence[str], units: Sequence[ImageUnit]) -> list[str]:
    """Replace image media with a stable marker, keeping line numbering intact."""

    sanitized = list(lines)
    for unit in units:
        sanitized[unit.source_start - 1] = unit.prompt_marker
        for number in range(unit.source_start + 1, unit.source_end + 1):
            sanitized[number - 1] = f"<media payload omitted: {unit.image_id}>"
    return sanitized


def sanitized_source(
    lines: Sequence[str], units: Sequence[ImageUnit]
) -> SanitizedSource:
    return SanitizedSource(
        text="\n".join(sanitize_lines(lines, units)), line_count=len(lines)
    )


def numbered_prompt_block(
    lines: Sequence[str],
    units: Sequence[ImageUnit],
    source_start: int,
    source_end: int,
) -> SanitizedSource:
    """Numbered, sanitized slice for a prompt; line numbers are the real ones."""

    body = sanitize_lines(lines, units)[source_start - 1 : source_end]
    text = "\n".join(
        f"{number}: {line}" for number, line in enumerate(body, source_start)
    )
    return SanitizedSource(text=text, line_count=source_end - source_start + 1)


# --------------------------------------------------------------------------
# Substitution and integrity
# --------------------------------------------------------------------------


def placeholder_pattern(image_id_value: str) -> re.Pattern[str]:
    return re.compile(re.escape(f"[[NEO-IMAGE:{image_id_value}]]"))


def placeholders_in(markdown: str) -> list[str]:
    return re.findall(r"\[\[NEO-IMAGE:([A-Za-z0-9_-]+)\]\]", markdown or "")


def restore_images(
    markdown: str, units: Sequence[ImageUnit]
) -> tuple[str, list[str]]:
    """Deterministically place original image units back into a rewrite.

    Returns the final markdown plus any placeholder that has no known unit --
    an unresolved placeholder is a hard gate failure, never a silent drop.
    """

    by_placeholder = {unit.placeholder: unit for unit in units}
    result = markdown
    for placeholder, unit in by_placeholder.items():
        if placeholder in result:
            result = result.replace(placeholder, unit.raw)

    unresolved = [
        token
        for token in placeholders_in(result)
        if f"[[NEO-IMAGE:{token}]]" not in by_placeholder
    ]
    return result, unresolved


def missing_image_ids(markdown: str, units: Sequence[ImageUnit]) -> list[str]:
    """Images the writer dropped entirely (placeholder neither present nor raw)."""

    missing = []
    for unit in units:
        if unit.placeholder in markdown or unit.raw in markdown:
            continue
        missing.append(unit.image_id)
    return missing


def unit_intact(markdown: str, unit: ImageUnit) -> bool:
    """The raw unit appears byte-identically, and it appears exactly once."""

    return (markdown or "").count(unit.raw) == 1


def count_units(markdown: str, units: Sequence[ImageUnit]) -> dict[str, int]:
    return {unit.image_id: (markdown or "").count(unit.raw) for unit in units}


def scrub_base64(text: str) -> str:
    """Logs must never contain image data (plan 14)."""

    scrubbed = _MEDIA_RE.sub(
        lambda match: f'src="data:{match.group("mime")};base64,<omitted>"', text
    )
    return _BASE64_RUN_RE.sub("<omitted-binary>", scrubbed)
