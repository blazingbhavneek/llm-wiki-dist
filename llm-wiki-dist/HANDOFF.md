# Realtime research handoff

## User goal

Replace the serial realtime RAG worker with a fast agentic fan-out/fan-in
pipeline. It must read a broad set of relevant source nodes, allow flexible
research decisions, and still emit each level inside the client's 15-second
per-stage timeout.

## Current problem

`graph/realtime.py` still has a serial worker flow in `_answer_query()`:

1. broad `search_with_evidence()` retrieval;
2. `ResearchMove` candidate-selection model call;
3. answer draft model call;
4. optional follow-up retrieval/model calls.

This can exit after a partial answer. A positive `research_seconds_per_query`
also makes serial model calls exceed the client timeout. The latest fast default
is `0`, which suppresses follow-up turns; the user rejected that tradeoff.

Observed request logs confirmed the serial path:

1. planning chat call;
2. embedding call;
3. candidate-selection chat call;
4. draft chat call;
5. follow-up embedding call;
6. final chat call.

## Required implementation

Implement a **parallel reader-agent fan-out/fan-in** inside each realtime level:

```text
broad candidate catalog
  -> coordinator agent chooses/assigns research angles
  -> 3-4 reader agents in parallel, each with a disjoint candidate group
       - may request one focused follow-up search
       - returns source-backed evidence report, not the user answer
  -> one synthesis model call over all reader reports
  -> emit level
```

Target timing: coordinator + parallel reader wave + synthesis should normally
fit below 15 seconds wall clock. Use a stage deadline around 12 seconds, not a
20-second serial loop.

## Design constraints from user

- Keep the leveled realtime structure and streaming behavior.
- Do not turn it into the full `researcher.py`/LangGraph pipeline.
- It must be an agent: the coordinator/readers choose relevant material; do not
  hardcode API names, `X±5` neighborhoods, or fixed topic logic.
- A broad candidate catalog should contain node ID, title, summary, match
  snippets, source path/range.
- Reader agents should read full selected node bodies and may use source-local
  neighbors/links when available.
- Do not stop merely because an answer model leaves `follow_up_query` blank.
- The first emitted answer should be complete enough; do not emit generic
  “provided material lacks information” filler.
- Client has a 15-second timeout per stage. `app.py` now sends SSE pings every
  2 seconds, but model work still must finish quickly.

## Known source correctness examples

For `mpf_mfs_open` the source documents explicitly state:

```c
int mpf_mfs_open(MPF_MFS_FCB *fcb, char *cpuname,
                 int filenum, int sbnum, ssize_t bufsize, int opentype)
```

Therefore the third argument is `filenum`, not `sbnum`.

For processing-request use, the source explicitly requires:

- `pmf_prg.txt`
- `pmf_procdata.txt`
- `mpf_mfs_cyclicfile.txt` registration/creation

The current realtime answers have incorrectly omitted the second filename and
demoted the third item to a conditional aside.

## Relevant files

- `graph/realtime.py`: current realtime pipeline; contains `ResearchMove`,
  candidate catalog formatting, and serial `_answer_query()` logic.
- `graph/researcher.py`: useful reference for candidate previews, tool agents,
  `read`, `search`, and subagent concurrency. Do not reuse its full slow graph.
- `graph/store.py`: source chunks have `start_char`/`end_char`; nodes expose
  `source_path` and `source_ranges`, useful for a future source-neighbor tool.
- `app.py`: realtime request options and SSE transport. Keep 2-second ping.
- `tests/test_realtime.py`: current tests pass but do not yet test parallel
  reader-agent fan-out.

## Current local changes

Modified: `graph/realtime.py`, `app.py`, `SSE_SPEC.md`,
`tests/test_realtime.py`.

Last verification before this handoff:

```bash
PYTHONPATH=. python -m unittest tests/test_realtime.py
```

Result: 8 tests passed.
