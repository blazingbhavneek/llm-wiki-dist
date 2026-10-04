# llm-wiki-air refactor plan — independent phases + fast path

## 0. Goal

Each phase lives in **its own folder**, takes **its own config and explicit input
and output paths**, and can be **run alone** with different settings. The phases
are convert, wiki builder, linker, index and publisher. Changing one phase's
logic touches only that folder.

- Shared logic lives in **`common/`**.
- Chaining phases (`sync`, `watch`, `build`, queue, transactions) lives in
  **`runner/`**, which contains no phase logic.
- `sync --fast` (`fast-path-proposal.md`) becomes a **policy inside `wiki/` and
  `linker/`**, plus a flag in `runner/`.
- **No feature is removed.** Line count is not a goal.

Scope: `main.py`, `graph/`, `publisher/`, `jev/`, `tests/`, root docs.
`growi-search/` is out of scope; its single `from graph.common import mokuji_data`
line is updated when that module moves, and `jev/` stays as is.

## 0.1 Backward compatibility — priority #1

**Promise.** Point the refactored code at an existing project. The next `sync`
with unchanged sources makes **0 LLM calls, 0 parser calls and 0 GROWI writes,
and changes 0 bytes** under `data/<project>/`.
- Changed sources are processed exactly as the old code would have, starting
  from the existing state (same tier decisions).
- Nothing is re-ingested.
- Fast mode writes the **same folders and file formats** as standard mode.

### Rules every step must follow

1. **The on-disk contract is frozen.** Moving code never changes any of these:
   - every path in §3.2
   - every file format and JSON `schema_version`
   - both SQLite schemas: queue (`watch-queue.sqlite`) and linker catalog
     (`wiki-linker.sqlite`)
   - `pipeline.json`, the human-sync journal, snapshots and activity cursor
   - project git refs (`refs/llm-wiki/last-good`) and the paths git tracks
2. **Identifiers that decide "is this up to date?" are frozen, byte-for-byte:**
   - `PROMPT_VERSION`, `SEED_PLAN_VERSION`, `SEED_PLAN_COMPILE_VERSION`,
     `REWRITE_PROMPT_VERSION`
   - `CHUNK_META_VERSION`, `EDGE_VERSION_LEGACY/NEO/JEV`,
     `REFERENCE_PLAN_VERSION`, `ALIAS_VERSION`
   - the human-sync `VERSION`/`SCORER_VERSION`/`PROMPT_VERSION`/
     `VALIDATION_VERSION`, `CURSOR_VERSION`, mokuji `VERSION`
   - how `source_id`, `chunk_id` (`id_seed`), `edge_id`, wiki folder names, page
     filenames and GROWI paths are derived
   - every HTML-comment marker
   - **all prompt text**: changing it changes the cached results and forces
     regeneration
3. **Additive only.** New data goes into *new keys* of existing files.
   - **Old keys never change meaning, are never renamed or removed, and
     `schema_version` is not bumped for additive keys.**
   - A missing new key means "what the old code did". For example, a missing
     `policy` means `standard`.
   - Old code can therefore still read new output, so rolling back is safe.
4. **No bulk migrations.** Any upgrade of an old document's files must be:
   - lazy, done only when that document is touched anyway
   - deterministic and **LLM-free**
   - idempotent
   - **gated by byte-equality of the final pages**: if the upgraded result
     differs, keep the existing files and report.
5. **Config.** Every INI key, `.env` variable and CLI flag keeps working.
   - Deleted (dead) settings stay **accepted and ignored with a warning**.
     Today an unknown INI key raises an error, so deleting them outright would
     break existing configs.
   - Divergent `getattr` defaults resolve to today's `Settings` value, which is
     what production already runs.
6. **Interrupted work survives the switch.** A queue, candidate worktree or
   prepared publish left half-done by the old code is recovered by the new
   `recover` (same tables, same transaction phases).
7. **GROWI.** Same page paths, bodies, markers and ownership stamps. Publishing
   an unchanged document is a no-op, so the switch never triggers a republish
   storm.
8. **Fast path.**
   - Same output folders, files and formats as standard. The policy is recorded
     additively per document.
   - Turning on `--fast` never reprocesses unchanged documents. A *changed*
     document switches policy with a full rebuild of **that document only**.
   - Converting an existing document to fast needs `--force` on that document.
   - Standard and fast documents coexist in one project: linker, index and
     publisher read both.
   - Fast linker caches use their own version keys, so standard caches stay
     valid if you switch back.

### Gates

These are added in step 0 and run after every step. **G2 is the one that proves
"no restart".**

| gate | what it proves |
|---|---|
| **G1 golden** | on `tests/samples/`, the new code reproduces today's outputs and file tree byte-for-byte |
| **G2 real-data no-op** | **`python -m runner.compat_check --data data/<project>`** copies the project (incl. `.git`) to a temp dir and runs the new `sync` + `build all` + `index --no-publish` with an LLM client and parser client that **fail on any call** and a GROWI client that records writes. It asserts 0 calls, 0 writes, and an identical file-hash tree. Run it on **every real project, here and on the work PC**, before switching over. |
| **G3 rollback** | the previous commit's code runs `sync` on data written by the new code with no reprocessing |
| **G4 mixed policy** | a project with standard documents built by the old code plus one new fast document: link, index and publish all work, and the standard documents are untouched |
| **G5 interrupted work** | old-code queue / candidate / prepared-publish fixtures are recovered by the new code (reuse `test_sync_fallback` and `test_human_sync_rollout` scenarios) |

