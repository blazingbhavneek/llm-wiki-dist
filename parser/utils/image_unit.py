"""Utilities for the custom <image-unit> markdown image blocks.

Parsers embed images as:

    <image-unit>
      <image-media>
        <img src="data:image/jpeg;base64,..." alt="">
      </image-media>
      <image-description>
        Optional LLM-generated description goes here.
      </image-description>
    </image-unit>

``strip_image_media`` is what the ``images=False`` policy uses so the
result is safe to hand to a text-only LLM (no giant base64 payloads).
"""

from __future__ import annotations

import base64
import io
import math
import re
from html import escape

from PIL import Image, ImageOps, UnidentifiedImageError

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

_IMAGE_SRC_RE = re.compile(
    r"""<img\b[^>]*\bsrc=["'](?P<src>data:image/[^"']+)["'][^>]*>""",
    re.IGNORECASE | re.DOTALL,
)


def image_data_url(image_bytes: bytes, mime: str = "image/png") -> str:
    """Encode image bytes once as a data URL suitable for Markdown and an LLM."""
    payload = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{payload}"


def llm_image_data_url(image_bytes: bytes, max_pixels: int = 16_000_000) -> str:
    """Validate and normalize an image for OpenAI-compatible vision APIs.

    Office documents may contain BMP, TIFF, EMF, or mislabeled image payloads
    that a vision endpoint rejects. Pillow decodes supported raster inputs,
    applies orientation, bounds their pixel count, and emits a standard PNG.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source)
            pixels = image.width * image.height
            if pixels > max_pixels:
                scale = math.sqrt(max_pixels / pixels)
                size = (
                    max(1, round(image.width * scale)),
                    max(1, round(image.height * scale)),
                )
                image.thumbnail(size)

            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("unsupported or invalid raster image") from exc

    return image_data_url(output.getvalue(), "image/png")


def embed_data_url(data_url: str, description: str = "", alt: str = "") -> str:
    """Build the canonical <image-unit> around an existing image data URL."""
    safe_url = escape(data_url, quote=True)
    safe_alt = escape(alt, quote=True)
    safe_description = description.strip().replace(
        "</image-description>",
        "&lt;/image-description&gt;",
    )
    return (
        "<image-unit>\n"
        f'  <image-media><img src="{safe_url}" alt="{safe_alt}"></image-media>\n'
        f"  <image-description>{safe_description}</image-description>\n"
        "</image-unit>"
    )


def embed_image(
    image_bytes: bytes,
    mime: str = "image/png",
    description: str = "",
    alt: str = "",
) -> str:
    """Build the canonical <image-unit> block with a base64-embedded image."""
    return embed_data_url(
        image_data_url(image_bytes, mime),
        description=description,
        alt=alt,
    )


def has_image_units(text: str) -> bool:
    """Return True if the text contains at least one image-unit block."""
    if not isinstance(text, str) or not text:
        return False

    return _IMAGE_UNIT_RE.search(text) is not None


def count_image_units(text: str) -> int:
    """Return the number of image-unit blocks."""
    if not isinstance(text, str) or not text:
        return 0

    return len(list(_IMAGE_UNIT_RE.finditer(text)))


def strip_image_media(text: str) -> str:
    """Remove embedded image media payloads from image-unit blocks.

    If an image-description exists, keep only the description.
    If no description exists, remove only the image-media block and keep
    any remaining non-media content inside the image-unit.
    """
    if not isinstance(text, str) or not text:
        return text

    def replace_image_unit(match: re.Match[str]) -> str:
        body = match.group("body")

        description = _IMAGE_DESCRIPTION_RE.search(body)
        if description:
            return description.group("description").strip()

        return _IMAGE_MEDIA_RE.sub("", body).strip()

    return _IMAGE_UNIT_RE.sub(replace_image_unit, text).strip()


def extract_image_descriptions(text: str) -> list[str]:
    """Return non-empty image-description values from image-unit blocks."""
    if not isinstance(text, str) or not text:
        return []

    descriptions: list[str] = []

    for unit_match in _IMAGE_UNIT_RE.finditer(text):
        body = unit_match.group("body")
        description = _IMAGE_DESCRIPTION_RE.search(body)

        if description:
            value = description.group("description").strip()
            if value:
                descriptions.append(value)

    return descriptions


def extract_image_data_urls(text: str) -> list[str]:
    """Return data:image/... URLs from image-media blocks."""
    if not isinstance(text, str) or not text:
        return []

    urls: list[str] = []

    for unit_match in _IMAGE_UNIT_RE.finditer(text):
        body = unit_match.group("body")
        media = _IMAGE_MEDIA_RE.search(body)

        if not media:
            continue

        src = _IMAGE_SRC_RE.search(media.group(0))

        if src:
            urls.append(src.group("src").strip())

    return urls


def extract_image_base64(text: str) -> list[str]:
    """Return raw base64 payloads from image data URLs."""
    values: list[str] = []

    for data_url in extract_image_data_urls(text):
        if "," in data_url:
            values.append(data_url.split(",", 1)[1])

    return values
