#!/usr/bin/env python3
"""
Standalone CLI for the chunker package (the webapp path runs the same
pipeline inside graph.librarian via the "chunk_and_ingest" write job).

Examples:
    python chunk.py --input big.md --out out/big
    python chunk.py --input big.md --out out/big --off skeleton_llm,adjudicator
    python chunk.py --input big.md --out out/big --no-llm
    python chunk.py --input big.md --out out/big --ingest http://localhost:51023

--off takes comma-separated feature names (ablation switches):
    structure, texttiling, sat, ppl, skeleton_llm, adjudicator
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from chunker import ChunkConfig, run_chunk_pipeline


class _OpenAIShim:
    """Minimal OpenAI-compatible adapter matching the llm protocol the
    chunker expects (complete / complete_structured)."""

    def __init__(self, base_url: str, api_key: str, model: str):
        import requests

        self._requests = requests
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def _chat(self, system: str, user: str) -> str:
        resp = self._requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.0,
            },
            timeout=300,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def complete(self, system: str, user: str) -> str:
        return self._chat(system, user)

    def complete_structured(self, system: str, user: str, output_model):
        text = self._chat(
            system + "\nReturn ONLY a JSON object matching the required schema.",
            user,
        )
        first, last = text.find("{"), text.rfind("}")
        if first == -1 or last <= first:
            raise ValueError("no JSON object in LLM response")
        return output_model.model_validate(json.loads(text[first : last + 1]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="markdown source file")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--off", default="", help="comma-separated features to disable")
    parser.add_argument("--no-llm", action="store_true", help="fully deterministic run")
    parser.add_argument("--ingest", default="", help="POST /api/ingest to this app URL")
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", "local"))
    parser.add_argument("--model", default=os.environ.get("WIKI_MODEL", ""))
    args = parser.parse_args()

    config = ChunkConfig.from_env()
    for feature in filter(None, (f.strip() for f in args.off.split(","))):
        attr = f"use_{feature}"
        if not hasattr(config, attr):
            parser.error(f"unknown feature: {feature}")
        setattr(config, attr, False)

    llm = None
    if not args.no_llm and (config.use_skeleton_llm or config.use_adjudicator):
        if args.base_url and args.model:
            llm = _OpenAIShim(args.base_url, args.api_key, args.model)
        else:
            print("[chunk] no --base-url/--model; LLM features disabled", file=sys.stderr)

    source = Path(args.input).read_text(encoding="utf-8")
    result = run_chunk_pipeline(
        source_text=source,
        document_name=Path(args.input).name,
        out_dir=args.out,
        config=config,
        llm=llm,
        on_progress=lambda p: print(f"[chunk] {p}", file=sys.stderr),
    )
    print(json.dumps(result.ablation, indent=2, ensure_ascii=False))

    if args.ingest:
        import requests

        resp = requests.post(
            args.ingest.rstrip("/") + "/api/ingest",
            json={"path": str(Path(args.out).resolve())},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"[chunk] ingest job queued: {resp.json().get('id')}", file=sys.stderr)


if __name__ == "__main__":
    main()
