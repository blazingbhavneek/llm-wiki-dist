"""Excel parsing with compact HTML tables, formulas, and embedded images."""

from __future__ import annotations

import io
import logging
import os
import posixpath
import re
import subprocess
import zipfile
from datetime import date, datetime, time
from html import escape
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from bs4 import BeautifulSoup, Tag
from openpyxl import load_workbook
from openpyxl.utils import coordinate_to_tuple, get_column_letter
from openpyxl.utils.units import (
    DEFAULT_COLUMN_WIDTH,
    DEFAULT_ROW_HEIGHT,
    EMU_to_pixels,
    points_to_pixels,
)
from xlsx2html.core import render_table, worksheet_to_data

from client.llm import LLMClient
from formats.base import BaseParser, ParseOptions
from utils.markdown_images import embed_markdown_images
from utils.vector_images import convert_document_vector_images, find_libreoffice_command
from workers import Workers

logger = logging.getLogger("doc-parser.xlsx")

_CONTENT_TYPES = "[Content_Types].xml"
_WORKBOOK_XML = "xl/workbook.xml"
_RELATIONSHIP_ID = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
)
_EMBED_ID = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
)
_VECTOR_SUFFIXES = frozenset({".emf", ".wmf", ".svg"})


class OpenpyxlError(RuntimeError):
    """The XLSX renderer could not produce a usable document."""


class LibreOfficeError(RuntimeError):
    """LibreOffice could not recalculate the workbook."""


def recalculate_with_libreoffice(
    xlsx_path: str,
    output_dir: str,
    command: list[str],
) -> str:
    """Open, calculate, and save an XLSX copy through headless LibreOffice."""
    source = Path(xlsx_path).resolve()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    profile = destination / "libreoffice-profile"
    profile.mkdir(parents=True, exist_ok=True)

    args = [
        *command,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--nofirststartwizard",
        f"-env:UserInstallation={profile.as_uri()}",
        "--convert-to",
        "xlsx",
        "--outdir",
        str(destination),
        str(source),
    ]
    timeout_s = float(os.getenv("LIBREOFFICE_TIMEOUT_SECONDS", "300"))
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise LibreOfficeError(
            f"LibreOffice executable was not found: {command[0]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise LibreOfficeError(
            f"LibreOffice recalculation timed out after {timeout_s:g} seconds"
        ) from exc

    recalculated = destination / source.name
    if completed.returncode or not recalculated.is_file():
        details = (completed.stderr or completed.stdout or "no output").strip()[-4000:]
        raise LibreOfficeError(
            f"LibreOffice recalculation failed with status "
            f"{completed.returncode}: {details}"
        )
    return str(recalculated)


def _html_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date, time)):
        value = value.isoformat()
    text = escape(str(value), quote=False)
    return text.replace("\r\n", "<br>").replace("\n", "<br>")


def _render_cell(cell, cached_value: Any) -> str:
    if cell.data_type != "f":
        return _html_text(cell.value)

    formula = _html_text(cell.value)
    resolved = (
        _html_text(cached_value)
        if cached_value is not None
        else "[not stored in workbook]"
    )
    return f"数式: <code>{formula}</code><br>キャッシュ値: {resolved}"


def _worksheet_cells(worksheet) -> dict[tuple[int, int], Any]:
    """Return stored, non-empty cells without expanding a sparse used range."""
    return {
        (cell.row, cell.column): cell
        for cell in worksheet._cells.values()
        if cell.value is not None
    }


