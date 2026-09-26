# Jev plan for `growi-search`

Status: implementation plan only. This document does not change the search
service.

The target is a per-question Jev sweep that examines every document, promotes
high-confidence pages to the existing researcher as seeds, and follows the
entity-defining edges already encoded in `00-目次` pages. Elasticsearch stays
the fast search lane and the lead/subagent `search` tool. Jev is a relevance
gate, not an answer generator.

The model reference is
[chaoliangUNSW/Jev-Style-0.8B-Decision-v3](https://huggingface.co/chaoliangUNSW/Jev-Style-0.8B-Decision-v3).
The research report describes its `JevStyleDecision` Python runtime,
`choice`/`noul` decisions, calibrated yes/no probabilities, batched
questions, a 25,600-token input limit, and local GGUF/`jev-score` operation.
The implementation must inspect the exact `jev_style_decision.py` API at
install time; do not guess an undocumented hosted Hugging Face text-generation
API.

Operational model notes from the referenced card:

- Pin the model repository revision in a real deployment; do not pull mutable
  `main` contents during a request.
- Keep Japanese calibration as a deployment validation task. The card reports
  multilingual/Japanese evaluation and calibrated probabilities, but the exact
  fine-tuning language split is not sufficient evidence for this wiki's
  Japanese threshold. Start with the two-threshold design and measure a small
  labelled sample before lowering thresholds.
- Preserve the model's Apache-2.0 attribution when packaging the local runtime
  or a hosted sidecar.

## 1. Scope and non-goals

### Required behavior

For one user question:

1. Enumerate every document visible under `GROWI_ROOT_PATH`.
2. If a document has `<document>/00-目次`, score its cards in one or more
   batched Jev calls; fetch and re-score the full body of cards above the
   candidate threshold.
3. If a document has no usable index, recursively enumerate its pages and score
   every page body.
4. Split oversized bodies into overlapping chunks; take the maximum probability
   for the page.
5. Treat `p(はい) > jev_threshold` as a candidate and
   `p(はい) >= jev_seed_threshold` after full-body confirmation as a seed.
6. For every confirmed page, use its card `エンティティ` values to find all
   wiki-wide cards that define those entities. Score those defining pages too,
   across document boundaries, until a visited set converges.
7. Put all confirmed pages in the existing seed context/ID list. Existing
   subagents still read, follow outgoing links, search, and cite pages exactly
   as they do today.
8. If Jev is disabled, unavailable, malformed, cancelled, or partially fails,
   preserve the current ES -> index map -> router -> lead/subagent behavior.

### Explicitly not in this change

- No new database, vector store, crawler service, background index, or GROWI
  write endpoint.
- No use of the linker SQLite catalog at query time. Live GROWI and parsed
  `00-目次` pages remain the only corpus metadata sources.
- No Jev answer generation, summarization, or replacement of the chat LLM.
- No changes to the Makefile or the publisher/index pipeline.
- No per-request Jev endpoint/path override; the endpoint is server-owned.
- No automatic download of model weights on a user query. Local weights and a
  hosted endpoint are deployment concerns.

## 2. Current code seams to reuse

Do not create parallel retrieval abstractions. Reuse these existing paths:

| Existing seam | Jev use |
| --- | --- |
| `researcher.py:IndexMap` | Root/document index loading, card parsing, card cache, entity fields. Add catalog helpers to its immutable snapshot. |
| `ResearchSession._fetch_page` | Page cache and request memoization. Add an optional sweep budget argument; never duplicate a page-fetch client. |
| `GrowiSearchClient.get_page` | Full page reads. |
| `GrowiSearchClient.list_children` | One-level child enumeration; add a sweep-only recursive walker in `ResearchSession`. |
| `gateway.py:Reranker` | Sibling location for optional Jev adapters and factory. |
| `ResearchSession._seed_context` / `_seed_ids` | Handoff to the current lead agent. |
| `app.py` SSE queue | Forward `jev_gate` and `jev_complete` events without a new stream. |
| `PageCache` and `_page_memo` | A body read by Jev is reused by subagents. |

The live path currently has no corpus enumeration method. The plan adds only
the recursive child walk needed by the sweep; it does not change the public
`/api/pages/children` contract.

## 3. Configuration (`growi-search/config.py`)

Add these `Settings` fields after the existing reranker/embedding settings.
Defaults keep Jev off, so an existing deployment behaves identically until it
opts in.

    jev_enabled: bool = False
    jev_backend: str = "auto"          # auto | local | hosted
    jev_local_path: str = ""            # directory containing jev_style_decision.py
    jev_base_url: str = ""              # hosted Jev-compatible HTTP endpoint
    jev_api_key: str = ""
    jev_model: str = "chaoliangUNSW/Jev-Style-0.8B-Decision-v3"
    jev_timeout: int = 60
    jev_threshold: float = 0.50
    jev_seed_threshold: float = 0.80
    jev_chunk_tokens: int = 25600
    jev_chunk_overlap: int = 10000
    jev_batch_size: int = 64
    jev_max_page_reads: int = 0         # 0 = unlimited; independent of RunBudget
    jev_max_list_calls: int = 0         # 0 = unlimited

Use these environment names in `Settings.from_env()`:

    WIKI_JEV_ENABLED
    WIKI_JEV_BACKEND
    WIKI_JEV_LOCAL_PATH
    WIKI_JEV_BASE_URL
    WIKI_JEV_API_KEY
    WIKI_JEV_MODEL
    WIKI_JEV_TIMEOUT
    WIKI_JEV_THRESHOLD
    WIKI_JEV_SEED_THRESHOLD
    WIKI_JEV_CHUNK_TOKENS
    WIKI_JEV_CHUNK_OVERLAP
    WIKI_JEV_BATCH_SIZE
    WIKI_JEV_MAX_PAGE_READS
    WIKI_JEV_MAX_LIST_CALLS

Validation/clamping:

- Parse booleans with the same `1/true/yes/on` convention already used.
- Clamp thresholds to `[0.0, 1.0]` and reject or correct
  `jev_seed_threshold < jev_threshold` by raising `ValueError` at startup.
- Require `jev_chunk_tokens > 0`, `0 <= jev_chunk_overlap < jev_chunk_tokens`,
  and `jev_batch_size >= 1`.
- Treat negative max budgets as invalid. Zero means unlimited because exhaustive
  traversal is the requested default.
- Do not put `jev_api_key` in `Settings.public_dict()`.
- Do not add Jev fields to `_sanitize_overrides`; Jev is server-controlled and
  a browser must not select a local file or arbitrary endpoint.

`Settings.public_dict()` may expose `jev_enabled`, `jev_backend`, and
thresholds for diagnostics, but not the key or local filesystem path.
`/api/ready` should report a boolean `jev` capability
(`Researcher.jev is not None`), not secrets.

Example local deployment:

    WIKI_JEV_ENABLED=1
    WIKI_JEV_BACKEND=local
    WIKI_JEV_LOCAL_PATH=/opt/models/Jev-Style-0.8B-Decision-v3

Example hosted deployment:

    WIKI_JEV_ENABLED=1
    WIKI_JEV_BACKEND=hosted
    WIKI_JEV_BASE_URL=http://jev-score.internal:8080
    WIKI_JEV_API_KEY=secret-if-required

`auto` selects local when `jev_local_path` is set, otherwise hosted when
`jev_base_url` is set. An ambiguous or missing configuration logs one warning
and disables Jev for that process; it does not fail GROWI search startup.

## 4. Jev adapter in `growi-search/gateway.py`

Keep the adapter beside `Reranker`; do not add a second gateway module.

### 4.1 Small common interface

Add two lightweight dataclasses/protocol types:

    @dataclass(frozen=True)
    class JevQuestion:
        key: str                 # stable page/chunk/card key
        text: str                # Japanese yes/no question

    class JevClassifier(Protocol):
        def score_many(
            self, state: dict[str, Any], questions: list[JevQuestion]
        ) -> list[float]: ...

`score_many` returns one finite probability in `[0.0, 1.0]` per input
question, in exactly the same order. Raise `RuntimeError` for transport,
model, shape, or length failures. The researcher catches that exception and
falls back; it must never silently align the wrong page with a probability.

The state is a JSON-like dictionary, not a pre-rendered opaque string:

    {
        "document": "/Moove/Manual",
        "page": {"id": "...", "title": "...", "path": "...", "text": "..."},
        "entity_definitions": [
            {"entity": "E", "title": "...", "path": "...", "summary": "..."}
        ],
    }

The local adapter passes this dictionary to the model runtime. The hosted
adapter sends it as JSON. Keeping the state structured makes the local and
hosted paths deterministic and testable.

### 4.2 Canonical question/options

Use one exact Japanese template for every card/page/chunk:

    状態:
    # 対象ページ
    タイトル: <title>
    パス: <path>
    本文（または目次カードの要約・キーワード）:
    <state page text>

    # このページで使われているエンティティを定義している他のページ
    - <entity> → <definer title>: <definer summary>

    質問: 上記のページ（およびそのエンティティ定義ページ）は、
    次の質問に答えるために関連しますか？
    質問: <user question>

    選択肢: はい / いいえ

The renderer, not each adapter, owns this wording. The `JevQuestion.text`
should contain the final question including the user query; the state carries
the page/card text. Use the model's `noul` type when the installed runtime
exposes it. If it does not, use the documented two-option `choice` call with
`{"yes": "はい", "no": "いいえ"}` and read the yes probability. Never
parse free-form generated text.

### 4.3 Local adapter

Implement `LocalJevClassifier` with lazy, process-scoped loading:

1. Resolve `jev_local_path` and verify it is a directory containing the model
   runtime (at minimum `jev_style_decision.py`).
2. Load that module with `importlib.util.spec_from_file_location`, rather than
   mutating global `sys.path`.
3. Instantiate `JevStyleDecision(local_path)` once in the `Researcher`, not
   once per question or per page.
4. At construction, inspect the runtime signature/version to determine whether
   `category="noul"` is accepted. Prefer `noul`; otherwise use `choice`.
5. For each state, call the runtime using its documented batched mode. The
   runtime's public quickstart is a `decide(state_dict, question, options=...)`
   call; preserve one rendered state and issue all `JevQuestion`s through the
   same model instance with `many_mode="batched"`. If the exact runtime exposes
   a native list-of-questions method, use it; otherwise loop over the questions
   while retaining the runtime's batched mode.
6. Extract `p(はい)` from `probabilities` using these accepted keys in order:
   `はい`, `yes`, `Yes`, then an answer-index representation. Reject missing,
   non-numeric, NaN, infinity, and out-of-range values.

Do not import `torch`, `transformers`, or MLX in the main service dependency
list. The model repository/runtime owns those optional dependencies. A missing
local runtime makes `build_jev()` return `None` with a useful log message.

### 4.4 Hosted adapter and wire contract

Implement `HostedJevClassifier` with the already-installed `httpx` dependency.
The endpoint is a Jev-compatible scorer (for example a `jev-score` sidecar),
not assumed to be a text-generation endpoint.

Request: `POST {jev_base_url.rstrip('/')}/score` (if the configured URL already
ends in `/score`, do not append it).

    {
      "model": "chaoliangUNSW/Jev-Style-0.8B-Decision-v3",
      "state": {"document": "...", "page": {"...": "..."}, "entity_definitions": []},
      "questions": [
        {"key": "page-id", "text": "質問 ..."}
      ],
      "options": {"yes": "はい", "no": "いいえ"},
      "category": "noul",
      "many_mode": "batched"
    }

Required response shape (one of the two equivalent forms):

    {"probabilities": [{"yes": 0.91}, {"yes": 0.12}]}

or:

    {"results": [{"key": "page-id", "probabilities": {"はい": 0.91}}]}

For `results`, align by request order unless every result has a unique
`key`, then align by key and reject duplicates/missing keys. Accept a bare
ordered list only if the sidecar explicitly documents it; otherwise fail closed.

Send `Authorization: Bearer <jev_api_key>` only when a key is configured and
`Content-Type: application/json` always. Use `jev_timeout`; make one bounded
retry only for a transport/5xx error if the existing gateway retry pattern is
reused. Never retry malformed 4xx responses. Do not log state, page bodies,
questions, or API keys; log endpoint/status and a short error only.

`build_jev(settings)` returns `None` when disabled or when
construction/probe fails. Do not make startup perform a full model inference. A
hosted `GET /health` probe is optional; the first real score call must still
handle failure.

### 4.5 Factory behavior

    jev_enabled=false                  -> None
    backend=local + local path         -> LocalJevClassifier
    backend=hosted + base URL          -> HostedJevClassifier
    backend=auto + local path          -> local
    backend=auto + base URL            -> hosted
    anything else                      -> warning + None

`Researcher.__init__` builds this once and passes the shared instance into
every `ResearchSession`. `ResearchSession.apply_overrides` must not rebuild
it.

## 5. Index catalog changes in `researcher.py`

Extend `_MapState` without changing the existing ranking behavior:

    entity_definers: dict[str, list[md.IndexCard]]
    cards_by_document: dict[str, list[md.IndexCard]]

Normalize entity keys with Unicode NFKC, whitespace collapse, and case folding.
Keep original spelling in cards and preserve insertion order. When `_build()`
parses each document index:

1. Set `card.document` to the root document title/path as it does today.
2. Add the card to `cards_by_document[document_key]`.
3. For every `card.entities` entry, append the card to
   `entity_definers[normalize(entity)]`.

Add read-only helpers:

    def cards_for_document(self, document_key: str) -> list[md.IndexCard]: ...
    def definers_for(self, entity: str) -> list[md.IndexCard]: ...
    def card_for_target(self, target: str) -> md.IndexCard | None: ...

Do not change `IndexMap.rank`, its IDF/embedding fusion, or its cache TTL.
The Jev sweep needs the complete snapshot, not only the top-k rank output.

## 6. Sweep data structures and independent budget

Add small private dataclasses in `researcher.py`:

    @dataclass
    class JevGate:
        page: WikiPage
        probability: float
        stage: str                 # "card" or "full"
        document: str
        card: md.IndexCard | None = None
        chunk_index: int | None = None

    @dataclass
    class JevSweepBudget:
        max_page_reads: int
        max_list_calls: int
        page_reads: int = 0
        list_calls: int = 0

Budget methods must be lock-protected if the implementation chooses parallel
document work. The initial implementation should keep model calls serialized
and rely on batched questions; this avoids oversubscribing a 0.8B local model.
`0` means unlimited. Cache/memo hits do not consume a budget unit.

Change `_fetch_page` to accept a keyword-only `sweep_budget=None`:

    def _fetch_page(..., sweep_budget: JevSweepBudget | None = None):

Behavior:

- Existing callers pass no budget and retain `RunBudget.try_page()` behavior.
- The sweep checks `sweep_budget.try_page()` instead of
  `self.budget.try_page()`.
- Both paths check `_page_memo` and `PageCache` first.
- A successfully fetched page is stored in both caches, so subsequent lead or
  subagent reads do not pay the normal per-run fetch budget.
- A denied sweep read returns `None` and emits a budget event; it must not call
  GROWI.

Add a sweep-only recursive child walker that calls
`client.list_children(page_id=...)` or `path=...` one level at a time. It
must:

- Consume `JevSweepBudget.list_calls` only on an actual network call.
- Skip pages named exactly `settings.index_page_name`.
- Track visited page IDs/paths to stop malformed GROWI cycles.
- Call `_check_stop(stop_event)` before each request and after each returned
  batch.
- Preserve `root_path` filtering already enforced by `GrowiSearchClient`.

## 7. Corpus enumeration

Implement private methods with narrow responsibilities:

    def _jev_documents(self, state: _MapState, budget, stop_event) -> list[JevDocument]: ...
    def _walk_document_pages(self, root: WikiPage, budget, stop_event) -> list[WikiPage]: ...
    def _document_index(self, root_or_card) -> tuple[WikiPage, list[IndexCard]] | None: ...

`JevDocument` can be a private dictionary/dataclass containing `key`,
`root`, `index_page`, and `cards`.

### Indexed documents

Use the root `00-目次` cards already loaded in
`IndexMap.snapshot().docs`:

1. Resolve each root card target with the existing `IndexMap._read` rules (ID
   target -> `get_page(page_id=...)`, path target ->
   `get_page(path=...)`).
2. Accept it as an indexed document only when the page exists and
   `md.is_index_page(page.body)` is true.
3. Use the document index path's parent as the document root and use the parsed
   document cards from the snapshot.
4. If the root index is missing/stale or the document index fails validation,
   mark this document for the no-index fallback; never silently omit it.

### No-index fallback

Call the child walker from `settings.growi_root_path`. Each first-level child is
one document root for sweep purposes. Recursively descend descendants; a
leaf/page is a page candidate. If a first-level child has no descendants,
classify that child itself. Deduplicate by page ID, then path.

This fallback may include a GROWI folder shell. It is safe to classify: the
shell has an empty body and will normally receive a low probability. Do not
make extra title-search calls to distinguish folders.

The result must include indexed documents even if the root index omitted one
that is discoverable by first-level enumeration. Merge both sources by stable
document path.

## 8. State rendering and token chunks

Add pure helpers, easy to unit test:

    def _jev_question(query: str) -> str: ...
    def _jev_state(document, page, text, entity_defs) -> dict[str, Any]: ...
    def _jev_chunks(text: str, max_tokens: int, overlap_tokens: int) -> list[str]: ...
    def _jev_probability(values: list[float]) -> float: ...

### Token accounting

25,600 tokens is the whole model input, not just page body. Reserve 512 tokens
for the Japanese template, metadata, and entity labels:

    body_budget = jev_chunk_tokens - 512

Prefer a tokenizer exposed by the local Jev runtime. The hosted adapter cannot
assume a tokenizer is installed, so provide a conservative fallback estimator
based on UTF-8 bytes (document the estimator in a `ponytail:` comment). The
fallback must under-fill the limit rather than risk a 25,600-token request.

Chunk rules:

- If estimated body tokens <= `body_budget`, return one chunk.
- Otherwise make forward chunks of at most `body_budget` with
  `jev_chunk_overlap` estimated tokens of overlap.
- Split first at Markdown section/paragraph boundaries where possible; then
  hard-split a single oversized paragraph. Never produce an empty chunk.
- Preserve a short page title/path prefix in every chunk; count that prefix in
  the budget.
- If overlap >= body budget after validation, fail startup rather than loop.
- For a page with N chunks, page confidence is `max(probability(chunk_i))`.

The entity-definition rule is deliberately strict: for a chunk, include only
definitions whose entity text occurs in that chunk or whose card was attached
to that page's card state. Do not append unrelated definitions from a distant
part of a document merely to fill context. This prevents the state from
crossing the intended chunk region.

## 9. Relevance sweep algorithm

Implement one method:

    def _run_jev_sweep(
        self, query: str, emit: Callable, stop_event: Event | None
    ) -> list[dict[str, Any]]:

It returns confirmed records in deterministic discovery order. One record must
contain at least `node`, `score`, `why`, and `evidence`, matching the
shape consumed by `format_lead_candidate` and `_cite`.

### Phase A: exhaustive document pass

1. Build `JevSweepBudget` from settings.
2. Snapshot the index map once. If the snapshot refresh fails, continue with
   an empty card catalog and the child-walk fallback.
3. Enumerate all documents as described above.
4. For an indexed document, split cards into batches whose state fits the Jev
   token budget and whose count is at most `jev_batch_size`. For each batch:
   - state contains the document and all cards in that batch (`title`,
     `path`, summary, chapter, keywords, entities);
   - create one `JevQuestion` per card, keyed by card target;
   - call `score_many` once;
   - emit one `jev_gate` per card;
   - for every card with `p > jev_threshold`, fetch the full page and run Phase
     A full confirmation below.
5. For a no-index document, fetch/classify every enumerated page body directly.
6. Add each page ID/path to `visited_pages` after its final full verdict. A
   page may be encountered through both the index and fallback enumeration but
   is confirmed only once.

### Full confirmation

For a card candidate:

1. Fetch the full page through
   `_fetch_page(..., sweep_budget=budget)`.
2. Build entity context from the card's `entities` and the catalog's
   `entity_definers` summaries. Do not fetch every definition body yet.
3. Chunk the body and call `score_many` per chunk (one question per chunk; a
   small chunk batch is allowed when the state is identical).
4. Emit `jev_gate` with `stage="full"`, the max probability, and the chunk
   count.
5. Add a confirmed result only when max probability >=
   `jev_seed_threshold`.

For a no-index page:

1. Fetch the full body directly.
2. Use known sibling-index entity summaries when available; otherwise use an
   empty entity context. Do not pretend that ordinary Markdown hyperlinks are
   entity-defining edges.
3. Classify chunks and use the same seed threshold.

If a candidate page cannot be fetched because the sweep budget is exhausted,
emit `stage="full", status="unread"` and do not call it a seed. ES remains the
recall fallback if no seeds are confirmed.

### Phase B: entity-definer frontier

Use a FIFO queue of `(confirmed_page, entity)` and a `visited_pages` set:

1. For a confirmed page, derive its entities from its card's `entities`. For a
   no-index page, normalize known entity names and match exact/word-boundary
   occurrences in the page title/body; prefer the longest entity names first.
2. For each entity, look up every card in `IndexMap.definers_for(entity)`.
3. A card target is the defining page under the publisher convention: an
   `エンティティ` value on a card means that card's page defines that entity.
4. Skip a target already in `visited_pages`; otherwise process it exactly like
   a normal document page: card stage if its document has a valid index, then
   full confirmation; direct full stage for a no-index document.
5. If the definer is confirmed, enqueue its own entities. A low-confidence
   verdict prunes that branch. The visited set guarantees cycles converge.
6. Continue until the queue is empty, cancellation is requested, or the
   independent sweep budget denies further work.

Do not recurse through arbitrary Markdown links here. Ordinary links remain the
subagents' existing exploration graph.

### Result shape and deterministic ordering

For each confirmed page create:

    {
        "node": page,
        "score": probability,
        "why": [{"field": "jev", "rank": ordinal}],
        "evidence": [{
            "page_id": page.id,
            "field": "jev",
            "text": "Jev p(はい)=0.87; stage=full; ...",
            "score": probability,
        }],
    }

Add `"jev"` to the `Evidence.field` literal in
`growi-search/models.py`. Sort only by discovery order for the seed list; do
not apply ES or reranker scores to Jev probabilities. Deduplicate by page ID,
falling back to normalized path when GROWI did not return an ID.

## 10. Research-session integration

### Constructor and service wiring

Change `ResearchSession.__init__` to accept `jev: JevClassifier | None` and
store it. Change `Researcher.__init__` to call `build_jev(settings)` once and
pass the shared instance when creating sessions. Existing fake/session callers
must keep working by giving the new parameter a default of `None` or by using a
keyword argument.

### `_try_route` ordering

At the start of `_try_route`, after clearing `_seed_context`/`_seed_ids`:

    if jev is configured:
        confirmed = _run_jev_sweep(...)
        emit jev_complete
        if confirmed:
            set seed context and seed IDs from confirmed
            emit candidates for those confirmed pages
            return None                 # force existing deep lead path

Returning `None` intentionally selects `_run_lead`; the lead sees the
complete Jev seed pool through `_seeded`, and it still chooses bounded
distinct starts for `explore`. Do not bypass the existing subagent flow or
create a second answer path.

If `confirmed` is empty, or Jev is absent/fails, execute the current code from
`search_with_evidence` onward unchanged. This preserves shallow answers and
the ES fallback. A Jev failure should emit `jev_unavailable` once and not
cause the `/api/ask` request to fail.

The existing `_distinct_starts` cap (`subagent_count`) remains the protection
against unbounded agents. All confirmed pages are still placed in
`_seed_ids`/`_seed_context`; the lead can choose which distinct starts to
send to the bounded team.

### Budget interaction

Jev page/list reads use `JevSweepBudget`, not `RunBudget`. They still use
`_fetch_page` and therefore warm the shared cache/memo. Consequently:

- the normal 12-page budget does not make an exhaustive sweep stop early;
- pages retained in cache cost no normal budget for the lead/subagents;
- if a page was evicted before exploration, the normal budget applies normally;
- no change is needed to `RunBudget` limits or agent tool semantics.

### Cancellation and errors

Call `_check_stop(stop_event)` before every batch, GROWI request, and entity
frontier item. Let `AgentStopped` propagate so the current SSE cancellation
contract remains intact. Catch `GrowiAPIError`, adapter `RuntimeError`, and
malformed individual page data at the smallest useful scope, log a concise
message, emit a gate/error event, and continue with other documents. If the
classifier itself becomes unusable after a transport failure, stop the sweep
and fall back to ES rather than hammering the endpoint for every page.

## 11. SSE and API surface

No new HTTP route is required.

Emit these events through the existing `emit` callback:

    {
      "type": "jev_gate",
      "stage": "card|full",
      "status": "candidate|pruned|confirmed|unread|error",
      "node": {"id": "...", "title": "..."},
      "document": "/Moove/Manual",
      "probability": 0.87,
      "threshold": 0.5,
      "chunk_count": 1
    }

Do not include page body text or the user question in events. Use rounded
probabilities only for display; retain the full float internally.

At sweep end emit:

    {
      "type": "jev_complete",
      "documents": 12,
      "pages_considered": 420,
      "cards_considered": 380,
      "candidates": 31,
      "confirmed": 14,
      "entity_edges_followed": 22,
      "page_reads": 28,
      "list_calls": 16,
      "elapsed_ms": 1840
    }

The current frontend ignores unknown event types, so backend correctness does
not depend on a UI change. If progress display is desired later, add one small
`jev_gate` handler to `frontend/src/hooks/useAskStream.js`; do not block the
backend implementation on a new visual component.

Update `/api/ready` in `app.py` with `jev`, and update the existing
readiness assertion in `growi-search/tests/test_app.py`. Do not expose keys or
local paths.

## 12. Tests (extend existing files; create no test file)

The repository test policy requires one test file per project workflow and
forbids unmentioned files. Add Jev tests to the existing
`growi-search/tests/test_researcher.py`; update only the existing app/config
assertions where needed. Do not add a `test_jev.py`, fixture directory, DB, or
Makefile change. Keep tests stdlib `unittest` and `httpx.MockTransport` based.

### Adapter tests

1. `JevQuestion` request order is preserved in returned probabilities.
2. Hosted adapter sends `/score`, model, options, `category`,
   `many_mode`, and auth only when configured.
3. Hosted parser accepts `probabilities` and keyed `results`, including
   Japanese `はい` keys.
4. Hosted parser rejects missing results, duplicate keys, wrong lengths,
   NaN/infinity, and values outside `[0, 1]`.
5. 4xx is not retried; a bounded 5xx/transport retry (if implemented) is.
6. Local adapter uses a fake injected `JevStyleDecision` object and verifies
   `noul` preference, `choice` fallback, and probability extraction. The
   test must not download weights or import torch.
7. `build_jev` returns `None` for disabled/missing/invalid configuration and
   returns a single reusable adapter for valid local/hosted configuration.

### Pure sweep tests

8. Japanese question rendering contains the user query, exact yes/no wording,
   page metadata, and entity summaries.
9. Chunking respects the configured maximum estimate, has the configured
   overlap, preserves title/path prefixes, and returns max probability across
   chunks.
10. Indexed document: card batch above `jev_threshold` triggers exactly one
    full read; a full probability below `jev_seed_threshold` is pruned.
11. No-index document: recursive children enumerate all leaves, including a
    nested child; every page is scored, not only ES hits.
12. A document whose index fetch fails is not omitted; it uses the slow fallback.
13. Two documents are both swept even when only one contains query vocabulary.

### Entity-tree tests

14. A confirmed page in document A finds an entity definer in document B.
15. A confirmed definer enqueues its own entity; a low-confidence definer does
    not enqueue descendants.
16. A two-page entity cycle terminates once due to `visited_pages`.
17. Entity lookup is Unicode/case/whitespace normalized but preserves original
    card labels in state/events.
18. A no-index page with no matching known entity does not invent an edge from
    an arbitrary Markdown hyperlink.

### Integration/fallback tests

19. Sweep reads do not increment `RunBudget.pages_used`; the same page is
    served from `PageCache`/memo on a later subagent read.
20. Independent sweep max-page/list budgets stop network calls and emit
    `unread`/summary counts without failing the answer.
21. Adapter failure emits `jev_unavailable` and executes the existing ES/router
    path; disabled Jev produces no Jev calls.
22. Confirmed pages populate `_seed_ids` and `_seed_context`, and
    `_run_lead` receives them while `_run_subagents` behavior remains
    unchanged.
23. `jev_gate` and `jev_complete` are present in streamed SSE and the final
    `answer`/`done` frames are unchanged.
24. `/api/ready` adds only the boolean `jev` field; no token/path leaks.

Run the existing test command after implementation:

    cd /home/seigyo/llm-wiki
    uv run python -m unittest discover -s growi-search/tests -p 'test_*.py'

If the repository's normal CI command differs, use it as well; do not modify
the Makefile to accommodate Jev.

## 13. Documentation changes (`growi-search/README.md`)

Add a short configuration section, not a second architecture manual:

- Jev is disabled by default.
- Local mode points `WIKI_JEV_LOCAL_PATH` at the model runtime/weights.
- Hosted mode points `WIKI_JEV_BASE_URL` at a Jev-compatible `/score` sidecar
  and optionally sets `WIKI_JEV_API_KEY`.
- `jev_threshold` is the candidate gate; `jev_seed_threshold` is the
  full-read seed gate.
- The sweep is exhaustive and can cost many GROWI reads; use the independent
  Jev budgets as an operational safety valve.
- The model has a 25,600-token input ceiling; chunks use the configured overlap.
- ES remains the fallback when Jev is disabled or unavailable.

Link the model page and state that the hosted wire contract is this service's
adapter contract, not a claim that the Hugging Face page exposes a generic
text-generation endpoint.

## 14. Implementation order for a weaker coding model

Implement in this exact order and keep each step green:

1. Add settings fields/env parsing/validation and the `jev` readiness boolean;
   update existing config/app tests.
2. Add the adapter interface, hosted parser, local dynamic loader, and factory
   in `gateway.py`; add adapter unit tests with fakes.
3. Add `Evidence.field="jev"`, state/question/chunk pure helpers, and their
   tests. Do not touch routing yet.
4. Extend `_MapState` and `IndexMap` helpers; verify all existing index-map
   tests still pass.
5. Add `JevSweepBudget`, optional `_fetch_page` budget, recursive child walk,
   and document enumeration tests.
6. Implement indexed card scoring and full confirmation; add gate events.
7. Implement no-index page scoring and chunk max aggregation.
8. Implement entity-definer frontier with visited/pruning/cycle tests.
9. Wire `_run_jev_sweep` into `_try_route`, seed handoff, fallback,
   cancellation, and summary event. Run the whole suite.
10. Update README and verify local/hosted environment examples manually with a
    fake adapter; only then test against a real local/hosted model.

At every step, retain the current behavior when `WIKI_JEV_ENABLED` is unset.

## 15. Acceptance checklist

- [ ] A clean deployment with no Jev settings passes all existing tests and
  follows the old ES/router path.
- [ ] A local model is loaded once and receives batched Japanese relevance
  decisions without adding mandatory ML dependencies to the service.
- [ ] A hosted model receives the documented structured request and returns
  validated ordered probabilities.
- [ ] Every indexed and no-index document is considered; ES is never the
  pre-filter for the sweep.
- [ ] Card passes are confirmed by full page reads before becoming seeds.
- [ ] Oversized bodies obey the 25,600-token ceiling and 10,000-token overlap
  defaults, with max-overlap deduplication.
- [ ] Entity-defining pages recurse across documents and terminate on cycles.
- [ ] Sweep reads use their own budget and warm the existing page cache.
- [ ] Confirmed pages seed the existing lead/subagent flow; subagents remain
  ungated and bounded.
- [ ] Failure/cancellation/low confidence never fabricates a candidate and
  always leaves ES as recall insurance.
- [ ] No Makefile, publisher, linker SQLite, or new test file was added.

The minimal shipped implementation is therefore three production-file changes
(`config.py`, `gateway.py`, `researcher.py`) plus the small model/API
literal, existing-test updates, and README documentation. Do not split
adapters, crawlers, or indexes into new modules until profiling demonstrates a
real need.
