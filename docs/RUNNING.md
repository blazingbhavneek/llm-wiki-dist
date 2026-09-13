# Running LLM-Wiki

This is the operator guide for the current repository. It describes the
implemented data-root, GROWI, format-aware wiki, and graph-ingestion flow.

The short version is:

~~~text
mounted files -> parser -> raw Markdown + raw Git commit
             -> wiki writer -> data/wiki + GROWI pages
             -> GROWI sync -> graph.sqlite nodes/edges/FTS5/sqlite-vec
             -> scoped search/research/UI/MCP
~~~

GROWI is the editable wiki source of truth after publication.
graph.sqlite is a rebuildable search and graph projection.
data/wiki is the local generated/export copy. The system intentionally uses
SQLite plus sqlite-vec; do not configure Qdrant, LanceDB, or another vector
database for this phase.

For design rationale and phase details, see:

- PLAN_SYNC.md
- PLAN_FORMATS.md
- PLAN_GROWI.md
- PLAN_NEO.md

## 1. Repository locations

The repository has two important working directories:

~~~text
/mnt/common/Code/llm-wiki-dist/
├── docs/                    this documentation and implementation plans
├── growi-stack/             MongoDB + GROWI Docker Compose project
├── data/                    runtime/project data; normally persistent
├── parser/                  document-to-Markdown parser service
└── llm-wiki-dist/           Python API, graph engine, writer, and frontend
~~~

Commands in this document use the absolute paths above only to make the
working directory unambiguous. Replace them with the path used on another
machine.

## 2. Prerequisites

Install or provide:

- Python 3.13 or newer.
- uv for the Python environment.
- Docker and Docker Compose for GROWI/MongoDB.
- A reachable OpenAI-compatible chat endpoint.
- A reachable embedding endpoint.
- A reachable reranker endpoint, unless the configured backend does not use
  one.
- A GPU-capable Docker runtime for the full parser/Dockerfile path. The parser
  can be started with PARSER_REQUIRE_GPU=0 for diagnostics, but real
  conversion may require the configured MinerU/vision stack.

The Python package uses unittest. It does not require pytest.

Check the basic tools:

~~~bash
python3 --version
uv --version
docker --version
docker compose version
~~~

## 3. Python environment

Create or synchronize the application environment:

~~~bash
cd /mnt/common/Code/llm-wiki-dist/llm-wiki-dist
uv sync --locked
~~~

The existing .venv is also usable directly:

~~~bash
.venv/bin/python --version
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
~~~

The application imports .env through python-dotenv, but exporting the values
in the shell is useful for subprocesses and Docker-related commands:

~~~bash
set -a
source .env
set +a
~~~

Never commit .env, API tokens, or generated data/ runtime state.

## 4. Environment variables

Create llm-wiki-dist/.env locally. A safe starting shape is:

~~~dotenv
# Project and routing
WIKI_DATA_ROOT=/mnt/common/Code/llm-wiki-dist/data
WIKI_PREFIX=/agent/llm-wiki
WIKI_GROWI_NAME=local
WIKI_SYNC_INTERVAL_SECONDS=300

# GROWI registry encryption. Use a long random value and keep it stable.
WIKI_SECRET_KEY=replace-with-a-private-random-secret

# Chat model: OpenAI-compatible /v1 endpoint
OPENAI_BASE_URL=http://127.0.0.1:8000/v1
OPENAI_API_KEY=replace-me
WIKI_MODEL=your-chat-model

# Embeddings
WIKI_EMBED_BACKEND=server
WIKI_EMBED_BASE_URL=http://127.0.0.1:8001/v1
WIKI_EMBED_API_KEY=local
WIKI_EMBED_MODEL=your-embedding-model

# Reranking
WIKI_RERANK_BACKEND=server
WIKI_RERANK_BASE_URL=http://127.0.0.1:8002/v1
WIKI_RERANK_API_KEY=local
WIKI_RERANK_MODEL=your-reranker-model

# Parser; leave empty only when raw Markdown is supplied manually.
WIKI_PARSER_BASE_URL=http://127.0.0.1:8003

# Writer mode
WIKI_INGEST_MODE=wiki
WIKI_OUTPUT_LANGUAGE=Japanese (日本語)
~~~

The names used in the checked-in local configuration may differ from the
placeholders above. Keep the existing working model names and URLs if those
services are already running.

### Required values

WIKI_DATA_ROOT must be the project directory containing mount, raw, metadata,
and wiki. It is not the data/mount directory itself.

WIKI_SECRET_KEY is required before registering a GROWI connection. It encrypts
the stored GROWI API token in engine.sqlite. Generate a value with:

