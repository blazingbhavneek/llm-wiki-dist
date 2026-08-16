# Realtime Speaking RAG SSE Protocol

This document defines the client contract for the level-wise realtime RAG
endpoint. It is intended for the speaking-agent service that receives verified
text from this application and places that text into a speech/TTS buffer.

## Endpoint

Application route:

```http
POST /api/ask/realtime/stream
```

In the normal multi-database deployment, the public URL is:

```text
{WIKI_PREFIX}/{database}/api/ask/realtime/stream
```

The default prefix is `/llm-wiki`, so an example URL is:

```text
/llm-wiki/wiki/api/ask/realtime/stream
```

This is a `POST` streaming endpoint. Browser `EventSource` cannot be used
directly because `EventSource` only performs `GET` requests. Browser clients
should use `fetch()` and read `response.body` as an SSE stream.

Request headers:

```http
Content-Type: application/json
Accept: text/event-stream
```

## Request

Minimal request:

```json
{
  "question": "What is X and why does it do Y?"
}
```

Full request:

```json
{
  "question": "What is X and why does it do Y?",
  "max_levels": 3,
  "shard_count": 4,
  "shard_detail_nodes": 5,
  "shard_wide_nodes": 10,
  "shard_deadline_seconds": 5,
  "neighbor_min_admit": 2,
  "neighbor_max_admit": 8,
  "neighbor_hops_fast": 1,
  "neighbor_hops_deep": 3,
  "neighbor_hops_typed": 1,
  "search_limit": 100,
  "rerank_top_k": 30,
  "max_context_chars": 32000,
  "subagent_count": 3,
  "subagent_concurrency": 3,
  "subagent_max_steps": 5,
  "subagent_min_reads": 1,
  "subagent_max_reads": 4,
  "deep_deadline_seconds": 9,
  "deep_node_limit": 24,
  "anticipation_deadline_seconds": 8,
  "anticipation_terms": 3,
  "emit_discovery": true,
  "deadline_seconds": 90,
  "overrides": null
}
```

Every field is optional; the defaults are the shipped configuration.

| Field | Default | Allowed | Meaning |
|---|---:|---:|---|
| `question` | required | 1–20,000 characters | The complete user request. |
| `max_levels` | `3` | 1–3 | `1` = fast answer only, `2` adds deep research over the structural subgraph, `3` adds the anticipation stage. |
| `deadline_seconds` | `90` | 10–300 | Wall-clock budget for the run. Past it no new level starts and the run ends `partial`. |
| `overrides` | `null` | existing `/api/ask` overrides | Optional chat-model/API settings for this request. |

Fast answer (level 1):

| Field | Default | Allowed | Meaning |
|---|---:|---:|---|
| `shard_count` | `4` | 1–8 | Parallel readers over the reranked set. The first two read their slice in full; the rest read a wide, shallow view. Drop to `2` if the model endpoint cannot serve four generations at once. |
| `shard_detail_nodes` | `5` | 1–20 | Sources per full-body shard. |
| `shard_wide_nodes` | `10` | 1–40 | Sources per wide shard. |
| `shard_deadline_seconds` | `5` | 1–30 | Harvest point. Whatever finished is emitted; a late shard shortens the answer rather than delaying it. |
| `max_context_chars` | `32,000` | 0–120,000 | Prompt character cap per shard family. `0` permits an intentionally exhaustive, slower prompt. |

Retrieval:

| Field | Default | Allowed | Meaning |
|---|---:|---:|---|
| `search_limit` | `100` | 2–300 | Width of the single hybrid net cast per question. |
| `rerank_top_k` | `30` | 1–100 | Working set after cross-encoder reranking. |

Structural exploring (speed ↔ accuracy):

| Field | Default | Allowed | Meaning |
|---|---:|---:|---|
| `neighbor_min_admit` | `2` | 0–20 | Neighbours always offered to a shard, so the fast answer sees some structure even at the fastest setting. |
| `neighbor_max_admit` | `8` | 0–40 | Ceiling on neighbours per shard and per subgraph seed. |
| `neighbor_hops_fast` | `1` | 0–3 | Hops walked for the shard neighbours. |
| `neighbor_hops_deep` | `3` | 0–6 | Hops walked along the document chain for the deep subgraph. Chunks of one page are chained, and this is how the rest of a list is found. |
| `neighbor_hops_typed` | `1` | 0–3 | Hops walked along non-chain edge labels. |

Deep stage (level 2) and anticipation (level 3):

