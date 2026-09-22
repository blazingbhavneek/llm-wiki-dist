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

# Ordinary Markdown image references (the generic-profile shape). The
# data-URL target may itself contain a closing parenthesis in theory, but
# base64 never does and a title is uncommon, so a non-greedy target is safe.
_MD_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*(?P<target>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\s*\)"
)


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
ImageDescriptionFilter = Callable[[str], bool]


async def describe_markdown_images(
    markdown: str,
    workers: Workers,
    describe_image: ImageDescriber,
) -> str:
    """Replace generic Markdown image alt text with LLM descriptions.

    This operates only on the images already present in generic Markdown. It
    deliberately has no image-selection or document-specific logic.
    """
    matches = [
        m
        for m in _MD_IMAGE_RE.finditer(markdown)
        if m.group("target").startswith("data:image/")
    ]
    if not matches:
        return markdown

    descriptions: dict[str, asyncio.Task[str]] = {}
    for match in matches:
        target = match.group("target")
        if target not in descriptions:
            descriptions[target] = asyncio.create_task(
                workers.run_network(describe_image, target, match.group("alt"))
            )

    outcomes = await asyncio.gather(*descriptions.values(), return_exceptions=True)
    resolved = {
        target: outcome.strip()
        for target, outcome in zip(descriptions, outcomes, strict=True)
        if isinstance(outcome, str) and outcome.strip()
    }

    def replace(match: re.Match[str]) -> str:
        target = match.group("target")
        description = resolved.get(target)
        if not description:
            return match.group(0)
        # Keep the result valid Markdown even if the model emits line breaks
        # or a closing bracket in its prose.
        alt = description.replace("\n", " ").replace("]", "）")
        return f"![{alt}]({target})"

    return _MD_IMAGE_RE.sub(replace, markdown)


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
    *,
    should_describe: ImageDescriptionFilter | None = None,
    repeat_descriptions: bool = True,
    style: str = "image-unit",
) -> str:
    """Replace local Markdown images with canonical ``<image-unit>`` blocks.

    Each unique file is read and encoded once. When a describer is supplied,
    its request starts immediately and is limited by the shared network pool.
    ``should_describe`` can exclude decorative images before an LLM call is
    scheduled. If ``repeat_descriptions`` is false, repeated references still
    retain their media but only their first occurrence includes the description.
    A failed description does not prevent the image itself from being returned.

    ``style="markdown"`` selects the generic profile: inline ``![alt](data:...)``
    URLs with no LLM description and no image-unit markup. The default
    ``"image-unit"`` preserves the historical behavior for existing callers.
    """
    if style == "markdown":
        return embed_markdown_data_urls(markdown, markdown_path, allowed_root)

    matches = [m for m in _IMAGE_REF_RE.finditer(markdown) if _ref_alt_target(m)]
    if not matches:
        return markdown

    assets: dict[Path, _ImageAsset] = {}
    try:
        for match in matches:
            alt, target = _ref_alt_target(match)  # type: ignore[misc]
            path = resolve_local_image(markdown_path, allowed_root, target)
            if path is None:
                continue

            asset = assets.get(path)
            if asset is not None:
                # A later occurrence can be the first one eligible for a
                # description (for example, the same icon used at two sizes).
                if (
                    describe_image is not None
                    and asset.task is None
                    and (should_describe is None or should_describe(alt))
                ):
                    try:
                        image_bytes = path.read_bytes()
                        llm_data_url = llm_image_data_url(
                            image_bytes,
                            max_pixels=int(
                                os.getenv("LLM_IMAGE_MAX_PIXELS", "16000000")
                            ),
                        )
                    except (OSError, ValueError) as exc:
                        logger.warning(
                            "image description skipped for %s: %s",
                            path,
                            exc,
                        )
                    else:
                        asset.task = asyncio.create_task(
                            workers.run_network(describe_image, llm_data_url, alt)
                        )
                        await asyncio.sleep(0)
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
            if describe_image is not None and (
                should_describe is None or should_describe(alt)
            ):
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

    descriptions_emitted: set[Path] = set()

    def replace_image(match: re.Match[str]) -> str:
        alt_target = _ref_alt_target(match)
        if alt_target is None:
            return match.group(0)
        alt, target = alt_target
        path = resolve_local_image(markdown_path, allowed_root, target)
        asset = assets.get(path) if path is not None else None
        if asset is None:
            return match.group(0)
        description = asset.description
        if should_describe is not None and not should_describe(alt):
            description = ""
        if description and not repeat_descriptions:
            if path in descriptions_emitted:
                description = ""
            else:
                descriptions_emitted.add(path)
        return embed_data_url(
            asset.data_url,
            description=description,
            alt=alt,
        )

    return _IMAGE_REF_RE.sub(replace_image, markdown)


def embed_markdown_data_urls(
    markdown: str,
    markdown_path: Path,
    allowed_root: Path,
) -> str:
    """Replace local Markdown images with inline ``![alt](data:...)`` URLs.

    This is the generic-profile image policy: ordinary Markdown that renders
    natively and never requires the custom ``<image-unit>`` markup or a vision
    model. Alt text is preserved verbatim. Each unique file is read and encoded
    once. Dangling or out-of-root references are left untouched.
    """
    matches = [m for m in _IMAGE_REF_RE.finditer(markdown) if _ref_alt_target(m)]
    if not matches:
        return markdown

    encoded: dict[Path, str] = {}
    for match in matches:
        _alt, target = _ref_alt_target(match)  # type: ignore[misc]
        path = resolve_local_image(
            markdown_path, allowed_root, target
        )
        if path is None or path in encoded:
            continue
        try:
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            encoded[path] = image_data_url(path.read_bytes(), mime)
        except OSError as exc:
            logger.warning("could not read extracted image %s: %s", path, exc)

    if not encoded:
        return markdown

    def replace(match: re.Match[str]) -> str:
        alt_target = _ref_alt_target(match)
        if alt_target is None:
            return match.group(0)
        alt, target = alt_target
        path = resolve_local_image(
            markdown_path, allowed_root, target
        )
        data_url = encoded.get(path) if path is not None else None
        if data_url is None:
            return match.group(0)
        return f"![{alt}]({data_url})"

    return _IMAGE_REF_RE.sub(replace, markdown)


def strip_markdown_image_media(markdown: str) -> str:
    """Remove Markdown image media, keeping only readable alt/location text.

    The generic ``images=false`` counterpart to :func:`embed_markdown_data_urls`
    and the image-unit ``strip_image_media``. A Markdown image becomes its alt
    text so worksheet-cell and slide-position context survives as plain text.
    """
    if not isinstance(markdown, str) or not markdown:
        return markdown

    def replace(match: re.Match[str]) -> str:
        alt = match.group("alt").strip()
        return alt if alt else ""

    return _MD_IMAGE_RE.sub(replace, markdown)


def count_markdown_images(markdown: str) -> int:
    """Return the number of Markdown image references in a document."""
    if not isinstance(markdown, str) or not markdown:
        return 0
    return len(_MD_IMAGE_RE.findall(markdown))
