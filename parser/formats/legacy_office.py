"""Word/Excel 97-2003 binaries: convert with LibreOffice, then parse as OOXML."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import olefile

from formats.base import BaseParser, ExtractedDocument, ParseOptions
from formats.docx import DocxParser
from formats.xlsx import XlsxParser
from utils.vector_images import find_libreoffice_command

if TYPE_CHECKING:
    from workers import Workers

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FORMATS = {
    "worddocument": ("doc", "docx", DocxParser),
    "workbook": ("xls", "xlsx", XlsxParser),
    "book": ("xls", "xlsx", XlsxParser),
}


class LegacyOfficeError(RuntimeError):
    """A .doc/.xls file could not be converted."""


def legacy_stream(data: bytes) -> str | None:
    """Return the identifying OLE stream of a Word/Excel 97-2003 file, else None."""

    if not data.startswith(_OLE_MAGIC):
        return None
    try:
        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            names = {"/".join(entry).lower() for entry in ole.listdir(streams=True, storages=False)}
    except Exception:
        return None
    if "encryptedpackage" in names:
        return None
    return next((stream for stream in _FORMATS if stream in names), None)


def convert_legacy_office(source: str, target_suffix: str, command: list[str]) -> str:
    """Convert one file with an isolated LibreOffice profile and return the new path."""

    source_path = Path(source)
    out_dir = source_path.parent / "converted"
    out_dir.mkdir(exist_ok=True)
    timeout_s = float(os.getenv("LIBREOFFICE_TIMEOUT_SECONDS", "300"))
    with tempfile.TemporaryDirectory(prefix="legacy-office-profile-") as profile:
        args = [
            *command, "--headless", "--nologo", "--nodefault", "--nolockcheck", "--nofirststartwizard",
            f"-env:UserInstallation={Path(profile).as_uri()}",
            "--convert-to", target_suffix, "--outdir", str(out_dir), str(source_path),
        ]
        try:
            completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s, check=False)
        except FileNotFoundError as exc:
            raise LegacyOfficeError(f"LibreOffice executable was not found: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise LegacyOfficeError(f"LibreOffice conversion timed out after {timeout_s:g} seconds") from exc
    target = out_dir / f"{source_path.stem}.{target_suffix}"
    if completed.returncode or not target.is_file() or target.stat().st_size == 0:
        details = (completed.stderr or completed.stdout or "no output").strip()[-2000:]
        raise LegacyOfficeError(f"LibreOffice could not convert {source_path.name} to {target_suffix}: {details}")
    return str(target)


class LegacyOfficeParser(BaseParser):
    name = "legacy-office"

    @classmethod
    def detect(cls, data: bytes) -> bool:
        return legacy_stream(data) is not None

    async def _extract(
        self,
        data: bytes,
        image_dir: str,
        options: ParseOptions,
        workers: Workers,
    ) -> ExtractedDocument:
        stream = legacy_stream(data)
        if stream is None:
            raise LegacyOfficeError("not a Word or Excel 97-2003 file")
        old_suffix, new_suffix, parser_cls = _FORMATS[stream]
        command = find_libreoffice_command()
        if command is None:
            raise LegacyOfficeError("LibreOffice is required to parse .doc and .xls files")
        stem = Path(options.filename or "document").stem or "document"
        work = Path(image_dir) / "legacy-office"
        work.mkdir(parents=True, exist_ok=True)
        source = work / f"{stem}.{old_suffix}"
        source.write_bytes(data)
        converted = await workers.run_external(convert_legacy_office, str(source), new_suffix, command)
        delegate_dir = Path(image_dir) / "converted-parse"
        delegate_dir.mkdir(parents=True, exist_ok=True)
        return await parser_cls()._extract(
            Path(converted).read_bytes(),
            str(delegate_dir),
            replace(options, filename=f"{stem}.{new_suffix}"),
            workers,
        )
