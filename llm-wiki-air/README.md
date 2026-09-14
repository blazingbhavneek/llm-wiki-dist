# llm-wiki-air

The no-Git source-to-wiki half of `llm-wiki-dist`, same folder shape as its
`llm-wiki-dist/` app folder. It scans `data/mount/`, parses supported files to
`data/raw/`, creates lossless wiki pages plus link footers in `data/wiki/`, then
optionally publishes owned pages to GROWI.

```bash
cp .env.example .env            # endpoints, WIKI_DATA_ROOT=./data, GROWI_*
python main.py check            # ping chat / embed / parser / GROWI
python main.py sync             # one pass: mount -> raw -> wiki -> links -> GROWI
python main.py watch --interval 60
python main.py wiki team/a.docx team/b.pdf --force --linker neo
python main.py publish          # GROWI sweep only
python main.py link status | relink team/a.docx | rebuild --mode legacy
python main.py -h               # everything else; ../commands.md is the cheat sheet
```

Layout: `graph/` is a byte-for-byte copy of the upstream factory allowlist
(`config.py`, `common/`, `clients/{chat,embeddings}.py`, `formats/`, `wiki/`,
`linker/`, `workspace/{project,parser_client,convert,writer}.py`,
`growi/{__init__,client,paths,publisher}.py`) — copy it again after upstream
changes; `publisher/` (ledger, scanner, pipeline) and `main.py` are the
downstream-only part. `data/` is runtime state and ignored by Git.

The publisher is one-way. It replaces only pages carrying its chunk markers;
unmarked GROWI pages are left alone. A parser, linker or publication failure
keeps the corresponding ledger row dirty so the next run retries it.