def _render_worksheet(worksheet, cached_worksheet) -> tuple[str, bool]:
    """Render one worksheet through xlsx2html and remove presentation markup."""
    cells = _worksheet_cells(worksheet)
    if not cells:
        return "_空のシート_", False

    cached_cells = _worksheet_cells(cached_worksheet)
    coordinates = sorted(cells)
    # xlsx2html preserves worksheet layout by walking from A1 through the used
    # range. Avoid expanding pathological sparse ranges into millions of tags.
    area = worksheet.max_row * worksheet.max_column
    max_table_cells = int(os.getenv("XLSX_MAX_TABLE_CELLS", "50000"))
    has_formulas = any(cell.data_type == "f" for cell in cells.values())

    def content(row: int, column: int) -> str:
        cell = cells.get((row, column))
        if cell is None:
            return ""
        cached = cached_cells.get((row, column))
        return _render_cell(cell, cached.value if cached is not None else None)

    if area > max_table_cells:
        lines = [
            "_矩形範囲が非常に大きいため、スパースセル表示を使用しました。_",
            "",
            "<table>",
            "<tr><th>Cell</th><th>Content</th></tr>",
        ]
        for row, column in coordinates:
            coordinate = f"{get_column_letter(column)}{row}"
            lines.append(
                f"<tr><td>{coordinate}</td><td>{content(row, column)}</td></tr>"
            )
        lines.append("</table>")
        return "\n".join(lines), has_formulas

    # Images are emitted separately as document image units. Prevent the
    # renderer from embedding duplicate data URLs inside cells.
    images = cached_worksheet._images
    cached_worksheet._images = []
    try:
        data = worksheet_to_data(cached_worksheet, fs=worksheet)
    finally:
        cached_worksheet._images = images

    # xlsx2html supplies formatted cached values and handles merged-cell
    # geometry. Keep both the expression and the recalculated value for formula
    # cells so a LibreOffice pass remains visible and auditable.
    for row in data["rows"]:
        for rendered_cell in row:
            formatted = rendered_cell["formatted_value"]
            if formatted == "&nbsp;":
                rendered_cell["formatted_value"] = ""
            elif isinstance(formatted, str):
                rendered_cell["formatted_value"] = formatted.replace(
                    "\r\n", "<br>"
                ).replace("\n", "<br>")

            formula_cell = cells.get(
                (rendered_cell["row"], rendered_cell["column"])
            )
            if formula_cell is None or formula_cell.data_type != "f":
                continue
            cached_cell = cached_cells.get(
                (rendered_cell["row"], rendered_cell["column"])
            )
            rendered_cell["formatted_value"] = _render_cell(
                formula_cell,
                cached_cell.value if cached_cell is not None else None,
            )

    markup = render_table(data, lambda *_: None, lambda *_: None)
    return _simplify_html_table(markup), has_formulas


def _simplify_html_table(markup: str) -> str:
    """Reduce xlsx2html output to semantic table markup without CSS noise."""
    soup = BeautifulSoup(markup, "html.parser")
    table = soup.find("table")
    if not isinstance(table, Tag):
        raise OpenpyxlError("xlsx2html completed without producing a table")

    for tag in list(table.find_all(True)):
        if tag.parent is None:
            continue
        if tag.name in {"col", "colgroup", "img"}:
            tag.decompose()
            continue
        if tag.name not in {"tr", "td", "th", "a", "code", "br"}:
            tag.unwrap()
            continue

        if tag.name in {"td", "th"}:
            tag.attrs = {
                name: value
                for name, value in tag.attrs.items()
                if name in {"rowspan", "colspan"}
                and str(value).isdigit()
                and int(value) > 1
            }
        elif tag.name == "a":
            tag.attrs = {"href": tag["href"]} if tag.get("href") else {}
        else:
            tag.attrs = {}
    table.attrs = {}
    return table.decode(formatter="minimal")


def _column_width_pixels(worksheet, column: int) -> int:
    dimension = worksheet.column_dimensions.get(get_column_letter(column))
    width = (
        dimension.width
        if dimension is not None and dimension.width is not None
        else worksheet.sheet_format.defaultColWidth or DEFAULT_COLUMN_WIDTH
    )
    return max(1, int(width * 7 + 5))


def _row_height_pixels(worksheet, row: int) -> int:
    dimension = worksheet.row_dimensions.get(row)
    height = (
        dimension.height
        if dimension is not None and dimension.height is not None
        else worksheet.sheet_format.defaultRowHeight or DEFAULT_ROW_HEIGHT
    )
    return max(1, points_to_pixels(height))


def _covered_cell_range(
    worksheet,
    start_row: int,
    start_column: int,
    width: int,
    height: int,
    column_offset: int = 0,
    row_offset: int = 0,
) -> str:
    """Return the worksheet cells touched by a one-cell image anchor."""
    end_column = start_column
    remaining_width = max(1, column_offset + width)
    while (
        remaining_width > _column_width_pixels(worksheet, end_column)
        and end_column < 16_384
    ):
        remaining_width -= _column_width_pixels(worksheet, end_column)
        end_column += 1

    end_row = start_row
    remaining_height = max(1, row_offset + height)
    while (
        remaining_height > _row_height_pixels(worksheet, end_row)
        and end_row < 1_048_576
    ):
        remaining_height -= _row_height_pixels(worksheet, end_row)
        end_row += 1

    start = f"{get_column_letter(start_column)}{start_row}"
    end = f"{get_column_letter(end_column)}{end_row}"
    return start if start == end else f"{start}:{end}"


