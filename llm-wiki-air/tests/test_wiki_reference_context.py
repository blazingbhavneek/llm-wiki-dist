import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from graph.wiki.pipeline import SeedPage, _research_references, _select_references


def _page(number: int, summary: str = "") -> SeedPage:
    return SeedPage(number, str(number), "", summary, [(number, number)], f"{number}.md", str(number))


class WikiReferenceContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_reference_research_sends_summaries_without_source(self) -> None:
        class Model:
            prompt = ""

            async def structured(self, schema, messages, **_kwargs):
                self.prompt = "\n".join(str(message.content) for message in messages)
                return schema(no_useful_information_reason="なし")

        model = Model()
        target = _page(1, "対象の短い要約")
        reference = _page(2, "参照の短い要約")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _research_references(
                target,
                pages=[target, reference],
                tokens={1: {"対象"}, 2: {"参照"}},
                model=model,
                config=SimpleNamespace(
                    reference_candidates=1,
                    reference_attempts=1,
                    reference_max_output_tokens=100,
                    output_language="Japanese",
                ),
                work_root=root / "work",
                seed_root=root / "seeds",
                stop_check=None,
                on_progress=None,
            )

        self.assertIn("対象の短い要約", model.prompt)
        self.assertIn("参照の短い要約", model.prompt)
        self.assertNotIn("<table>", model.prompt)

    def test_reference_candidate_limit_is_a_total_limit(self) -> None:
        pages = [_page(number) for number in range(1, 10)]
        tokens = {page.number: {"共通", str(page.number)} for page in pages}

        self.assertLessEqual(
            len(_select_references(pages[4], pages, tokens, limit=3)), 3
        )


if __name__ == "__main__":
    unittest.main()
