"""Count marker words in GROWI and in the local state of one test project.

  HC_PROJECT=<config name> .venv/bin/python tools/hc_live/count.py WORD [WORD...] [--page LOCAL_PATH ...]

Prints, per word: growi = occurrences in GROWI, current = in the human state
(metadata/human-sync/doc/*/current), live = in the local wiki pages. "Once" in the handoff
means growi == current == live == 1. Without --page every page below /<target_name>/ is read
(table-of-contents pages 00-目次 included; they may lag after a small source patch). With
--page (the first column of `edit.py pages`; repeatable) only those pages are read, in GROWI
and locally: use that on a target that also holds documents you must not read.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from common.settings import load_settings  # noqa: E402
from graph.growi.client import GrowiClient  # noqa: E402
from graph.workspace.project import open_project  # noqa: E402
from publisher.human_changes import HumanStore  # noqa: E402


async def main(args: list[str]) -> None:
    words = [a for i, a in enumerate(args) if a != "--page" and (i == 0 or args[i - 1] != "--page")]
    pages = [args[i + 1] for i, a in enumerate(args) if a == "--page" and i + 1 < len(args)]
    settings = load_settings(os.environ.get("HC_PROJECT", "hc-e2e-fast"))
    root = "/" + settings.target_name.strip("/")
    client = GrowiClient(settings.growi_url.rstrip("/"), settings.growi_token, timeout=30)
    project = open_project(settings)
    if pages:
        ledger = json.loads((project.metadata / "pipeline.json").read_text(encoding="utf-8"))["published_pages"]
        ids = [ledger[page]["page_id"] for page in pages]
        store = HumanStore(project)
        state = {store._dir(json.loads((project.wiki / Path(page).parent / "_planning" / "source.json")
                                       .read_text(encoding="utf-8"))["raw"]) / "current" / Path(page).name for page in pages}
    else:
        ids = [page.page_id for page in await client.list_all_pages(root) if page.path.startswith(root + "/")]
    bodies = []
    for page_id in ids:
        full = await client.get_page(page_id=page_id)
        bodies.append(full.body if full and full.path.startswith(root + "/") else "")
    current = [f.read_text(encoding="utf-8") for f in (state if pages else project.metadata.glob("human-sync/doc/*/current/*.md"))
               if f.is_file()]
    live = [(project.wiki / page).read_text(encoding="utf-8") for page in pages if (project.wiki / page).is_file()] if pages else [
        f.read_text(encoding="utf-8") for f in project.wiki.glob("**/*.md") if f.is_file() and "_planning" not in f.parts]
    for word in words:
        print({"word": word, "growi": sum(b.count(word) for b in bodies),
               "current": sum(t.count(word) for t in current), "live": sum(t.count(word) for t in live)})


asyncio.run(main(sys.argv[1:]))
