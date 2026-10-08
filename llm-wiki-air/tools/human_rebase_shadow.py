"""Shadow check for publisher.human_changes.rebase on real history. Read-only; run it on a COPY of a data root.

For each document it takes consecutive Git versions of the writer's pure output
(metadata/state/<document>/wiki/*.md, _review.md excluded) where the source
changed, injects human edits into the older version, and re-bases them onto the
newer one with the real model. It prints counts and hashes only, never page text.

    .venv/bin/python tools/human_rebase_shadow.py --project /abs/copy.ini [--limit 5] [--seed 1] [--no-model]

Checks: every injected human text is present exactly once (result or Appendix
entries); rebase(x, x, z) == z and rebase(x, y, x) == y.
"""

import argparse
import hashlib
import json
import random
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graph.config import Settings  # noqa: E402
from graph.workspace.project import open_project  # noqa: E402
from publisher.human_changes import LlmHumanModel, rebase  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "core.quotepath=off", *args], check=True, capture_output=True, text=True).stdout


def versions(root: Path, document: str) -> list[dict[str, str]]:
    """Distinct consecutive page sets of one document, oldest first."""
    folder = f"metadata/state/{document}/wiki"
    result: list[dict[str, str]] = []
    for commit in reversed(git(root, "log", "--format=%H", "--", folder).split()):
        names = [n for n in git(root, "ls-tree", "--name-only", commit, f"{folder}/").split("\n") if n.endswith(".md")]
        pages = {Path(n).name: git(root, "show", f"{commit}:{n}") for n in names if not n.endswith("_review.md")}
        if pages and (not result or pages != result[-1]):
            result.append(pages)
    return result


def inject(pages: dict[str, str], newer: dict[str, str], rng: random.Random) -> tuple[dict[str, str], list[str]]:
    """Human edits; returns (edited pages, texts that must survive exactly once)."""
    edited, texts = dict(pages), []
    names = sorted(edited)
    newer_lines = {line for text in newer.values() for line in text.splitlines()}

    def pick(test):
        spots = [(n, i) for n in names for i, line in enumerate(edited[n].splitlines()) if test(line)]
        return rng.choice(spots) if spots else None

    def change(spot, make):
        name, i = spot
        lines = edited[name].splitlines()
        lines[i:i + 1] = make(lines[i])
        edited[name] = "\n".join(lines) + "\n"

    if (spot := pick(lambda l: re.search(r"\d", l) and not l.startswith("#"))):
        change(spot, lambda l: [re.sub(r"\d+", lambda m: str(int(m[0]) + 1), l, count=1)])
        texts.append(edited[spot[0]].splitlines()[spot[1]])
    if (spot := pick(lambda l: l.strip() and not l.startswith(("#", "|", "`", "-")))):
        note = f"Human shadow paragraph {rng.randint(1000, 9999)}."
        change(spot, lambda l: [l, "", note])
        texts.append(note)
    if (spot := pick(lambda l: l.startswith("## "))):
        change(spot, lambda l: [l + " (renamed)"])
        texts.append(edited[spot[0]].splitlines()[spot[1]])
    if (spot := pick(lambda l: l.strip() and l not in newer_lines and not l.startswith(("#", "|", "`")))):
        change(spot, lambda l: [l + " (checked by a human)"])  # a sentence the newer version rewrites
        texts.append(edited[spot[0]].splitlines()[spot[1]])
    if (spot := pick(lambda l: l.strip() and not l.startswith(("#", "|", "`")))):
        change(spot, lambda l: [])  # a deleted sentence: nothing to keep
    return edited, texts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--project", required=True, help="absolute INI of a COPY of the project")
    parser.add_argument("--limit", type=int, default=5, help="documents to check")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--no-model", action="store_true", help="skip the model: window case 5 takes the fallback note")
    args = parser.parse_args()
    settings = Settings.from_env(args.project)
    project = open_project(settings)
    root = Path(project.root)
    rng = random.Random(args.seed)
    model = None if args.no_model else LlmHumanModel(settings, project)
    tracked = git(root, "log", "--name-only", "--format=", "--", "metadata/state/").split("\n")  # deleted documents keep their history
    documents = sorted({n[len("metadata/state/"):].split("/wiki/")[0] for n in tracked if "/wiki/" in n and n.endswith(".md")})
    total, failures = Counter(), 0
    for document in documents[:args.limit]:
        history = versions(root, document)
        for older, newer in zip(history, history[1:]):
            edited, texts = inject(older, newer, rng)
            stats, started = Counter(), time.monotonic()
            pages, entries = rebase(older, edited, newer, model=model, stats=stats)
            everything = "\n".join(pages.values()) + "\n".join(entries)
            seen = everything + "\n" + everything.replace("\n> ", "\n")  # a quoted source note still shows the line
            lost = [hashlib.sha256(t.encode()).hexdigest()[:12] for t in texts if everything.count(t) != 1]
            identities = rebase(older, older, newer)[0] == newer and rebase(older, edited, older)[0] == edited
            missing = [hashlib.sha256(l.encode()).hexdigest()[:12] for t in newer.values()
                       for l in t.splitlines() if l.strip() and l not in seen]
            total.update(stats)
            failures += bool(lost) or not identities
            print(json.dumps({
                "document": hashlib.sha256(document.encode()).hexdigest()[:12], "pages": len(newer),
                "injected": len(texts), "injected_not_exactly_once": lost, "identities_hold": identities,
                "paths": dict(stats), "newer_lines_missing": missing[:20], "newer_lines_missing_count": len(missing),
                "seconds": round(time.monotonic() - started, 1),
            }, ensure_ascii=False))
    print(json.dumps({"summary": dict(total), "failing_pairs": failures}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
