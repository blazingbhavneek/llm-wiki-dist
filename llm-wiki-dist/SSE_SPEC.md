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
  "max_levels": 4,
  "max_queries_per_level": 4,
  "max_recovery_levels": 2,
  "search_limit": 8,
  "max_context_chars": 14000,
  "min_search_results": 3,
  "overrides": null
}
```

| Field | Default | Allowed | Meaning |
|---|---:|---:|---|
| `question` | required | 1–20,000 characters | The complete user request. |
| `max_levels` | `4` | 1–6 | Maximum number of initially planned dependency levels. |
| `max_queries_per_level` | `4` | 1–6 | Maximum shallow queries that may run in parallel inside one level. |
| `max_recovery_levels` | `2` | 0–3 | Additional levels that may be inserted when evidence is insufficient. |
| `search_limit` | `8` | 2–16 | Maximum ranked evidence nodes given to one shallow answer call. |
| `max_context_chars` | `14000` | 2,000–40,000 | Maximum retrieved context given to one shallow answer call. |
| `min_search_results` | `3` | 1–8 | Results below this count trigger one broader search automatically. |
| `overrides` | `null` | existing `/api/ask` overrides | Optional chat-model/API settings for this request. |

Invalid request bodies receive an HTTP `422` response before streaming starts.

## Processing model

1. One fast LLM call creates the complete initial level plan.
2. The server emits that plan before emitting any answer level.
3. All shallow queries inside the current level run concurrently.
4. Each query performs hybrid keyword/vector retrieval and reranking.
5. Sparse retrieval automatically performs one broader search.
6. One LLM call per shallow query produces short referenced facts. Calls for
   the same level run concurrently.
7. Facts with missing or non-retrieved node IDs are removed by the backend.
8. The completed level is placed on the SSE queue immediately.
9. The server starts the next level immediately; it does not wait for the
   client to finish speaking the previous level.
10. If evidence is insufficient, a recovery level is inserted and a
    `plan_update` event announces the new order.

Levels are sequential because later levels may depend on earlier facts. Work
inside one level is parallel. This lets the client speak Level 1 while the
server researches Level 2.

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

Adaptive recovery order:

```text
run
plan
level_start                 (planned level is processed)
plan_update                 (recovery is inserted)
level                       (supported facts from the planned level)
level_start                 (inserted recovery level starts)
level
...
done
```

`run` is transport metadata. `plan` is always the first semantic pipeline
event, and no `level` event can precede it.

Already emitted levels are immutable. A `plan_update` only changes future work.
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

Sent whenever adaptive processing changes the remaining order. The event
contains the complete current plan, not a partial patch.

```json
{
  "type": "plan_update",
  "version": 2,
  "reason": "insufficient_evidence",
  "inserted_level_id": "recovery_1",
  "after_level_id": "level_1",
  "levels": [
    {
      "id": "level_1",
      "position": 1,
      "objective": "Define X",
      "queries": ["What exactly is X?"],
      "depends_on": [],
      "kind": "planned",
      "recovery_for": null,
      "status": "partial"
    },
    {
      "id": "recovery_1",
      "position": 2,
      "objective": "Find missing evidence for Define X",
      "queries": ["What configuration determines X's behavior?"],
      "depends_on": ["level_1"],
      "kind": "recovery",
      "recovery_for": "level_1",
      "status": "pending"
    },
    {
      "id": "level_2",
      "position": 3,
      "objective": "Explain why X does Y",
      "queries": ["What mechanism causes X to do Y?"],
      "depends_on": ["recovery_1"],
      "kind": "planned",
      "recovery_for": null,
      "status": "pending"
    }
  ]
}
```

Clients must use the highest `version` received. Replace the locally stored
pending plan atomically when this event arrives.

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
      "enough": true,
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
  least one backend-allowlisted node ID.
- Deduplicate speech by `level_id`. A level is emitted at most once during a
  normal connection.
- `complete=false` means the level found some supported facts but not everything
  it needed. Speak its non-empty `text`; an announced recovery level may provide
  the missing part. Do not invent a transition or missing explanation.
- `text` may be empty when no supported fact survived reference validation. Do
  not enqueue an empty string.

### `done`

Terminal success event. It summarizes all accepted facts but is not a second
answer and should not be spoken again.

```json
{
  "type": "done",
  "status": "complete",
  "plan_version": 1,
  "levels_completed": 2,
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

- `complete`: every final level completed or was resolved by recovery.
- `partial`: the recovery budget was exhausted while information was still
  missing. Previously emitted facts remain source-backed and usable.

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
- Shallow queries inside a level run concurrently.
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
