from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import benchmark
from benchmark.clients import vanilla
from benchmark.clients import llm_wiki
from benchmark.datasets import novel
from benchmark import runner


def _dataset_files(root: Path, question_count: int = 3) -> tuple[Path, Path]:
    corpus = root / "corpus.json"
    questions = root / "questions.json"
    corpus.write_text(
        json.dumps(
            [{"corpus_name": "book", "context": "Paris is the capital of France."}]
        ),
        encoding="utf-8",
    )
    questions.write_text(
        json.dumps(
            [
                {
                    "id": f"q-{index}",
                    "source": "book",
                    "question": f"Question {index}?",
                    "answer": "Paris",
                    "question_type": "Fact Retrieval",
                }
                for index in range(question_count)
            ]
        ),
        encoding="utf-8",
    )
    return corpus, questions


def _args(root: Path, question_count: int = 3) -> SimpleNamespace:
    corpus, questions = _dataset_files(root, question_count)
    args = runner.build_args("novel", chat_base_url="http://model.invalid/v1")
    args.corpus = str(corpus)
    args.questions = str(questions)
    args.judge = "none"
    args.embed_dim = 2
    args.chunk_tokens = 4
    args.chunk_overlap = 0
    args.embed_batch_size = 1
    args.chat_concurrency = 1
    return args


class _FakeResponse:
    def __init__(self, value):
        self.body = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.body


def _fake_urlopen(request, *, timeout):
    del timeout
    payload = json.loads(request.data)
    if request.get_full_url().endswith("/embeddings"):
        return _FakeResponse(
            {
                "data": [
                    {"index": index, "embedding": [1.0, 0.0]}
                    for index, _text in enumerate(payload["input"])
                ],
                "usage": {
                    "input_tokens": len(payload["input"]),
                    "total_tokens": len(payload["input"]),
                },
            }
        )
    return _FakeResponse(
        {
            "choices": [{"message": {"content": "Paris"}}],
            "usage": {
                "input_tokens": 5,
                "output_tokens": 1,
                "total_tokens": 6,
            },
        }
    )


class DatasetPackageTests(unittest.TestCase):
    def test_novel_uses_every_question_and_completed_store_reopens_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = _args(root, question_count=7)
            datastore = root / "store"
            first = novel.prepare(datastore, args)
            Path(args.corpus).unlink()
            Path(args.questions).unlink()
            second = novel.prepare(datastore, args)

        self.assertEqual(len(first.questions), 7)
        self.assertEqual(first, second)
        self.assertEqual(first.corpus_scope, "combined")


