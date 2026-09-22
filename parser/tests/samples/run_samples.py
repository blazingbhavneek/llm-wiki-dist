"""Ad-hoc end-to-end runner for tests/samples/.

Drives the real parser pipeline (detect -> parser.parse -> Workers) for every
file in tests/samples/, exactly the way server.py does, and writes each
resulting Markdown document to scratchpad/outputs/.

Usage:
    .venv/bin/python run_samples.py [options]

--describe          call the real vision LLM from .env for image descriptions
--dummy-describe    substitute a fake describer (no network) that returns a
                    synthetic per-image description -- exercises the full
                    embed+describe+assemble path without the endpoint
--only substr       only run sample files whose name contains substr
--llm-timeout N     override LLM_TIMEOUT_SECONDS
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path("/mnt/common/Code/doc-parser")
sys.path.insert(0, str(ROOT))

SAMPLES = ROOT / "tests" / "samples"
OUT = Path(__file__).resolve().parent / "_outputs"
LO_SHIM = Path.home() / ".local" / "lib" / "lo-shim"


class _DummyLLMClient:
    """Stand-in for client.llm.LLMClient: no network, synthetic descriptions."""

    instances: list[dict] = []

    def __init__(self, **kwargs) -> None:
        _DummyLLMClient.instances.append(kwargs)
        self._n = 0

    async def describe_image(self, data_url: str, alt_text: str = "") -> str:
        await asyncio.sleep(0)
        self._n += 1
        kb = len(data_url) * 3 // 4 // 1024
        tag = alt_text.strip() or "unlabelled"
        return (
            f"[SYNTHETIC DESCRIPTION] Image #{self._n}: '{tag}'. "
            f"~{kb} KB decoded. This placeholder stands in for a real vision-model "
            f"caption so the embedding + assembly path can be verified offline."
        )

    async def describe_slide(self, data_url: str, context: str) -> str:
        await asyncio.sleep(0)
        self._n += 1
        return (
            "[SYNTHETIC SLIDE SYNTHESIS] Layout and relationships derived from "
            f"the slide overview and {len(context)} context characters."
        )

    async def close(self) -> None:
        return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--describe", action="store_true")
    ap.add_argument("--dummy-describe", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--llm-timeout", default="")
    args = ap.parse_args()

    if args.llm_timeout:
        os.environ["LLM_TIMEOUT_SECONDS"] = args.llm_timeout
    # no-sudo boost-1.91->1.92 shim so headless LibreOffice can start
    if LO_SHIM.is_dir():
        os.environ["LD_LIBRARY_PATH"] = (
            f"{LO_SHIM}{os.pathsep}{os.environ.get('LD_LIBRARY_PATH', '')}"
        )

    from formats import ParseOptions, UnsupportedFormatError, detect
    from workers import Workers

    describe = args.describe or args.dummy_describe
    if args.dummy_describe:
        import formats.docx as fdocx
        import formats.pdf as fpdf
        import formats.xlsx as fxlsx

        for mod in (fdocx, fpdf, fxlsx):
            mod.LLMClient = _DummyLLMClient

    OUT.mkdir(parents=True, exist_ok=True)
    workers = Workers()

    files = sorted(
        p
        for p in SAMPLES.iterdir()
        if p.is_file() and p.suffix.lower() in {".pdf", ".docx", ".xlsx", ".csv", ".pptx"}
    )
    if args.only:
        files = [p for p in files if args.only in p.name]

    rows: list[tuple[str, str]] = []
    mode = "dummy" if args.dummy_describe else ("real-llm" if args.describe else "off")
    print(f"\n{'='*100}\n{len(files)} sample(s)   describe_images={mode}\n{'='*100}")

    for path in files:
        data = path.read_bytes()
        label = path.name
        t0 = time.perf_counter()
        try:
            parser_cls = detect(data)
        except UnsupportedFormatError as exc:
            print(f"\n### {label}\n  -> UNSUPPORTED ({exc})  [expected for csv/pptx]")
            rows.append((label, f"unsupported (HTTP 415): {exc}"))
            continue

        print(f"\n### {label}\n  parser={parser_cls.name} stream={parser_cls.stream_response}")
        options = ParseOptions(images=True, describe_images=describe)
        try:
            result = await parser_cls().parse(data, options, workers)
        except Exception as exc:  # noqa: BLE001
            dt = time.perf_counter() - t0
            print(f"  -> FAILED after {dt:.1f}s: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            rows.append((label, f"FAILED: {type(exc).__name__}: {exc}"))
            continue

        dt = time.perf_counter() - t0
        md = result.markdown
        (OUT / f"{path.stem}.md").write_text(md, encoding="utf-8")
        n_units = md.count("<image-unit>")
        n_desc = md.count("[SYNTHETIC DESCRIPTION]")
        n_imgtag = len(__import__("re").findall(r"<img\b", md))
        print(
            f"  -> OK [{dt:.1f}s]  images(counted)={result.image_count}  "
            f"<image-unit>={n_units}  synthetic_desc={n_desc}  bare_<img>_tags={n_imgtag}  "
            f"md_chars={len(md):,}"
        )
        print(f"  preview: {' '.join(md[:220].split())}")
        rows.append(
            (
                label,
                f"OK {dt:.1f}s  units={n_units} desc={n_desc} bare_img={n_imgtag} "
                f"chars={len(md):,}",
            )
        )

    workers.shutdown()
    print(f"\n{'='*100}\nSUMMARY\n{'='*100}")
    for name, status in rows:
        print(f"  {name:<40} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
