# Setup

## 1. Python environment

Requires Python **3.13+** and [uv](https://docs.astral.sh/uv/).

```bash
uv sync              # base install (all parsers; PDFs use the external API)
```

PDF parsing requires a reachable MinerU v4 API endpoint; no local MinerU
installation or GPU model is needed by this process.

## 2. External command-line tools

The parsers shell out to these; pip cannot install them.

| Tool | Used for | Required? |
|---|---|---|
| `pandoc` | DOCX → Markdown | yes, for `.docx` |
| `libreoffice` (`soffice`) | EMF/WMF vector images → PNG, PPTX slide rendering, XLSX formula recalculation | recommended — features degrade gracefully without it |

### Fedora / RHEL / CentOS (dnf)

```bash
sudo dnf install pandoc libreoffice-headless libreoffice-writer libreoffice-impress libreoffice-calc
```

`libreoffice-headless` alone is enough if you do not want the GUI packages.

### Debian / Ubuntu (apt)

```bash
sudo apt-get install pandoc libreoffice
# Smaller, GUI-free variants:
sudo apt-get install pandoc libreoffice-core libreoffice-writer \
     libreoffice-impress libreoffice-calc --no-install-recommends
```

Fonts matter for slide/PDF rendering — for Japanese documents install
`google-noto-sans-cjk-fonts` (dnf) or `fonts-noto-cjk` (apt).

### Verify

```bash
pandoc --version
soffice --version
```

## 3. Environment variables

Create a `.env` in the project root (`python-dotenv` loads it automatically).
Values already supplied in the process environment take precedence over `.env`
(`.env` is a fallback, never an override).

```bash
# Image-description LLM (OpenAI-compatible endpoint)
LLM_BASE_URL=http://10.160.144.101:51029/v1
LLM_API_KEY=local
LLM_MODEL=gemma-4-31B

# External command locations
PANDOC_COMMAND=pandoc
LIBREOFFICE_COMMAND=soffice
MINERU_API_URL=http://10.160.144.101:51020/v1
MINERU_API_TIER=advanced       # basic=hybrid-basic; standard/advanced=higher accuracy
# A failed MinerU request is retried once after 10 seconds.
MINERU_TIMEOUT_SECONDS=1800

# URL deployment prefix only (never selects behavior). Both routes are served
# under it: /parse (generic Markdown) and /parse/llm-wiki (image-unit + LLM).
URL_PREFIX=/agent/doc-parser/

# Behaviour switches
PPTX_RENDER_SLIDES=auto            # slide PNG fallback via LibreOffice
PPTX_SLIDE_DESCRIPTION_ATTEMPTS=4  # iterative judged drafts (maximum 5)
PPTX_INDIVIDUAL_IMAGE_MIN_AREA_PERCENT=1.0  # skip tiny standalone descriptions
XLSX_RECALCULATE_FORMULAS=auto     # LibreOffice recalculation pass
VECTOR_IMAGE_CONVERSION=auto       # EMF/WMF -> PNG
CSV_MAX_ROWS=5000
```

## 4. Frontend

```bash
cd frontend
npm install
npm run build     # outputs to ../static, served by the FastAPI app
```

## 5. Run

```bash
uv run python -m uvicorn server:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

## 6. Docker

The production image builds the frontend once, installs the locked Python
dependencies, includes the repository `.env` as its default configuration, and
includes Pandoc, LibreOffice (Writer/Calc/Impress/Draw), and CJK fonts. It runs
one Uvicorn worker as a non-root user; PDF parsing still uses the external
`MINERU_API_URL` service.

```bash
docker build \
  --build-arg URL_PREFIX="${URL_PREFIX:-}" \
  --build-arg HTTP_PROXY="${HTTP_PROXY:-http://133.141.7.237:9515}" \
  --build-arg HTTPS_PROXY="${HTTPS_PROXY:-http://133.141.7.237:9515}" \
  --build-arg NO_PROXY="${NO_PROXY:-localhost,127.0.0.1}" \
  -t doc-parser-pj_10002-mg37274 .
docker run -p 8000:8000 \
  -e HTTP_PROXY="${HTTP_PROXY:-http://133.141.7.237:9515}" \
  -e HTTPS_PROXY="${HTTPS_PROXY:-http://133.141.7.237:9515}" \
  -e NO_PROXY="${NO_PROXY:-localhost,127.0.0.1}" \
  doc-parser-pj_10002-mg37274
# Optional: --env-file .env or explicit `-e NAME=value` options override image defaults.
```

The image defaults to the company proxy for build-time package downloads and
runtime outbound requests. The build arguments and runtime `-e` values can be
overridden for another environment.

Pass `URL_PREFIX` as a build argument when the service is mounted below a
known path; if omitted, the frontend derives its prefix from the serving URL.
The image healthcheck calls `/health/ready`; `/health/live` is a cheap process
liveness probe. A failed Docker healthcheck marks the container unhealthy;
automatic restart on health failure is managed by the external orchestrator.

`MAX_UPLOAD_BYTES` defaults to 100 MiB and can be lowered in `.env`.
