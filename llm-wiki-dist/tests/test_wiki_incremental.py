import json
import tempfile
import unittest
from pathlib import Path

from graph.wiki.incremental import invalidate_pages


def make_run(root: Path, text: str) -> Path:
    (root / "state" / "pages").mkdir(parents=True)
    (root / "wiki").mkdir()
    lines = text.splitlines()
    plan = {
        "source_sha256": "old",
        "source_line_count": len(lines),
        "pages": [
            {
                "number": 1,
                "filename": "001-a.md",
                "owner_ranges": [[1, 5]],
                "reference_ranges": [[8, 8]],
            },
            {
                "number": 2,
                "filename": "002-b.md",
                "owner_ranges": [[6, 10]],
                "reference_ranges": [],
            },
        ],
    }
    (root / "state" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    for number, name in ((1, "001-a.md"), (2, "002-b.md")):
        (root / "wiki" / name).write_text("x", encoding="utf-8")
        (root / "state" / "pages" / f"{number:03d}.json").write_text("{}", encoding="utf-8")
    return root


class IncrementalTests(unittest.TestCase):
    def test_same_length_edit_invalidates_only_touching_pages(self) -> None:
        text = "\n".join(f"l{i}" for i in range(1, 11))
        with tempfile.TemporaryDirectory() as directory:
            run = make_run(Path(directory), text)
            new = text.replace("l7", "L7")
            self.assertEqual(invalidate_pages(run, [(7, 1, 7, 1)], new), "resumed")
            self.assertTrue((run / "wiki" / "001-a.md").exists())
            self.assertFalse((run / "wiki" / "002-b.md").exists())
            self.assertFalse((run / "state" / "pages" / "002.json").exists())
            plan = json.loads((run / "state" / "plan.json").read_text(encoding="utf-8"))
            self.assertNotEqual(plan["source_sha256"], "old")

    def test_reference_range_hit_invalidates_importing_page(self) -> None:
        text = "\n".join(f"l{i}" for i in range(1, 11))
        with tempfile.TemporaryDirectory() as directory:
            run = make_run(Path(directory), text)
            invalidate_pages(run, [(8, 1, 8, 1)], text.replace("l8", "L8"))
            self.assertFalse((run / "wiki" / "001-a.md").exists())
            self.assertFalse((run / "wiki" / "002-b.md").exists())

    def test_line_count_change_is_full(self) -> None:
        text = "\n".join(f"l{i}" for i in range(1, 11))
        with tempfile.TemporaryDirectory() as directory:
            run = make_run(Path(directory), text)
            self.assertEqual(
                invalidate_pages(run, [(7, 1, 7, 2)], text + "\nextra"), "full"
            )
            self.assertFalse(run.exists())