~~~bash
cd /mnt/common/Code/llm-wiki-dist/llm-wiki-dist
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
~~~

Keep this key. If it changes, the registry cannot decrypt the saved token and
the connection must be registered again.

OPENAI_BASE_URL and WIKI_MODEL configure the chat/model gateway. The embedding
and reranker settings configure retrieval. They must describe
OpenAI-compatible HTTP APIs; they are not GROWI credentials.

### Useful runtime settings

The full mapping is in graph/core.py, Settings.from_env, approximately lines
187-348. The settings most often changed by operators are:

| Variable | Purpose |
| --- | --- |
| WIKI_INGEST_MODE | chunks, pages, or wiki for ordinary Markdown/documents |
| WIKI_PAGE_STITCH | Allow the page writer to stitch related page fragments |
| WIKI_SECTION_TARGET_LINES | Target size of a wiki section rewrite |
| WIKI_WRITE_ATTEMPTS | Writer validation/retry count |
| WIKI_REWRITE_CONCURRENCY | Concurrent section rewrite calls |
| WIKI_STRUCTURE_TARGET_LINES | Format-aware structural page target |
| WIKI_STRUCTURE_MIN_LINES | Minimum structural page size |
| WIKI_SLIDE_DELIMITER | PPTX-to-Markdown slide delimiter |
| WIKI_SLIDE_TITLE | Whether PPTX slide titles are headings |
| WIKI_PDF_USE_HEADINGS | Opt into heading-based PDF page planning |
| WIKI_TABULAR_PREVIEW_ROWS | Rows sent to the Excel/CSV structure judge |
| WIKI_TABULAR_PREVIEW_COLS | Columns sent to the structure judge |
| WIKI_TABULAR_SLICE_RECORDS | Records stored per table analysis slice |
| WIKI_INGEST_CONCURRENCY | Graph node preparation concurrency |
| WIKI_RECLUSTER_EVERY | Recluster cadence after changed GROWI documents |
| WIKI_SYNC_INTERVAL_SECONDS | Background sync period; 0 disables the timer |
| WIKI_AGENT_MAX_STEPS | Lead research-agent step budget |
| WIKI_SUBAGENT_COUNT | Number of research subagents |
| WIKI_SUBAGENT_CONCURRENCY | Concurrent subagent limit |
| WIKI_SERVICE_MAX_AGENTS | Server-level concurrent agent limit |

The browser settings page can patch many of these at runtime. A runtime patch
does not replace .env; restarting or using settings reset returns to
Settings.from_env values.

### SQLite vector backend

The current backend is SqliteVecIndex in graph/vectors.py lines 31-92,
backed by vector virtual tables inside graph.sqlite. The QdrantIndex class
below it is retained for compatibility tests/callers, but it is not part of
the supported runtime path. Do not set WIKI_VECTOR_BACKEND=qdrant; do not add
a Qdrant container for this phase.

The practical reset/rebuild consequence is simple: preserve engine.sqlite and
GROWI, and graph.sqlite can be rebuilt from GROWI without adopting a second
database.

## 5. Data root and mount layout

data/ is one project root. The normal layout is:

~~~text
data/
├── mount/                         source files mounted/copied here
│   └── test/                      team/scope name
│       ├── csv/
│       ├── docx/
│       ├── pdf/
│       ├── pptx/
│       └── xlsx/
├── raw/                           parser Markdown; Git repository
│   └── test/
│       ├── csv/csv_long_lines_csv.md
│       ├── docx/Input1_docx.md
│       └── ...
├── wiki/                          locally generated Markdown wiki
│   ├── index.md
│   └── test/
│       └── docx/Input1.docx/
│           ├── 001-*.md
│           └── _planning/
├── metadata/
│   ├── convert.json                mount conversion ledger
│   ├── last_sha                    last raw Git commit consumed
│   ├── state/                       resumable writer state
│   └── work/                        temporary writer output
├── graph.sqlite                    derived nodes/edges/search/vectors
├── graph.sqlite-wal                transient SQLite WAL, if present
├── graph.sqlite-shm                transient SQLite shared memory, if present
└── engine.sqlite                   encrypted GROWI registry and page ledger
~~~

The path rules are implemented in graph/project.py:

- A mounted Input1.docx becomes raw/.../Input1_docx.md.
- The raw name Input1_docx.md maps to the wiki folder Input1.docx.
- The same mapping is used for metadata/state and metadata/work.
- The last path segment is kept as the source document; subdirectories before
  it are preserved.

Use normal filesystem operations to add or update files under mount/. Do not
edit generated files under wiki/ as the normal source-edit workflow. Use
GROWI for human wiki edits, or change the mounted source and sync again.

### Scope/team rules