def _image_cell_range(worksheet, image) -> str:
    anchor = image.anchor
    if isinstance(anchor, str):
        row, column = coordinate_to_tuple(anchor)
        return _covered_cell_range(
            worksheet,
            row,
            column,
            round(image.width),
            round(image.height),
        )

    marker = getattr(anchor, "_from", None)
    if marker is None:
        return "不明"
    start_row = marker.row + 1
    start_column = marker.col + 1

    end_marker = getattr(anchor, "to", None)
    if end_marker is not None:
        start = f"{get_column_letter(start_column)}{start_row}"
        end_column = max(start_column, end_marker.col + 1)
        end_row = max(start_row, end_marker.row + 1)
        end = f"{get_column_letter(end_column)}{end_row}"
        return start if start == end else f"{start}:{end}"

    extent = getattr(anchor, "ext", None)
    width = EMU_to_pixels(extent.cx) if extent is not None else round(image.width)
    height = EMU_to_pixels(extent.cy) if extent is not None else round(image.height)
    return _covered_cell_range(
        worksheet,
        start_row,
        start_column,
        width,
        height,
        EMU_to_pixels(marker.colOff),
        EMU_to_pixels(marker.rowOff),
    )


def _extract_images(workbook, output_dir: Path) -> dict[str, list[str]]:
    media_dir = output_dir / "media"
    extracted: dict[str, list[str]] = {}
    image_number = 0

    for worksheet in workbook.worksheets:
        references: list[str] = []
        for image in worksheet._images:
            source_format = (image.format or "png").lower()
            if f".{source_format}" in {".emf", ".wmf", ".svg"}:
                # Preserve these directly from the XLSX package below; Pillow
                # and OpenPyXL cannot reliably decode or re-encode them.
                continue
            image_number += 1
            extension = re.sub(r"[^a-z0-9]", "", source_format)
            extension = extension or "png"
            if extension not in {"gif", "jpeg", "png"}:
                # Image._data() transcodes every other Pillow format to PNG.
                extension = "png"
            filename = f"image-{image_number}.{extension}"
            try:
                payload = image._data()
                media_dir.mkdir(parents=True, exist_ok=True)
                (media_dir / filename).write_bytes(payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "could not extract image %s from sheet %s: %s",
                    image_number,
                    worksheet.title,
                    exc,
                )
                continue

            cell_range = _image_cell_range(worksheet, image)
            alt = f"{worksheet.title} シートのセル {cell_range} を覆う画像"
            references.append(
                f"**画像位置:** {worksheet.title} シート、"
                f"セル {cell_range}\n\n"
                f"![{alt}](media/{filename})"
            )
        if references:
            extracted[worksheet.title] = references
    return extracted


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _relationship_targets(
    archive: zipfile.ZipFile,
    part_name: str,
) -> dict[str, str]:
    part_dir, filename = posixpath.split(part_name)
    relationships_name = posixpath.join(part_dir, "_rels", f"{filename}.rels")
    if relationships_name not in archive.namelist():
        return {}

    relationships = ElementTree.fromstring(archive.read(relationships_name))
    targets: dict[str, str] = {}
    for relationship in relationships:
        if relationship.attrib.get("TargetMode") == "External":
            continue
        target = relationship.attrib.get("Target")
        relationship_id = relationship.attrib.get("Id")
        if not target or not relationship_id:
            continue
        if target.startswith("/"):
            resolved = target.lstrip("/")
        else:
            resolved = posixpath.normpath(posixpath.join(part_dir, target))
        targets[relationship_id] = resolved
    return targets


def _marker_values(node) -> tuple[int, int, int, int] | None:
    values: dict[str, int] = {}
    for child in node:
        if child.text is None:
            continue
        try:
            values[_local_name(child.tag)] = int(child.text)
        except ValueError:
            continue
    if "row" not in values or "col" not in values:
        return None
    return (
        values["row"] + 1,
        values["col"] + 1,
        EMU_to_pixels(values.get("rowOff", 0)),
        EMU_to_pixels(values.get("colOff", 0)),
    )


