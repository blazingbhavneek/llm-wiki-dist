# llm-wiki

## Build

```bash
cd llm-wiki-dist
docker build -t llm-wiki-rikiseisan:latest .
```

(`--build-arg PROXY_URL=` disables the baked proxy; drop it to use it.)

## Run

```bash
docker run -d --name llm-wiki-rikiseisan \
  -p 51025:8000 -p 51026:8001 -p 51024:22 \
  -v "$PWD/data:/data" \
  llm-wiki-rikiseisan:latest
```

Register one GROWI connection in `/llm-wiki/admin/`, then open
`http://localhost:8000/llm-wiki/` (redirects to `/llm-wiki/all/`). GROWI is the
wiki source of truth; SQLite (`data/graph.sqlite`) is the derived search index.
Team scopes are top-level folders under `data/mount/` or `data/raw/`, for example
`/llm-wiki/research/` and `/llm-wiki/all/`.

The same database segment selects the stateless MCP endpoint:

```text
http://localhost:8001/llm-wiki/research/mcp
http://localhost:8001/llm-wiki/all/mcp
```

MCP reads are proxied to the backend, and `queue_agent_note` submits to the
backend's write queue and returns immediately. Set `MCP_ALLOWED_WIKIS` only if
you want to restrict the available scopes.

### Env overrides (optional)

- `WIKI_DATA_ROOT` — project root containing `mount/`, `raw/`, `wiki/`, and `graph.sqlite` (default `data`)
- `WIKI_ENGINE_DB` — encrypted GROWI connection registry (default `<root>/engine.sqlite`)
- `WIKI_GROWI_NAME` — connection name; omitted when there is exactly one
- `WIKI_INGEST_MODE` — source-to-wiki mode (`wiki` for the format-aware writer)
- `WIKI_PARSER_BASE_URL` — optional doc-parser endpoint for converting `mount/`
- `WIKI_SYNC_INTERVAL_SECONDS` — automatic raw/GROWI sync interval; `0` disables the timer
- `WIKI_PREFIX` — reverse-proxy prefix (default `/llm-wiki`)
- `MCP_BACKEND_ORIGIN` — trusted `app.py` origin (default `http://127.0.0.1:8000`)
- `MCP_ALLOWED_WIKIS` — optional comma-separated MCP wiki allowlist
- Models default to `10.160.144.101` (chat 51029, embed 51024, rerank 51025).
  To point at local vllm (`./vllm_embed_reranker.sh`, embed 8081 / rerank 8082):

```bash
docker run -d --name llm-wiki-rikiseisan \
  --add-host=host.docker.internal:host-gateway \
  -p 51025:8000 -p 51026:8001 -p 51024:22 \
  -e WIKI_PREFIX="/llm-wiki" \
  llm-wiki-rikiseisan:latest
  # -v "$PWD/.wiki:/home/seigyo/llm-wiki/.wiki" \
  # -e WIKI_EMBED_BASE_URL=http://host.docker.internal:8081/v1 \
  # -e WIKI_RERANK_BASE_URL=http://host.docker.internal:8082/v1 \
```

For a local GROWI, start the companion stack with `cd ../growi-stack && docker compose up -d`.

---

# doc-parser

GPU PDF parser (MinerU 3.4.4 on the vLLM 0.21.0 CUDA 13 base image). All
MinerU models are baked into the image at build time and the running
container is configured for offline model access.

## Build

Run from the repository root, not `parser/`. The work proxy
`http://133.141.7.237:9515` is the default for every build-network operation:

```bash
docker build -f parser/Dockerfile \
  -t doc-parser-rikiseisan:latest .
```

Outside the work network, disable it with one empty build argument:

```bash
docker build --build-arg PROXY_URL= -f parser/Dockerfile \
  -t doc-parser-rikiseisan:latest .
```

Models are pulled from `opendatalab/PDF-Extract-Kit-1.0` and
`opendatalab/MinerU2.5-Pro-2605-1.2B` at their current `main` revisions.

## Run

The host needs an NVIDIA driver new enough for CUDA 13 and
`nvidia-container-toolkit`. A host CUDA toolkit is not required because the
image contains the CUDA 13 user-space stack.

```bash
docker run -d --name parser --gpus all \
  -p 127.0.0.1:8000:8000 \
  --shm-size=8g \
  doc-parser-rikiseisan:latest
```

This uses the embedded work proxy. On any machine outside the work network, add
the single empty runtime override; the entrypoint then unsets all upper- and
lower-case HTTP, HTTPS, and ALL proxy variables:

```bash
docker run -d --name parser --gpus all \
  -e PROXY_URL= \
  -p 127.0.0.1:8000:8000 \
  --shm-size=8g \
  doc-parser-rikiseisan:latest
```

API and frontend: http://localhost:8000. The health check calls `/queue`.

### Flags that matter

- `--gpus all` — required for normal parsing; startup fails clearly without a GPU.
- `--shm-size=8g` — avoids Docker's 64 MB shared-memory default for torch/vLLM.
- `-e PROXY_URL=` — disables the embedded work proxy outside that network.

### Env overrides (optional)

- `MINERU_BACKEND` — `hybrid-engine` by default; `pipeline` is useful for
  CPU-only validation.
- `MINERU_METHOD` — `auto` by default; supported values are `auto`, `txt`, and
  `ocr`.
- `MINERU_EFFORT` — `medium` by default; set `high` for higher-accuracy hybrid
  parsing with image/chart analysis.
- `PARSER_REQUIRE_GPU=0` — allows CPU/pipeline diagnostics and slow validation;
  normal hybrid-engine production parsing expects a GPU.
- `PROXY_URL` — proxy applied to all common proxy variables; empty unsets them.
- `NO_PROXY_VALUE` — default `localhost,127.0.0.1,::1`.

### Logs

Uvicorn runs directly as PID 1, so normal Docker logging and signals work:

```bash
docker logs -f parser
```

To transfer the already-built, self-contained image to an offline H200 host:

```bash
docker save -o doc-parser-rikiseisan-3.4.4-cuda13.tar \
  doc-parser-rikiseisan:3.4.4-cuda13
# Copy the tar to the server, then:
docker load -i doc-parser-rikiseisan-3.4.4-cuda13.tar
```