The first directory under mount/ or raw/ is a project team/scope:

~~~text
data/mount/test/docx/Input1.docx
              └── test = scope/team
~~~

The application exposes:

~~~text
<PREFIX>/all/       aggregate read scope
<PREFIX>/test/      test-only read/write scope
<PREFIX>/admin/     prefix-level administration
~~~

all is not a writable team. Uploads, exogenous notes, and other scoped writes
must be sent while the browser is inside a team route. The aggregate route is
intentionally read-only for those operations.

Reserved names all, admin, and assets are not treated as teams.

### No project/team scope

There are two useful interpretations of “global”:

1. **See every project:** use the /all/ route. This is the recommended normal
   operation when multiple team folders exist.
2. **Do not have a team folder at all:** put files directly below mount/, and
   let the converter create raw files directly below raw/.

Root-level raw files are internally assigned the fallback team name general.
There is no /general/ URL unless a real general/ directory is present, so
access them through /all/. The app upload endpoint cannot write to /all/; it
requires a team route. If browser uploads are needed for a single global
collection, use a real global/ directory and open /global/.

For a reliable operational setup, even a one-project installation should use
one explicit folder such as global/ or test/. This keeps uploads, GROWI path
boundaries, ZIP export, and scope filtering unambiguous.

## 6. Start GROWI

Start the bundled MongoDB and GROWI stack:

~~~bash
cd /mnt/common/Code/llm-wiki-dist/growi-stack
docker compose up -d
docker compose ps
~~~

Open http://localhost:3000. On first boot:

1. Complete the GROWI setup wizard.
2. Create or choose the operator account.
3. Create an API token from the GROWI user/admin interface.
4. Keep the token private; it is sent only to the backend registration API.

The compose file uses:

- MongoDB 6 in the mongo_data volume.
- GROWI in the growi_data volume.
- GROWI local file upload mode.
- Port 3000 on the host.

The compose file does not add Elasticsearch. Local graph search uses SQLite
FTS5 and sqlite-vec.

Useful commands:

~~~bash
docker compose logs -f growi
docker compose logs -f mongo
docker compose restart growi
docker compose stop
~~~

Do not use docker compose down -v unless you intentionally want to delete the
MongoDB/GROWI volumes and all wiki history.

## 7. Register GROWI with LLM-Wiki

The registry lives in data/engine.sqlite. The API token is encrypted with
WIKI_SECRET_KEY; the API never returns the token to the browser.

Start the backend first, then register the connection. Set the base URL
variables once:

~~~bash
APP=http://127.0.0.1:51023/agent/llm-wiki
ADMIN_PASSWORD='the-value-used-by-your-admin-auth'
~~~

The exact host port and prefix are determined by WIKI_PREFIX and the uvicorn
port. Register local with a GROWI write boundary:

~~~bash
curl -f -X POST "$APP/admin/api/connections/local" \
  -H "X-Admin-Password: $ADMIN_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "http://127.0.0.1:3000",
    "api_token": "replace-with-your-growi-api-token",
    "mode": "attach",
    "root_path": "/",
    "write_path": "/inbox"
  }'
~~~

Replace the example token. attach means the application may publish only below
write_path. Use own only when the application is explicitly responsible for
its configured root_path/write boundary.

Test the connection:

~~~bash
curl -f -X POST "$APP/admin/api/connections/local/test" \
  -H "X-Admin-Password: $ADMIN_PASSWORD"
curl -f "$APP/admin/api/status" \
  -H "X-Admin-Password: $ADMIN_PASSWORD" | jq
~~~

If the application is running in Docker, 127.0.0.1:3000 inside the app
container is not the host's GROWI. Use the Compose service name on a shared
Docker network, or use the host gateway address. The same rule applies to
chat, embedding, reranker, and parser URLs.

## 8. Start the parser

Build from the repository root:

~~~bash
cd /mnt/common/Code/llm-wiki-dist
docker build -f parser/Dockerfile -t doc-parser-rikiseisan:latest .
~~~

Run the GPU parser on host port 8000:

~~~bash
docker run -d --name parser --gpus all \
  -p 127.0.0.1:8000:8000 \
  --shm-size=8g \
  doc-parser-rikiseisan:latest
~~~

If the model/API is outside the container, pass the corresponding model
environment variables and proxy/no-proxy configuration. For a CPU-only
startup diagnostic, use the image's supported PARSER_REQUIRE_GPU=0 setting;
expect reduced or unavailable conversion capability.

The parser's relevant endpoints are:

~~~bash
curl -f http://127.0.0.1:8000/queue | jq
curl -f http://127.0.0.1:8000/status/<task-id> | jq
~~~

