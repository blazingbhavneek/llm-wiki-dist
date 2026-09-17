"""CSV / TSV parsing into a minimal HTML table.

CSV has no magic bytes, so :meth:`CsvParser.detect` sniffs the content and is
deliberately strict (needs a mostly-rectangular delimited grid); it is
registered last so the signature-based parsers always get first refusal.
Rendering runs in the external worker pool because a large upload produces a
large table.
"""

from __future__ import annotations

import csv
import logging
import os
from collections import Counter
from html import escape
from pathlib import Path

from formats.base import BaseParser, ExtractedDocument, ParseOptions
from workers import Workers

logger = logging.getLogger("doc-parser.csv")

_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
_DELIMITERS = (",", ";", "\t", "|")
_SNIFF_BYTES = 65536
_SNIFF_LINES = 50
_MIN_RECTANGULAR = 0.8  # fraction of sample rows that must share the modal width


class CsvError(RuntimeError):
    """The upload could not be read as a delimited table."""


def _decode(data: bytes) -> str | None:
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _read_rows(lines: list[str], delimiter: str) -> list[list[str]]:
    return [
        row
        for row in csv.reader(lines, delimiter=delimiter, quotechar='"')
        if row
    ]


def _pick_delimiter(lines: list[str]) -> tuple[str, int] | None:
    """Return (delimiter, modal column count) for the most rectangular grid.

    ``csv.Sniffer`` is consulted as a tie-breaker only; it rejects ragged rows,
    quoted newlines and trailing delimiters, so the decision is a manual
    per-delimiter scan.
    """
    try:
        sniffed = csv.Sniffer().sniff(
            "\n".join(lines[:_SNIFF_LINES]), delimiters="".join(_DELIMITERS)
        ).delimiter
    except csv.Error:
        sniffed = None

    best: tuple[float, int, str] | None = None
    for delimiter in _DELIMITERS:
        try:
            rows = _read_rows(lines[:200], delimiter)
        except csv.Error:
            continue
        if len(rows) < 2:
            continue
        modal_width, modal_count = Counter(len(r) for r in rows).most_common(1)[0]
        if modal_width < 2:
            continue
        score = modal_count / len(rows) + (0.05 if delimiter == sniffed else 0.0)
        candidate = (score, modal_width, delimiter)
        if best is None or candidate > best:
            best = candidate

    if best is None or best[0] < _MIN_RECTANGULAR:
        return None
    return best[2], best[1]


def _looks_like_table(text: str) -> tuple[str, int] | None:
    """Return (delimiter, column count) when the text is a delimited grid."""
    stripped = text.lstrip()
    if not stripped or stripped[0] in "{[<":  # JSON / XML / HTML, not CSV
        return None

    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    return _pick_delimiter(lines)


def _cell(value: str) -> str:
    text = escape(value or "", quote=False)
    return text.replace("\r\n", "<br>").replace("\n", "<br>")


def run_csv(csv_path: str, output_dir: str) -> str:
    """Render a delimited file as a minimal HTML table."""
    source = Path(csv_path).resolve()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    markdown_path = destination / "document.md"

    text = _decode(source.read_bytes())
    if text is None:
        raise CsvError("file is not valid text in any supported encoding")

    table = _looks_like_table(text[:_SNIFF_BYTES])
    if table is None:
        raise CsvError("file does not parse as a delimited table")
    delimiter, _ = table

    rows = _read_rows(text.splitlines(), delimiter)
    if not rows:
        raise CsvError("no data rows found")

    width = max(len(row) for row in rows)
    max_rows = int(os.getenv("CSV_MAX_ROWS", "5000"))
    body = rows[1:]
    truncated = len(body) > max_rows
    if truncated:
        body = body[:max_rows]

    def cells(row: list[str], tag: str) -> str:
        padded = [_cell(v) for v in row] + [""] * (width - len(row))
        return "<tr>" + "".join(f"<{tag}>{value}</{tag}>" for value in padded) + "</tr>"

    title = source.stem if source.stem not in {"document", ""} else "CSV data"
    lines = [
        f"# {_cell(title)}",
        "",
        "<table>",
        cells(rows[0], "th"),
    ]
    lines.extend(cells(row, "td") for row in body)
    lines.append("</table>")
    if truncated:
        lines.append("")
        lines.append(
            f"_Showing the first {max_rows:,} of {len(rows) - 1:,} data rows._"
        )

    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(markdown_path)


class CsvParser(BaseParser):
    name = "csv"

    @classmethod
    def detect(cls, data: bytes) -> bool:
        if not data or b"\x00" in data[:8192]:
            return False
        text = _decode(data[:_SNIFF_BYTES])
        if text is None:
            return False
        return _looks_like_table(text) is not None

    async def _extract(
        self,
        data: bytes,
        image_dir: str,
        options: ParseOptions,
        workers: Workers,
    ) -> ExtractedDocument:
        work_dir = Path(image_dir)
        upload_name = Path(options.filename or "").name or "document.csv"
        if Path(upload_name).suffix.lower() != ".csv":
            upload_name = f"{Path(upload_name).stem}.csv"
        csv_path = work_dir / upload_name
        output_dir = work_dir / "csv-output"
        csv_path.write_bytes(data)

        markdown_path = Path(
            await workers.run_external(run_csv, str(csv_path), str(output_dir))
        )
        # CSV is profile-agnostic: it has no images, no LLM work, and always
        # returns an empty page list. The whole table is one Markdown document.
        return ExtractedDocument(
            markdown=markdown_path.read_text(encoding="utf-8"),
            pages=[],
            markdown_path=markdown_path,
            asset_root=output_dir,
        )
