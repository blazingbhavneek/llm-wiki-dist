# llm-wiki

## Build

```bash
cd llm-wiki-dist
DOCKER_BUILDKIT=1 docker build --build-arg PROXY_URL= -t llm-wiki:latest .
```

(`--build-arg PROXY_URL=` disables the baked proxy; drop it to use it.)

## Run

```bash
docker run -d --name llm-wiki \
  -p 8000:8000 -p 51027:22 \
  -v "$PWD/.wiki:/home/seigyo/llm-wiki/.wiki" \
  llm-wiki:latest
```

Open http://localhost:8000/llm-wiki/ (redirects to `/llm-wiki/wiki/`).
Any URL segment picks/creates a db: `/llm-wiki/manual/`, `/llm-wiki/meetings/`.

### Env overrides (optional)

- `WIKI_PREFIX` — reverse-proxy prefix (default `/llm-wiki`)
- `WIKI_DEFAULT_DB` — db the bare prefix redirects to (default `wiki`)
- `WIKI_DB_DIR` — dir of `<db>.sqlite` files (default `.wiki`)
- Models default to `10.160.144.101` (chat 51029, embed 51024, rerank 51025).
  To point at local vllm (`./vllm_embed_reranker.sh`, embed 8081 / rerank 8082):

```bash
docker run -d --name llm-wiki \
  --add-host=host.docker.internal:host-gateway \
  -p 8000:8000 -p 51027:22 \
  -v "$PWD/.wiki:/home/seigyo/llm-wiki/.wiki" \
  -e WIKI_EMBED_BASE_URL=http://host.docker.internal:8081/v1 \
  -e WIKI_RERANK_BASE_URL=http://host.docker.internal:8082/v1 \
  llm-wiki:latest
```

---

# doc-parser

GPU PDF parser (MinerU + vLLM). Base image `vllm/vllm-openai:v0.21.0`.

## Build

From repo root (not `parser/`):

```bash
DOCKER_BUILDKIT=1 docker buildx build \
  --build-arg PROXY_URL= \
  --build-context hf_cache="$HOME/.cache/huggingface" \
  -f parser/Dockerfile \
  -t doc-parser-rikiseisan:reduced \
  --load .
```

`hf_cache` is a named build context, not a volume: MinerU models are copied from
the host cache into the image at build time. Populate `~/.cache/huggingface`
first or the parser has no models at runtime.

## Run

Needs `nvidia-container-toolkit` on the host (driver alone is not enough — the
container's start script exits if `nvidia-smi` is missing):

```bash
sudo pacman -S nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

```bash
docker run -d --name parser --gpus all \
  -p 127.0.0.1:8000:8000 \
  -p 127.0.0.1:2222:22 \
  --shm-size=8g \
  doc-parser-rikiseisan:reduced
```

API on http://localhost:8000

### Flags that matter

- `--gpus all` — required. Startup aborts without it.
- `--shm-size=8g` — required. Torch gets 64 MB of `/dev/shm` by default and crashes.
- `-p 127.0.0.1:2222:22` — sshd runs with password auth and a password baked into
  the image. Bind to loopback; do not publish on all interfaces.

### Env overrides (optional)

- `GPU_TARGET_ALLOC_MB` — VRAM to reserve, default `7168`. Container refuses to
  start if the GPU has less total VRAM than this. `GPU_MEMORY_UTILIZATION` is
  derived as `GPU_TARGET_ALLOC_MB / total_vram_mb`.
- `MINERU_MODEL_SOURCE` — default `local` (use the baked-in `hf_cache` models).

### Logs

`uvicorn` runs inside tmux, not as PID 1 — `docker logs` shows only the startup
banner. For server output:

```bash
docker exec -it parser runuser -u seigyo -- tmux attach -t parser
```

Detach with `Ctrl-b d`.
