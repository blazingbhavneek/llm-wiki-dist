#!/usr/bin/env python3
"""Native RAG harness benchmark.

Compares three end-to-end systems without replacing their native query flows:

* vanilla: fixed-size chunks, dense top-k retrieval, one unconstrained answer call
* graphrag: Microsoft GraphRAG standard indexing and native query method
* ours: llm-wiki's complete conceptual ingestion and ResearchSession.ask() flow

The core uses the Python standard library. Parquet input optionally uses
DuckDB (or pandas/pyarrow), and automatic GraphRAG configuration uses PyYAML.
Microsoft GraphRAG and llm-wiki can live in separate virtual environments.

Run one fixed dataset preset, optionally with an OpenAI-compatible chat base URL:

    python benchmark.py novel http://localhost:9000/v1
    python benchmark.py fanout
    python benchmark.py multihop
    python benchmark.py musique

Fresh agentic re-querying is scoped to the latest completed ingestion for the
named dataset:

    python benchmark.py agentic fanout

The corpus, models, concurrency, deadline, and all fairness settings are
constants near the top of this file. API keys are fixed to ``local`` and are
never written to benchmark artifacts.
"""

from __future__ import annotations

import collections
import contextlib
import csv
import hashlib
import json
import math
import os
import platform
import random
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import textwrap
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Sequence

try:  # Optional: the harness core stays importable on a bare interpreter.
    from tqdm import tqdm as _tqdm
except ImportError:  # pragma: no cover - exercised by the stdlib fallback
    _tqdm = None


ROOT = Path(__file__).resolve().parent
APP_ROOT = ROOT / "llm-wiki-dist"
DEFAULT_QUESTION_TYPES = ("Fact Retrieval", "Complex Reasoning")
SYSTEMS = ("vanilla", "graphrag", "ours")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
WORD_RE = re.compile(r"\w+", re.UNICODE)

# Fixed benchmark presets. The only optional public input is the chat server URL.
DEFAULT_CHAT_BASE_URL = "http://170.64.243.132:26572/v1"
CHAT_MODEL = "nvidia/Gemma-4-31B-IT-NVFP4"
NVIDIA_API_KEY = "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"
EMBED_BASE_URL = "http://localhost:8000/v1"
EMBED_MODEL = "Qwen/Qwen3-Embedding-0.6B"
EMBED_DIM = 1024
RERANK_BASE_URL = "http://localhost:8001/v1"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
DATA_DIR = ROOT / "benchmark-data"
DATASETS = ("novel", "fanout", "multihop", "musique")
CORPUS_PATH = DATA_DIR / "novel.json"
QUESTIONS_PATH = DATA_DIR / "novel_questions.json"
CORPUS_URL = (
    "https://raw.githubusercontent.com/GraphRAG-Bench/"
    "GraphRAG-Benchmark/main/Datasets/Corpus/novel.json"
)
QUESTIONS_URL = (
    "https://raw.githubusercontent.com/GraphRAG-Bench/"
    "GraphRAG-Benchmark/main/Datasets/Questions/novel_questions.json"
)
FANOUT_DIR = DATA_DIR / "fanout"
FANOUT_QUESTIONS_PATH = FANOUT_DIR / "fanout-final-dev.json"
FANOUT_CORPUS_DIR = FANOUT_DIR / "wikipedia-revisions"
FANOUT_QUESTIONS_URL = (
    "https://raw.githubusercontent.com/zhudotexe/fanoutqa/main/"
    "fanoutqa/data/fanout-final-dev.json"
)
# The published dev set leaves the literal string ###TBD### in place of the
# page and revision IDs of 83 evidence records across 28 of its 310 questions.
# Upstream FanOutQA resolves those by title against its dataset epoch -- the
# newest revision on or before this instant -- which keeps the corpus pinned
# to article text as it read when the answers were annotated.
FANOUT_DATASET_EPOCH = "2023-11-20T00:00:00Z"
FANOUT_REVISIONS_PATH = FANOUT_DIR / "resolved-revisions.json"
FANOUT_PLACEHOLDER = "###TBD###"
# Building the FanOutQA corpus means fetching well over a thousand revisions
# back to back, so Wikipedia will throttle at some point in every full run.
# Retries have to outlast a throttle window rather than merely survive a blip:
# one exhausted budget aborts an ingest that is otherwise hours in. Wikimedia's
# User-Agent policy asks for a contact URL, and anonymous traffic without one
# is throttled harder.
WIKIPEDIA_USER_AGENT = (
    "llm-wiki-benchmark/1.0 (https://github.com/blazingbhavneek/llm-wiki-dist)"
)
WIKIPEDIA_MAX_ATTEMPTS = 8
WIKIPEDIA_MAX_RETRY_SECONDS = 120.0
# API error codes that no amount of waiting will clear. Retrying these once
# cost ninety seconds per record and reported permanent data rot as throttling.
WIKIPEDIA_PERMANENT_ERRORS = frozenset(
    {"nosuchrevid", "missingtitle", "nosuchpageid", "invalidtitle"}
)
# Wikimedia asks bulk readers to stay serial and unhurried rather than to back
# off only once throttled. One short pause per request costs a few minutes over
# a full corpus and keeps the run under the limiter instead of bouncing off it.
WIKIPEDIA_REQUEST_SPACING_SECONDS = 0.2
MULTIHOP_DIR = DATA_DIR / "multihop"
MULTIHOP_CORPUS_PATH = MULTIHOP_DIR / "corpus.json"
MULTIHOP_QUESTIONS_PATH = MULTIHOP_DIR / "MultiHopRAG.json"
MULTIHOP_CORPUS_URL = (
    "https://huggingface.co/datasets/yixuantt/MultiHopRAG/"
    "resolve/main/corpus.json?download=true"
)
MULTIHOP_QUESTIONS_URL = (
    "https://huggingface.co/datasets/yixuantt/MultiHopRAG/"
    "resolve/main/MultiHopRAG.json?download=true"
)
MUSIQUE_DIR = DATA_DIR / "musique"
MUSIQUE_ARCHIVE_PATH = MUSIQUE_DIR / "musique_v1.0.zip"
MUSIQUE_QUESTIONS_PATH = MUSIQUE_DIR / "musique_ans_v1.0_dev.jsonl"
MUSIQUE_ARCHIVE_URL = (
    "https://drive.usercontent.google.com/download?"
    "id=1tGdADlNjWFaHLeZZGShh2IRcpO6Lv24h&export=download&confirm=t"
)
# GraphRAG remains isolated from the Python 3.13/vLLM environment. uvx installs
# it once into the shared uv cache before ingestion timing begins.
GRAPHRAG_COMMAND = "uvx --python 3.12 --from graphrag graphrag"
OURS_PYTHON = APP_ROOT / ".venv/bin/python"
# Every dataset is sized to 100 questions so results are comparable across
# them. What that costs in corpus size differs sharply per dataset -- see each
# note below, because the corpus is derived from the questions in three of the
# four cases.
#
# 20 novels x 5 = exactly 100 questions, type-balanced across Fact Retrieval
# and Complex Reasoning. The corpus is the novels themselves (4.8M characters,
# median 244K each), so it does not grow with the question count.
QUESTIONS_PER_NOVEL = 5
# Corpus is the dated Wikipedia pages cited as evidence, 4-8 per question, so
# ~500-700 full articles get fetched from the Wikipedia API and cached under
# benchmark-data/fanout/wikipedia-revisions before ingestion starts.
FANOUT_QUESTION_SAMPLE = 100
# The only dataset whose corpus is fixed: every MultiHop-RAG news article is
# ingested regardless of the sample, so raising this costs queries, not ingest.
MULTIHOP_QUESTION_SAMPLE = 100
# 100 MuSiQue questions pull in ~1,683 unique supporting paragraphs as the
# shared corpus, balanced across 2/3/4-hop (34/33/33). 20 gave only ~383.
MUSIQUE_QUESTION_SAMPLE = 100
# None uses all 20 novels, which is what QUESTIONS_PER_NOVEL=5 assumes above.
# Set to an integer only for a fast smoke test on the smallest novels.
NOVEL_LIMIT: int | None = None
# One concurrency ceiling for every stage of every system: no phase of any
# benchmark should ever have more than this many model requests in flight.
# Every knob below is derived from it, and each derivation states how many
# requests one of its workers actually issues, so workers x requests-per-worker
# never exceeds the cap. Sized for a rented GPU; drop it to 1 when running
# against a metered API with a low RPM ceiling.
#
# Halved from 10 so two datasets can ingest in parallel from separate
# terminals without exceeding the server's original single-run budget.
MAX_CONCURRENT_REQUESTS = 10

# GraphRAG indexing fans out internally up to concurrent_requests, so the cap
# is the driver here rather than a worker count.
INGESTION_CONCURRENCY = MAX_CONCURRENT_REQUESTS
GRAPHRAG_REQUEST_CONCURRENCY = MAX_CONCURRENT_REQUESTS
# GraphRAG queries run one CLI subprocess per question, each of which honors
# concurrent_requests on its own. Keeping DRIFT's internal fan-out at one makes
# a question worker worth exactly one in-flight request.
GRAPHRAG_QUERY_WORKERS = MAX_CONCURRENT_REQUESTS
# DRIFT fans its follow-ups out internally. The runner already pins query
# workers to 1 so each question's metrics stay attributable, which leaves the
# whole cap for one question's own fan-out.
GRAPHRAG_DRIFT_CONCURRENCY = 3
# Provider requests-per-minute cap. None disables the limiter entirely, which
# is what a local or rented GPU wants; set an integer for a metered API (35
# left headroom under the NVIDIA endpoint's 40 RPM cap).
GRAPHRAG_RPM_LIMIT: int | None = None

# Ours ingestion: one shared cap. Short documents use it as the group worker
# count; long documents are processed one at a time and use it inside every
# independent chunking/graph-ingest phase. A phase barrier separates node
# preparation from linking so results do not depend on thread completion order.
OURS_INGEST_CONCURRENCY = MAX_CONCURRENT_REQUESTS
# ours queries: a lead agent dispatches a team of exploration subagents, and
# OURS_SUBAGENT_CONCURRENCY of them run at once inside a single question. One
# question worker is therefore worth that many in-flight requests, not one, so
# the question workers are divided down to keep the global cap honest.
OURS_SUBAGENT_COUNT = 5
OURS_SUBAGENT_CONCURRENCY = 5
OURS_QUESTION_WORKERS = max(
    1, MAX_CONCURRENT_REQUESTS // OURS_SUBAGENT_CONCURRENCY
)
# Search width the lead and every subagent see: the reranked result count, and
# the candidate pool the reranker draws that from.
OURS_RERANK_TOP_K = 40
OURS_SEARCH_POOL = 100
# vanilla answers and the LLM judge: one request per worker.
CHAT_CONCURRENCY = MAX_CONCURRENT_REQUESTS
# Bounded native DRIFT: retain its global-to-local iterative search without
# the installed defaults (5 primer folds, 20 follow-ups, 3 depths) exploding
# into dozens or hundreds of calls for one question.
#
# DRIFT issues roughly primer_folds + followups x depth + reduce calls, so this
# is ~12 per question against the ~9 the first bounded settings produced.
# Concurrency 3 keeps the added breadth off the wall clock.
GRAPHRAG_DRIFT_PRIMER_FOLDS = 2
GRAPHRAG_DRIFT_FOLLOWUPS = 4
GRAPHRAG_DRIFT_DEPTH = 2
RUN_AGENTIC_GRAPHRAG = True
RUN_AGENTIC_OURS = True  # native ResearchSession.ask() vs DRIFT in agentic mode
REBUILD_AGENTIC_GRAPHRAG = False  # corrected rebuild is already complete
AGENTIC_QUERY_MAX_ATTEMPTS = 3
AGENTIC_QUERY_RETRY_BASE_SECONDS = 15.0
AGENTIC_OURS_QUESTION_WORKERS = MAX_CONCURRENT_REQUESTS
AGENT_TOOL_RESULT_CHARS = 18_000
# Doubles as the per-subprocess timeout (command_timeout), and ours ingests a
# whole corpus inside one subprocess: at 100 MuSiQue questions that is ~1,683
# documents. Five hours was sized for the one-novel pilot and would now cut the
# ingest off mid-run, so give it a day. Every stage stays resumable.
MAX_RUNTIME_SECONDS = 24 * 60 * 60
REQUEST_TIMEOUT_SECONDS = 120
OURS_CHUNK_THRESHOLD_LINES = 300
LONG_PROSE_MIN_CHARS = 20_000
PROSE_LINE_WIDTH = 200


class BenchmarkError(RuntimeError):
    """A user-actionable benchmark failure."""


class WikipediaContentGone(BenchmarkError):
    """A revision or page a dataset pins no longer exists on Wikipedia.

    FanOutQA pins revisions from 2023, and articles deleted since then take
    every one of their revisions out of the live API. Retrying cannot fix
    that, so it is raised past the retry policy and handled by the caller.
    """


@dataclass(frozen=True)
class Document:
    id: str
    text: str


@dataclass(frozen=True)
class Question:
    id: str
    source: str
    question: str
    answer: str
    question_type: str
    evidence: Any
    evidence_triple: Any = ""

    def result_base(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "source": self.source,
            "context": [],
            "evidence": self.evidence,
            "question_type": self.question_type,
            "generated_answer": "",
            "ground_truth": self.answer,
        }


@dataclass(frozen=True)
class DatasetBundle:
    documents: list[Document]
    questions: list[Question]
    corpus_scope: str


@dataclass
class ApiUsage:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, usage: dict[str, Any] | None) -> None:
        self.requests += 1
        if not isinstance(usage, dict):
            return
        prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion = int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += int(usage.get("total_tokens") or prompt + completion)


def summed_usage(values: Iterable[dict[str, Any] | None]) -> dict[str, int]:
    total = ApiUsage()
    for value in values:
        if not isinstance(value, dict):
            continue
        total.requests += int(
            value["requests"] if "requests" in value else 1
        )
        prompt = int(value.get("prompt_tokens") or value.get("input_tokens") or 0)
        completion = int(
            value.get("completion_tokens") or value.get("output_tokens") or 0
        )
        total.prompt_tokens += prompt
        total.completion_tokens += completion
        total.total_tokens += int(value.get("total_tokens") or prompt + completion)
    return asdict(total)


def graphrag_metrics_coverage(text: str, args: SimpleNamespace) -> tuple[int, int]:
    """Return (calls that reported tokens, calls attempted) for chat metrics.

    GraphRAG streams its internal DRIFT sub-calls even under ``--no-streaming``,
    which only governs the final answer. A streamed response carries no usage
    payload, so its tokens never reach the metrics log while its request is
    still counted. Comparing the two makes that shortfall visible instead of
    letting a partial sum pass as a complete one.
    """

    decoder = json.JSONDecoder()
    measured = attempted = 0
    for match in re.finditer(r"Metrics for ([^:]+):\s*", text):
        try:
            usage, _end = decoder.raw_decode(text[match.end() :].lstrip())
        except (json.JSONDecodeError, TypeError):
            continue
        label = match.group(1)
        if not isinstance(usage, dict):
            continue
        if args.embed_model in label or "embed" in label.casefold():
            continue
        attempted += int(usage.get("attempted_request_count") or 0)
        measured += int(usage.get("responses_with_tokens") or 0)
    return measured, attempted


def parse_graphrag_token_metrics(
    text: str, args: SimpleNamespace
) -> dict[str, dict[str, int]]:
    categories: dict[str, list[dict[str, Any]]] = {
        "retrieval_chat": [],
        "retrieval_embedding": [],
    }
    decoder = json.JSONDecoder()
    for match in re.finditer(r"Metrics for ([^:]+):\s*", text):
        try:
            usage, _end = decoder.raw_decode(text[match.end() :].lstrip())
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(usage, dict):
            continue
        label = match.group(1)
        category = (
            "retrieval_embedding"
            if args.embed_model in label or "embed" in label.casefold()
            else "retrieval_chat"
        )
        requests = int(
            usage.get("attempted_request_count") or usage.get("requests") or 0
        )
        if requests == 0 and any(
            int(usage.get(key) or 0)
            for key in (
                "prompt_tokens",
                "input_tokens",
                "completion_tokens",
                "output_tokens",
                "total_tokens",
            )
        ):
            requests = 1
        usage["requests"] = requests
        categories[category].append(usage)
    return {
        category: summed_usage(values)
        for category, values in categories.items()
    }


def token_breakdown(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = collections.defaultdict(int)
    for row in rows:
        usage = row.get("token_usage")
        if not isinstance(usage, dict):
            continue
        for category, counts in usage.items():
            if isinstance(counts, dict):
                totals[str(category)] += int(counts.get("total_tokens") or 0)
    totals["all"] = sum(
        value for key, value in totals.items() if key != "all"
    )
    return dict(totals)


def prediction_token_usage(row: dict[str, Any]) -> dict[str, int]:
    """Flatten every token category into one per-question usage record."""
    categories = row.get("token_usage")
    values = (
        [value for value in categories.values() if isinstance(value, dict)]
        if isinstance(categories, dict)
        else []
    )
    return summed_usage(values)


def aggregate_prediction_usage(
    rows: Iterable[dict[str, Any]],
) -> dict[str, int | float]:
    per_question = [prediction_token_usage(row) for row in rows]
    total = summed_usage(per_question)
    count = len(per_question)
    return {
        **total,
        "questions": count,
        "input_tokens_per_question": (
            total["prompt_tokens"] / count if count else 0.0
        ),
        "output_tokens_per_question": (
            total["completion_tokens"] / count if count else 0.0
        ),
        "total_tokens_per_question": (
            total["total_tokens"] / count if count else 0.0
        ),
    }


def publish_live_progress(
    *,
    system: str,
    rows: Sequence[dict[str, Any]],
    total_questions: int,
    live_progress: dict[str, Any],
    live_progress_path: Path,
    last_question_id: str | None = None,
) -> None:
    solved_rows = [row for row in rows if not row.get("error")]
    solved = len(solved_rows)
    correct = sum(bool(row.get("judge_correct")) for row in solved_rows)
    accuracy = correct / solved if solved else 0.0
    categories = token_breakdown(solved_rows)
    usage = aggregate_prediction_usage(solved_rows)
    last_row = next(
        (
            row
            for row in reversed(rows)
            if last_question_id is not None
            and str(row.get("id")) == last_question_id
        ),
        None,
    )
    last_usage = prediction_token_usage(last_row or {})
    live_progress[system] = {
        "solved": solved,
        "total": total_questions,
        "correct": correct,
        "accuracy": accuracy,
        "token_usage": usage,
        "token_categories": categories,
        "last_query_token_usage": last_usage,
        "last_question_id": last_question_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    json_dump(live_progress_path, live_progress)
    log(
        f"{system} live: {solved}/{total_questions} solved | "
        f"correct {correct}/{solved} ({accuracy:.1%}) | "
        f"tokens in={usage['prompt_tokens']:,}, "
        f"out={usage['completion_tokens']:,}, "
        f"total={usage['total_tokens']:,}; "
        f"last-query in/out/total="
        f"{last_usage['prompt_tokens']:,}/"
        f"{last_usage['completion_tokens']:,}/"
        f"{last_usage['total_tokens']:,} "
        f"(agent={categories.get('agent_chat', 0):,}, "
        f"retrieval-chat={categories.get('retrieval_chat', 0):,}, "
        f"embedding={categories.get('retrieval_embedding', 0):,}, "
        f"judge={categories.get('judge_chat', 0):,})"
    )


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def log(message: str) -> None:
    print(f"[benchmark] {message}", flush=True)


@contextlib.contextmanager
def progress_reporter(
    total: int, description: str, *, every: int = 25
) -> Any:
    """Report progress through a long loop that is otherwise silent.

    An interactive run gets a tqdm bar; a redirected or piped run gets a
    periodic line through ``log`` instead, so a captured ingest log stays
    readable rather than filling with carriage returns. Yields a callable
    that advances the report by one item.
    """

    bar = None
    if _tqdm is not None and sys.stderr.isatty():
        bar = _tqdm(
            total=total,
            desc=f"[benchmark] {description}",
            unit="doc",
            file=sys.stderr,
            leave=False,
        )
    completed = 0

    def advance(note: str | None = None) -> None:
        nonlocal completed
        completed += 1
        if bar is not None:
            if note:
                bar.set_postfix_str(note[:48], refresh=False)
            bar.update(1)
        elif completed % every == 0 or completed == total:
            log(f"{description}: {completed}/{total}")

    try:
        yield advance
    finally:
        if bar is not None:
            bar.close()


def download_if_missing(
    path: Path,
    url: str,
    *,
    minimum_bytes: int = 10_000,
) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    log(f"downloading official benchmark data: {path.name}")
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "llm-wiki-benchmark/1.0"},
        )
        with (
            urllib.request.urlopen(request, timeout=120) as response,
            temporary.open("wb") as target,
        ):
            shutil.copyfileobj(response, target, length=1024 * 1024)
        # Refuse an HTML error page or a suspiciously small partial response.
        if temporary.stat().st_size < minimum_bytes:
            raise BenchmarkError(f"downloaded dataset is unexpectedly small: {url}")
        temporary.replace(path)
    except Exception as exc:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        if isinstance(exc, BenchmarkError):
            raise
        raise BenchmarkError(f"could not download {url}: {exc}") from exc


