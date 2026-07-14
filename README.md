# llm-wiki

## Build

```bash
cd llm-wiki-dist
DOCKER_BUILDKIT=1 docker build -t llm-wiki-rikiseisan:latest .
```

(`--build-arg PROXY_URL=` disables the baked proxy; drop it to use it.)

## Run

```bash
docker run -d --name llm-wiki-rikiseisan \
  -p 8000:8000 -p 8001:8001 -p 51027:22 \
  -v "$PWD/.wiki:/home/seigyo/llm-wiki/.wiki" \
  llm-wiki-rikiseisan:latest
```

Open http://localhost:8000/llm-wiki/ (redirects to `/llm-wiki/wiki/`).
Any URL segment picks/creates a db: `/llm-wiki/manual/`, `/llm-wiki/meetings/`.

The same database segment selects the stateless MCP endpoint:

```text
http://localhost:8001/llm-wiki/manual/mcp
http://localhost:8001/llm-wiki/meetings/mcp
```

MCP reads are proxied to the backend, and `queue_agent_note` submits to the
backend's existing per-wiki write queue and returns immediately. By default MCP
only serves existing `<db>.sqlite` files; set `MCP_ALLOW_NEW_WIKIS=1` to allow
MCP routes to create new wikis, or set `MCP_ALLOWED_WIKIS=manual,meetings` to
restrict the service to an explicit allowlist.

### Env overrides (optional)

- `WIKI_PREFIX` — reverse-proxy prefix (default `/llm-wiki`)
- `WIKI_DEFAULT_DB` — db the bare prefix redirects to (default `wiki`)
- `WIKI_DB_DIR` — dir of `<db>.sqlite` files (default `.wiki`)
- `MCP_BACKEND_ORIGIN` — trusted `app.py` origin (default `http://127.0.0.1:8000`)
- `MCP_ALLOWED_WIKIS` — optional comma-separated MCP wiki allowlist
- `MCP_ALLOW_NEW_WIKIS` — allow MCP access before the sqlite file exists (default `0`)
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
