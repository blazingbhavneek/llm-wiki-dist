# Realtime RAG — v2 Design

For `graph/realtime.py` and its callers. Replaces the current one-level fast path.

The client side is designed separately in `llm-wiki-realtime/plan.md`. This
document is the server.

---

## 1. What we are building

**A smart coworker with a big book.**

He does not know the answer by heart. He knows the book. When you ask him
something he tells you what he is about to look up, gives you a real answer
fast, and then keeps reading while he talks — because he already knows what you
will ask next.

Rules that come from that image:

| Behavior | What it means here |
|---|---|
| Says what he is about to look up | `plan` event goes out in under a second |
| Answers fast, not perfectly | First answer in ~5s, from a wide net |
| Keeps reading while talking | Deeper research runs while the client speaks |
| Anticipates the next question | Next stage looks up what the first answer mentioned |
| Never says "not in my notes" | No "the material does not state…" ever reaches speech |
| Reads the rest of the page | Missing list items come from document neighbours |

Speed and parallelism everywhere. The client speaks for 6+ seconds per answer —
that is free server time, and we use all of it.

---

## 2. The whole flow

```
USER SPEAKS  (frontend: click orb to start, click again to stop)
     |
     v
[1] FIX THE WORDS                                        no LLM, ~1ms
     ASR may mishear. Frontend does not know the book.
     Match the question against corpus vocabulary:
       - cluster names
       - keywords from every node
       - identifiers (mpf_*, MPF_*, *.txt)
     「エムピーエフ エムエフエス オープン」 -> mpf_mfs_open
     |
     v
[2] ONE WIDE SEARCH                                      ~300ms
     hybrid, all fields, top 100
     RRF weights change based on the question type
     |
     v
[3] RERANK 100 -> 30                                     ~200ms
     ruri-v3-310m, already running on port 51025
     |
     v
[4] SEND `plan`                                          t ~= 0.5s
     "Here is what I will look into."
     Structured data, NOT a sentence.
     The frontend speaker LLM turns it into speech.
     |
     +===================== SPLIT =====================+
     |                                                 |
     v                                                 v
[5] FAST ANSWER                              [6] SUBGRAPH BUILD
    4 LLM calls in parallel                      DB only, no GPU
    (see section 3)                              runs at the same time
    |                                            as [5]
    v                                            (see section 4)
   send `level` 1   t ~= 5s                              |
   CLIENT STARTS SPEAKING                                |
     |                                                   |
     +====================== JOIN ======================+
     |
     v
[7] DEEP RESEARCH                                        t ~= 5s -> 14s
     GPU is free now, client is speaking
     3 subagents in parallel over the subgraph
     harvest whatever is done at the deadline
     |
     v
   send `level` 2
     |
     v
[8] ANTICIPATION                                         t ~= 14s -> 22s
     Look up what level 1 mentioned but did not explain.
     "3rd argument is filenum"  ->  now go find what filenum is.
     |
     v
   send `level` 3
     |
     v
   send `done`
```

**First audio at ~5 seconds. Everything after that is free.**

---

## 3. The fast answer — 4 parallel calls

This is the most important part. Do not make it one LLM call.

```
                 reranked top 30 nodes
                          |
     +--------------+-----+-----+--------------+
     |              |           |              |
    S1             S2          S3             S4
  nodes 1-5     nodes 6-10  nodes 11-20   nodes 21-30
  full bodies   full bodies   summaries     summaries
      +             +         + claims      + claims
  1-hop         1-hop        + all fields  + all fields
  neighbours    neighbours   + neighbours  + neighbours
  (best ones)   (best ones)
     |              |           |              |
   DETAIL        DETAIL       WIDE           WIDE
     |              |           |              |
     +--------------+-----+-----+--------------+
                          |
                    CONCATENATE
                (do NOT rewrite, do NOT compile)
                          |
                          v
                     `level` 1 text
```

**Why 4 calls and not 1:** one call over everything is semi-blind. It skims. Four
calls each read their own slice properly. Two go deep on the top 10, two go wide
over the rest.

**Why concatenate and not compile:** the frontend speaker LLM rewrites it into
speech anyway. Compiling here costs another generation and adds nothing.

It also means a slow shard is harmless. If S4 is late, the answer is just
shorter. Nothing breaks. Harvest at 5 seconds and send what came back.

### Neighbours in the shards

`researcher.py` already has the format (`_sub_neighbors`, line ~761):

```
- [label] node_id | title | summary
```