def _drawing_anchor_range(anchor, worksheet) -> str:
    start_node = next(
        (child for child in anchor if _local_name(child.tag) == "from"),
        None,
    )
    if start_node is None or (start := _marker_values(start_node)) is None:
        return "不明"
    start_row, start_column, row_offset, column_offset = start

    end_node = next(
        (child for child in anchor if _local_name(child.tag) == "to"),
        None,
    )
    if end_node is not None and (end := _marker_values(end_node)) is not None:
        end_row, end_column, _, _ = end
        start_cell = f"{get_column_letter(start_column)}{start_row}"
        end_cell = f"{get_column_letter(end_column)}{end_row}"
        return start_cell if start_cell == end_cell else f"{start_cell}:{end_cell}"

    extent = next(
        (child for child in anchor if _local_name(child.tag) == "ext"),
        None,
    )
    if extent is None:
        return f"{get_column_letter(start_column)}{start_row}"
    return _covered_cell_range(
        worksheet,
        start_row,
        start_column,
        EMU_to_pixels(int(extent.attrib.get("cx", "0"))),
        EMU_to_pixels(int(extent.attrib.get("cy", "0"))),
        column_offset,
        row_offset,
    )


def _vector_image_locations(
    archive: zipfile.ZipFile,
    workbook,
) -> dict[str, list[tuple[str, str]]]:
    """Map vector-media package members to their sheet and covered cells."""
    workbook_rels = _relationship_targets(archive, _WORKBOOK_XML)
    workbook_xml = ElementTree.fromstring(archive.read(_WORKBOOK_XML))
    worksheets = {worksheet.title: worksheet for worksheet in workbook.worksheets}
    locations: dict[str, list[tuple[str, str]]] = {}

    for sheet in workbook_xml.iter():
        if _local_name(sheet.tag) != "sheet":
            continue
        sheet_name = sheet.attrib.get("name", "不明なシート")
        worksheet = worksheets.get(sheet_name)
        sheet_part = workbook_rels.get(sheet.attrib.get(_RELATIONSHIP_ID, ""))
        if (
            worksheet is None
            or sheet_part is None
            or sheet_part not in archive.namelist()
        ):
            continue

        sheet_rels = _relationship_targets(archive, sheet_part)
        sheet_xml = ElementTree.fromstring(archive.read(sheet_part))
        for drawing in sheet_xml.iter():
            if _local_name(drawing.tag) != "drawing":
                continue
            drawing_part = sheet_rels.get(drawing.attrib.get(_RELATIONSHIP_ID, ""))
            if drawing_part is None or drawing_part not in archive.namelist():
                continue

            drawing_rels = _relationship_targets(archive, drawing_part)
            drawing_xml = ElementTree.fromstring(archive.read(drawing_part))
            for anchor in drawing_xml.iter():
                if _local_name(anchor.tag) not in {
                    "oneCellAnchor",
                    "twoCellAnchor",
                    "absoluteAnchor",
                }:
                    continue
                cell_range = _drawing_anchor_range(anchor, worksheet)
                for node in anchor.iter():
                    if _local_name(node.tag) != "blip":
                        continue
                    media_part = drawing_rels.get(node.attrib.get(_EMBED_ID, ""))
                    if (
                        media_part is not None
                        and Path(media_part).suffix.lower() in _VECTOR_SUFFIXES
                    ):
                        locations.setdefault(media_part, []).append(
                            (sheet_name, cell_range)
                        )
    return locations


def _extract_package_vector_images(
    xlsx_path: Path,
    output_dir: Path,
    workbook,
) -> dict[str, list[str]]:
    """Extract dropped vector media with its worksheet cell placement."""
    media_dir = output_dir / "media"
    references: dict[str, list[str]] = {}

    with zipfile.ZipFile(xlsx_path) as archive:
        members = sorted(
            name
            for name in archive.namelist()
            if name.startswith("xl/media/")
            and Path(name).suffix.lower() in _VECTOR_SUFFIXES
        )
        locations = _vector_image_locations(archive, workbook)
        for number, member in enumerate(members, start=1):
            suffix = Path(member).suffix.lower()
            filename = f"vector-{number}{suffix}"
            media_dir.mkdir(parents=True, exist_ok=True)
            (media_dir / filename).write_bytes(archive.read(member))
            placements = locations.get(member) or [("", "不明")]
            for sheet_name, cell_range in placements:
                alt = (
                    f"{sheet_name} シートのセル {cell_range} を覆う"
                    "ベクター画像"
                    if sheet_name
                    else f"位置不明の Excel ベクター画像 {number}"
                )
                location = (
                    f"{sheet_name} シート、セル {cell_range}"
                    if sheet_name
                    else "不明"
                )
                references.setdefault(sheet_name, []).append(
                    f"**画像位置:** {location}\n\n![{alt}](media/{filename})"
                )
    return references


