"""xlsx parser output: one sheet block becomes one table page."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .tabular import write_tables

SHEET_RE = re.compile(r"^## Sheet: (.+?)\s*$")


def split_sheets(lines: list[str]) -> list[tuple[str, str, tuple[int, int]]]:
    starts = [(n, match.group(1)) for n, line in enumerate(lines, 1) if (match := SHEET_RE.match(line))]
    output = []
    for index, (start, name) in enumerate(starts):
        end = starts[index + 1][0] - 1 if index + 1 < len(starts) else len(lines)
        block = "\n".join(lines[start:end])
        table = block[block.find("<table>"):block.rfind("</table>") + len("</table>")] if "<table>" in block else block
        output.append((name, table, (start, end)))
    return output


async def run(source_path: Path, *, run_dir: Path, model: Any, config: Any, on_progress=None, stop_check=None):
    lines = source_path.read_text(encoding="utf-8").splitlines()
    return await write_tables(sheets=split_sheets(lines), run_dir=run_dir, model=model, config=config, on_progress=on_progress, stop_check=stop_check)
