# growi-search

A **read-only**, self-contained search + research service over a **live GROWI**
instance. It trims the heavier `llm-wiki` graph service down to exactly three
things:

1. **GROWI keyword search + page reads**, with a local disk mirror for page
   bodies after warm-up (no SQLite, vector store, or search index).
2. **Keyword-based retrieval** using GROWI's Elasticsearch order and index-card
   term overlap. Embeddings and reranking are optional and disabled by default.
3. **A lead-agent + bounded parallel subagent researcher** that answers a
   question with citations, streamed to the browser over SSE.

There are **no write endpoints**, no graph canvas, no upload/queue, and no
admin page. The bundled frontend is a read-only chat + search + lazy folder
browser.

## Layout

```
growi-search/
  app.py            FastAPI routes + SSE + prefix middleware + static frontend
  config.py         Settings.from_env() (project INI and env configuration)
  models.py         WikiPage / WikiLink / Evidence / AgentAnswer
  markdown.py       heading sections + link extraction (pure, no I/O)
  growi_client.py   sync httpx client (GROWI _api/search + _api/v3 page/children)
  gateway.py        OpenAI chat client (LlmClient), Embedder + server-only Reranker
  prompts.py        router / shallow / lead / subagent prompts (ja)
  researcher.py     ResearchSession + Researcher (budgets, caches, SSE events)
  frontend/         trimmed React SPA (single build entry, read-only)
  tests/            stdlib unittest + httpx.MockTransport (no DB, no network)
```

## Requirements

