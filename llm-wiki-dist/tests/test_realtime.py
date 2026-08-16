from __future__ import annotations

import re
import threading
import time
import unittest
from dataclasses import dataclass, field

from graph.neighborhood import NeighborRef
from graph.realtime import (
    PROFILE_MULTIPLIERS,
    RealtimeOptions,
    RealtimePipeline,
    ShallowResearchAnswer,
    classify_question,
)
from graph.vocab import Vocabulary


@dataclass
class FakeNode:
    id: str
    title: str = ""
    summary: str = ""
    body: str = ""
    claims: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    source_path: str = ""
    source_ranges: list = field(default_factory=list)
    status: str = "active"


def result(node_id: str, text: str, **node_fields) -> dict:
    fields = {"title": node_id, "summary": text, "body": text}
    fields.update(node_fields)
    return {
        "node": FakeNode(node_id, **fields),
        "score": 1.0,
        "evidence": [{"field": "small_chunk", "text": text}],
    }


def results(count: int, prefix: str = "node") -> list[dict]:
    return [result(f"{prefix}:{index}", f"Evidence {index}") for index in range(1, count + 1)]


def sourced(count: int, prefix: str, source_path: str) -> list[dict]:
    """Results carrying a document, for the stages that must not leave one."""
    return [
        result(f"{prefix}:{index}", f"Evidence {index}", source_path=source_path)
        for index in range(1, count + 1)
    ]


_NODE_ID_RE = re.compile(r"node_id: (\S+)")


class FakeLlm:
    """Records every prompt and answers with a caller-supplied responder."""

    def __init__(self, responder=None, delays: dict[str, float] | None = None) -> None:
        self._responder = responder or self._echo
        self._delays = delays or {}
        self._lock = threading.Lock()
        self.prompts: list[tuple[str, str]] = []

    def __call__(self) -> "FakeLlm":
        return self

    def complete_structured(self, system: str, user: str, output_model):
        with self._lock:
            self.prompts.append((system, user))
        for marker, delay in self._delays.items():
            if marker in user:
                time.sleep(delay)
        answer = self._responder(system, user)
        return answer if answer is not None else ShallowResearchAnswer()

    @property
    def user_prompts(self) -> list[str]:
        with self._lock:
            return [user for _system, user in self.prompts]

    @staticmethod
    def _echo(_system: str, user: str) -> ShallowResearchAnswer:
        # Every stage prompt lists its sources as `node_id: ...`, so answering
        # with the first one makes each parallel branch identifiable.
        found = _NODE_ID_RE.findall(user)
        if not found:
            return ShallowResearchAnswer()
        return ShallowResearchAnswer(
            answer=f"answer for {found[0]}", node_ids=[found[0]]
        )


class RecordingSearch:
    def __init__(self, table: dict[str, list[dict]] | None = None, default: list[dict] | None = None):
        self.table = table or {}
        self.default = default if default is not None else results(30)
        self.calls: list[tuple[str, int, dict | None]] = []
        self._lock = threading.Lock()

    def __call__(self, text: str, limit: int, profile: dict | None = None) -> list[dict]:
        with self._lock:
            self.calls.append((text, limit, profile))
        for key, value in self.table.items():
            if key in text:
                return value[:limit]
        return self.default[:limit]

    @property
    def queries(self) -> list[str]:
        return [text for text, _limit, _profile in self.calls]


def run_pipeline(**kwargs):
    """Run one pipeline and return (events, summary, llm, search)."""
    llm = kwargs.pop("llm", None) or FakeLlm()
    search = kwargs.pop("search", None) or RecordingSearch()
    options = kwargs.pop("options", None) or RealtimeOptions(max_levels=1)
    question = kwargs.pop("question", "Explain mpf_mfs_open")
    events: list[dict] = []
    summary = RealtimePipeline(llm_factory=llm, search=search, **kwargs).run(
        question, emit=events.append, options=options
    )
    return events, summary, llm, search


