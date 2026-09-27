"""Gateway tests for streaming behavior that does not need an LLM endpoint."""

import unittest
from types import SimpleNamespace

from gateway import LlmClient


class StreamTests(unittest.TestCase):
    def test_stream_preserves_whitespace_between_chunks(self):
        client = object.__new__(LlmClient)

        class FakeLLM:
            def stream(self, _messages):
                yield SimpleNamespace(content="Hello", usage_metadata=None)
                yield SimpleNamespace(content=" world", usage_metadata={"output_tokens": 2})
                yield SimpleNamespace(content="! ", usage_metadata=None)

        client.llm = FakeLLM()
        deltas = []
        result = client.stream("system", "user", deltas.append)
        self.assertEqual(deltas, ["Hello", " world", "! "])
        self.assertEqual(result, "Hello world! ")
        self.assertEqual(client.last_usage, {"output_tokens": 2})


if __name__ == "__main__":
    unittest.main()
