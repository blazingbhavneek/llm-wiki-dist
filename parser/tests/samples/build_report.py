"""Consolidate scratchpad/outputs/*.md into one readable markdown report.

Base64 image payloads are truncated so the report stays legible; the full
untruncated Markdown for each sample lives beside this script in outputs/.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "_outputs"
REPORT = HERE.parent.parent / "tests" / "samples" / "PARSE_OUTPUTS.md"
SAMPLES = Path("/mnt/common/Code/doc-parser/tests/samples")

_B64 = re.compile(r"(data:image/[a-z+]+;base64,)([A-Za-z0-9+/=]{60,})")


def truncate_b64(text: str) -> str:
    return _B64.sub(lambda m: f"{m.group(1)}{m.group(2)[:48]}…<{len(m.group(2))} b64 chars>", text)


def main() -> None:
    parts: list[str] = []
    parts.append("# doc-parser — end-to-end sample parse outputs\n")
    parts.append(
        f"Generated {datetime.now():%Y-%m-%d %H:%M}. "
        "Descriptions are synthetic (`--dummy-describe`, no vision endpoint). "
        "LibreOffice recalculation ran via the boost-1.91→1.92 shim.\n"
    )
    parts.append(f"- Full untruncated outputs: `{OUT_DIR}/`\n- Source documents: `{SAMPLES}/`\n")

    md_files = sorted(OUT_DIR.glob("*.md"))
    parts.append("## Index\n")
    for f in md_files:
        raw = f.read_text(encoding="utf-8")
        units = raw.count("<image-unit>")
        parts.append(f"- **{f.stem}** — {len(raw):,} chars, {units} image-unit(s) — `{f}`")
    parts.append("")

    for f in md_files:
        raw = f.read_text(encoding="utf-8")
        shown = truncate_b64(raw)
        LIMIT = 16000
        clipped = ""
        if len(shown) > LIMIT:
            shown, clipped = shown[:LIMIT], f"\n\n…(clipped; full file {len(raw):,} chars at `{f}`)"
        parts.append(f"\n---\n\n## {f.stem}\n")
        parts.append(f"`{f}` · {len(raw):,} chars · {raw.count('<image-unit>')} image-unit(s)\n")
        parts.append("````markdown")
        parts.append(shown + clipped)
        parts.append("````")

    REPORT.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {REPORT}  ({REPORT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
