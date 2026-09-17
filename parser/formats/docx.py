"""DOCX parsing through Pandoc with parallel image descriptions."""

from __future__ import annotations

import io
import os
import shlex
import subprocess
import zipfile
from pathlib import Path

from client.llm import LLMClient
from formats.base import BaseParser, ExtractedDocument, ParseOptions, ParseProfile
from utils.markdown_images import embed_markdown_images
from utils.vector_images import convert_document_vector_images
from workers import Workers

_DOCX_CONTENT_TYPES = "[Content_Types].xml"
_DOCX_DOCUMENT = "word/document.xml"


class PandocError(RuntimeError):
    """Pandoc did not produce a usable Markdown document."""


def run_pandoc(docx_path: str, output_dir: str) -> str:
    """Convert one DOCX to GFM and extract its media into ``output_dir``."""
    source = Path(docx_path).resolve()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    markdown_path = destination / "document.md"

    command = shlex.split(os.getenv("PANDOC_COMMAND", "pandoc"))
    if not command:
        raise PandocError("PANDOC_COMMAND is empty")

    args = [
        *command,
        str(source),
        "--from=docx",
        "--to=gfm",
        "--wrap=none",
        "--extract-media=media",
        f"--output={markdown_path.name}",
    ]
    extra_args = os.getenv("PANDOC_EXTRA_ARGS", "").strip()
    if extra_args:
        args.extend(shlex.split(extra_args))

    timeout_s = float(os.getenv("PANDOC_TIMEOUT_SECONDS", "300"))
    try:
        completed = subprocess.run(
            args,
            cwd=destination,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PandocError(f"Pandoc executable was not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PandocError(f"Pandoc timed out after {timeout_s:g} seconds") from exc

    if completed.returncode:
        details = (completed.stderr or completed.stdout or "no output").strip()[-4000:]
        raise PandocError(
            f"Pandoc exited with status {completed.returncode}: {details}"
        )
    if not markdown_path.is_file():
        raise PandocError("Pandoc completed without producing a Markdown file")

    return str(markdown_path)


class DocxParser(BaseParser):
    name = "docx"

    @classmethod
    def detect(cls, data: bytes) -> bool:
        if not data.startswith(b"PK"):
            return False
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile):
            return False
        return _DOCX_CONTENT_TYPES in names and _DOCX_DOCUMENT in names

    async def _extract(
        self,
        data: bytes,
        image_dir: str,
        options: ParseOptions,
        workers: Workers,
    ) -> ExtractedDocument:
        work_dir = Path(image_dir)
        docx_path = work_dir / "document.docx"
        output_dir = work_dir / "pandoc-output"
        docx_path.write_bytes(data)

        markdown_path = Path(
            await workers.run_external(
                run_pandoc,
                str(docx_path),
                str(output_dir),
            )
        )
        markdown = markdown_path.read_text(encoding="utf-8")
        # Word stores many images as EMF/WMF vectors, which Pandoc extracts
        # verbatim and Pillow cannot decode; convert them to PNG first.
        markdown = await convert_document_vector_images(
            markdown, output_dir / "media", workers
        )

        # The generic profile returns raw Markdown with relative references and
        # never creates an LLM client; BaseParser.parse embeds data URLs. DOCX
        # has no page structure, so pages stays empty for both profiles.
        if options.profile == ParseProfile.GENERIC:
            return ExtractedDocument(
                markdown=markdown,
                pages=[],
                markdown_path=markdown_path,
                asset_root=output_dir,
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
            embedded = await embed_markdown_images(
                markdown,
                markdown_path,
                output_dir,
                workers,
                client.describe_image if client is not None else None,
            )
        finally:
            if client is not None:
                await client.close()
        return ExtractedDocument(markdown=embedded, pages=[])
