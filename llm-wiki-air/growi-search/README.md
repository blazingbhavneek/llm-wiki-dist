# growi-search

A **read-only**, self-contained search + research service over a **live GROWI**
instance. It trims the heavier `llm-wiki` graph service down to exactly three
things:

1. **Live GROWI keyword search + page reads** (no SQLite, no local index, no
   vector store — every read hits GROWI through its REST API).
2. **A reranker-augmented retrieval pipeline** (GROWI Elasticsearch order,
   optionally re-ranked by a `/v1/rerank` server, then optional section-level
   rerank of a couple of hydrated pages).
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
  endpoint. A reranker server is optional — when absent, search keeps
  Elasticsearch order.

## Setup

```bash
cd growi-search
uv sync                       # creates .venv from pyproject.toml
cp /dev/null .env             # optional service-local overrides
```

Configuration is read from an optional `growi-search/.env` first, then the
repository `.env` supplies any missing values. Minimal required set:

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
| `WIKI_RERANK_BASE_URL` | no | `/v1/rerank` server; if absent, ES order is kept. |
| `WIKI_EMBED_BASE_URL` / `WIKI_EMBED_MODEL` | no | OpenAI-compatible embeddings for index-card ranking; keyword overlap is used when absent. |
| `WIKI_INDEX_PAGE_NAME` | no | Published index page name; default `00-目次` (what `main.py index` writes). |
| `WIKI_INDEX_CACHE_TTL` / `WIKI_INDEX_MAP_TOP_K` / `WIKI_INDEX_MAP_EMBED_K` | no | Index-map refresh interval and ranking limits. |
| `WIKI_ALLOWED_LLM_HOSTS` | no | Optional comma-separated allowlist for per-request LLM override hosts; the configured chat host is always allowed. |
| `WIKI_PREFIX` | no | Public path prefix; default `/growi-search`. |

