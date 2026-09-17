"""Format-specific wiki planning and tabular output."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

KINDS = {"docx", "pptx", "xlsx", "csv", "pdf", "md"}
TABULAR = {"xlsx", "csv"}


def kind_of(document_name: str) -> str:
    stem = PurePosixPath(document_name).stem
    _base, sep, ext = stem.rpartition("_")
    ext = ext.lower()
    return "xlsx" if sep and ext == "xlsm" else ext if sep and ext in KINDS else "md"


def is_tabular(kind: str) -> bool:
    return kind in TABULAR


async def structural_seed_plan(
    lines: list[str], *, kind: str, config: Any, model: Any,
    on_progress=None, stop_check=None,
):
    if kind == "docx":
        from . import docx

        return docx.plan(lines, config=config)
    if kind == "pptx":
        from . import pptx

        return await pptx.plan(
            lines, config=config, model=model, on_progress=on_progress, stop_check=stop_check
        )
    if kind == "pdf":
        from . import pdf

        return pdf.plan(lines, config=config)
    return None
