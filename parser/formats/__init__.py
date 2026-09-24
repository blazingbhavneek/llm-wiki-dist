"""Auto-detecting parser registry.

Adding a format later:
  1. Create ``formats/<fmt>.py`` with a ``BaseParser`` subclass.
  2. Add ``from formats import <fmt>`` below - registration happens
     automatically via ``BaseParser.__init_subclass__``.

``detect`` tries parsers in import order, so signature-based parsers
(magic bytes, ZIP members) come first and content-sniffed ones (CSV) last.

Parser extraction is async so each stage can choose inline, external, GPU,
or network execution through the shared ``Workers`` object.
"""

# Import parser modules to populate BaseParser's registry, most specific first.
from formats import docx as docx
from formats import pdf as pdf
from formats import pptx as pptx
from formats import xlsx as xlsx
from formats import legacy_office as legacy_office
from formats import csv as csv  # last: no magic bytes, content-sniffed
from formats.base import BaseParser, ParseOptions, ParseResult


class UnsupportedFormatError(Exception):
    """No registered parser recognized (or is registered for) the input."""


def detect(data: bytes) -> type[BaseParser]:
    """Return the first parser class whose detect() accepts the raw bytes."""
    for parser_cls in BaseParser._REGISTRY:
        if parser_cls.detect(data):
            return parser_cls
    raise UnsupportedFormatError("Could not detect document type")


def get_parser(name: str) -> BaseParser:
    """Instantiate a registered parser by name."""
    for parser_cls in BaseParser._REGISTRY:
        if parser_cls.name == name:
            return parser_cls()
    raise UnsupportedFormatError(f"Unknown parser: {name!r}")


__all__ = [
    "BaseParser",
    "ParseOptions",
    "ParseResult",
    "UnsupportedFormatError",
    "detect",
    "get_parser",
]
