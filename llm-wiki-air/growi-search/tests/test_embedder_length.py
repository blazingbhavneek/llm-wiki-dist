"""Embedding length fallback: long texts are sliced and their vectors pooled back."""

import unittest

import gateway
from gateway import Embedder


class EmbedderLengthTests(unittest.TestCase):
    def setUp(self):
        gateway.EMBED_TOKEN_LIMIT = 1000
        self.addCleanup(setattr, gateway, "EMBED_TOKEN_LIMIT", 8000)

    def embedder(self, max_piece: int):
        """Fake server: rejects a call whose longest slice exceeds ``max_piece`` and
        returns a 1-d vector carrying each slice's length."""
        embedder = object.__new__(Embedder)
        embedder.doc_prefix = ""
        calls: list[list[int]] = []

        class FakeEmbeddings:
            def embed_documents(self, pieces):
                lengths = [len(piece) for piece in pieces]
                calls.append(lengths)
                if max(lengths) > max_piece:
                    raise ValueError("maximum context length is 8192 tokens")
                return [[float(length)] for length in lengths]

        embedder._client = FakeEmbeddings()
        return embedder, calls

    def test_long_text_is_split_and_pooled_into_one_vector(self):
        embedder, calls = self.embedder(max_piece=1000)
        vectors = embedder.embed_documents(["あ" * 2500, "short"])
        self.assertEqual([len(call) for call in calls], [4])   # 3 slices + 1 whole text
        self.assertTrue(max(calls[0]) <= 1000)                 # every slice fits
        self.assertEqual(vectors, [[sum(calls[0][:3]) / 3], [5.0]])

    def test_rejection_escalates_parts_until_accepted(self):
        embedder, calls = self.embedder(max_piece=300)
        vectors = embedder.embed_documents(["あ" * 5000])
        self.assertGreater(len(calls), 1)                      # retried with more parts
        self.assertLessEqual(max(calls[-1]), 300)
        self.assertEqual(len(vectors), 1)                      # one vector per input text

    def test_short_and_empty_texts_stay_one_slice(self):
        embedder, calls = self.embedder(max_piece=1000)
        self.assertEqual(embedder.embed_documents(["", "x", "あ" * 1000]), [[0.0], [1.0], [1000.0]])
        self.assertEqual(calls, [[0, 1, 1000]])


if __name__ == "__main__":
    unittest.main()