| Field | Default | Allowed | Meaning |
|---|---:|---:|---|
| `subagent_count` | `3` | 0–6 | Research agents started over the subgraph. `0` skips agent research. |
| `subagent_concurrency` | `3` | 1–6 | How many run at once. |
| `subagent_max_steps` | `5` | 1–20 | Per-agent step budget. Realtime-sized on purpose. |
| `subagent_min_reads` | `1` | 0–10 | Reads required before an agent may answer. A higher gate makes the stage miss its deadline. |
| `subagent_max_reads` | `4` | 1–20 | Per-agent read budget. |
| `deep_deadline_seconds` | `9` | 1–60 | Harvest point for level 2. |
| `deep_node_limit` | `24` | 1–120 | Subgraph nodes considered by the stage. |
| `anticipation_deadline_seconds` | `8` | 1–60 | Harvest point for level 3. |
| `anticipation_terms` | `3` | 0–6 | Terms looked up that the earlier answer used but never explained. |
| `emit_discovery` | `true` | boolean | Whether `discovery` events are sent. |

Accepted but no longer meaningful, kept so older clients do not break:
`max_queries_per_level`, `max_recovery_levels`, `min_search_results`,
`research_seconds_per_query`, `min_initial_read_nodes`.

Invalid request bodies receive an HTTP `422` response before streaming starts.
A blank or whitespace-only `question` is also rejected with `422`.

## Processing model

The three levels are fixed. There is no planning model call: what the server
says it will look up comes from retrieval, which is why `plan` arrives in about
half a second.

1. **Repair the words.** Speech recognition does not know the corpus. The
   question is matched against corpus vocabulary — cluster names, node
   keywords, and identifiers harvested from titles, claims and bodies — so
   「エムピーエフ エムエフエス オープン」 reaches retrieval as `mpf_mfs_open`.
   The original wording is never removed, only augmented.
2. **One wide search.** Hybrid keyword/vector retrieval over `search_limit`
   candidates. The RRF field weights are leaned by question shape: a question
   naming an exact thing weights the lexical and claim channels, a concept
   question weights summaries and titles, a list question widens the item pool.
   Weights, never filters: a filter discards material the answer may need.
3. **Rerank** the net down to `rerank_top_k` with the cross-encoder. If the
   reranker is unavailable the RRF order is used unchanged.
4. **Emit `plan`** — structured data, not a sentence. It carries the objectives,
   the repaired query, the matched cluster names, the pinned identifier and the
   candidate titles. The client's speaker model turns that into speech.
5. **Level 1: the fast answer.** `shard_count` readers run in parallel over
   different slices of the reranked set: the first two read their sources in
   full, the rest read a wide, shallow view (summaries, claims, keywords,
   matched snippets). Each shard is also given the best structural neighbours
   of its own sources. At `shard_deadline_seconds` the finished shards are
   concatenated — not merged by a further model call, because the client's
   speaker model rewrites the text into speech anyway.
6. **In parallel, the structural subgraph is built.** Pure database work with
   no GPU cost: the document chain around the ranked sources for
   `neighbor_hops_deep` hops, typed edges for `neighbor_hops_typed`, and the
   rest of each source's page. It is ready before level 1 is even sent.
   No level-2 generation starts before level 1 is emitted, so deep research
   cannot delay first audio.
7. **Level 2: deep research** over that subgraph while the client speaks level
   1, harvested at `deep_deadline_seconds`. `subagent_count` research agents
   choose what to open next, and one plain reader runs beside them over the
   same subgraph, so an agent loop that overruns the harvest cannot leave the
   level empty.
8. **Level 3: anticipation.** Terms the earlier levels used but never explained
   are looked up, harvested at `anticipation_deadline_seconds`.
9. Sections with missing or non-retrieved node IDs are removed by the backend,
   and every level is queued for the client the moment it is harvested.
10. Once `deadline_seconds` passes, no further level is started. Levels already
    emitted stay valid; the run terminates with `status="partial"` and
    `incomplete_reason="deadline"`.

No stage waits for all of its branches. Fan-out finishes at the slowest branch,
so each stage takes what completed by its deadline and drops the rest: a late
shard makes the answer shorter, which is recoverable, while a late shard that
blocks the stream is not.

## SSE framing

Every JSON message uses both a named SSE event and a matching `type` field:

```text
event: level
data: {"type":"level","level_id":"level_1",...}

```

The server also sends SSE comments:

```text
: connected

: ping

```

Comments are connection/heartbeat signals and must not be added to the speech
buffer.

The response includes:

```http
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
Connection: keep-alive
X-Accel-Buffering: no
```

## Event order

Normal order:

```text
run
plan
level_start
level
level_start
discovery        (optional, only while a level is being researched)
level
...
done
```

`run` is transport metadata. `plan` is always the first semantic pipeline
event, and no `level` event can precede it.

Already emitted levels are immutable. A deadline `plan_update` only changes future work.
The client must replace its pending plan with the newest version and must not
replay or remove text that has already been spoken.

## Events

### `run`

Sent immediately so the client can cancel the request even while planning is
still running.

```json
{
  "type": "run",
  "run_id": "31cb54f2-bcf3-439d-9ad4-e7737e988908"
}
```

Store `run_id` until the stream terminates.

### `plan`

Emitted from retrieval alone, with no model call, at roughly 0.5 s. It is
**structured data, not a sentence**: it names what is about to be looked up so
the client's speaker model can phrase that in its own words.

```json
{
  "type": "plan",
  "version": 1,
  "question": "エムピーエフ エムエフエス オープン の第3引数は？",
  "planning_fallback": false,
  "search_query": "エムピーエフ エムエフエス オープン の第3引数は？ mpf_mfs_open",
  "question_type": "named",
  "pinned_identifier": "mpf_mfs_open",
  "vocabulary": {
    "query": "エムピーエフ エムエフエス オープン の第3引数は？ mpf_mfs_open",
    "identifiers": ["mpf_mfs_open"],
    "clusters": ["MFSファイル管理"],
    "keywords": [],
    "repairs": [
      {
        "heard": "エムピーエフ エムエフエス オープン",
        "matched": "mpf_mfs_open",
        "kind": "identifier",
        "score": 0.9
      }
    ]
  },
  "candidates": [
    {"node_id": "node:14", "title": "mpf_mfs_open", "summary": "ファイルを開く"}
  ],
  "levels": [
    {
      "id": "level_1",
      "position": 1,
      "objective": "mpf_mfs_open を直接答える",
      "queries": ["..."],
      "depends_on": [],
      "kind": "fast",
      "recovery_for": null,
      "status": "pending"
    },
    {
      "id": "level_2",
      "position": 2,
      "objective": "資料の続きと関連ノードを読み、詳細を補う",
      "queries": ["..."],
      "depends_on": ["level_1"],
      "kind": "deep",
      "recovery_for": null,
      "status": "pending"
    },
    {
      "id": "level_3",
      "position": 3,
      "objective": "回答で触れた用語を先回りして調べる",
      "queries": ["..."],
      "depends_on": ["level_2"],
      "kind": "anticipation",
      "recovery_for": null,
      "status": "pending"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `search_query` | What was actually searched, after vocabulary repair. |
| `question_type` | `named`, `concept` or `list`. Selects the retrieval weight profile. |
| `pinned_identifier` | The corpus identifier the answer is scoped to, or `""`. |
| `vocabulary.repairs` | Each mishearing the server corrected: what it heard, what the corpus calls it, and the match score. |
| `candidates` | Highest-ranked node IDs with titles, for anticipating what is coming. |
| `levels[].kind` | `fast`, `deep` or `anticipation`. |

`planning_fallback` is always `false` and is retained for compatibility: this
path no longer has a planning model call that could fail.

The plan itself should not be spoken verbatim.

### `plan_update`

Sent only when the deadline causes pending levels to become `skipped`. The
event contains the complete current plan, not a partial patch. Clients must use
the highest `version` received and replace their locally stored pending plan.

Level `status` values in `plan` and `plan_update`: `pending`, `running`,
`complete`, and `skipped` (never started, only after the deadline passed). A
level's status is a snapshot at emit time; the `level` event is the authoritative
result for that level.

### `level_start`

Signals that the server has started processing one level. This is operational
metadata and should not be spoken.

```json
{
  "type": "level_start",
  "plan_version": 1,
  "level_id": "level_1",
  "position": 1,
  "objective": "Define X",
  "queries": ["What exactly is X?"]
}
```

### `level`

Contains the newly completed, immediately speakable output for one level.

```json
{
  "type": "level",
  "plan_version": 1,
  "level_id": "level_1",
  "position": 1,
  "objective": "Define X",
  "queries": [
    {
      "query": "What exactly is X?",
      "answered": true,
      "reference_node_ids": ["node:14"],
      "search_result_count": 8,
      "latency_ms": 184,
      "error": null
    }
  ],
  "text": "X is the component that coordinates upload retries.",
  "facts": [
    {
      "text": "X is the component that coordinates upload retries.",
      "node_ids": ["node:14"]
    }
  ],
  "reference_node_ids": ["node:14"],
  "complete": true,
  "latency_ms": 187
}
```

Client behavior:

- Append non-empty `text` to the TTS/speech buffer immediately.
- Do not wait for `done`.
- Do not speak `reference_node_ids`; store them with the spoken segment.
- Treat `facts` as the source map for the segment. Every accepted fact has at
  least one backend-allowlisted node ID. Node IDs are stripped from spoken
  `text`, so a TTS client never reads a reference out loud.
- `queries[].error` is a single-line diagnostic truncated to 200 characters, or
  `null`. It marks one failed subquery, not a failed run.
- Deduplicate speech by `level_id`. A level is emitted at most once during a
  normal connection.
- `complete` is true for every level that ran. Individual workers report
  `answered=false` when no sourced section survived validation; this is a
  diagnostic, not spoken content or a request for recovery.
- `text` may be empty when no supported fact survived reference validation. Do
  not enqueue an empty string.
- `queries` holds one entry per parallel branch of that level — one per shard
  in level 1, one per agent or reader in level 2, one per looked-up term in
  level 3. A branch that did not finish before the level's deadline is absent,
  which is normal and not an error.
- `kind` repeats the plan's `fast` / `deep` / `anticipation` label.

### `discovery`

Optional. Sent while a level is being researched, to say what is being read
right now. A client that does not render progress may ignore it entirely.

```json
{
  "type": "discovery",
  "level_id": "level_2",
  "text": "mpf_mfs_cyclicfile の登録手順を読んでいます",
  "node_ids": ["node:412"],
  "speakable": true
}
```

A `discovery` is emitted only when it carries a node ID this run has not
mentioned yet, which keeps it real information rather than a progress bar. It
is never a substitute for a `level`: nothing in it is a verified answer, and
the facts from the node it names arrive later in the level event. Set
`emit_discovery: false` to turn these off.

### `done`

Terminal success event. It summarizes all accepted facts but is not a second
answer and should not be spoken again.

```json
{
  "type": "done",
  "status": "complete",
  "incomplete_reason": null,
  "plan_version": 1,
  "levels_completed": 2,
  "levels_planned": 2,
  "facts": [
    {
      "text": "X is the component that coordinates upload retries.",
      "node_ids": ["node:14"]
    },
    {
      "text": "It uses backoff to prevent repeated immediate failures.",
      "node_ids": ["node:14", "node:22"]
    }
  ],
  "reference_node_ids": ["node:14", "node:22"],
  "latency_ms": 496
}
```

`status` values:

- `complete`: every planned level completed.
- `partial`: the deadline passed before every planned level ran. Previously
  emitted facts remain source-backed and usable.

`incomplete_reason` explains a `partial` run and is `null` when `status` is
`complete`:

- `deadline`: `deadline_seconds` passed before every planned level ran. Levels
  never started are reported with `status: "skipped"` in the last plan the
  client received; `levels_completed` is lower than `levels_planned`.

After `done`, allow the existing TTS buffer to finish and close the stream.

### `error`

Terminal pipeline failure after the HTTP stream has started.

```json
{
  "type": "error",
  "message": "connection to model server failed",
  "retryable": true,
  "code": "realtime_failed"
}
```

Do not discard text that was already spoken. Stop waiting for new levels and
apply the speaking agent's retry/fallback policy.

### `cancelled`

Terminal acknowledgement that the run was stopped.

```json
{
  "type": "cancelled",
  "run_id": "31cb54f2-bcf3-439d-9ad4-e7737e988908"
}
```

Stop accepting new speech segments for this run. Already buffered audio may be
discarded or allowed to finish according to the speaking agent's interruption
policy.

## Cancellation

Use the `run_id` from the `run` event:

```http
POST {WIKI_PREFIX}/{database}/api/agent-runs/{run_id}/stop
```

No request body is required.

Successful response:

```json
{
  "run_id": "31cb54f2-bcf3-439d-9ad4-e7737e988908",
  "status": "stopping"
}
```

Closing the streaming connection also signals cancellation, but the explicit
stop endpoint is preferred when the user interrupts speech because it reaches
the server immediately.

## Browser client example

```js
async function streamRealtimeAnswer({ baseUrl, database, question, onEvent, signal }) {
  const response = await fetch(
    `${baseUrl}/llm-wiki/${encodeURIComponent(database)}/api/ask/realtime/stream`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({ question }),
      signal,
    },
  );

  if (!response.ok) {
    throw new Error(`Realtime request failed: HTTP ${response.status}`);
  }
  if (!response.body) {
    throw new Error("Streaming response body is unavailable");
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    buffer = buffer.replaceAll("\r\n", "\n");

    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);

      if (!frame || frame.startsWith(":")) continue;

      let eventName = "message";
      const dataLines = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
      }
      if (!dataLines.length) continue;

      const event = JSON.parse(dataLines.join("\n"));
      if (event.type !== eventName) {
        throw new Error(`SSE event mismatch: ${eventName} != ${event.type}`);
      }
      onEvent(event);
    }
  }
}
```

Suggested speaking-agent handler:

```js
const spokenLevels = new Set();
let runId = null;
let planVersion = 0;
let pendingPlan = [];