The parser's PDF queue endpoints are not the same as the LLM-Wiki sync queue.
The normal LLM-Wiki WIKI_PARSER_BASE_URL conversion path calls the parser's
/parse endpoint directly for each mount file.

## 9. Start the application

### Host development run

Run the backend from the application directory:

~~~bash
cd /mnt/common/Code/llm-wiki-dist/llm-wiki-dist
set -a; source .env; set +a
.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 51023
~~~

On startup the app:

1. Reads Settings and the encrypted GROWI registry.
2. Requires a registered GROWI connection.
3. Opens one writable GraphStore for the aggregate index.
4. Starts the write queue and background enrichment.
5. Bootstraps the graph schema/vector/search state.
6. Queues sync_raw, followed by sync_growi.
7. Creates read-only scoped stores lazily for team routes.
8. Runs the sync timer unless WIKI_SYNC_INTERVAL_SECONDS=0.

The default URL for this example is:

- all scopes: http://127.0.0.1:51023/agent/llm-wiki/all/
- test scope: http://127.0.0.1:51023/agent/llm-wiki/test/
- admin: http://127.0.0.1:51023/agent/llm-wiki/admin/

Use the actual prefix in your .env.

### Health and readiness

~~~bash
curl -s "$APP/all/api/ready" | jq
curl -s "$APP/all/api/scopes" | jq
curl -s "$APP/all/api/health" | jq
curl -s "$APP/all/api/growi" | jq
~~~

/api/ready may return 503 while model/GROWI bootstrap is still running. Wait
until ready is true. A failed bootstrap can be retried with:

~~~bash
curl -f -X POST "$APP/all/api/admin/restart-bootstrap"
~~~

### Docker application run

Build the app image from llm-wiki-dist/:

~~~bash
cd /mnt/common/Code/llm-wiki-dist/llm-wiki-dist
docker build -t llm-wiki-rikiseisan:latest .
~~~

Run with the persistent project root mounted at /data:

~~~bash
docker run -d --name llm-wiki-rikiseisan \
  -p 51025:8000 \
  -p 51026:8001 \
  -p 51024:22 \
  -v /mnt/common/Code/llm-wiki-dist/data:/data \
  --env-file .env \
  llm-wiki-rikiseisan:latest
~~~

The container exposes:

- backend HTTP on container port 8000;
- MCP on container port 8001;
- SSH on container port 22.

The values in .env must be reachable from inside the container. In
particular, localhost means the app container, not the host. Prefer Docker
service names or a host gateway address for model, parser, and GROWI URLs.

The image starts uvicorn and the MCP server in a tmux session named backend:

~~~bash
docker logs -f llm-wiki-rikiseisan
docker exec -it llm-wiki-rikiseisan tmux attach -t backend
docker exec -it llm-wiki-rikiseisan tmux ls
~~~

The host URL for the example mapping is
http://127.0.0.1:51025/llm-wiki/all/ unless WIKI_PREFIX is overridden.

## 10. Convert mounted files to raw Markdown

mount/ is the input boundary. raw/ is the normalized Markdown boundary.
graph.convert.convert_mount() performs this work.

For each supported mount file it:

1. Computes a relative path and file mtime/size key.
2. Calls the parser's /parse endpoint with image/description options.
3. Writes Markdown using the _ext convention.
4. Records the conversion key in metadata/convert.json.
5. Commits changed raw files to the data/raw/.git repository.
6. Removes raw files whose mount source disappeared.

Unchanged files are skipped. Unsupported files are recorded as unsupported;
transient failures remain eligible for a later retry.

### Normal command: API sync

The supported operator command is one sync request:

~~~bash
curl -f -X POST "$APP/test/api/sync" \
  -H 'Content-Type: application/json' \
  -d '{"ingest_mode":"wiki"}' | jq
~~~

The response is the first queued job. Poll it until it is done:

~~~bash
JOB_ID='<job-id-from-the-response>'
watch -n 2 "curl -s '$APP/test/api/write-jobs/$JOB_ID' | jq"
~~~

The endpoint queues both jobs in order:

~~~text
sync_raw    convert mount -> raw, write wiki, publish generated pages to GROWI
sync_growi  read changed GROWI folders -> revise graph nodes/edges/vectors
~~~

The ingest_mode body value overrides the configured default for that sync. If
omitted, WIKI_INGEST_MODE is used.

### Manual raw Markdown path

If a parser is not available, put Markdown directly into raw/ using the raw
naming convention, then commit it:

