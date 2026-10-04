"""Act as a human GROWI editor on a published page (token from HUMAN_TOKEN).

    human.py pages                         list published hc-test pages
    human.py show <local_path>             print the page body
    human.py replace <local_path> OLD NEW  replace exactly one occurrence
    human.py insert <local_path> ANCHOR TEXT   new paragraph after the line containing ANCHOR
    human.py delete <local_path> TEXT      remove exactly one occurrence (and a blank line after it)
"""

import asyncio
import os
import sys

from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from graph.growi.client import GrowiClient  # noqa: E402
from publisher.ledger import load_ledger  # noqa: E402

LEDGER = ROOT / os.environ.get("HC_DATA_ROOT", "data_std2") / "mountdocs" / "metadata" / "pipeline.json"


def client() -> GrowiClient:
    return GrowiClient("http://localhost:3000", os.environ["HUMAN_TOKEN"], timeout=30)


def page_id(local_path: str) -> str:
    return str(load_ledger(LEDGER).published_pages[local_path]["page_id"])


async def main(argv: list[str]) -> None:
    command = argv[0]
    if command == "pages":
        for path in sorted(load_ledger(LEDGER).published_pages):
            if path.startswith(os.environ.get("HC_DOC", "hc-test")):
                print(path)
        return
    local_path = argv[1]
    page = await client().get_page(page_id=page_id(local_path))
    body = page.body
    if command == "show":
        print(body)
        return
    if command == "replace":
        old, new = argv[2], argv[3]
        if body.count(old) != 1:
            raise SystemExit(f"expected exactly one {old!r}, found {body.count(old)}")
        body = body.replace(old, new)
    elif command == "insert":
        anchor, text = argv[2], argv[3]
        lines = body.split("\n")
        hits = [i for i, line in enumerate(lines) if anchor in line]
        if len(hits) != 1:
            raise SystemExit(f"expected exactly one line with {anchor!r}, found {len(hits)}")
        lines[hits[0] + 1:hits[0] + 1] = ["", text]
        body = "\n".join(lines)
    elif command == "delete":
        text = argv[2]
        if body.count(text) != 1:
            raise SystemExit(f"expected exactly one {text!r}, found {body.count(text)}")
        body = body.replace(text + "\n\n", "", 1) if (text + "\n\n") in body else body.replace(text, "", 1)
    else:
        raise SystemExit(__doc__)
    updated = await client().update_page(page.page_id, page.revision_id, body)
    print(f"{command} ok: {local_path} revision {page.revision_id[:8]} -> {updated.revision_id[:8]}")


asyncio.run(main(sys.argv[1:]))
