# growi-search

A **read-only** search + research service over a **live GROWI** instance
(design and handoff: `../docs/new-growi-search.md`):

1. **A local Qdrant index** (file mode, no server) that a background sync builds from
   the data blocks at the bottom of every `00-目次` page, following their hash tree.
2. **A finder**: hybrid search (meaning + BM25 + exact names) → reranker → JEV yes/no
   waves over the top sections.
3. **A two-dial researcher**: quick exit for answers written in one place, otherwise
   parallel researchers per evidence thread, a lead gap check, and a 1- or 2-stage compiler,
   streamed to the browser over SSE.

There are **no write endpoints**. The bundled frontend is a read-only chat + search +
lazy folder browser.

## Layout

```
growi-search/
  app.py            FastAPI routes + SSE + prefix middleware + static frontend
  config.py         Settings.from_env() (project INI and env configuration)
  models.py         WikiPage / WikiLink / Evidence / AgentAnswer
  markdown.py       heading sections + link extraction (pure, no I/O)
  growi_client.py   sync httpx client (GROWI _api/v3 page reads + children)
  gateway.py        chat model factory, Embedder, Reranker, JEV engine
  store.py          local Qdrant index (points, hybrid search, in-memory catalog)
  sync.py           目次 hash-tree sync + revision sweep into the store
  prompts.py        JEV questions, researcher / lead check / compiler prompts (ja)
  researcher.py     finder → quick exit or research rounds → compiler (asyncio)
  frontend/         trimmed React SPA (single build entry, read-only)
  tests/            stdlib unittest + httpx.MockTransport (no network)
```

## Requirements

- Python **3.13** and [`uv`](https://docs.astral.sh/uv/); `qdrant-client` (in the root
  `pyproject.toml`).
- Node **20+** / npm to build the frontend.
- A reachable GROWI v3 instance whose `00-目次` pages were published by the current
  builder (they carry the `llm-wiki-data` block), an OpenAI-compatible chat endpoint,
  and ideally an embedding endpoint, a `/v1/rerank` endpoint and JEV.

## Configuration

Read from `growi-search/.env` only — this service shares no config file with the rest of
the repository (`override=False`, so an exported shell variable still wins). With
`WIKI_PROJECT=projectA` (or an absolute INI path) the project's `growi_url`,
`growi_token` and `settings.chat_base_url` are used.

| Variable | Default | Meaning |
| --- | --- | --- |
| `GROWI_URL` / `GROWI_TOKEN` | required | Live GROWI and a read token (`API Token:` prefix is stripped). |
| `GROWI_ATTACHMENT_TOKEN` | `GROWI_TOKEN` | Scoped token for cookie-free image downloads. |
| `GROWI_ROOT_PATH` | `/` | One project root; `/` means every first-level folder with its own `00-目次`. |
| `WIKI_CHAT_BASE_URL` / `WIKI_CHAT_MODEL` | — | Chat endpoint and model. |
| `WIKI_SEARCH_LLM_MAX_CONCURRENCY` | `4` | LLM slots this service uses on the shared server (researchers hold one each). |
| `WIKI_LLM_CONTEXT_TOKENS` | `131072` | Model window; inputs are split into complete parts only when they cannot fit, never cut. |
| `WIKI_EMBED_BASE_URL` / `WIKI_EMBED_MODEL` | — | Embedder; without it search is BM25-only. If configured but unreachable, startup fails (it would rebuild the index twice). |
| `WIKI_EMBED_QUERY_PREFIX` / `WIKI_EMBED_DOC_PREFIX` | ruri: `検索クエリ: ` / `検索文書: ` | Retrieval prefixes; changing them rebuilds the index. |
| `WIKI_RERANK_BASE_URL` / `WIKI_RERANK_MODEL` | — | Cross-encoder `/v1/rerank`; optional. |
| `WIKI_JEV_ENABLED` + `WIKI_JEV_*` | off | JEV judge (see below). |
| `WIKI_JEV_HELP_THRESHOLD` / `WIKI_JEV_DIRECT_THRESHOLD` | `0.5` / `0.8` | Question B ("helps?") and question A ("answers directly?"). |
| `WIKI_SEARCH_STORE_DIR` | `../data/growi-search-store` | Qdrant files + `state.json`; disposable, and rebuilt automatically when the embedder or the GROWI endpoint/root changes. |
| `WIKI_SEARCH_SYNC_SECONDS` / `WIKI_SEARCH_REVISION_SWEEP_SECONDS` | `30` / `300` | Root 目次 poll / page-revision sweep (hand edits). |
| `WIKI_SEARCH_POOL` / `WIKI_RERANK_POOL` | `300` / `200` | Sections pulled from Qdrant / reranked. |
| `WIKI_JEV_WAVE_SIZE` / `WIKI_JEV_MAX_WAVES` | `100` / `4` | Sections per JEV wave / most waves. |
| `WIKI_QUICK_MAX_PAGES` | `3` | Quick exit reads at most this many pages. |
| `WIKI_MAX_THREADS` / `WIKI_MAX_ROUNDS` / `WIKI_MAX_GAPS` | `12` / `3` / `6` | WHERE dial ceiling / HOW FAR dial ceiling / gaps per round. |
| `WIKI_RUN_SECONDS` | `1200` | Soft budget: no new research round starts after it. |
| `WIKI_SUBAGENT_MAX_STEPS` / `WIKI_SUBAGENT_REPORT_TOKENS` | `20` / `16384` | Researcher tool steps / output cap. |
| `WIKI_LEAD_CHECK_TOKENS` / `WIKI_REPORT_FOLD_TOKENS` / `WIKI_FINAL_COMPILER_TOKENS` | `16384` / `32768` / `65536` | Output caps only. |

