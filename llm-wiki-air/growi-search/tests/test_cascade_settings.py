"""Settings and prompt contract for cascade mode and WP-16 switches."""

import os
import unittest
from unittest import mock

from config import Settings
from gateway import (jev_cascade_profile_questions, jev_cascade_section_question,
                     jev_toc_question)
from prompts import PACKED_SUBAGENT_PROMPT, SYNTHESIS_PROMPT


class CascadeSettingsTests(unittest.TestCase):
    def settings(self, values=None):
        env = {"GROWI_URL": "http://growi", "GROWI_TOKEN": "test", **(values or {})}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("config._load_env_files"):
            return Settings.from_env()

    def test_defaults_keep_exhaustive_mode(self):
        settings = self.settings()
        self.assertEqual(settings.jev_mode, "exhaustive")
        self.assertEqual(settings.cascade_max_docs, 40)
        self.assertEqual(settings.cascade_section_threshold, 0.3)
        self.assertEqual(settings.cascade_subagents, 15)
        self.assertEqual(settings.cascade_context_tokens, 48000)
        self.assertEqual(settings.cascade_subagent_steps, 4)
        self.assertEqual(settings.cascade_early_stop, 0.9)
        self.assertFalse(settings.answer_cache)

    def test_environment_values_and_subagent_clamp(self):
        settings = self.settings({
            "WIKI_JEV_MODE": "cascade",
            "WIKI_CASCADE_SUBAGENTS": "99",
            "WIKI_CASCADE_SECTION_THRESHOLD": "1.4",
            "WIKI_ANSWER_CACHE": "yes",
            "WIKI_JEV_TOC_GATE": "1",
            "WIKI_JEV_TOC_GATE_THRESHOLD": "0.25",
            "WIKI_JEV_DETERMINISTIC": "true",
            "WIKI_JEV_VERDICT_CACHE": "on",
            "WIKI_LEAD_AFTER_REPORTS": "synthesis",
            "WIKI_AGENT_TOOL_CONCURRENCY": "5",
        })
        self.assertEqual(settings.jev_mode, "cascade")
        self.assertEqual(settings.cascade_subagents, 32)
        self.assertEqual(settings.cascade_section_threshold, 1.0)
        self.assertTrue(settings.answer_cache)
        self.assertTrue(settings.jev_toc_gate)
        self.assertEqual(settings.jev_toc_gate_threshold, 0.25)
        self.assertTrue(settings.jev_deterministic)
        self.assertTrue(settings.jev_verdict_cache)
        self.assertEqual(settings.lead_after_reports, "synthesis")
        self.assertEqual(settings.agent_tool_concurrency, 5)

    def test_invalid_modes_and_lead_strategy_fail_strict_validation(self):
        for values, message in (({"WIKI_JEV_MODE": "unknown"}, "WIKI_JEV_MODE"),
                                ({"WIKI_LEAD_AFTER_REPORTS": "unknown"}, "WIKI_LEAD_AFTER_REPORTS")):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, message):
                self.settings(values).validate_strict()

    def test_exact_question_contracts_and_prompts(self):
        self.assertEqual(jev_cascade_profile_questions(), (
            "この質問は、該当する項目をすべて列挙することを求めていますか？\n選択肢: はい / いいえ",
            "この質問は、1つの事実や値だけで答えられますか？\n選択肢: はい / いいえ",
        ))
        self.assertEqual(jev_toc_question("質問"),
                         "この目次に、次の質問に関係する項目は含まれていますか？\n質問: 質問\n選択肢: はい / いいえ")
        self.assertEqual(jev_cascade_section_question("Q"),
                         "上記の節は、次の質問に対する答えの全部または一部を実際に含んでいますか？\n質問: Q\n選択肢: はい / いいえ")
        self.assertIn("一覧の一項目", jev_cascade_section_question("Q", list_profile=True))
        self.assertIn("引用:", SYNTHESIS_PROMPT)
        self.assertIn("省略せず全部", PACKED_SUBAGENT_PROMPT)


if __name__ == "__main__":
    unittest.main()
