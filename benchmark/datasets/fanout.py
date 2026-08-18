"""FanOutQA loader with resumable per-revision Wikipedia caching.

--sample caps the question count, but every one of FanOutQA's 310 questions
cites at least one full Wikipedia article long enough to need llm-wiki's
chunked ingest path, so unlike musique the per-question evidence size does
not shrink with the sample -- only the question count (and total revisions
fetched) does.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import benchmark as legacy

from .common import prepare_dataset


NAME = "fanout"


def load(args: SimpleNamespace) -> legacy.DatasetBundle:
    questions_path = Path(args.questions)
    if questions_path.resolve() == legacy.FANOUT_QUESTIONS_PATH.resolve():
        legacy.download_if_missing(questions_path, legacy.FANOUT_QUESTIONS_URL)
    raw = legacy.read_records(questions_path)
    selected = [
        item
        for item in raw
        if str(item.get("question") or "").strip()
        and legacy.answer_text(item.get("answer"))
        and legacy.fanout_evidence_records(item)
    ]
    if not selected:
        raise legacy.BenchmarkError(
            f"no evidence-backed FanOutQA questions in {questions_path}"
        )
    selected = legacy.stable_record_sample(
        selected, sample=args.sample, seed=args.seed
    )

    questions: list[legacy.Question] = []
    keys_by_question: dict[str, set[tuple[str, str]]] = {}
    evidence_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for index, item in enumerate(selected, start=1):
        evidence = legacy.fanout_evidence_records(item)
        keys = {(str(record["pageid"]), str(record["revid"])) for record in evidence}
        for record in evidence:
            evidence_by_key[(str(record["pageid"]), str(record["revid"]))] = record
        categories = [str(value) for value in item.get("categories") or []]
        question_id = str(item.get("id") or f"fanout-{index:05d}")
        keys_by_question[question_id] = keys
        questions.append(
            legacy.Question(
                id=question_id,
                source="fanout",
                question=str(item["question"]).strip(),
                answer=legacy.answer_text(item.get("answer")),
                question_type="Fan-out"
                + (f" / {', '.join(categories)}" if categories else ""),
                evidence=evidence,
            )
        )

    cache_dir = Path(args.corpus)
    documents: list[legacy.Document] = []
    # Articles deleted from Wikipedia since the 2023 epoch cannot be fetched at
    # any revision. Keeping their questions would score every system zero for a
    # missing source rather than for its own retrieval, so both go.
    missing: set[tuple[str, str]] = set()
    # Cached revisions return instantly and uncached ones cost a network round
    # trip each, so this loop can run for many silent minutes on a cold cache.
    with legacy.progress_reporter(
        len(evidence_by_key), "fanout wikipedia revisions"
    ) as advance:
        for key in sorted(evidence_by_key):
            record = evidence_by_key[key]
            title = str(record.get("title") or record.get("pageid") or "Wikipedia")
            try:
                text = legacy.load_fanout_revision_text(record, cache_dir)
            except legacy.WikipediaContentGone as exc:
                legacy.log(f"dropping deleted FanOutQA evidence {title!r}: {exc}")
                missing.add(key)
                advance(title)
                continue
            documents.append(
                legacy.Document(
                    f"wikipedia-{key[0]}-{key[1]}",
                    f"Title: {title}\n\n{text}",
                )
            )
            advance(title)
    if missing:
        retained = [
            question
            for question in questions
            if not (keys_by_question[question.id] & missing)
        ]
        legacy.log(
            f"FanOutQA: dropped {len(questions) - len(retained)} of "
            f"{len(questions)} questions citing {len(missing)} deleted articles"
        )
        questions = retained
        if not questions:
            raise legacy.BenchmarkError(
                "every FanOutQA question cites a deleted Wikipedia article"
            )
    return legacy.DatasetBundle(documents, questions, "combined")


def prepare(datastore: Path, args: SimpleNamespace) -> legacy.DatasetBundle:
    # load_fanout_revision_text writes each revision atomically, so a stopped
    # run resumes at the first revision absent from the dataset cache.
    return prepare_dataset(NAME, datastore, args, load)