Add the best 1-hop neighbours to each shard this way. The model gets the summary
and the node ID, so it knows more, and it can cite them for follow-ups. How many
neighbours get in is controlled by min/max knobs — see section 6.

### The answer must match what was actually asked

This is a hard requirement, not a hint:

| Asked | Answer must be |
|---|---|
| All arguments of a function | All arguments **of that function only** |
| The 3rd argument | That function only, that argument |
| Which files do I need | As many items of that list as possible |
| What is X | X |

Two mechanisms:

1. **RRF weight profiles** (section 5) — lean the search lexical when the
   question is lexical.
2. **Prompt scope** — every shard prompt names the pinned identifier.

Do not hard-filter results. Change the weights instead. Filtering throws away
things we might need.

**Never hallucinate.** Existing guard stays: any node ID the model cites that was
not retrieved gets dropped (`_resolve_ids`).

---

## 4. The subgraph — built while the fast answer generates

Pure database work. No GPU. Runs at the same time as section 3, so it costs
nothing.

```
seeds = top ranked nodes from [3]
   |
   +-- prev/next direction:  go 3 hops
   |     chunks of a document are chained.
   |     this is how we find the rest of a list.
   |
   +-- other edge labels:    go 1 hop
   |
   +-- same document:        get_nodes_by_document + source_ranges
   |
   v
subgraph, ready before level 1 is even sent
   |
   v
3 subagents start on it as soon as level 1 goes out
```

**Why prev/next gets more hops:** `pmf_prg.txt` and `pmf_procdata.txt` are on the
same page. Ranking put one at #3 and the other at #19. Reading the rest of the
page finds it. Meaning-based search never will.

### Offline part

Precompute a `node_neighborhood` table during enrichment (the librarian already
has a job queue for this):

```
node_id -> { prev_next: [3 hops each way]
             typed:     [1 hop, other labels]
             siblings:  [same source_path, nearby source_ranges] }
```

Query time becomes one indexed lookup instead of walking the graph. Costs
nothing at request time.

---

## 5. RRF weight profiles

The weights already exist in `core.py`. `ask_realtime` already does
`settings.model_copy(update={...})`. So this is a dict, not new code.

Current defaults:

```
item_bm25       1.35     title_vec       1.30
claim_vec       1.25     small_chunk_vec 1.15
summary_vec     1.00     big_chunk_vec   0.95
node_bm25       0.90     body_vec        0.75
```

Profiles:

| Question type | Push up | Push down |
|---|---|---|
| Named thing ("3rd arg of `mpf_mfs_open`") | `node_bm25`, `item_bm25`, `claim_vec` | `body_vec`, `summary_vec` |
| Concept ("what is X for") | `summary_vec`, `title_vec` | `item_bm25` |
| List ("which files") | `item_bm25`, `claim_vec`, widen `pool_item_bm25` | — |

Keywords and claims carry the most weight when the question names something
exact.

---

## 6. Knobs — everything configurable per request

Like `subagent_count` in `researcher.py`, the client sends these and can
experiment during a demo.

```
# fast answer
shard_count               4      how many parallel calls
shard_detail_nodes        5      nodes per DETAIL shard
shard_wide_nodes          10     nodes per WIDE shard
shard_deadline_seconds    5      harvest and send at this point

# how much exploring is allowed  (speed <-> accuracy dial)
neighbor_min_admit        2      always let some neighbours in
neighbor_max_admit        8      ceiling
neighbor_hops_fast        1
neighbor_hops_deep        3      prev/next direction
neighbor_hops_typed       1      other edges

# retrieval
search_limit              100
rerank_top_k              30

# deep stage
subagent_count            3
subagent_concurrency      3
subagent_max_steps        5      NOT 20 — that is the slow path's number
subagent_min_reads        1      NOT 5  — that gate blocks a fast answer
subagent_max_reads        4      NOT 10
```

`neighbor_min_admit` is the important one. It guarantees the fast answer always
sees some structure beyond flat ranking, even at the fastest setting.

---

## 7. Models

No listwise reranker. Normal rerankers only.

| Job | Model | Where | Status |
|---|---|---|---|
| Rerank 100 -> 30 | `cl-nagoya/ruri-v3-reranker-310m` | port 51025 | already running |
| Generation | `gemma-4-31B` | port 51029 | already running |
| Neighbour scoring (optional) | `Qwen3-Reranker-0.6B` | new | Apache-2.0, later |

ruri-v3 is the best Japanese reranker available (86.9 nDCG@10 on JQaRA vs 77.1
previous best). It stays as the workhorse.

