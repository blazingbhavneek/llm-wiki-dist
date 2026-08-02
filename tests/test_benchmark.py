from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import benchmark


def make_question(
    number: int,
    question_type: str = "Fact Retrieval",
    *,
    answer: str = "Paris",
) -> benchmark.Question:
    return benchmark.Question(
        id=f"q-{number:03d}",
        source="novel-1",
        question=f"Question {number}?",
        answer=answer,
        question_type=question_type,
        evidence="supporting passage",
    )


class FakeResponse:
    def __init__(self, value: dict[str, object]) -> None:
        self.body = json.dumps(value).encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
    del timeout
    payload = json.loads(request.data)
    url = request.get_full_url()
    if url.endswith("/embeddings"):
        inputs = payload["input"]
        data = []
        for index, text in enumerate(inputs):
            lowered = str(text).casefold()
            vector = (
                [1.0, 0.0]
                if "paris" in lowered or "france" in lowered
                else [0.0, 1.0]
            )
            data.append({"index": index, "embedding": vector})
        return FakeResponse(
            {
                "data": data,
                "usage": {
                    "prompt_tokens": len(inputs),
                    "total_tokens": len(inputs),
                },
            }
        )
    if url.endswith("/chat/completions"):
        return FakeResponse(
            {
                "choices": [{"message": {"content": "Paris"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 1,
                    "total_tokens": 11,
                },
            }
        )
    raise AssertionError(f"unexpected URL: {url}")


class DatasetTests(unittest.TestCase):
    def test_loads_graphrag_bench_json_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus.json"
            questions = root / "questions.json"
            corpus.write_text(
                json.dumps(
                    [{"corpus_name": "novel-1", "context": "Paris is in France."}]
                ),
                encoding="utf-8",
            )
            questions.write_text(
                json.dumps(
                    [
                        {
                            "id": "q-1",
                            "source": "novel-1",
                            "question": "What is the capital?",
                            "answer": "Paris",
                            "question_type": "Fact Retrieval",
                            "evidence": "Paris is in France.",
                            "evidence_triple": "France|capital|Paris",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            documents = benchmark.load_documents(corpus)
            loaded_questions = benchmark.load_questions(questions)

        self.assertEqual(documents, [benchmark.Document("novel-1", "Paris is in France.")])
        self.assertEqual(loaded_questions[0].answer, "Paris")
        self.assertEqual(loaded_questions[0].evidence_triple, "France|capital|Paris")

    def test_sampling_is_deterministic_and_stratified(self) -> None:
        questions = [
            *(make_question(index, "Fact Retrieval") for index in range(100)),
            *(make_question(index + 100, "Complex Reasoning") for index in range(100)),
        ]

        first = benchmark.select_questions(
            questions,
            allowed_types=benchmark.DEFAULT_QUESTION_TYPES,
            sample=20,
            seed=7,
        )
        second = benchmark.select_questions(
            questions,
            allowed_types=benchmark.DEFAULT_QUESTION_TYPES,
            sample=20,
            seed=7,
        )

        self.assertEqual([item.id for item in first], [item.id for item in second])
        counts = {name: 0 for name in benchmark.DEFAULT_QUESTION_TYPES}
        for question in first:
            counts[question.question_type] += 1
        self.assertEqual(counts, {"Fact Retrieval": 10, "Complex Reasoning": 10})

    def test_all_novel_sampling_covers_and_balances_each_novel(self) -> None:
        documents = [
            benchmark.Document("novel-1", "first"),
            benchmark.Document("novel-2", "second"),
        ]
        questions = []
        for source_index, source in enumerate(("novel-1", "novel-2")):
            for type_index, question_type in enumerate(
                benchmark.DEFAULT_QUESTION_TYPES
            ):
                question = make_question(
                    source_index * 10 + type_index,
                    question_type,
                )
                questions.append(
                    benchmark.Question(
                        **{**question.__dict__, "source": source}
                    )
                )

        selected = benchmark.select_all_novel_questions(
            documents,
            questions,
            per_novel=2,
            seed=7,
        )

        self.assertEqual(len(selected), 4)
        self.assertEqual(
            {question.source for question in selected},
            {"novel-1", "novel-2"},
        )
        self.assertEqual(
            {
                (question.source, question.question_type)
                for question in selected
            },
            {
                (source, question_type)
                for source in ("novel-1", "novel-2")
                for question_type in benchmark.DEFAULT_QUESTION_TYPES
            },
        )

    def test_isolated_scope_keeps_only_referenced_corpora(self) -> None:
        documents = [
            benchmark.Document("novel-1", "first"),
            benchmark.Document("novel-2", "second"),
        ]
        selected = benchmark.documents_for_questions(
            documents,
            [make_question(1)],
            corpus_scope="isolated",
        )
        self.assertEqual(selected, [documents[0]])

    def test_isolated_scope_rejects_unmatched_sources(self) -> None:
        question = make_question(1)
        question = benchmark.Question(
            **{**question.__dict__, "source": "missing"}
        )
        with self.assertRaisesRegex(benchmark.BenchmarkError, "question.source"):
            benchmark.documents_for_questions(
                [benchmark.Document("novel-1", "first")],
                [question],
                corpus_scope="isolated",
            )

    def test_long_single_line_prose_gets_shared_sentence_boundaries(self) -> None:
        text = " ".join(
            f"Sentence {index} contains enough ordinary prose."
            for index in range(700)
        )
        document = benchmark.Document("novel-1", text)

        normalized = benchmark.normalize_long_prose_layout(document)

        self.assertGreater(
            len(normalized.text.splitlines()),
            benchmark.OURS_CHUNK_THRESHOLD_LINES,
        )
        self.assertEqual(normalized.text.replace("\n", " "), text)

    def test_short_document_layout_is_unchanged(self) -> None:
        document = benchmark.Document("novel-1", "A short document.")
        self.assertIs(benchmark.normalize_long_prose_layout(document), document)

    def test_long_article_below_line_threshold_is_still_normalized(self) -> None:
        text = " ".join(
            f"Article sentence {index} contains several ordinary words."
            for index in range(250)
        )
        text = f"{text} {'context ' * 1200}".strip()

        normalized = benchmark.normalize_long_prose_layout(
            benchmark.Document("news-1", text)
        )

        self.assertGreater(len(normalized.text.splitlines()), 1)
        self.assertEqual(normalized.text.replace("\n", " "), text)
        self.assertTrue(
            benchmark.needs_native_conceptual_chunking(
                normalized.text,
                len(normalized.text.splitlines()),
            )
        )

    def test_fanout_adapter_builds_shared_revision_corpus(self) -> None:
        evidence = [
            {
                "pageid": index,
                "revid": index + 100,
                "title": f"Page {index}",
                "url": f"https://example.invalid/{index}",
            }
            for index in range(1, 5)
        ]
        question = {
            "id": "fanout-1",
            "question": "Combine four facts.",
            "answer": {"one": 1, "two": 2},
            "categories": ["Test"],
            "decomposition": [
                {"evidence": item, "decomposition": []} for item in evidence
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            questions_path = root / "fanout.json"
            questions_path.write_text(json.dumps([question]), encoding="utf-8")
            args = benchmark.fixed_args("http://model.invalid/v1", "fanout")
            args.questions = str(questions_path)
            args.corpus = str(root / "revisions")
            args.sample = 1
            with mock.patch.object(
                benchmark,
                "load_fanout_revision_text",
                side_effect=lambda item, _path: f"text for {item['title']}",
            ):
                bundle = benchmark.load_fanout_dataset(args)

        self.assertEqual(bundle.corpus_scope, "combined")
        self.assertEqual(len(bundle.documents), 4)
        self.assertEqual(bundle.questions[0].source, "fanout")
        self.assertEqual(bundle.questions[0].answer, '{"one": 1, "two": 2}')

    def test_wikipedia_html_text_keeps_table_values(self) -> None:
        text = benchmark.wikipedia_html_text(
            "<style>hidden</style><table><tr><th>City</th><td>Paris</td>"
            "</tr></table><p>Capital of France.</p>"
        )
        self.assertNotIn("hidden", text)
        self.assertIn("City", text)
        self.assertIn("Paris", text)
        self.assertIn("Capital of France.", text)

    def test_multihop_adapter_uses_official_news_schema(self) -> None:
        corpus_row = {
            "title": "A title",
            "source": "A source",
            "published_at": "2024-01-01T00:00:00Z",
            "body": "The complete article body.",
        }
        question_row = {
            "query": "What follows from both reports?",
            "answer": "An answer",
            "question_type": "inference_query",
            "evidence_list": [{**corpus_row, "fact": "A fact"}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus_path = root / "corpus.json"
            questions_path = root / "questions.json"
            corpus_path.write_text(json.dumps([corpus_row]), encoding="utf-8")
            questions_path.write_text(
                json.dumps([question_row]), encoding="utf-8"
            )
            args = benchmark.fixed_args("http://model.invalid/v1", "multihop")
            args.corpus = str(corpus_path)
            args.questions = str(questions_path)
            args.sample = 1
            bundle = benchmark.load_multihop_dataset(args)

        self.assertEqual(bundle.corpus_scope, "combined")
        self.assertEqual(len(bundle.documents), 1)
        self.assertIn("Published: 2024-01-01", bundle.documents[0].text)
        self.assertEqual(bundle.questions[0].question_type, "inference_query")

    def test_musique_adapter_unions_selected_contexts(self) -> None:
        row = {
            "id": "2hop-test",
            "question": "Which city is implied?",
            "answer": "Paris",
            "answerable": True,
            "question_decomposition": [{"question": "one"}, {"question": "two"}],
            "paragraphs": [
                {
                    "idx": 0,
                    "title": "France",
                    "paragraph_text": "France is a country.",
                    "is_supporting": True,
                },
                {
                    "idx": 1,
                    "title": "Paris",
                    "paragraph_text": "Paris is its capital.",
                    "is_supporting": False,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "musique.jsonl"
            benchmark.jsonl_dump(path, [row])
            args = benchmark.fixed_args("http://model.invalid/v1", "musique")
            args.questions = str(path)
            args.sample = 1
            bundle = benchmark.load_musique_dataset(args)

        self.assertEqual(bundle.corpus_scope, "combined")
        self.assertEqual(len(bundle.documents), 2)
        self.assertEqual(bundle.questions[0].question_type, "2-hop")
        self.assertEqual(bundle.questions[0].evidence[0]["title"], "France")


class VanillaRagTests(unittest.TestCase):
    def test_chunking_does_not_emit_an_overlap_only_tail(self) -> None:
        text = " ".join(f"word-{index}" for index in range(12))
        with mock.patch.dict(sys.modules, {"tiktoken": None}):
            chunks, unit = benchmark.fixed_chunks(text, size=10, overlap=2)

        self.assertEqual(unit, "whitespace tokens")
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0].split()), 10)
        self.assertEqual(len(chunks[1].split()), 4)

    def test_vanilla_ingest_and_answer_against_openai_compatible_api(self) -> None:
        base_url = "http://model.invalid/v1"
        args = SimpleNamespace(
            embed_base_url=base_url,
            embed_api_key="local",
            embed_model="fake-embed",
            embed_dim=2,
            embed_batch_size=8,
            timeout=5,
            chunk_tokens=8,
            chunk_overlap=2,
            chat_base_url=base_url,
            chat_api_key="local",
            chat_model="fake-chat",
            top_k=1,
            temperature=0.0,
            max_answer_tokens=100,
            chat_concurrency=12,
        )
        with mock.patch.object(benchmark.urllib.request, "urlopen", fake_urlopen):
            with tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                metrics = benchmark.vanilla_ingest(
                    workspace,
                    [
                        benchmark.Document("france", "Paris is the capital of France."),
                        benchmark.Document("other", "Berlin is the capital of Germany."),
                    ],
                    args,
                )
                predictions = benchmark.vanilla_answer(
                    workspace,
                    [
                        benchmark.Question(
                            id="q-1",
                            source="france",
                            question="What is the capital of France?",
                            answer="Paris",
                            question_type="Fact Retrieval",
                            evidence="Paris is the capital of France.",
                        )
                    ],
                    args,
                )
        self.assertEqual(metrics["chunks"], 2)
        self.assertEqual(predictions[0]["generated_answer"], "Paris")
        self.assertEqual(predictions[0]["retrieved_chunk_ids"][0].split("-")[0], "france")


class GraphRagTests(unittest.TestCase):
    def test_model_mapping_uses_graphrag_concurrency(self) -> None:
        mapping = {"default": {}}
        benchmark._update_model_mapping(
            mapping,
            model="model",
            api_base="http://localhost/v1",
            api_key_env="KEY",
        )
        self.assertEqual(
            mapping["default"]["concurrent_requests"],
            benchmark.GRAPHRAG_REQUEST_CONCURRENCY,
        )
        self.assertEqual(mapping["default"]["async_mode"], "asyncio")

    def test_ours_environment_exports_the_ingestion_budget(self) -> None:
        args = benchmark.fixed_args("http://localhost:8000/v1")
        args.ingestion_concurrency = 7

        environment = benchmark.ours_environment(args)

        self.assertEqual(environment["WIKI_INGEST_CONCURRENCY"], "7")
        self.assertEqual(environment["WIKI_CHUNK_CONCURRENCY"], "7")

    def test_generated_settings_bound_native_drift(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed in this test environment")
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "settings.yaml"
            settings_path.write_text(
                "completion_models:\n"
                "  default_completion_model: {}\n"
                "embedding_models:\n"
                "  default_embedding_model: {}\n",
                encoding="utf-8",
            )
            args = benchmark.fixed_args("http://model.invalid/v1")
            benchmark.configure_generated_graphrag_settings(
                settings_path, args
            )
            settings = yaml.safe_load(
                settings_path.read_text(encoding="utf-8")
            )

        drift = settings["drift_search"]
        self.assertEqual(args.graphrag_method, "drift")
        self.assertEqual(drift["concurrency"], 1)
        self.assertEqual(
            drift["primer_folds"],
            benchmark.GRAPHRAG_DRIFT_PRIMER_FOLDS,
        )
        self.assertEqual(
            drift["drift_k_followups"],
            benchmark.GRAPHRAG_DRIFT_FOLLOWUPS,
        )
        self.assertEqual(
            drift["n_depth"],
            benchmark.GRAPHRAG_DRIFT_DEPTH,
        )

    def test_query_command_supports_current_positional_syntax(self) -> None:
        args = SimpleNamespace(
            graphrag_command="/opt/graphrag/bin/graphrag",
            graphrag_method="drift",
        )
        command = benchmark.graphrag_query_command(
            args,
            Path("/tmp/index"),
            "Who is involved?",
            style="positional",
        )
        self.assertEqual(command[-1], "Who is involved?")
        self.assertNotIn("--query", command)
        self.assertIn("--no-streaming", command)

    def test_query_command_supports_legacy_flag_syntax(self) -> None:
        args = SimpleNamespace(
            graphrag_command="graphrag",
            graphrag_method="drift",
        )
        command = benchmark.graphrag_query_command(
            args,
            Path("/tmp/index"),
            "Who is involved?",
            style="flag",
        )
        self.assertEqual(command[-2:], ["--query", "Who is involved?"])

    def test_parses_native_cli_response(self) -> None:
        output = (
            "\x1b[32mSUCCESS: DRIFT Search Response:\x1b[0m\n"
            "The answer is supported by the indexed text."
        )
        self.assertEqual(
            benchmark.parse_graphrag_answer(output),
            "The answer is supported by the indexed text.",
        )


class ScoringAndReportTests(unittest.TestCase):
    def test_normalized_metrics(self) -> None:
        self.assertEqual(benchmark.normalized_answer("The Paris!"), "paris")
        self.assertEqual(benchmark.token_f1("Paris, France", "Paris"), 2 / 3)

    def test_report_has_speed_and_accuracy_headlines(self) -> None:
        questions = [
            make_question(1),
            make_question(2, "Complex Reasoning", answer="blue"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for system, elapsed, answers in (
                ("vanilla", 1.0, ("Paris", "blue")),
                ("graphrag", 4.0, ("Paris", "red")),
                ("ours", 2.0, ("Paris", "blue")),
            ):
                system_dir = output / "systems" / system
                benchmark.json_dump(
                    system_dir / "run-01" / "ingestion.json",
                    {
                        "elapsed_seconds": elapsed,
                        "characters": 1000,
                        "index_bytes": 1024,
                    },
                )
                rows = []
                for question, answer in zip(questions, answers):
                    row = question.result_base()
                    row.update(
                        {
                            "generated_answer": answer,
                            "exact_match": benchmark.normalized_answer(answer)
                            == benchmark.normalized_answer(question.answer),
                            "token_f1": benchmark.token_f1(answer, question.answer),
                            "latency_seconds": 0.25,
                            "error": None,
                        }
                    )
                    rows.append(row)
                benchmark.jsonl_dump(system_dir / "predictions.jsonl", rows)

            summary = benchmark.generate_report(
                output,
                questions,
                benchmark.SYSTEMS,
                judge="none",
                close_margin=0.05,
                seed=7,
            )
            markdown = (output / "summary.md").read_text(encoding="utf-8")
            token_csv_exists = (output / "token-usage.csv").is_file()

        self.assertEqual(summary["comparison"]["ours_ingestion_speedup"], 2.0)
        self.assertEqual(summary["accuracy"]["ours"]["accuracy"], 1.0)
        self.assertEqual(summary["accuracy"]["graphrag"]["accuracy"], 0.5)
        self.assertIn("Ours is faster: **PASS**", markdown)
        self.assertIn("Accuracy difference (ours − GraphRAG): **+50.0%**", markdown)
        self.assertIn("## Token usage", markdown)
        self.assertTrue(token_csv_exists)


class AgenticFreshQueryTests(unittest.TestCase):
    def test_native_ours_queries_each_existing_corpus_index(self) -> None:
        first = make_question(1)
        second_base = make_question(2)
        second = benchmark.Question(
            **{**second_base.__dict__, "source": "novel-2"}
        )

        def fake_ours_answer(
            workspace: Path,
            questions: list[benchmark.Question],
            _args: SimpleNamespace,
        ) -> list[dict[str, object]]:
            return [
                {
                    **question.result_base(),
                    "generated_answer": question.answer,
                    "error": None,
                }
                for question in questions
            ]

        with tempfile.TemporaryDirectory() as temporary:
            index_workspace = Path(temporary) / "ours" / "run-01"
            with mock.patch.object(
                benchmark,
                "ours_answer",
                side_effect=fake_ours_answer,
            ) as ours_answer:
                rows = benchmark.run_agentic_system_answers(
                    "ours",
                    index_workspace,
                    [first, second],
                    SimpleNamespace(),
                )

        self.assertEqual([row["id"] for row in rows], [first.id, second.id])
        self.assertEqual(ours_answer.call_count, 2)
        self.assertEqual(
            ours_answer.call_args_list[0].args[0],
            index_workspace / "corpora" / benchmark.safe_name("novel-1"),
        )
        self.assertEqual(
            ours_answer.call_args_list[1].args[0],
            index_workspace / "corpora" / benchmark.safe_name("novel-2"),
        )

    def test_combined_agentic_queries_one_shared_index(self) -> None:
        questions = [make_question(1), make_question(2)]

        def fake_ours_answer(
            _workspace: Path,
            values: list[benchmark.Question],
            _args: SimpleNamespace,
        ) -> list[dict[str, object]]:
            return [
                {
                    **question.result_base(),
                    "generated_answer": question.answer,
                    "error": None,
                }
                for question in values
            ]

        workspace = Path("/tmp/shared-index")
        with mock.patch.object(
            benchmark, "ours_answer", side_effect=fake_ours_answer
        ) as ours_answer:
            rows = benchmark.run_agentic_system_answers(
                "ours",
                workspace,
                questions,
                SimpleNamespace(corpus_scope="combined"),
            )

        self.assertEqual(len(rows), 2)
        ours_answer.assert_called_once()
        self.assertEqual(ours_answer.call_args.args[0], workspace)

    def test_agentic_restores_drift_from_source_run(self) -> None:
        args = SimpleNamespace(
            corpus_scope="combined",
            graphrag_method="local",
            graphrag_query_style="flag",
        )
        benchmark.apply_source_run_query_config(
            args,
            {
                "corpus_scope": "isolated",
                "run_config": {
                    "graphrag_method": "drift",
                    "graphrag_query_style": "positional",
                },
            },
        )
        self.assertEqual(args.corpus_scope, "isolated")
        self.assertEqual(args.graphrag_method, "drift")
        self.assertEqual(args.graphrag_query_style, "positional")

    def test_latest_completed_run_is_scoped_to_dataset(self) -> None:
        def create_run(
            root: Path,
            name: str,
            dataset: str | None,
            created_at: str,
        ) -> Path:
            run = root / "benchmark-results" / name
            manifest = {"created_at": created_at, "run_config": {}}
            if dataset:
                manifest["dataset"] = dataset
                manifest["run_config"] = {"dataset": dataset}
            benchmark.json_dump(
                run / "manifest.json",
                manifest,
            )
            benchmark.json_dump(run / "canonical" / "documents.json", [])
            benchmark.jsonl_dump(run / "canonical" / "questions.jsonl", [])
            for system in benchmark.SYSTEMS:
                benchmark.json_dump(
                    run / "systems" / system / "run-01" / "ingestion.json",
                    {"system": system},
                )
            return run

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fanout = create_run(
                root,
                "fanout-old",
                "fanout",
                "2026-01-01T00:00:00+00:00",
            )
            create_run(
                root,
                "novel-new",
                "novel",
                "2026-02-01T00:00:00+00:00",
            )
            legacy_novel = create_run(
                root,
                "pilot-legacy",
                None,
                "2026-03-01T00:00:00+00:00",
            )
            with mock.patch.object(benchmark, "ROOT", root):
                selected = benchmark.latest_completed_run(dataset="fanout")
                selected_legacy = benchmark.latest_completed_run(dataset="novel")

        self.assertEqual(selected, fanout)
        self.assertEqual(selected_legacy, legacy_novel)

    def test_fresh_agentic_run_discards_cache_and_retries_only_errors(self) -> None:
        questions = [make_question(1), make_question(2)]

        def result(
            question: benchmark.Question,
            *,
            answer: str = "Paris",
            error: str | None = None,
        ) -> dict[str, object]:
            return {
                **question.result_base(),
                "generated_answer": "" if error else answer,
                "latency_seconds": 0.1,
                "error": error,
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            predictions_path = root / "predictions.jsonl"
            live_progress_path = root / "live-progress.json"
            benchmark.jsonl_dump(
                predictions_path,
                [result(question, answer="cached") for question in questions],
            )
            calls: list[list[str]] = []

            def fake_run(
                _system: str,
                _workspace: Path,
                pending: list[benchmark.Question],
                _args: SimpleNamespace,
                _on_result: object,
            ) -> list[dict[str, object]]:
                calls.append([question.id for question in pending])
                if len(calls) == 1:
                    self.assertEqual(benchmark.jsonl_load(predictions_path), [])
                    return [
                        result(questions[0], answer="fresh"),
                        result(questions[1], error="RateLimitError: 429"),
                    ]
                return [result(questions[1], answer="retried")]

            args = SimpleNamespace(
                judge="none",
                judge_base_url="",
                chat_base_url="http://model.invalid/v1",
                judge_api_key="",
                chat_api_key="local",
                timeout=1,
                deadline=time.monotonic() + 60,
            )
            with (
                mock.patch.object(
                    benchmark,
                    "run_agentic_system_answers",
                    side_effect=fake_run,
                ),
                mock.patch.object(benchmark.time, "sleep") as sleep,
            ):
                rows = benchmark.run_fresh_agentic_answers(
                    system="ours",
                    index_workspace=root / "index",
                    questions=questions,
                    args=args,
                    predictions_path=predictions_path,
                    live_progress={},
                    live_progress_path=live_progress_path,
                )

            persisted = benchmark.jsonl_load(predictions_path)

        self.assertEqual(calls, [["q-001", "q-002"], ["q-002"]])
        self.assertEqual(
            [row["generated_answer"] for row in rows],
            ["fresh", "retried"],
        )
        self.assertEqual(
            [row["generated_answer"] for row in persisted],
            ["fresh", "retried"],
        )
        self.assertTrue(all(not row.get("error") for row in rows))
        sleep.assert_called_once_with(benchmark.AGENTIC_QUERY_RETRY_BASE_SECONDS)


class CommandTests(unittest.TestCase):
    def test_isolated_ingestion_quarantines_incomplete_corpus(self) -> None:
        document = benchmark.Document("novel-1", "source text")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_workspace = root / "run-01"
            corpus_name = benchmark.safe_name(document.id)
            corpus_workspace = run_workspace / "corpora" / corpus_name
            stale_file = corpus_workspace / "output" / "lancedb" / "stale.bin"
            stale_file.parent.mkdir(parents=True)
            stale_file.write_bytes(b"stale")
            source = root / "novel-1.txt"
            source.write_text(document.text, encoding="utf-8")

            def ingest_fresh(_system, workspace, *_args, **_kwargs):
                self.assertEqual(workspace, corpus_workspace)
                self.assertFalse(workspace.exists())
                return {
                    "system": "graphrag",
                    "documents": 1,
                    "characters": len(document.text),
                    "elapsed_seconds": 3.0,
                    "index_bytes": 17,
                }

            args = SimpleNamespace(corpus_scope="isolated", resume=True)
            with (
                mock.patch.object(
                    benchmark,
                    "run_one_system_ingestion",
                    side_effect=ingest_fresh,
                ) as ingest,
                mock.patch.object(
                    benchmark.shutil,
                    "rmtree",
                    side_effect=AssertionError("recursive deletion must not run"),
                ),
            ):
                result = benchmark.run_system_ingestion(
                    "graphrag",
                    run_workspace,
                    [document],
                    [{"id": document.id, "path": str(source)}],
                    {"documents": 1, "characters": len(document.text)},
                    args,
                )

            quarantines = list(
                (run_workspace / "corpora").glob(
                    f".{corpus_name}.incomplete-*"
                )
            )

            self.assertEqual(ingest.call_count, 1)
            self.assertEqual(len(quarantines), 1)
            self.assertEqual(
                (quarantines[0] / "output" / "lancedb" / "stale.bin").read_bytes(),
                b"stale",
            )
            self.assertTrue((corpus_workspace / "ingestion.json").is_file())
            self.assertEqual(result["corpora"][0]["corpus"], document.id)

    def test_isolated_ingestion_resumes_completed_corpus_checkpoints(self) -> None:
        documents = [
            benchmark.Document("novel-1", "first corpus"),
            benchmark.Document("novel-2", "second corpus"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_workspace = root / "run-01"
            first_workspace = (
                run_workspace
                / "corpora"
                / benchmark.safe_name(documents[0].id)
            )
            benchmark.json_dump(
                first_workspace / "ingestion.json",
                {
                    "corpus": documents[0].id,
                    "system": "ours",
                    "documents": 1,
                    "characters": len(documents[0].text),
                    "elapsed_seconds": 11.0,
                    "index_bytes": 101,
                },
            )
            mapping = []
            for document in documents:
                path = root / f"{document.id}.txt"
                path.write_text(document.text, encoding="utf-8")
                mapping.append({"id": document.id, "path": str(path)})

            def ingest_second(*_args, **_kwargs):
                return {
                    "system": "ours",
                    "documents": 1,
                    "characters": len(documents[1].text),
                    "elapsed_seconds": 13.0,
                    "index_bytes": 103,
                }

            args = SimpleNamespace(corpus_scope="isolated", resume=True)
            with mock.patch.object(
                benchmark,
                "run_one_system_ingestion",
                side_effect=ingest_second,
            ) as ingest:
                result = benchmark.run_system_ingestion(
                    "ours",
                    run_workspace,
                    documents,
                    mapping,
                    {"documents": 2, "characters": 24},
                    args,
                )

            second_checkpoint = (
                run_workspace
                / "corpora"
                / benchmark.safe_name(documents[1].id)
                / "ingestion.json"
            )
            second_checkpoint_exists = second_checkpoint.is_file()

        self.assertEqual(ingest.call_count, 1)
        self.assertEqual(result["elapsed_seconds"], 24.0)
        self.assertEqual(
            [item["corpus"] for item in result["corpora"]],
            ["novel-1", "novel-2"],
        )
        self.assertTrue(second_checkpoint_exists)

    def test_old_ours_worker_result_is_a_resumable_checkpoint(self) -> None:
        document = benchmark.Document("Novel-1", "source text")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "wiki.sqlite").write_bytes(b"sqlite index")
            benchmark.json_dump(
                workspace / "ingest-result.json",
                {
                    "elapsed_seconds": 9.5,
                    "graph": {"active_nodes": 3},
                    "document_results": [
                        {"document": document.id, "ingested": 3}
                    ],
                },
            )

            detail = benchmark.load_completed_corpus_ingestion(
                system="ours",
                corpus_workspace=workspace,
                document=document,
            )

        self.assertIsNotNone(detail)
        self.assertEqual(detail["corpus"], document.id)
        self.assertEqual(detail["documents"], 1)
        self.assertGreater(detail["index_bytes"], 0)

    def test_finds_only_exact_compatible_incomplete_run(self) -> None:
        documents = [benchmark.Document("novel-1", "source")]
        questions = [make_question(1)]
        config = {"dataset": "novel", "systems": ["ours"]}
        with tempfile.TemporaryDirectory() as temporary:
            results_root = Path(temporary)
            candidate = results_root / "novel-existing"
            benchmark.json_dump(
                candidate / "manifest.json",
                {
                    "dataset_fingerprint": benchmark.dataset_fingerprint(
                        documents, questions
                    ),
                    "run_config": config,
                },
            )

            found = benchmark.latest_compatible_incomplete_run(
                results_root=results_root,
                documents=documents,
                questions=questions,
                run_config=config,
            )
            self.assertEqual(found, candidate)

            benchmark.json_dump(
                candidate / "systems" / "ours" / "run-01" / "ingestion.json",
                {"system": "ours"},
            )
            benchmark.jsonl_dump(
                candidate / "systems" / "ours" / "predictions.jsonl",
                [{"id": questions[0].id}],
            )
            (candidate / "summary.md").write_text("done", encoding="utf-8")

            self.assertIsNone(
                benchmark.latest_compatible_incomplete_run(
                    results_root=results_root,
                    documents=documents,
                    questions=questions,
                    run_config=config,
                )
            )

    def test_cli_routes_normal_and_agentic_by_dataset(self) -> None:
        with mock.patch.object(benchmark, "command_run", return_value=0) as run:
            self.assertEqual(
                benchmark.main(["fanout", "http://model.invalid/v1"]), 0
            )
        self.assertEqual(run.call_args.args[0].dataset, "fanout")
        self.assertEqual(run.call_args.args[0].corpus_scope, "combined")

        with mock.patch.object(
            benchmark, "command_agentic_fresh", return_value=0
        ) as agentic:
            self.assertEqual(benchmark.main(["agentic", "musique"]), 0)
        self.assertEqual(agentic.call_args.args[0].dataset, "musique")
        self.assertEqual(agentic.call_args.args[0].graphrag_method, "drift")

    def test_vanilla_only_run_writes_readable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus.json"
            questions = root / "questions.json"
            output = root / "result"
            corpus.write_text(
                json.dumps(
                    [
                        {
                            "corpus_name": "novel-1",
                            "context": "Paris is the capital of France.",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            questions.write_text(
                json.dumps(
                    [
                        {
                            "id": "q-1",
                            "source": "novel-1",
                            "question": "What is the capital of France?",
                            "answer": "Paris",
                            "question_type": "Fact Retrieval",
                            "evidence": ["Paris is the capital of France."],
                        },
                        {
                            "id": "q-2",
                            "source": "novel-1",
                            "question": "Why is Paris associated with France?",
                            "answer": "Paris",
                            "question_type": "Complex Reasoning",
                            "evidence": ["Paris is the capital of France."],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            args = benchmark.fixed_args("http://model.invalid/v1")
            args.corpus = str(corpus)
            args.questions = str(questions)
            args.output = str(output)
            args.systems = "vanilla"
            args.chat_model = "fake-chat"
            args.embed_base_url = "http://model.invalid/v1"
            args.embed_model = "fake-embed"
            args.embed_dim = 2
            args.judge = "none"
            with mock.patch.object(
                benchmark.urllib.request,
                "urlopen",
                fake_urlopen,
            ):
                self.assertEqual(benchmark.command_run(args), 0)

            summary = json.loads(
                (output / "summary.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            predictions = benchmark.jsonl_load(
                output / "systems" / "vanilla" / "predictions.jsonl"
            )

        self.assertEqual(summary["accuracy"]["vanilla"]["accuracy"], 1.0)
        self.assertEqual(manifest["corpus_scope"], "isolated")
        self.assertEqual(predictions[0]["evidence"], ["Paris is the capital of France."])


if __name__ == "__main__":
    unittest.main()
