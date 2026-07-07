#!/usr/bin/env python3

import subprocess
import sys
import os
from pathlib import Path

PDF_DIR = Path(os.environ.get("PDF_DIR", "./pdfs"))
OUTPUT_DIR = Path(os.environ.get("MINERU_OUTPUT_DIR", "./mineru"))
GPU_MEMORY_UTILIZATION = "0.05"


def main() -> int:
    if not PDF_DIR.exists():
        print(f"ERROR: PDF directory does not exist: {PDF_DIR}", file=sys.stderr)
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(
        p for p in PDF_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"
    )

    if not pdf_files:
        print(f"No PDF files found in: {PDF_DIR}")
        return 0

    print(f"Found {len(pdf_files)} PDF file(s).")
    print(f"PDF dir: {PDF_DIR}")
    print(f"Output dir: {OUTPUT_DIR}")
    print("-" * 80)

    completed = 0
    skipped = 0
    failed = 0

    for index, pdf_path in enumerate(pdf_files, start=1):
        expected_output_folder = OUTPUT_DIR / pdf_path.stem

        print(f"[{index}/{len(pdf_files)}] PDF: {pdf_path.name}")

        if expected_output_folder.exists():
            print(f"  SKIP: Output folder already exists: {expected_output_folder}")
            skipped += 1
            print("-" * 80)
            continue

        cmd = [
            "mineru",
            "-p",
            str(pdf_path),
            "-o",
            str(OUTPUT_DIR),
            "--gpu-memory-utilization",
            GPU_MEMORY_UTILIZATION,
        ]

        print("  RUN:", " ".join(cmd))

        try:
            result = subprocess.run(cmd)
        except KeyboardInterrupt:
            print("\nInterrupted by user. Exiting.")
            return 130
        except Exception as e:
            print(f"  ERROR: Failed to start command: {e}", file=sys.stderr)
            failed += 1
            print("-" * 80)
            continue

        if result.returncode == 0:
            print(f"  DONE: {pdf_path.name}")
            completed += 1
        else:
            print(
                f"  FAILED: {pdf_path.name} returned exit code {result.returncode}",
                file=sys.stderr,
            )
            failed += 1

        print("-" * 80)

    print("Summary")
    print(f"  Total PDFs: {len(pdf_files)}")
    print(f"  Completed:  {completed}")
    print(f"  Skipped:    {skipped}")
    print(f"  Failed:     {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