class PlanTests(unittest.TestCase):
    def test_plan_precedes_every_generation_and_lists_the_stages(self):
        llm = FakeLlm()
        search = RecordingSearch()
        events: list[dict] = []
        at_plan: dict[str, int] = {}

        def emit(event: dict) -> None:
            if event["type"] == "plan":
                at_plan["searches"] = len(search.calls)
                at_plan["generations"] = len(llm.prompts)
            events.append(event)

        RealtimePipeline(llm_factory=llm, search=search).run(
            "Explain mpf_mfs_open", emit=emit, options=RealtimeOptions(max_levels=3)
        )

        plan = events[0]
        self.assertEqual(plan["type"], "plan")
        self.assertEqual(
            [level["kind"] for level in plan["levels"]],
            ["fast", "deep", "anticipation"],
        )
        # Planning is retrieval, not generation: one search, no model call.
        self.assertEqual(at_plan, {"searches": 1, "generations": 0})
        self.assertEqual(plan["planning_fallback"], False)

    def test_plan_carries_candidates_and_the_pinned_identifier(self):
        vocabulary = Vocabulary.from_rows(
            [{"title": "mpf_mfs_open", "cluster": "MFS", "body": "mpf_mfs_open()"}]
        )
        events, _summary, _llm, search = run_pipeline(
            question="エムピーエフ エムエフエス オープン の第3引数は？",
            vocabulary=vocabulary,
        )

        plan = events[0]
        self.assertEqual(plan["pinned_identifier"], "mpf_mfs_open")
        self.assertIn("mpf_mfs_open", search.queries[0])
        self.assertEqual(plan["candidates"][0]["node_id"], "node:1")
        self.assertEqual(plan["question_type"], "named")

    def test_named_question_leans_the_weights_lexical(self):
        _events, _summary, _llm, search = run_pipeline(
            question="mpf_mfs_open の第3引数は何ですか"
        )

        _text, _limit, profile = search.calls[0]
        self.assertEqual(profile, PROFILE_MULTIPLIERS["named"])
        self.assertGreater(profile["weight_node_bm25"], 1.0)
        self.assertLess(profile["weight_body_vec"], 1.0)

    def test_list_question_widens_the_item_pool(self):
        _events, _summary, _llm, search = run_pipeline(
            question="処理要求に必要なファイルは何ですか"
        )

        _text, _limit, profile = search.calls[0]
        self.assertEqual(profile, PROFILE_MULTIPLIERS["list"])
        self.assertGreater(profile["pool_item_bm25"], 1.0)

    def test_question_shape_classification(self):
        self.assertEqual(classify_question("mpf_mfs_open の第3引数は"), "named")
        self.assertEqual(classify_question("必要なファイルは何ですか"), "list")
        self.assertEqual(classify_question("これは何のための仕組みですか"), "concept")


