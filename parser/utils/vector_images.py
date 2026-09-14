"""Convert Office vector images (EMF/WMF) to PNG before embedding.

Word, Excel, and PowerPoint documents commonly store images as EMF or WMF
vector graphics. Pillow cannot decode EMF at all and decodes WMF only with
the optional ``libwmf`` library, so those images would be embedded raw and
skipped by the vision pipeline. When LibreOffice is installed, headless
Draw converts all discovered vector images to PNG in one batched call, and
the Markdown references are rewritten to the new raster files.

Mode is controlled by ``VECTOR_IMAGE_CONVERSION``:

* ``auto`` (default): convert when LibreOffice is available, otherwise keep
  the original vector images and log a warning;
* ``false``: never convert;
* ``required``: fail the request when conversion cannot run.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from workers import Workers

logger = logging.getLogger("doc-parser.vectors")

VECTOR_IMAGE_SUFFIXES = frozenset({".emf", ".wmf"})


class VectorConversionError(RuntimeError):
    """LibreOffice could not convert vector images to PNG."""


def find_libreoffice_command() -> list[str] | None:
    """Return the configured or locally available LibreOffice command."""
    configured = os.getenv("LIBREOFFICE_COMMAND", "").strip()
    if configured:
        command = shlex.split(configured)
        return command or None

    for executable in ("libreoffice", "soffice"):
        if path := shutil.which(executable):
            return [path]
    return None


def find_vector_images(root: Path) -> list[Path]:
    """Return every EMF/WMF file below ``root`` in a stable order."""
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in VECTOR_IMAGE_SUFFIXES
    )


def _unique_png_name(source: Path, taken: set[str]) -> str:
    """Pick a collision-free .png name for ``source`` within a directory."""
    candidate = f"{source.stem}.png"
    if candidate.lower() in taken:
        candidate = f"{source.stem}{source.suffix.lower()}.png"
    taken.add(candidate.lower())
    return candidate


def convert_vector_images(root: str, command: list[str]) -> list[tuple[str, str]]:
    """Convert every vector image below ``root`` to PNG beside the original.

    Runs blocking LibreOffice code and executes in the external worker pool.
    All files are converted in a single headless invocation using staged,
    uniquely named copies so same-stem ``.emf``/``.wmf`` pairs cannot
    overwrite each other's output. Returns ``(original, png)`` path pairs
    for successfully converted files.
    """
    sources = find_vector_images(Path(root))
    if not sources:
        return []

    work_dir = Path(tempfile.mkdtemp(prefix="vector-convert-"))
    try:
        staging = work_dir / "staging"
        out_dir = work_dir / "png"
        profile = work_dir / "profile"
        staging.mkdir()
        out_dir.mkdir()
        profile.mkdir()

        # Staged names are unique so LibreOffice's <stem>.png outputs cannot
        # collide, and we retain the staged-name -> source mapping.
        staged: dict[str, Path] = {}
        for index, source in enumerate(sources):
            staged_name = f"vector-{index:04d}{source.suffix.lower()}"
            shutil.copyfile(source, staging / staged_name)
            staged[staged_name] = source

        args = [
            *command,
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile.as_uri()}",
            "--convert-to",
            "png",
            "--outdir",
            str(out_dir),
            *(str(staging / name) for name in staged),
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
            raise VectorConversionError(
                f"LibreOffice executable was not found: {command[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise VectorConversionError(
                f"LibreOffice vector conversion timed out after {timeout_s:g} seconds"
            ) from exc

        if completed.returncode:
            details = (completed.stderr or completed.stdout or "no output").strip()[
                -4000:
            ]
            raise VectorConversionError(
                f"LibreOffice vector conversion exited with status "
                f"{completed.returncode}: {details}"
            )

        converted: list[tuple[str, str]] = []
        taken_by_dir: dict[Path, set[str]] = {}
        for staged_name, source in staged.items():
            png_output = out_dir / f"{Path(staged_name).stem}.png"
            if not png_output.is_file():
                logger.warning(
                    "LibreOffice produced no PNG for %s; keeping the original",
                    source,
                )
                continue
            taken = taken_by_dir.setdefault(
                source.parent, {path.name for path in source.parent.iterdir()}
            )
            final = source.parent / _unique_png_name(source, taken)
            shutil.move(png_output, final)
            converted.append((str(source), str(final)))
        return converted
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def convert_document_vector_images(
    markdown: str,
    media_root: Path,
    workers: Workers,
) -> str:
    """Convert vector images under a parsed document and rewrite references.

    The LibreOffice batch runs on the external worker pool. On success each
    Markdown reference to ``imageN.emf``/``imageN.wmf`` is rewritten to the
    sibling PNG produced next to the original. Failures are tolerated under
    the default ``auto`` mode: the original vector reference survives and the
    description pipeline keeps its existing skip-and-warn behavior.
    """
    mode = os.getenv("VECTOR_IMAGE_CONVERSION", "auto").lower()
    if mode in {"0", "false", "no", "off"}:
        return markdown

    sources = find_vector_images(media_root)
    if not sources:
        return markdown

    command = find_libreoffice_command()
    if command is None:
        if mode in {"1", "true", "yes", "required"}:
            raise VectorConversionError(
                "vector image conversion was required, but LibreOffice was not found"
            )
        logger.warning(
            "LibreOffice not found; leaving %d EMF/WMF image(s) unconverted",
            len(sources),
        )
        return markdown

    try:
        converted = await workers.run_external(
            convert_vector_images, str(media_root), command
        )
    except VectorConversionError as exc:
        if mode in {"1", "true", "yes", "required"}:
            raise
        logger.warning(
            "LibreOffice vector conversion failed; keeping original images: %s",
            exc,
        )
        return markdown

    if not converted:
        return markdown

    rewritten = markdown
    for original, png in converted:
        original_name = Path(original).name
        png_name = Path(png).name
        rewritten = rewritten.replace(original_name, png_name)
        logger.info("converted vector image %s -> %s", original_name, png_name)
    return rewritten
