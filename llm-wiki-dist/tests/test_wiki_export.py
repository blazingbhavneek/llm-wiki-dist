import json
import tempfile
import unittest
from pathlib import Path

from graph.wiki.export import canonical_name, export_ingest_layout


class ExportTests(unittest.TestCase):
    def test_canonical_name_strips_numeric_prefix(self) -> None:
        self.assertEqual(canonical_name("001-概要.md"), "概要.md")
        self.assertEqual(canonical_name("readme.md"), "readme.md")

    def test_export_writes_loader_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            (run / "state").mkdir(parents=True)
            (run / "wiki").mkdir()
            (run / "wiki" / "001-概要.md").write_text(
                "# 概要\n\n本文\n", encoding="utf-8"
            )
            (run / "state" / "plan.json").write_text(
                json.dumps(
                    {
                        "source": "x.md",
                        "source_sha256": "abc",
                        "source_line_count": 10,
                        "prompt_version": "v",
                        "pages": [
                            {
                                "number": 1,
                                "title": "概要",
                                "chapter": "導入",
                                "summary": "説明",
                                "filename": "001-概要.md",
                                "owner_ranges": [[1, 10]],
                                "reference_ranges": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (run / "state" / "manifest.json").write_text(
                json.dumps({"pages": [{"number": 1, "judge_score": 95, "verbatim_sections": []}]}),
                encoding="utf-8",
            )
            dest = Path(directory) / "out"
            export_ingest_layout(run, dest, document_name="f1/x_docx.md")
            self.assertTrue((dest / "docs" / "001-概要.md").exists())
            coverage = json.loads(
                (dest / "_planning" / "coverage.json").read_text(encoding="utf-8")
            )
            self.assertEqual(coverage["files"][0]["filename"], "概要.md")
            self.assertEqual(coverage["files"][0]["source_start"], 1)
            self.assertEqual(coverage["files"][0]["source_end"], 10)
            manifest = json.loads(
                (dest / "_planning" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["planning"]["ingest_mode"], "pages")
            self.assertEqual(manifest["files"][0]["judge_score"], 95)
