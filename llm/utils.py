"""Utility helpers for LLM input cleanup."""

from __future__ import annotations

import re


_IMAGE_UNIT_RE = re.compile(
    r"<image-unit\b[^>]*>(?P<body>.*?)</image-unit>",
    re.IGNORECASE | re.DOTALL,
)

_IMAGE_DESCRIPTION_RE = re.compile(
    r"<image-description\b[^>]*>(?P<description>.*?)</image-description>",
    re.IGNORECASE | re.DOTALL,
)

_IMAGE_MEDIA_RE = re.compile(
    r"<image-media\b[^>]*>.*?</image-media>",
    re.IGNORECASE | re.DOTALL,
)


def strip_image_media(text: str) -> str:
    """Remove embedded image media payloads and keep image descriptions."""
    if not text:
        return text

    def replace_image_unit(match: re.Match[str]) -> str:
        body = match.group("body")
        description = _IMAGE_DESCRIPTION_RE.search(body)
        if description:
            return description.group("description").strip()
        return _IMAGE_MEDIA_RE.sub("", body).strip()

    return _IMAGE_UNIT_RE.sub(replace_image_unit, text).strip()