class FastAnswerTests(unittest.TestCase):
    def test_four_shards_read_four_slices_and_are_concatenated(self):
        events, _summary, llm, _search = run_pipeline()

        level = next(event for event in events if event["type"] == "level")
        self.assertEqual(len(llm.prompts), 4)
        self.assertEqual(len(level["facts"]), 4)
        # Slices are 5 / 5 / 10 / 10 over the reranked 30.
        self.assertEqual(
            level["text"],
            "answer for node:1\n\nanswer for node:6\n\n"
            "answer for node:11\n\nanswer for node:21",
        )
        self.assertEqual(
            level["reference_node_ids"],
            ["node:1", "node:6", "node:11", "node:21"],
        )

    def test_detail_shards_get_bodies_and_wide_shards_get_claims(self):
        search = RecordingSearch(
            default=[
                result(
                    f"node:{index}",
                    f"Evidence {index}",
                    body=f"BODY-{index}",
                    claims=[f"CLAIM-{index}"],
                    keywords=[f"KEY-{index}"],
                )
                for index in range(1, 31)
            ]
        )
        _events, _summary, llm, _search = run_pipeline(search=search)

        detail = next(p for p in llm.user_prompts if "node_id: node:1\n" in p)
        wide = next(p for p in llm.user_prompts if "node_id: node:11\n" in p)
        self.assertIn("BODY-1", detail)
        self.assertNotIn("claims:", detail)
        self.assertIn("CLAIM-11", wide)
        self.assertIn("KEY-11", wide)
        self.assertNotIn("BODY-11", wide)

    def test_a_slow_shard_shortens_the_answer_instead_of_delaying_it(self):
        llm = FakeLlm(delays={"node_id: node:21": 2.0})
        started = time.perf_counter()
        events, _summary, _llm, _search = run_pipeline(
            llm=llm,
            options=RealtimeOptions(max_levels=1, shard_deadline_seconds=0.4),
        )
        elapsed = time.perf_counter() - started

        level = next(event for event in events if event["type"] == "level")
        self.assertLess(elapsed, 1.5)
        self.assertEqual(len(level["facts"]), 3)
        self.assertNotIn("node:21", level["text"])

    def test_shard_count_one_still_answers(self):
        events, _summary, llm, _search = run_pipeline(
            options=RealtimeOptions(max_levels=1, shard_count=1)
        )

        level = next(event for event in events if event["type"] == "level")
        self.assertEqual(len(llm.prompts), 1)
        self.assertTrue(level["text"])

    def test_shards_offer_neighbours_and_accept_citations_to_them(self):
        neighbour = NeighborRef(
            node_id="node:99",
            title="pmf_procdata.txt",
            summary="rest of the same page",
            label="follows",
            relation="chain",
        )

        def neighbors(node_ids, **_kwargs):
            return {node_id: [neighbour] for node_id in node_ids}

        def responder(_system, user):
            if "node:99" in user:
                return ShallowResearchAnswer(
                    answer="Also register pmf_procdata.txt.", node_ids=["node:99"]
                )
            return ShallowResearchAnswer()

        events, _summary, llm, _search = run_pipeline(
            llm=FakeLlm(responder), neighbors=neighbors
        )

        level = next(event for event in events if event["type"] == "level")
        self.assertIn(
            "- [follows] node:99 | pmf_procdata.txt | rest of the same page",
            llm.user_prompts[0],
        )
        self.assertIn("node:99", level["reference_node_ids"])

    def test_neighbour_admission_respects_the_ceiling(self):
        refs = [
            NeighborRef(node_id=f"node:n{index}", title=f"N{index}")
            for index in range(20)
        ]

        def neighbors(node_ids, *, limit, **_kwargs):
            return {node_id: refs[:limit] for node_id in node_ids}

        _events, _summary, llm, _search = run_pipeline(
            neighbors=neighbors,
            options=RealtimeOptions(max_levels=1, neighbor_max_admit=3),
        )

        block = [p for p in llm.user_prompts if "Related nodes" in p][0]
        self.assertEqual(block.count("- ["), 3)

    def test_a_starved_shard_falls_back_to_the_rest_of_the_page(self):
        # The cheap one-hop walk finds nothing, so the floor forces a second
        # lookup that also considers same-page siblings.
        sibling = NeighborRef(
            node_id="node:page2", title="same page", relation="sibling", label="same-page"
        )
        calls: list[bool] = []

        def neighbors(node_ids, *, chain_hops, typed_hops, siblings, limit):
            calls.append(siblings)
            if not siblings:
                return {node_id: [] for node_id in node_ids}
            return {node_id: [sibling] for node_id in node_ids}

        _events, _summary, llm, _search = run_pipeline(
            neighbors=neighbors,
            options=RealtimeOptions(
                max_levels=1, neighbor_min_admit=1, neighbor_max_admit=4
            ),
        )

        self.assertEqual(calls, [False, True])
        self.assertIn("node:page2", llm.user_prompts[0])

    def test_no_extra_lookup_when_the_floor_is_already_met(self):
        ref = NeighborRef(node_id="node:99", title="linked", relation="typed")
        calls: list[bool] = []

        def neighbors(node_ids, *, chain_hops, typed_hops, siblings, limit):
            calls.append(siblings)
            return {node_id: [ref] for node_id in node_ids}

        run_pipeline(
            neighbors=neighbors,
            options=RealtimeOptions(
                max_levels=1, neighbor_min_admit=1, neighbor_max_admit=4
            ),
        )

        self.assertEqual(calls, [False])

    def test_scope_line_pins_the_identifier_in_every_shard(self):
        vocabulary = Vocabulary.from_rows([{"title": "mpf_mfs_open", "body": "x"}])
        _events, _summary, llm, _search = run_pipeline(
            question="mpf_mfs_open の第3引数", vocabulary=vocabulary
        )

        for prompt in llm.user_prompts:
            self.assertIn("mpf_mfs_open", prompt)
            self.assertIn("no similarly", prompt)


