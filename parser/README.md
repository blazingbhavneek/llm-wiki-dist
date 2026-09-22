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
An optional multipart `manifest` JSON field can select final sheets and supply
XLSM lineage/VBA metadata. In that mode, every sheet is emitted first (split at
100 rows, 100 columns, or 100,000 rendered characters), followed by one page per
VBA procedure; downstream generation adds Japanese-named, workbook-wide
narrative pages last using the parser-created parts as indivisible planning
units and worksheet-cell provenance.
Manifested XLSM files skip LibreOffice recalculation so no macro-capable
application opens them.
CSV and TSV inputs use the same minimal HTML table representation, with their
first row emitted as header cells.

PPTX picture labels retain each picture's left/top position and dimensions as
percentages of the slide. By default, LibreOffice and PDFium also render every
complete slide to a PNG placed at the end of that slide section; those overview
images receive a second-stage synthesis prompt containing the extracted slide
text and completed individual-image descriptions. The synthesis focuses on
layout, relationships, visual roles, and the overall message without repeating
already-extracted content. Composition-rich slides produce four iterative drafts; a
vision judge scores missing interactions and feeds its criticism into later
drafts before the best-scoring description is selected. Byte-identical pictures
are described once; tiny icons or visual fragments are omitted as standalone
images and left to the complete-slide description. Set
`PPTX_SLIDE_DESCRIPTION_ATTEMPTS` from 1 to 5 to set the
draft count, and use `PPTX_INDIVIDUAL_IMAGE_MIN_AREA_PERCENT` to adjust the
default 1% slide-area cutoff for individual descriptions. Set
`PPTX_RENDER_SLIDES=false` to disable this or `required` to fail when rendering
is unavailable. `PPTX_SLIDE_RENDER_WIDTH` controls the default 1600-pixel width.

When LibreOffice is installed, XLSX formulas are recalculated first using its
headless mode. `XLSX_RECALCULATE_FORMULAS=auto` falls back to existing cached
values, `false` disables recalculation, and `required` rejects the request when
recalculation cannot run. LibreOffice results can differ for Excel-only
functions, macros, or unavailable external links.

## Setup

Install the server dependencies:

```bash
uv sync
cp .env.example .env
```

Configure `MINERU_API_URL` to point at your running MinerU v4 API service. PDF
jobs are uploaded directly to that endpoint; this application does not start a
MinerU service or local CLI. `MINERU_API_TIER=advanced` is the accuracy-first
default; `basic` is the closest v4 equivalent to hybrid-basic. A failed API
request is retried once after 10 seconds.
Pandoc must also be installed and available as `pandoc`, or configured through
`PANDOC_COMMAND`.

Start the server:

```bash
uv run uvicorn server:app --host 0.0.0.0 --port 8000
```

## Routes and profiles

`URL_PREFIX` (for example `/agent/doc-parser`) is a deployment mount point only.
It never selects behavior; two explicit routes do:

- `POST /agent/doc-parser/parse` — generic Markdown. Ordinary Markdown images
  as `![alt](data:...)` URLs, with optional LLM-generated detailed alt text
  when `describe_images=true`; it never emits image-unit or image-description
  blocks and does not apply llm-wiki image-selection logic. The option is
  disabled by default. A non-empty `manifest` is rejected with HTTP 400.
- `POST /agent/doc-parser/parse/llm-wiki` — the historical pipeline: image-unit
  blocks, LLM descriptions, PPTX judge/revision loop, XLSM manifests, splitting,
  lineage, and `vba://` links.

Both routes return the same JSON contract:

```json
{
  "markdown": "full-document Markdown",
  "pages": ["ordered page/sheet/unit Markdown strings"],
  "parser": "pptx",
  "image_count": 2,
  "duration_s": 1.23,
  "meta": {}
}
```

PDF extraction uses the same MinerU v4 tier for both profiles. The
returned Markdown embeds extracted images as data URLs. Requests using
`describe_images=false` perform no LLM image-description work.

`pages` is always present and always a list. DOCX and CSV return `[]`. PDF
yields one item per PDF page, PPTX one per slide, XLSX one per worksheet, and
XLSM every worksheet followed by one consolidated VBA-code page and one
VBA-final-output page. Generic `pages` contain the same data-URL image form as
`markdown`; llm-wiki `pages` contain the same image-unit form. `image_count`
always describes `markdown`, never the sum across `pages`.

Generic request (no LLM):

```bash
curl -X POST 'http://127.0.0.1:8000/parse' \
  -F 'file=@document.pptx'
```

llm-wiki request:

```bash
curl -X POST \
  'http://127.0.0.1:8000/parse/llm-wiki?images=true&describe_images=true' \
  -H 'X-LLM-Base-URL: http://llm.example/v1' \
  -H 'X-LLM-Model: model-name' \
  -F 'file=@document.pptx'
```

The graph pipeline migrates to `/parse/llm-wiki` so its downstream lineage,
splitting, and description behavior remains unchanged.

## Parse a PDF

PDF responses stream invisible JSON-whitespace heartbeats while MinerU runs.
The completed response body is standard JSON, so normal clients can call
`response.json()` without parsing server-sent events:

```bash
curl -N -X POST \
  'http://127.0.0.1:8000/parse/llm-wiki?images=true&describe_images=true' \
  -H 'X-LLM-Base-URL: http://10.160.144.101:51029/v1' \
  -H 'X-LLM-Model: gemma-4-31B' \
  -H 'X-LLM-API-Key: local' \
  -F 'file=@document.pdf'
```

DOCX uses the llm-wiki route too and returns a normal JSON response:

```bash
curl -X POST \
  'http://127.0.0.1:8000/parse/llm-wiki?images=true&describe_images=true' \
  -F 'file=@document.docx'
```

The three `X-LLM-*` headers override `.env` for that request. Omit them to
use `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY`. Set
`describe_images=false` to embed images without calling the LLM. This option is
available on both routes and defaults to false for the generic route. Set
`images=false` to return descriptions/alt text without base64 media.

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
