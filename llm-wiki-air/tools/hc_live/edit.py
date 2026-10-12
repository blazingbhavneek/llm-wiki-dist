"""Act as a human GROWI editor on one test project's pages (saves like the browser editor: origin=editor).

  HC_PROJECT=<config name> .venv/bin/python tools/hc_live/edit.py <command> ...

  pages                                   list the project's published pages: local path, page id, GROWI path
  show <local_path>                       print the page body as the editor shows it
  replace <local_path> OLD NEW            replace exactly one occurrence of OLD (NEW may be '')
  insert-after <local_path> ANCHOR TEXT   add TEXT as a new paragraph after the one line containing ANCHOR
  delete <local_path> TEXT                remove exactly one occurrence of TEXT (and its line if that becomes empty)
  save-unchanged <local_path>             open and save without changes
  history <local_path>                    list revisions (id, time, size), newest first
  restore <local_path> <revision_id>      save an older revision's body as a new revision
  move <local_path> NEW_GROWI_PATH        rename (move) the page in GROWI
  trash <local_path>                      move the page to the GROWI trash
  create NEW_GROWI_PATH TEXT              create a page the pipeline does not own

<local_path> is the first column of `pages`. In OLD/NEW/ANCHOR/TEXT a literal \\n means a line break.
Only pages below /<target_name>/ of the selected project can be touched. The token is HUMAN_TOKEN
(a separate editor account) if set, else the project's GROWI token. An API edit is a pre-check only:
it never counts as real-user evidence (handoff-conflicts.md 12.5).
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

SETTINGS = load_settings(os.environ.get("HC_PROJECT", "hc-e2e-fast"))
PREFIX = "/" + SETTINGS.target_name.strip("/") + "/"
LEDGER = open_project(SETTINGS).metadata / "pipeline.json"
COMMANDS = {"pages", "show", "replace", "insert-after", "delete", "save-unchanged", "history", "restore",
            "move", "trash", "create"}


def client() -> GrowiClient:
    token = os.environ.get("HUMAN_TOKEN") or SETTINGS.growi_token
    return GrowiClient(SETTINGS.growi_url.rstrip("/"), token, timeout=30)


def rows() -> dict:
    return json.loads(LEDGER.read_text(encoding="utf-8"))["published_pages"]


async def main(argv: list[str]) -> None:
    if not argv or argv[0] not in COMMANDS:
        raise SystemExit(__doc__)
    if argv[0] == "pages":
        for path, row in sorted(rows().items()):
            print(path, row["page_id"], row["growi_path"])
        return
    argv = argv[:2] + [a.replace("\\n", "\n") for a in argv[2:]]
    if argv[0] == "create":
        if not argv[1].startswith(PREFIX):
            raise SystemExit("create only below " + PREFIX)
        page = await client().create_page(argv[1], argv[2])
        print(f"create ok: {page.path} page {page.page_id} revision {page.revision_id}")
        return
    local_path = argv[1]
    page = await client().get_page(page_id=rows()[local_path]["page_id"])
    if page is None or not page.path.startswith(PREFIX):
        raise SystemExit("page missing or outside " + PREFIX)
    body = page.body
    if argv[0] == "show":
        print(body)
        return
    if argv[0] == "move":
        if not argv[2].startswith(PREFIX):
            raise SystemExit("move only below " + PREFIX)
        moved = await client().rename_page(page.page_id, page.revision_id, argv[2])
        print(f"move ok: {page.path} -> {moved.path} revision {page.revision_id} -> {moved.revision_id}")
        return
    if argv[0] == "trash":
        await client().delete_pages({page.page_id: page.revision_id})
        print(f"trash ok: {page.path} revision {page.revision_id}")
        return
    if argv[0] in {"history", "restore"}:
        listing = (await client()._request("GET", "/revisions/list",
                                           params={"pageId": page.page_id, "limit": 50})).json()["revisions"]
        if argv[0] == "history":
            for revision in listing:
                print(revision["_id"], revision.get("createdAt"), len(revision.get("body", "")))
            return
        body = next(revision["body"] for revision in listing if revision["_id"] == argv[2])
    elif argv[0] == "replace":
        old, new = argv[2], argv[3]
        if body.count(old) != 1:
            raise SystemExit(f"expected exactly one {old!r}, found {body.count(old)}")
        body = body.replace(old, new)
    elif argv[0] == "insert-after":
        anchor, text = argv[2], argv[3]
        lines = body.split("\n")
        hits = [i for i, line in enumerate(lines) if anchor in line]
        if len(hits) != 1:
            raise SystemExit(f"expected exactly one line containing {anchor!r}, found {len(hits)}")
        lines[hits[0] + 1:hits[0] + 1] = ["", text]
        body = "\n".join(lines)
    elif argv[0] == "delete":
        text = argv[2]
        if body.count(text) != 1:
            raise SystemExit(f"expected exactly one {text!r}, found {body.count(text)}")
        lines = body.split("\n")
        i = next(i for i, line in enumerate(lines) if text in line)
        lines[i] = lines[i].replace(text, "", 1)
        if not lines[i].strip():
            del lines[i]
        body = "\n".join(lines)
    response = await client()._request("PUT", "/page/", json_body={
        "pageId": page.page_id, "revisionId": page.revision_id, "body": body, "origin": "editor"})
    revision = response.json().get("page", {}).get("revision", {})
    new_revision = revision.get("_id", revision) if isinstance(revision, dict) else revision
    print(f"{argv[0]} ok: {local_path} revision {page.revision_id} -> {new_revision}")


asyncio.run(main(sys.argv[1:]))