class ClientPackageTests(unittest.TestCase):
    def test_vanilla_resumes_at_first_unwritten_embedding_part(self) -> None:
        class FakeEmbeddingClient:
            calls = 0
            fail_on = 2

            def __init__(self, *_args, **_kwargs):
                pass

            def embeddings(self, texts, _model):
                type(self).calls += 1
                if type(self).calls == type(self).fail_on:
                    raise benchmark.BenchmarkError("stopped")
                return [[1.0, 0.0] for _ in texts], {
                    "input_tokens": len(texts),
                    "total_tokens": len(texts),
                }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "vanilla"
            args = _args(root, question_count=1)
            bundle = benchmark.DatasetBundle(
                [benchmark.Document("doc", "one two three four five six seven")],
                [],
                "combined",
            )
            manifest = {"dataset_fingerprint": "fingerprint"}
            with mock.patch.object(
                vanilla.legacy,
                "OpenAICompatibleClient",
                FakeEmbeddingClient,
            ):
                with self.assertRaises(benchmark.BenchmarkError):
                    vanilla.ingest(workspace, bundle, [], manifest, args)
                first_part = workspace / "index-parts" / "000000000.jsonl"
                self.assertTrue(first_part.is_file())
                FakeEmbeddingClient.fail_on = -1
                vanilla.ingest(workspace, bundle, [], manifest, args)

            rows = benchmark.jsonl_load(workspace / "index.jsonl")

        self.assertEqual(len(rows), 2)
        # One failed call plus the two distinct successful chunks: the first
        # completed part was not embedded again.
        self.assertEqual(FakeEmbeddingClient.calls, 3)

    def test_graphrag_metrics_accept_openai_input_output_names(self) -> None:
        text = (
            'Metrics for chat-model: {"input_tokens": 10, "output_tokens": 4, '
            '"total_tokens": 14, "attempted_request_count": 2}\n'
            'Metrics for embed-model: {"input_tokens": 7, "total_tokens": 7, '
            '"attempted_request_count": 1}\n'
        )
        usage = benchmark.parse_graphrag_token_metrics(
            text,
            SimpleNamespace(embed_model="embed-model"),
        )
        self.assertEqual(usage["retrieval_chat"]["prompt_tokens"], 10)
        self.assertEqual(usage["retrieval_chat"]["completion_tokens"], 4)
        self.assertEqual(usage["retrieval_embedding"]["total_tokens"], 7)

    def test_llm_wiki_resumes_at_first_uncheckpointed_document(self) -> None:
        documents = [
            benchmark.Document("first", "first text"),
            benchmark.Document("second", "second text"),
        ]
        bundle = benchmark.DatasetBundle(documents, [], "combined")
        manifest = {"dataset_fingerprint": "fingerprint"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = _args(root, question_count=1)
            mapping = []
            for document in documents:
                path = root / f"{document.id}.txt"
                path.write_text(document.text, encoding="utf-8")
                mapping.append({"id": document.id, "path": str(path)})
            calls = []
            should_fail = True

            def fake_worker(_action, request, **_kwargs):
                nonlocal should_fail
                batch = request["documents"]
                if not batch:
                    calls.append("finalize")
                    return {
                        "elapsed_seconds": 0.5,
                        "document_results": [],
                        "token_usage": {},
                    }
                document_id = batch[0]["id"]
                calls.append(document_id)
                if document_id == "second" and should_fail:
                    should_fail = False
                    raise benchmark.BenchmarkError("stopped")
                return {
                    "elapsed_seconds": 1.0,
                    "document_results": [
                        {"document": document_id, "ingested": 1}
                    ],
                    "token_usage": {},
                }

            with mock.patch.object(
                llm_wiki.legacy,
                "run_ours_worker",
                side_effect=fake_worker,
            ):
                with self.assertRaises(benchmark.BenchmarkError):
                    llm_wiki.ingest(root / "wiki", bundle, mapping, manifest, args)
                result = llm_wiki.ingest(
                    root / "wiki", bundle, mapping, manifest, args
                )

        self.assertEqual(calls, ["first", "second", "second", "finalize"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["document_checkpoints"], 2)


class RunnerPackageTests(unittest.TestCase):
    def test_full_dataset_args_pass_legacy_runtime_sample_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = _args(Path(temporary), question_count=1)
            self.assertIsNone(args.sample)
            # Vanilla preflight is entirely local and reaches the compatibility
            # check that previously compared None to zero.
            benchmark.validate_runtime(args, ("vanilla",))

    def test_two_phases_use_stable_paths_and_full_question_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = _args(root, question_count=4)

            def fake_ingest(workspace, bundle, _mapping, manifest, _args):
                result = {
                    "client": "vanilla",
                    "status": "complete",
                    "dataset_fingerprint": manifest["dataset_fingerprint"],
                    "documents": len(bundle.documents),
                    "elapsed_seconds": 1.0,
                    "index_bytes": 1,
                    "token_usage": {},
                }
                benchmark.json_dump(workspace / "ingestion.json", result)
                return result

            def fake_query(_workspace, questions, _args, on_result):
                rows = []
                for question in questions:
                    row = {
                        **question.result_base(),
                        "generated_answer": question.answer,
                        "latency_seconds": 0.1,
                        "token_usage": {
                            "agent_chat": {
                                "requests": 1,
                                "prompt_tokens": 5,
                                "completion_tokens": 1,
                                "total_tokens": 6,
                            }
                        },
                        "error": None,
                    }
                    rows.append(row)
                    on_result(row)
                return rows

            fake_client = SimpleNamespace(ingest=fake_ingest, query=fake_query)
            with (
                mock.patch.object(runner, "RESULTS_ROOT", root / "benchmark-results"),
                mock.patch.object(runner, "get_client", return_value=fake_client),
            ):
                self.assertEqual(
                    runner.command_ingest(args, ("vanilla",), preflight=False), 0
                )
                self.assertEqual(
                    runner.command_bench(args, ("vanilla",), preflight=False), 0
                )
                datastore = runner.datastore_path("novel")
                output = runner.benchmark_path("novel")
                predictions = benchmark.jsonl_load(
                    output / "clients" / "vanilla" / "predictions.jsonl"
                )
                manifest = json.loads(
                    (output / "manifest.json").read_text(encoding="utf-8")
                )

        self.assertEqual(datastore.name, "novel")
        self.assertEqual(output.name, "novel")
        self.assertEqual(len(predictions), 4)
        self.assertIsNone(manifest["sampling"])

    def test_debug_bench_samples_questions_without_touching_the_datastore(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = _args(root, question_count=6)
            args.debug = True
            args.output = str(
                Path(args.output).with_name(Path(args.output).name + "-debug")
            )
            with (
                mock.patch.object(runner, "RESULTS_ROOT", root / "results"),
                mock.patch.object(runner, "DEBUG_QUESTION_SAMPLE", 4),
                mock.patch.object(
                    benchmark.urllib.request,
                    "urlopen",
                    _fake_urlopen,
                ),
            ):
                runner.command_ingest(args, ("vanilla",), preflight=False)
                runner.command_bench(args, ("vanilla",), preflight=False)
                datastore = runner.datastore_path("novel")
                output = runner.benchmark_path("novel", debug=True)
                manifest = json.loads(
                    (output / "manifest.json").read_text(encoding="utf-8")
                )
                predictions = benchmark.jsonl_load(
                    output / "clients" / "vanilla" / "predictions.jsonl"
                )
                stored = benchmark.jsonl_load(
                    datastore / "canonical" / "questions.jsonl"
                )
                first = [str(row.get("id")) for row in predictions]
                # A rerun must ask the identical subset, not resample.
                runner.command_bench(args, ("vanilla",), preflight=False)
                second = [
                    str(row.get("id"))
                    for row in benchmark.jsonl_load(
                        output / "clients" / "vanilla" / "predictions.jsonl"
                    )
                ]

        self.assertEqual(output.name, "novel-debug")
        self.assertEqual(len(predictions), 4)
        self.assertEqual(first, second)
        # The ingested corpus keeps every question; only the asking shrinks.
        self.assertEqual(len(stored), 6)
        self.assertEqual(manifest["questions"], 4)
        self.assertEqual(manifest["sampling"]["mode"], "debug")
        self.assertEqual(manifest["sampling"]["seed"], runner.DEBUG_SAMPLE_SEED)

    def test_real_vanilla_adapter_runs_through_both_phases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = _args(root, question_count=2)
            with (
                mock.patch.object(runner, "RESULTS_ROOT", root / "results"),
                mock.patch.object(
                    benchmark.urllib.request,
                    "urlopen",
                    _fake_urlopen,
                ),
            ):
                runner.command_ingest(args, ("vanilla",), preflight=False)
                runner.command_bench(args, ("vanilla",), preflight=False)
                summary = json.loads(
                    (runner.benchmark_path("novel") / "summary.json").read_text(
                        encoding="utf-8"
                    )
                )

        self.assertEqual(summary["accuracy"]["vanilla"]["accuracy"], 1.0)
        self.assertGreater(summary["query_token_usage"]["vanilla"]["total_tokens"], 0)
        self.assertTrue(summary["token_accounting"]["vanilla"]["complete"])


if __name__ == "__main__":
    unittest.main()
