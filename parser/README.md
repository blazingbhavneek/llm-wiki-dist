# doc-parser

An async document parsing server with stage-level resource isolation. Small
work remains on the event loop, external converters use a bounded thread
executor, MinerU uses a spawn-based GPU process, and LLM requests share an
async concurrency limit.

PDF parsing currently performs:

1. MinerU conversion in the GPU executor.
2. Local image discovery and one-time base64 encoding.
3. Concurrent image-description requests to an OpenAI-compatible local LLM.
4. Replacement of Markdown images with `<image-unit>` blocks.

DOCX parsing uses Pandoc in the shared external worker pool, then applies the
same image embedding and concurrent LLM-description pipeline as PDF parsing.

Word documents often embed EMF/WMF vector images, which Pillow cannot decode.
When LibreOffice is installed, all extracted vector images are converted to
PNG in one headless batch through the external worker pool before embedding
and description. `VECTOR_IMAGE_CONVERSION=auto` (default) falls back to the
original files, `false` disables conversion, and `required` fails the request
when conversion cannot run. The same conversion applies to PPTX and XLSX media.

XLSX parsing uses OpenPyXL and `xlsx2html` in the external worker pool. Sheets
are emitted as minimal HTML tables so merged cells retain `rowspan` and
`colspan` without carrying spreadsheet CSS into the result. Formula cells
include both their formula and Excel's saved cached result, while raster and
vector workbook images use the shared conversion and description pipeline.
Workbook image labels include the worksheet cell or cell range covered by the
image, including vector media recovered directly from the XLSX package.
CSV and TSV inputs use the same minimal HTML table representation, with their
first row emitted as header cells.

PPTX picture labels retain each picture's left/top position and dimensions as
percentages of the slide. By default, LibreOffice and PDFium also render every
complete slide to a PNG placed at the end of that slide section; those overview
images receive a second-stage synthesis prompt containing the extracted slide
text and completed individual-image descriptions. The synthesis focuses on
layout, relationships, visual roles, and the overall message without repeating
already-extracted content. Set
`PPTX_RENDER_SLIDES=false` to disable this or `required` to fail when rendering
is unavailable. `PPTX_SLIDE_RENDER_WIDTH` controls the default 1600-pixel width.

When LibreOffice is installed, XLSX formulas are recalculated first using its
headless mode. `XLSX_RECALCULATE_FORMULAS=auto` falls back to existing cached
values, `false` disables recalculation, and `required` rejects the request when
recalculation cannot run. LibreOffice results can differ for Excel-only
functions, macros, or unavailable external links.

## Setup

Install the server and optional PDF dependencies:

```bash
uv sync --extra pdf
cp .env.example .env
```

MinerU must be able to find its models and run through the `mineru` command.
`MINERU_VENV_BIN` is auto-discovered (PATH, then the project's `.venv`/`venv`,
then common venv roots); set it only to force a specific install. Also set
`MINERU_COMMAND` when the executable has a different name.
The included defaults select physical CUDA device 1, reserve a `0.1` GPU-memory
fraction, and process pages in windows of 4; all are configurable in `.env`.
Pandoc must also be installed and available as `pandoc`, or configured through
`PANDOC_COMMAND`.

Start the server:

```bash
uv run uvicorn server:app --host 0.0.0.0 --port 8000
```

## Parse a PDF

PDF responses stream invisible JSON-whitespace heartbeats while MinerU runs.
The completed response body is standard JSON, so normal clients can call
`response.json()` without parsing server-sent events:

```bash
curl -N -X POST \
  'http://127.0.0.1:8000/parse?images=true&describe_images=true' \
  -H 'X-LLM-Base-URL: http://10.160.144.101:51029/v1' \
  -H 'X-LLM-Model: gemma-4-31B' \
  -H 'X-LLM-API-Key: local' \
  -F 'file=@document.pdf'
```

DOCX uses the same endpoint and returns a normal JSON response:

```bash
curl -X POST \
  'http://127.0.0.1:8000/parse?images=true&describe_images=true' \
  -F 'file=@document.docx'
```

The three `X-LLM-*` headers override `.env` for that request. Omit them to
use `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY`. Set
`describe_images=false` to embed images without calling the LLM. Set
`images=false` to return descriptions without base64 media.

Before an image is sent to the vision endpoint it is validated, normalized to
PNG, and bounded by `LLM_IMAGE_MAX_PIXELS`. The original extracted image remains
in the returned `<image-unit>`. Unsupported vector media is retained but its
description is skipped unless a raster decoder is installed.

Internal callers can override the same defaults directly with
`LLMClient(base_url=..., api_key=..., model=...)`.

See [workers/README.md](workers/README.md) for the parser-stage APIs and
concurrency rules.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```
