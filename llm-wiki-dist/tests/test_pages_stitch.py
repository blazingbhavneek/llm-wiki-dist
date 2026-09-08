from __future__ import annotations

import unittest

from graph.pages import StitchOp, apply_stitch_ops


class StitchTests(unittest.TestCase):
    def test_structural_ops_preserve_chunk_payloads(self):
        body = "## 見出し\n\n<!-- chunk: c1 lines 1-2 hash:x -->\nVERBATIM BODY"
        result = apply_stitch_ops(body, [StitchOp(op="insert_intro", text="導入")])
        self.assertIn("VERBATIM BODY", result)
        self.assertIn("導入", result)

    def test_attempt_to_insert_chunk_text_is_rejected(self):
        body = "## 見出し\n\n<!-- chunk: c1 lines 1-2 hash:x -->\nVERBATIM BODY"
        with self.assertRaises(RuntimeError):
            apply_stitch_ops(body, [StitchOp(op="insert_intro", text="VERBATIM BODY")])


if __name__ == "__main__":
    unittest.main()