class DisclaimerTests(unittest.TestCase):
    """No sentence about what the material lacks may reach a speaking client."""

    def clean(self, text: str) -> str:
        return RealtimePipeline._clean_fact_text(text, {})

    def test_a_disclaimer_without_the_usual_opening_is_still_dropped(self):
        # Observed in production: the stripper wanted 「提供された資料」 and an
        # 「ありません」 ending, so a sentence that opened with 「資料には」 and
        # closed with 「言及されていません」 was streamed and spoken.
        self.assertEqual(
            self.clean("資料には割り当て挙動の差異については言及されていません。"), ""
        )
        self.assertEqual(self.clean("この点については記載されていません。"), "")

    def test_the_marker_matters_more_than_the_noun_after_it(self):
        # Listing document nouns was whack-a-mole: 資料 was covered and ソース
        # was not, so the identical sentence shape leaked a second time. What
        # actually marks a disclaimer is 提供された/与えられた — the sentence is
        # about the evidence handed to the model, whatever noun follows.
        for noun in ("ソース", "資料", "情報", "コンテキスト", "テキスト"):
            with self.subTest(noun=noun):
                self.assertEqual(
                    self.clean(f"提供された{noun}には、その種類のリストは含まれていません。"),
                    "",
                )

    def test_a_negation_without_a_document_marker_is_content(self):
        # 「規定するものではありません」 ends in ありません and is the substance
        # of the answer; only the document reference makes a sentence a
        # disclaimer.
        sentence = "このヒントはデータがどこに常駐すべきかを規定するものではありません。"
        self.assertEqual(self.clean(sentence), sentence)

    def test_only_the_disclaiming_sentence_is_removed(self):
        cleaned = self.clean(
            "compute capability 5.xはGPU上に割り当てます。"
            "資料にはその差異について言及されていません。"
            "6.x以降はfirst touch時に配置します。"
        )
        self.assertEqual(
            cleaned,
            "compute capability 5.xはGPU上に割り当てます。6.x以降はfirst touch時に配置します。",
        )

    def test_ordinary_technical_negatives_survive(self):
        # A bare negation is content, not a disclaimer. Stripping these would
        # delete exactly the constraints the answer exists to convey.
        for sentence in (
            "compute capability 6.0未満はオンデマンド移行をサポートしていません。",
            "GPUメモリのサイズを超えるManaged Memoryを割り当てることはできません。",
            "デバイスコードから直接呼び出すことはできません。",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(self.clean(sentence), sentence)


class GroundingTests(unittest.TestCase):
    def test_unretrieved_citations_are_not_streamed(self):
        llm = FakeLlm(
            lambda _s, _u: ShallowResearchAnswer(
                answer="Invented claim.", node_ids=["node:invented"]
            )
        )
        events, summary, _llm, _search = run_pipeline(
            llm=llm, search=RecordingSearch(default=results(1))
        )

        level = next(event for event in events if event["type"] == "level")
        # The text survives, attributed to the slice it was given; the invented
        # ID never reaches the client.
        self.assertNotIn("node:invented", level["reference_node_ids"])
        self.assertEqual(level["reference_node_ids"], ["node:1"])
        self.assertEqual(summary["status"], "complete")

    def test_disclaimer_sentences_never_reach_the_level_text(self):
        llm = FakeLlm(
            lambda _s, _u: ShallowResearchAnswer(
                answer=(
                    "### 第3引数\n第3引数は `bufsize` です。\n\n"
                    "提供された資料には具体的な記載はありません。"
                ),
                node_ids=["node:1"],
            )
        )
        events, _summary, _llm, _search = run_pipeline(
            llm=llm, search=RecordingSearch(default=results(1))
        )

        level = next(event for event in events if event["type"] == "level")
        self.assertIn("bufsize", level["text"])
        self.assertNotIn("提供された資料", level["text"])

    def test_identical_shard_answers_are_not_repeated(self):
        llm = FakeLlm(
            lambda _s, _u: ShallowResearchAnswer(answer="Same text.", node_ids=["node:1"])
        )
        events, _summary, _llm, _search = run_pipeline(llm=llm)

        level = next(event for event in events if event["type"] == "level")
        self.assertEqual(level["text"], "Same text.")

    def test_evidence_budget_reads_later_concise_required_source(self):
        long_results = [
            {
                "node": FakeNode(
                    f"node:long:{index}",
                    f"Long {index}",
                    "overview",
                    "unrelated overview " * 4_000,
                ),
                "evidence": [{"text": "overview match"}],
            }
            for index in range(7)
        ]
        required = {
            "node": FakeNode(
                "node:required",
                "Processing-request registration",
                "required files",
                (
                    "For a processing request, register pmf_prg.txt and "
                    "pmf_procdata.txt, then register and create "
                    "mpf_mfs_cyclicfile.txt."
                ),
            ),
            "evidence": [{"text": "pmf_prg.txt pmf_procdata.txt mpf_mfs_cyclicfile.txt"}],
        }

        rendered = RealtimePipeline._format_evidence([*long_results, required], 32_000)

        self.assertIn("node_id: node:required", rendered)
        self.assertIn("pmf_prg.txt", rendered)
        self.assertIn("pmf_procdata.txt", rendered)
        self.assertIn("mpf_mfs_cyclicfile.txt", rendered)

    def test_evidence_includes_node_body_even_when_match_snippets_exist(self):
        rendered = RealtimePipeline._format_evidence(
            [
                {
                    "node": FakeNode(
                        "node:open",
                        "Open",
                        "summary",
                        "signature int open(..., int filenum, ...)",
                    ),
                    "evidence": [{"text": "nearby paragraph"}],
                }
            ],
            10_000,
        )
        self.assertIn("signature int open", rendered)
        self.assertIn("nearby paragraph", rendered)


class RerankTests(unittest.TestCase):
    def test_wide_net_is_reranked_down_to_the_working_set(self):
        search = RecordingSearch(default=results(100))
        seen: dict[str, object] = {}

        def rerank(query, items, k):
            seen["count"] = len(items)
            # Reverse the ranking so the effect is unmistakable.
            return [(index, 1.0) for _text, index in reversed(list(items))][:k]

        events, _summary, _llm, _search = run_pipeline(search=search, rerank=rerank)

        plan = events[0]
        self.assertEqual(seen["count"], 100)
        self.assertEqual(plan["candidates"][0]["node_id"], "node:100")

    def test_rerank_failure_keeps_the_rrf_order(self):
        def rerank(_query, _items, _k):
            raise RuntimeError("reranker down")

        events, _summary, _llm, _search = run_pipeline(
            search=RecordingSearch(default=results(100)), rerank=rerank
        )

        plan = events[0]
        self.assertEqual(plan["candidates"][0]["node_id"], "node:1")
        self.assertEqual(len(plan["levels"]), 1)


class DeepStageTests(unittest.TestCase):
    def _neighbors(self, refs: list[NeighborRef]):
        def neighbors(node_ids, *, chain_hops, typed_hops, siblings, limit):
            # Only the deep call asks for siblings and multiple chain hops.
            if not siblings:
                return {node_id: [] for node_id in node_ids}
            return {list(node_ids)[0]: refs[:limit]}

        return neighbors

    def test_deep_level_reads_subgraph_nodes_the_fast_answer_missed(self):
        refs = [
            NeighborRef(
                node_id="node:page2",
                title="pmf_procdata.txt",
                summary="the rest of the list",
                label="follows",
                relation="chain",
            )
        ]
        loaded: list[list[str]] = []

        def load_nodes(node_ids):
            loaded.append(list(node_ids))
            return [FakeNode("node:page2", "pmf_procdata.txt", body="register pmf_procdata.txt")]

        events, summary, llm, _search = run_pipeline(
            neighbors=self._neighbors(refs),
            load_nodes=load_nodes,
            options=RealtimeOptions(max_levels=2, shard_deadline_seconds=3.0),
        )

        levels = [event for event in events if event["type"] == "level"]
        self.assertEqual(len(levels), 2)
        self.assertEqual(loaded, [["node:page2"]])
        self.assertIn("node:page2", levels[1]["reference_node_ids"])
        self.assertEqual(summary["levels_completed"], 2)

    def test_deep_agent_is_used_when_one_is_wired(self):
        refs = [NeighborRef(node_id="node:page2", title="p2", relation="chain")]
        calls: list[dict] = []

        def deep_agent(*, question, node_id, sibling_ids, index, stop_event, extra_instructions=""):
            calls.append({"node_id": node_id, "index": index})
            return {"answer": "agent finding", "cited": ["node:agentread"]}

        events, _summary, _llm, _search = run_pipeline(
            neighbors=self._neighbors(refs),
            deep_agent=deep_agent,
            options=RealtimeOptions(max_levels=2, subagent_count=1),
        )

        levels = [event for event in events if event["type"] == "level"]
        self.assertEqual(calls, [{"node_id": "node:page2", "index": 1}])
        self.assertIn("agent finding", levels[1]["text"])
        # An agent cites what it opened, which becomes citable for this run.
        self.assertIn("node:agentread", levels[1]["reference_node_ids"])

    def test_one_plain_reader_runs_beside_the_agents(self):
        # An agent loop can miss the harvest; the reader is one generation and
        # keeps the level from being empty when that happens.
        refs = [
            NeighborRef(node_id="node:page2", title="p2", summary="rest of the page",
                        relation="chain")
        ]

        def slow_agent(*, question, node_id, sibling_ids, index, stop_event, extra_instructions=""):
            time.sleep(2.0)
            return {"answer": "too late", "cited": [node_id]}

        events, _summary, llm, _search = run_pipeline(
            neighbors=self._neighbors(refs),
            deep_agent=slow_agent,
            load_nodes=lambda ids: [FakeNode(i, i, body="continuation text") for i in ids],
            options=RealtimeOptions(
                max_levels=2, subagent_count=1, deep_deadline_seconds=0.6
            ),
        )

        levels = [event for event in events if event["type"] == "level"]
        self.assertNotIn("too late", levels[1]["text"])
        self.assertIn("node:page2", levels[1]["reference_node_ids"])
        self.assertTrue(
            any("Neighbouring sources" in prompt for prompt in llm.user_prompts)
        )

    def test_deep_stage_emits_discovery_only_for_unseen_nodes(self):
        refs = [
            NeighborRef(node_id="node:1", title="already read", relation="chain"),
            NeighborRef(node_id="node:new", title="新しいページ", relation="chain"),
        ]

        def neighbors(node_ids, *, chain_hops, typed_hops, siblings, limit):
            if not siblings:
                return {node_id: [] for node_id in node_ids}
            return {list(node_ids)[0]: refs}

        events, _summary, _llm, _search = run_pipeline(
            neighbors=neighbors,
            load_nodes=lambda ids: [FakeNode(i, i, body="text") for i in ids],
            options=RealtimeOptions(max_levels=2),
        )

        discoveries = [event for event in events if event["type"] == "discovery"]
        self.assertEqual(len(discoveries), 1)
        self.assertEqual(discoveries[0]["node_ids"], ["node:new"])
        self.assertTrue(discoveries[0]["speakable"])
        self.assertIn("読んでいます", discoveries[0]["text"])

    def test_deep_stage_without_a_subgraph_is_simply_empty(self):
        events, summary, _llm, _search = run_pipeline(
            options=RealtimeOptions(max_levels=2)
        )

        levels = [event for event in events if event["type"] == "level"]
        self.assertEqual(len(levels), 2)
        self.assertEqual(levels[1]["facts"], [])
        self.assertEqual(summary["status"], "complete")


class AnticipationTests(unittest.TestCase):
    def test_terms_the_answer_used_but_did_not_explain_are_looked_up(self):
        vocabulary = Vocabulary.from_rows(
            [{"title": "mpf_mfs_open", "body": "the third argument is `filenum`"}]
        )
        search = RecordingSearch(
            table={"filenum": results(3, "node:filenum")}, default=results(30)
        )

        def responder(system, user):
            if "Term to explain" in system or "term" in system.lower():
                return ShallowResearchAnswer(
                    answer="filenum is the file number.",
                    node_ids=[_NODE_ID_RE.findall(user)[0]],
                )
            return ShallowResearchAnswer(
                answer="The third argument is filenum.",
                node_ids=[_NODE_ID_RE.findall(user)[0]],
            )

        events, _summary, _llm, _search = run_pipeline(
            question="mpf_mfs_open の第3引数は",
            llm=FakeLlm(responder),
            search=search,
            vocabulary=vocabulary,
            options=RealtimeOptions(max_levels=3),
        )

        levels = [event for event in events if event["type"] == "level"]
        self.assertEqual(len(levels), 3)
        self.assertIn("filenum", search.queries[-1])
        self.assertIn("filenum is the file number.", levels[2]["text"])

    def test_terms_outside_the_corpus_vocabulary_are_ignored(self):
        vocabulary = Vocabulary.from_rows([{"title": "mpf_mfs_open", "body": "x"}])
        llm = FakeLlm(
            lambda _s, u: ShallowResearchAnswer(
                answer="See the Markdown appendix for details.",
                node_ids=[_NODE_ID_RE.findall(u)[0]],
            )
        )
        search = RecordingSearch()

        events, _summary, _llm, _search = run_pipeline(
            question="mpf_mfs_open とは",
            llm=llm,
            search=search,
            vocabulary=vocabulary,
            options=RealtimeOptions(max_levels=3),
        )

        levels = [event for event in events if event["type"] == "level"]
        self.assertEqual(levels[2]["facts"], [])
        self.assertNotIn("Markdown", " ".join(search.queries))

    def test_generic_keywords_in_the_corpus_are_not_chased(self):
        # Enrichment fills keywords_json with ordinary nouns, so mere presence
        # in the vocabulary is not evidence that a term is worth a stage. A
        # search for "System" returns whichever document in the corpus defines
        # the word, which is how a CUDA answer acquired a paragraph of OpenMP.
        vocabulary = Vocabulary.from_rows(
            [
                {
                    "title": "Unified Memory",
                    "keywords": ["System", "Allocated", "Compute"],
                    "body": "Managed memory is allocated with `cudaMallocManaged`.",
                }
            ]
        )
        llm = FakeLlm(
            lambda _s, u: ShallowResearchAnswer(
                answer="The System reports memory as Allocated during Compute.",
                node_ids=[_NODE_ID_RE.findall(u)[0]],
            )
        )
        search = RecordingSearch()

        events, _summary, _llm, _search = run_pipeline(
            question="マネージドメモリの割り当てについて",
            llm=llm,
            search=search,
            vocabulary=vocabulary,
            options=RealtimeOptions(max_levels=3),
        )

        levels = [event for event in events if event["type"] == "level"]
        self.assertEqual(levels[2]["facts"], [])
        issued = " ".join(search.queries)
        for word in ("System", "Allocated", "Compute"):
            self.assertNotIn(word, issued)

    def test_identifiers_outrank_more_frequent_generic_terms(self):
        # Frequency ranks backwards: the most repeated term in an answer is its
        # subject, already explained. The name used once in passing is the one
        # about to be asked about.
        vocabulary = Vocabulary.from_rows(
            [
                {
                    "title": "Unified Memory",
                    "keywords": ["System"],
                    "body": "Allocate with `cudaMallocManaged` and read `filenum`.",
                }
            ]
        )
        llm = FakeLlm(
            lambda _s, u: ShallowResearchAnswer(
                answer="System, System, System and one call to cudaMallocManaged.",
                node_ids=[_NODE_ID_RE.findall(u)[0]],
            )
        )
        search = RecordingSearch()

        run_pipeline(
            question="マネージドメモリとは",
            llm=llm,
            search=search,
            vocabulary=vocabulary,
            options=RealtimeOptions(max_levels=3, anticipation_terms=1),
        )

        self.assertIn("cudaMallocManaged", search.queries[-1])

    def test_anticipation_stays_in_the_documents_the_answer_came_from(self):
        # The reported failure: a one-word follow-up search left the CUDA guide
        # and returned the same word defined in the OpenMP spec, which the
        # speaker then wove into the answer as though the two were related.
        vocabulary = Vocabulary.from_rows(
            [{"title": "Unified Memory", "body": "Use `cudaMallocManaged` to allocate."}]
        )
        search = RecordingSearch(
            table={"cudaMallocManaged": sourced(3, "node:omp", "openmp-api-v6-0.md")},
            default=sourced(30, "node:cuda", "cuda-c.md"),
        )
        llm = FakeLlm(
            lambda _s, u: ShallowResearchAnswer(
                answer="Memory is allocated with cudaMallocManaged.",
                node_ids=[_NODE_ID_RE.findall(u)[0]],
            )
        )

        events, _summary, _llm, _search = run_pipeline(
            question="マネージドメモリの割り当て",
            llm=llm,
            search=search,
            vocabulary=vocabulary,
            options=RealtimeOptions(max_levels=3),
        )

        levels = [event for event in events if event["type"] == "level"]
        self.assertEqual(levels[2]["facts"], [])
        # The term was still looked up; only its out-of-corpus hits were dropped.
        self.assertIn("cudaMallocManaged", search.queries[-1])

    def test_a_term_documented_in_the_same_book_is_still_explained(self):
        # The scope filter must not empty the stage whenever source paths exist.
        vocabulary = Vocabulary.from_rows(
            [{"title": "Unified Memory", "body": "Use `cudaMallocManaged` to allocate."}]
        )
        search = RecordingSearch(
            table={"cudaMallocManaged": sourced(3, "node:api", "cuda-c.md")},
            default=sourced(30, "node:cuda", "cuda-c.md"),
        )

        def responder(system, user):
            explaining = "Term to explain" in user
            return ShallowResearchAnswer(
                answer=(
                    "cudaMallocManaged takes a size in bytes."
                    if explaining
                    else "Memory is allocated with cudaMallocManaged."
                ),
                node_ids=[_NODE_ID_RE.findall(user)[0]],
            )

        llm = FakeLlm(responder)

        events, _summary, _llm, _search = run_pipeline(
            question="マネージドメモリの割り当て",
            llm=llm,
            search=search,
            vocabulary=vocabulary,
            options=RealtimeOptions(max_levels=3),
        )

        levels = [event for event in events if event["type"] == "level"]
        self.assertTrue(levels[2]["facts"])
        self.assertTrue(
            any(
                node_id.startswith("node:api")
                for node_id in levels[2]["reference_node_ids"]
            )
        )


class BudgetTests(unittest.TestCase):
    def test_expired_run_budget_skips_the_remaining_levels(self):
        def slow_search(_text, _limit, _profile=None):
            time.sleep(0.2)
            return results(4)

        events: list[dict] = []
        summary = RealtimePipeline(llm_factory=FakeLlm(), search=slow_search).run(
            "Explain the first and second parts of this system",
            emit=events.append,
            options=RealtimeOptions(max_levels=3, deadline_seconds=0.1),
        )

        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["incomplete_reason"], "deadline")
        self.assertTrue(any(event["type"] == "plan_update" for event in events))

    def test_cancelled_run_raises_between_operations(self):
        from graph.realtime import RealtimeStopped

        stop = threading.Event()
        stop.set()
        with self.assertRaises(RealtimeStopped):
            RealtimePipeline(llm_factory=FakeLlm(), search=RecordingSearch()).run(
                "Question", emit=lambda _event: None, stop_event=stop
            )

    def test_search_port_without_a_profile_argument_still_works(self):
        calls: list[tuple[str, int]] = []

        def legacy_search(text, limit):
            calls.append((text, limit))
            return results(6)

        events, _summary, _llm, _search = run_pipeline(search=legacy_search)

        self.assertEqual(len(calls), 1)
        self.assertTrue(next(e for e in events if e["type"] == "level")["text"])


if __name__ == "__main__":
    unittest.main()
