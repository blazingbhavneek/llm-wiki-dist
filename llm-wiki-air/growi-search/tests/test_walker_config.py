"""WP-11 question text and walker setting contract."""

import os
import unittest
from unittest import mock

from config import Settings
from gateway import jev_page_question, jev_route_question


class WalkerConfigTests(unittest.TestCase):
    def test_walker_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("config._load_env_files"):
            settings = Settings.from_env()
        self.assertEqual(settings.walker_threshold, 0.5)
        self.assertEqual(settings.walker_route_threshold, 0.15)
        self.assertEqual(settings.walker_min_children, 2)
        self.assertEqual(settings.walker_k, 3)
        self.assertEqual(settings.walker_max_items, 150)
        self.assertFalse(settings.walker_es_rescue)

    def test_walker_settings_and_bounds(self):
        values = {
            "WIKI_WALKER_THRESHOLD": "1.5",
            "WIKI_WALKER_ROUTE_THRESHOLD": "-0.5",
            "WIKI_WALKER_MIN_CHILDREN": "101",
            "WIKI_WALKER_K": "0",
            "WIKI_WALKER_MAX_ITEMS": "10001",
            "WIKI_WALKER_ES_RESCUE": "yes",
        }
        with mock.patch.dict(os.environ, values, clear=True), mock.patch("config._load_env_files"):
            settings = Settings.from_env()
        self.assertEqual(settings.walker_threshold, 1.0)
        self.assertEqual(settings.walker_route_threshold, 0.0)
        self.assertEqual(settings.walker_min_children, 100)
        self.assertEqual(settings.walker_k, 1)
        self.assertEqual(settings.walker_max_items, 10000)
        self.assertTrue(settings.walker_es_rescue)

    def test_question_builders_are_exact(self):
        self.assertEqual(
            jev_route_question("設定方法"),
            "上記のフォルダまたは文書の配下に、次の内容を説明しているページ、またはその手がかりになる"
            "ページが含まれている可能性はありますか？\n"
            "内容: 設定方法\n"
            "明らかに別の分野・別の種類の資料だけを含む場合のみ いいえ と答えてください。\n"
            "選択肢: はい / いいえ",
        )
        self.assertEqual(
            jev_page_question("設定方法"),
            "上記のページは、次の内容そのもの（説明・定義・手順・一覧・値など）を実際に述べていますか？\n"
            "内容: 設定方法\n"
            "関連する話題に触れているだけのページ、他のページへのリンクや目次だけのページは "
            "いいえ と答えてください。\n"
            "選択肢: はい / いいえ",
        )


if __name__ == "__main__":
    unittest.main()
