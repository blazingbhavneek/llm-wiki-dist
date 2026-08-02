# RAG Benchmarking Plan

## Goal

Benchmark this project against a conventional vector RAG baseline and Microsoft GraphRAG, prioritising answer accuracy and then ingestion speed.  The evaluation should distinguish the value of the retrieval/index architecture from the value of agentic query planning.

This report assumes the prompts and embedding model will be converted to English.

## Current system

The application is exposed through `llm-wiki-dist/app.py` and is implemented mainly in `llm-wiki-dist/graph/`.

- Ingestion uses LLM conceptual chunking, derived summaries/keywords/claims/entities, dense vectors, BM25/FTS search items, semantic edges, entity deduplication, bridge probes, and clustering.
- Retrieval is hybrid: body/summary/bridge dense retrieval plus BM25/FTS, RRF fusion, reranking, MMR, and evidence caps.
- The full `ask()` path can run a LangGraph lead agent that delegates to subagents, which may search, read, and follow links.

It is therefore not just a graph index: the end-to-end system combines a rich retrieval architecture with an agentic research workflow.

## Recommended benchmark and harnesses

### Primary benchmark: GraphRAG-Bench

Use [GraphRAG-Bench](https://github.com/GraphRAG-Bench/GraphRAG-Benchmark) as the primary English benchmark.  It directly targets graph-RAG capabilities rather than only single-hop vector retrieval, including fact retrieval and complex reasoning.  Its evaluator covers answer generation, retrieval/evidence, and indexing-quality outputs.

It provides benchmark data, an evaluator, output schemas, and example framework runners.  It is not a one-click harness that already knows this repository and every competing framework: add small adapters that emit its common result format.

Suggested adapters:

- `run_ours.py`: calls this application's benchmark-mode retrieval/answer endpoint.
- `run_ms_graphrag.py`: runs Microsoft GraphRAG query modes and normalises returned contexts/answers.
- `run_raglab.py`: optional agentic conventional-RAG baseline.

The essential per-question result shape is:

```json
{
  "id": "question-id",
  "question": "...",
  "source": "benchmark-corpus",
  "context": ["retrieved source text"],
  "evidence": ["gold evidence if supplied"],
  "question_type": "...",
  "generated_answer": "...",
  "ground_truth": "..."
}
```

Start with a stratified 100-question pilot (fact retrieval and complex-reasoning questions), verify traces and scoring, then run the full selected edition.  GraphRAG-Bench's Novel and Medical editions are domain-specific, so report which edition and split were used; do not generalise a result from one domain to all RAG use cases.

### Microsoft GraphRAG: principal graph competitor

Use [Microsoft GraphRAG](https://microsoft.github.io/graphrag/index/overview/) as the direct graph competitor.  Its standard indexing pipeline extracts entities, relationships, and claims; performs community detection/report creation; and embeds text units.

Run these query modes:

| System/mode | Role in the study |
| --- | --- |
| Microsoft `basic` | Conventional vector-RAG-style query baseline using GraphRAG text units |
| Microsoft `local` | Static knowledge-graph + text GraphRAG baseline |
| Microsoft `drift` | Native iterative graph query system; follows up from a global primer with local search |
| This system: retrieval-only mode | Static graph/hybrid retrieval comparison |
| This system: full `ask()` | Native agentic product comparison |

Microsoft's CLI supports the standard index method and `basic`, `local`, `global`, and `drift` query methods.  It also supports a no-cache option; use a fresh run/no cache whenever measuring index work.

Important qualification: Microsoft `basic` is an excellent query-quality conventional-RAG baseline, but it uses text units produced by the GraphRAG indexing stack.  It is *not* a pure vector-only ingestion-speed baseline.

### Conventional RAG harnesses

[RAGLAB](https://arxiv.org/abs/2408.11381) is the best prebuilt research framework for the optional *agentic conventional RAG* competitor.  It reproduces Naive RAG, RRR query rewriting, Iter-RetGen, Self-Ask, Active RAG, and Self-RAG behind a modular experimental framework.

For an initial comparison, use:

- **Naive RAG** for the static vector-retrieval baseline.
- **Self-Ask** *or* **Iter-RetGen** for the native agentic conventional-RAG baseline.

Avoid starting with Self-RAG: its specialised model/training-token assumptions make it a less controlled and less convenient first comparator.

For a clean, reproducible *non-agentic* vector-RAG index and ingestion baseline, use [BERGEN](https://github.com/naver/bergen).  BERGEN is an end-to-end reproducible RAG library with retriever, reranker, and generator components.  It is preferable to Microsoft `basic` for the pure-vector speed result, although it does not provide a general-purpose agent loop.

## Fairness: agentic versus static evaluations

Do **not** make only this project agentic when claiming a graph-vs-vector accuracy advantage.  That would measure both the graph architecture and extra planning/LLM calls at the same time.

Run three explicitly labelled studies instead:

| Study | Question answered | Systems |
| --- | --- | --- |
| A. Static architecture | Does graph/hybrid retrieval improve evidence and answers under one fixed retrieve-then-answer path? | BERGEN/Naive RAG, Microsoft `basic`, Microsoft `local`, this system with agent disabled |
| B. Controlled agentic architecture | With the same agent budget, which retrieval backend best supports an agent? | The same backends behind one shared controller |
| C. Native end-to-end product | Which production system is best as actually shipped? | This system `ask()`, Microsoft `drift`, RAGLAB Self-Ask or Iter-RetGen |

### Study A controls

All systems should use the same:

- answer model, English answer prompt, temperature `0`, and maximum output tokens;
- top-k, reranking policy where applicable, and retrieved-context token budget;
- corpus text and document boundaries;
- evaluation questions and scoring procedure.

For this application, expose a benchmark mode that calls the retrieval/evidence path without LangGraph delegation and returns source contexts plus a concise answer.  Disable Mermaid formatting and persisted agent notes for evaluation.

### Study B shared agent contract

Wrap every backend in the same external ReAct/LangGraph-style controller.  Give it the same model/prompt and a maximum of **three** `retrieve(query)` tool calls, followed by one final answer.  Each backend returns raw source contexts, citations/IDs, and optional scores; the controller can reformulate a query but receives the same call, context, and token budgets in every condition.

Graph traversal internal to a Microsoft/local or this-project retrieval call may remain enabled: that is part of the retrieval architecture under test.  Save the tool-call and citation trace per question.

### Study C interpretation

Native agentic systems are expected to differ in planning, graph traversal, number of calls, and token use.  This is a valid product benchmark, but label it an **end-to-end system comparison**, not evidence that the graph alone caused an accuracy difference.

There is no turnkey package that directly runs this application, Microsoft GraphRAG, and RAGLAB through one shared agent controller.  The lowest-effort accurate setup is GraphRAG-Bench's evaluator plus the thin adapters above; the shared controller is a small additional adapter layer.

## Accuracy metrics

Use GraphRAG-Bench's task-appropriate official evaluation first.  Record both answer and retrieval measures:

- answer correctness / judge score, plus exact-match or F1 where the dataset supports it;
- evidence recall and context relevance;
- citation precision/recall (whether cited sources actually support the answer);
- per-category results: fact retrieval versus complex/multi-hop reasoning;
- failure rate and abstention behaviour.

For user-owned documents, add a small manually verified set of local/global/multi-hop questions.  [BenchmarkQED](https://microsoft.github.io/benchmark-qed/) can help generate candidate questions and evaluate assertions, but generated questions/claims should be manually checked before treating them as ground truth.

## Ingestion-speed protocol

The graph-to-graph headline is this project's complete ingestion pipeline versus Microsoft GraphRAG **standard indexing**.  The vector-only headline is this project (or its static configuration) versus BERGEN/another embedding-only vector index.  Do not use Microsoft `basic` to claim pure vector-index speed.

Use identical canonical raw UTF-8 inputs and identical document boundaries for every condition.  Start the timer when raw documents are handed to the pipeline and stop only when the index is query-ready.

For this project, query-ready includes conceptual chunking, summaries/claims/entities, embedding, bridges/edges, clustering, and all queued/background enrichment.  For Microsoft GraphRAG, include its full standard index process.  Do not time only the final file write.

Report:

- wall-clock time, documents/minute, and characters/minute;
- three cold runs per condition: median, min/max (or standard deviation);
- hardware, OS, package versions, model/provider, embedding model, concurrency, and cache status;
- LLM and embedding request counts, input/output tokens, and estimated cost;
- index size, number of chunks/text units, graph nodes/edges/communities, and failures/retries.

Keep cache off and begin from fresh index/output directories for timing.  For a realistic throughput number, add a second warm/batched-concurrency run and label it separately.

## Recommended first milestone

1. Install GraphRAG-Bench and Microsoft GraphRAG in isolated environments.  Microsoft GraphRAG currently supports Python 3.10–3.12, while this project declares Python 3.13+, so do not force both into the same environment.
2. Build `run_ours.py` and `run_ms_graphrag.py`, write their output in GraphRAG-Bench's result schema, and run the 100-question static pilot.
3. Compare this system's retrieval-only mode to Microsoft `basic` and `local`; retain full traces and calculate answer/evidence metrics.
4. Add the native comparison: this system's full `ask()` versus Microsoft `drift`.
5. Add RAGLAB Self-Ask or Iter-RetGen only after the first two adapters are stable.
6. Run cold ingestion measurements: this system versus Microsoft standard index, then add BERGEN for the vector-only baseline.
7. Add the shared-agent controller if the goal is a causal agentic architecture result rather than only a product comparison.

## Sources

- [GraphRAG-Bench repository](https://github.com/GraphRAG-Bench/GraphRAG-Benchmark)
- [Microsoft GraphRAG overview](https://microsoft.github.io/graphrag/index/overview/)
- [Microsoft GraphRAG CLI reference](https://microsoft.github.io/graphrag/cli/)
- [Microsoft GraphRAG query overview](https://microsoft.github.io/graphrag/query/overview/)
- [Microsoft DRIFT Search](https://microsoft.github.io/graphrag/query/drift_search/)
- [RAGLAB paper](https://arxiv.org/abs/2408.11381)
- [BERGEN repository](https://github.com/naver/bergen)
- [BenchmarkQED](https://microsoft.github.io/benchmark-qed/)
