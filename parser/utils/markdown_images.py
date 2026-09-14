"""Embed local Markdown images and optionally describe them with an LLM."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from utils.image_unit import embed_data_url, image_data_url, llm_image_data_url
from workers import Workers

logger = logging.getLogger("doc-parser.images")

# Pandoc's ``gfm`` writer emits any image that carries size/position attributes
# as a raw ``<img>`` tag rather than ``![alt](path)`` (``gfm-raw_html`` would fix
# that but also drops complex HTML tables), so both spellings are recognised.
_IMAGE_REF_RE = re.compile(
    r"!\[(?P<md_alt>[^\]]*)\]\((?P<md_target>[^)]+)\)"
    r"|"
    r"<img\b(?P<html_attrs>[^>]*?)/?>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_SRC_RE = re.compile(r"""\bsrc\s*=\s*["']?(?P<src>[^"'>\s]+)""", re.IGNORECASE)
_HTML_ALT_RE = re.compile(r"""\balt\s*=\s*["'](?P<alt>[^"']*)["']""", re.IGNORECASE)

ImageDescriber = Callable[[str, str], Awaitable[str]]


def _ref_alt_target(match: re.Match[str]) -> tuple[str, str] | None:
    """Return ``(alt, target)`` for a Markdown or HTML image match, or None."""
    if match.group("md_target") is not None:
        return match.group("md_alt"), match.group("md_target")
    attrs = match.group("html_attrs") or ""
    src = _HTML_SRC_RE.search(attrs)
    if src is None:
        return None
    alt = _HTML_ALT_RE.search(attrs)
    return (alt.group("alt") if alt else ""), src.group("src")


@dataclass(slots=True)
class _ImageAsset:
    data_url: str
    task: asyncio.Task[str] | None = None
    description: str = ""


def _strip_markdown_title(target: str) -> str:
    target = target.strip()
    if target.startswith("<") and ">" in target:
        return target[1 : target.index(">")].strip()
    for marker in (' "', " '"):
        if marker in target:
            return target.split(marker, 1)[0].strip()
    return target


def resolve_local_image(
    markdown_path: Path,
    allowed_root: Path,
    raw_target: str,
) -> Path | None:
    """Resolve a Markdown image target without allowing paths outside a job."""
    target = unquote(_strip_markdown_title(raw_target))
    if urlparse(target).scheme:
        return None

    target = target.split("#", 1)[0].split("?", 1)[0]
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = markdown_path.parent / candidate
    candidate = candidate.resolve()

    root = allowed_root.resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


async def embed_markdown_images(
    markdown: str,
    markdown_path: Path,
    allowed_root: Path,
    workers: Workers,
    describe_image: ImageDescriber | None = None,
) -> str:
    """Replace local Markdown images with canonical ``<image-unit>`` blocks.

    Each unique file is read and encoded once. When a describer is supplied,
    its request starts immediately and is limited by the shared network pool.
    A failed description does not prevent the image itself from being returned.
    """
    matches = [m for m in _IMAGE_REF_RE.finditer(markdown) if _ref_alt_target(m)]
    if not matches:
        return markdown

    assets: dict[Path, _ImageAsset] = {}
    try:
        for match in matches:
            alt, target = _ref_alt_target(match)  # type: ignore[misc]
            path = resolve_local_image(markdown_path, allowed_root, target)
            if path is None or path in assets:
                continue

            try:
                mime = mimetypes.guess_type(path.name)[0] or "image/png"
                image_bytes = path.read_bytes()
                encoded = image_data_url(image_bytes, mime)
            except OSError as exc:
                logger.warning("could not read extracted image %s: %s", path, exc)
                continue

            asset = _ImageAsset(data_url=encoded)
            assets[path] = asset
            if describe_image is not None:
                try:
                    llm_data_url = llm_image_data_url(
                        image_bytes,
                        max_pixels=int(
                            os.getenv("LLM_IMAGE_MAX_PIXELS", "16000000")
                        ),
                    )
                except ValueError as exc:
                    logger.warning(
                        "image description skipped for %s: %s",
                        path,
                        exc,
                    )
                    continue
                asset.task = asyncio.create_task(
                    workers.run_network(describe_image, llm_data_url, alt)
                )
                # Start this request while the next image is encoded.
                await asyncio.sleep(0)

        described = [asset for asset in assets.values() if asset.task is not None]
        if described:
            outcomes = await asyncio.gather(
                *(asset.task for asset in described if asset.task is not None),
                return_exceptions=True,
            )
            for asset, outcome in zip(described, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    logger.warning("image description failed: %s", outcome)
                else:
                    asset.description = outcome
    finally:
        pending = [
            asset.task
            for asset in assets.values()
            if asset.task is not None and not asset.task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def replace_image(match: re.Match[str]) -> str:
        alt_target = _ref_alt_target(match)
        if alt_target is None:
            return match.group(0)
        alt, target = alt_target
        path = resolve_local_image(markdown_path, allowed_root, target)
        asset = assets.get(path) if path is not None else None
        if asset is None:
            return match.group(0)
        return embed_data_url(
            asset.data_url,
            description=asset.description,
            alt=alt,
        )

    return _IMAGE_REF_RE.sub(replace_image, markdown)