## 1. Why changes spread today (short version)

- **Phases call each other.**
  - The wiki writer runs the human-edit overlay before and after building
    (`writer.py` imports `publisher.human_changes` 3×).
  - Publishing runs the linker, the GROWI capture and the index
    (`_publish_sweep`).
  - The GROWI HTTP client contains the human-edit merge (`pull_changes`, 256
    lines).
  - The index imports linker internals (`jev_judge.related_documents`).
- **Three phases write the same page files.** The overlay writes effective
  pages, the linker rewrites them (footer + inline links), and overlay and
  publish both carve the footer back out (`footer_span`, `split_footer`).
- **No owner for files.** `_planning/` paths are built by hand in 16 modules.
  The linker writes `chunks.json` into the wiki's folder.
- **One giant config.**
  - 133-field `Settings`, typed `Any` in 58 places.
  - 108 `getattr(settings, …, default)` reads, 25 of them with a default that
    disagrees with `Settings` (`wiki_linker_mode` `'legacy'` vs `'neo'` in 10
    places).
  - So a phase can't be run "with different stuff" without side effects.
- **Every path re-assembles the pipeline.** `sync`, isolated sync, `watch`,
  `queue`, `sync_once`, `move_sources` and `delete_sources` each hand-chain the
  steps. `build_index` is called from 7 places.
- **Same rule, two implementations.** This caused 5 of the 9 confirmed bugs in
  `plan_diff_2.md`.
- **The progress feature took 8 files.** Adding stage timing needed 8 files and
  several re-edits, because there are 55+ hand-built event dicts and 143
  callback parameters.

---

## 2. Target layout

```
llm-wiki-air/
  main.py           CLI only: argparse → runner function → print. Existing commands unchanged.
  common/           shared logic, no phase knowledge
    settings.py       INI/.env loader → builds each phase's Config (keeps every current env/INI name)
    context.py        Context: llm client, embedder, events (Stage/Event), cancel, per-stage LLM-call counter
    paths.py          DataLayout: the one place that names folders/files under data/<project>/
    llm.py            chat client, structured call + retry (+ validate= hook), embeddings
    markdown.py       atomic blocks / safe cuts, fences, tables (one implementation)
    images.py         image identity by content hash
    markers.py        every HTML-comment marker (bot-ref, chunk, hedit, link footer, index)
    git.py storage.py hashing.py mokuji_data.py prompts_common.py …
  convert/          phase: source file → raw Markdown           (parser client, md/txt passthrough, xlsm lineage)
  wiki/             phase: raw Markdown → wiki pages             (formats/, planning, tiers, writer, judge, chunk mode, policy.py)
  linker/           phase: wiki pages → links + chunk metadata   (catalog, neo|legacy, jev judge, render, policy.py)
  index/            phase: wiki + links → index pages + mokuji   (00-目次, related docs)
  publisher/        phase: wiki + links + index → GROWI          (REST client, human edits: capture/journal/merge/semantic,
                                                                  assemble, revision CAS, delete, reset, published ledger)
  runner/           chains phases: sync, watch, build, queue, transactions (git candidates), source ledger,
                                    scanner, steps.py, progress UI, failure logs
  jev/              unchanged (also used by growi-search)
  tests/            tests/<phase>/…, tests/runner/…, tests/helpers.py
```

Rules:
- **A phase folder imports only `common/` (and `jev/`).** Never another phase,
  never `runner/`.
- **`runner/` imports phase facades and `common/`.**
- **`main.py` imports only `runner/`.**

## 3. The phase contract (same shape for all five)

```python
# <phase>/__init__.py — the facade; the only names other folders may import
@dataclass(frozen=True)
class Config: ...                         # ONLY this phase's knobs (+ the client settings it needs)

def run(cfg: Config, inp: Input, out: Path, ctx: Context) -> Result: ...
#   inp  = paths to earlier phases' outputs + optional hints (changed pages, force_full, scope)
#   out  = this phase's own output folder — the only place it writes
#   Result is also recorded in the phase's own existing state files (no new files), so a later run can read it from disk

# <phase>/__main__.py — run the phase alone
#   python -m wiki --config proj.ini [--set policy=fast] --in data/p/raw/a_docx.md --out /tmp/wiki-fast
```

