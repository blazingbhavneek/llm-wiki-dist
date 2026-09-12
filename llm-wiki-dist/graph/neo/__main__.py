from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import NeoConfig
from .pipeline import run_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a lossless wiki from Markdown")
    parser.add_argument("source", help="source Markdown document")
    parser.add_argument("--output", default=".wiki/neo", help="output root")
    parser.add_argument("--slug", default="", help="output slug")
    parser.add_argument(
        "--agent-backend", choices=("hermes", "pi", "chat"), default="hermes"
    )
    args = parser.parse_args()
    config = NeoConfig(
        output_root=args.output,
        document_slug=args.slug,
        agent_backend=args.agent_backend,
    )
    def progress(event: dict) -> None:
        stage = event.get("stage", "work")
        step = event.get("step", "")
        current = event.get("current")
        total = event.get("total")
        count = f" {current}/{total}" if current is not None and total is not None else ""
        detail = event.get("batch") or event.get("page") or event.get("output") or ""
        if stage == "observe" and step == "window":
            detail = (
                f"window={event.get('window', '?')} "
                f"lines={event.get('source_start', '?')}-{event.get('source_end', '?')} "
                f"observations={event.get('observations', 0)}"
            )
            if event.get("live_output"):
                detail += f" -> {event['live_output']}"
        elif stage == "plan" and step in {"compile_retry", "compile_done"}:
            pieces = []
            if event.get("attempt") is not None:
                pieces.append(f"attempt={event['attempt']}")
            if event.get("pages") is not None:
                pieces.append(f"pages={event['pages']}")
            if event.get("repairs"):
                pieces.append(f"boundary_repairs={event['repairs']}")
            if event.get("prompt"):
                pieces.append(f"prompt={event['prompt']}")
            if event.get("response"):
                pieces.append(f"response={event['response']}")
            detail = " ".join(pieces)
        elif stage == "plan" and step == "region":
            detail = (
                f"lines={event.get('source_start', '?')}-{event.get('source_end', '?')} "
                f"candidates={event.get('pages', 0)}"
            )
        elif stage == "plan" and step == "semantic":
            detail = f"attempt={event.get('attempt', '?')}"
        elif stage == "judge":
            pieces = [str(event.get("page", ""))]
            if event.get("version") is not None:
                pieces.append(f"version={event['version']}")
            if event.get("score") is not None:
                pieces.append(f"score={event['score']}")
            if event.get("enrichment_score") is not None:
                pieces.append(f"enrichment={event['enrichment_score']}")
            if event.get("missing") is not None:
                pieces.append(f"missing={event['missing']}")
            if event.get("attempts") is not None:
                pieces.append(f"attempts={event['attempts']}")
            detail = " ".join(pieces)
        elif stage == "research":
            pieces = [str(event.get("page", ""))]
            if event.get("reference"):
                pieces.append(f"reference={event['reference']}")
            if event.get("candidates") is not None:
                pieces.append(f"candidates={event['candidates']}")
            if event.get("facts") is not None:
                pieces.append(f"facts={event['facts']}")
            if event.get("reads") is not None:
                pieces.append(f"reads={event['reads']}")
            if event.get("minimum") is not None:
                pieces.append(f"minimum={event['minimum']}")
            if event.get("attempts") is not None:
                pieces.append(f"attempts={event['attempts']}")
            detail = " ".join(pieces)
        elif stage == "rewrite" and step in {
            "plan_start",
            "plan_done",
            "plan_retry",
            "write_start",
            "write_retry",
        }:
            pieces = [str(event.get("page", ""))]
            if event.get("version") is not None:
                pieces.append(f"version={event['version']}")
            if event.get("attempt") is not None:
                pieces.append(f"attempt={event['attempt']}")
            if event.get("output"):
                pieces.append(f"output={event['output']}")
            detail = " ".join(pieces)
        elif stage == "rewrite" and step == "page_done":
            pieces = [str(event.get("page", ""))]
            if event.get("attempts") is not None:
                pieces.append(f"attempts={event['attempts']}")
            if event.get("version"):
                pieces.append(f"selected_version={event['version']}")
            if event.get("score") is not None:
                pieces.append(f"score={event['score']}")
            detail = " ".join(pieces)
        elif stage == "link" and step == "page_done":
            detail = f"{event.get('page', '')} attempts={event.get('attempts', 0)}"
        if event.get("error"):
            detail = f"{detail} error={event['error']}".strip()
        fallback = " [fallback]" if event.get("fallback") else ""
        cached = " [cached]" if event.get("cached") else ""
        print(
            f"[neo] {stage}: {step}{count}{fallback}{cached} {detail}".rstrip(),
            file=sys.stderr,
            flush=True,
        )

    result = asyncio.run(run_pipeline(args.source, config=config, on_progress=progress))
    print(json.dumps({"run": str(result), "wiki": str(result / "wiki")}, indent=2))


if __name__ == "__main__":
    main()
