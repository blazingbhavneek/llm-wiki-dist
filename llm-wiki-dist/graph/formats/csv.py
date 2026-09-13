"""csv parser output: the complete table becomes one page."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .tabular import write_tables


async def run(source_path: Path, *, run_dir: Path, model: Any, config: Any, on_progress=None, stop_check=None):
    lines = source_path.read_text(encoding="utf-8").splitlines()
    title = next((line.lstrip("# ").strip() for line in lines if line.startswith("# ")), source_path.stem)
    return await write_tables(sheets=[(title, "\n".join(lines), (1, len(lines)))], run_dir=run_dir, model=model, config=config, on_progress=on_progress, stop_check=stop_check)
