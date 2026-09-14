"""Generate wiki output for one raw Markdown file, without app/GROWI.

Usage:
    .venv/bin/python wiki_one.py rikiseisan/test/<name>_pdf.md
"""

from __future__ import annotations

import sys
from pathlib import Path

from graph.core import Settings
from graph.formats import kind_of
from graph.project import Project
from graph.wiki.model import ChatModelPort
from graph.writers import wiki_config, write_index, write_wiki


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python wiki_one.py <raw-relative-path>")
    rel = sys.argv[1].strip().lstrip("/")

    settings = Settings.from_env()
    project = Project(Path(settings.data_root)).ensure()
    if not project.raw_file(rel).exists():
        raise SystemExit(f"raw file not found: {project.raw_file(rel)}")

    kind = kind_of(rel)
    cfg = wiki_config(settings, run_dir=project.state_dir(rel), source_kind=kind)
    llm = ChatModelPort(cfg)

    print(f"[wiki_one] source={rel} kind={kind}", flush=True)

    def on_progress(event: dict) -> None:
        print(f"[wiki_one] {event}", flush=True)

    target = write_wiki(
        project,
        rel,
        mode="wiki",
        settings=settings,
        llm=llm,
        embedder=None,
        on_progress=on_progress,
    )
    write_index(project)
    print(f"[wiki_one] DONE -> {target}", flush=True)


if __name__ == "__main__":
    main()
