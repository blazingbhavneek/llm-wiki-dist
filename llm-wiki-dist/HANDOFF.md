# Realtime research handoff

State of `graph/realtime.py` and its callers after implementing `plan.md`
(*Realtime RAG — v2 Design*). The design document remains the statement of
intent; this file records what exists, what was measured, and what is still
open.

## What was built

All nine steps of the build order in `plan.md` §11 are implemented.

| Step | Where |
|---|---|
| 1. Retrieval clamp removed, working set raised to 30 | `researcher.py` `ask_realtime` |
| 2. Vocabulary sheet and word fixing | `graph/vocab.py`, `store.vocabulary_rows` |
| 3. `plan` with no model call | `realtime.py` `_retrieve` / `_plan` |
| 4. Sharded fast answer, harvest at deadline | `realtime.py` `_fast_answer`, `_harvest` |
| 5. `node_neighborhood` table and librarian job | `store.py`, `librarian.py` `refresh_neighborhood` |
| 6. Subgraph build parallel to the shards | `realtime.py` `_build_subgraph` |
| 7. Deep subagents on realtime budgets | `realtime.py` `_deep_answer`, wired in `researcher.py` |
| 8. Anticipation stage | `realtime.py` `_anticipate` |
| 9. `discovery` events | `realtime.py` `_emit_discovery` |

The pipeline takes injected ports (`search`, `rerank`, `neighbors`,
`load_nodes`, `deep_agent`, `vocabulary`) and degrades one stage at a time when
one is missing or fails, so the module is testable without a model server and a
dead reranker or an empty neighbourhood cache costs quality, never the run.

One deliberate departure from the design document: level 2 runs the plan's
research agents **and** one plain reader over the same subgraph. An agent loop
is several model turns and can miss the harvest entirely, which would make the
speaker fall silent between levels; the reader is one generation and reliably
lands inside the stage. If the agents turn out to fit their budget comfortably
against the real endpoint, the reader is one line to remove in `_deep_answer`.

## Verified during implementation

Measured against the two real corpora on this machine
(`llm-wiki/.wiki/wiki.sqlite`, 102 nodes; `wiki-backup/test.sqlite`, 971
nodes), which resolves three of the "unverified" items in `plan.md` §12:

- **`keywords_json` is fully populated** — 102/102 and 970/971 active nodes.
  The vocabulary sheet does not have to lean on body scanning, though it does
  harvest identifiers from titles, claims, code spans and bodies anyway so it
  cannot be empty exactly when enrichment has not run.
- **`follows` is the chunk chain** — 101 edges over 102 nodes, 970 over 971.
  Chain walking is keyed on that label. Other labels observed: `same-as`,
  `uses`, `precedes`, `references`, `complements`, `related-to`,
  `prerequisite-for`, `part-of`. `precedes` is semantic ordering produced by
  the edge model, not document order, so it is treated as a typed edge.
- **Vocabulary cost** — 6.8k terms built in ~160 ms for 971 nodes, and 0.3–5 ms
  per question match. It is built once at stack startup and rebuilt only when
  the corpus fingerprint changes, so no request pays for it.

Spoken-form matching on that corpus: 「エヌブイシーシー」→ `NVCC`,
「シムティー」→ `SIMT`, 「クーダ」→ `CUDA` (the last one only through the
phonetic fallback: katakana カ行 cannot spell a leading `c`). An unrelated
question pins nothing.

## Still unverified

- **What serves port 51029.** Both model endpoints were unreachable from the
  development machine, so the four-way fan-out has not been measured against
  the real server. If it turns out to be a single-slot llama.cpp, set
  `shard_count: 2` in the request; nothing else has to change.
- **Peak in-flight generations.** `realtime_slots` is `service_max_agents`
  (4). One request now issues up to `shard_count` concurrent generations, so a
  saturated server sees up to 16. If that is too many, lower
  `WIKI_SERVICE_MAX_AGENTS` or `shard_count`.
- **What ASR actually emits for identifiers.** The matcher handles katakana
  letter names, katakana loanwords, hiragana and romaji, and it folds `c`/`k`,
  `l`/`r`, `v`/`b` for anything heard in kana. Whether that covers the real
  transcriber is a question for the first live session.

## Correctness examples this path must get right

From the source documents:

```c
int mpf_mfs_open(MPF_MFS_FCB *fcb, char *cpuname,
                 int filenum, int sbnum, ssize_t bufsize, int opentype)
```

The third argument is `filenum`, not `sbnum`. The `named` weight profile plus
the pinned identifier in every shard prompt exist for this.

Processing-request use requires all three of `pmf_prg.txt`, `pmf_procdata.txt`
and `mpf_mfs_cyclicfile.txt`. Earlier answers dropped the second and demoted
the third. The chain walk exists for this: those chunks are the same page, and
`tests/test_ask_realtime.py` pins the behaviour — a node sharing no vocabulary
with the question is still reached, through `follows`, by the deep stage.

## Tests

```bash
cd llm-wiki-dist
.venv/bin/python -m unittest tests.test_realtime tests.test_vocab \
    tests.test_neighborhood tests.test_ask_realtime
```

- `test_vocab.py` — transliteration, harvesting, matching, no false pins.
- `test_neighborhood.py` — real SQLite: chain vs typed hop budgets, siblings,
  cache hit/miss, deletion cleanup.
- `test_realtime.py` — plan before generation, shard fan-out and concatenation,
  deadline harvesting, neighbour admission floor and ceiling, grounding rules,
  deep and anticipation stages, discovery rules.
- `test_ask_realtime.py` — real store and real hybrid retrieval with fake
  generation: vocabulary repair through to a grounded answer, the clamp being
  gone, realtime subagent budgets, chain discovery.

No model server is needed for any of them.

## Next

- Run it against the real endpoints and record the actual stage timings; the
  deadlines (5 s / 9 s / 8 s) are budgets, not measurements.
- `Qwen3-Reranker-0.6B` for instruction-aware neighbour scoring is still
  optional and unbuilt (`plan.md` §7). Neighbours are currently admitted by
  structural distance, chain before page before typed. Add the model only if
  neighbour quality proves to be the limiting factor.
- The frontend does not consume `discovery` yet; the event is documented in
  `SSE_SPEC.md` and safe to ignore.