~~~bash
mkdir -p /mnt/common/Code/llm-wiki-dist/data/raw/test/md
cp notes.md /mnt/common/Code/llm-wiki-dist/data/raw/test/md/notes.md
cd /mnt/common/Code/llm-wiki-dist/llm-wiki-dist
.venv/bin/python -c \
  'from graph.project import Project; from graph.sync import commit_raw; commit_raw(Project("/mnt/common/Code/llm-wiki-dist/data"), "manual raw Markdown")'
~~~

Then call /api/sync. For a source named notes.docx, the expected raw name is
notes_docx.md; this suffix selects DOCX-specific writer logic.

### Direct converter call (advanced)

The application path is preferred because it also publishes and ingests. For
conversion-only diagnostics:

~~~bash
cd /mnt/common/Code/llm-wiki-dist/llm-wiki-dist
set -a; source .env; set +a
.venv/bin/python - <<'PY'
from pathlib import Path
from graph.convert import convert_mount
from graph.core import Settings
from graph.project import Project

settings = Settings.from_env()
project = Project(Path(settings.data_root)).ensure()
print(convert_mount(project, parser_base_url=settings.parser_base_url, settings=settings))
PY
~~~

This command only creates raw Markdown and conversion metadata. Follow it
with the API sync to generate wiki pages, publish GROWI, and update the graph.

## 11. Wiki generation and ingest modes

The writer dispatch is in graph/writers.py:

- wiki: the lossless section-wise writer in graph/wiki/pipeline.py. It
  observes overlapping windows, compiles a source-covering seed plan, rewrites
  sections, restores protected images, validates coverage, and writes
  references/navigation/index metadata.
- pages: the shelf/router/stitch pipeline in graph/pages.py.
- chunks: the historical concept/chunk writer in graph/chunk.py.
- xlsx and csv: format-aware tabular generation in
  graph/formats/tabular.py. Format dispatch happens before ordinary mode
  dispatch, so tabular files use the table pipeline even when the default mode
  is not wiki.

For wiki mode, output for a raw file is staged in metadata/work, then
published to:

~~~text
data/wiki/test/docx/Input1.docx/
├── 001-*.md
├── 002-*.md
└── _planning/
    ├── metadata.json
    ├── coverage.json
    ├── manifest.json
    └── source.json
~~~

The writer deletes temporary metadata/work output after publication. Resumable
per-document state remains under metadata/state.

## 12. GROWI publication and graph ingestion

After a raw change, sync_raw does the following for each changed Markdown file:

1. Deletes or invalidates old local wiki/state output when needed.
2. Generates the new local wiki folder.
3. Publishes generated pages below the registered GROWI write_path.
4. Deletes only pages previously marked as generated and no longer present.
5. Rebuilds wiki/index.md.

Then sync_growi:

1. Lists pages below the GROWI connection root_path.
2. Groups pages by their parent document folder.
3. Uses the registry page ledger in engine.sqlite to detect changed/deleted
   pages.
4. Fetches the full changed document folder.
5. Creates or revises page nodes, recognizing table pages as table nodes from
   the table-spec marker.
6. Rebuilds chain/structural relationships and derived fields.
7. Stores FTS5 text and sqlite-vec embeddings in graph.sqlite.
8. Marks old node versions stale/superseded and cascades dependent graph work
   when a document changed.

A human edit made directly in GROWI does not regenerate the source document.
It is picked up by sync_growi on the next timer/manual sync and becomes the
graph's current indexed content. Treat the original mount/raw file as the
source for converter-owned documents and GROWI as the source for human wiki
edits; choose one side for a given change to avoid overwriting it on the next
sync_raw.

The write queue serializes graph writes. Long sync jobs use incremental
commits plus a pre-job SQLite snapshot for recovery. Do not edit graph.sqlite
while the app is running.

## 13. Scope endpoints and useful API calls

Set the base URL to the prefix without the scope:

~~~bash
APP=http://127.0.0.1:51023/agent/llm-wiki
SCOPE="$APP/test"
~~~

Read endpoints:

~~~bash
curl -s "$SCOPE/api/ready" | jq
curl -s "$SCOPE/api/scopes" | jq
curl -s "$SCOPE/api/graph" > /tmp/graph.json
curl -s "$SCOPE/api/health" | jq
curl -sG "$SCOPE/api/search" --data-urlencode 'q=売上' --data-urlencode 'limit=20' | jq
curl -sG "$SCOPE/api/query" \
  --data-urlencode 'query_type=document' \
  --data-urlencode 'value=Input1.docx' | jq
curl -s "$SCOPE/api/growi" | jq
~~~

Write/maintenance endpoints:

~~~bash
curl -f -X POST "$SCOPE/api/sync" \
  -H 'Content-Type: application/json' \
  -d '{"ingest_mode":"wiki"}' | jq