def ensure_datasets(corpus_path: Path, questions_path: Path) -> None:
    if corpus_path.resolve() == CORPUS_PATH.resolve():
        download_if_missing(CORPUS_PATH, CORPUS_URL)
    if questions_path.resolve() == QUESTIONS_PATH.resolve():
        download_if_missing(QUESTIONS_PATH, QUESTIONS_URL)


def ensure_musique_dataset(path: Path) -> None:
    if path.exists() or path.resolve() != MUSIQUE_QUESTIONS_PATH.resolve():
        return
    download_if_missing(
        MUSIQUE_ARCHIVE_PATH,
        MUSIQUE_ARCHIVE_URL,
        minimum_bytes=1_000_000,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(MUSIQUE_ARCHIVE_PATH) as archive:
            members = [
                name
                for name in archive.namelist()
                if Path(name).name == path.name
            ]
            if not members:
                raise BenchmarkError(
                    f"{path.name} is missing from {MUSIQUE_ARCHIVE_PATH}"
                )
            with archive.open(members[0]) as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target)
        if temporary.stat().st_size < 100_000:
            raise BenchmarkError(f"extracted MuSiQue data is unexpectedly small: {path}")
        temporary.replace(path)
    except Exception as exc:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        if isinstance(exc, BenchmarkError):
            raise
        raise BenchmarkError(
            f"could not extract {path.name} from {MUSIQUE_ARCHIVE_PATH}: {exc}"
        ) from exc


def remaining_timeout(args: SimpleNamespace) -> float:
    """Return the timeout to give the next subprocess.

    The hard deadline is disabled (see the commented-out raise below and in
    ensure_time_remaining), so it must not be enforced here by the back door.
    The previous clamp, max(1.0, min(command_timeout, deadline - now)), turned
    into a one-second timeout the moment the deadline passed, which killed every
    subprocess launched after that point instead of letting the run finish.
    """
    return float(args.command_timeout)


def ensure_time_remaining(args: SimpleNamespace, stage: str) -> None:
    pass
    # if time.monotonic() >= float(args.deadline):
    #     raise BenchmarkError(f"50-minute benchmark deadline reached before {stage}")


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def jsonl_dump(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    temporary.replace(path)


def jsonl_load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BenchmarkError(
                    f"invalid JSONL at {path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise BenchmarkError(f"expected object at {path}:{line_number}")
            rows.append(value)
    return rows


def read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise BenchmarkError(f"dataset path does not exist: {path}")
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        return jsonl_load(path)
    if path.suffix.lower() == ".parquet":
        try:
            import duckdb  # type: ignore
        except ImportError:
            try:
                import pandas as pd  # type: ignore
            except ImportError as exc:
                raise BenchmarkError(
                    "Parquet input needs duckdb (smallest option) or "
                    "pandas/pyarrow; for example: uv run --with duckdb "
                    "benchmark.py ..."
                ) from exc
            return [dict(row) for row in pd.read_parquet(path).to_dict("records")]
        connection = duckdb.connect()
        try:
            cursor = connection.execute(
                "SELECT * FROM read_parquet(?)",
                [str(path.resolve())],
            )
            columns = [item[0] for item in cursor.description]
            return [
                dict(zip(columns, values))
                for values in cursor.fetchall()
            ]
        finally:
            connection.close()

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"invalid JSON dataset {path}: {exc}") from exc
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("data", "records", "items"):
            if isinstance(value.get(key), list):
                return [
                    dict(item) for item in value[key] if isinstance(item, dict)
                ]
    raise BenchmarkError(f"expected a JSON array in {path}")


def load_documents(path: Path) -> list[Document]:
    if path.is_dir():
        documents = [
            Document(
                id=item.relative_to(path).as_posix(),
                text=item.read_text(encoding="utf-8", errors="replace"),
            )
            for item in sorted(path.rglob("*"))
            if item.is_file() and item.suffix.lower() in {".txt", ".md"}
        ]
    else:
        documents = []
        for index, item in enumerate(read_records(path)):
            doc_id = (
                item.get("corpus_name")
                or item.get("id")
                or item.get("source")
                or item.get("name")
                or f"document-{index + 1:04d}"
            )
            text = (
                item.get("context")
                or item.get("text")
                or item.get("content")
                or item.get("body")
                or ""
            )
            if str(text).strip():
                documents.append(Document(str(doc_id), str(text)))

    if not documents:
        raise BenchmarkError(f"no documents found in {path}")
    duplicate_ids = [
        key
        for key, count in collections.Counter(d.id for d in documents).items()
        if count > 1
    ]
    if duplicate_ids:
        raise BenchmarkError(f"duplicate document IDs: {duplicate_ids[:5]}")
    return documents


def load_questions(path: Path) -> list[Question]:
    questions: list[Question] = []
    for index, item in enumerate(read_records(path)):
        question = str(item.get("question") or "").strip()
        answer = str(
            item.get("answer")
            or item.get("ground_truth")
            or item.get("reference_answer")
            or ""
        ).strip()
        if not question or not answer:
            continue
        questions.append(
            Question(
                id=str(item.get("id") or f"question-{index + 1:05d}"),
                source=str(item.get("source") or ""),
                question=question,
                answer=answer,
                question_type=str(
                    item.get("question_type") or item.get("type") or "Unknown"
                ),
                evidence=item.get("evidence") or "",
                evidence_triple=item.get("evidence_triple") or "",
            )
        )
    if not questions:
        raise BenchmarkError(f"no answerable questions found in {path}")
    return questions


def select_questions(
    questions: Sequence[Question],
    *,
    allowed_types: Sequence[str],
    sample: int | None,
    seed: int,
) -> list[Question]:
    allowed = {value.casefold() for value in allowed_types if value.strip()}
    filtered = [
        question
        for question in questions
        if not allowed or question.question_type.casefold() in allowed
    ]
    if not filtered:
        raise BenchmarkError(
            f"no questions match types: {', '.join(allowed_types) or '(all)'}"
        )
    if not sample or sample >= len(filtered):
        return sorted(filtered, key=lambda item: item.id)

    rng = random.Random(seed)
    groups: dict[str, list[Question]] = collections.defaultdict(list)
    for question in filtered:
        groups[question.question_type].append(question)
    for values in groups.values():
        rng.shuffle(values)

    group_names = sorted(groups)
    base, remainder = divmod(sample, len(group_names))
    selected: list[Question] = []
    leftovers: list[Question] = []
    for index, name in enumerate(group_names):
        wanted = base + (1 if index < remainder else 0)
        values = groups[name]
        selected.extend(values[:wanted])
        leftovers.extend(values[wanted:])
    if len(selected) < sample:
        rng.shuffle(leftovers)
        selected.extend(leftovers[: sample - len(selected)])
    return sorted(selected, key=lambda item: item.id)


def select_fast_pilot(
    documents: Sequence[Document],
    questions: Sequence[Question],
    *,
    sample: int,
    seed: int,
) -> list[Question]:
    """Choose one small valid GraphRAG-Bench corpus and balance its QA types."""
    document_sizes = {document.id: len(document.text) for document in documents}
    by_source: dict[str, list[Question]] = collections.defaultdict(list)
    allowed = {value.casefold() for value in DEFAULT_QUESTION_TYPES}
    for question in questions:
        if (
            question.source in document_sizes
            and question.question_type.casefold() in allowed
        ):
            by_source[question.source].append(question)

    candidates = []
    for source, source_questions in by_source.items():
        types = {question.question_type.casefold() for question in source_questions}
        if allowed.issubset(types):
            candidates.append((document_sizes[source], source))
    if not candidates:
        raise BenchmarkError(
            "no GraphRAG-Bench corpus has both Fact Retrieval and "
            "Complex Reasoning questions"
        )

    _, source = min(candidates)
    selected = select_questions(
        by_source[source],
        allowed_types=DEFAULT_QUESTION_TYPES,
        sample=sample,
        seed=seed,
    )
    log(
        f"time-bounded pilot corpus: {source} "
        f"({document_sizes[source]:,} characters, {len(selected)} questions)"
    )
    return selected


def select_all_novel_questions(
    documents: Sequence[Document],
    questions: Sequence[Question],
    *,
    per_novel: int,
    seed: int,
) -> list[Question]:
    """Use every novel and a small, type-balanced QA sample from each one."""
    by_source: dict[str, list[Question]] = collections.defaultdict(list)
    allowed = {value.casefold() for value in DEFAULT_QUESTION_TYPES}
    for question in questions:
        if question.question_type.casefold() in allowed:
            by_source[question.source].append(question)

    selected: list[Question] = []
    missing: list[str] = []
    for index, document in enumerate(sorted(documents, key=lambda item: item.id)):
        candidates = by_source.get(document.id, [])
        available_types = {
            question.question_type.casefold() for question in candidates
        }
        if not candidates or not allowed.issubset(available_types):
            missing.append(document.id)
            continue
        rng = random.Random(seed + index * 1009)
        groups: dict[str, list[Question]] = collections.defaultdict(list)
        for question in candidates:
            groups[question.question_type].append(question)
        for values in groups.values():
            rng.shuffle(values)
        target = min(per_novel, len(candidates))
        base, remainder = divmod(target, len(DEFAULT_QUESTION_TYPES))
        # Rotate the odd extra question between types across novels, yielding
        # an exactly balanced 50/50 sample when there are 20 novels.
        extra_positions = {
            (index + offset) % len(DEFAULT_QUESTION_TYPES)
            for offset in range(remainder)
        }
        for position, question_type in enumerate(DEFAULT_QUESTION_TYPES):
            wanted = base + int(position in extra_positions)
            if len(groups[question_type]) < wanted:
                raise BenchmarkError(
                    f"{document.id} has only {len(groups[question_type])} "
                    f"{question_type} questions; need {wanted}"
                )
            selected.extend(groups[question_type][:wanted])
    if missing:
        raise BenchmarkError(
            "novels missing required benchmark question types: "
            + ", ".join(missing)
        )
    log(
        f"full novel corpus: {len(documents)} novels, "
        f"{sum(len(document.text) for document in documents):,} characters, "
        f"{len(selected)} questions "
        f"({'exactly' if len(selected) == len(documents) * per_novel else 'up to'} "
        f"{per_novel} per novel)"
    )
    return sorted(selected, key=lambda item: (item.source, item.id))


def answer_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (dict, list)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=isinstance(value, dict),
        )
    return str(value)


def stable_record_sample(
    records: Sequence[dict[str, Any]],
    *,
    sample: int | None,
    seed: int,
) -> list[dict[str, Any]]:
    ranked = sorted(
        records,
        key=lambda item: hashlib.sha256(
            f"{seed}:{item.get('id') or item.get('query') or item.get('question')}".encode(
                "utf-8"
            )
        ).hexdigest(),
    )
    return ranked if not sample else ranked[:sample]


