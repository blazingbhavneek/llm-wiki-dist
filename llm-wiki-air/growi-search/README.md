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
| `GET /api/ready` | `{ready, growi, search, llm, reranker, embedder, root_path}` (capabilities reported separately). |
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
