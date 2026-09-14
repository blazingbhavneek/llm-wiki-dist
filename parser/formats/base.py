"""Common contract every document format parser must follow."""

from __future__ import annotations

import mimetypes
import re
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from utils.image_unit import count_image_units, embed_image, strip_image_media

if TYPE_CHECKING:
    from workers import Workers

_MD_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<path>[^)\s]+)\)")


@dataclass
class ParseOptions:
    """Client-level overrides for one request (kept picklable for workers)."""

    images: bool = True  # on: base64 <image-unit> blocks, off: description text only
    describe_images: bool = True
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    filename: str | None = None  # original upload name, used for document titles


@dataclass
class ParseResult:
    """Outcome of a parse, returned to the client."""

    markdown: str
    parser: str
    image_count: int = 0
    duration_s: float = 0.0
    meta: dict = field(default_factory=dict)


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
        """Template method: staged extract -> inline assembly and image policy."""
        options = options or ParseOptions()
        start = time.perf_counter()

        with tempfile.TemporaryDirectory(prefix="doc-parser-") as image_dir:
            text = await self._extract(data, image_dir, options, workers)
            markdown = self._embed_images(text, image_dir)
            image_count = count_image_units(markdown)

        if not options.images:
            markdown = strip_image_media(markdown)

        return ParseResult(
            markdown=markdown,
            parser=self.name,
            image_count=image_count,
            duration_s=time.perf_counter() - start,
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
    ) -> str:
        """Format-specific step.

        Keep genuinely small work inline. Use ``workers.run_external`` for
        blocking converters, ``workers.run_gpu`` for GPU functions, and
        ``workers.run_network`` for async image-description/LLM calls.

        Return the document as markdown. Any images must be written as
        files inside ``image_dir`` and referenced relatively, e.g.
        ``![alt](img_1.png)``. ``_embed_images`` converts those references
        into base64 ``<image-unit>`` blocks afterwards.
        """

    def _embed_images(self, markdown: str, image_dir: str) -> str:
        """Replace relative image references with base64 <image-unit> blocks."""

        def replace(match: re.Match[str]) -> str:
            root = Path(image_dir).resolve()
            path = (root / match.group("path")).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                return match.group(0)  # leave dangling refs untouched
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            return embed_image(path.read_bytes(), mime=mime)

        return _MD_IMAGE_RE.sub(replace, markdown)