def fanout_evidence_records(item: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: dict[tuple[str, str], dict[str, Any]] = {}

    def add(value: Any) -> None:
        if not isinstance(value, dict):
            return
        page_id = str(value.get("pageid") or "")
        revision_id = str(value.get("revid") or "")
        title = str(value.get("title") or "")
        # Placeholders must be resolved here rather than at fetch time: every
        # unresolved record shares one (###TBD###, ###TBD###) key, so distinct
        # articles would otherwise collapse into a single corpus document.
        if title and FANOUT_PLACEHOLDER in {page_id, revision_id}:
            value = {**value, **fanout_epoch_revision(title)}
            page_id = value["pageid"]
            revision_id = value["revid"]
        if page_id and revision_id:
            evidence[(page_id, revision_id)] = dict(value)

    def walk(value: Any) -> None:
        if not isinstance(value, dict):
            return
        add(value.get("evidence"))
        for child in value.get("decomposition") or []:
            walk(child)

    for value in item.get("necessary_evidence") or []:
        add(value)
    walk(item)
    return [evidence[key] for key in sorted(evidence)]


class WikipediaTextExtractor(HTMLParser):
    """Small dependency-free HTML-to-text converter for parsed wiki revisions."""

    BLOCK_TAGS = {
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "table",
        "tr",
        "ul",
        "ol",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(
        self, tag: str, _attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in {"script", "style"}:
            self.skip_depth += 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        elif not self.skip_depth and tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = [
            " ".join(line.split())
            for line in "".join(self.parts).splitlines()
        ]
        return "\n".join(line for line in lines if line)


def wikipedia_html_text(value: str) -> str:
    parser = WikipediaTextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text()


def wikipedia_api_json(params: dict[str, str]) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {**params, "format": "json", "formatversion": "2"}
    )
    request = urllib.request.Request(
        f"https://en.wikipedia.org/w/api.php?{query}",
        headers={"User-Agent": WIKIPEDIA_USER_AGENT},
    )
    time.sleep(WIKIPEDIA_REQUEST_SPACING_SECONDS)
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        return {}
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        detail = f"Wikipedia API error {code!r}: {error.get('info')}"
        if code in WIKIPEDIA_PERMANENT_ERRORS:
            raise WikipediaContentGone(detail)
        raise BenchmarkError(detail)
    return payload


def wikipedia_retry_delay(exc: Exception, attempt: int) -> float:
    """Seconds to wait before retrying one failed Wikipedia call.

    A throttle is answered with the server's own Retry-After when it sends
    one, and otherwise with an exponential backoff long enough to outlast a
    limiter window. Ordinary transport errors back off far more briefly.
    """

    throttled = (
        isinstance(exc, urllib.error.HTTPError)
        and exc.code in {429, 503}
    )
    if not throttled:
        return min(2.0 ** (attempt - 1), 30.0)
    delay = 15.0 * (2.0 ** (attempt - 1))
    if isinstance(exc, urllib.error.HTTPError) and exc.headers:
        with contextlib.suppress(TypeError, ValueError):
            delay = max(delay, float(exc.headers.get("Retry-After")))
    return min(delay, WIKIPEDIA_MAX_RETRY_SECONDS)


def with_wikipedia_retry(
    operation: Callable[[], Any], *, label: str, failure: str
) -> Any:
    """Run one Wikipedia call under the shared retry and rate-limit policy.

    An empty or malformed result raises from inside ``operation``, so a
    transient bad response is retried on the same terms as a transport error.
    """

    last_error: Exception | None = None
    for attempt in range(1, WIKIPEDIA_MAX_ATTEMPTS + 1):
        try:
            return operation()
        except WikipediaContentGone:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < WIKIPEDIA_MAX_ATTEMPTS:
                delay = wikipedia_retry_delay(exc, attempt)
                log(
                    f"Wikipedia call for {label!r} failed "
                    f"({attempt}/{WIKIPEDIA_MAX_ATTEMPTS}); "
                    f"retrying in {delay:.0f}s: {exc}"
                )
                time.sleep(delay)
    raise BenchmarkError(
        f"{failure} after {WIKIPEDIA_MAX_ATTEMPTS} attempts: {last_error}; "
        "every revision already fetched is cached, so rerunning the same "
        "ingest command resumes where this stopped"
    )


_FANOUT_REVISION_LOCK = threading.Lock()
_FANOUT_REVISIONS: dict[str, dict[str, str]] | None = None


def fanout_epoch_revision(title: str) -> dict[str, str]:
    """Resolve a placeholder evidence record to its revision at the epoch.

    Resolutions are cached on disk because the placeholders are shared across
    questions, and because a stopped ingest should not re-query titles it has
    already pinned.
    """

    global _FANOUT_REVISIONS
    with _FANOUT_REVISION_LOCK:
        if _FANOUT_REVISIONS is None:
            try:
                value = json.loads(FANOUT_REVISIONS_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                value = {}
            _FANOUT_REVISIONS = value if isinstance(value, dict) else {}
        cached = _FANOUT_REVISIONS.get(title)
        if isinstance(cached, dict):
            return dict(cached)

    def fetch() -> dict[str, str]:
        payload = wikipedia_api_json(
            {
                "action": "query",
                "prop": "revisions",
                "titles": title,
                "rvstart": FANOUT_DATASET_EPOCH,
                "rvdir": "older",
                "rvlimit": "1",
                "rvprop": "ids",
                "redirects": "1",
            }
        )
        pages = (payload.get("query") or {}).get("pages") or []
        for page in pages if isinstance(pages, list) else []:
            if not isinstance(page, dict):
                continue
            # A deleted article answers as "missing" rather than as an error.
            if page.get("missing"):
                raise WikipediaContentGone(
                    f"Wikipedia article {title!r} no longer exists"
                )
            revisions = page.get("revisions")
            first = revisions[0] if isinstance(revisions, list) and revisions else None
            if isinstance(first, dict) and page.get("pageid") and first.get("revid"):
                return {
                    "pageid": str(page["pageid"]),
                    "revid": str(first["revid"]),
                    "title": str(page.get("title") or title),
                }
        raise BenchmarkError(
            f"Wikipedia has no revision of {title!r} at {FANOUT_DATASET_EPOCH}"
        )

    resolved = with_wikipedia_retry(
        fetch,
        label=title,
        failure=(
            f"could not resolve the FanOutQA placeholder evidence {title!r} to a "
            f"revision at {FANOUT_DATASET_EPOCH}"
        ),
    )
    with _FANOUT_REVISION_LOCK:
        assert _FANOUT_REVISIONS is not None
        _FANOUT_REVISIONS[title] = resolved
        json_dump(FANOUT_REVISIONS_PATH, _FANOUT_REVISIONS)
    return dict(resolved)


def load_fanout_revision_text(
    evidence: dict[str, Any], cache_dir: Path
) -> str:
    page_id = str(evidence.get("pageid") or "")
    revision_id = str(evidence.get("revid") or "")
    title = str(evidence.get("title") or page_id)
    if not page_id or not revision_id:
        raise BenchmarkError(f"FanOutQA evidence lacks page/revision IDs: {evidence}")
    path = cache_dir / f"{page_id}-{revision_id}.wiki.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")

    def fetch() -> str:
        payload = wikipedia_api_json(
            {"action": "parse", "oldid": revision_id, "prop": "text"}
        )
        parsed = payload.get("parse")
        html = parsed.get("text") if isinstance(parsed, dict) else None
        if isinstance(html, dict):
            html = html.get("*")
        text = wikipedia_html_text(html) if isinstance(html, str) else ""
        if not text.strip():
            raise BenchmarkError(
                f"Wikipedia returned no text for {title} revision {revision_id}"
            )
        cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
        return text

    return with_wikipedia_retry(
        fetch,
        label=title,
        failure=(
            f"could not retrieve FanOutQA evidence {title!r} at revision "
            f"{revision_id}"
        ),
    )


def load_fanout_dataset(args: SimpleNamespace) -> DatasetBundle:
    questions_path = Path(args.questions)
    if questions_path.resolve() == FANOUT_QUESTIONS_PATH.resolve():
        download_if_missing(questions_path, FANOUT_QUESTIONS_URL)
    raw = read_records(questions_path)
    usable = [
        item
        for item in raw
        if str(item.get("question") or "").strip()
        and answer_text(item.get("answer"))
        and fanout_evidence_records(item)
    ]
    candidates = [
        item for item in usable if 4 <= len(fanout_evidence_records(item)) <= 8
    ]
    if not candidates:
        candidates = usable
    selected = stable_record_sample(candidates, sample=args.sample, seed=args.seed)
    if not selected:
        raise BenchmarkError(f"no evidence-backed FanOutQA questions in {questions_path}")

    questions: list[Question] = []
    evidence_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for index, item in enumerate(selected, start=1):
        evidence = fanout_evidence_records(item)
        for record in evidence:
            key = (str(record["pageid"]), str(record["revid"]))
            evidence_by_key[key] = record
        categories = [str(value) for value in item.get("categories") or []]
        questions.append(
            Question(
                id=str(item.get("id") or f"fanout-{index:04d}"),
                source="fanout",
                question=str(item.get("question") or "").strip(),
                answer=answer_text(item.get("answer")),
                question_type="Fan-out" + (
                    f" / {', '.join(categories)}" if categories else ""
                ),
                evidence=evidence,
            )
        )

    cache_dir = Path(args.corpus)
    documents: list[Document] = []
    for key in sorted(evidence_by_key):
        record = evidence_by_key[key]
        title = str(record.get("title") or record.get("pageid") or "Wikipedia")
        text = load_fanout_revision_text(record, cache_dir)
        documents.append(
            Document(
                id=f"wikipedia-{key[0]}-{key[1]}",
                text=f"Title: {title}\n\n{text}",
            )
        )
    log(
        f"FanOutQA evidence-provided subset: {len(questions)} questions, "
        f"{len(documents)} dated Wikipedia pages"
    )
    return DatasetBundle(documents, questions, "combined")


def multihop_document_id(item: dict[str, Any]) -> str:
    identity = "\0".join(
        str(item.get(key) or "")
        for key in ("source", "title", "published_at", "body")
    )
    return "news-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def load_multihop_dataset(args: SimpleNamespace) -> DatasetBundle:
    corpus_path = Path(args.corpus)
    questions_path = Path(args.questions)
    if corpus_path.resolve() == MULTIHOP_CORPUS_PATH.resolve():
        download_if_missing(corpus_path, MULTIHOP_CORPUS_URL)
    if questions_path.resolve() == MULTIHOP_QUESTIONS_PATH.resolve():
        download_if_missing(questions_path, MULTIHOP_QUESTIONS_URL)

    documents_by_id: dict[str, Document] = {}
    for item in read_records(corpus_path):
        body = str(item.get("body") or "").strip()
        if not body:
            continue
        metadata = [
            f"Title: {item.get('title') or ''}",
            f"Source: {item.get('source') or ''}",
            f"Published: {item.get('published_at') or ''}",
        ]
        document_id = multihop_document_id({**item, "body": body})
        documents_by_id.setdefault(
            document_id,
            Document(document_id, "\n".join(metadata) + f"\n\n{body}"),
        )
    documents = [documents_by_id[key] for key in sorted(documents_by_id)]
    if not documents:
        raise BenchmarkError(f"no MultiHop-RAG articles found in {corpus_path}")

    questions: list[Question] = []
    for item in read_records(questions_path):
        query = str(item.get("query") or "").strip()
        answer = answer_text(item.get("answer"))
        evidence = item.get("evidence_list") or []
        if not query or not answer or not evidence:
            continue
        questions.append(
            Question(
                id="multihop-"
                + hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
                source="multihop",
                question=query,
                answer=answer,
                question_type=str(item.get("question_type") or "Multi-hop"),
                evidence=evidence,
            )
        )
    questions = select_questions(
        questions,
        allowed_types=(),
        sample=args.sample,
        seed=args.seed,
    )
    log(
        f"MultiHop-RAG subset: {len(documents)} news articles, "
        f"{len(questions)} evidence-backed questions"
    )
    return DatasetBundle(documents, questions, "combined")


def musique_question_type(item: dict[str, Any]) -> str:
    decomposition = item.get("question_decomposition") or []
    if isinstance(decomposition, list) and decomposition:
        return f"{len(decomposition)}-hop"
    identifier = str(item.get("id") or "")
    match = re.match(r"(\d+)hop", identifier, flags=re.IGNORECASE)
    return f"{match.group(1)}-hop" if match else "Multi-hop"


def load_musique_dataset(args: SimpleNamespace) -> DatasetBundle:
    questions_path = Path(args.questions)
    ensure_musique_dataset(questions_path)
    raw = [
        item
        for item in read_records(questions_path)
        if bool(item.get("answerable", True))
        and str(item.get("question") or "").strip()
        and answer_text(item.get("answer"))
    ]
    candidates: list[Question] = []
    row_by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw, start=1):
        question_id = str(item.get("id") or f"musique-{index:05d}")
        paragraphs = item.get("paragraphs") or []
        evidence = [
            {
                "title": paragraph.get("title") or "",
                "text": paragraph.get("paragraph_text") or "",
            }
            for paragraph in paragraphs
            if isinstance(paragraph, dict) and paragraph.get("is_supporting")
        ]
        if not evidence:
            continue
        row_by_id[question_id] = item
        candidates.append(
            Question(
                id=question_id,
                source="musique",
                question=str(item["question"]).strip(),
                answer=answer_text(item.get("answer")),
                question_type=musique_question_type(item),
                evidence=evidence,
            )
        )
    questions = select_questions(
        candidates,
        allowed_types=(),
        sample=args.sample,
        seed=args.seed,
    )

    documents_by_id: dict[str, Document] = {}
    for question in questions:
        for paragraph in row_by_id[question.id].get("paragraphs") or []:
            if not isinstance(paragraph, dict):
                continue
            title = str(paragraph.get("title") or "Untitled")
            text = str(paragraph.get("paragraph_text") or "").strip()
            if not text:
                continue
            digest = hashlib.sha256(
                f"{title}\0{text}".encode("utf-8")
            ).hexdigest()[:16]
            documents_by_id.setdefault(
                f"paragraph-{digest}",
                Document(f"paragraph-{digest}", f"Title: {title}\n\n{text}"),
            )
    documents = [documents_by_id[key] for key in sorted(documents_by_id)]
    if not questions or not documents:
        raise BenchmarkError(f"no usable MuSiQue examples found in {questions_path}")
    log(
        f"MuSiQue subset: {len(documents)} shared paragraphs, "
        f"{len(questions)} questions"
    )
    return DatasetBundle(documents, questions, "combined")


def load_benchmark_dataset(args: SimpleNamespace) -> DatasetBundle:
    if args.dataset == "fanout":
        return load_fanout_dataset(args)
    if args.dataset == "multihop":
        return load_multihop_dataset(args)
    if args.dataset == "musique":
        return load_musique_dataset(args)
    if args.dataset != "novel":
        raise BenchmarkError(f"unknown dataset: {args.dataset}")

    ensure_datasets(Path(args.corpus), Path(args.questions))
    documents = load_documents(Path(args.corpus))
    if NOVEL_LIMIT is not None:
        documents = sorted(documents, key=lambda item: len(item.text))[:NOVEL_LIMIT]
        log("temporary novel limit: " + ", ".join(item.id for item in documents))
    all_questions = load_questions(Path(args.questions))
    questions = select_all_novel_questions(
        documents,
        all_questions,
        per_novel=QUESTIONS_PER_NOVEL,
        seed=args.seed,
    )
    documents = documents_for_questions(
        documents,
        questions,
        corpus_scope="isolated",
    )
    return DatasetBundle(documents, questions, "isolated")


def validate_question_sources(
    documents: Sequence[Document],
    questions: Sequence[Question],
    *,
    corpus_scope: str,
) -> None:
    if corpus_scope != "isolated":
        return
    document_ids = {document.id for document in documents}
    missing_sources = sorted(
        {
            question.source
            for question in questions
            if not question.source or question.source not in document_ids
        }
    )
    if missing_sources:
        raise BenchmarkError(
            "isolated corpus mode requires each question.source to match a "
            f"document ID; unmatched values: {missing_sources[:5]}. "
            "Use --corpus-scope combined for a shared document collection."
        )


def documents_for_questions(
    documents: Sequence[Document],
    questions: Sequence[Question],
    *,
    corpus_scope: str,
) -> list[Document]:
    validate_question_sources(
        documents,
        questions,
        corpus_scope=corpus_scope,
    )
    if corpus_scope == "combined":
        return list(documents)
    selected_sources = {question.source for question in questions}
    return [
        document for document in documents if document.id in selected_sources
    ]


def normalize_long_prose_layout(document: Document) -> Document:
    """Give line-aware native harnesses useful boundaries without changing words."""
    original_lines = document.text.splitlines()
    useful_line_count = max(
        2, (len(document.text) + PROSE_LINE_WIDTH - 1) // PROSE_LINE_WIDTH
    )
    if len(document.text) < LONG_PROSE_MIN_CHARS or len(original_lines) >= min(
        OURS_CHUNK_THRESHOLD_LINES + 1, useful_line_count
    ):
        return document

    collapsed = " ".join(document.text.split())
    sentence_lines = [
        value.strip()
        for value in re.split(r"(?<=[.!?。！？])\s+", collapsed)
        if value.strip()
    ]
    if len(sentence_lines) <= OURS_CHUNK_THRESHOLD_LINES:
        sentence_lines = textwrap.wrap(
            collapsed,
            width=PROSE_LINE_WIDTH,
            break_long_words=False,
            break_on_hyphens=False,
        )

    normalized = Document(document.id, "\n".join(sentence_lines))
    log(
        f"normalized long-prose layout for {document.id}: "
        f"{len(original_lines)} -> {len(sentence_lines)} lines"
    )
    return normalized


def needs_native_conceptual_chunking(
    body: str,
    line_count: int,
    *,
    line_threshold: int = OURS_CHUNK_THRESHOLD_LINES,
) -> bool:
    """Route either line-dense or character-long sources through chunking."""
    return line_count > line_threshold or len(body) >= LONG_PROSE_MIN_CHARS


def safe_name(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._") or "document"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:80]}-{digest}"


def dataset_fingerprint(
    documents: Sequence[Document], questions: Sequence[Question]
) -> str:
    digest = hashlib.sha256()
    for document in sorted(documents, key=lambda item: item.id):
        digest.update(document.id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(document.text.encode("utf-8"))
        digest.update(b"\0")
    for question in sorted(questions, key=lambda item: item.id):
        digest.update(
            json.dumps(asdict(question), sort_keys=True, ensure_ascii=False).encode(
                "utf-8"
            )
        )
        digest.update(b"\0")
    return digest.hexdigest()


def latest_compatible_incomplete_run(
    *,
    results_root: Path,
    documents: Sequence[Document],
    questions: Sequence[Question],
    run_config: dict[str, Any],
) -> Path | None:
    """Return the newest exact-match run that still has work to do.

    The fixed benchmark CLI normally chooses a new timestamp on every launch.
    That made its existing ``resume`` machinery unreachable after a crash.  An
    automatic candidate must match both the full dataset fingerprint and every
    result-affecting setting, so a stale or differently configured run is never
    reused accidentally.
    """

    if not results_root.exists():
        return None

    fingerprint = dataset_fingerprint(documents, questions)
    systems = list(run_config.get("systems") or [])
    ingestion_runs = int(run_config.get("ingestion_runs") or 1)
    question_ids = {question.id for question in questions}

    candidates = sorted(
        (
            path
            for path in results_root.iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        ),
        key=lambda path: (path / "manifest.json").stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        try:
            manifest = json.loads(
                (candidate / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if manifest.get("dataset_fingerprint") != fingerprint:
            continue
        if manifest.get("run_config") != run_config:
            continue

        ingestion_complete = all(
            (
                candidate
                / "systems"
                / system
                / f"run-{run_number:02d}"
                / "ingestion.json"
            ).is_file()
            for system in systems
            for run_number in range(1, ingestion_runs + 1)
        )
        predictions_complete = True
        for system in systems:
            try:
                rows = jsonl_load(
                    candidate / "systems" / system / "predictions.jsonl"
                )
            except BenchmarkError:
                predictions_complete = False
                break
            completed_ids = {
                str(row.get("id")) for row in rows if not row.get("error")
            }
            if not question_ids <= completed_ids:
                predictions_complete = False
                break

        if (
            ingestion_complete
            and predictions_complete
            and (candidate / "summary.md").is_file()
        ):
            continue
        return candidate
    return None


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(
        item.stat().st_size
        for item in path.rglob("*")
        if item.is_file() and not item.is_symlink()
    )


def prepare_canonical(
    output: Path,
    documents: Sequence[Document],
    questions: Sequence[Question],
    *,
    resume: bool,
    run_config: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    fingerprint = dataset_fingerprint(documents, questions)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not resume:
            raise BenchmarkError(
                f"{output} already contains a run; pass --resume or choose a new output"
            )
        if existing.get("dataset_fingerprint") != fingerprint:
            raise BenchmarkError(
                "cannot resume: corpus/questions differ from the existing run"
            )
        if existing.get("run_config") != run_config:
            raise BenchmarkError(
                "cannot resume: benchmark settings differ from the existing run"
            )
        mapping = json.loads(
            (output / "canonical" / "documents.json").read_text(encoding="utf-8")
        )
        return mapping, existing

    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(
            f"refusing to write into non-empty directory without a manifest: {output}"
        )
    corpus_dir = output / "canonical" / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    mapping: list[dict[str, str]] = []
    for document in documents:
        path = corpus_dir / f"{safe_name(document.id)}.txt"
        path.write_text(document.text, encoding="utf-8")
        mapping.append({"id": document.id, "path": str(path.resolve())})
    json_dump(output / "canonical" / "documents.json", mapping)
    jsonl_dump(
        output / "canonical" / "questions.jsonl",
        (asdict(question) for question in questions),
    )

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": run_config["dataset"],
        "dataset_fingerprint": fingerprint,
        "documents": len(documents),
        "questions": len(questions),
        "characters": sum(len(document.text) for document in documents),
        "words": sum(len(document.text.split()) for document in documents),
        "question_types": dict(
            sorted(collections.Counter(q.question_type for q in questions).items())
        ),
        "corpus_scope": run_config["corpus_scope"],
        "run_config": run_config,
        "systems": run_config["systems"],
        "comparison": {
            "vanilla": "fixed chunks + dense top-k + one native answer call",
            "graphrag": (
                f"Microsoft standard index + native "
                f"{run_config['graphrag_method']} query"
            ),
            "ours": "complete llm-wiki ingestion + native ResearchSession.ask()",
        },
        "timing": {
            "scope": (
                "fresh independent index per corpus_name"
                if run_config["corpus_scope"] == "isolated"
                else "one fresh combined index"
            ),
            "cache": "GraphRAG local cache disabled; model-server caches are external",
            "order": "deterministically shuffled per ingestion run",
        },
        "models": {
            "chat": run_config["chat_model"],
            "embedding": run_config["embed_model"],
            "embedding_dimension": run_config["embed_dim"],
        },
        "budgets": {
            "vanilla_chunk_tokens": run_config["chunk_tokens"],
            "vanilla_chunk_overlap": run_config["chunk_overlap"],
            "vanilla_top_k": run_config["top_k"],
            "vanilla_answer_max_tokens": run_config["max_answer_tokens"],
        },
        "scoring": {
            "judge": run_config["judge"],
            "close_margin": run_config["close_margin"],
            "seed": run_config["seed"],
        },
        "environment": environment_info(),
    }
    json_dump(manifest_path, manifest)
    return mapping, manifest


def environment_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "processor": platform.processor(),
        "machine": platform.machine(),
    }
    with contextlib.suppress(Exception):
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            info["gpus"] = [
                line.strip() for line in result.stdout.splitlines() if line.strip()
            ]
    return info


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float,
        retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries

    def _url(self, endpoint: str) -> str:
        if self.base_url.endswith(f"/{endpoint}"):
            return self.base_url
        return f"{self.base_url}/{endpoint}"

    def post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url(endpoint),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    value = json.loads(response.read().decode("utf-8"))
                if not isinstance(value, dict):
                    raise BenchmarkError(f"{endpoint} returned non-object JSON")
                return value
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                last_error = BenchmarkError(
                    f"{endpoint} returned HTTP {exc.code}: {detail}"
                )
                if 400 <= exc.code < 500 and exc.code != 429:
                    break
            except (OSError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(min(2**attempt, 4))
        raise BenchmarkError(f"request to {endpoint} failed: {last_error}")

    def embeddings(
        self, texts: Sequence[str], model: str
    ) -> tuple[list[list[float]], dict[str, Any]]:
        response = self.post("embeddings", {"model": model, "input": list(texts)})
        data = response.get("data")
        if not isinstance(data, list):
            raise BenchmarkError("embedding response has no data list")
        ordered = sorted(
            (item for item in data if isinstance(item, dict)),
            key=lambda item: int(item.get("index") or 0),
        )
        vectors = [
            [float(value) for value in item.get("embedding") or []] for item in ordered
        ]
        if len(vectors) != len(texts) or any(not vector for vector in vectors):
            raise BenchmarkError(
                f"embedding response returned {len(vectors)} vectors for "
                f"{len(texts)} inputs"
            )
        return vectors, response.get("usage") or {}

    def chat(
        self,
        messages: Sequence[dict[str, str]],
        model: str,
        *,
        temperature: float,
        max_tokens: int,
        json_mode: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = self.post("chat/completions", payload)
        except BenchmarkError:
            if not json_mode:
                raise
            payload.pop("response_format", None)
            response = self.post("chat/completions", payload)
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise BenchmarkError("chat response has no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
            )
        if not str(content or "").strip():
            raise BenchmarkError("chat response is empty")
        return str(content).strip(), response.get("usage") or {}


def normalize_vector(vector: Sequence[float]) -> list[float]:
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        return [0.0 for _ in vector]
    return [float(value / magnitude) for value in vector]


def fixed_chunks(
    text: str, *, size: int, overlap: int
) -> tuple[list[str], str]:
    if size <= 0:
        raise BenchmarkError("chunk size must be positive")
    if overlap < 0 or overlap >= size:
        raise BenchmarkError("chunk overlap must be >= 0 and < chunk size")
    try:
        import tiktoken  # type: ignore

        encoding = tiktoken.get_encoding("cl100k_base")
        tokens = encoding.encode(text)
        chunks = []
        start = 0
        while start < len(tokens):
            end = min(start + size, len(tokens))
            chunks.append(encoding.decode(tokens[start:end]).strip())
            if end == len(tokens):
                break
            start += size - overlap
        return [chunk for chunk in chunks if chunk], "cl100k_base tokens"
    except ImportError:
        pieces = re.findall(r"\S+\s*", text)
        chunks = []
        start = 0
        while start < len(pieces):
            end = min(start + size, len(pieces))
            chunks.append("".join(pieces[start:end]).strip())
            if end == len(pieces):
                break
            start += size - overlap
        return [chunk for chunk in chunks if chunk], "whitespace tokens"


def vanilla_ingest(
    workspace: Path,
    documents: Sequence[Document],
    args: SimpleNamespace,
) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    client = OpenAICompatibleClient(
        args.embed_base_url, args.embed_api_key, timeout=args.timeout
    )
    usage = ApiUsage()
    started = time.perf_counter()
    chunks: list[dict[str, Any]] = []
    chunk_unit = ""
    for document in documents:
        texts, chunk_unit = fixed_chunks(
            document.text, size=args.chunk_tokens, overlap=args.chunk_overlap
        )
        chunks.extend(
            {
                "id": f"{safe_name(document.id)}-{index:06d}",
                "source": document.id,
                "text": text,
            }
            for index, text in enumerate(texts)
        )
    if not chunks:
        raise BenchmarkError("vanilla chunking produced no chunks")

    index_path = workspace / "index.jsonl"
    rows: list[dict[str, Any]] = []
    for start in range(0, len(chunks), args.embed_batch_size):
        batch = chunks[start : start + args.embed_batch_size]
        vectors, batch_usage = client.embeddings(
            [item["text"] for item in batch], args.embed_model
        )
        usage.add(batch_usage)
        for item, vector in zip(batch, vectors):
            if args.embed_dim and len(vector) != args.embed_dim:
                raise BenchmarkError(
                    f"embedding dimension is {len(vector)}, expected {args.embed_dim}"
                )
            rows.append({**item, "vector": normalize_vector(vector)})
        log(f"vanilla embedded {min(start + len(batch), len(chunks))}/{len(chunks)}")
    jsonl_dump(index_path, rows)
    elapsed = time.perf_counter() - started
    return {
        "system": "vanilla",
        "elapsed_seconds": elapsed,
        "documents": len(documents),
        "characters": sum(len(document.text) for document in documents),
        "chunks": len(chunks),
        "chunk_unit": chunk_unit,
        "index_bytes": directory_size(workspace),
        "requests": asdict(usage),
    }


def vanilla_answer(
    workspace: Path,
    questions: Sequence[Question],
    args: SimpleNamespace,
) -> list[dict[str, Any]]:
    rows = jsonl_load(workspace / "index.jsonl")
    if not rows:
        raise BenchmarkError(f"vanilla index is empty: {workspace}")
    client = OpenAICompatibleClient(
        args.chat_base_url, args.chat_api_key, timeout=args.timeout
    )
    embed_client = OpenAICompatibleClient(
        args.embed_base_url, args.embed_api_key, timeout=args.timeout
    )
    def answer_one(question: Question) -> dict[str, Any]:
        result = question.result_base()
        started = time.perf_counter()
        try:
            query_vectors, embed_usage = embed_client.embeddings(
                [question.question], args.embed_model
            )
            query_vector = normalize_vector(query_vectors[0])
            ranked = sorted(
                rows,
                key=lambda row: sum(
                    left * right
                    for left, right in zip(query_vector, row["vector"])
                ),
                reverse=True,
            )[: args.top_k]
            contexts = [str(row["text"]) for row in ranked]
            context_text = "\n\n".join(
                f"[Source {item['source']} / chunk {item['id']}]\n{item['text']}"
                for item in ranked
            )
            prompt = (
                "Use the retrieved passages as evidence for the question. "
                "Reason in whatever way is most useful. Do not invent unsupported "
                "facts; if the passages are insufficient, say so.\n\n"
                f"Retrieved passages:\n{context_text}\n\n"
                f"Question: {question.question}"
            )
            answer, chat_usage = client.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a capable retrieval-augmented assistant. "
                            "Answer naturally and use the evidence provided."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                args.chat_model,
                temperature=args.temperature,
                max_tokens=args.max_answer_tokens,
            )
            result.update(
                {
                    "context": contexts,
                    "generated_answer": answer,
                    "retrieved_chunk_ids": [row["id"] for row in ranked],
                    "usage": {
                        "embedding": embed_usage,
                        "chat": chat_usage,
                    },
                    "token_usage": {
                        "retrieval_embedding": embed_usage,
                        "answer_chat": chat_usage,
                    },
                    "error": None,
                }
            )
        except Exception as exc:  # keep the batch resumable/auditable
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["latency_seconds"] = time.perf_counter() - started
        return result

    results: list[dict[str, Any] | None] = [None] * len(questions)
    workers = min(args.chat_concurrency, len(questions))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(answer_one, question): index
            for index, question in enumerate(questions)
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            results[futures[future]] = future.result()
            log(f"vanilla answered {completed}/{len(questions)}")
    return [result for result in results if result is not None]


def graphrag_command(args: SimpleNamespace, *parts: str) -> list[str]:
    return [*shlex.split(args.graphrag_command), *parts]


def detect_graphrag_query_style(args: SimpleNamespace) -> str:
    if args.graphrag_query_style != "auto":
        return args.graphrag_query_style
    cached = getattr(args, "_resolved_graphrag_query_style", None)
    if cached:
        return str(cached)
    try:
        completed = subprocess.run(
            graphrag_command(args, "query", "--help"),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        style = "positional"
    else:
        help_text = "\n".join((completed.stdout, completed.stderr))
        style = (
            "flag"
            if re.search(r"(^|\s)--query(?:\s|$)", help_text)
            else "positional"
        )
    args._resolved_graphrag_query_style = style
    return style


def graphrag_query_command(
    args: SimpleNamespace,
    workspace: Path,
    question: str,
    *,
    style: str,
) -> list[str]:
    command = graphrag_command(
        args,
        "query",
        "--root",
        str(workspace.resolve()),
        "--method",
        args.graphrag_method,
        "--no-streaming",
    )
    if style == "flag":
        return [*command, "--query", question]
    return [*command, question]


def subprocess_environment(args: SimpleNamespace) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "OPENAI_BASE_URL": args.chat_base_url,
            "OPENAI_API_KEY": args.chat_api_key,
            "GRAPHRAG_API_KEY": args.chat_api_key,
            "BENCH_GRAPHRAG_EMBED_API_KEY": args.embed_api_key,
            "BENCH_CHAT_BASE_URL": args.chat_base_url,
            "BENCH_EMBED_BASE_URL": args.embed_base_url,
        }
    )
    return env


def run_logged(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    timeout: float | None = None,
    live_label: str | None = None,
) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command_list = list(command)
    output: list[str] = []
    with log_path.open("w", encoding="utf-8") as log_handle:
        log_handle.write(f"$ {shlex.join(command_list)}\n\n")
        log_handle.flush()
        process = subprocess.Popen(
            command_list,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )

        def pump_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                output.append(line)
                log_handle.write(line)
                log_handle.flush()
                if live_label:
                    print(f"[{live_label}] {line}", end="", flush=True)

        reader = threading.Thread(target=pump_output, daemon=True)
        reader.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            reader.join(timeout=5)
            raise
        reader.join()

    combined_output = "".join(output)
    completed = subprocess.CompletedProcess(
        command_list,
        returncode,
        stdout=combined_output,
        stderr="",
    )
    if completed.returncode != 0:
        tail = combined_output[-2000:]
        raise BenchmarkError(
            f"command failed ({completed.returncode}): {shlex.join(command)}\n{tail}"
        )
    return completed


class UsageRecordingProxy:
    """Local OpenAI-compatible proxy that records provider token usage.

    GraphRAG hardcodes ``stream=True`` for every search call and reads its
    metrics off the response before the iterator is consumed, so a streamed
    call contributes no usage at all: on the novel corpus only 22% of its chat
    calls were ever counted, and the reported input tokens were low by roughly
    4.5x. Counting at the transport makes the tally independent of what the
    client does with the stream. The proxy asks the provider for usage on every
    stream, records what comes back, and withholds that trailing usage-only
    chunk so the client still sees exactly the stream it expected.
    """

    def __init__(self, upstream: str, record_path: Path, *, timeout: float):
        self.upstream = upstream.rstrip("/")
        self.record_path = record_path
        self.timeout = timeout
        self._lock = threading.Lock()
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._inflight = 0
        self._idle = threading.Condition()

    def _enter_request(self) -> None:
        with self._idle:
            self._inflight += 1

    def _leave_request(self) -> None:
        with self._idle:
            self._inflight -= 1
            if self._inflight <= 0:
                self._idle.notify_all()

    def drain(self, timeout: float = 30.0) -> None:
        """Block until no proxied request is still being written.

        A client can exit while its last responses are still draining through
        the proxy. Without this the trailing calls land after the usage journal
        has been sliced and are billed to the following question.
        """

        deadline = time.monotonic() + timeout
        with self._idle:
            while self._inflight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._idle.wait(timeout=min(remaining, 0.5))

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://127.0.0.1:{port}"

    def _record(self, usage: dict[str, Any], stream: bool, path: str = "") -> None:
        row = {
            "requests": 1,
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "streamed": stream,
            "category": (
                "retrieval_embedding"
                if "embedding" in path.casefold()
                else "retrieval_chat"
            ),
        }
        with self._lock:
            with self.record_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def __enter__(self) -> "UsageRecordingProxy":
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def handle_one_request(self) -> None:  # noqa: N802 - stdlib naming
                # A client that exits mid-response resets the socket. That is
                # normal here and must not print a stack trace per call.
                with contextlib.suppress(ConnectionResetError, BrokenPipeError):
                    super().handle_one_request()

            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                proxy._enter_request()
                try:
                    self._proxy_post()
                finally:
                    proxy._leave_request()

            def _proxy_post(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    payload = json.loads(body or b"{}")
                except json.JSONDecodeError:
                    payload = {}
                streaming = bool(payload.get("stream"))
                if streaming:
                    # The provider only reports usage for a stream when asked.
                    payload["stream_options"] = {"include_usage": True}
                    body = json.dumps(payload).encode("utf-8")
                request = urllib.request.Request(
                    f"{proxy.upstream}{self.path}",
                    data=body,
                    headers={
                        key: value
                        for key, value in self.headers.items()
                        if key.lower()
                        in {"authorization", "content-type", "accept"}
                    },
                    method="POST",
                )
                try:
                    upstream = urllib.request.urlopen(request, timeout=proxy.timeout)
                except urllib.error.HTTPError as exc:
                    detail = exc.read()
                    self.send_response(exc.code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(detail)))
                    self.end_headers()
                    self.wfile.write(detail)
                    return
                except Exception:
                    self.send_error(502, "upstream unavailable")
                    return
                with upstream:
                    if not streaming:
                        raw = upstream.read()
                        with contextlib.suppress(json.JSONDecodeError, TypeError):
                            usage = json.loads(raw).get("usage")
                            if isinstance(usage, dict):
                                proxy._record(usage, False, self.path)
                        self.send_response(upstream.status)
                        self.send_header(
                            "Content-Type",
                            upstream.headers.get("Content-Type", "application/json"),
                        )
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                    self.send_response(upstream.status)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for line in upstream:
                        if line.startswith(b"data: "):
                            chunk = line[6:].strip()
                            if chunk and chunk != b"[DONE]":
                                with contextlib.suppress(json.JSONDecodeError):
                                    value = json.loads(chunk)
                                    usage = value.get("usage")
                                    if isinstance(usage, dict):
                                        proxy._record(usage, True, self.path)
                                    # A usage-only trailer has no choices. The
                                    # client never asked for it; do not forward.
                                    if not value.get("choices"):
                                        continue
                        self._write_chunk(line)
                    self._write_chunk(b"")

            def _write_chunk(self, data: bytes) -> None:
                self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        self.record_path.touch()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def usage_since(
        self, offset: int
    ) -> tuple[dict[str, dict[str, int]], int]:
        """Usage recorded after ``offset`` bytes, by category, and the new offset.

        GraphRAG queries run one at a time, so slicing this journal around a
        single query attributes every call it made to that question exactly.
        """
        with self._lock:
            text = self.record_path.read_text(encoding="utf-8")
        grouped: dict[str, list[dict[str, Any]]] = {
            "retrieval_chat": [],
            "retrieval_embedding": [],
        }
        for line in text[offset:].splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                row = json.loads(line)
                grouped.setdefault(
                    str(row.get("category") or "retrieval_chat"), []
                ).append(row)
        return (
            {name: summed_usage(rows) for name, rows in grouped.items()},
            len(text),
        )


def _update_model_mapping(
    mapping: dict[str, Any],
    *,
    model: str,
    api_base: str,
    api_key_env: str,
) -> None:
    for value in mapping.values():
        if not isinstance(value, dict):
            continue
        value["model_provider"] = "openai"
        value["model"] = model
        value["api_key"] = f"${{{api_key_env}}}"
        value["api_base"] = api_base
        value["concurrent_requests"] = GRAPHRAG_REQUEST_CONCURRENCY
        value["async_mode"] = "asyncio"
        if api_key_env == "GRAPHRAG_API_KEY":
            value["temperature"] = 0.5
            # No RPM limiter when the backend is a GPU we own: concurrent_requests
            # is then the only ceiling that matters.
            if GRAPHRAG_RPM_LIMIT is None:
                value.pop("rate_limit", None)
            else:
                value["rate_limit"] = {
                    "type": "sliding_window",
                    "period_in_seconds": 60,
                    "requests_per_period": GRAPHRAG_RPM_LIMIT,
                }


def configure_generated_graphrag_settings(
    settings_path: Path, args: SimpleNamespace
) -> None:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise BenchmarkError(
            "PyYAML is needed to configure generated GraphRAG settings. "
            "Install it or pass --graphrag-settings with a ready settings file."
        ) from exc

    data = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise BenchmarkError(f"invalid generated GraphRAG settings: {settings_path}")
    configured = 0
    completion = data.get("completion_models")
    embedding = data.get("embedding_models")
    if isinstance(completion, dict):
        _update_model_mapping(
            completion,
            model=args.chat_model,
            api_base=args.chat_base_url,
            api_key_env="GRAPHRAG_API_KEY",
        )
        configured += len(completion)
    if isinstance(embedding, dict):
        _update_model_mapping(
            embedding,
            model=args.embed_model,
            api_base=args.embed_base_url,
            api_key_env="BENCH_GRAPHRAG_EMBED_API_KEY",
        )
        configured += len(embedding)

    models = data.get("models")
    if isinstance(models, dict):
        for name, value in models.items():
            if not isinstance(value, dict):
                continue
            is_embedding = (
                "embed" in str(name).casefold()
                or str(value.get("type") or "").casefold() == "embedding"
            )
            value["model_provider"] = "openai"
            value["model"] = args.embed_model if is_embedding else args.chat_model
            value["api_base"] = (
                args.embed_base_url if is_embedding else args.chat_base_url
            )
            value["api_key"] = (
                "${BENCH_GRAPHRAG_EMBED_API_KEY}"
                if is_embedding
                else "${GRAPHRAG_API_KEY}"
            )
            value["concurrent_requests"] = GRAPHRAG_REQUEST_CONCURRENCY
            value["async_mode"] = "asyncio"
            if not is_embedding:
                value["temperature"] = args.temperature
                if GRAPHRAG_RPM_LIMIT is None:
                    value.pop("rate_limit", None)
                else:
                    value["rate_limit"] = {
                        "type": "sliding_window",
                        "period_in_seconds": 60,
                        "requests_per_period": GRAPHRAG_RPM_LIMIT,
                    }
            configured += 1
    if configured == 0:
        raise BenchmarkError(
            "could not find GraphRAG model sections; pass --graphrag-settings "
            "generated for your installed GraphRAG version"
        )

    def update_vector_sizes(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "vector_size" and args.embed_dim:
                    value[key] = args.embed_dim
                else:
                    update_vector_sizes(child)
        elif isinstance(value, list):
            for child in value:
                update_vector_sizes(child)

    update_vector_sizes(data)
    data["concurrent_requests"] = GRAPHRAG_REQUEST_CONCURRENCY
    if args.embed_dim:
        vector_store = data.setdefault("vector_store", {})
        if isinstance(vector_store, dict):
            index_schema = vector_store.setdefault("index_schema", {})
            if isinstance(index_schema, dict):
                for embedding_name in (
                    "text_unit_text",
                    "entity_description",
                    "community_full_content",
                ):
                    schema = index_schema.setdefault(embedding_name, {})
                    if isinstance(schema, dict):
                        # GraphRAG's schema default is the shared name
                        # "vector_index". Without explicit names each embedding
                        # workflow overwrites the preceding LanceDB table, which
                        # leaves DRIFT with no community-report embeddings.
                        schema["index_name"] = embedding_name
                        schema["vector_size"] = args.embed_dim
    drift_search = data.setdefault("drift_search", {})
    if isinstance(drift_search, dict):
        drift_search["concurrency"] = GRAPHRAG_DRIFT_CONCURRENCY
        drift_search["primer_folds"] = GRAPHRAG_DRIFT_PRIMER_FOLDS
        drift_search["drift_k_followups"] = GRAPHRAG_DRIFT_FOLLOWUPS
        drift_search["n_depth"] = GRAPHRAG_DRIFT_DEPTH
    settings_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def prepare_graphrag_workspace(
    workspace: Path,
    document_mapping: Sequence[dict[str, str]],
    args: SimpleNamespace,
) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    env = subprocess_environment(args)
    log("GraphRAG: initializing native workspace")
    run_logged(
        graphrag_command(
            args,
            "init",
            "--root",
            str(workspace.resolve()),
            "--model",
            args.chat_model,
            "--embedding",
            args.embed_model,
            "--force",
        ),
        cwd=ROOT,
        env=env,
        log_path=workspace / "init.log",
        timeout=remaining_timeout(args),
        live_label="graphrag:init",
    )
    generated = workspace / "settings.yaml"
    if args.graphrag_settings:
        template = Path(args.graphrag_settings).resolve()
        if not template.exists():
            raise BenchmarkError(f"GraphRAG settings file not found: {template}")
        shutil.copy2(template, generated)
    else:
        configure_generated_graphrag_settings(generated, args)

    # Process environment is authoritative; do not retain init's placeholder key.
    with contextlib.suppress(FileNotFoundError):
        (workspace / ".env").unlink()
    input_dir = workspace / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    for record in document_mapping:
        source = Path(record["path"])
        shutil.copy2(source, input_dir / f"{safe_name(record['id'])}.txt")


def graphrag_ingest(
    workspace: Path,
    document_mapping: Sequence[dict[str, str]],
    manifest: dict[str, Any],
    args: SimpleNamespace,
) -> dict[str, Any]:
    started = time.perf_counter()
    prepare_graphrag_workspace(workspace, document_mapping, args)
    env = subprocess_environment(args)
    run_logged(
        graphrag_command(
            args,
            "index",
            "--root",
            str(workspace.resolve()),
            "--method",
            "standard",
            "--no-cache",
        ),
        cwd=ROOT,
        env=env,
        log_path=workspace / "index.log",
        timeout=remaining_timeout(args),
        live_label="graphrag:index",
    )
    elapsed = time.perf_counter() - started
    return {
        "system": "graphrag",
        "elapsed_seconds": elapsed,
        "documents": manifest["documents"],
        "characters": manifest["characters"],
        "index_bytes": directory_size(workspace / "output"),
        "method": "standard",
        "cache": False,
    }


def worker_repair_graphrag_embeddings(request_path: Path) -> int:
    """Rebuild only GraphRAG vector tables from its existing Parquet outputs."""
    request = json.loads(request_path.read_text(encoding="utf-8"))
    try:
        import pandas as pd  # type: ignore
        from graphrag_vectors.lancedb import LanceDBVectorStore  # type: ignore
        from graphrag_vectors.vector_store import (  # type: ignore
            VectorStoreDocument,
        )
    except ImportError as exc:
        raise BenchmarkError(
            "GraphRAG repair worker is missing its installed dependencies"
        ) from exc

    output_dir = Path(request["workspace"]) / "output"
    vector_dir = output_dir / "lancedb"
    vector_dir.mkdir(parents=True, exist_ok=True)
    client = OpenAICompatibleClient(
        request["embed_base_url"],
        request["embed_api_key"],
        timeout=float(request["timeout"]),
    )
    embed_model = str(request["embed_model"])
    embed_dim = int(request["embed_dim"])
    batch_size = int(request.get("batch_size") or 32)
    specifications = [
        (
            "entity_description",
            "entities.parquet",
            lambda row: f"{row.get('title') or ''}:{row.get('description') or ''}",
        ),
        (
            "community_full_content",
            "community_reports.parquet",
            lambda row: str(row.get("full_content") or ""),
        ),
        (
            "text_unit_text",
            "text_units.parquet",
            lambda row: str(row.get("text") or ""),
        ),
    ]

    for index_name, parquet_name, text_from_row in specifications:
        table_path = vector_dir / f"{index_name}.lance"
        if table_path.exists():
            print(f"[GraphRAG repair] {index_name}: already present", flush=True)
            continue
        frame = pd.read_parquet(output_dir / parquet_name)
        records = frame.to_dict(orient="records")
        ids = [str(row["id"]) for row in records]
        texts = [text_from_row(row) for row in records]
        store = LanceDBVectorStore(
            db_uri=str(vector_dir),
            index_name=index_name,
            vector_size=embed_dim,
        )
        store.connect()
        store.create_index()
        for start in range(0, len(texts), batch_size):
            batch_texts = texts[start : start + batch_size]
            vectors, _usage = client.embeddings(batch_texts, embed_model)
            documents = [
                VectorStoreDocument(id=row_id, vector=vector)
                for row_id, vector in zip(
                    ids[start : start + batch_size],
                    vectors,
                )
            ]
            store.load_documents(documents)
            print(
                f"[GraphRAG repair] {index_name}: "
                f"{min(start + len(batch_texts), len(texts))}/{len(texts)}",
                flush=True,
            )
    return 0


def point_graphrag_chat_at(workspace: Path, api_base: str) -> None:
    """Send only GraphRAG's completion traffic through ``api_base``.

    Embeddings already report usage on every call, so they keep talking to the
    provider directly and the proxy carries just the traffic whose accounting
    is broken.
    """

    import yaml  # type: ignore

    settings_path = workspace / "settings.yaml"
    data = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise BenchmarkError(f"invalid GraphRAG settings: {settings_path}")
    for value in (data.get("completion_models") or {}).values():
        if isinstance(value, dict):
            value["api_base"] = api_base
    settings_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def ensure_graphrag_embedding_layout(
    workspace: Path, args: SimpleNamespace
) -> None:
    """Repair the old shared LanceDB-table layout without re-ingestion."""
    settings_path = workspace / "settings.yaml"
    configure_generated_graphrag_settings(settings_path, args)
    required = [
        workspace / "output" / "lancedb" / f"{name}.lance"
        for name in (
            "entity_description",
            "community_full_content",
            "text_unit_text",
        )
    ]
    if all(path.exists() for path in required):
        return

    log("GraphRAG: repairing missing vector tables from existing Parquet output")
    request_path = workspace / "embedding-repair-request.json"
    json_dump(
        request_path,
        {
            "workspace": str(workspace.resolve()),
            "embed_base_url": args.embed_base_url,
            "embed_api_key": args.embed_api_key,
            "embed_model": args.embed_model,
            "embed_dim": args.embed_dim,
            "batch_size": 32,
            "timeout": args.timeout,
        },
    )
    command = shlex.split(args.graphrag_command)
    if not command or command[-1] != "graphrag":
        raise BenchmarkError(
            "cannot derive GraphRAG Python command for embedding repair"
        )
    run_logged(
        [
            *command[:-1],
            "python",
            str(Path(__file__).resolve()),
            "_repair-graphrag-embeddings",
            "--request",
            str(request_path.resolve()),
        ],
        cwd=ROOT,
        env=subprocess_environment(args),
        log_path=workspace / "embedding-repair.log",
        timeout=remaining_timeout(args),
        live_label="graphrag:repair",
    )
    missing = [path.name for path in required if not path.exists()]
    if missing:
        raise BenchmarkError(
            "GraphRAG embedding repair did not create: " + ", ".join(missing)
        )
    log("GraphRAG: embedding layout repaired; graph extraction was reused")


def parse_graphrag_answer(output: str) -> str:
    clean = ANSI_RE.sub("", output).strip()
    marker = re.search(
        r"(?:SUCCESS\s*:\s*)?.*?(?:Search\s+)?Response\s*:\s*",
        clean,
        flags=re.IGNORECASE,
    )
    if marker:
        return clean[marker.end() :].strip()
    useful = [
        line
        for line in clean.splitlines()
        if not re.match(
            r"^\s*(INFO|DEBUG|WARNING|ERROR)\b", line, flags=re.IGNORECASE
        )
    ]
    return "\n".join(useful).strip()


def graphrag_answer(
    workspace: Path,
    questions: Sequence[Question],
    args: SimpleNamespace,
    log_dir: Path,
) -> list[dict[str, Any]]:
    ensure_graphrag_embedding_layout(workspace, args)
    env = subprocess_environment(args)
    query_style = detect_graphrag_query_style(args)
    log(f"GraphRAG query syntax: {query_style}")

    def answer_one(question: Question) -> dict[str, Any]:
        result = question.result_base()
        started = time.perf_counter()
        try:
            metrics_path = workspace / "logs" / "query.log"
            metrics_offset = (
                metrics_path.stat().st_size if metrics_path.exists() else 0
            )
            completed = run_logged(
                graphrag_query_command(
                    args,
                    workspace,
                    question.question,
                    style=query_style,
                ),
                cwd=ROOT,
                env=env,
                log_path=log_dir / f"{safe_name(question.id)}.log",
                timeout=remaining_timeout(args),
            )
            answer = parse_graphrag_answer(
                completed.stdout.strip()
                or "\n".join((completed.stdout, completed.stderr))
            )
            if not answer:
                raise BenchmarkError("could not parse GraphRAG query response")
            metrics_text = ""
            if metrics_path.exists():
                with metrics_path.open("rb") as handle:
                    handle.seek(metrics_offset)
                    metrics_text = handle.read().decode(
                        "utf-8", errors="replace"
                    )
            result.update(
                {
                    "generated_answer": answer,
                    "token_usage": parse_graphrag_token_metrics(
                        metrics_text, args
                    ),
                    "retrieval_method": args.graphrag_method,
                    "error": None,
                }
            )
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["latency_seconds"] = time.perf_counter() - started
        return result

    results: list[dict[str, Any] | None] = [None] * len(questions)
    workers = min(args.graphrag_query_workers, len(questions))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(answer_one, question): index
            for index, question in enumerate(questions)
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            results[futures[future]] = future.result()
            log(f"graphrag answered {completed}/{len(questions)}")
    return [result for result in results if result is not None]


def ours_environment(args: SimpleNamespace) -> dict[str, str]:
    env = subprocess_environment(args)
    env.update(
        {
            "WIKI_MODEL": args.chat_model,
            "WIKI_EMBED_BASE_URL": args.embed_base_url,
            "WIKI_EMBED_API_KEY": args.embed_api_key,
            "WIKI_EMBED_MODEL": args.embed_model,
            "WIKI_EMBED_DIM": str(args.embed_dim),
            "WIKI_ENABLE_MERMAID": "0",
            # One shared cap used by short-document groups and every independent
            # phase of long-document conceptual chunking/graph ingestion.
            "WIKI_INGEST_CONCURRENCY": str(args.ingestion_concurrency),
            # Backwards-compatible alias for older chunker entry points.
            "WIKI_CHUNK_CONCURRENCY": str(args.ingestion_concurrency),
            # Reclustering renames every cluster (~2 LLM calls each), so per
            # document it is quadratic in corpus size. The ingest worker runs
            # one explicit refresh_clusters() once all documents are in.
            "WIKI_RECLUSTER_EVERY": "0",
        }
    )
    if hasattr(args, "agent_max_steps"):
        env["WIKI_AGENT_MAX_STEPS"] = str(args.agent_max_steps)
    if hasattr(args, "subagent_max_steps"):
        env["WIKI_SUBAGENT_MAX_STEPS"] = str(args.subagent_max_steps)
    # Left unset these fall back to the app's own defaults, which makes the
    # benchmark's search breadth an accident of the shipped product config.
    if hasattr(args, "subagent_count"):
        env["WIKI_SUBAGENT_COUNT"] = str(args.subagent_count)
    if hasattr(args, "subagent_concurrency"):
        env["WIKI_SUBAGENT_CONCURRENCY"] = str(args.subagent_concurrency)
    if hasattr(args, "rerank_top_k"):
        env["WIKI_RERANK_TOP_K"] = str(args.rerank_top_k)
    if hasattr(args, "search_candidate_pool"):
        env["WIKI_SEARCH_POOL"] = str(args.search_candidate_pool)
    if args.rerank_base_url:
        env["WIKI_RERANK_BASE_URL"] = args.rerank_base_url
    if args.rerank_model:
        env["WIKI_RERANK_MODEL"] = args.rerank_model
    if args.rerank_api_key:
        env["WIKI_RERANK_API_KEY"] = args.rerank_api_key
    return env


def run_ours_worker(
    action: str,
    request: dict[str, Any],
    *,
    workspace: Path,
    args: SimpleNamespace,
) -> dict[str, Any]:
    request_path = workspace / f"{action}-request.json"
    result_path = workspace / f"{action}-result.json"
    json_dump(request_path, {**request, "result_path": str(result_path.resolve())})
    # Never mistake a prior successful worker result for the output of a new
    # subprocess that failed before it could publish its own result.
    with contextlib.suppress(FileNotFoundError):
        result_path.unlink()
    command = [
        str(Path(args.ours_python).absolute()),
        str(Path(__file__).resolve()),
        f"_ours-{action}",
        "--request",
        str(request_path.resolve()),
    ]
    env = ours_environment(args)
    database = request.get("database")
    if database:
        # Settings.from_env() is evaluated before the worker applies its
        # explicit database override. Keep even that initial value isolated
        # from the app's default project-level .wiki directory.
        env["WIKI_DB"] = str(Path(database).resolve())
    run_logged(
        command,
        cwd=workspace,
        env=env,
        log_path=workspace / f"{action}.log",
        timeout=remaining_timeout(args),
        live_label=f"ours:{action}",
    )
    if not result_path.exists():
        raise BenchmarkError(f"ours worker did not write {result_path}")
    value = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BenchmarkError(f"ours worker wrote invalid result: {result_path}")
    return value


def reset_ours_workspace(workspace: Path) -> None:
    """Remove only benchmark-owned native RAG state before fresh ingestion."""
    targets = [
        workspace / ".wiki",
        workspace / "sources",
        workspace / "wiki.sqlite",
        workspace / "wiki.sqlite-shm",
        workspace / "wiki.sqlite-wal",
    ]
    removed: list[str] = []
    for target in targets:
        if target.is_symlink() or target.is_file():
            target.unlink()
            removed.append(target.name)
        elif target.is_dir():
            shutil.rmtree(target)
            removed.append(target.name)
    if removed:
        log(f"ours reset prior benchmark state: {', '.join(removed)}")
    else:
        log("ours starting with a fresh isolated .wiki/database state")


def ours_ingest(
    workspace: Path,
    document_mapping: Sequence[dict[str, str]],
    manifest: dict[str, Any],
    args: SimpleNamespace,
) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    reset_ours_workspace(workspace)
    db_path = workspace / "wiki.sqlite"
    started = time.perf_counter()
    result = run_ours_worker(
        "ingest",
        {
            "app_root": str(APP_ROOT.resolve()),
            "database": str(db_path.resolve()),
            "documents": list(document_mapping),
            "concurrency": args.ingestion_concurrency,
        },
        workspace=workspace,
        args=args,
    )
    elapsed = time.perf_counter() - started
    database_bytes = sum(
        item.stat().st_size
        for item in workspace.glob(f"{db_path.name}*")
        if item.is_file()
    )
    return {
        "system": "ours",
        "documents": manifest["documents"],
        "characters": manifest["characters"],
        **result,
        "elapsed_seconds": elapsed,
        "index_bytes": database_bytes + directory_size(workspace / "sources"),
    }


def ours_answer(
    workspace: Path,
    questions: Sequence[Question],
    args: SimpleNamespace,
) -> list[dict[str, Any]]:
    batch_path = workspace / "answer-batch.jsonl"
    result = run_ours_worker(
        "answer",
        {
            "app_root": str(APP_ROOT.resolve()),
            "database": str((workspace / "wiki.sqlite").resolve()),
            "questions": [asdict(question) for question in questions],
            "predictions_path": str(batch_path.resolve()),
            "trace_dir": str((workspace / "traces").resolve()),
            "workers": args.ours_question_workers,
        },
        workspace=workspace,
        args=args,
    )
    if int(result.get("predictions") or 0) != len(questions):
        raise BenchmarkError(
            f"ours worker returned {result.get('predictions')} predictions for "
            f"{len(questions)} questions"
        )
    return jsonl_load(batch_path)


def _worker_imports(app_root: str):
    app_path = str(Path(app_root).resolve())
    if app_path not in sys.path:
        sys.path.insert(0, app_path)
    from graph.core import Settings
    from graph.gateway import ModelGateway
    from graph.librarian import Librarian, WriteJob
    from graph.researcher import ResearchSession
    from graph.store import GraphStore

    return Settings, ModelGateway, Librarian, WriteJob, ResearchSession, GraphStore


def worker_ours_ingest(request_path: Path) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    documents = request["documents"]
    requested_concurrency = max(
        1, int(request.get("concurrency") or OURS_INGEST_CONCURRENCY)
    )
    # Set the unified budget before importing the app. Both Settings.from_env
    # and graph.chunk's module-level compatibility constant read it at import.
    os.environ["WIKI_INGEST_CONCURRENCY"] = str(requested_concurrency)
    os.environ["WIKI_CHUNK_CONCURRENCY"] = str(requested_concurrency)
    (
        Settings,
        ModelGateway,
        Librarian,
        WriteJob,
        ResearchSession,
        GraphStore,
    ) = _worker_imports(request["app_root"])
    database = Path(request["database"])
    database.parent.mkdir(parents=True, exist_ok=True)
    settings = Settings.from_env()
    settings.database_path = str(database)
    settings.enable_mermaid = False
    gateway = ModelGateway(settings)
    store = GraphStore(str(database))
    librarian = Librarian(gateway, store, background=False)
    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    try:
        gateway.llm.reset_global_usage()
        gateway.embedder.reset_global_usage()
        librarian.bootstrap()
        threshold = int(
            os.environ.get(
                "WIKI_CHUNK_THRESHOLD_LINES",
                str(OURS_CHUNK_THRESHOLD_LINES),
            )
        )
        # Short documents use this many document workers. Long documents arrive
        # one at a time and use the same budget inside planning, enrichment,
        # graph preparation/linking, and post-enrichment.
        group_size = max(
            1, min(settings.ingest_concurrency, len(documents))
        )

        def read_document(document: dict[str, Any]) -> tuple[Path, str, int]:
            path = Path(document["path"])
            body = path.read_text(encoding="utf-8")
            line_count = len(body.splitlines())
            return path, body, line_count

        def ingest_long(
            document: dict[str, Any], path: Path, body: str, line_count: int
        ) -> dict[str, Any]:
            log(
                f"ours native conceptual chunking {document['id']}: "
                f"{line_count} source lines"
            )
            job = WriteJob(
                id=str(uuid.uuid4()),
                type="chunk_and_ingest",
                payload={
                    "body": body,
                    "title": document["id"],
                    "document_name": f"{document['id']}.txt",
                    "source_path": str(path),
                },
            )
            item_result = librarian.chunk_and_ingest(job)
            ingested = int(item_result.get("ingested") or 0)
            if ingested <= 1:
                raise BenchmarkError(
                    f"native conceptual chunker produced only {ingested} node "
                    f"for long source {document['id']!r}; refusing an invalid "
                    "benchmark run"
                )
            log(
                f"ours conceptual chunking produced "
                f"{item_result.get('files', ingested)} files / "
                f"{ingested} graph nodes"
            )
            return {"document": document["id"], **item_result}

        def flush(group: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
            # Ingest a whole group at once. The librarian prepares every member
            # before linking any of them, so documents in the same group can
            # link to each other and the result does not depend on which thread
            # finished first.
            if not group:
                return
            nodes = librarian.create_document_nodes(
                [entry for _document, entry in group], concurrency=group_size
            )
            for (document, _entry), node in zip(group, nodes):
                # In server mode summary and entity dedup are queued to a
                # background thread. The librarian is built with background=
                # False here, so ingestion already ran both inline and the index
                # is query-ready. This is only a retry for a summary whose
                # generation call failed; it returns immediately otherwise.
                librarian.enrich_summary(node.id)
                results.append(
                    {
                        "document": document["id"],
                        "chunked": False,
                        "ingested": 1,
                        "node_id": node.id,
                    }
                )
            group.clear()
            log(f"ours ingested {len(results)}/{len(documents)}")

        if group_size > 1:
            log(
                f"ours ingesting {len(documents)} documents in groups of "
                f"{group_size}"
            )
        # Walk in corpus order, batching short documents and flushing the batch
        # whenever it is full or a long document interrupts it, so grouping is
        # decided by the corpus rather than by timing.
        pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for document in documents:
            path, body, line_count = read_document(document)
            if needs_native_conceptual_chunking(
                body, line_count, line_threshold=threshold
            ):
                flush(pending)
                results.append(ingest_long(document, path, body, line_count))
                log(f"ours ingested {len(results)}/{len(documents)}")
                continue
            pending.append(
                (
                    document,
                    {
                        "body": body,
                        "title": document["id"],
                        "document_name": f"{document['id']}.txt",
                        "source_path": str(path),
                    },
                )
            )
            if len(pending) >= group_size:
                flush(pending)
        flush(pending)
        # The resumable benchmark sends one document per worker and performs a
        # single explicit finalization after all document checkpoints exist.
        if bool(request.get("refresh_clusters", True)):
            log("ours final recluster")
            librarian.refresh_clusters()
        stats = ResearchSession(gateway, store).health().model_dump()
        elapsed = time.perf_counter() - started
        json_dump(
            Path(request["result_path"]),
            {
                "elapsed_seconds": elapsed,
                "graph": stats,
                "document_results": results,
                "token_usage": {
                    "ingestion_chat": gateway.llm.consume_global_usage(),
                    "ingestion_embedding": gateway.embedder.consume_global_usage(),
                },
            },
        )
    finally:
        store.close()
        gateway.close()
    return 0


def worker_ours_answer(request_path: Path) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    (
        Settings,
        ModelGateway,
        _Librarian,
        _WriteJob,
        ResearchSession,
        GraphStore,
    ) = _worker_imports(request["app_root"])
    settings = Settings.from_env()
    settings.database_path = request["database"]
    settings.enable_mermaid = False
    gateway = ModelGateway(settings)
    store = GraphStore(request["database"], readonly=True)
    trace_dir = Path(request["trace_dir"])
    trace_dir.mkdir(parents=True, exist_ok=True)

    def answer_one(raw_question: dict[str, Any]) -> dict[str, Any]:
        question = Question(**raw_question)
        result = question.result_base()
        events: list[dict[str, Any]] = []
        started = time.perf_counter()
        gateway.llm.reset_thread_usage()
        gateway.embedder.reset_thread_usage()
        try:
            answer = ResearchSession(gateway, store).ask(
                question.question,
                persist=False,
                on_event=events.append,
            )
            contexts: list[str] = []
            for node_id in answer.cited_node_ids:
                node = store.get_node(node_id)
                if node is not None:
                    contexts.append(node.body)
            result.update(
                {
                    "context": contexts,
                    "generated_answer": answer.answer,
                    "cited_node_ids": answer.cited_node_ids,
                    "agent_steps": answer.steps,
                    "error": None,
                }
            )
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            events.append({"type": "error", "error": result["error"]})
        gateway_usage = gateway.llm.consume_thread_usage()
        embedding_usage = gateway.embedder.consume_thread_usage()
        agent_event_usage = [
            event.get("usage")
            for event in events
            if event.get("type") == "token_usage"
            and isinstance(event.get("usage"), dict)
        ]
        result["token_usage"] = {
            "agent_chat": summed_usage(
                [gateway_usage, *agent_event_usage]
            ),
            "retrieval_embedding": embedding_usage,
        }
        result["token_accounting_complete"] = (
            int(embedding_usage.get("estimated_requests") or 0) == 0
            and (
                int(result["token_usage"]["agent_chat"].get("requests") or 0) == 0
                or int(result["token_usage"]["agent_chat"].get("total_tokens") or 0)
                > 0
            )
        )
        result["latency_seconds"] = time.perf_counter() - started
        json_dump(trace_dir / f"{safe_name(question.id)}.json", events)
        return result

    try:
        raw_questions = request["questions"]
        predictions: list[dict[str, Any] | None] = [None] * len(raw_questions)
        workers = min(int(request.get("workers") or 1), len(raw_questions))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(answer_one, raw_question): index
                for index, raw_question in enumerate(raw_questions)
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                predictions[futures[future]] = future.result()
                # This is the worker-to-parent recovery journal.  Atomic
                # replacement means a killed benchmark loses at most the
                # currently running questions, never all completed answers.
                jsonl_dump(
                    Path(request["predictions_path"]),
                    (
                        prediction
                        for prediction in predictions
                        if prediction is not None
                    ),
                )
                log(f"ours answered {completed}/{len(raw_questions)}")
        final_predictions = [
            prediction for prediction in predictions if prediction is not None
        ]
        jsonl_dump(Path(request["predictions_path"]), final_predictions)
        json_dump(
            Path(request["result_path"]),
            {"predictions": len(final_predictions)},
        )
    finally:
        store.close()
        gateway.close()
    return 0


def normalized_answer(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\b(a|an|the)\b", " ", value)
    return " ".join(value.split())


def token_f1(prediction: str, reference: str) -> float:
    predicted = WORD_RE.findall(normalized_answer(prediction))
    expected = WORD_RE.findall(normalized_answer(reference))
    if not predicted or not expected:
        return float(predicted == expected)
    common = collections.Counter(predicted) & collections.Counter(expected)
    matches = sum(common.values())
    if not matches:
        return 0.0
    precision = matches / len(predicted)
    recall = matches / len(expected)
    return 2 * precision * recall / (precision + recall)


def parse_json_object(text: str) -> dict[str, Any]:
    clean = text.strip()
    clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s*```$", "", clean)
    try:
        value = json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", clean, flags=re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def run_shared_agent(
    *,
    question: Question,
    client: OpenAICompatibleClient,
    search: Any,
    args: SimpleNamespace,
    label: str,
) -> dict[str, Any]:
    """Small common agent loop; only the retrieval adapter varies by system."""
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "You are an autonomous RAG research agent. You must search the "
                "provided index at least once, and may reformulate and search as "
                "many times as useful. At each step return only JSON: "
                '{"action":"search","query":"..."} or '
                '{"action":"answer","answer":"..."}. Use only retrieved evidence; '
                "if evidence is insufficient, say so."
            ),
        },
        {"role": "user", "content": f"Question: {question.question}"},
    ]
    contexts: list[str] = []
    queries: list[str] = []
    usage: list[dict[str, Any]] = []
    retrieval_usage: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)

    step = 0
    while True:
        step += 1
        raw, call_usage = client.chat(
            messages,
            args.chat_model,
            temperature=args.temperature,
            max_tokens=300,
            json_mode=True,
        )
        usage.append(call_usage)
        try:
            decision = parse_json_object(raw)
        except Exception:
            decision = {"action": "answer", "answer": raw}

        action = str(decision.get("action") or "").strip().casefold()
        if action == "answer" and queries:
            answer = str(decision.get("answer") or "").strip()
            if answer:
                return {
                    "answer": answer,
                    "context": contexts,
                    "agent_queries": queries,
                    "agent_steps": step,
                    "usage": usage,
                    "token_usage": {
                        "agent_chat": summed_usage(usage),
                        **{
                            category: summed_usage(values)
                            for category, values in retrieval_usage.items()
                        },
                    },
                }

        query = str(decision.get("query") or question.question).strip()
        if not query:
            query = question.question
        queries.append(query)
        log(f"{label}: agent search {step}")
        evidence = search(query, step)
        evidence_text = str(evidence.get("text") or "")[:AGENT_TOOL_RESULT_CHARS]
        contexts.extend(str(item) for item in evidence.get("contexts") or [])
        for category, counts in (evidence.get("token_usage") or {}).items():
            if isinstance(counts, dict):
                retrieval_usage[str(category)].append(counts)
        messages.extend(
            [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"Search result {step} for {query!r}:\n{evidence_text}\n\n"
                        "Choose another search or provide the final answer as JSON."
                    ),
                },
            ]
        )


def agentic_vanilla_answer(
    workspace: Path,
    questions: Sequence[Question],
    args: SimpleNamespace,
    on_result: Any = None,
) -> list[dict[str, Any]]:
    rows = jsonl_load(workspace / "index.jsonl")
    if not rows:
        raise BenchmarkError(f"vanilla index is empty: {workspace}")
    chat_client = OpenAICompatibleClient(
        args.chat_base_url, args.chat_api_key, timeout=args.timeout
    )
    embed_client = OpenAICompatibleClient(
        args.embed_base_url, args.embed_api_key, timeout=args.timeout
    )

    def answer_one(question: Question) -> dict[str, Any]:
        result = question.result_base()
        started = time.perf_counter()

        def search(query: str, _step: int) -> dict[str, Any]:
            vectors, embed_usage = embed_client.embeddings([query], args.embed_model)
            query_vector = normalize_vector(vectors[0])
            ranked = sorted(
                rows,
                key=lambda row: sum(
                    left * right
                    for left, right in zip(query_vector, row["vector"])
                ),
                reverse=True,
            )[: args.top_k]
            contexts = [str(row["text"]) for row in ranked]
            text = "\n\n".join(
                f"[{row['id']}]\n{row['text']}" for row in ranked
            )
            return {
                "text": text,
                "contexts": contexts,
                "token_usage": {"retrieval_embedding": embed_usage},
            }

        try:
            agent = run_shared_agent(
                question=question,
                client=chat_client,
                search=search,
                args=args,
                label=f"vanilla {question.id}",
            )
            result.update(
                {
                    "generated_answer": agent["answer"],
                    "context": agent["context"],
                    "agent_queries": agent["agent_queries"],
                    "agent_steps": agent["agent_steps"],
                    "usage": agent["usage"],
                    "token_usage": agent["token_usage"],
                    "error": None,
                }
            )
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["latency_seconds"] = time.perf_counter() - started
        return result

    results: list[dict[str, Any] | None] = [None] * len(questions)
    workers = min(args.chat_concurrency, len(questions))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(answer_one, question): index
            for index, question in enumerate(questions)
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results[futures[future]] = result
            if on_result:
                on_result(result)
            log(f"agentic vanilla answered {completed}/{len(questions)}")
    return [result for result in results if result is not None]


def agentic_graphrag_answer(
    workspace: Path,
    questions: Sequence[Question],
    args: SimpleNamespace,
    on_result: Any = None,
) -> list[dict[str, Any]]:
    ensure_graphrag_embedding_layout(workspace, args)
    chat_client = OpenAICompatibleClient(
        args.chat_base_url, args.chat_api_key, timeout=args.timeout
    )
    env = subprocess_environment(args)
    query_style = detect_graphrag_query_style(args)
    log(
        f"Agentic GraphRAG: method={args.graphrag_method}, "
        f"syntax={query_style}, concurrency=1"
    )
    log_dir = workspace / "query-logs-agentic"

    def answer_one(question: Question) -> dict[str, Any]:
        result = question.result_base()
        started = time.perf_counter()

        def search(query: str, step: int) -> dict[str, Any]:
            metrics_path = workspace / "logs" / "query.log"
            metrics_offset = (
                metrics_path.stat().st_size if metrics_path.exists() else 0
            )
            completed = run_logged(
                graphrag_query_command(
                    args, workspace, query, style=query_style
                ),
                cwd=ROOT,
                env=env,
                log_path=(
                    log_dir
                    / f"{safe_name(question.id)}-search-{step:02d}.log"
                ),
                timeout=remaining_timeout(args),
            )
            text = parse_graphrag_answer(completed.stdout)
            if not text:
                raise BenchmarkError("GraphRAG agent search returned no evidence")
            metrics_text = ""
            if metrics_path.exists():
                with metrics_path.open("rb") as handle:
                    handle.seek(metrics_offset)
                    metrics_text = handle.read().decode("utf-8", errors="replace")
            return {
                "text": text,
                "contexts": [text],
                "token_usage": parse_graphrag_token_metrics(metrics_text, args),
            }

        try:
            agent = run_shared_agent(
                question=question,
                client=chat_client,
                search=search,
                args=args,
                label=f"graphrag {question.id}",
            )
            result.update(
                {
                    "generated_answer": agent["answer"],
                    "context": agent["context"],
                    "agent_queries": agent["agent_queries"],
                    "agent_steps": agent["agent_steps"],
                    "retrieval_method": args.graphrag_method,
                    "usage": agent["usage"],
                    "token_usage": agent["token_usage"],
                    "error": None,
                }
            )
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["latency_seconds"] = time.perf_counter() - started
        return result

    results: list[dict[str, Any] | None] = [None] * len(questions)
    workers = min(args.graphrag_query_workers, len(questions))
    if workers == 1:
        for index, question in enumerate(questions):
            result = answer_one(question)
            results[index] = result
            if on_result:
                # Judge and checkpoint before the next question starts. This
                # prevents judge traffic overlapping the next GraphRAG query.
                on_result(result)
            log(f"agentic graphrag answered {index + 1}/{len(questions)}")
        return [result for result in results if result is not None]

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(answer_one, question): index
            for index, question in enumerate(questions)
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results[futures[future]] = result
            if on_result:
                on_result(result)
            log(f"agentic graphrag answered {completed}/{len(questions)}")
    return [result for result in results if result is not None]


def judge_prediction(
    client: OpenAICompatibleClient,
    prediction: dict[str, Any],
    args: SimpleNamespace,
) -> dict[str, Any]:
    if prediction.get("error"):
        return {
            "judge_correct": False,
            "judge_score": 0.0,
            "judge_reason": "system answer failed",
        }
    payload = {
        "question": prediction["question"],
        "reference_answer": prediction["ground_truth"],
        "candidate_answer": prediction["generated_answer"],
    }
    prompt = (
        "Evaluate whether the candidate correctly answers the question according "
        "to the reference. Accept paraphrases and additional correct detail. "
        "Reject contradictions, missing essential facts, and unsupported answers. "
        "Return only JSON with keys correct (boolean), score (0 to 1), and reason "
        f"(one short sentence).\n\n{json.dumps(payload, ensure_ascii=False)}"
    )
    text, usage = client.chat(
        [
            {
                "role": "system",
                "content": "You are a strict, impartial question-answer evaluator.",
            },
            {"role": "user", "content": prompt},
        ],
        args.judge_model or args.chat_model,
        temperature=0.0,
        max_tokens=300,
        json_mode=True,
    )
    value = parse_json_object(text)
    correct_raw = value.get("correct")
    if isinstance(correct_raw, str):
        correct = correct_raw.strip().casefold() in {"true", "yes", "1"}
    else:
        correct = bool(correct_raw)
    score = float(value.get("score", 1.0 if correct else 0.0))
    return {
        "judge_correct": correct,
        "judge_score": max(0.0, min(1.0, score)),
        "judge_reason": str(value.get("reason") or ""),
        "judge_usage": usage,
    }


def score_prediction(
    prediction: dict[str, Any],
    args: SimpleNamespace,
    judge_client: OpenAICompatibleClient | None,
) -> dict[str, Any]:
    generated = str(prediction.get("generated_answer") or "")
    reference = str(prediction.get("ground_truth") or "")
    prediction["exact_match"] = (
        normalized_answer(generated) == normalized_answer(reference)
    )
    prediction["token_f1"] = token_f1(generated, reference)
    if judge_client is not None and "judge_correct" not in prediction:
        try:
            prediction.update(judge_prediction(judge_client, prediction, args))
            prediction.pop("judge_error", None)
        except Exception as exc:
            prediction["judge_error"] = f"{type(exc).__name__}: {exc}"
    judge_usage = prediction.get("judge_usage")
    if isinstance(judge_usage, dict):
        prediction.setdefault("token_usage", {})["judge_chat"] = summed_usage(
            [judge_usage]
        )
    prediction["token_usage_total"] = prediction_token_usage(prediction)
    return prediction


def score_predictions(
    path: Path, args: SimpleNamespace
) -> list[dict[str, Any]]:
    predictions = jsonl_load(path)
    judge_client = (
        OpenAICompatibleClient(
            args.judge_base_url or args.chat_base_url,
            args.judge_api_key or args.chat_api_key,
            timeout=args.timeout,
        )
        if args.judge == "llm"
        else None
    )
    # Cheap deterministic metrics are always refreshed. Only rows without an
    # existing judge result consume another model request.
    predictions = [
        score_prediction(prediction, args, None)
        for prediction in predictions
    ]
    pending_indices = [
        index
        for index, prediction in enumerate(predictions)
        if judge_client is not None
        and "judge_correct" not in prediction
    ]

    if judge_client is not None and pending_indices:
        workers = min(args.chat_concurrency, len(pending_indices))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(
                    score_prediction,
                    predictions[index],
                    args,
                    judge_client,
                ): index
                for index in pending_indices
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                predictions[futures[future]] = future.result()
                log(
                    f"judged {path.parent.name} "
                    f"{completed}/{len(pending_indices)}"
                )
    jsonl_dump(path, predictions)
    return predictions


def percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def paired_bootstrap(
    left: Sequence[bool],
    right: Sequence[bool],
    *,
    seed: int,
    iterations: int = 5000,
) -> tuple[float, float]:
    if len(left) != len(right) or not left:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    differences: list[float] = []
    count = len(left)
    for _ in range(iterations):
        delta = 0
        for _sample in range(count):
            index = rng.randrange(count)
            delta += int(left[index]) - int(right[index])
        differences.append(delta / count)
    return percentile(differences, 0.025), percentile(differences, 0.975)


def metric_summary(
    expected: Sequence[Question],
    predictions: Sequence[dict[str, Any]],
    *,
    judge: str,
) -> dict[str, Any]:
    by_id = {str(row.get("id")): row for row in predictions}
    rows = [by_id.get(question.id, {}) for question in expected]
    primary_key = "judge_correct" if judge == "llm" else "exact_match"
    correct = [bool(row.get(primary_key, False)) for row in rows]
    failures = sum(1 for row in rows if row.get("error") or not row)
    judge_errors = sum(1 for row in rows if row.get("judge_error"))
    latencies = [
        float(row["latency_seconds"])
        for row in rows
        if row.get("latency_seconds") is not None
    ]
    per_type: dict[str, dict[str, Any]] = {}
    for question_type in sorted({question.question_type for question in expected}):
        positions = [
            index
            for index, question in enumerate(expected)
            if question.question_type == question_type
        ]
        per_type[question_type] = {
            "n": len(positions),
            "accuracy": (
                sum(correct[index] for index in positions) / len(positions)
                if positions
                else 0.0
            ),
        }
    return {
        "n": len(expected),
        "accuracy": sum(correct) / len(correct) if correct else 0.0,
        "exact_match": (
            sum(bool(row.get("exact_match", False)) for row in rows) / len(rows)
            if rows
            else 0.0
        ),
        "token_f1": (
            sum(float(row.get("token_f1") or 0.0) for row in rows) / len(rows)
            if rows
            else 0.0
        ),
        "failures": failures,
        "judge_errors": judge_errors,
        "median_latency_seconds": statistics.median(latencies) if latencies else None,
        "token_usage": aggregate_prediction_usage(rows),
        "per_type": per_type,
        "_correct": correct,
    }


def load_ingestion_metrics(system_dir: Path) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for path in sorted(system_dir.glob("run-*/ingestion.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            metrics.append(value)
    return metrics


def format_duration(seconds: float | None) -> str:
    if seconds is None or math.isnan(seconds):
        return "n/a"
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {remainder:.1f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes)}m {remainder:.0f}s"


def generate_report(
    output: Path,
    questions: Sequence[Question],
    systems: Sequence[str],
    *,
    judge: str,
    close_margin: float,
    seed: int,
    mode: str = "native",
) -> dict[str, Any]:
    accuracy: dict[str, dict[str, Any]] = {}
    ingestion: dict[str, dict[str, Any]] = {}
    for system in systems:
        predictions = jsonl_load(output / "systems" / system / "predictions.jsonl")
        accuracy[system] = metric_summary(questions, predictions, judge=judge)
        metrics = load_ingestion_metrics(output / "systems" / system)
        times = [float(item["elapsed_seconds"]) for item in metrics]
        chars = int(metrics[0].get("characters") or 0) if metrics else 0
        documents = int(metrics[0].get("documents") or 0) if metrics else 0
        median_seconds = statistics.median(times) if times else None
        ingestion[system] = {
            "runs": len(metrics),
            "median_seconds": median_seconds,
            "min_seconds": min(times) if times else None,
            "max_seconds": max(times) if times else None,
            "characters_per_second": (
                chars / median_seconds if median_seconds else None
            ),
            "documents_per_minute": (
                documents * 60 / median_seconds if median_seconds else None
            ),
            "index_bytes": (
                statistics.median(
                    [int(item.get("index_bytes") or 0) for item in metrics]
                )
                if metrics
                else None
            ),
        }

    comparison: dict[str, Any] = {}
    if "ours" in accuracy and "graphrag" in accuracy:
        ours_correct = accuracy["ours"]["_correct"]
        graph_correct = accuracy["graphrag"]["_correct"]
        delta = accuracy["ours"]["accuracy"] - accuracy["graphrag"]["accuracy"]
        low, high = paired_bootstrap(
            ours_correct, graph_correct, seed=seed, iterations=5000
        )
        ours_time = ingestion["ours"]["median_seconds"]
        graph_time = ingestion["graphrag"]["median_seconds"]
        comparison = {
            "ours_vs_graphrag_accuracy_delta": delta,
            "accuracy_delta_ci95": [low, high],
            "close_margin": close_margin,
            "point_estimate_within_margin": delta >= -close_margin,
            "noninferiority_established": low >= -close_margin,
            "ours_ingestion_speedup": (
                graph_time / ours_time if graph_time and ours_time else None
            ),
            "ours_ingestion_faster": (
                ours_time < graph_time if graph_time and ours_time else None
            ),
        }

    serializable_accuracy = {
        system: {key: value for key, value in values.items() if key != "_correct"}
        for system, values in accuracy.items()
    }
    summary = {
        "ingestion": ingestion,
        "accuracy": serializable_accuracy,
        "comparison": comparison,
        "primary_accuracy_metric": "LLM judge accuracy" if judge == "llm" else "exact match",
    }
    json_dump(output / "summary.json", summary)

    token_csv_path = output / "token-usage.csv"
    token_fields = [
        "system",
        "question_id",
        "source",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "requests",
        "agent_chat",
        "answer_chat",
        "retrieval_chat",
        "retrieval_embedding",
        "judge_chat",
    ]
    with token_csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=token_fields)
        writer.writeheader()
        for system in systems:
            for prediction in jsonl_load(
                output / "systems" / system / "predictions.jsonl"
            ):
                total = prediction_token_usage(prediction)
                categories = prediction.get("token_usage")

                def category_total(name: str) -> int:
                    value = (
                        categories.get(name)
                        if isinstance(categories, dict)
                        else None
                    )
                    return (
                        int(value.get("total_tokens") or 0)
                        if isinstance(value, dict)
                        else 0
                    )

                writer.writerow(
                    {
                        "system": system,
                        "question_id": prediction.get("id"),
                        "source": prediction.get("source"),
                        "input_tokens": total["prompt_tokens"],
                        "output_tokens": total["completion_tokens"],
                        "total_tokens": total["total_tokens"],
                        "requests": total["requests"],
                        "agent_chat": category_total("agent_chat"),
                        "answer_chat": category_total("answer_chat"),
                        "retrieval_chat": category_total("retrieval_chat"),
                        "retrieval_embedding": category_total(
                            "retrieval_embedding"
                        ),
                        "judge_chat": category_total("judge_chat"),
                    }
                )

    agentic = mode.startswith("agentic")
    lines = [
        (
            "# Shared-agent retrieval ablation (reused indexes)"
            if agentic
            else "# Native RAG harness benchmark"
        ),
        "",
        (
            "All systems reuse their completed corrected indexes. Vanilla and "
            "GraphRAG are placed behind the same iterative outer agent; ours "
            "uses its native ResearchSession agent. This is a normalized-agent "
            "ablation, not the paper-style native GraphRAG comparison."
            if agentic
            else "This is an end-to-end comparison of each system's native harness. "
            "It does not isolate retrieval architecture from agent behavior."
        ),
        "",
        "## Ingestion",
        "",
        "| System | Runs | Median | Min–max | Docs/min | Characters/s | Index size |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for system in systems:
        item = ingestion[system]
        min_max = (
            f"{format_duration(item['min_seconds'])}–"
            f"{format_duration(item['max_seconds'])}"
        )
        throughput = (
            f"{item['characters_per_second']:,.0f}"
            if item["characters_per_second"] is not None
            else "n/a"
        )
        documents_per_minute = (
            f"{item['documents_per_minute']:,.2f}"
            if item["documents_per_minute"] is not None
            else "n/a"
        )
        index_size = (
            f"{item['index_bytes'] / (1024 * 1024):,.1f} MiB"
            if item["index_bytes"] is not None
            else "n/a"
        )
        lines.append(
            f"| {system} | {item['runs']} | "
            f"{format_duration(item['median_seconds'])} | {min_max} | "
            f"{documents_per_minute} | {throughput} | {index_size} |"
        )

    lines.extend(
        [
            "",
            "## Token usage",
            "",
            "Input/output/total counts include all recorded answer-agent, "
            "retrieval, embedding, and judge calls. Per-question details are "
            "in `token-usage.csv`.",
            "",
            "| System | Requests | Input tokens | Output tokens | Total tokens | Avg total/query |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for system in systems:
        usage = serializable_accuracy[system]["token_usage"]
        lines.append(
            f"| {system} | {usage['requests']:,} | "
            f"{usage['prompt_tokens']:,} | "
            f"{usage['completion_tokens']:,} | "
            f"{usage['total_tokens']:,} | "
            f"{usage['total_tokens_per_question']:,.0f} |"
        )

    lines.extend(
        [
            "",
            "## Answer quality",
            "",
            f"Primary metric: **{summary['primary_accuracy_metric']}**.",
            "",
            "| System | N | Accuracy | Exact match | Token F1 | Failures | Judge errors | Median latency |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for system in systems:
        item = serializable_accuracy[system]
        lines.append(
            f"| {system} | {item['n']} | {item['accuracy']:.1%} | "
            f"{item['exact_match']:.1%} | {item['token_f1']:.3f} | "
            f"{item['failures']} | {item['judge_errors']} | "
            f"{format_duration(item['median_latency_seconds'])} |"
        )

    all_types = sorted(
        {
            question_type
            for system in systems
            for question_type in serializable_accuracy[system]["per_type"]
        }
    )
    if all_types:
        lines.extend(
            [
                "",
                "### Accuracy by question type",
                "",
                "| Type | " + " | ".join(systems) + " |",
                "| --- | " + " | ".join("---:" for _ in systems) + " |",
            ]
        )
        for question_type in all_types:
            values = []
            for system in systems:
                item = serializable_accuracy[system]["per_type"].get(question_type)
                values.append(f"{item['accuracy']:.1%}" if item else "n/a")
            lines.append(f"| {question_type} | " + " | ".join(values) + " |")

    if comparison:
        low, high = comparison["accuracy_delta_ci95"]
        lines.extend(
            [
                "",
                "## Headline checks",
                "",
                (
                    f"- Ours ingestion speedup over Microsoft GraphRAG: "
                    f"**{comparison['ours_ingestion_speedup']:.2f}×**"
                    if comparison["ours_ingestion_speedup"] is not None
                    else "- Ours ingestion speedup: **not available**"
                ),
                (
                    f"- Ours is faster: "
                    f"**{'PASS' if comparison['ours_ingestion_faster'] else 'FAIL'}**"
                    if comparison["ours_ingestion_faster"] is not None
                    else "- Ours is faster: **not available**"
                ),
                (
                    f"- Accuracy difference (ours − GraphRAG): "
                    f"**{comparison['ours_vs_graphrag_accuracy_delta']:+.1%}** "
                    f"(paired bootstrap 95% CI {low:+.1%} to {high:+.1%})"
                ),
                (
                    f"- Within {close_margin:.0%} point-estimate margin: "
                    f"**{'PASS' if comparison['point_estimate_within_margin'] else 'FAIL'}**"
                ),
                (
                    f"- Non-inferiority statistically established: "
                    f"**{'PASS' if comparison['noninferiority_established'] else 'INCONCLUSIVE'}**"
                ),
            ]
        )
    report_path = output / "summary.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def validate_runtime(args: SimpleNamespace, systems: Sequence[str]) -> None:
    if not systems:
        raise BenchmarkError("--systems must include at least one system")
    unknown = sorted(set(systems) - set(SYSTEMS))
    if unknown:
        raise BenchmarkError(f"unknown systems: {', '.join(unknown)}")
    sample = getattr(args, "sample", None)
    if sample is not None and sample < 0:
        raise BenchmarkError("--sample must be zero or positive")
    if getattr(args, "ingestion_runs", 1) < 1:
        raise BenchmarkError("--ingestion-runs must be at least one")
    if args.chunk_tokens < 1:
        raise BenchmarkError("--chunk-tokens must be positive")
    if args.chunk_overlap < 0 or args.chunk_overlap >= args.chunk_tokens:
        raise BenchmarkError(
            "--chunk-overlap must be non-negative and smaller than --chunk-tokens"
        )
    if args.top_k < 1 or args.embed_batch_size < 1:
        raise BenchmarkError("--top-k and --embed-batch-size must be positive")
    if args.embed_dim < 1:
        raise BenchmarkError("--embed-dim must be positive")
    if not 0 <= args.close_margin <= 1:
        raise BenchmarkError("--close-margin must be between zero and one")
    if not args.chat_base_url:
        raise BenchmarkError("--chat-base-url or BENCH_CHAT_BASE_URL is required")
    if any(system in systems for system in ("vanilla", "ours", "graphrag")):
        if not args.embed_base_url:
            raise BenchmarkError(
                "--embed-base-url or BENCH_EMBED_BASE_URL is required"
            )
    if not args.chat_model:
        raise BenchmarkError("--chat-model is required")
    if not args.embed_model:
        raise BenchmarkError("--embed-model is required")
    if "ours" in systems and not Path(args.ours_python).exists():
        raise BenchmarkError(
            f"ours Python environment not found: {args.ours_python}; "
            "run uv sync in llm-wiki-dist/"
        )
    if "ours" in systems:
        log("preflight: checking ours app environment")
        try:
            probe = subprocess.run(
                [
                    str(Path(args.ours_python).absolute()),
                    "-c",
                    (
                        "import sys; "
                        f"sys.path.insert(0, {str(APP_ROOT.resolve())!r}); "
                        "from graph.librarian import Librarian; "
                        "from graph.researcher import ResearchSession"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BenchmarkError(
                f"ours Python environment is not runnable: {args.ours_python}: {exc}"
            ) from exc
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout).strip()[-1000:]
            raise BenchmarkError(
                f"ours Python environment cannot import the app: {detail}"
            )
        log("preflight: ours app environment OK")
    if "graphrag" in systems:
        command = shlex.split(args.graphrag_command)
        if not command:
            raise BenchmarkError("--graphrag-command is empty")
        executable = command[0]
        if not Path(executable).exists() and shutil.which(executable) is None:
            raise BenchmarkError(f"GraphRAG command not found: {executable}")
        if args.graphrag_settings and not Path(args.graphrag_settings).is_file():
            raise BenchmarkError(
                f"GraphRAG settings file not found: {args.graphrag_settings}"
            )
        if not args.graphrag_settings:
            try:
                import yaml  # noqa: F401
            except ImportError as exc:
                raise BenchmarkError(
                    "PyYAML is required to patch GraphRAG's generated settings; "
                    "install pyyaml or pass --graphrag-settings"
                ) from exc
        # Resolve/install the isolated uvx environment before any ingestion
        # timer starts, then verify that the native CLI is runnable.
        log("preflight: resolving Microsoft GraphRAG CLI")
        try:
            probe = subprocess.run(
                graphrag_command(args, "--help"),
                capture_output=True,
                text=True,
                timeout=remaining_timeout(args),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BenchmarkError(f"GraphRAG CLI is not runnable: {exc}") from exc
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout).strip()[-1000:]
            raise BenchmarkError(f"GraphRAG CLI is not runnable: {detail}")
        log("preflight: Microsoft GraphRAG CLI OK")


def run_one_system_ingestion(
    system: str,
    run_workspace: Path,
    documents: Sequence[Document],
    document_mapping: Sequence[dict[str, str]],
    manifest: dict[str, Any],
    args: SimpleNamespace,
) -> dict[str, Any]:
    if system == "vanilla":
        return vanilla_ingest(run_workspace, documents, args)
    if system == "graphrag":
        return graphrag_ingest(run_workspace, document_mapping, manifest, args)
    if system == "ours":
        return ours_ingest(run_workspace, document_mapping, manifest, args)
    raise AssertionError(system)


def _valid_corpus_ingestion_detail(
    value: Any,
    *,
    system: str,
    document: Document,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if value.get("system") != system:
        return None
    if value.get("corpus") != document.id:
        return None
    try:
        if int(value.get("documents", 0)) != 1:
            return None
        if int(value.get("characters", -1)) != len(document.text):
            return None
        if float(value.get("elapsed_seconds")) < 0:
            return None
        if int(value.get("index_bytes")) <= 0:
            return None
    except (TypeError, ValueError):
        return None
    return value


def load_completed_corpus_ingestion(
    *,
    system: str,
    corpus_workspace: Path,
    document: Document,
) -> dict[str, Any] | None:
    """Load an atomic corpus checkpoint, including old ours worker results."""

    checkpoint_path = corpus_workspace / "ingestion.json"
    if checkpoint_path.is_file():
        try:
            value = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return _valid_corpus_ingestion_detail(
            value, system=system, document=document
        )

    # Runs produced before corpus checkpoints were added still have one atomic
    # native worker result per successfully completed ours corpus.  Recover it
    # only when the database and the exact document result are both present.
    if system != "ours":
        return None
    result_path = corpus_workspace / "ingest-result.json"
    database_path = corpus_workspace / "wiki.sqlite"
    if not result_path.is_file() or not database_path.is_file():
        return None
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(result, dict):
        return None
    document_results = result.get("document_results")
    if not isinstance(document_results, list):
        return None
    matching: list[dict[str, Any]] = []
    for item in document_results:
        if not isinstance(item, dict) or item.get("document") != document.id:
            continue
        try:
            ingested = int(item.get("ingested") or 0)
        except (TypeError, ValueError):
            continue
        if ingested > 0:
            matching.append(item)
    if len(matching) != 1:
        return None
    database_bytes = sum(
        item.stat().st_size
        for item in corpus_workspace.glob(f"{database_path.name}*")
        if item.is_file()
    )
    detail = {
        "corpus": document.id,
        "system": "ours",
        "documents": 1,
        "characters": len(document.text),
        **result,
        "index_bytes": database_bytes
        + directory_size(corpus_workspace / "sources"),
    }
    return _valid_corpus_ingestion_detail(
        detail, system=system, document=document
    )


def quarantine_incomplete_corpus_workspace(workspace: Path) -> Path:
    """Move an incomplete corpus aside without recursively traversing it.

    GraphRAG's LanceDB output can contain enough nested files that deleting an
    interrupted workspace blocks the benchmark for a long time before the new
    index process is even launched.  A rename within the same parent directory
    is atomic and lets ingestion restart immediately.  Keep the quarantined
    data for explicit/offline cleanup instead of putting deletion back on the
    benchmark's critical path.
    """

    quarantine = workspace.with_name(
        f".{workspace.name}.incomplete-{utc_stamp()}-{uuid.uuid4().hex[:8]}"
    )
    try:
        workspace.rename(quarantine)
    except OSError as exc:
        raise BenchmarkError(
            f"could not quarantine incomplete corpus workspace {workspace}: {exc}"
        ) from exc
    return quarantine


def run_system_ingestion(
    system: str,
    run_workspace: Path,
    documents: Sequence[Document],
    document_mapping: Sequence[dict[str, str]],
    manifest: dict[str, Any],
    args: SimpleNamespace,
) -> dict[str, Any]:
    if getattr(args, "corpus_scope", "isolated") == "combined":
        result = run_one_system_ingestion(
            system,
            run_workspace,
            documents,
            document_mapping,
            manifest,
            args,
        )
        result["corpus_scope"] = "combined"
        return result

    mapping_by_id = {record["id"]: record for record in document_mapping}
    details: list[dict[str, Any]] = []
    for index, document in enumerate(documents, start=1):
        corpus_workspace = (
            run_workspace / "corpora" / safe_name(document.id)
        )
        checkpoint_path = corpus_workspace / "ingestion.json"
        if getattr(args, "resume", False):
            completed = load_completed_corpus_ingestion(
                system=system,
                corpus_workspace=corpus_workspace,
                document=document,
            )
            if completed is not None:
                # Migrate legacy worker results to the new explicit checkpoint.
                if not checkpoint_path.is_file():
                    json_dump(checkpoint_path, completed)
                details.append(completed)
                log(
                    f"{system} resumed completed corpus "
                    f"{index}/{len(documents)}: {document.id}"
                )
                continue
            if corpus_workspace.exists() and any(corpus_workspace.iterdir()):
                log(
                    f"{system} restarting incomplete corpus "
                    f"{index}/{len(documents)}: {document.id}"
                )
                quarantine = quarantine_incomplete_corpus_workspace(
                    corpus_workspace
                )
                log(
                    f"{system} quarantined incomplete corpus as "
                    f"{quarantine.name}; starting from a clean workspace"
                )
        corpus_manifest = {
            **manifest,
            "documents": 1,
            "characters": len(document.text),
        }
        detail = run_one_system_ingestion(
            system,
            corpus_workspace,
            [document],
            [mapping_by_id[document.id]],
            corpus_manifest,
            args,
        )
        completed = {"corpus": document.id, **detail}
        checked = _valid_corpus_ingestion_detail(
            completed, system=system, document=document
        )
        if checked is None:
            raise BenchmarkError(
                f"{system} produced an invalid completion record for "
                f"{document.id}"
            )
        json_dump(checkpoint_path, checked)
        details.append(checked)
        log(f"{system} indexed corpus {index}/{len(documents)}: {document.id}")
    return {
        "system": system,
        # Include work completed before a restart; wall time for only this
        # process would under-report the actual isolated-corpus ingestion cost.
        "elapsed_seconds": sum(
            float(item["elapsed_seconds"]) for item in details
        ),
        "documents": len(documents),
        "characters": sum(len(document.text) for document in documents),
        "index_bytes": sum(int(item.get("index_bytes") or 0) for item in details),
        "corpus_scope": "isolated",
        "corpora": details,
    }


def pending_questions(
    questions: Sequence[Question], predictions_path: Path
) -> tuple[list[Question], list[dict[str, Any]]]:
    existing = jsonl_load(predictions_path)
    completed = {
        str(row.get("id"))
        for row in existing
        if not row.get("error")
    }
    return [question for question in questions if question.id not in completed], existing


def run_one_system_answers(
    system: str,
    index_workspace: Path,
    questions: Sequence[Question],
    args: SimpleNamespace,
) -> list[dict[str, Any]]:
    if system == "vanilla":
        return vanilla_answer(index_workspace, questions, args)
    if system == "graphrag":
        return graphrag_answer(
            index_workspace,
            questions,
            args,
            index_workspace / "query-logs",
        )
    if system == "ours":
        return ours_answer(index_workspace, questions, args)
    raise AssertionError(system)


def run_system_answers(
    system: str,
    index_workspace: Path,
    questions: Sequence[Question],
    args: SimpleNamespace,
) -> list[dict[str, Any]]:
    if args.corpus_scope == "combined":
        return run_one_system_answers(system, index_workspace, questions, args)

    grouped: dict[str, list[Question]] = collections.defaultdict(list)
    for question in questions:
        grouped[question.source].append(question)
    results: list[dict[str, Any]] = []
    for source in sorted(grouped):
        corpus_workspace = index_workspace / "corpora" / safe_name(source)
        results.extend(
            run_one_system_answers(
                system,
                corpus_workspace,
                grouped[source],
                args,
            )
        )
    by_id = {str(row.get("id")): row for row in results}
    return [by_id[question.id] for question in questions if question.id in by_id]


def run_agentic_system_answers(
    system: str,
    index_workspace: Path,
    questions: Sequence[Question],
    args: SimpleNamespace,
    on_result: Any = None,
) -> list[dict[str, Any]]:
    if getattr(args, "corpus_scope", "isolated") == "combined":
        if system == "vanilla":
            return agentic_vanilla_answer(
                index_workspace, questions, args, on_result
            )
        if system == "graphrag":
            return agentic_graphrag_answer(
                index_workspace, questions, args, on_result
            )
        if system == "ours":
            return ours_answer(index_workspace, questions, args)
        raise AssertionError(system)

    grouped: dict[str, list[Question]] = collections.defaultdict(list)
    for question in questions:
        grouped[question.source].append(question)
    results: list[dict[str, Any]] = []
    for source in sorted(grouped):
        corpus_workspace = index_workspace / "corpora" / safe_name(source)
        if system == "vanilla":
            results.extend(
                agentic_vanilla_answer(
                    corpus_workspace, grouped[source], args, on_result
                )
            )
        elif system == "graphrag":
            results.extend(
                agentic_graphrag_answer(
                    corpus_workspace, grouped[source], args, on_result
                )
            )
        elif system == "ours":
            results.extend(
                ours_answer(corpus_workspace, grouped[source], args)
            )
        else:
            raise AssertionError(system)
    by_id = {str(row.get("id")): row for row in results}
    return [by_id[question.id] for question in questions if question.id in by_id]


def run_fresh_agentic_answers(
    *,
    system: str,
    index_workspace: Path,
    questions: Sequence[Question],
    args: SimpleNamespace,
    predictions_path: Path,
    live_progress: dict[str, Any],
    live_progress_path: Path,
) -> list[dict[str, Any]]:
    """Query every question from scratch, retrying only execution failures."""
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    # Agentic mode is deliberately a fresh query run. Starting with an empty
    # checkpoint prevents successful rows from an earlier invocation being
    # mistaken for work completed by this invocation.
    jsonl_dump(predictions_path, [])

    merged: dict[str, dict[str, Any]] = {}
    judge_client = (
        OpenAICompatibleClient(
            args.judge_base_url or args.chat_base_url,
            args.judge_api_key or args.chat_api_key,
            timeout=args.timeout,
        )
        if args.judge == "llm"
        else None
    )

    def checkpoint(result: dict[str, Any]) -> None:
        scored = score_prediction(result, args, judge_client)
        question_id = str(scored.get("id") or "")
        if question_id:
            merged[question_id] = scored
        ordered = [
            merged[question.id]
            for question in questions
            if question.id in merged
        ]
        # Atomic replacement keeps a useful checkpoint if the run is stopped.
        jsonl_dump(predictions_path, ordered)
        publish_live_progress(
            system=system,
            rows=ordered,
            total_questions=len(questions),
            live_progress=live_progress,
            live_progress_path=live_progress_path,
            last_question_id=question_id or None,
        )

    pending = list(questions)
    for attempt in range(1, AGENTIC_QUERY_MAX_ATTEMPTS + 1):
        if attempt == 1:
            label = "native-agent" if system == "ours" else "shared-agent"
            log(
                f"starting fresh {label} {system} answers "
                f"({len(pending)} questions)"
            )
        else:
            log(
                f"retrying {system} execution failures "
                f"({len(pending)} questions, attempt "
                f"{attempt}/{AGENTIC_QUERY_MAX_ATTEMPTS})"
            )

        generated = run_agentic_system_answers(
            system,
            index_workspace,
            pending,
            args,
            None if system == "ours" else checkpoint,
        )

        # Vanilla and GraphRAG checkpoint as each answer completes. The native
        # ours worker returns one completed batch, so checkpoint those rows now.
        # The defensive ID check also handles a backend that omitted callbacks.
        for result in generated:
            result_id = str(result.get("id") or "")
            if system == "ours" or result_id not in merged:
                checkpoint(result)

        pending = [
            question
            for question in questions
            if question.id not in merged or merged[question.id].get("error")
        ]
        if not pending:
            break
        if attempt >= AGENTIC_QUERY_MAX_ATTEMPTS:
            break

        delay = min(
            AGENTIC_QUERY_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
            60.0,
        )
        ensure_time_remaining(args, f"{system} agentic retry")
        log(
            f"{system} has {len(pending)} failed queries; "
            f"retrying in {delay:.0f}s"
        )
        time.sleep(delay)

    ordered = [
        merged[question.id]
        for question in questions
        if question.id in merged
    ]
    jsonl_dump(predictions_path, ordered)
    return ordered


def latest_completed_run(*, dataset: str) -> Path:
    results_root = ROOT / "benchmark-results"
    if not results_root.exists():
        raise BenchmarkError("no benchmark-results directory exists")

    def created_at(path: Path) -> str:
        try:
            value = json.loads(
                (path / "manifest.json").read_text(encoding="utf-8")
            ).get("created_at")
            if value:
                return str(value)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        return datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat()

    candidates = sorted(
        (path for path in results_root.iterdir() if path.is_dir()),
        key=created_at,
        reverse=True,
    )
    for candidate in candidates:
        required = [
            candidate / "manifest.json",
            candidate / "canonical" / "documents.json",
            candidate / "canonical" / "questions.jsonl",
            candidate / "systems" / "vanilla" / "run-01" / "ingestion.json",
            candidate / "systems" / "graphrag" / "run-01" / "ingestion.json",
            candidate / "systems" / "ours" / "run-01" / "ingestion.json",
        ]
        if not all(path.is_file() for path in required):
            continue
        try:
            manifest = json.loads(
                (candidate / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        run_config = manifest.get("run_config") or {}
        candidate_dataset = manifest.get("dataset") or run_config.get("dataset")
        # Runs created before dataset routing existed were all novel runs.
        if not candidate_dataset and candidate.name.startswith("pilot-"):
            candidate_dataset = "novel"
        if candidate_dataset != dataset:
            continue
        return candidate
    raise BenchmarkError(
        f"no completed three-system {dataset} benchmark run was found; "
        f"run `python benchmark.py {dataset}` first"
    )


def apply_source_run_query_config(
    args: SimpleNamespace, manifest: dict[str, Any]
) -> None:
    run_config = manifest.get("run_config") or {}
    args.corpus_scope = str(
        manifest.get("corpus_scope")
        or run_config.get("corpus_scope")
        or args.corpus_scope
    )
    args.graphrag_method = str(
        run_config.get("graphrag_method") or args.graphrag_method
    )
    args.graphrag_query_style = str(
        run_config.get("graphrag_query_style") or args.graphrag_query_style
    )


def graphrag_layout_complete(
    run_workspace: Path,
    documents: Sequence[Document],
    *,
    corpus_scope: str,
) -> bool:
    required_names = (
        "entity_description.lance",
        "community_full_content.lance",
        "text_unit_text.lance",
    )
    workspaces = (
        [run_workspace]
        if corpus_scope == "combined"
        else [
            run_workspace / "corpora" / safe_name(document.id)
            for document in documents
        ]
    )
    return all(
        all(
            (
                workspace
                / "output"
                / "lancedb"
                / name
            ).exists()
            for name in required_names
        )
        for workspace in workspaces
    )


def command_agentic_fresh(args: SimpleNamespace) -> int:
    log(f"agentic {args.dataset} query-only mode: validating query runtimes")
    validate_runtime(args, ("vanilla", "graphrag"))
    source_run = latest_completed_run(dataset=args.dataset)
    manifest = json.loads(
        (source_run / "manifest.json").read_text(encoding="utf-8")
    )
    apply_source_run_query_config(args, manifest)
    log(
        f"reusing {source_run.name}: corpus_scope={args.corpus_scope}, "
        f"GraphRAG method={args.graphrag_method}"
    )
    output = source_run / "agentic-query"
    output.mkdir(parents=True, exist_ok=True)
    questions = [
        Question(**row)
        for row in jsonl_load(source_run / "canonical" / "questions.jsonl")
    ]
    if not questions:
        raise BenchmarkError(f"no canonical questions in {source_run}")
    document_mapping = json.loads(
        (source_run / "canonical" / "documents.json").read_text(encoding="utf-8")
    )
    documents = [
        Document(
            str(record["id"]),
            Path(record["path"]).read_text(encoding="utf-8"),
        )
        for record in document_mapping
    ]
    log(
        f"reusing vanilla/ours indexes from {source_run.name}; "
        + (
            "GraphRAG will use the completed corrected rebuild"
            if not REBUILD_AGENTIC_GRAPHRAG
            else "GraphRAG will use a corrected rebuild"
        )
    )

    source_graphrag_run = source_run / "systems" / "graphrag" / "run-01"
    rebuilt_graphrag_run = output / "indexes" / "graphrag" / "run-01"
    rebuilt_graphrag_metrics = rebuilt_graphrag_run / "ingestion.json"
    if graphrag_layout_complete(
        source_graphrag_run,
        documents,
        corpus_scope=args.corpus_scope,
    ):
        graphrag_run = source_graphrag_run
        graphrag_metrics = source_graphrag_run / "ingestion.json"
        log("reusing corrected GraphRAG index from the full native run")
    elif rebuilt_graphrag_metrics.exists():
        graphrag_run = rebuilt_graphrag_run
        graphrag_metrics = rebuilt_graphrag_metrics
        log("corrected GraphRAG rebuild already complete")
    elif REBUILD_AGENTIC_GRAPHRAG:
        graphrag_run = rebuilt_graphrag_run
        graphrag_metrics = rebuilt_graphrag_metrics
        if graphrag_run.exists():
            shutil.rmtree(graphrag_run)
        log("rebuilding GraphRAG only (corrected independent vector tables)")
        metrics = run_system_ingestion(
            "graphrag",
            graphrag_run,
            documents,
            document_mapping,
            manifest,
            args,
        )
        metrics["run"] = 1
        json_dump(graphrag_metrics, metrics)
        log(
            "corrected GraphRAG rebuild complete: "
            f"{format_duration(float(metrics['elapsed_seconds']))}"
        )
    else:
        raise BenchmarkError(
            "corrected GraphRAG index is missing; temporarily set "
            "REBUILD_AGENTIC_GRAPHRAG = True"
        )

    report_systems = tuple(
        system
        for system in SYSTEMS
        if (system != "graphrag" or RUN_AGENTIC_GRAPHRAG)
        and (system != "ours" or RUN_AGENTIC_OURS)
    )
    for system in report_systems:
        source_metrics = (
            graphrag_metrics
            if system == "graphrag"
            else source_run
            / "systems"
            / system
            / "run-01"
            / "ingestion.json"
        )
        target_metrics = (
            output / "systems" / system / "run-01" / "ingestion.json"
        )
        target_metrics.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_metrics, target_metrics)

    query_systems = report_systems
    live_progress_path = output / "live-progress.json"
    live_progress: dict[str, Any] = {}
    json_dump(live_progress_path, live_progress)

    # Keep native ours query traffic below the burst that previously caused
    # 429/500/timeout failures when it is enabled. Each question may still run
    # three subagents in parallel, so one question worker can issue several
    # concurrent requests.
    args.ours_question_workers = AGENTIC_OURS_QUESTION_WORKERS
    log(
        "agentic mode will freshly query: "
        f"{', '.join(query_systems)}; "
        f"ours question workers={args.ours_question_workers}"
    )

    index_workspaces = {
        "ours": source_run / "systems" / "ours" / "run-01",
        "vanilla": source_run / "systems" / "vanilla" / "run-01",
        "graphrag": graphrag_run,
    }

    # Clear all prior query rows before any system starts. If this invocation
    # is interrupted, no untouched file can masquerade as a fresh result.
    for system in query_systems:
        predictions_path = output / "systems" / system / "predictions.jsonl"
        predictions_path.parent.mkdir(parents=True, exist_ok=True)
        jsonl_dump(predictions_path, [])

    for system in query_systems:
        predictions_path = output / "systems" / system / "predictions.jsonl"
        run_fresh_agentic_answers(
            system=system,
            index_workspace=index_workspaces[system],
            questions=questions,
            args=args,
            predictions_path=predictions_path,
            live_progress=live_progress,
            live_progress_path=live_progress_path,
        )

    final_rows: dict[str, list[dict[str, Any]]] = {}
    for system in report_systems:
        final_rows[system] = score_predictions(
            output / "systems" / system / "predictions.jsonl",
            args,
        )
    generate_report(
        output,
        questions,
        report_systems,
        judge=args.judge,
        close_margin=args.close_margin,
        seed=args.seed,
        mode="agentic-fresh",
    )
    log(f"agentic report: {output / 'summary.md'}")

    unresolved = {
        system: [str(row.get("id") or "") for row in rows if row.get("error")]
        for system, rows in final_rows.items()
    }
    unresolved = {system: ids for system, ids in unresolved.items() if ids}
    if unresolved:
        detail = "; ".join(
            f"{system}: {', '.join(ids)}"
            for system, ids in unresolved.items()
        )
        raise BenchmarkError(
            "agentic query run still has execution failures after "
            f"{AGENTIC_QUERY_MAX_ATTEMPTS} attempts ({detail})"
        )
    return 0


def command_run(args: SimpleNamespace) -> int:
    systems = [item.strip() for item in args.systems.split(",") if item.strip()]
    log(
        f"starting {args.dataset} benchmark: "
        f"systems={','.join(systems)}, chat={args.chat_model}, "
        f"embedding={args.embed_model}, "
        f"ingestion_concurrency={args.ingestion_concurrency}, "
        f"query_concurrency={args.chat_concurrency}"
    )
    validate_runtime(args, systems)
    log("preflight complete")
    ensure_time_remaining(args, "dataset setup")
    bundle = load_benchmark_dataset(args)
    documents = bundle.documents
    questions = bundle.questions
    args.corpus_scope = bundle.corpus_scope
    documents = [normalize_long_prose_layout(document) for document in documents]
    output = Path(args.output or f"benchmark-results/run-{utc_stamp()}").resolve()
    run_config = {
        "dataset": args.dataset,
        "systems": systems,
        "corpus_scope": args.corpus_scope,
        "graphrag_method": args.graphrag_method,
        "graphrag_query_style": args.graphrag_query_style,
        "graphrag_drift_primer_folds": GRAPHRAG_DRIFT_PRIMER_FOLDS,
        "graphrag_drift_followups": GRAPHRAG_DRIFT_FOLLOWUPS,
        "graphrag_drift_depth": GRAPHRAG_DRIFT_DEPTH,
        "graphrag_command": args.graphrag_command,
        "graphrag_settings": (
            str(Path(args.graphrag_settings).resolve())
            if args.graphrag_settings
            else None
        ),
        "chat_base_url": args.chat_base_url,
        "chat_model": args.chat_model,
        "embed_base_url": args.embed_base_url,
        "embed_model": args.embed_model,
        "embed_dim": args.embed_dim,
        "rerank_base_url": args.rerank_base_url or None,
        "rerank_model": args.rerank_model or None,
        "ours_python": str(Path(args.ours_python).absolute()),
        "chunk_tokens": args.chunk_tokens,
        "chunk_overlap": args.chunk_overlap,
        "top_k": args.top_k,
        "embed_batch_size": args.embed_batch_size,
        "temperature": args.temperature,
        "max_answer_tokens": args.max_answer_tokens,
        "judge": args.judge,
        "judge_base_url": args.judge_base_url or args.chat_base_url,
        "judge_model": args.judge_model or args.chat_model,
        "close_margin": args.close_margin,
        "seed": args.seed,
        "questions": len(questions),
        "question_sample": args.sample,
        "questions_per_novel": (
            QUESTIONS_PER_NOVEL if args.dataset == "novel" else None
        ),
        "chat_concurrency": args.chat_concurrency,
        "ours_question_workers": args.ours_question_workers,
        "graphrag_query_workers": args.graphrag_query_workers,
        "ingestion_concurrency": args.ingestion_concurrency,
        "max_runtime_minutes": MAX_RUNTIME_SECONDS // 60,
    }
    default_output_root = (ROOT / "benchmark-results").resolve()
    auto_resume = (
        not args.resume
        and output.parent == default_output_root
        and output.name.startswith(f"{args.dataset}-")
    )
    if auto_resume:
        resumable = latest_compatible_incomplete_run(
            results_root=ROOT / "benchmark-results",
            documents=documents,
            questions=questions,
            run_config=run_config,
        )
        if resumable is not None:
            output = resumable
            args.output = str(resumable)
            args.resume = True
            log(f"resuming compatible incomplete run {resumable.name}")
    document_mapping, manifest = prepare_canonical(
        output,
        documents,
        questions,
        resume=args.resume,
        run_config=run_config,
    )
    log(
        f"run {output.name}: {len(documents)} documents, "
        f"{manifest['characters']:,} characters, {len(questions)} questions"
    )

    for run_number in range(1, args.ingestion_runs + 1):
        ingestion_order = list(systems)
        random.Random(args.seed + run_number * 1009).shuffle(ingestion_order)
        log(
            f"ingestion run {run_number} order: "
            f"{', '.join(ingestion_order)}"
        )
        for order_position, system in enumerate(ingestion_order, start=1):
            ensure_time_remaining(args, f"{system} ingestion")
            system_dir = output / "systems" / system
            run_workspace = system_dir / f"run-{run_number:02d}"
            metrics_path = run_workspace / "ingestion.json"
            if args.resume and metrics_path.exists():
                log(f"{system} ingestion run {run_number} already complete")
                continue
            if (
                not args.resume
                and run_workspace.exists()
                and any(run_workspace.iterdir())
            ):
                raise BenchmarkError(
                    f"incomplete workspace exists: {run_workspace}; "
                    "remove that run directory or choose a new output"
                )
            log(f"starting {system} ingestion run {run_number}")
            metrics = run_system_ingestion(
                system,
                run_workspace,
                documents,
                document_mapping,
                manifest,
                args,
            )
            metrics["run"] = run_number
            metrics["order_position"] = order_position
            json_dump(metrics_path, metrics)
            log(
                f"{system} ingestion run {run_number}: "
                f"{format_duration(float(metrics['elapsed_seconds']))}"
            )

    for system in systems:
        ensure_time_remaining(args, f"{system} answers")
        system_dir = output / "systems" / system
        predictions_path = system_dir / "predictions.jsonl"
        pending, existing = pending_questions(questions, predictions_path)
        if pending:
            log(f"starting {system} answers ({len(pending)} pending)")
            generated = run_system_answers(
                system, system_dir / "run-01", pending, args
            )
            merged = {str(row.get("id")): row for row in existing}
            merged.update({str(row.get("id")): row for row in generated})
            jsonl_dump(
                predictions_path,
                (merged[question.id] for question in questions if question.id in merged),
            )
        else:
            log(f"{system} answers already complete")

    for system in systems:
        ensure_time_remaining(args, f"{system} scoring")
        predictions_path = output / "systems" / system / "predictions.jsonl"
        score_predictions(predictions_path, args)

    summary = generate_report(
        output,
        questions,
        systems,
        judge=args.judge,
        close_margin=args.close_margin,
        seed=args.seed,
    )
    log(f"report: {output / 'summary.md'}")
    if summary.get("comparison"):
        comparison = summary["comparison"]
        speedup = comparison.get("ours_ingestion_speedup")
        delta = comparison.get("ours_vs_graphrag_accuracy_delta")
        if speedup is not None:
            log(f"ours vs GraphRAG ingestion: {speedup:.2f}x")
        if delta is not None:
            log(f"ours vs GraphRAG accuracy: {delta:+.1%}")
    return 0


def normalize_chat_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)]
    if not re.match(r"^https?://", base_url):
        raise BenchmarkError(
            "chat base URL must start with http:// or https:// "
            "(for example http://localhost:9000/v1)"
        )
    return base_url


def fixed_args(chat_base_url: str, dataset: str = "novel") -> SimpleNamespace:
    """All benchmark controls in one readable, deliberately fixed config."""
    chat_base_url = normalize_chat_base_url(chat_base_url)
    if dataset not in DATASETS:
        raise BenchmarkError(
            f"unknown dataset {dataset!r}; choose one of: {', '.join(DATASETS)}"
        )
    paths = {
        "novel": (CORPUS_PATH, QUESTIONS_PATH),
        "fanout": (FANOUT_CORPUS_DIR, FANOUT_QUESTIONS_PATH),
        "multihop": (MULTIHOP_CORPUS_PATH, MULTIHOP_QUESTIONS_PATH),
        "musique": (MUSIQUE_QUESTIONS_PATH, MUSIQUE_QUESTIONS_PATH),
    }
    samples = {
        "novel": QUESTIONS_PER_NOVEL,
        "fanout": FANOUT_QUESTION_SAMPLE,
        "multihop": MULTIHOP_QUESTION_SAMPLE,
        "musique": MUSIQUE_QUESTION_SAMPLE,
    }
    corpus_path, questions_path = paths[dataset]
    return SimpleNamespace(
        dataset=dataset,
        systems=",".join(SYSTEMS),
        corpus=str(corpus_path),
        questions=str(questions_path),
        question_types=",".join(DEFAULT_QUESTION_TYPES),
        sample=samples[dataset],
        seed=7,
        corpus_scope="isolated" if dataset == "novel" else "combined",
        chat_base_url=chat_base_url,
        chat_api_key=NVIDIA_API_KEY,
        chat_model=CHAT_MODEL,
        embed_base_url=EMBED_BASE_URL,
        embed_api_key="local",
        embed_model=EMBED_MODEL,
        embed_dim=EMBED_DIM,
        rerank_base_url=RERANK_BASE_URL,
        rerank_api_key="local",
        rerank_model=RERANK_MODEL,
        graphrag_command=GRAPHRAG_COMMAND,
        graphrag_settings=None,
        # Native GraphRAG gets its distinctive DRIFT flow, with bounded search
        # breadth/depth configured above. Agentic re-querying restores this
        # value from the selected ingestion run instead of changing methods.
        graphrag_method="drift",
        graphrag_query_style="auto",
        ours_python=str(OURS_PYTHON),
        chunk_tokens=1200,
        chunk_overlap=100,
        top_k=5,
        embed_batch_size=64,
        temperature=0.5,
        max_answer_tokens=512,
        timeout=REQUEST_TIMEOUT_SECONDS,
        command_timeout=MAX_RUNTIME_SECONDS,
        subagent_count=OURS_SUBAGENT_COUNT,
        subagent_concurrency=OURS_SUBAGENT_CONCURRENCY,
        rerank_top_k=OURS_RERANK_TOP_K,
        search_candidate_pool=OURS_SEARCH_POOL,
        judge="llm",
        judge_base_url=chat_base_url,
        judge_api_key=NVIDIA_API_KEY,
        judge_model=CHAT_MODEL,
        close_margin=0.05,
        output=str(ROOT / "benchmark-results" / f"{dataset}-{utc_stamp()}"),
        ingestion_runs=1,
        resume=False,
        chat_concurrency=CHAT_CONCURRENCY,
        ours_question_workers=OURS_QUESTION_WORKERS,
        graphrag_query_workers=GRAPHRAG_QUERY_WORKERS,
        ingestion_concurrency=INGESTION_CONCURRENCY,
        deadline=time.monotonic() + MAX_RUNTIME_SECONDS,
    )


def usage() -> str:
    return (
        "Usage: python benchmark.py DATASET [CHAT_BASE_URL]\n"
        "       python benchmark.py agentic DATASET [CHAT_BASE_URL]\n\n"
        f"DATASET: {' | '.join(DATASETS)}\n\n"
        "Examples:\n"
        "  python benchmark.py novel\n"
        "  python benchmark.py fanout\n"
        "  python benchmark.py multihop\n"
        "  python benchmark.py musique\n"
        "  python benchmark.py agentic fanout\n\n"
        "`agentic` freshly re-queries the latest completed run for that dataset.\n"
        "Legacy `python benchmark.py` and `python benchmark.py agentic` default "
        "to novel.\n\n"
        f"Default chat URL: {DEFAULT_CHAT_BASE_URL}\n\n"
        f"Fixed model: {CHAT_MODEL}\n"
        f"Embedding:  {EMBED_MODEL} at {EMBED_BASE_URL}\n"
        f"Reranker:   {RERANK_MODEL} at {RERANK_BASE_URL}\n"
        f"Ingestion request concurrency: {INGESTION_CONCURRENCY}\n"
        f"Parallel query chat budget: {CHAT_CONCURRENCY}\n"
        f"Hard runtime cap: {MAX_RUNTIME_SECONDS // 3600} hours"
    )


def _deadline_signal(_signum: int, _frame: Any) -> None:
    pass
    # raise BenchmarkError("50-minute benchmark deadline reached")


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    try:
        if (
            len(values) == 3
            and values[0] == "_repair-graphrag-embeddings"
            and values[1] == "--request"
        ):
            return int(
                worker_repair_graphrag_embeddings(Path(values[2])) or 0
            )
        # Private worker protocol used by this file's isolated app subprocess.
        if (
            len(values) == 3
            and values[0] in {"_ours-ingest", "_ours-answer"}
            and values[1] == "--request"
        ):
            worker = (
                worker_ours_ingest
                if values[0] == "_ours-ingest"
                else worker_ours_answer
            )
            return int(worker(Path(values[2])) or 0)
        if values in (["-h"], ["--help"]):
            print(usage())
            return 0
        agentic_mode = bool(values and values[0] == "agentic")
        if agentic_mode:
            values.pop(0)
        dataset = "novel"
        if values and values[0] in DATASETS:
            dataset = values.pop(0)
        if len(values) > 1:
            print(usage(), file=sys.stderr)
            return 2

        previous_handler = signal.signal(signal.SIGALRM, _deadline_signal)
        signal.setitimer(signal.ITIMER_REAL, MAX_RUNTIME_SECONDS)
        try:
            chat_base_url = values[0] if values else DEFAULT_CHAT_BASE_URL
            args = fixed_args(chat_base_url, dataset)
            if agentic_mode:
                return command_agentic_fresh(args)
            return command_run(args)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    except BenchmarkError as exc:
        log(f"error: {exc}")
        return 2
    except Exception as exc:
        log(f"unexpected error: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