function onEvent(event) {
  switch (event.type) {
    case "run":
      runId = event.run_id;
      break;

    case "plan":
    case "plan_update":
      if (event.version >= planVersion) {
        planVersion = event.version;
        pendingPlan = event.levels;
        speaker.prepare(pendingPlan); // Do not speak the plan.
      }
      break;

    case "level":
      if (!spokenLevels.has(event.level_id) && event.text) {
        spokenLevels.add(event.level_id);
        speechBuffer.enqueue({
          text: event.text,
          levelId: event.level_id,
          facts: event.facts,
          referenceNodeIds: event.reference_node_ids,
        });
      }
      break;

    case "discovery":
      // Optional: what is being read right now. Never an answer, and safe to
      // ignore entirely.
      speaker.mentionProgress(event.text);
      break;

    case "done":
      speechBuffer.finishAfterDrain();
      break;

    case "error":
    case "cancelled":
      speechBuffer.stopAccepting();
      break;
  }
}
```

## Complete abbreviated stream

```text
: connected

event: run
data: {"type":"run","run_id":"31cb54f2-bcf3-439d-9ad4-e7737e988908"}

event: plan
data: {"type":"plan","version":1,"question":"What is X and why does it do Y?","planning_fallback":false,"question_type":"named","pinned_identifier":"X","candidates":[...],"levels":[...]}

