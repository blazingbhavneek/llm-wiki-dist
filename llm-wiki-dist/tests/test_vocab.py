from __future__ import annotations

import unittest

from graph.vocab import Vocabulary, fold, kana_readings


CORPUS = [
    {
        "title": "mpf_mfs_open 関数",
        "cluster": "MFSファイル管理",
        "keywords": ["ファイル登録", "mpf_mfs_cyclicfile"],
        "claims": ["第3引数は `filenum` を指定する"],
        "source_path": "docs/mfs/open.md",
        "body": (
            "処理要求には pmf_prg.txt と pmf_procdata.txt の登録が必要です。"
            "`bufsize` はバッファ長を表します。"
        ),
    },
    {
        "title": "MPF_MODE_SYNC の設定",
        "cluster": "設定パラメータ",
        "keywords": [],
        "claims": [],
        "body": "同期モードは MPF_MODE_SYNC を使用します。",
    },
]


class VocabularyBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vocab = Vocabulary.from_rows(CORPUS)

    def test_identifiers_are_harvested_from_every_field(self):
        # keywords_json is not reliably populated in every corpus, so the sheet
        # cannot depend on one enrichment pass having run.
        for term in (
            "mpf_mfs_open",       # title
            "mpf_mfs_cyclicfile", # keywords
            "filenum",            # claim, in a code span
            "bufsize",            # body, in a code span
            "pmf_prg.txt",        # body, filename
            "MPF_MODE_SYNC",      # body, all caps
        ):
            with self.subTest(term=term):
                self.assertTrue(self.vocab.knows(term), term)

    def test_ordinary_words_are_not_identifiers(self):
        self.assertFalse(self.vocab.knows("function"))
        self.assertFalse(self.vocab.knows("設定します"))

    def test_kind_separates_names_from_enrichment_keywords(self):
        # Presence is not the useful test. A caller asking "is this a thing one
        # can look up on its own" needs the kind: keywords are ordinary nouns
        # and searching for one returns whichever document defines the word.
        vocab = Vocabulary.from_rows(
            [
                {
                    "title": "Unified Memory",
                    "keywords": ["System", "Allocated", "Compute", "PCIe"],
                    "body": "Allocate with `cudaMallocManaged`; pass `filenum`.",
                }
            ]
        )
        for term in ("System", "Allocated", "Compute"):
            with self.subTest(term=term):
                self.assertTrue(vocab.knows(term))
                self.assertEqual(vocab.kind_of(term), "keyword")
        for term in ("cudaMallocManaged", "filenum"):
            with self.subTest(term=term):
                self.assertEqual(vocab.kind_of(term), "identifier")

    def test_a_second_capital_marks_a_name_even_among_keywords(self):
        # English capitalises one letter per word, so a second capital is not
        # prose. `PCIe` and `NVLink` are exactly the sort of term a reader asks
        # a follow-up question about, and they arrive as enrichment keywords
        # alongside ordinary nouns.
        vocab = Vocabulary.from_rows(
            [{"title": "Interconnect", "keywords": ["PCIe", "NVLink", "Allocated"]}]
        )
        for term in ("PCIe", "NVLink"):
            with self.subTest(term=term):
                self.assertEqual(vocab.kind_of(term), "identifier")
        self.assertEqual(vocab.kind_of("Allocated"), "keyword")

    def test_kind_of_an_absent_term_is_empty(self):
        self.assertEqual(self.vocab.kind_of("nonexistent_thing"), "")

    def test_a_word_does_not_inherit_the_kind_of_an_identifier_it_folds_onto(self):
        # Folding strips the punctuation that makes a name a name, so
        # `__managed__`, `_system` and `_ALLOCATION` collapse onto the English
        # words "managed", "system" and "allocation". Matching wants that
        # looseness; classification must not have it, or every one of those
        # words becomes something worth spending a research stage on.
        vocab = Vocabulary.from_rows(
            [
                {
                    "title": "Unified Memory",
                    "body": "Declare `__managed__` and call `_system`; see `_ALLOCATION`.",
                }
            ]
        )
        for name in ("__managed__", "_system", "_ALLOCATION"):
            with self.subTest(name=name):
                self.assertEqual(vocab.kind_of(name), "identifier")
        for word in ("managed", "system", "Allocation"):
            with self.subTest(word=word):
                self.assertEqual(vocab.kind_of(word), "")
                # The looser matching path still reaches the identifier, which
                # is what lets a mishearing find it.
                self.assertTrue(vocab.knows(word))

    def test_a_capitalised_word_in_a_code_span_is_not_an_identifier(self):
        # Documentation marks up prose constantly. Capitalisation is what
        # separates `filenum` from a backticked common noun.
        vocab = Vocabulary.from_rows(
            [{"title": "doc", "body": "The `System` allocates; call `cudaMalloc`."}]
        )
        self.assertEqual(vocab.kind_of("System"), "")
        self.assertEqual(vocab.kind_of("cudaMalloc"), "identifier")


class VocabularyMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vocab = Vocabulary.from_rows(CORPUS)

    def test_an_acronym_dictated_in_katakana_resolves_to_the_identifier(self):
        match = self.vocab.match("エムピーエフ エムエフエス オープン の第3引数は？")

        self.assertEqual(match.pinned, "mpf_mfs_open")
        self.assertIn("mpf_mfs_open", match.query)
        # The original wording is never removed: it is what the user said.
        self.assertIn("エムピーエフ", match.query)

    def test_a_romaji_transcription_resolves_too(self):
        match = self.vocab.match("mpf mfs open の使い方")
        self.assertEqual(match.pinned, "mpf_mfs_open")

    def test_a_japanese_cluster_name_is_matched_by_substring(self):
        match = self.vocab.match("MFSファイル管理について教えて")
        self.assertIn("MFSファイル管理", match.clusters)

    def test_a_filename_heard_as_katakana_is_repaired(self):
        # Spoken without the extension, so the stem is the honest match; both
        # spellings are in the sheet and either one reaches the same page.
        match = self.vocab.match("ピーエムエフ ピーアールジー のファイルについて")
        self.assertTrue(match.pinned.startswith("pmf_prg"), match.pinned)

    def test_repairs_report_what_was_heard_and_what_was_matched(self):
        repairs = self.vocab.match("エムピーエフ エムエフエス オープン").public()["repairs"]
        matched = {repair["matched"] for repair in repairs}
        self.assertIn("mpf_mfs_open", matched)

    def test_a_sound_japanese_cannot_spell_still_matches(self):
        # カ行 always transliterates to `k`, so 「クーダ」 can never reach `cuda`
        # by spelling; only by what the two would have sounded like.
        vocab = Vocabulary.from_rows([{"title": "CUDA kernel", "body": "`cudaMalloc`"}])
        self.assertEqual(vocab.match("クーダ カーネルについて").pinned, "CUDA")

    def test_content_hashes_are_not_vocabulary(self):
        vocab = Vocabulary.from_rows(
            [{"title": "figure", "body": "![](e9ab4278380e5210fa318a790495e17e2bf20da3.jpg)"}]
        )
        self.assertFalse(vocab.knows("e9ab4278380e5210fa318a790495e17e2bf20da3.jpg"))

    def test_an_unrelated_question_pins_nothing(self):
        match = self.vocab.match("今日の天気はどうですか")
        self.assertEqual(match.pinned, "")
        self.assertEqual(match.query, "今日の天気はどうですか")

    def test_an_empty_vocabulary_is_a_no_op(self):
        match = Vocabulary.empty().match("エムピーエフ")
        self.assertEqual(match.query, "エムピーエフ")
        self.assertEqual(match.identifiers, [])


class SimilarNameTests(unittest.TestCase):
    """"Never widen the subject to a similarly named one" starts here."""

    def setUp(self) -> None:
        self.vocab = Vocabulary.from_rows(
            [
                {"title": "cudaFree", "body": "`cudaFree` releases the allocation."},
                {"title": "cudaFreeHost", "body": "`cudaFreeHost` releases pinned host memory."},
            ]
        )

    def test_a_particle_glued_to_an_identifier_does_not_reach_its_neighbour(self):
        # Windows used to join across scripts, so 「cudaFree は をする」 became one
        # candidate and fuzzy-matched `cudaFreeHost` at 0.82. That pinned a
        # different function into the scope line every shard reads.
        match = self.vocab.match("cudaFree は何をする関数ですか")
        self.assertEqual(match.identifiers, ["cudaFree"])
        self.assertNotIn("cudaFreeHost", match.query)

    def test_the_other_function_is_still_found_when_it_is_asked_for(self):
        match = self.vocab.match("cudaFreeHost について")
        self.assertEqual(match.identifiers, ["cudaFreeHost"])

    def test_a_leading_underscore_is_part_of_the_name(self):
        # The token regex used to start at [A-Za-z], so 「__syncthreads とは」
        # tokenised to "syncthreads" and then failed the decoration check
        # against its own corpus spelling — the question had the underscores
        # and the tokenizer dropped them, pinning nothing at all.
        vocab = Vocabulary.from_rows(
            [{"title": "同期", "body": "`__syncthreads` と `__managed__` を使います。"}]
        )
        self.assertEqual(
            vocab.match("__syncthreads の使い方").identifiers, ["__syncthreads"]
        )
        self.assertEqual(
            vocab.match("__managed__ 変数とは").identifiers, ["__managed__"]
        )
        # ...while a bare word still must not reach the decorated identifier.
        self.assertEqual(vocab.match("managed memory とは").identifiers, [])


class TransliterationTests(unittest.TestCase):
    def test_letter_names_and_syllables_are_both_offered(self):
        # ケー is the letter `k` inside an acronym and `ke` inside a loanword;
        # neither reading can be assumed, so both are produced.
        self.assertEqual(kana_readings("エムピーエフ")[0], "mpf")
        self.assertIn("ke", kana_readings("ケース")[-1])

    def test_hiragana_is_folded_into_katakana(self):
        self.assertEqual(kana_readings("えむぴーえふ")[0], "mpf")

    def test_folding_ignores_separators_and_case(self):
        self.assertEqual(fold("MPF-MFS_Open"), "mpfmfsopen")


if __name__ == "__main__":
    unittest.main()