def run_openpyxl(
    xlsx_path: str,
    output_dir: str,
    document_title: str | None = None,
) -> str:
    """Convert workbook cells to compact HTML tables and extract its images."""
    source = Path(xlsx_path).resolve()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    markdown_path = destination / "document.md"

    try:
        formulas = load_workbook(source, data_only=False, keep_links=False)
        cached_values = load_workbook(source, data_only=True, keep_links=False)
    except Exception as exc:
        raise OpenpyxlError(f"OpenPyXL could not read the workbook: {exc}") from exc

    try:
        images = _extract_images(formulas, destination)
        vector_images = _extract_package_vector_images(
            source,
            destination,
            formulas,
        )
        title = document_title or "Excel ワークブック"
        sections = [f"# {_html_text(title)}"]
        has_formulas = False
        for worksheet in formulas.worksheets:
            cached_worksheet = cached_values[worksheet.title]
            table, sheet_has_formulas = _render_worksheet(
                worksheet,
                cached_worksheet,
            )
            has_formulas = has_formulas or sheet_has_formulas
            sections.extend(
                ["", f"## シート: {_html_text(worksheet.title)}", "", table]
            )
            references = [
                *images.get(worksheet.title, []),
                *vector_images.get(worksheet.title, []),
            ]
            if references:
                sections.extend(["", "### 画像", "", *references])

        unplaced_vectors = vector_images.get("")
        if unplaced_vectors:
            sections.extend(
                ["", "## 位置不明のベクター画像", "", *unplaced_vectors]
            )

        if has_formulas:
            sections[1:1] = [
                "",
                (
                    "> 数式セルには数式とワークブックのキャッシュ値の両方を記載しています。"
                    "利用可能な環境では LibreOffice により抽出前に再計算されます。"
                ),
            ]
        markdown_path.write_text("\n".join(sections) + "\n", encoding="utf-8")
    except OpenpyxlError:
        raise
    except Exception as exc:
        raise OpenpyxlError(f"could not render the workbook: {exc}") from exc
    finally:
        formulas.close()
        cached_values.close()

    return str(markdown_path)


class XlsxParser(BaseParser):
    name = "xlsx"

    @classmethod
    def detect(cls, data: bytes) -> bool:
        if not data.startswith(b"PK"):
            return False
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile):
            return False
        return _CONTENT_TYPES in names and _WORKBOOK_XML in names

    async def _extract(
        self,
        data: bytes,
        image_dir: str,
        options: ParseOptions,
        workers: Workers,
    ) -> str:
        work_dir = Path(image_dir)
        upload_name = Path(options.filename or "").name or "document.xlsx"
        if Path(upload_name).suffix.lower() != ".xlsx":
            upload_name = f"{Path(upload_name).stem}.xlsx"
        xlsx_path = work_dir / upload_name
        output_dir = work_dir / "openpyxl-output"
        xlsx_path.write_bytes(data)
        document_title = (
            Path(upload_name).stem
            if Path(upload_name).stem not in {"document", ""}
            else "Excel ワークブック"
        )

        workbook_path = xlsx_path
        recalculate_mode = os.getenv("XLSX_RECALCULATE_FORMULAS", "auto").lower()
        if recalculate_mode not in {"0", "false", "no", "off"}:
            command = find_libreoffice_command()
            if command is None:
                if recalculate_mode in {"1", "true", "yes", "required"}:
                    raise OpenpyxlError(
                        "formula recalculation was required, but LibreOffice "
                        "was not found"
                    )
            else:
                try:
                    workbook_path = Path(
                        await workers.run_external(
                            recalculate_with_libreoffice,
                            str(xlsx_path),
                            str(work_dir / "recalculated"),
                            command,
                        )
                    )
                except LibreOfficeError as exc:
                    if recalculate_mode in {"1", "true", "yes", "required"}:
                        raise
                    logger.warning(
                        "LibreOffice recalculation failed; using saved values: %s",
                        exc,
                    )

        markdown_path = Path(
            await workers.run_external(
                run_openpyxl,
                str(workbook_path),
                str(output_dir),
                document_title,
            )
        )
        markdown = markdown_path.read_text(encoding="utf-8")
        markdown = await convert_document_vector_images(
            markdown, output_dir / "media", workers
        )

        client = (
            LLMClient(
                base_url=options.llm_base_url,
                api_key=options.llm_api_key,
                model=options.llm_model,
            )
            if options.describe_images
            else None
        )
        try:
            return await embed_markdown_images(
                markdown,
                markdown_path,
                output_dir,
                workers,
                client.describe_image if client is not None else None,
            )
        finally:
            if client is not None:
                await client.close()
