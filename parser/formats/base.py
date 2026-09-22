"""Common contract every document format parser must follow."""

from __future__ import annotations

import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from client.llm import LLMClient
from utils.image_unit import count_image_units, strip_image_media
from utils.markdown_images import (
    count_markdown_images,
    describe_markdown_images,
    embed_markdown_data_urls,
    strip_markdown_image_media,
)

if TYPE_CHECKING:
    from workers import Workers


class ParseProfile(StrEnum):
    """How a parse request processes images and pages.

    ``GENERIC`` produces ordinary Markdown with data-URL images and never calls
    an LLM. ``LLM_WIKI`` preserves the historical ``<image-unit>`` pipeline.
    The profile is always selected explicitly by the caller (server route);
    it is never inferred from a URL, filename, header, or manifest.
    """

    GENERIC = "generic"
    LLM_WIKI = "llm-wiki"


@dataclass
class ParseOptions:
    """Client-level overrides for one request (kept picklable for workers)."""

    images: bool = True  # on: base64 image blocks, off: description text only
    # ``None`` keeps the historical llm-wiki default while leaving generic
    # parsing opt-in; HTTP routes pass an explicit value.
    describe_images: bool | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    filename: str | None = None  # original upload name, used for document titles
    manifest: dict | None = None  # optional caller-supplied format manifest
    profile: ParseProfile = ParseProfile.GENERIC


@dataclass(slots=True)
class ExtractedDocument:
    """Raw, format-specific extraction before any profile image policy.

    ``markdown`` and every ``pages`` entry share the same relative image
    references. ``BaseParser.parse`` embeds them either as Markdown data URLs
    (generic) or leaves them pre-embedded as image-unit blocks (llm-wiki).
    The internal structure is never exposed directly in the HTTP API.
    """

    markdown: str
    pages: list[str] = field(default_factory=list)
    markdown_path: Path | None = None
    asset_root: Path | None = None


@dataclass
class ParseResult:
    """Outcome of a parse, returned to the client."""

    markdown: str
    parser: str
    image_count: int = 0
    duration_s: float = 0.0
    meta: dict = field(default_factory=dict)
    pages: list[str] = field(default_factory=list)


class BaseParser(ABC):
    """All document parsers follow this contract.

    Subclasses set ``name`` and implement ``detect`` and async ``_extract``.
    An extractor may run several stages through ``workers``; a format is
    intentionally not pinned to one executor. Image embedding, the client
    image policy, and result assembly remain lightweight inline work.
    """

    name: ClassVar[str] = "base"
    stream_response: ClassVar[bool] = False

    _REGISTRY: ClassVar[list[type[BaseParser]]] = []

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # Only register concrete parsers, not intermediate abstract bases.
        if "detect" in cls.__dict__ and "name" in cls.__dict__:
            BaseParser._REGISTRY.append(cls)

    @classmethod
    @abstractmethod
    def detect(cls, data: bytes) -> bool:
        """Return True if this parser handles the raw bytes (magic bytes etc.)."""

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def parse(
        self,
        data: bytes,
        options: ParseOptions | None,
        workers: Workers,
    ) -> ParseResult:
        """Template method: staged extract -> inline assembly and image policy.

        ``GENERIC`` embeds ordinary Markdown data-URL images into both the
        full document and every page, optionally replacing their alt text with
        LLM descriptions. ``LLM_WIKI`` trusts the extractor's already-embedded
        image-unit output and applies the historical stripping/counting policy.
        ``image_count`` always describes the returned ``markdown``, never the
        sum across pages.
        """
        options = options or ParseOptions()
        if options.describe_images is None:
            options = replace(
                options,
                describe_images=options.profile == ParseProfile.LLM_WIKI,
            )
        start = time.perf_counter()

        with tempfile.TemporaryDirectory(prefix="doc-parser-") as image_dir:
            document = await self._extract(data, image_dir, options, workers)

            if options.profile == ParseProfile.GENERIC:
                markdown, pages = self._embed_generic(document)
                if options.describe_images:
                    client = LLMClient(
                        base_url=options.llm_base_url,
                        api_key=options.llm_api_key,
                        model=options.llm_model,
                    )
                    try:
                        markdown = await describe_markdown_images(
                            markdown, workers, client.describe_image
                        )
                        pages = [
                            await describe_markdown_images(
                                page, workers, client.describe_image
                            )
                            for page in pages
                        ]
                    finally:
                        await client.close()
                if not options.images:
                    markdown = strip_markdown_image_media(markdown)
                    pages = [strip_markdown_image_media(page) for page in pages]
                # Count after the images=false strip so the returned count
                # always describes the returned markdown.
                image_count = count_markdown_images(markdown)
            else:
                markdown = document.markdown
                pages = list(document.pages)
                image_count = count_image_units(markdown)
                if not options.images:
                    markdown = strip_image_media(markdown)
                    pages = [strip_image_media(page) for page in pages]

        return ParseResult(
            markdown=markdown,
            parser=self.name,
            image_count=image_count,
            duration_s=time.perf_counter() - start,
            pages=pages,
        )

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------
    @abstractmethod
    async def _extract(
        self,
        data: bytes,
        image_dir: str,
        options: ParseOptions,
        workers: Workers,
    ) -> ExtractedDocument:
        """Format-specific step.

        Keep genuinely small work inline. Use ``workers.run_external`` for
        blocking converters, ``workers.run_gpu`` for GPU functions, and
        ``workers.run_network`` for async image-description/LLM calls.

        In ``GENERIC`` return raw Markdown with relative image references
        (``![alt](media/img_1.png)``) plus the resolved ``markdown_path`` and
        ``asset_root`` so :meth:`parse` can embed data URLs. In ``LLM_WIKI``
        return the historical fully-embedded ``<image-unit>`` Markdown and
        pages; ``markdown_path``/``asset_root`` are unused on that path.
        """

    def _embed_generic(self, document: ExtractedDocument) -> tuple[str, list[str]]:
        """Replace relative references with Markdown data URLs everywhere."""
        markdown = document.markdown
        pages = list(document.pages)
        if document.markdown_path is not None and document.asset_root is not None:
            markdown = embed_markdown_data_urls(
                markdown,
                document.markdown_path,
                document.asset_root,
            )
            pages = [
                embed_markdown_data_urls(
                    page, document.markdown_path, document.asset_root
                )
                for page in pages
            ]
        return markdown, pages
