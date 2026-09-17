# llm-wiki-air

No-Git document pipeline:

```text
external mount -> raw Markdown -> wiki -> neo linker -> GROWI
```

Run every command below from `llm-wiki-air/`. The default build mode is `wiki`
with the `neo` linker.

## Configuration

Copy the environment template and set the parser, model, embedding, and GROWI
endpoints:

```bash
cp .env.example .env
```

Set `WIKI_CONCURRENCY` in `.env` to control planner, wiki/Excel generation,
ingestion, linker, and API-agent parallelism from one place.

Each source project has one file under `configs/`. The filename selects the
configuration; `target_name` controls both the data subfolder and GROWI path:

```ini
# configs/projectA.ini
[project]
source_mount = /home/seigyo/mnt/projectA
target_name = Moove
data_root = ../data
```

`data_root` may be absolute or relative to the INI file. `source_mount` must be
absolute. This example reads from projectA, writes local state under
`data/Moove/`, and publishes below `/Moove` in GROWI. Optional `growi_url` and
`growi_token` values override `GROWI_URL` and `GROWI_TOKEN` from `.env` for that
project only.

```ini
growi_url = http://10.160.152.235:3000/
growi_token = API Token:<token>
```

Project-specific runtime overrides go in an optional `[settings]` section.
Values here take precedence over `.env`; omitted values continue to use `.env`:

```ini
[settings]
growi_url = http://growi.example:3000/
growi_token = API Token:<token>
growi_mode = attach
growi_timeout = 30
chat_base_url = http://llm.example:8000/v1
chat_api_key = local
chat_model = gemma-4-31B
embed_base_url = http://embed.example:8001/v1
rerank_base_url = http://rerank.example:8002/v1
parser_base_url = http://parser.example/agent/doc-parser/
parser_timeout = 7200
concurrency = 4
wiki_linker_enabled = true
wiki_linker_mode = neo
```

Any non-structural `Settings` field name can be overridden in `[settings]`;
values are type checked when the project is loaded. Keep `source_mount`,
`target_name`, and `data_root` under `[project]`. Unknown names fail immediately.

Each project writes to `data/<project>/{raw,metadata,wiki}`. Source files remain
in the external path; no `mount/` directory is created below `data/`.

Verify a project and its configured services:

```bash
.venv/bin/python main.py check --project projectA
```

An absolute INI path can be used instead of a name:

```bash
.venv/bin/python main.py check --project /srv/wiki/configs/smoke.ini
```

Relative paths outside `configs/` are rejected: pass only a config name or an
absolute INI path. One command invocation always operates on one project.

`--data-root /another/path` can be added before or after a command when an
isolated output tree is needed.

## Commands

### 1. Mount a source directory manually

If the selected project INI already points directly to the populated source
directory, no mount command is needed. To bind another local directory to the
configured path:

```bash
sudo mkdir -p /home/seigyo/mnt/projectA
sudo mount --bind /path/to/source /home/seigyo/mnt/projectA
findmnt --target /home/seigyo/mnt/projectA
```

Use the appropriate `mount` command instead of `--bind` for NFS, SMB, or a block
device. The configured directory must be readable by the process running the
pipeline.

### 2. Convert the mount to raw Markdown

This performs only `mount -> raw`; it does not build, link, or publish:

```bash
.venv/bin/python main.py -v convert --project projectA
```

Converted files are written below `data/Moove/raw/`. Non-Markdown documents
require `WIKI_PARSER_BASE_URL` in `.env`.

Macro-enabled `.xlsm` files are inspected statically before upload; macros are
never run and the original workbook is never rewritten. The watcher sends the
original file plus a sheet-lineage manifest. Workbook pages are ordered as all
`シート-*` pages first, then one `マクロ-*` code/reference page per VBA
procedure, then the `解説-*` pages. A sheet fits one page when it stays within
100 rows, 100 columns, and 100,000 rendered characters; otherwise it is split
into numbered parts. The explanation pages are not generated independently:
each parser-created sheet part is one indivisible observation unit, and those
units pass through whole-workbook planning, section planning, rewrite, and
validation stages equivalent to the PDF pipeline. Excel content is never split
again using the PDF pipeline's line-window size.
Their published provenance is the worksheet cell range (for example
`操作!A1:J100`), not parser Markdown line numbers. Formulas, cached values,
charts, images, hidden/intermediate roles, and lineage are retained. Other
files, including `.xlsm` files without VBA, use the unchanged parser path.

### 3. Run the manual end-to-end flow, including publish

Run the complete project once:

```bash
.venv/bin/python main.py -v sync --project projectA
```

This performs `mount -> raw -> wiki -> neo linker -> GROWI`. It does not require
step 2 because `sync` performs its own mount conversion.

### 4. Build wiki and linker output

Build all raw Markdown and then link the completed batch. Nothing is published:

```bash
.venv/bin/python main.py -v build --project projectA all
```

### 5. Build wiki only

Build all raw Markdown into wiki pages without running the linker or publishing:

```bash
.venv/bin/python main.py -v build --project projectA wiki
```

### 6. Build linker only

Link all pending wiki documents without rebuilding or publishing them:

```bash
.venv/bin/python main.py -v build --project projectA link
```

Use `--force` to relink documents already marked complete:

```bash
.venv/bin/python main.py -v build --project projectA link --force
```

### 7. Build specific targets

Build targets are paths relative to the project's `raw/` directory:

```bash
.venv/bin/python main.py -v build --project projectA all \
  "マニュアル/kdmパッケージ取扱説明書B改訂_pdf.md"

.venv/bin/python main.py -v build --project projectA wiki \
  "マニュアル/kdmパッケージ取扱説明書B改訂_pdf.md"

.venv/bin/python main.py -v build --project projectA link \
  "マニュアル/kdmパッケージ取扱説明書B改訂_pdf.md"
```

### 8. Run one mount target end to end for smoke testing

`sync` targets are paths relative to the configured mount:

```bash
.venv/bin/python main.py -v sync --project projectA --force \
  "マニュアル/kdmパッケージ取扱説明書B改訂.pdf"
```

This performs the complete pipeline for that source only. If linking changes a
related document, that affected document is also republished; unrelated pending
documents are not built, linked, or published.

### 9. Watch one mount target for smoke testing

The watcher performs only a cheap `path + size + mtime_ns` scan every 10 seconds.
`--force` queues the target once at startup even when it is already up to date:

```bash
.venv/bin/python main.py -v watch --project projectA --interval 10 --force \
  "マニュアル/kdmパッケージ取扱説明書B改訂.pdf"
```

After that first pass, only new metadata changes are queued. Stop with Ctrl-C.
Use `--growi-interval 0` when the smoke test should not poll GROWI.

### 10. Watch a complete project

Run the persistent scanner and worker together:

```bash
.venv/bin/python main.py -v watch --project projectA --interval 10
```

The first scan starts from an empty metadata tree and queues every supported
source as one slow batch. The batch builds all wiki documents first, links them
together, and publishes only after both phases succeed. Later scans coalesce
adds and updates into the slow queue. Deletes use the fast queue and invalidate
an older queued or active operation for the same source.

The scanner keeps running in its own lightweight thread while a long build is
active. GROWI revisions are checked while the worker is idle every 300 seconds;
changed managed sections are pulled into local wiki state and marked pending for
the next linker batch. Set another interval with `--growi-interval SECONDS`, or
disable it with `--growi-interval 0`.

The persistent source tree and both queues live in
`data/<project>/metadata/watch-queue.sqlite`.

Pulled user edits are also copied into the generator's resumable page state. On
a later source update, untouched pages retain those edits; pages whose owned or
referenced source-line ranges changed are regenerated, so the source document
wins for overlapping changes.

### Run the watcher from cron

Cron has one-minute resolution, so cron should keep the long-running watcher
alive; the watcher itself performs the 10-second scans. After `check` has created
the project data directories, add this with `crontab -e` (replace the repository
path):

```cron
* * * * * cd /absolute/path/to/llm-wiki-air && flock -n /tmp/llm-wiki-air-Moove-watch.lock .venv/bin/python main.py watch --project projectA --interval 10 >> data/Moove/metadata/watch.log 2>&1
```

`flock` makes later cron invocations exit while the existing worker is alive. If
the worker exits, cron restarts it within one minute and returns interrupted
queue entries to pending state.

### Inspect and operate the queue manually

```bash
# One metadata-only scan; useful for testing the detector.
.venv/bin/python main.py queue scan --project projectA --settle 10

# Process one fast or slow batch, then exit.
.venv/bin/python main.py queue work --project projectA --once

# Run only the persistent worker (when a separate process runs queue scan).
.venv/bin/python main.py queue work --project projectA

# Inspect queued/running/failed operations and retry failures.
.venv/bin/python main.py queue status --project projectA
.venv/bin/python main.py queue retry --project projectA
```

Within a project, fast deletes always run before the next slow batch. Slow jobs
are FIFO and coalesced by mount-relative path; later edits replace older pending
edits instead of adding duplicates. Specific priority rules beyond fast-before-
slow are intentionally not assigned yet. A failed fast delete blocks slow work
until `queue retry` succeeds, preventing newer publication from passing an
unreconciled deletion.

## Other operations

```bash
.venv/bin/python main.py link --project projectA status
.venv/bin/python main.py link --project projectA relink \
  "マニュアル/kdmパッケージ取扱説明書B改訂.pdf"
.venv/bin/python main.py link --project projectA rebuild --mode neo
.venv/bin/python main.py publish --project projectA
.venv/bin/python main.py reset --project projectA
.venv/bin/python main.py -h
```

`reset` deletes only GROWI pages carrying this publisher's markers. Unmanaged
GROWI content is preserved.

## Notes

Publishing resolves local pages before writing links, so generated links use
stable GROWI `/{pageId}` permalinks while local Markdown keeps portable relative
paths. GROWI edits inside publisher markers are pulled into unchanged local wiki
pages; simultaneous local and remote edits fail as conflicts.

Local generation and linking failures never start publishing. A source event
that changes while its batch is running cancels that batch at the next phase
boundary and prevents its publish step. GROWI itself has no multi-page
transaction, so a network failure during GROWI's page-by-page API writes can
still leave a partial remote publish; retry the failed queue item after fixing
the connection.

`graph/` is copied from the upstream factory allowlist. `publisher/` and
`main.py` contain the downstream no-Git pipeline. `data/` is runtime state and
is ignored by Git.