curl -s "$SCOPE/api/write-jobs?limit=20" | jq
curl -s "$SCOPE/api/assimilation" | jq
curl -f -X POST "$SCOPE/api/recluster" | jq
curl -f "$SCOPE/api/wiki.zip" -o test-wiki.zip
~~~

Use the admin route for connection operations:

~~~bash
curl -s "$APP/admin/api/connections" \
  -H "X-Admin-Password: $ADMIN_PASSWORD" | jq
curl -f -X POST "$APP/admin/api/sync" \
  -H "X-Admin-Password: $ADMIN_PASSWORD" | jq
~~~

The API returns a job quickly. Always inspect the job's final status and
error, rather than treating a successful enqueue response as successful
ingestion.

## 14. Format-specific testing

### Common checks

After a sync, inspect generated files and metadata:

~~~bash
find data/wiki/test -type f -name '*.md' | sort | head -40
find data/wiki/test -type f -path '*/_planning/*' | sort | head -40
grep -R -n 'source\|coverage\|table-spec' data/wiki/test \
  --include='*.json' --include='*.md' | head -40
~~~

Check that graph nodes came from GROWI and are visible in the scope:

~~~bash
curl -s "$SCOPE/api/graph" \
  | jq '[.nodes[] | {id,type,title,document:.original_document_name,team}] | .[0:20]'
~~~

The app UI document sidebar is a folder tree. In /test/, the first folders
should be csv, docx, pdf, pptx, and xlsx; in /all/, the tree includes project
folders and then their format folders. The current right knowledge rail can
be dragged to resize, is capped at a maximum width, and scrolls horizontally
when document paths are deeper than the visible width.

### DOCX and PDF

DOCX uses heading-aware structural seeds when converted Markdown contains
usable headings. A large handbook is split by chapter/section boundaries
before the writer calls the LLM for page text. PDF uses the ordinary observed
window strategy by default; set WIKI_PDF_USE_HEADINGS=1 to opt into heading
planning when extracted Markdown has reliable headings.

~~~bash
curl -s "$SCOPE/api/search" --get \
  --data-urlencode 'q=known phrase from the document' | jq
~~~

Inspect _planning/coverage.json and manifest.json for source ranges. A
lossless wiki run must cover source ranges without silently dropping protected
image/table content.

### PPTX

PPTX is first split into slide blocks using WIKI_SLIDE_DELIMITER. A single
deck-level structural decision groups slides atomically where possible, then
the writer rewrites sections. Check that generated pages do not cut a slide
block in the middle.

### Excel and CSV

Excel files are converted into ## Sheet: sections. CSV is treated as one
table. For each sheet/table:

1. Python detects table regions and creates small row/column previews.
2. One structured LLM call decides the sheet's table structure.
3. Python validates the returned ranges.
4. After two invalid decisions, Python falls back to its deterministic
   heuristic.
5. The generated page keeps the complete original table plus statistics and a
   table-spec marker.
6. The graph stores a table node with extracted records for safe row queries.

Use a known Excel file:

~~~bash
curl -f -X POST "$SCOPE/api/sync" \
  -H 'Content-Type: application/json' \
  -d '{"ingest_mode":"wiki"}' | jq
find data/wiki/test/xlsx -maxdepth 2 -type f -name '*.md' -print
grep -R -n '<!-- table-spec:' data/wiki/test/xlsx --include='*.md' | head -20
~~~

The generated folder for xlsx_subtable_cases.xlsx is
data/wiki/test/xlsx/xlsx_subtable_cases.xlsx/.

Search for a value or header:

~~~bash
curl -sG "$SCOPE/api/search" \
  --data-urlencode 'q=some exact table header or value' \
  --data-urlencode 'limit=20' | jq
~~~

Ask a numeric row question through the research agent:

~~~bash
curl -N -X POST "$SCOPE/api/ask/stream" \
  -H 'Content-Type: application/json' \
  -d '{"question":"Which row has the largest 売上, and what is its value?"}'
~~~

When the starting/evidence node is a table, the researcher instructs the
subagent to call query_table(node_id, sql). That tool accepts one read-only
SELECT over an in-memory SQLite table named t and applies a maximum result
limit. It is not a path for arbitrary SQL against graph.sqlite.

For a direct deterministic test, use the table helper against a generated page
containing records:

~~~bash
cd /mnt/common/Code/llm-wiki-dist/llm-wiki-dist
.venv/bin/python - <<'PY'
from pathlib import Path
from graph.formats.tabular import query_records, records_from_page

page = next(Path("../data/wiki/test/xlsx").rglob("*.md"))
text = page.read_text(encoding="utf-8")
columns, records = records_from_page(text)
if columns and records:
    print(query_records(columns, records, "SELECT * FROM t LIMIT 3"))