event: level_start
data: {"type":"level_start","plan_version":1,"level_id":"level_1","position":1,"objective":"Define X","queries":["What exactly is X?"]}

event: level
data: {"type":"level","plan_version":1,"level_id":"level_1","text":"X is the retry controller.","facts":[{"text":"X is the retry controller.","node_ids":["node:14"]}],"reference_node_ids":["node:14"],"complete":true,...}

: ping

event: level_start
data: {"type":"level_start","plan_version":1,"level_id":"level_2",...}

event: level
data: {"type":"level","plan_version":1,"level_id":"level_2","text":"It uses backoff to prevent repeated immediate failures.","facts":[{"text":"It uses backoff to prevent repeated immediate failures.","node_ids":["node:14","node:22"]}],"reference_node_ids":["node:14","node:22"],"complete":true,...}

event: done
data: {"type":"done","status":"complete","plan_version":1,"levels_completed":2,...}

```

## Guarantees and limitations

- The full initial plan is emitted before answer levels.
- Plan changes are versioned and emitted before the changed future level starts.
- A run always terminates: with `done` inside `deadline_seconds` plus the
  duration of the calls already in flight, or with `error`/`cancelled`.
- A run that could not support every part of the question always terminates
  with `status="partial"`; unsupported parts are omitted, never filled in.
- Every stage harvests at its own deadline and emits what finished. Branches
  that did not finish are dropped, never waited for.
- Generations inside one level run in parallel: the model endpoint serves
  concurrent generations, which is what makes a four-shard fast answer cost
  roughly one generation of wall clock. (An earlier revision of this document
  claimed the endpoint serializes generations. It does not — the documentation
  path has run three agents concurrently against it in production.)
- No generation belonging to a later level starts before the current level is
  emitted, so deep research cannot delay first audio. Database work does
  overlap, because it uses no GPU.
- The next level starts without waiting for speech playback.
- Every streamed fact has at least one node ID from retrieved evidence or an
  already accepted earlier-level fact.
- Node-ID allowlisting prevents invented references from reaching the stream.
- A node ID proves provenance, not logical entailment. A generative model can
  still describe a real source incorrectly; clients should not treat references
  as a mathematical guarantee of correctness.
- The protocol currently provides no `id:` field, `Last-Event-ID` replay, or
  resume support. Reconnecting starts a new run and may duplicate speech unless
  the client deduplicates at the conversation layer.