Run a single uvicorn worker: local Qdrant locks its folder to one process.

## Run

```bash
# backend (serves the built frontend under WIKI_PREFIX automatically)
cd growi-search
GROWI_URL=http://127.0.0.1:3000 GROWI_TOKEN='your-local-api-token' \
  uv run uvicorn app:app --host 0.0.0.0 --port 8001
```

Then open `http://<host>:8001/growi-search/` (or `http://<host>:8001/` when
`WIKI_PREFIX=""`). `/health` is also reachable unprefixed. The first start builds the
index in the background (`index ready: …` in the log); questions asked before that see a
"索引を作成中" message.

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
rebuild. Set `VITE_API_URL` to override. The Vite dev server proxies `/api/*` to the
backend on port 8001 (`VITE_API_PROXY_TARGET` / `VITE_API_PROXY_PREFIX` to change it).

## API

All under `WIKI_PREFIX` (default `/growi-search`). No writes.

| Method & path | Purpose |
| --- | --- |
| `GET /api/ready` | `{ready, growi, search, llm, reranker, embedder, jev, index, root_path}`; `index` = `{ready, error, documents, pages, sections}`. |
| `GET /api/growi` | `{enabled, url, root_path, doc_parser_url}` — **never** the token. |
| `GET /api/settings` | Public runtime settings; secrets are excluded. |
| `GET /health` | Process liveness, also served unprefixed. |
| `GET /api/pages/children?page_id=…\|path=…` | One level of child pages; exactly one selector (400 otherwise). |
| `GET /api/document?path=…` | Document folder plus page summaries/kinds from the index. |
| `GET /api/node/{id}` | Full page view (`body`) + `links[]`. |
| `GET /api/attachment/{id}` | Authenticated proxy for a 24-character GROWI attachment ID. |
| `GET /api/search?q=&limit=` | Fast hybrid search over the local index, grouped by page (no JEV, no LLM). |
| `POST /api/ask` | `{question, overrides?}` → `{question, answer, cited_node_ids, cited_nodes, steps}`. |
| `POST /api/ask/stream` | Same, streamed: `run`, `search`, `candidates`, `jev_progress`, `jev_complete`, `map`, `route`, `subagents_spawned`, `subagent_start`, `read`, `follow_link`, `find`, `subagent_done`, `reports_folded`, `compiling`, `answer_delta`, `answer`, `cancelled`, `error`, `done`, `: ping`. |
| `POST /api/agent-runs/{run_id}/stop` | Cancel an in-flight run: every task of it stops at once (repeat-safe). |

Per-request overrides: `chat_base_url`, `chat_api_key`, `chat_model`, `chat_temperature`,
`subagent_concurrency` (never above the LLM slots), `subagent_max_steps`. Other keys the
frontend sends are ignored. Hosts outside `WIKI_ALLOWED_LLM_HOSTS` (or blocked
metadata/localhost ranges) return **400**.

## JEV

JEV ([chaoliangUNSW/Jev-Style-0.8B-Decision-v3](https://huggingface.co/chaoliangUNSW/Jev-Style-0.8B-Decision-v3))
is the finder's judge, not an answer generator. For every section of a wave it answers
two yes/no questions over one shared state, and it reads the question itself once
(how/why? full list?). Without JEV the finder hands the top 20 reranked sections to the
researchers and never takes the quick exit.

```bash
# local model (Hugging Face cache, or a fixed path)
WIKI_JEV_ENABLED=1
WIKI_JEV_BACKEND=local
WIKI_JEV_LOCAL_PATH=/opt/models/Jev-Style-0.8B-Decision-v3   # optional
WIKI_JEV_DEVICE=cuda
WIKI_JEV_DTYPE=bfloat16

# hosted: any server with the vanilla POST /v1/systemone API (e.g. stock llm2jev over sglang/vllm);
# the aliases systemone / sglang / vllm / jpt mean the same
WIKI_JEV_ENABLED=1
WIKI_JEV_BACKEND=hosted
WIKI_JEV_BASE_URL=http://127.0.0.1:8080            # /v1/systemone is appended

# llm2jev: the custom batched scorer, POST {base}/score with all questions of a state in one call
WIKI_JEV_ENABLED=1
WIKI_JEV_BACKEND=llm2jev
WIKI_JEV_BASE_URL=http://jev-score.internal:8080   # /score is appended
```

Both HTTP backends score different states at the same time, up to
`WIKI_JEV_HTTP_CONCURRENCY` (default 20; the old name `WIKI_JEV_LLM2JEV_CONCURRENCY` still
works). Engine knobs (`WIKI_JEV_MAX_BATCH_REQUESTS`, `WIKI_JEV_MAX_BATCH_TOKENS`, …) are read
by the shared `jev/` package.

## Tests

```bash
cd growi-search
uv run python -m unittest discover -s tests -v
```

## Security notes

- The GROWI token and model keys are read from env and **never returned** by any
  endpoint (`/api/growi` returns only url/root/doc-parser).
- Same-origin serving; **no wildcard CORS**.
- Per-request LLM overrides are gated behind the configured/own chat host and an
  SSRF blocklist (loopback / link-local / `*.local`).
- **Rotate** any GROWI token that has ever been committed before production use.
