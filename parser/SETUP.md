# Setup

## 1. Python environment

Requires Python **3.13+** and [uv](https://docs.astral.sh/uv/).

```bash
uv sync              # base install (DOCX, PPTX, XLSX, CSV)
uv sync --extra pdf  # also install MinerU for PDF parsing (large: pulls torch/vLLM)
```

MinerU occasionally lags on the newest Python. If `--extra pdf` fails to
resolve, create a separate Python 3.12 virtualenv for MinerU and point this
project at its binary instead (see `MINERU_VENV_BIN` below).

## 2. External command-line tools

The parsers shell out to these; pip cannot install them.

| Tool | Used for | Required? |
|---|---|---|
| `pandoc` | DOCX → Markdown | yes, for `.docx` |
| `libreoffice` (`soffice`) | EMF/WMF vector images → PNG, PPTX slide rendering, XLSX formula recalculation | recommended — features degrade gracefully without it |
| `mineru` | PDF → Markdown (GPU) | only for `.pdf`; installed by `uv sync --extra pdf` into the venv |

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
uv run mineru --version   # or: uv run python -c "import mineru"
```

## 3. Environment variables (all optional)

Create a `.env` in the project root (`python-dotenv` loads it automatically).

```bash
# Image-description LLM (OpenAI-compatible endpoint)
LLM_BASE_URL=http://10.160.144.101:51029/v1
LLM_API_KEY=local
LLM_MODEL=gemma-4-31B

# Override binary locations if they are not on PATH
PANDOC_COMMAND=pandoc
LIBREOFFICE_COMMAND=soffice
MINERU_COMMAND=mineru
# MINERU_VENV_BIN=/path/to/mineru-venv/bin   # optional override; auto-discovered
# from PATH, the project's .venv/venv, or this venv when unset

# PDF (MinerU) tuning
MINERU_BACKEND=pipeline
MINERU_CUDA_VISIBLE_DEVICES=1
MINERU_GPU_MEMORY_UTILIZATION=0.5
# Avoid UniMERNet cuDNN attention-plan failures; other SDPA backends stay enabled.
MINERU_DISABLE_CUDNN_SDPA=true
MINERU_TIMEOUT_SECONDS=1800
# Warm mineru-api logs (10 MiB active file plus 3 rotated backups)
MINERU_API_LOG_PATH=logs/mineru-api.log
MINERU_API_LOG_MAX_BYTES=10485760
MINERU_API_LOG_BACKUP_COUNT=3

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