- Python **3.13** and [`uv`](https://docs.astral.sh/uv/).
- Node **20+** / npm to build the frontend.
- A reachable GROWI v3 instance and (for `/api/ask`) an OpenAI-compatible chat
  endpoint. Embedding and reranker endpoints are not required.

## Setup

```bash
cd growi-search
uv sync                       # creates .venv from pyproject.toml
cp /dev/null .env             # optional service-local overrides
```

Configuration is read from an optional `growi-search/.env` first, then the
repository `.env` supplies any missing values. Minimal required set:

The service-local `.env` sets `WIKI_EMBED_BASE_URL=` and
`WIKI_RERANK_BASE_URL=` empty so search uses keyword overlap and Elasticsearch
order. If that ignored local file is absent, the service also defaults these
URLs to empty before loading the shared builder `.env`. Process-level
environment variables and non-empty service-local values can still enable them;
an inherited process value takes precedence over both files.

To reuse an existing project configuration, set `WIKI_PROJECT=projectA` (or an
absolute INI path). Its `growi_url`, `growi_token`, `target_name` (as the GROWI
root), and `settings.chat_base_url` are used directly; explicit `WIKI_*`
settings still control all other search-service behavior.

| Variable | Required | Meaning |
| --- | --- | --- |
| `GROWI_URL` | yes | Base URL of the live GROWI instance. |
| `GROWI_TOKEN` | yes | Service-account API token (an `API Token:` prefix is stripped). |
| `GROWI_ATTACHMENT_TOKEN` | no | Scoped GROWI access token with attachment-read permission; required for cookie-free image downloads. Falls back to `GROWI_TOKEN`. |
| `GROWI_ROOT_PATH` | no | Restrict all results/browsing to this path; default `/`. |
| `WIKI_CHAT_BASE_URL` | ask | Chat endpoint (falls back to `OPENAI_BASE_URL`). |
| `WIKI_CHAT_MODEL` | ask | Chat model (falls back to `WIKI_MODEL`). |
| `WIKI_LLM_CONTEXT_TOKENS` | no | Model context window used to partition compiler and synthesis inputs (default `131072`; set this to the deployed model's actual window). No source text is discarded. |
| `WIKI_SEARCH_LLM_MAX_CONCURRENCY` | no | Process-wide maximum concurrent LLM requests; default `4`. |
| `WIKI_SUBAGENT_CONCURRENCY` | no | Per-question concurrency, clamped to the process-wide LLM maximum; default `2`. |
| `WIKI_SUBAGENT_REPORT_TOKENS` | no | Output-token ceiling for one subagent reply (default `16384`, clamped `256..16384`). |
| `WIKI_REPORT_FOLD_TOKENS` / `WIKI_FINAL_COMPILER_TOKENS` | no | Generation limits for level-1 report folds and the final answer compiler; defaults `32768` / `65536`. These are output limits, not evidence/input limits. |
| `WIKI_COMPILER_CONCURRENCY` | no | Independent level-1 compiler pool size; default `4`. Compilers run while explorer groups are still finishing. |
| `WIKI_LLM_MAX_RETRIES` | no | Connection retries inside the lead and subagent model calls (default `2`, `0` = off). Only connection errors / 408 / 409 / 429 / 5xx are retried. |
| `WIKI_EMBED_BASE_URL` / `WIKI_EMBED_MODEL` | no | Optional index-card embedding ranking. Empty URL disables it. |
| `WIKI_RERANK_BASE_URL` | no | Optional `/v1/rerank` service. Empty URL preserves Elasticsearch order. |
| `WIKI_INDEX_PAGE_NAME` | no | Published index page name; default `00-目次` (what `main.py index` writes). |
| `WIKI_INDEX_CACHE_TTL` / `WIKI_INDEX_MAP_TOP_K` / `WIKI_INDEX_MAP_EMBED_K` | no | Index-map refresh interval and ranking limits. |
| `WIKI_WALKER_THRESHOLD` / `WIKI_WALKER_ROUTE_THRESHOLD` | no | Minimum Jev probability for a page result / folder or document expansion; defaults `0.5` / `0.15`. |
| `WIKI_WALKER_MIN_CHILDREN` / `WIKI_WALKER_K` | no | Minimum children retained while routing / maximum `find` results; defaults `2` / `3`. |
| `WIKI_WALKER_MAX_ITEMS` | no | Maximum Jev questions per walk; default `150`. |
| `WIKI_WALKER_ES_RESCUE` | no | Set to `1` to add GROWI Elasticsearch hits as extra walker start nodes; default `0`. |
| `WIKI_JEV_MODE` | no | `exhaustive` (default) or opt-in `cascade`; exhaustive remains the default pending real-corpus evaluation. |
| `WIKI_CASCADE_MAX_DOCS` / `WIKI_CASCADE_SECTION_THRESHOLD` | no | Cascade routing and section thresholds; defaults `40` / `0.3`. |
| `WIKI_CASCADE_SUBAGENTS` | no | Cascade subagent limit; default `15`, clamped to `1..32`. |
| `WIKI_CASCADE_CONTEXT_TOKENS` / `WIKI_CASCADE_SUBAGENT_STEPS` | no | Evidence-token budget and tool steps per cascade subagent; defaults `48000` / `4`. |
| `WIKI_CASCADE_EARLY_STOP` / `WIKI_ANSWER_CACHE` | no | Cascade sufficiency threshold (`0.9`) and optional mirror-backed answer cache (default off). |
| `WIKI_JEV_TOC_GATE` / `WIKI_JEV_TOC_GATE_THRESHOLD` | no | Optional Jev check before TOC summarization; default off / `0.2`. |
| `WIKI_JEV_DETERMINISTIC` / `WIKI_JEV_VERDICT_CACHE` | no | Optional deterministic note/rewrite calls and mirror verdict cache; both default off. |
| `WIKI_LEAD_AFTER_REPORTS` / `WIKI_AGENT_TOOL_CONCURRENCY` | no | Compiled Jev reports always use direct `synthesis`; agent tool calls elsewhere default to concurrency `1`. |
| `WIKI_ALLOWED_LLM_HOSTS` | no | Optional comma-separated allowlist for per-request LLM override hosts; the configured chat host is always allowed. |
| `WIKI_PREFIX` | no | Public path prefix; default `/growi-search`. |

The local mirror defaults to `../data/growi-search-mirror`, polls every 10
seconds, and relists every 30 minutes. Set `WIKI_MIRROR_DIR=` to disable it.
`WIKI_MIRROR_CHANGES=auto` prefers the audit log and falls back to recent-page
polling when audit access is denied. In recent mode, edits are polled but
deletes and renames are detected at the periodic full relist. The mirror stores
compressed page bodies on disk and a bounded in-memory hot set; the namespace
contains a hash of the token, never the token itself.

The full env contract (candidate limits, budgets, mirror, cache, subagent
limits, doc-parser URL, usage log, …) is implemented in `config.py`.

## Run

```bash
# backend (serves the built frontend under WIKI_PREFIX automatically)
cd growi-search
GROWI_URL=http://127.0.0.1:3000 GROWI_TOKEN='your-local-api-token' \
  uv run uvicorn app:app --host 0.0.0.0 --port 8001
```

Then open `http://<host>:8001/growi-search/` (or `http://<host>:8001/` when
`WIKI_PREFIX=""`). `/health` is also reachable unprefixed.

### Frontend

```bash
cd growi-search/frontend
npm ci
npm run build          # -> frontend/dist, served as static files
# or for development (hot reload, proxies handled by the same-origin base URL):
npm run dev
```

The frontend derives its API base from the URL it was loaded from
(`window.location.pathname`), so it works behind any `WIKI_PREFIX` without a
rebuild. Set `VITE_API_URL` to override.

### Frontend development against local GROWI

The Vite dev server proxies `/api/*` to the backend on port 8001, which then
talks directly to GROWI on `http://127.0.0.1:3000`:

```bash
# terminal 1
cd llm-wiki-air/growi-search
GROWI_URL=http://127.0.0.1:3000 GROWI_TOKEN='your-local-api-token' \
  uv run uvicorn app:app --host 127.0.0.1 --port 8001

# terminal 2
cd llm-wiki-air/growi-search/frontend
npm ci
npm run dev
```

Open `http://localhost:8000/`. Set `VITE_API_PROXY_TARGET` or
`VITE_API_PROXY_PREFIX` if the backend uses another host or prefix.

## API

All under `WIKI_PREFIX` (default `/growi-search`). There is **no `/api/graph`**
and no writes.

| Method & path | Purpose |
| --- | --- |
| `GET /api/ready` | `{ready, growi, search, llm, reranker, embedder, jev, root_path}` (capabilities reported separately; booleans only, never paths or keys). |
| `GET /api/growi` | `{enabled, url, root_path, doc_parser_url}` — **never** the token. |
| `GET /api/settings` | Public runtime defaults for the settings screen; secrets are excluded. |
| `GET /health` | process liveness, also served unprefixed. |
| `GET /api/pages/children?page_id=…\|path=…` | one level of child pages; exactly one selector (400 otherwise). |
| `GET /api/document?path=…` | document folder plus page summaries/keywords from its index page. |
| `GET /api/node/{id}` | full page view (`body`) + `links[]` from the already-read body — no second GROWI call. |
| `GET /api/attachment/{id}` | authenticated proxy for a 24-character GROWI attachment ID. |
| `GET /api/search?q=&limit=` | fast search: one ES call + rerank; candidate `body` is intentionally empty; adds `score` + `evidence`. |
| `POST /api/ask` | `{question, overrides?}` → `{question, answer, cited_node_ids, cited_nodes, steps}`. |
| `POST /api/ask/stream` | same, plus SSE `map` and budget events (`run`,`search`,`map`,`candidates`,`route`,`subagents_spawned`,`subagent_start`,`read`,`follow_link`,`subagent_done`,`compiling`,`answer`,`cancelled`,`error`,`done`, `: ping` keepalives). |
| `POST /api/agent-runs/{run_id}/stop` | cancel an in-flight run (repeat-safe; 404 for unknown). |

Per-request LLM/agent overrides (`chat_base_url`, `chat_api_key`, `chat_model`,
`chat_temperature`, `subagent_*`, `agent_max_steps`, `rerank_top_k`, and index
map limits) are sanitized. Unknown keys are ignored, numeric values are capped
at hard safety limits, and hosts outside `WIKI_ALLOWED_LLM_HOSTS` (or blocked
metadata/localhost ranges) return **400**.

Error contract: GROWI auth failures → `502 growi_auth_failed`; timeouts/unreach-
able → `503 growi_unavailable`; non-JSON/5xx → `502 growi_bad_response`;
missing page → `404 page_not_found`.

## How retrieval works

1. `/api/search` (and the router's first hop) makes **exactly one** GROWI
   `/_api/search` call and returns snippets in Elasticsearch order — never page
   bodies. Reranking is optional.
2. The optional index map reads the root `/<root>/00-目次` tree and document
   indexes. It ranks page cards by term overlap when embeddings are disabled;
   embeddings and reranking can be enabled separately. Index refreshes are
   process-local and do not consume per-run page budgets.
3. `/api/ask` runs the router: `shallow` answers from ≤ `WIKI_SHALLOW_PAGE_READS`
   hydrated pages; section reranking is optional. `deep` hands the lead agent
   the candidates. Failed hydration falls back to deep.
4. The lead agent (`search` / `explore` / `finish`) spawns bounded parallel
   subagents (`read` / `follow_link` **outgoing-only** / `search` / `finish`).
   Every page body and ES call is charged to a shared per-run budget
   (`WIKI_MAX_PAGE_FETCHES_PER_RUN`, `WIKI_MAX_SEARCH_CALLS_PER_RUN`); duplicate
   reads/searches are memoized and the process-local TTL page cache
   (`WIKI_PAGE_CACHE_*`) is shared across subagents. With the mirror enabled,
   page bodies are also persisted in its disk cache.

## Jev relevance gate (optional)

Jev ([chaoliangUNSW/Jev-Style-0.8B-Decision-v3](https://huggingface.co/chaoliangUNSW/Jev-Style-0.8B-Decision-v3))
is a per-question **relevance gate**, not an answer generator. When enabled,
each `/api/ask` question runs either the default exhaustive sweep or the opt-in
cascade pipeline (`WIKI_JEV_MODE=cascade`). Keep exhaustive mode as the default
until WP-15 meets its quality targets on the real corpus. In exhaustive mode,
each question scores page entries in document `00-目次` pages reachable from a
project root `00-目次`. With `GROWI_ROOT_PATH=/`, each first-level project with a
valid root index is a separate entry point. Unlisted pages are not scored. A
"yes" card is fetched as a research seed without a second, full-body Jev check.
Before cards are scored, the question is scoped against reachable document
indexes:

1. **Find document 目次** — from each project that has its own root `00-目次`,
   walk that project's page tree to every depth and read every document-level
   `00-目次`. The GROWI-level `/` is never treated as a project root.
2. **Analyze intent and rank them** — an LLM first expands the user's target,
   requested range, required evidence, and exclusions. Jev then asks every 目次
   whether it has concrete evidence for that scoped intent, one page
   at a time in `WIKI_JEV_TOC_CHUNK_TOKENS` chunks: Jev refuses an over-budget input
   and never truncates, so one oversized 目次 must not sit in the same batch as the
   pages scored beside it. The top 10% are kept, plus everything tied with them up to
   the material budget (5 × 12,000 chars), because a wiki whose scores are all `0.00`
   has no 10% to speak of.
3. **Summarise the winners** — `TOC_NOTE_GROUPS` LLM calls (5), balanced by page size
   so a huge 目次 gets its own call and small ones share, each checking whether the
   document has the requested evidence rather than merely sharing the topic.
4. **Rewrite** — one call combines the original question, the initial intent, and
   those notes into a page-target question whose breadth follows the user's actual
   range. Each document-index card is judged only against that scoped requery, so
   topic-only pages are rejected.

Each stage is printed as it lands (`Jev 目次 digest: 24 文書 …`, then `Jev query
(rewritten from 24 目次 entries): …`), and a stage that fails says why instead of
quietly sweeping with the raw question.
The gate asks whether each document-index entry indicates a page useful for the
question. One thread scores cards while `WIKI_JEV_WORKERS` threads fetch accepted
pages for the lead and subagents. Page bodies are not sent to Jev in this sweep.

Confirmed seeds then drive the research directly: they are grouped by document
and sliced into at most `WIKI_JEV_SUBAGENT_GROUPS` groups of at most
`WIKI_JEV_SUBAGENT_GROUP_SIZE` seeds, and each group gets its own subagent
(running `WIKI_SUBAGENT_CONCURRENCY` at a time). A group only ever sees its own
seeds; reading a page another group owns is refused by the `read` tool, so the
groups follow genuinely new paths instead of overlapping. Reports are folded in
windows of 4 as they land. With eight or more explorer groups, each completed
window is submitted immediately to an independent level-1 compiler pool; the
level-2 merge waits only for those completed folds. With fewer than eight groups,
raw reports go directly to the level-2/final compiler. Inputs are partitioned only
when they exceed the configured model context; every slice is sent in full and no
source text is discarded. Each fold streams `reports_folded` (`reports`, `chars`).

A lead-agent call that still fails (proxy reset, model crash) no longer throws the run
away: after `WIKI_LLM_MAX_RETRIES` connection retries it emits `lead_failed` and answers
straight from the seed evidence and the compiled report, with the sweep's page ids as
citations, instead of ending in `agent run failed`.

Jev is **disabled by default**; when disabled, unavailable, or failing, the
existing ES → index map → router path runs unchanged (a `jev_unavailable` SSE
event is emitted and Elasticsearch remains the recall fallback).

```bash
# local model: checks the Hugging Face cache, then downloads before readiness
WIKI_JEV_ENABLED=1
WIKI_JEV_BACKEND=local
# optional fixed destination; omit to use the standard Hugging Face cache
WIKI_JEV_LOCAL_PATH=/opt/models/Jev-Style-0.8B-Decision-v3
# pin the GPU: a CUDA device that is missing or invisible fails at startup instead of
# scoring silently on CPU. bfloat16 halves memory and time vs the float32 default and
# drops to float16 on GPUs without bf16.
WIKI_JEV_DEVICE=cuda
WIKI_JEV_DTYPE=bfloat16

# hosted Jev-compatible scorer sidecar
WIKI_JEV_ENABLED=1
WIKI_JEV_BACKEND=hosted
WIKI_JEV_BASE_URL=http://jev-score.internal:8080   # POST {base}/score
WIKI_JEV_API_KEY=secret-if-required
```

| Variable | Meaning |
| --- | --- |
| `WIKI_JEV_THRESHOLD` | candidate gate on card scores (default `0.50`). |
| `WIKI_JEV_SEED_THRESHOLD` | full-read seed gate (default `0.80`, must be ≥ threshold). |
| `WIKI_JEV_DEVICE` | local runtime device: `auto` (default), `cuda`, `mps`, `cpu`. |
| `WIKI_JEV_DTYPE` | local runtime weights: `bfloat16` (default), `float16`, `float32`. |
| `WIKI_JEV_MODEL_REVISION` | Optional Hugging Face commit pin for the runtime and model snapshot. |
| `WIKI_JEV_MAX_BATCH_REQUESTS` | Engine request cap (default `64`). |
| `WIKI_JEV_LLM2JEV_CONCURRENCY` | concurrent single-state `/v1/systemone` calls for the `llm2jev` backend (default `20`). |
| `WIKI_JEV_RECORD` / `WIKI_JEV_RECORD_MAX` | Optional JSONL path and maximum requests to record for parity checks (default `300`). |
| `WIKI_JEV_CHUNK_TOKENS` / `WIKI_JEV_CHUNK_OVERLAP` | body chunking (defaults `25600` / `10000`). |
| `WIKI_JEV_TOC_CHUNK_TOKENS` | 目次 chunking for the rewrite stage's ranking (default `15000`), comfortably inside the model's 25,600-token input ceiling so the question head also fits. |
| `WIKI_JEV_WORKERS` | concurrent fetch+classify threads in the sweep pipeline (default `4`). The shared `jev/` engine owns model access on one worker while callers prepare requests. |
| `WIKI_JEV_SUBAGENT_GROUP_SIZE` / `WIKI_JEV_SUBAGENT_GROUPS` | seeds per seed-group subagent / max such subagents per question (defaults `5` / `8`). Size `WIKI_JEV_SUBAGENT_GROUPS × WIKI_JEV_SUBAGENT_GROUP_SIZE` pages into `WIKI_MAX_PAGE_FETCHES_PER_RUN` or the groups starve. |
| `WIKI_JEV_PREFILTER_MIN_OVERLAP` | keyword units a page must share with the rewritten question before it gets an expensive body score (default `2`, `0` = off). Dropped pages are counted in `jev_complete.prefiltered`: if that number climbs and seeds collapse, even the rewrite missed the corpus's vocabulary — lower this to `1`, then `0`. |
| `WIKI_JEV_MAX_PAGE_READS` / `WIKI_JEV_MAX_LIST_CALLS` | independent sweep safety valves; `0` = unlimited. The sweep is exhaustive and can cost many GROWI reads. |

The rewrite stage streams its own events before the sweep starts, so it is never a
silent wait: `jev_toc_scan` (`documents` found, one activity line), then one
`jev_progress` with `stage: "toc"` per ranked page (`done` / `total` / `percent` /
`document` / `probability` / `yes`), `jev_toc_chunk` when a 目次 chunk still cannot be
scored, `jev_toc` per summary call, `jev_toc_digest` (`scanned` / `kept` / `groups` /
`chars`), `jev_toc_failed` when the stage gives up and the raw question is used, and
finally `jev_query`. The progress bar relabels itself `00-目次 スキャン` while
`stage` is `toc`, so the two stages of one question are readable apart.

Sweep reads use their own budget (never the per-run `WIKI_MAX_PAGE_FETCHES_PER_RUN`)
and warm the shared page cache. The sweep streams `jev_progress`
(`done` / `total` / `percent` plus `scored`, `yes`, `mean_yes_probability` and
`mean_yes_card_probability`; the total grows as the crawl discovers work, so the
percent never recedes and ends at 100) plus the per-page `jev_gate` /
`jev_complete` / `jev_unavailable` events. The frontend renders `jev_progress` as
one fixed 250px bar that fills in place with the running average of `p(はい)` next
to it, and `jev_complete` prints a one-line
summary (`N seeds from M pages, top p=0.62`) so a sweep that clears no seed is
visible instead of silently falling back to Elasticsearch. The gate events
themselves stay invisible.
The average is over **the verdicts leaning yes only** (`p > 0.5`), because averaging
over a corpus where most pages are irrelevant just accumulates noes. Card and body
verdicts are averaged separately — a card summary and a full page body are different
distributions — and the yes *count* travels with the average, since an average over
zero yeses and an average of zero confidence would otherwise look identical. The
console gets the same numbers as a heartbeat every 25 verdicts
(`Jev sweep: 63% 646/1019 scored=612 yes=18 avg_p=0.71 card_avg_p=0.83`).
The `/score` wire contract (structured `state` + ordered `questions` → ordered
`p(はい)`) is **this service's adapter contract**, not a claim that the Hugging
Face page exposes a generic text-generation endpoint. All backends are selected
inside the shared `jev/` package; growi-search only calls its `score_many` shim.
Local mode checks a complete
`WIKI_JEV_LOCAL_PATH` first; when it is unset, it checks the standard Hugging
Face cache. Only a missing/incomplete snapshot is downloaded, synchronously at
server startup, before `/api/ready` can succeed. The local runtime dependencies
are declared by this project, and a failed download/load fails startup instead
of deferring work to the first user query.
Local mode uses the reference PyTorch kernels unless `causal_conv1d` and
`flash-linear-attention` are installed; the sweep is correct without them but
several times slower per page.

## Tests

```bash
cd growi-search
uv run python -m unittest discover -s tests -v
```

Tests use `httpx.MockTransport`, fake GROWI/reranker/LLM services injected via
`create_app(transport=…, researcher=…)`, and assert no `sqlite3`/`GraphStore`
imports anywhere in the service.

## Security notes

- The GROWI token and reranker key are read from env and **never returned** by
  any endpoint (`/api/growi` returns only url/root/doc-parser).
- Same-origin serving; **no wildcard CORS**.
- Per-request LLM overrides are gated behind the configured/own chat host and an
  SSRF blocklist (loopback / link-local / `*.local`).
- **Rotate** any GROWI token that has ever been committed before production use.
