import asyncio
import unittest

from pydantic import BaseModel

from graph.clients.chat import structured_ainvoke
from graph.wiki.config import WikiConfig
from graph.wiki.model import ChatModelPort


class Result(BaseModel):
    value: str


class FakeModel:
    def __init__(self) -> None:
        self.bindings = []

    def bind(self, **kwargs):
        self.bindings.append(kwargs)
        return self

    def with_structured_output(self, _schema):
        return self

    async def ainvoke(self, _messages):
        return Result(value="ok")


class ModelThinkingTests(unittest.TestCase):
    def test_thinking_is_enabled_for_structured_and_text_calls(self) -> None:
        model = FakeModel()
        asyncio.run(structured_ainvoke(model, Result, []))
        asyncio.run(ChatModelPort(WikiConfig(), llm=model).text([]))

        self.assertEqual(
            [binding["extra_body"]["chat_template_kwargs"]["enable_thinking"] for binding in model.bindings],
            [True, True],
        )


if __name__ == "__main__":
    unittest.main()
