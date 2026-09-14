# llm-wiki command cheat sheet

`python main.py -h` in either folder lists everything. Both CLIs load `.env`
from their own folder (`graph.config` calls `load_dotenv()`); paths are
relative to `WIKI_DATA_ROOT`.

## 0. Model / helper servers (external — not started by this repo)

| type: server | URL | env |
|---|---|---|
| chat — llama-server / vLLM, `gemma-4-12B` | `http://localhost:8000/v1` | `OPENAI_BASE_URL`, `WIKI_MODEL` |
| embed — `cl-nagoya/ruri-v3-30m` | `http://localhost:8001/v1` | `WIKI_EMBED_BASE_URL` |
| rerank — `ruri-v3-reranker-310m` (engine only) | `http://localhost:8002/v1` | `WIKI_RERANK_BASE_URL` |
| parser — doc-parser (needed for non-`.md` sources) | `http://<host>:8888` | `WIKI_PARSER_BASE_URL` |

```bash
cd /mnt/common/Code/llm-wiki-dist/parser && python3 -m uvicorn server:app --host 0.0.0.0 --port 8888
.venv/bin/python main.py check        # from either folder: pings chat/embed/rerank/parser(/GROWI)
```

## 1. Full product — `llm-wiki-dist/llm-wiki-dist`

```bash
cd /mnt/common/Code/llm-wiki-dist/llm-wiki-dist
```

### type: server — engine API + UI (FastAPI), MCP proxy

```bash
.venv/bin/python main.py serve --port 8000            # == uv run uvicorn app:app --port 8000 --host 0.0.0.0
.venv/bin/python main.py serve --port 8000 --reload   # dev
.venv/bin/python main.py mcp --port 8001              # == python mcp_server.py --port 8001
```

### type: pipeline, full — everything under `data/`

Convert `mount/` → `raw/`, regenerate changed raw files, link, publish to
GROWI, republish documents the linker touched.

```bash
.venv/bin/python main.py sync                         # GROWI connection from WIKI_GROWI_NAME / the only registered one
.venv/bin/python main.py sync --growi local           # named registered connection
.venv/bin/python main.py sync --growi none            # same pass without GROWI (local wiki only)
.venv/bin/python main.py -v sync --linker neo --timeout 900
```

### type: pipeline, all raw files — no GROWI, skips up-to-date documents

```bash
.venv/bin/python main.py wiki --all
.venv/bin/python main.py wiki --all --force --keep-going   # regenerate everything, continue past failures
```

### type: pipeline, list of files

```bash
.venv/bin/python main.py wiki test/docx/Input1_docx.md test/docx/Input2_docx.md test/docx/Valid_docx.md
.venv/bin/python main.py wiki --from-file files.txt   # one raw-relative path per line, # comments ok
```

### type: pipeline, one file (the old `wiki_one.py`)

```bash
.venv/bin/python main.py wiki 'rikiseisan/test/系統制御ミドルウェア（Ｍｏｏｖｅ）_構成制御ユーザーズマニュアル_pdf.md'
.venv/bin/python main.py wiki 'rikiseisan/test/系統制御ミドルウェア（Ｍｏｏｖｅ）_システム運転情報管理ユーザーズマニュアル_pdf.md'
.venv/bin/python main.py wiki 'rikiseisan/test/系統制御ミドルウェア（Ｍｏｏｖｅ）_エラー管理ユーザーズマニュアル_pdf.md'
.venv/bin/python main.py -v wiki test/docx/Input2_docx.md --timeout 900        # progress events; slow local model
```

### type: pipeline variants (flags work on `wiki` and `sync`)

| flag | meaning |
|---|---|
| `--mode wiki` | section-rewrite wiki (default, `WIKI_INGEST_MODE=wiki`) |
| `--mode chunks` | legacy chunk pages (`graph/wiki/legacy.py`) |
| `--linker legacy` | RRF candidates + `EDGE_PROMPT` groups of 4 (default, `WIKI_LINKER_MODE`) |
| `--linker neo` | entity define/use + behaviour hops, inline links at first mention |
| `--linker off` | no linker, writes `{"status":"disabled"}` markers |
| `--timeout N` | per-call model timeout (`WIKI_REQUEST_TIMEOUT`, default 300; gemma-4-12B planner needs ~900) |

```bash
.venv/bin/python main.py wiki test/docx/Input1_docx.md --mode chunks --linker off
.venv/bin/python main.py wiki --all --force --linker neo
```

### type: parser only — `mount/` → `raw/`

```bash
.venv/bin/python main.py convert                      # Markdown copied, other formats via WIKI_PARSER_BASE_URL
```

### type: linker only (== `python -m graph.linker …`)

```bash
.venv/bin/python main.py link status                                  # {'mode': 'legacy', 'documents': 3, 'chunks': 5, 'edges': 3}
.venv/bin/python main.py link relink test/docx/Valid.docx             # one document; 0 model calls when nothing changed
.venv/bin/python main.py link relink test/docx/Valid_docx.md          # raw path works too
.venv/bin/python main.py link rebuild --mode legacy                   # drop catalog, relink all (re-pays every edge call)
.venv/bin/python main.py link rebuild --mode neo                      # switch mode (catalog is either/or)
.venv/bin/python main.py link rebuild --mode legacy --no-edges        # strip footers/inline links, keep chunk metadata
```

After `relink`/`rebuild`, GROWI is only updated by the next `main.py sync`.
Runtime files: `data/metadata/wiki-linker.sqlite`,
`data/wiki/<doc>/_planning/{pages/,chunks.json,links.json,linker.json}`.

### type: misc

```bash
.venv/bin/python main.py index                                       # rewrite data/wiki/index.md
.venv/bin/python main.py --data-root /tmp/other-root wiki --all      # any command on another data root
.venv/bin/python -m unittest discover -s tests                       # 202 tests, no model needed
```

## 2. Minimal no-Git publisher — `llm-wiki-air` (same flags, mount-driven)

```bash
cd /mnt/common/Code/llm-wiki-dist/llm-wiki-air
cp .env.example .env                                  # WIKI_DATA_ROOT=./data, WIKI_CHAT_*, WIKI_EMBED_*, GROWI_*
```

### type: server — none of its own (uses the same chat/embed/parser servers + GROWI)

```bash
python main.py check
```

### type: pipeline, full — one pass over `data/mount`: parse → raw → wiki → links → GROWI sweep

```bash
python main.py sync
python main.py watch --interval 60                    # loop
python main.py -v sync --linker neo --timeout 900
```

### type: pipeline, list of files / one file (mount-relative paths; still ledger-tracked)

```bash
python main.py wiki demo/Input1.md demo/Valid.md
python main.py wiki team/manual.pdf --force           # regenerate even if the source is unchanged
python main.py wiki --from-file files.txt
```

### type: publish only — GROWI sweep by content hash, no generation

```bash
python main.py publish
```

### type: linker only — identical to the full product

```bash
python main.py link status
python main.py link relink demo/Valid.md
python main.py link rebuild --mode neo
```