The full, documented env contract (candidates, top-k, budgets, cache, subagent
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
   `/_api/search` call, ranks the `<title>\n<path>\n<snippet>` candidates with
   the reranker (ES order if unavailable), and returns snippets — never page
   bodies.
1. The optional index map reads the root `/<root>/00-目次` and each document's
   `<document>/00-目次`. It ranks page cards by embedding cosine plus reranking,
   or by term overlap plus reranking when embeddings are unavailable. Index
   refreshes are process-local and do not consume per-run page budgets.
2. `/api/ask` runs the router: `shallow` answers from ≤ `WIKI_SHALLOW_PAGE_READS`
   hydrated pages (section-reranked evidence); `deep` hands the lead agent the
   candidates. Failed hydration falls back to deep.
3. The lead agent (`search` / `explore` / `finish`) spawns bounded parallel
   subagents (`read` / `follow_link` **outgoing-only** / `search` / `finish`).
   Every page body and ES call is charged to a shared per-run budget
   (`WIKI_MAX_PAGE_FETCHES_PER_RUN`, `WIKI_MAX_SEARCH_CALLS_PER_RUN`); duplicate
   reads/searches are memoized and a process-local TTL page cache (
   `WIKI_PAGE_CACHE_*`) is shared across subagents. Nothing is persisted to disk.

## Jev relevance gate (optional)

Jev ([chaoliangUNSW/Jev-Style-0.8B-Decision-v3](https://huggingface.co/chaoliangUNSW/Jev-Style-0.8B-Decision-v3))
is a per-question **relevance gate**, not an answer generator. When enabled,
each `/api/ask` question first runs an exhaustive sweep of every visible
document: `00-目次` cards are batch-scored, candidates are confirmed by full
page reads (chunked to the model's 25,600-token input ceiling with the
configured overlap), and entity-defining card edges are followed across
documents. Confirmed pages become seeds for the existing lead/subagent flow.
Before anything is scored, the question is **rewritten against the whole wiki** in
two stages, so the sweep is written from what the entire wiki covers:

1. **Find every 目次** — the tree is walked to **every depth** (cycles and the
   sweep's own list-call budget apply) and every `00-目次` / `…一覧` page found is
   read whole. Subdirectories carry their own `00-目次`, so a first-level scan
   describes one chapter and reports the rest of the wiki as 関連なし.
2. **Per-目次 summary** — one LLM call per index page, in parallel
   (`WIKI_JEV_WORKERS` at a time), each answering what that part of the wiki can
   provide for this question, in the corpus's own words. Nothing is truncated before
   its summary is written, so no document can be crowded out of the prompt.
3. **Rewrite** — one call over all of those notes, producing a single page-target
   question in the corpus's own vocabulary. That text — not the original question —
   is what every page is asked and what the keyword gate compares against, which is
   how an English question can still match a Japanese manual.

Each summary is printed and streamed as it lands (`JEV 目次 digest: 24 目次 …`, then
`JEV に投げる質問…`), so you can read the whole-wiki picture before the run finishes.
The gate question asks whether a page **contains the answer**, never whether it
is merely *related* — relatedness makes a single-domain corpus answer yes to
everything, which is how a "list every function" question ends up with 950 seeds.
Before a page is allowed to cost a body-score call it must also share keywords
with the rewritten question, so the sweep spends its model calls on plausible
pages only.
Crawling and classification are pipelined: one thread enumerates documents,
cards and pages and publishes full-read targets on a bounded queue while
`WIKI_JEV_WORKERS` threads fetch and classify, so GROWI traversal and page reads
overlap classification instead of running before it.

Confirmed seeds then drive the research directly: they are grouped by document
and sliced into at most `WIKI_JEV_SUBAGENT_GROUPS` groups of at most
`WIKI_JEV_SUBAGENT_GROUP_SIZE` seeds, and each group gets its own subagent
(running `WIKI_SUBAGENT_CONCURRENCY` at a time). A group only ever sees its own
seeds; reading a page another group owns is refused by the `read` tool, so the
groups follow genuinely new paths instead of overlapping. The merged reports plus
the seed blocks become the lead agent's context.

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
| `WIKI_JEV_DEVICE` | local runtime device: `auto` (default), `cuda`, `mps`, `cpu`. Set `cuda` in production so a GPU that disappears is a startup error, not a 30× slowdown. |
| `WIKI_JEV_DTYPE` | local runtime weights: `bfloat16` (default), `float16`, `float32`. bf16 becomes fp16 automatically when the GPU has no bf16. Questions are scored one per forward pass — the GPU is the speed lever, not batching. |
| `WIKI_JEV_CHUNK_TOKENS` / `WIKI_JEV_CHUNK_OVERLAP` | body chunking (defaults `25600` / `10000`). |
| `WIKI_JEV_BATCH_SIZE` | cards per batched score call (default `64`). |
| `WIKI_JEV_WORKERS` | concurrent fetch+classify threads in the sweep pipeline (default `4`). The local adapter serializes model calls, so extra workers overlap GROWI reads, not scoring; a hosted adapter gains real parallel scoring. |
| `WIKI_JEV_SUBAGENT_GROUP_SIZE` / `WIKI_JEV_SUBAGENT_GROUPS` | seeds per seed-group subagent / max such subagents per question (defaults `5` / `8`). Size `WIKI_JEV_SUBAGENT_GROUPS × WIKI_JEV_SUBAGENT_GROUP_SIZE` pages into `WIKI_MAX_PAGE_FETCHES_PER_RUN` or the groups starve. |
| `WIKI_JEV_PREFILTER_MIN_OVERLAP` | keyword units a page must share with the rewritten question before it gets an expensive body score (default `2`, `0` = off). Dropped pages are counted in `jev_complete.prefiltered`: if that number climbs and seeds collapse, even the rewrite missed the corpus's vocabulary — lower this to `1`, then `0`. |
| `WIKI_JEV_MAX_PAGE_READS` / `WIKI_JEV_MAX_LIST_CALLS` | independent sweep safety valves; `0` = unlimited. The sweep is exhaustive and can cost many GROWI reads. |

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
The `/score`
wire contract (structured `state` + ordered `questions` → ordered `p(はい)`)
is **this service's adapter contract**, not a claim that the Hugging Face page
exposes a generic text-generation endpoint. Local mode checks a complete
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
