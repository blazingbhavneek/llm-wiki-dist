# Growi LLM Wiki — Operations Guide

Takes a folder of documents (an NFS or bind mount) and produces an AI-rewritten,
cross-linked Markdown wiki in **GROWI**, under one folder named after the project.
People read and edit the result in GROWI; the pipeline owns the pages it generated.

Everything is driven by one program, `main.py`, and one project = one file in `configs/`.
Architecture and internals for developers: [`DEV_README.md`](DEV_README.md).

## What you get

| Where             | What                                                                |
| ----------------- | ------------------------------------------------------------------- |
| Source mount      | your original documents, never modified or copied                   |
| `data/<name>/`  | local working copy: converted text, generated pages, progress state |
| GROWI`/<name>/` | published wiki: numbered pages, cross-links,`00-目次` index pages |

## Supported file formats

`.md` `.txt` `.pdf` `.docx` `.doc` `.pptx` `.xlsx` `.xlsm` `.xls` `.csv`

- `.md` / `.txt` are used as they are — no conversion service needed.
- Everything else is converted by the external **doc-parser** service (runs on a GPU host).
- `.xlsm` is inspected statically: macros are never run, worksheets and VBA are published as separate pages.
- Word/Excel 97-2003 (`.doc`, `.xls`) are converted first; macros in `.xls` are not kept.

## One-time environment setup

```bash
uv sync     # creates .venv/
```

Edit `.env` in the repository root — these are the keys that matter:

| Key                                             | Purpose                                                 |
| ----------------------------------------------- | ------------------------------------------------------- |
| `WIKI_CHAT_BASE_URL` (or `OPENAI_BASE_URL`) | main LLM endpoint (OpenAI-compatible)                   |
| `WIKI_CHAT_API_KEY` (or `OPENAI_API_KEY`)   | its key;`local` when the endpoint needs none          |
| `WIKI_MODEL` (or `WIKI_CHAT_MODEL`)         | model name on that endpoint                             |
| `WIKI_PARSER_BASE_URL`                        | doc-parser service; only needed for non-`.md` sources |
| `GROWI_URL`, `GROWI_TOKEN`                  | GROWI address and API token                             |
| `WIKI_DATA_ROOT`                              | where local working state lives (default`./data`)     |
| `WIKI_CONCURRENCY`                            | parallel LLM calls (default`4`)                       |
| `WIKI_OUTPUT_LANGUAGE`                        | language of generated pages (default Japanese)          |

Settings are read in this order, later wins:
**built-in defaults → `.env` → `configs/<project>.ini` → command-line flags**.
`.env` is the machine-wide setup; the project file overrides it for one project.

Check everything before a first run:

```bash
.venv/bin/python main.py check --project projectA
```

## Adding a new source folder

1. **Mount** it (any absolute, readable path; `--bind`, NFS or SMB):

   ```bash
   sudo mkdir -p /home/seigyo/mnt/newproj
   sudo mount --bind /path/to/documents /home/seigyo/mnt/newproj
   ```
2. **Create the project file** `configs/newproj.ini` — the file name is what you type after `--project`:

   ```ini
   [project]
   source_mount = /home/seigyo/mnt/newproj   # absolute path to the mounted documents
   target_name  = NewProj                    # one folder name: data/NewProj/ and /NewProj in GROWI
   data_root    = data                       # local state folder (absolute, or relative to this repo)

   # optional, only when this project uses a different GROWI:
   growi_url   = http://growi.example:3000/
   growi_token = API Token:<token>
   ```
3. **Verify and run**:

   ```bash
   .venv/bin/python main.py check --project newproj
   .venv/bin/python main.py -v sync --project newproj
   ```

## What can be changed in the project file

`[project]` holds the three structural keys above. An optional `[settings]` section can
override any runtime setting for that project only — useful ones:

| Key                                                 | Effect                                                          |
| --------------------------------------------------- | --------------------------------------------------------------- |
| `chat_base_url`, `chat_model`, `chat_api_key` | the LLM endpoint and model for this project                     |
| `parser_base_url`, `parser_timeout`             | doc-parser service address and how long a big file may take     |
| `growi_url`, `growi_token`, `growi_mode`      | GROWI target;`attach` never writes outside the project folder |
| `concurrency`                                     | parallel LLM calls                                              |
| `wiki_linker_enabled`, `wiki_linker_mode`       | cross-document links on/off and which linker to use             |
| `wiki_output_language`                            | language of generated pages                                     |
| `data_root` (in `[project]`) or `--data-root` | move the working folder to another disk                         |

Unknown key names are rejected at once, so a typo is reported instead of ignored.

## Daily use

| I want to …                       | Command                                                        |
| ---------------------------------- | -------------------------------------------------------------- |
| run the whole pipeline once        | `main.py -v sync --project projectA`                         |
| re-run one document                | `main.py -v sync --project projectA --force "path/file.pdf"` |
| push pages that are already built  | `main.py publish --project projectA`                         |
| rebuild locally without publishing | `main.py -v build --project projectA all`                    |
| only re-link documents             | `main.py -v build --project projectA link`                   |
| capture GROWI edits, no generation | `main.py pull --project projectA`                            |
| remove this project's GROWI pages  | `main.py reset --project projectA`                           |

`sync` converts, generates, links and publishes in one go, and retries failed work once.
A document whose generation failed is never published.
`publish` sends what is already built, even when its links are still pending; add
`--no-allow-unlinked` to link first and publish only fully linked documents.

All commands run from the repository root with `.venv/bin/python main.py …`.

## Large mounts: `--fast`

For large mounts where the normal pipeline would take too long:

```bash
.venv/bin/python main.py -v sync --project projectA --fast
```

`--fast` uses a reduced generation path and **skips cross-document linking**, so pages
are published without links between documents. Add them afterwards, then push:

```bash
.venv/bin/python main.py -v build --project projectA link
.venv/bin/python main.py publish --project projectA
```

## Keeping a folder fresh

`watch` is a program that keeps running: it scans the mount for new or changed files
every `--interval` seconds (default `86400`, once a day) and checks GROWI for human
edits every 5 minutes. Start it in the background and leave it alone — one instance
per project, a second one refuses to start while the first is alive:

```bash
# detached, survives closing the terminal
nohup .venv/bin/python main.py -v watch --project projectA >> data/<name>/metadata/watch.log 2>&1 &

# or in a terminal session you can come back to (tmux starts in $HOME, so cd first)
tmux new -s wiki 'cd /path/to/this/repo && .venv/bin/python main.py -v watch --project projectA'
```

Stop it with Ctrl-C in tmux, or `pkill -f "watch --project projectA"`.
To refresh once and exit instead — after dropping files into the mount, or on any
schedule you already have — run `sync`.