else:
    print("no embedded table records in", page)
PY
~~~

If the selected page has no records, choose a specific page containing the
table-spec marker rather than changing the code.

## 15. Rebuild, cleanup, and reingest rules

### Ordinary update

Change one file under data/mount/<team>/..., then:

~~~bash
curl -f -X POST "$SCOPE/api/sync" \
  -H 'Content-Type: application/json' \
  -d '{"ingest_mode":"wiki"}' | jq
~~~

No full reingest is needed. The raw converter commits the changed Markdown;
the Git diff selects only the changed file; the writer may use incremental
same-line-count page invalidation; GROWI publication updates that document;
and GROWI sync revises only the touched document folder. Changed node hashes
drive supersession, vectors, edges, and dependent cascade work.

If the edit changes line count in a wiki-mode document, the incremental writer
falls back to a full document rewrite because old source ranges can no longer
be trusted. This is still one-document work, not an entire data-root rebuild.

### GROWI-only edit

Do not run the converter or wiki writer just to index a human GROWI edit:

~~~bash
curl -f -X POST "$APP/admin/api/connections/local/resync" \
  -H "X-Admin-Password: $ADMIN_PASSWORD" | jq
~~~

Or call the scoped /api/sync; it queues raw sync as well, so the admin
resync endpoint is the narrower operation. The next timer tick also performs
it automatically.

### Rebuild the derived graph

To rebuild graph.sqlite from GROWI while preserving the GROWI registry:

1. Stop the application.
2. Remove only data/graph.sqlite, graph.sqlite-wal, and graph.sqlite-shm if
   present.
3. Start the application.
4. Wait for bootstrap, then wait for sync_growi to finish.

Do not remove data/engine.sqlite; it contains the encrypted connection
registry and page ledger. The graph is recreated from pages under the
registered GROWI root. Embeddings are recreated through the configured
embedding service, not copied from a vector database.

### Regenerate local wiki output

If data/wiki/<document> is deleted but metadata/last_sha still points at the
current raw commit, a normal sync may see no Git change. Remove the affected
document's wiki/state output and reset the raw sync marker before a controlled
rebuild:

~~~bash
rm -rf data/wiki/test/docx/Input1.docx
rm -rf data/metadata/state/test/docx/Input1.docx
rm -f data/metadata/last_sha
curl -f -X POST "$SCOPE/api/sync" \
  -H 'Content-Type: application/json' \
  -d '{"ingest_mode":"wiki"}' | jq
~~~

Use this only when the application is stopped or when the target is not being
written by an active job. The next sync regenerates raw-derived wiki output,
publishes it to GROWI, and then indexes it.

### Reconvert all mount files

If raw output was deleted or corrupted, also remove metadata/convert.json
before starting the parser and syncing. The converter uses that file to avoid
reparsing unchanged mount files; deleting raw files alone should not be
assumed to force conversion.

### Delete one document

Use the document delete API for a graph-only deletion. It queues a bounded
whole-document job rather than one job per node:

~~~bash
curl -f -X POST "$SCOPE/api/document/delete" \
  -H 'Content-Type: application/json' \
  -d '{"document_name":"test/docx/Input1_docx.md"}' | jq
~~~

If the document is still in mount/raw, the next raw sync recreates it. To
remove it permanently, remove the mount file, let conversion remove the
matching raw file, and sync again.

## 16. Minimal deterministic diff demonstration

This demonstrates the update path without adding a random fact. Choose a
small raw Markdown file whose generated wiki is easy to inspect:

~~~bash
cd /mnt/common/Code/llm-wiki-dist
cp data/raw/test/docx/Input1_docx.md /tmp/Input1_docx.md.before
~~~

Append a deterministic fact to the raw file, commit it, and sync:

~~~bash
cat >> data/raw/test/docx/Input1_docx.md <<'EOF'

## Deterministic sync check

The sync demonstration identifier is SYNC-CHECK-2026-09-13.
EOF

cd data/raw
git add test/docx/Input1_docx.md
git -c user.name=llm-wiki -c user.email=llm-wiki@localhost \
  commit -m 'test: deterministic sync check'

cd ../../llm-wiki-dist
curl -f -X POST "$SCOPE/api/sync" \
  -H 'Content-Type: application/json' \
  -d '{"ingest_mode":"wiki"}' | jq
~~~

Poll the returned job and inspect the result:

~~~bash
grep -R -n 'SYNC-CHECK-2026-09-13' ../data/wiki/test ../data/metadata/state/test
curl -sG "$SCOPE/api/search" \
  --data-urlencode 'q=SYNC-CHECK-2026-09-13' | jq
