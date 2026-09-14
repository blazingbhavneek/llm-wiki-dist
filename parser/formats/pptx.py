"""PPTX parsing through python-pptx with parallel image descriptions.

Each slide becomes a section containing its text, tables, spatially annotated
pictures, speaker notes, and a rendered overview image. Extracted pictures and
slide overviews flow through the shared image-description pipeline.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import subprocess
import tempfile
import zipfile
from html import escape
from pathlib import Path

import pypdfium2 as pdfium
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.exc import PackageNotFoundError

from client.llm import LLMClient
from formats.base import BaseParser, ParseOptions
from utils.image_unit import strip_image_media
from utils.markdown_images import embed_markdown_images
from utils.vector_images import convert_document_vector_images, find_libreoffice_command
from workers import Workers

logger = logging.getLogger("doc-parser.pptx")

_CONTENT_TYPES = "[Content_Types].xml"
_PRESENTATION_XML = "ppt/presentation.xml"
_SLIDE_OVERVIEW_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<target>media/slide-(?P<slide>\d+)\.png)\)"
)


class PptxError(RuntimeError):
    """python-pptx could not produce a usable Markdown document."""


class SlideRenderError(RuntimeError):
    """LibreOffice or PDFium could not render the presentation slides."""


def _cell_text(value: str) -> str:
    text = escape(value or "", quote=False)
    return text.replace("|", "&#124;").replace("\r\n", "<br>").replace("\n", "<br>")


def _render_table(table) -> list[str]:
    rows = list(table.rows)
    if not rows:
        return []
    width = max(len(list(row.cells)) for row in rows)

    def cells(row) -> list[str]:
        values = [_cell_text(cell.text) for cell in row.cells]
        return values + [""] * (width - len(values))

    header, *body = rows
    lines = [
        "| " + " | ".join(cells(header)) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    lines.extend("| " + " | ".join(cells(row)) + " |" for row in body)
    return lines


def _iter_shapes(shapes):
    """Yield shapes in reading order, flattening group shapes."""
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_shapes(shape.shapes)
        else:
            yield shape


def _text_frame_lines(text_frame) -> list[str]:
    lines: list[str] = []
    for para in text_frame.paragraphs:
        text = "".join(run.text for run in para.runs) or para.text
        text = text.strip()
        if not text:
            continue
        level = getattr(para, "level", 0) or 0
        if level:
            lines.append(f"{'  ' * (level - 1)}- {text}")
        else:
            lines.append(text)
    return lines


def _slide_context(markdown: str, slide_number: int, token: str) -> str:
    match = re.search(
        rf"(?ms)^## スライド {slide_number}\s*$.*?"
        rf"(?=^## スライド \d+\s*$|\Z)",
        markdown,
    )
    section = match.group(0) if match else markdown
    return strip_image_media(section.replace(token, "")).replace(
        "### スライド全体",
        "",
    ).strip()


def _shape_placement(shape, slide_width: int, slide_height: int) -> str:
    """Describe a shape's bounding box as percentages of its slide."""
    left = 100 * shape.left / slide_width
    top = 100 * shape.top / slide_height
    width = 100 * shape.width / slide_width
    height = 100 * shape.height / slide_height
    return (
        f"左 {left:.1f}%、上 {top:.1f}%、"
        f"幅 {width:.1f}%、高さ {height:.1f}%"
    )