The optional third model is instruction-aware — you can tell it *what kind* of
passage you want, not just "score this". Useful for picking neighbours and for
the anticipation stage. **Not required for v1.** Use ruri for neighbour scoring
first and add this only if neighbour quality is bad.

---

## 8. SSE events

Same events as today. The client does not have to change to get the speed win.

| Event | Change |
|---|---|
| `run` | none |
| `plan` | now sent in ~0.5s, no LLM call, always lists 3 levels |
| `level_start` | none |
| `level` | none — 4 shards become 4 entries in `facts[]` |
| `plan_update` | only for deadline / skipped levels |
| `done` | none |

`plan` is **data, not a sentence**. The frontend speaker LLM paraphrases it.
Send objectives, matched cluster names, the pinned identifier, and candidate
titles. Do not write speakable Japanese here.

### New optional event: `discovery`

Foundation for "reading while speaking". The client can ignore it today.

```json
{ "type": "discovery",
  "level_id": "level_2",
  "text": "mpf_mfs_cyclicfile の登録手順を読んでいます",
  "node_ids": ["node:412"],
  "speakable": true }
```

**Rule:** only send one when it carries a node ID not seen yet in this run. That
keeps it real information instead of a progress bar.

Frontend work for this comes later.

---

## 9. Client timing limits

From `llm-wiki-realtime/plan.md`. Do not break these.

| Limit | Value | Our margin |
|---|---|---|
| `plan` must arrive | 5s | we send at ~0.5s |
| Gap between `level` events | 20s | ours are ~5s / ~9s / ~8s |
| Retries before giving up | 1 | — |

---

## 10. What to change in the code

| File | Change |
|---|---|
| `researcher.py:1965` | **Remove the clamp.** It cuts `evidence_rerank_pool` to 40 and `evidence_max_per_node` to 2. This is what is throttling retrieval today. |
| `researcher.py:1980` | Chat timeout is 12s. Keep. |
| `realtime.py:_plan` | Delete the hardcoded 「詳細・記述項目・設定パラメータ」 level 2. Replace with the anticipation stage. |
| `realtime.py:_run_level` | No deadline check between queries today. Add harvest-at-deadline. |
| `realtime.py:_answer_query` | Replace with the 4-shard fan-out. |
| `realtime.py` | Add subgraph build, run parallel to shards. |
| `gateway.py:787` | Reranker HTTP timeout is 120s. Way too long for realtime. |
| `store.py` | New `node_neighborhood` table + librarian job. |
| `app.py:RealtimeAskBody` | Add the section 6 knobs. |

---

## 11. Build order

Each step leaves the system working.

1. **Remove the retrieval clamp**, raise `rerank_top_k` to 30. Measure. This
   alone should improve answers with no new code.
2. **Vocabulary sheet + word fixing.** Pure data, testable alone, no LLM.
3. **`plan` without an LLM call.** Immediately kills the 5s watchdog risk.
4. **4-shard fast answer** with harvest-at-deadline. Biggest quality change.
5. **`node_neighborhood` table + librarian job.** Offline, no request impact.
6. **Subgraph build**, running parallel to shards.
7. **Deep subagents** with realtime budgets.
8. **Anticipation stage.**
9. **`discovery` events.**

Steps 1–4 are the demo. 5–8 are the coworker. 9 is polish.

---

## 12. Things to watch (not blockers)

Recorded so they are not rediscovered later.

- **Fan-out shows tail latency.** 4 parallel calls finish at the slowest of 4.
  That is why every stage must harvest at its deadline and send what it has.
  Never wait for all branches.
- **Do not start deep generations before `level` 1 is sent.** They would compete
  with the fast shards for GPU and slow down first audio. DB work is fine to
  overlap — it uses no GPU.
- **Unverified:** what serves port 51029. If it is vLLM/SGLang, 4-way fan-out is
  nearly free. If it is llama.cpp with one slot, drop to 2 shards.
- **Unverified:** `realtime_slots` (`researcher.py:1862`). Peak in-flight
  generations becomes 4 per request.
- **Unverified:** how well `keywords_json` is filled in `wiki_moove`. The word
  fixing in step 2 depends on it. If sparse, harvest identifiers from titles and
  source text instead.
- **Unverified:** what the ASR emits for identifiers — katakana, romaji, or
  mixed. Decides how much folding the matcher needs.
- **Old claim, now known false:** `SSE_SPEC.md` says the model endpoint
  serializes generations. It does not. `researcher.py` runs 3 subagents in
  parallel against the same endpoint in production. Fix that line in the spec.