curl -s "$SCOPE/api/graph" \
  | jq '[.nodes[] | select(.body | contains("SYNC-CHECK-2026-09-13")) | {id,type,document:.original_document_name,source_version}]'
~~~

Expected behavior:

1. raw has one new Git commit.
2. Only Input1_docx.md is selected by the raw diff.
3. Its wiki folder/source stamp changes.
4. GROWI receives the new generated section/page.
5. sync_growi revises the corresponding document's graph nodes.
6. Search and the UI show the deterministic identifier.
7. Other documents retain their existing source hashes and pages.

To restore the test fixture, stop active sync work, restore the saved file,
commit the restore, and sync once more:

~~~bash
cp /tmp/Input1_docx.md.before data/raw/test/docx/Input1_docx.md
cd data/raw
git add test/docx/Input1_docx.md
git -c user.name=llm-wiki -c user.email=llm-wiki@localhost \
  commit -m 'test: restore deterministic sync fixture'
cd ../../llm-wiki-dist
curl -f -X POST "$SCOPE/api/sync" \
  -H 'Content-Type: application/json' \
  -d '{"ingest_mode":"wiki"}' | jq
~~~

## 17. MCP

The app image starts mcp_server.py on port 8001. The public route is:

~~~text
<MCP_ORIGIN><WIKI_PREFIX>/<scope>/mcp
~~~

For the Docker mapping above:

~~~text
http://127.0.0.1:51026/llm-wiki/test/mcp
~~~

The MCP router validates a safe scope token, forwards calls to the matching
backend scope, waits for /api/ready when necessary, and exposes read tools
such as search/read/follow plus research operations. query_table is the safe
table-record query path. queue_agent_note is the write path for an
agent-generated note and is subject to the backend write queue.

Set MCP_BACKEND_ORIGIN when the MCP process cannot reach the backend at its
default http://127.0.0.1:8000. MCP_ALLOWED_WIKIS, when non-empty, limits which
scope names the MCP router will accept.

## 18. Tests

Run the full standard-library test suite:

~~~bash
cd /mnt/common/Code/llm-wiki-dist/llm-wiki-dist
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
~~~

Run the main plan groups:

~~~bash
.venv/bin/python -m unittest \
  tests.test_project \
  tests.test_convert \
  tests.test_sync \
  tests.test_writers \
  tests.test_wiki_chunking \
  tests.test_wiki_page \
  tests.test_wiki_incremental \
  tests.test_growi_client \
  tests.test_growi_publish \
  tests.test_growi_sync \
  tests.test_growi_routing \
  tests.test_registry \
  tests.test_vectors \
  tests.test_pages_shelf \
  tests.test_pages_router \
  tests.test_pages_stitch \
  tests.test_neighborhood
~~~

The tests that call live model, parser, or GROWI services are separate from
pure unit tests and should be run only when those services are available.

When a sync fails, inspect both the returned job and the application log; the
enqueue HTTP response alone is not the result of the job.

## 19. Troubleshooting checklist

### no GROWI connection registered

Check WIKI_SECRET_KEY, data/engine.sqlite, and the admin connection list.
Register local again if the key changed or the registry was intentionally
removed.

### unknown scope

Check data/mount/<scope> or data/raw/<scope>, then call /api/scopes. Restart
or bootstrap after adding a new top-level scope if the current process has not
refreshed its route state.

### Readiness never becomes true

Check /api/ready, /api/health, application logs, model URLs, embedding
dimension compatibility, and GROWI connectivity. Retry only after fixing the
reported dependency.

### Mount file is not converted

Check WIKI_PARSER_BASE_URL, parser logs, metadata/convert.json, file
extension, parser reachability from the app process, and whether the file is
listed as unsupported. Delete convert.json only when intentionally forcing a
full reconversion.

### GROWI has no new page

Check that sync_raw finished, that the connection write_path is correct, and
that the GROWI API token can write below it. Then use the admin connection
test and inspect generated data/wiki/<scope>/... output.

### GROWI changed but graph is stale

Call the connection resync endpoint or wait for the sync timer. Check the
sync_growi job and engine.sqlite page ledger.

### Excel answer is wrong

Inspect the generated table page and its table-spec marker, then verify the
records with a direct SELECT through query_records. If structure judging
failed twice, the deterministic Python heuristic was used; adjust preview
settings or source extraction rather than writing arbitrary SQL against the
graph database.

### SQLite is large

The graph database contains nodes, FTS5, and sqlite-vec data. Embedded image
media should be stripped from FTS text by GraphStore._reindex_fts; do not
replace SQLite with Qdrant for this problem. Inspect image payloads and FTS
reindex behavior first.