def render_slides_with_libreoffice(
    pptx_path: str,
    output_dir: str,
    command: list[str],
) -> list[str]:
    """Render every slide through LibreOffice and PDFium into a PNG."""
    source = Path(pptx_path).resolve()
    destination = Path(output_dir).resolve()
    media_dir = destination / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="slide-render-") as directory:
        render_dir = Path(directory)
        profile = render_dir / "libreoffice-profile"
        profile.mkdir()
        args = [
            *command,
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile.as_uri()}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(render_dir),
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
            raise SlideRenderError(
                f"LibreOffice executable was not found: {command[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SlideRenderError(
                f"LibreOffice slide rendering timed out after {timeout_s:g} seconds"
            ) from exc

        pdf_path = render_dir / f"{source.stem}.pdf"
        if completed.returncode or not pdf_path.is_file():
            details = (completed.stderr or completed.stdout or "no output").strip()[
                -4000:
            ]
            raise SlideRenderError(
                f"LibreOffice slide rendering failed with status "
                f"{completed.returncode}: {details}"
            )

        filenames: list[str] = []
        target_width = max(1, int(os.getenv("PPTX_SLIDE_RENDER_WIDTH", "1600")))
        try:
            document = pdfium.PdfDocument(str(pdf_path))
            try:
                expected_pages = len(Presentation(str(source)).slides)
                if len(document) != expected_pages:
                    raise SlideRenderError(
                        "LibreOffice rendered "
                        f"{len(document)} of {expected_pages} slides"
                    )
                for index in range(len(document)):
                    page = document[index]
                    try:
                        page_width, _ = page.get_size()
                        bitmap = page.render(scale=target_width / page_width)
                        try:
                            rendered = bitmap.to_pil()
                            filename = f"slide-{index + 1}.png"
                            rendered.save(media_dir / filename, format="PNG")
                            rendered.close()
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
                    filenames.append(filename)
            finally:
                document.close()
        except SlideRenderError:
            raise
        except Exception as exc:  # noqa: BLE001 - PDFium exposes several errors
            raise SlideRenderError(
                f"PDFium could not render the slides: {exc}"
            ) from exc

    return filenames


def run_pptx(
    pptx_path: str,
    output_dir: str,
    slide_images: list[str] | None = None,
) -> str:
    """Convert a PPTX to Markdown and extract pictures into ``output_dir``."""
    source = Path(pptx_path).resolve()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    media_dir = destination / "media"
    markdown_path = destination / "document.md"

    try:
        presentation = Presentation(str(source))
    except (PackageNotFoundError, KeyError, ValueError) as exc:
        raise PptxError(f"python-pptx could not read the presentation: {exc}") from exc

    title = (
        source.stem
        if source.stem not in {"document", ""}
        else "プレゼンテーション"
    )
    sections = [f"# {_cell_text(title)}"]
    image_number = 0

    for index, slide in enumerate(presentation.slides, start=1):
        sections.extend(["", f"## スライド {index}"])
        title_shape = slide.shapes.title

        for shape in _iter_shapes(slide.shapes):
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                try:
                    image = shape.image
                    payload = image.blob
                    extension = (image.ext or "png").lstrip(".").lower() or "png"
                except Exception as exc:  # noqa: BLE001 - pptx raises bare Exceptions
                    logger.warning(
                        "could not extract picture on slide %s: %s",
                        index,
                        exc,
                    )
                    continue
                image_number += 1
                media_dir.mkdir(parents=True, exist_ok=True)
                filename = f"image-{image_number}.{extension}"
                (media_dir / filename).write_bytes(payload)
                alt = (shape.name or f"スライド {index} の画像").strip()
                placement = _shape_placement(
                    shape,
                    presentation.slide_width,
                    presentation.slide_height,
                )
                contextual_alt = f"{alt}（{placement}）"
                sections.extend(
                    [
                        "",
                        f"**画像位置:** {placement}",
                        "",
                        f"![{contextual_alt}](media/{filename})",
                    ]
                )
                continue

            if shape.has_table:
                table_lines = _render_table(shape.table)
                if table_lines:
                    sections.extend(["", *table_lines])
                continue

            if shape.has_text_frame:
                lines = _text_frame_lines(shape.text_frame)
                if not lines:
                    continue
                if title_shape is not None and shape == title_shape:
                    sections.extend(["", f"### {' '.join(lines)}"])
                else:
                    sections.extend(["", *lines])

        if slide.has_notes_slide:
            note = slide.notes_slide.notes_text_frame.text.strip()
            if note:
                quoted = "\n".join(f"> {line}" for line in note.splitlines())
                sections.extend(["", "> **スピーカーノート:**", ">", quoted])

        if slide_images and index <= len(slide_images):
            sections.extend(
                [
                    "",
                    "### スライド全体",
                    "",
                    f"![スライド {index} 全体のレンダリング]"
                    f"(media/{slide_images[index - 1]})",
                ]
            )
        sections.extend(["", "---"])

    markdown_path.write_text("\n".join(sections) + "\n", encoding="utf-8")
    return str(markdown_path)


