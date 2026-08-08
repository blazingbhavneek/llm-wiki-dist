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
  "max_levels": 1,
  "max_queries_per_level": 1,
  "max_recovery_levels": 0,
  "search_limit": 16,
  "max_context_chars": 32000,
  "min_initial_read_nodes": 16,
  "min_search_results": 1,
  "deadline_seconds": 90,
  "research_seconds_per_query": 0,
  "overrides": null
}
```

| Field | Default | Allowed | Meaning |
|---|---:|---:|---|
| `question` | required | 1–20,000 characters | The complete user request. |
| `max_levels` | `1` | 1–6 | `1` answers the original question directly; values above `1` enable dependency planning. |
| `max_queries_per_level` | `1` | 1–6 | Maximum shallow queries in a planned level. |
| `max_recovery_levels` | `0` | 0–3 | Deprecated compatibility field; ignored. |
| `search_limit` | `16` | 2–32 | Maximum ranked evidence nodes read by one realtime worker. |
| `max_context_chars` | `32,000` | 0–60,000 | Prompt character cap shared across the selected sources. `0` permits an intentionally exhaustive, slower prompt. |
| `min_initial_read_nodes` | `16` | 1–40 | Number of ranked sources read before synthesis; the context budget is distributed across them. |
| `min_search_results` | `1` | 1–8 | Deprecated compatibility field; ignored. A broad fallback is used only for an empty focused search. |
| `deadline_seconds` | `90` | 10–300 | Wall-clock budget for the run. Past it no new level starts and the run ends `partial`. |
| `research_seconds_per_query` | `0` | 0–90 | Deprecated compatibility field; the fast path does not run an open-ended follow-up loop. |
| `overrides` | `null` | existing `/api/ask` overrides | Optional chat-model/API settings for this request. |

Invalid request bodies receive an HTTP `422` response before streaming starts.
A blank or whitespace-only `question` is also rejected with `422`.

## Processing model

1. The default creates one direct level from the original question without a planning model call. Requests allowing more than one level use the dependency planner and always contain at least two levels, even if the planner initially returns only one.
2. The server emits that plan before emitting any answer level.
3. Each query performs hybrid keyword/vector retrieval and reranking.
4. The first 16 ranked sources are read before synthesis. Their query-match snippets and a fair share of each document body are supplied, so a long overview cannot exclude a concise prerequisite source.
5. An empty focused retrieval automatically performs one broader search.
6. One synthesis call writes the source-backed section.
7. Sections with missing or non-retrieved node IDs are removed by the backend.
8. The completed section is appended and the level is placed on the
   SSE queue immediately.
9. The server starts the next level immediately; it does not wait for the
   client to finish speaking the previous level.
10. Earlier completed sections are supplied as editorial context to later
    levels, but never appended to their retrieval query.
11. Once `deadline_seconds` passes, no further level is started. Levels already
    emitted stay valid; the run terminates with `status="partial"` and
    `incomplete_reason="deadline"`.

Levels are sequential because later levels may depend on earlier facts. The
realtime model endpoint serializes generations, so planned queries are also
issued serially rather than queued behind one another. This lets the client
speak Level 1 while the server researches Level 2.

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

Contains every initially planned level and shallow query.

```json
{
  "type": "plan",
  "version": 1,
  "question": "What is X and why does it do Y?",
  "planning_fallback": false,
  "levels": [
    {
      "id": "level_1",
      "position": 1,
      "objective": "Define X",
      "queries": ["What exactly is X?"],
      "depends_on": [],
      "kind": "planned",
      "recovery_for": null,
      "status": "pending"
    },
    {
      "id": "level_2",
      "position": 2,
      "objective": "Explain why X does Y",
      "queries": ["What mechanism causes X to do Y?"],
      "depends_on": ["level_1"],
      "kind": "planned",
      "recovery_for": null,
      "status": "pending"
    }
  ]
}
```

`planning_fallback=true` means the planning call failed and the server safely
fell back to a single level containing the original question.

The speaking agent should inspect this event to anticipate what information is
coming. The plan itself should not be spoken.

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
data: {"type":"plan","version":1,"question":"What is X and why does it do Y?","planning_fallback":false,"levels":[...]}

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
- The same subquery is never issued twice, except to retry one that failed.
- Planned queries inside a level are issued serially because the configured
  model endpoint serializes generations.
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