Two ways to chain phases:
- **From `runner/`:** passes `Result` objects and paths directly.
- **By hand:** from the folders on disk. Each phase can be pointed at any input
  folder (yours, an old run's, a fixture) with any config.

### 3.1 Phase specs

Paths below are today's paths. `<doc>` is the existing per-document folder name,
e.g. `wiki/spec.docx/`.

| phase | `run(…)` input → output | owns (sole writer) | config (from today's Settings) | variants inside the phase |
|---|---|---|---|---|
| **convert** | source file (+ previous raw for image-description reuse) → `raw/<doc>.md` | `raw/`, `metadata/convert.json` | `parser_base_url`, `parser_fallback_base_url`, `parser_describe_images`, `parser_timeout`, `ingest_concurrency`, chat client (image description) | md/txt passthrough · parser + fallback · xlsm static lineage · size gate |
| **wiki** | `raw/<doc>.md` (+ `force_full`) → generated pages + planning state; returns tier, changed/regenerated pages, policy+version (also stamped into its own state) | `metadata/state/<doc>/`, `metadata/work/<doc>/`, `wiki/<doc>/_planning/{manifest,coverage,metadata,source}.json` | `ingest_mode`, `structure_*`, `pdf_use_headings`, `slide_*`, `tabular_*`, `wiki_section_target_lines`, `wiki_write_attempts`, `wiki_planner_concurrency`, `wiki_rewrite_concurrency`, `wiki_output_language`, `wiki_request_timeout`, **`policy`**, chat client | `mode`: wiki \| chunks · format planners (docx/pptx/pdf/md/xlsx/csv) · tiers 0–3 · **policy: standard \| fast** |
| **linker** | generated pages + list of docs (+ changed/regenerated pages) → link decisions | `wiki/<doc>/_planning/{links,chunks,linker,navigation}.json`, `metadata/wiki-linker.sqlite` (+ lock) | `wiki_linker_*` (mode, judge, thresholds, caps, hop caps, footer/inline max…), `embed_*`, Jev backend, chat client, **`policy`** | neo \| legacy · judge llm \| jev · **policy: standard \| fast** |
| **index** | `_planning` summaries + `chunks.json` (+ scope: changed docs) → index pages + mokuji data | `metadata/index/` | `wiki_index_related_docs`, Jev for related docs, page naming | full \| scoped (changed docs + ancestors) |
| **publisher** | generated pages + human journal + `links.json` + index pages → final pages → GROWI | **`wiki/<doc>/*.md` (the final pages, via `assemble`)**, `metadata/human-sync/`, GROWI | `growi_url`, `growi_token`, `growi_mode`, `growi_timeout`, `human_sync_*`, chat client (semantic assistant) | human-sync off \| observe \| apply · publish \| delete \| reset · capture: activity poll \| inventory |

**The publisher's `assemble(doc)`** writes the final pages in `wiki/<doc>/`,
exactly as they look today. A final page is the generated page (from
`metadata/state/<doc>/`) ⊕ human edits (journal) ⊕ links ⊕ ownership stamp.
This is today's overlay render + linker render, done in one place, in today's
order.
- **Links come from what the linker already stores.** That's the curation
  choices in `_planning/navigation.json` (`references` per page) plus the edges.
  `linker/service.py:941` already re-renders pages from these with no LLM.
- **The linker no longer edits page files.** Next to `references` in
  `navigation.json` it also writes each page's edge display data (peer title,
  heading, path, summary, label, via). This is an **additive key**: old code
  ignores it, and the schema version is unchanged.
- **The pure render function moves to `common/links_render.py`.** That's
  today's `render_page`, `link_titles`, `link_entity_mentions` and the footer
  format, moved byte-for-byte. Both the linker (to validate) and `assemble` use
  it.
- **Old documents without the additive key** get it rebuilt from the linker
  catalog, LLM-free, the first time that document is touched, under rule 4.
- **`publish`** runs `assemble`, then writes to GROWI.
- **`build`**, which never touches GROWI, also runs `assemble`, so `wiki/` looks
  the same as today after a local build.

### 3.2 Data folder: same shape as today, one writer per file

**No folder or file is added, renamed or moved** under `data/<project>/`.
Isolation comes from giving every existing file exactly one owning phase:

```
data/<project>/
  raw/<doc>.md                                         convert
  metadata/convert.json                                convert
  metadata/state/<doc>/  metadata/work/<doc>/          wiki
  wiki/<doc>/_planning/{manifest,coverage,metadata,source}.json   wiki
  wiki/<doc>/_planning/{links,chunks,linker,navigation}.json      linker
  metadata/wiki-linker.sqlite (+ .lock)                linker
  metadata/index/                                      index
  wiki/<doc>/*.md  (final pages)                       publisher (assemble)
  metadata/human-sync/                                 publisher
  metadata/pipeline.json                               common/ledger.py only (runner writes sources, publisher's results recorded through it)
  metadata/watch-queue.sqlite, metadata/pipeline.lock  runner
  .git (last-good / candidate worktrees)               runner
```

- **Paths.** `common/paths.py` `DataLayout(root)` is the only code that spells
  these paths. Each accessor names its owning phase. Phases receive a
  `DataLayout` and touch only their own files.
- **`pipeline.json`** keeps its current shape. Only `common/ledger.py` reads or
  writes it, through typed functions. The publisher gets `known_pages` as input
  and returns what it published; it never edits the file itself.
- **`--out` on a standalone run** gives a different root. The phase writes the
  same relative shape under that root (e.g. `/tmp/a/metadata/state/…`), so
  experiments never touch the real project.

### 3.3 Runner: every command is a composition

`runner/steps.py` is the only place phases are put in order:

```python
def update(ctx, docs):          # per doc: convert.run → wiki.run(force_full=publisher.needs_full_rebuild(doc)) ; then linker.run(changed) → publisher.assemble
def remove(ctx, docs)           # each phase deletes its own files for doc; linker relinks peers
def move(ctx, moves)            # identity-preserving rename, then update() for edited docs
def publish_and_index(ctx, touched)   # index.run(scope=touched) → publisher.run(touched + index pages)
```

`runner/transaction.py` wraps steps in a candidate worktree. The phases just
receive candidate folders and never know about git.

| command | composition | policy |
|---|---|---|
| `convert` | `convert.run` × docs | local |
| `build wiki\|link\|all` | `update` (link off / link only / both), ending in `publisher.assemble` → `wiki/` as today | local, no GROWI, no git |
| `link status\|relink\|rebuild\|calibrate-jev` | `linker.*` | local |
| `index [--no-publish --delete]` | `index.run` (+ `publisher.run` for index pages / `publisher.delete_index`) | |
| `publish` | `publish_and_index` | git checkpoint |
| `pull [--inventory]` | `publisher.capture` | git checkpoint |
| `reset` | `publisher.reset` | |
| `human …` | `publisher.human.*` (status, resolve, recover-legacy, live-plan) | |
| `queue scan\|work\|status\|retry` | `queue.*` / `drain` | |
| `sync [--fast] [--isolated]` | recover → `publisher.republish_if_stale` → `queue.scan` → `drain(policy)` → `index.run` | isolated (default): one transaction per doc + one retry pass + deferred link · `--no-isolated`: one per batch · `--fast`: `wiki.policy=fast`, `linker.policy=fast`, scoped index |
| `watch` | loop: `queue.scan` → `drain` every `--interval`; `publisher.capture` every `--growi-interval` | worker lock |

### 3.4 What a change touches afterwards

| you change… | you touch only… |
|---|---|
| how parsing/xlsm lineage works | `convert/` |
| page splitting for a format, tiers, section writing, judge, prompts | `wiki/` |
| linker candidates, judge, footer/inline choice | `linker/` |
| index page content or related docs | `index/` |
| GROWI API, human-edit merge, page assembly, revision rules | `publisher/` |
| order of phases / adding a phase | `runner/steps.py` |
| retry, isolation, transaction, `watch` timing | `runner/` |
| a setting | the phase's `Config` + one line in `common/settings.py` mapping |
| fast-mode behaviour | `wiki/policy.py`, `linker/policy.py` |
| a command or flag | `main.py` + one runner function |

### 3.5 Run a phase alone, with different stuff

```
# default: read and write the project's own data/<project>/ (same paths as today)
python -m wiki   --config configs/projA.ini spec.docx
# experiment: same shape under another root; inputs read from --from
python -m wiki   --config configs/projA.ini --set policy=fast --from data/projA --out /tmp/a spec.docx
python -m linker --config configs/projA.ini --set policy=fast --out /tmp/a
python -m index  --config configs/projA.ini --out /tmp/a --no-publish
python -m publisher assemble --out /tmp/a          # final pages into /tmp/a/wiki/
python -m publisher publish  --config configs/projA_test.ini --out /tmp/a --dry-run
```

This is also how the fast path gets evaluated: the same inputs go to two output
folders, one per policy, and the two are compared.

## 4. Config: one loader, one config per phase

- `common/settings.py` reads INI + `.env` exactly as today. It accepts **every
  current env var and INI key** through a single mapping table, so existing
  `configs/*.ini` and `.env` keep working. Then it builds:
  - `convert.Config`, `wiki.Config`, `linker.Config`, `index.Config`,
    `publisher.Config`, `runner.Config`
  - shared `LLMConfig` / `EmbedConfig` / `JevConfig` embedded where needed
- **New optional INI sections** `[wiki]`, `[linker]`, `[index]`, `[publisher]`
  and `[convert]` override per phase. `--set wiki.policy=fast` overrides from
  the CLI.
- **Phases never see the full settings object.** No `getattr` fallbacks: each
  `Config` field has one default, defined once. For the 25 disagreeing defaults,
  today's `Settings` value wins, because that's what production runs.
- **Dead settings are deleted:** the 46 never-read fields (Appendix A). Re-check
  25 more that are referenced only inside `graph/config.py` (`subagent_*`,
  `mermaid_*`, `weight_*_vec`, `rerank_*`, `wiki_jev_*` …) during the split.
  Delete those only if nothing reads them after the move. The `wiki_jev_*`
  fields feed Jev, so they're expected to survive.

## 5. Fast path inside the phases

`fast-path-proposal.md` stays the behavioural spec. Here is where each item
lands. Every switch is a field of a frozen `Policy` dataclass. `STANDARD`
reproduces today exactly; `FAST = fast-v1`.

**Same output as standard.** Fast writes the same folders and files in the same
formats (`metadata/state/<doc>/`, `wiki/<doc>/`, `_planning/*.json`, catalog,
`pipeline.json`). Everything downstream (linker, index, publisher, growi-search)
reads fast and standard documents identically. The only on-disk difference is
the additive `policy`/version key (rule 3 and rule 8 in §0.1).

### wiki/policy.py

| proposal item | code it changes | `Policy` field (standard → fast) |
|---|---|---|
| parse, docx/pptx/pdf/md seed plans unchanged | — | — |
| hierarchy context without model calls | `wiki/formats/context.py` `summarize_hierarchy` / `context_block` | `hierarchy_context: "llm" → "deterministic"` (path, neighbour titles, seed summaries) |
| skip cross-page reference research | `wiki/pipeline._research_references` | `research: True → False` |
| one rewrite + one judge per section; one targeted repair; then verbatim source + review mark; judge timeout ≠ approval | `wiki/pipeline._write_section` | `repair_attempts: N → 1`, `on_judge_failure: "keep-draft" → "verbatim"` |
| skip intro | `wiki/pipeline._write_intro` | `intro: True → False` |
| strip chapter/section numbering from titles/headings only | new helper in `wiki/page.py` (applied to seed titles and verbatim fallback headings) | `strip_heading_numbers: False → True` |
| keep intra-document links + navigation | `wiki/page.link_titles`, nav footer | — |
| XLSX/CSV stop after table pages; heuristic row structure; keep VBA description; manifest/index fields from sheet name, headers, row count | `wiki/formats/xlsx.py` (`_append_story`), `tabular.py` (`decide_structure` → `heuristic_structure`), `csv.py` | `spreadsheet_story: True → False`, `table_structure: "llm" → "heuristic"` |
| distinct prompt/state version for fast only (standard versions frozen); switching policy on a **changed** source forces a full rebuild of that doc; unchanged source stays a no-op; missing `policy` key = standard | `wiki/incremental.decide_update` + a `policy` field in the wiki's existing state stamp | `version: "standard-vN" / "fast-v1"` |

### linker/policy.py

| proposal item | code it changes | `Policy` field |
|---|---|---|
| batch chunk metadata per page, validate per chunk, retry missing/invalid chunks singly, split oversized pages, per-chunk fallback for unreliable pages; reuse hash cache | `linker/chunks.describe_all` | `metadata: "per-chunk" → "per-page"` |
| exact entity/definition + 1-hop + small FTS/embedding shortlist; reserved cross-doc slots | `linker/neo.candidates`, hop caps | `max_hops: 3 → 1`, `shortlist`, `cross_doc_slots` |
| Jev on the shortlist, stricter cross-doc acceptance | `linker/jev_judge` | `cross_doc_threshold` |
| no routine LLM tie-break; Jev unavailable → exact-match rule (never silently the LLM path) | `linker/service` filter/judge | `tiebreak: "llm" → "none"`, `jev_unavailable: "llm" → "exact"` |
| deterministic curation (keep still-valid choices; inline needs exact anchor) | `linker/service._curate_page` | `curation: "llm" → "deterministic"` |
| own cache versions; record metadata calls, fallbacks, Jev decisions, accepted/omitted edges | `wiki/<doc>/_planning/linker.json`, catalog | `version: "fast-v1"` |

### index and runner

- **index:** `scope="changed"` refreshes changed docs + their ancestors + root,
  and still cleans up deleted documents. The runner stops running a second
  whole-tree pass when the scoped one already reconciled. This also helps
  standard mode, so it isn't fast-only.
- **runner:**
  - `sync --fast` sets both policies. It works with `--isolated` (default) and
    `--no-isolated`.
  - `build`, `watch`, `link` and ordinary `sync` are unchanged unless `--fast`
    is given.
  - It remains one sync implementation: fast is config, not a code path.
- **Acceptance:** `runner/compare.py` (or a test script) runs `wiki` +
  `linker` with STANDARD and FAST on the proposal's corpus into two folders:
  - The corpus: long DOCX with tables, PPTX with images, unstructured PDF,
    XLSX/XLSM with formulas + VBA, a source edit that deletes a fact.
  - It reports wall time and LLM calls per stage from `Context`'s counter.
  - It checks coverage, identifier/table/image preservation, judge omissions,
    stale deleted facts, and link precision vs recall.

## 6. Enforcement (so isolation doesn't decay)

`tests/test_structure.py` uses `ast`, including in-function imports, and fails
on any of these:
1. a phase folder importing another phase, `runner/` or `main`
2. an import of a non-facade name from another folder: only `__all__` of
   `<phase>/__init__.py`
3. `runner/` code other than `steps.py`/drivers calling more than one phase
4. a hard-coded data path or file name outside `common/paths.py`, or a phase
   writing a `DataLayout` path owned by another phase
5. `getattr(settings…)` or a full-settings object inside a phase

Each rule starts with an `ALLOWED` list of today's violations; steps shrink it to
empty.

Tests mirror the folders:
- `tests/<phase>/` runs the phase alone on fixture input folders, with a stub
  model.
- `tests/runner/` runs the runner with stub phases (plain functions that record
  calls), covering batch vs isolated, the retry pass, deferred link, `watch`
  intervals and `--fast` wiring.

## 7. Steps

Each step is behaviour-preserving and lands as its own commit. Run the full
suite after every one.

**Rules for whoever executes this:**
- Prompt text moves byte-for-byte.
- No change to LLM call counts, concurrency, thinking or prompt length (shared
  server). The fast policy is the only intended change in call counts, and only
  when `--fast` is selected.
- **No re-export shims.** Update every importer, tests included.

| step | what | done when |
|---|---|---|
| **0 Guards** | **compat gates G1–G5 (§0.1), incl. `runner/compat_check.py` (G2) run on every real project**; `tests/test_cli_surface.py` (every command/flag in §9). `tests/test_structure.py` with `ALLOWED` lists. **`tests/test_golden.py`**: run build → link → index → publish (FakeGrowiClient, recorded/stub model) on `tests/samples/`; snapshot **the full `data/<project>/` file tree (paths) plus raw, generated state, final `wiki/` pages and GROWI payloads**. Every later step must reproduce them byte-for-byte; the path listing proves the data folder shape never changes. Record baseline failures (e.g. `Publisher.doc_path` in `test_rename_plus_edit`) | guards pass |
| **1 AGENTS.md** | `llm-wiki-air/AGENTS.md` (+ `CLAUDE.md` → `@AGENTS.md`): layout §2, contract §3, data layout §3.2, runner table §3.3, change table §3.4, invariants §8, test command, don't-list. Updated as steps land | exists, linked from README |
| **2 Remove look-alikes** | dead code (Appendix A) incl. `graph/config.py` past L592 + 46 dead fields (their INI/env names stay accepted-and-ignored with a warning, rule 5); rename `wiki/legacy.py` → `chunk_mode.py`; delete the `growi/paths.py` and `growi/publisher.py` shims and the `.reverse_sync` import; delete root `new-growi-search.md` (dup of `docs/`) | AST scan: no unreferenced symbols |
| **3 `common/`** | create `common/` from `graph/common`, `graph/clients`, `graph/wiki/storage`, `markdown_blocks`, `graph/workspace/project.py` (→ `paths.py` `DataLayout`, same paths as today, each accessor tagged with its owner), `publisher/ledger.py` (→ `common/ledger.py`, sole reader/writer of `pipeline.json`); one `markers.py`; one LLM structured call (absorb `legacy.py`'s copy and the linker's hand-rolled try/except; add unused `validate=` hook); one git runner; `httpx` only; update growi-search's `mokuji_data` import | no duplicate helper definitions; layout literals only in `common/paths.py` |
| **4 Configs** | `common/settings.py` loader + per-phase `Config` dataclasses (fields in §3.1); env/INI mapping keeps every current name; replace all `getattr(settings…)` and `settings: Any`; tests build configs via `tests/helpers.py` | rule 5 `ALLOWED` empty |
| **5 Context + events** | `common/context.py`: `Context` (llm, embedder, emit, cancel, LLM-call counter), `Stage` literal, `Event` TypedDict, `ctx.stage(…)` (start before cache/resume checks → done + elapsed, heartbeat); replaces 55+ event dicts and 143 callback params; `runner/progress.py` consumes `Event` | no `on_progress({` literals |
| **6a `convert/`** | from `graph/workspace/{convert,parser_client,xlsm}.py` + `pipeline._parse`, `_check_parse_size`, `_can_resume_parsed_source`, `_assert_source_unchanged`, `_write_raw`; facade + `__main__` | `tests/convert/` passes; golden unchanged |
| **6b `wiki/`** | from `graph/wiki` + `graph/formats` (→ `wiki/formats/`) + `writer.write_wiki_pages` tier/build logic; overlay calls leave (become `force_full` input + publisher's job); records tier / changed / regenerated pages / policy in its existing state stamp (no new file); `PLANNERS` table for formats; `Policy` with `STANDARD` only | `tests/wiki/` runs on fixture raw with stub model; imports only `common/`; golden unchanged |
| **6c `linker/`** | from `graph/linker` + `writer.run_linkers`; fold `graph/linker/__main__` into `python -m linker` + `main.py link` (`calibrate-jev` added); gets changed/regenerated pages from the wiki result; still renders into pages for now (switch in 6e); `chunks.json` etc. stay where they are; `Policy` with `STANDARD` only; split `link_document` (484) into scope → metadata → entities → edges → judge → render | `tests/linker/` on fixture wiki folder; golden unchanged |
| **6d `index/`** | from `publisher/index.py`; renders index pages locally (relations cache stays in `metadata/index/`); GROWI upsert/delete of index pages moves to publisher; `related_documents` moves from `linker/jev_judge` into `index/`; reads `_planning` files read-only | `tests/index/`; golden unchanged |
| **6e `publisher/`** | from `graph/growi/client.py` (REST), `GrowiPublisher`, `publisher/{human_changes,human_semantic,activity,legacy_recovery,live_verification}.py`, `_capture_*`, `_publish_sweep` (minus link/capture/index), `publish_only`, `republish_if_stale`, `reset_growi`; **add `assemble`** (sole writer of final `wiki/<doc>/*.md`); move `render_page` & co. byte-for-byte to `common/links_render.py`; **the linker stops editing pages and adds page edge display data to `navigation.json` (additive key)**; old documents get that key lazily, LLM-free, from the catalog; `footer_span`/`split_footer` stripping disappears; published-page results recorded via `common/ledger.py` (same `pipeline.json`) | **gates:** `assemble` output == today's final pages and GROWI payloads, byte-for-byte, on G1 and on every page of every real project (G2); G3 rollback passes; `tests/publisher/` |
| **7 `runner/`** | `steps.py` (update, remove, move, publish_and_index) from `sync_once`, `move_sources`, `delete_sources`, `build_wiki_only`, `link_raw`. **First diff the build route vs the sync route:** each difference becomes an option or a bug fix with a test. `transaction.py` from `_work_once_locked`, `restore_publication`, `candidate_publication_complete`, history. `queue.py` keeps job-table ops only. Drivers per §3.3 (`main._cmd_sync_isolated` + `cmd_sync` + `queue.serve` → `sync`/`drain`/`watch`). `main.py` → argparse + one runner call | `tests/runner/` with stub phases; `graph/` and the old `publisher/pipeline.py` gone; structure `ALLOWED` lists empty |
| **8a fast wiki** | `wiki/policy.py` `FAST` per §5 | `python -m wiki --set policy=fast` on corpus; `tests/wiki/` covers verbatim fallback, judge-timeout ≠ approval, heading-number stripping, policy-switch full rebuild, spreadsheet stop |
| **8b fast linker** | `linker/policy.py` `FAST` per §5 | `tests/linker/` covers batched metadata validation + per-chunk retry, no LLM tie-break, Jev-unavailable exact rule, deterministic curation |
| **8c scoped index** | `index.run(scope=…)`; runner drops the redundant whole-tree pass | `tests/index/` + `tests/runner/` |
| **8d `sync --fast`** | flag in `main.py` → runner sets both policies; `runner/compare.py` acceptance run | acceptance report standard vs fast on the corpus; G2 on real projects with `--fast` still 0 calls/0 bytes (unchanged docs not reprocessed); G4 mixed-policy passes |
| **9 Split remaining giants** | inside each phase along `ctx.stage` boundaries: `queue.scan` (259); `wiki.run_pipeline` (239) / `_write_section` (192) / `_research_references` (137) with one `cached_json(path, build)` resume helper; `_apply_incremental_edits` (200); `HumanStore.render`/`capture`; `human_semantic.propose` (201); `settings` loader table-driven | no function >~120 lines or >5 params outside prompts |
| **10 Tests tidy** | `tests/helpers.py` (`make_config`, `make_layout(tmp)`, one `FakeGrowiClient` instead of ≥4, one stub model, image-unit builders); move tests into `tests/<phase>/`; retarget ~100 private-function tests to facades; `subTest` for the big repetitive suites; `jev/test_jev.py` → `tests/jev/` | tests import only facades/runner (+ short allowed list) |
| **11 Docs** | `fast-path-proposal.md`, plans and handoffs → `docs/plans/`, `docs/handoffs/`; `codex-logs.md` → `docs/archive/`; README: layout + AGENTS.md link | |

Notes on the order:
- **8a and 8b can start right after 6b/6c,** since they touch only `wiki/` or
  `linker/` and run through `python -m wiki|linker`. Only 8d needs the runner
  (7).
- **To ship fast mode sooner,** do 0–6c first, then 8a/8b, then 6d–7.

## 8. Invariants every step must keep

- **Identity and history.**
  - `source_id` survives rename, move and edit.
  - `refs/llm-wiki/last-good` advances only after successful publication.
  - Candidate worktrees are isolated and cleaned up.
  - Queued blob OIDs are immutable.
  - Recovery is idempotent.
- **Update tiers.**
  - Tiers 0–3 only escalate.
  - Page ranges partition `1..N` contiguously.
  - Unmappable cuts force tier 3.
  - Python, not the model, decides what survives.
- **Human edits.**
  - Captured durably before any overwrite.
  - Re-applied on every tier.
  - Unmatched edits become retained notes, never deleted.
  - HTTP 409 is never blindly merged.
  - Missing markers fail closed.
  - Re-applying the same revision is idempotent.
  - The human store commits and rolls back with wiki and ledger.
- **Linker.**
  - Stable chunk/edge IDs (`id_seed`).
  - Only edges touching regenerated pages are re-checked.
  - Standard-policy LLM verification calls are not capped (nightly, quality
    over speed).
- **Pipeline.**
  - Generation failure never starts a publish.
  - A source change mid-run cancels at the next phase boundary.
  - Only the managed body above `<!-- llm-wiki-bot-ref:… -->` is replaced.
- **Fast mode.** No known missing, changed or invented source facts. Doubtful
  links are omitted.
- **Shared LLM server.** No concurrency bumps, thinking never disabled, prompts
  never truncated.

## 9. Feature checklist (guarded by step 0)

- **Commands:**
  - `check`, `convert`, `publish`, `reset`
  - `build [wiki|link|all]`
  - `pull [--inventory]`
  - `sync [--fast --isolated/--no-isolated --continue --mode --linker --timeout --force]`
  - `watch [--interval --growi-interval]`
  - `queue scan|work|status|retry`
  - `link status|relink|rebuild|calibrate-jev`
  - `index [--no-publish --delete]`
  - `human status|resolve|recover-legacy|live-plan`
- **New:** `python -m convert|wiki|linker|index|publisher`.
- **Modes:**
  - `--mode wiki|chunks`
  - `--linker legacy|neo|off`
  - Jev `torch|gguf|hosted|llm2jev`
  - human-sync `off → observe → apply`
  - policy `standard|fast`
- **Formats:** md/txt passthrough; docx, pptx, pdf, xlsx, xlsm, xls and csv via the
  parser; xlsm static VBA lineage.
- **Other:**
  - index pages + mokuji data (growi-search)
  - failure logs
  - activity-cursor polling + inventory
  - legacy-ancestor recovery
  - live verification
  - progress bars + per-stage timing

## 10. Decisions (defaults chosen; say so to flip)

1. **Human edits live in `publisher/`.** The linker links generated pages, and
   `assemble` applies human edits and links (skipping anchors inside human
   regions). Effect: text that exists *only* in a human edit no longer feeds
   link discovery. Everything else is unchanged, gated by byte-equality on
   current data.
2. **Parsing is its own `convert/` phase.** It's already a separate command and
   service. Folding it into `wiki/` is a folder move if you prefer that.
3. **The code layout changes** to phase folders + `common/` + `runner/`,
   replacing `graph/` + `publisher/`. There is still no wrapper folder;
   growi-search needs a one-line import update. **The `data/` folder shape does
   not change** (§3.2), and the golden test checks it.
4. **Judge failure in standard mode.** Should standard also switch from
   "keep draft after the retry budget" to "verbatim fallback"? The proposal says
   fast only, but it's a fact-loss risk in standard too.
5. **25 disagreeing defaults:** today's `Settings` value wins.
6. **Build route vs sync route (step 7):** each difference found becomes an
   explicit option or a bug fix. Expect a short list.

---

## Appendix A — verified-dead symbols (AST reachability from every importer, incl. growi-search)

Re-check each with `grep -rnw --include='*.py' NAME . --exclude-dir=.venv`
before deleting.

- `graph/config.py`: everything from L592 to EOF. That is: models
  `NodeType`…`EnrichJob`, `LEAD_TOOLS`/`SUBAGENT_TOOLS`, 15 prompt constants, and
  `short_hash`…`_sanitize_message`.
- `Settings` fields never read:
  - `agent_max_steps`, `agent_patience`, `agent_early_exit`,
    `early_exit_candidates`, `shallow_answer_max_nodes`
  - `hf_embed_model`, `hf_device`, `rerank_backend`, `hf_rerank_model`,
    `rerank_device`, `rerank_timeout_seconds`, `rerank_top_k`
  - `database_path`, `edge_candidate_k`, `vector_query_k`, `cascade_max_hops`,
    `cascade_max_nodes`
  - `search_rrf_k`, `search_candidate_pool`, `search_big_chunk_size`,
    `search_big_chunk_overlap`, `search_small_chunk_size`,
    `search_small_chunk_overlap`
  - `engine_semantic_edges`, `vector_backend`, `qdrant_url`,
    `qdrant_collection`, `qdrant_growi_id`, `growi_name`,
    `sync_interval_seconds`, `recluster_every`
  - `pool_node_bm25`, `pool_vec_body`, `pool_vec_summary`, `pool_item_bm25`,
    `pool_vec_item`, `weight_item_bm25`, `weight_node_bm25`, `weight_body_vec`
  - `evidence_max_per_node`, `evidence_max_per_field`,
    `evidence_dedup_char_window`, `evidence_rerank_pool`, `evidence_mmr_lambda`
  - `service_max_reads`, `enable_mermaid`
- `graph/wiki/legacy.py`:
  - pydantic models and records: `FileRef`, `NewFileRef`, `GenerationDecision`,
    `VerificationResult`, `RepairResult`, `ChunkSummary`, `TopicRange`,
    `H1Plan`, `H1Layout`, `LeafPagePlan`, `CurrentFileState`
  - range helpers: `range_to_markdown`, `clamp_range_to_chunk`,
    `full_chunk_range`, `split_chunk_ranges`, `overlap_size`, `fixed_windows`
  - file and filename helpers: `read_lines`, `load_json`, `yaml_quote`,
    `count_file_lines`, `ensure_md_suffix`, `slugify`, `clean_filename_hint`,
    `make_numbered_filename`, `get_next_file_index`, `make_unique_filename`,
    `append_markdown`, `last_n_lines_from_file`
  - manifest and markdown-file writers: `find_file_record`,
    `add_or_update_file_record`, `update_markdown_frontmatter`,
    `create_markdown_file`, `add_chunk_record`
  - other: `find_best_target_for_source_window`, `make_llm`,
    `concept_range_text`
- `graph/growi/client.py`: `_sync_growi_pages_legacy`, `sync_growi_pages`,
  `registry_page`, `_parent`, `GrowiPublisher.publish_document`.
- `graph/formats/tabular.py`: `specs_in_page`, `grids_in_page`,
  `records_from_page`, `query_records`.
- `graph/linker/`: `neo._view`, `neo.filter_candidates`, `neo.inline_targets`,
  `legacy._view`, `legacy.filter_candidates`, `render.FooterLink`,
  `render.display`, `render.parse_footer`, `service._edge_id`,
  `Catalog.chunks_for_team`, `Catalog.document_of`,
  `Catalog.all_edges_for_documents`.
- Misc:
  - `wiki/images.py`: `sanitized_source`, `placeholder_pattern`,
    `missing_image_ids`, `count_units`
  - `common/markdown.py`: `chunk_text`
  - `wiki/pipeline.py`: `_reference_ranges_from_markdown`
  - `wiki/markdown_blocks.py`: `assert_no_unclosed_blocks`,
    `BlockIndex.blocks_overlapping`
  - `workspace/project.py`: `Project.last_sha_path`, `Project.teams`
  - `wiki/prompts.py`: `Prompt.fingerprint`
  - `workspace/writer.py`: the `run_wiki_linker` alias