class PptxParser(BaseParser):
    name = "pptx"

    @classmethod
    def detect(cls, data: bytes) -> bool:
        if not data.startswith(b"PK"):
            return False
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile):
            return False
        return _CONTENT_TYPES in names and _PRESENTATION_XML in names

    async def _extract(
        self,
        data: bytes,
        image_dir: str,
        options: ParseOptions,
        workers: Workers,
    ) -> str:
        work_dir = Path(image_dir)
        upload_name = Path(options.filename or "").name or "document.pptx"
        if Path(upload_name).suffix.lower() != ".pptx":
            upload_name = f"{Path(upload_name).stem}.pptx"
        pptx_path = work_dir / upload_name
        output_dir = work_dir / "pptx-output"
        pptx_path.write_bytes(data)

        slide_images: list[str] = []
        render_mode = os.getenv("PPTX_RENDER_SLIDES", "auto").lower()
        if render_mode not in {"0", "false", "no", "off"}:
            command = find_libreoffice_command()
            if command is None:
                if render_mode in {"1", "true", "yes", "required"}:
                    raise SlideRenderError(
                        "slide rendering was required, but LibreOffice was not found"
                    )
                logger.warning(
                    "LibreOffice not found; presentation overview images were skipped"
                )
            else:
                try:
                    slide_images = await workers.run_external(
                        render_slides_with_libreoffice,
                        str(pptx_path),
                        str(output_dir),
                        command,
                    )
                except SlideRenderError as exc:
                    if render_mode in {"1", "true", "yes", "required"}:
                        raise
                    logger.warning(
                        "slide rendering failed; overview images were skipped: %s",
                        exc,
                    )

        markdown_path = Path(
            await workers.run_external(
                run_pptx,
                str(pptx_path),
                str(output_dir),
                slide_images,
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
            overview_refs: list[tuple[str, int, str]] = []

            def mask_overview(match: re.Match[str]) -> str:
                token = f"<!-- pptx-overview-{len(overview_refs) + 1} -->"
                overview_refs.append(
                    (token, int(match.group("slide")), match.group(0))
                )
                return token

            # First describe only the individual pictures. Their completed
            # descriptions become part of the context for the slide overview.
            markdown = await embed_markdown_images(
                _SLIDE_OVERVIEW_RE.sub(mask_overview, markdown),
                markdown_path,
                output_dir,
                workers,
                client.describe_image if client is not None else None,
            )

            async def embed_overview(
                token: str,
                slide_number: int,
                reference: str,
            ) -> tuple[str, str]:
                context = _slide_context(markdown, slide_number, token)

                async def describe(data_url: str, _alt_text: str) -> str:
                    assert client is not None
                    return await client.describe_slide(data_url, context)

                embedded = await embed_markdown_images(
                    reference,
                    markdown_path,
                    output_dir,
                    workers,
                    describe if client is not None else None,
                )
                return token, embedded

            if overview_refs:
                overviews = await asyncio.gather(
                    *(embed_overview(*overview) for overview in overview_refs)
                )
                for token, overview in overviews:
                    markdown = markdown.replace(token, overview, 1)
            return markdown
        finally:
            if client is not None:
                await client.close()
