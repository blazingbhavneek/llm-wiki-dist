# Implementation plan — pre-ingestion cross-document wiki linker

Status: implemented in `llm-wiki-dist/graph/wiki/linker.py` and wired into
`graph/writers.py`; see `docs/DEV.md` §15 and `docs/RUNNING.md` §20. The spec
below remains the normative contract.

This plan adds a phase to the local wiki maker. It runs after one source document has
been split into seed pages and rewritten, but before those Markdown pages are considered
finished. It researches useful relationships with previously generated wiki pages, then
adds reciprocal links and short explanatory prose to both endpoints.

The phase is intentionally independent from `graph.sqlite`, Librarian, Researcher, GROWI,
and query-time RAG. Its output remains ordinary Markdown, so either downstream direction
remains possible:

1. publish the locally generated wiki directly to GROWI for later cloud-model use; or
2. ingest the same wiki into the current local Librarian/Researcher engine.

The implementer must follow the work packages in order. In particular, do not replace the
exhaustive map comparison with vector-only search. That would largely duplicate ordinary
RAG and miss the non-obvious relationships this feature exists to find.

---

## 1. Required outcome

Assume documents `1..X-1` already exist under `data/wiki/`, and document `X` has just
completed its normal seed/rewrite/intra-document-link phases.

For every new or materially changed page in `X`, the linker must:

1. compare its compact line-range/topic map with every existing same-team document map, in
   bounded page-map batches;
2. use FTS5, embeddings, bridge queries, and optional reranking only as additional discovery
   and prioritization channels, never as the only admission gate;
3. search deliberately for prerequisites, consequences, mechanisms, constraints, failure
   causes, recovery actions, alternatives, workflow stages, evidence, and useful analogies;
4. inspect existing page links up to two hops from promising pages;
5. read the complete current Markdown of both proposed final endpoints;
6. reject links justified only by shared words, entities, topic, or vector similarity;
7. require verifiable evidence in both endpoint pages;
8. add one or two explanatory sentences beside each link so the reader knows where it goes
   and why it matters;
9. edit both Markdown files with direction-specific prose;
10. add the relationship to a managed related-reading footer on both pages;
11. remain idempotent and remove stale reciprocal links after regeneration/rename/removal;
12. recover from an interrupted bilateral write; and
13. update a standalone linker catalog for the next generated document.

Zero accepted links is valid. A weak relationship must not be published to meet a quota.

---

## 2. The anti-goal: do not rebuild query-time RAG

A naked link between pages that both mention `mpf_mfs_open`, or pages returned together by
an embedding search, adds little algorithmic information: a later RAG query already finds
them. Those links may help a human, but they are not enough for this phase.

The linker must seek relationships such as:

- a configuration page that is a prerequisite for an apparently unrelated operation;
- a clock/calibration page that explains a timeout symptom described elsewhere;
- a data format page that constrains a consumer using different terminology;
- an upstream producer and downstream consumer;
- a monitoring signal that supplies evidence for a failure mode;
- two subsystems sharing a hidden resource or lifecycle constraint;
- a recovery procedure that answers a failure described in another document;
- an alternative implementation with a concrete trade-off;
- a conceptual page and a concrete implementation or validation of that concept;
- a contradiction/counterexample that changes interpretation;
- different stages of the same operational workflow; or
- an analogy that genuinely transfers a mechanism.

Keyword/vector similarity may find a starting page, but it is never itself a reason to
publish a link. The final relationship must state a useful bridge supported by both pages.

### 2.1 Hard rejection examples

Reject all of these:

- “Both pages discuss file systems.”
- “This page is related to timeout handling.”
- “For more information, see [Other page].”
- “These pages use similar terminology.”
- a link whose explanation is only the peer title;
- a link justified only by BM25/vector/reranker score;
- a speculative causal claim without evidence in both pages;
- a link inserted only in the new page; or
- a footer link with no nearby explanatory bridge.

### 2.2 Acceptable relationship example

If page A describes early timeout symptoms and page B explains that a hardware clock runs
fast unless calibrated, the forward page might receive:

```markdown
<!-- llm-wiki-link:llink-6d1c...:start -->
> 関連: このタイムアウト値は実クロックのずれでも早く満了し得るため、測定方法と補正値は[クロック校正](../clock/004-calibration.md)で確認できます。
<!-- llm-wiki-link:llink-6d1c...:end -->
```

The reciprocal page must explain the reverse reading direction: for example, that the
timeout symptom is an observable consequence useful when validating calibration. Do not
copy the same sentence in both directions.

---

## 3. Current implementation facts

### 3.1 Entry point and hook

`llm-wiki-dist/wiki_one.py` calls `graph/writers.py::write_wiki` with `mode="wiki"`.
`write_wiki` builds staged output, publishes it under `data/wiki/`, writes the source stamp,
and returns. The linker belongs in `write_wiki`, not in `wiki_one.py`, so every wiki-writer
caller receives identical output.

### 3.2 Existing single-document work

`graph/wiki/pipeline.py` already performs:

```text
observe source windows
  -> build final seed/page map
  -> research references within the source document
  -> rewrite each page
  -> add intra-document title/navigation links
  -> emit state, planning metadata, and Markdown
```

Do not change its lossless rewrite, ownership checks, image restoration, intra-document
links, or provenance. The cross-document linker is a separate post-rewrite phase.

### 3.3 Maps already available

Normally generated documents retain:

- `metadata/state/<document>/state/plan.json`: page title, chapter/path, summary, filename,
  owned ranges, imported/reference ranges, and provenance;
- `metadata/state/<document>/work/observations/live/*.json`: detailed `ObservedRange`
  records with title, kind, summary, parent, enumeration family, flags, and exact ranges;
- `wiki/<document>/_planning/manifest.json`: published filenames/ranges/review data;
- `wiki/<document>/_planning/coverage.json`: page title, summary, and owned range; and
- `wiki/<document>/_planning/metadata.json`: source name and page names.

There is no project-wide map catalog, pre-ingestion vector index, or cross-document link
ledger yet.

### 3.4 Existing reusable clients

Reuse:

- `graph/wiki/model.py::ModelPort` / `ChatModelPort` for bounded model calls;
- `graph/gateway.py::Embedder` for the configured embedding endpoint/fallback;
- `graph/gateway.py::Reranker` for the configured reranker endpoint/fallback;
- Python `sqlite3` FTS5; and
- installed `sqlite-vec` for dense page-map vectors.

Do not add ChromaDB, FAISS, Qdrant, LanceDB, Elasticsearch, or another dependency. One small
standalone SQLite database provides the required separation.

---

## 4. Hard constraints

### C1 — Pre-ingestion only

The linker must run without importing or opening Librarian, Researcher, `GraphStore`,
`data/graph.sqlite`, `data/engine.sqlite`, GROWI clients, or query-time indexes.
`python wiki_one.py <raw-relative-path>` must exercise the complete linker.

### C2 — Markdown is the product

The durable output is edited Markdown under `data/wiki/`. The linker database is a rebuildable
catalog/cache/ledger. Deleting it must not delete normal wiki prose.

### C3 — Retrieval is not the gate

Every same-team existing document map must be compared with every new/changed target page
map. Hybrid retrieval may add/prioritize candidates, but a map-scout candidate survives even
with zero lexical/dense/reranker score.

### C4 — Full-page research

Both final endpoint pages must be completely read before acceptance. Maps are triage only.
If endpoints do not fit one request, use section evidence notes and a final endpoint-specific
call; never silently truncate and call it a full read.

### C5 — Deep relationships only

The result schema must not permit generic `related`, `similar`, or `same_topic` types. A
relationship must state reader value and cite both pages.

### C6 — Additive edits only

The model never rewrites existing prose. Python inserts/removes uniquely marked blocks.
Outside those blocks, files remain byte-for-byte unchanged.

### C7 — Bilateral or absent

A relation is committed only if both pages contain a direction-specific explanatory link
and a related-reading footer entry. If both sides cannot be validated/written, neither side
is committed.

### C8 — Bounded exploration

Existing links may be followed for at most two hops. Model-returned IDs come from a Python
allowlist. The model cannot open files, invent paths, run tools, browse, or request unbounded
reads.

### C9 — Same scope only

Only pages in the same project/team may link. Team means the first component below
`data/wiki/`, matching `graph.project.team_of`. Never escape the wiki root.

### C10 — Current wiki guarantees remain

Do not weaken lossless checks, change ownership/ranges, renumber pages, edit coverage data,
or remove intra-document links. Cross-document prose claims no original-source ownership.

### C11 — No silent incomplete reasoning

Embedding/reranker failure may degrade to map-scout plus FTS. A failed exhaustive comparison
still marks the phase incomplete. An individual final-research candidate is retried, then
logged and skipped so one malformed model response cannot stop unrelated linking work.

### C12 — Source-file scope

Implementation may create/edit only:

- new `llm-wiki-dist/graph/wiki/linker.py`;
- `llm-wiki-dist/graph/wiki/config.py`;
- `llm-wiki-dist/graph/wiki/wire.py`;
- `llm-wiki-dist/graph/wiki/prompts.py`;
- `llm-wiki-dist/graph/project.py`;
- `llm-wiki-dist/graph/writers.py`;
- `llm-wiki-dist/graph/core.py`;
- new `llm-wiki-dist/tests/test_wiki_linker.py`;
- `llm-wiki-dist/tests/test_writers.py`;
- `docs/DEV.md`;
- `docs/RUNNING.md`; and
- this file if implementation discoveries require corrections.

Do not edit Librarian, Researcher, GROWI, graph storage, frontend, dependency files, or
existing rewrite tests unless a public signature requires a mechanical update.

### C13 — Runtime-file scope

The feature may create only:

```text
data/metadata/wiki-linker.sqlite
data/metadata/wiki-linker.sqlite-wal
data/metadata/wiki-linker.sqlite-shm
data/metadata/wiki-linker.lock
data/metadata/state/<document>/work/linker/
data/wiki/<document>/_planning/linker.json
```

Do not create one vector file per page/document. All maps, vectors, caches, relations, and
recovery state belong in the single standalone SQLite database.

---

## 5. Finished flow

```text
write_wiki(project, X)
  +-- existing: build_wiki_output(X)
  |      observe -> seed -> rewrite -> intra-document links
  +-- existing: publish_output(..., wiki/X)
  +-- new: link_generated_document(project, X)
  |      acquire metadata/wiki-linker.lock
  |      recover interrupted bilateral commits
  |      bootstrap/sync existing generated-document maps
  |      identify new/changed/removed X pages
  |      for each new/changed page:
  |        exhaustive old-document map scout
  |        FTS5 + dense page-map retrieval
  |        bridge-probe retrieval + optional reranking
  |        one/two-hop existing-link expansion
  |        bounded full-page deep research
  |        endpoint-pair grounding/novelty judge
  |      stage relation changes in standalone catalog
  |      render both endpoint files
  |      verify bilateral links and activate relation state
  |      update wiki/X/_planning/linker.json
  |      release lock
  +-- existing: write_source_stamp(...)
  +-- return wiki/X
```

The source stamp is written only after linker success. Retry reuses completed map comparisons,
page discovery, candidate research, pair judges, and fully researched pages rather than
repeating successful calls.

---

## 6. Storage contract

### 6.1 Project path and database ownership

Add this property to `graph/project.py::Project`:

```python
@property
def linker_database(self) -> Path:
    return self.metadata / "wiki-linker.sqlite"
```

`graph/wiki/linker.py::LinkCatalog` alone opens/migrates this database. It must:

- use `sqlite3.connect(path, timeout=30)` and `sqlite3.Row`;
- enable foreign keys, WAL, and a 30-second busy timeout;
- store the schema version in `meta`;
- use explicit short transactions for relation state changes; and
- never import `GraphStore`.

### 6.2 Schema version 1

The exact SQL may use normal SQLite spellings, but these logical columns, uniqueness rules,
and foreign-key behaviours are required.

#### `meta`

```text
key TEXT PRIMARY KEY
value TEXT NOT NULL
```

Required keys: `schema_version`, `embedding_fingerprint`, `embedding_dimension`, and
`last_complete_run_id`.

#### `documents`

```text
document_id TEXT PRIMARY KEY
raw_rel TEXT UNIQUE NOT NULL
wiki_rel TEXT UNIQUE NOT NULL
team TEXT NOT NULL
source_sha256 TEXT NOT NULL
map_hash TEXT NOT NULL
map_quality TEXT NOT NULL       -- observed | derived | coverage_only
page_count INTEGER NOT NULL
active INTEGER NOT NULL
updated_at TEXT NOT NULL
```

`document_id` is based on normalized `raw_rel`, not generated titles. It therefore remains
stable when page titles change.

#### `pages`

```text
page_id TEXT PRIMARY KEY
document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE
wiki_rel_path TEXT UNIQUE NOT NULL
filename TEXT NOT NULL
title TEXT NOT NULL
chapter TEXT NOT NULL
summary TEXT NOT NULL
owner_ranges_json TEXT NOT NULL
reference_ranges_json TEXT NOT NULL
map_text TEXT NOT NULL
map_hash TEXT NOT NULL
body_hash TEXT NOT NULL
vector_state TEXT NOT NULL      -- ready | pending | failed
active INTEGER NOT NULL
updated_at TEXT NOT NULL
```

A renamed page is removed + new in schema v1. Source-range overlap may prioritize the new
page during reconsideration, but never silently preserves the old relation.

#### `map_entries`

```text
entry_id TEXT PRIMARY KEY
page_id TEXT NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE
source_start INTEGER NOT NULL
source_end INTEGER NOT NULL
title TEXT NOT NULL
kind TEXT NOT NULL
summary TEXT NOT NULL
parent TEXT NOT NULL
enumeration_family TEXT NOT NULL
flags_json TEXT NOT NULL
quality TEXT NOT NULL           -- observed | mechanical | derived
```

An observation crossing a final page boundary produces one clipped entry per intersected
page. Preserve its text; clip only the numeric range.

#### `pages_fts`

Create an FTS5 table with:

```text
page_id UNINDEXED, title, chapter, summary, map_text
```

Use built-in `unicode61`. Delete/reinsert the row when the page map changes. Do not add a
Python BM25 dependency.

#### `page_vectors`

After the first successful embedding reveals its dimension, create sqlite-vec `vec0` storage:

```text
page_id TEXT PRIMARY KEY, embedding float[DIMENSION]
```

Embed `map_text`, not full body text and never managed cross-link prose. If embedding model
or dimension changes:

1. drop/recreate only `page_vectors`;
2. set active pages to `vector_state='pending'`;
3. preserve maps, comparisons, relations, and runs; and
4. refill vectors incrementally.

#### `page_edges`

```text
source_page_id TEXT NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE
target_page_id TEXT NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE
kind TEXT NOT NULL              -- intra | reference | managed
pair_id TEXT NOT NULL DEFAULT ''
summary TEXT NOT NULL DEFAULT ''
PRIMARY KEY(source_page_id,target_page_id,kind,pair_id)
```

Populate `intra` from safe Markdown links, `reference` from plan provenance, and `managed`
from active cross-document relations. These edges exist only for bounded hop exploration.

#### `map_comparisons`

```text
target_map_hash TEXT NOT NULL
document_block_hash TEXT NOT NULL
prompt_version TEXT NOT NULL
result_json TEXT NOT NULL
created_at TEXT NOT NULL
PRIMARY KEY(target_map_hash,document_block_hash,prompt_version)
```

Cache only validated completions. Never cache a timeout/invalid response as empty.

#### `research_cache`

```text
target_body_hash TEXT NOT NULL
bundle_hash TEXT NOT NULL
prompt_version TEXT NOT NULL
result_json TEXT NOT NULL
created_at TEXT NOT NULL
PRIMARY KEY(target_body_hash,bundle_hash,prompt_version)
```

The bundle hash includes candidate bodies, discovery paths, allowed anchors, and maps. The
final pair judge uses the proposal plus compact, mechanically verified endpoint evidence views.

#### `judge_cache`

Cache each completed pair-judge result by both endpoint body hashes, the complete proposal
hash, and judge prompt version. Cache both acceptance and rejection because both are completed
judgments. Never cache a timeout or invalid response.

#### `page_discovery_checkpoints` and `page_link_checkpoints`

After one target page finishes discovery, persist its bounded candidate list using an input
hash covering the target, all eligible peer page bodies/maps, graph edges, prompt versions,
language, and selection limits. After all candidate research and judges for that target page
finish, persist its validated proposed links under the same input hash. On retry, the latter
skips the whole page; otherwise the discovery checkpoint skips map scouting, retrieval,
bridge probes, reranking, and hop expansion. Any relevant content or graph change invalidates
both checkpoints automatically.

#### `links`

```text
pair_id TEXT PRIMARY KEY
page_a_id TEXT NOT NULL
page_b_id TEXT NOT NULL
relation_type TEXT NOT NULL
discovery_path_json TEXT NOT NULL
evidence_json TEXT NOT NULL
a_anchor_id TEXT NOT NULL
b_anchor_id TEXT NOT NULL
a_bridge_template TEXT NOT NULL
b_bridge_template TEXT NOT NULL
a_footer_reason TEXT NOT NULL
b_footer_reason TEXT NOT NULL
novelty_score INTEGER NOT NULL
usefulness_score INTEGER NOT NULL
confidence_score INTEGER NOT NULL
target_hashes_json TEXT NOT NULL
prompt_version TEXT NOT NULL
status TEXT NOT NULL             -- pending | active | deleting
run_id TEXT NOT NULL
updated_at TEXT NOT NULL
```

There is one undirected relation row, with separate prose for each reading direction.

#### `link_runs`

```text
run_id TEXT PRIMARY KEY
document_id TEXT NOT NULL
status TEXT NOT NULL             -- researching | committing | complete | failed
affected_pages_json TEXT NOT NULL
error TEXT NOT NULL
started_at TEXT NOT NULL
finished_at TEXT NOT NULL
```

`committing` is durable recovery state. A later run must rerender all affected pages from
desired relation state, verify both sides, then activate/delete relations.

### 6.3 Completion marker

After success, atomically write `wiki/<document>/_planning/linker.json`:

```json
{
  "schema_version": 1,
  "prompt_versions": {
    "map": "wiki-link-map-1",
    "bridge": "wiki-link-bridge-1",
    "research": "wiki-link-research-2",
    "judge": "wiki-link-judge-2"
  },
  "source_sha256": "...",
  "document_map_hash": "...",
  "run_id": "...",
  "status": "complete",
  "pages_considered": 4,
  "links_added": 2,
  "links_removed": 1,
  "degraded_maps": 0,
  "overflow_review_required": false
}
```

Put a staged `pending` marker in the document before linking. Update `writers.up_to_date` so
a present pending/failed marker is never up to date. Preserve legacy behaviour when no marker
exists so old output can bootstrap.

---

## 7. IDs, paths, hashes, and managed text

### 7.1 Path rules

- Normalize with `PurePosixPath`.
- Reject absolute/empty/`..` paths.
- Require resolved paths to remain below `project.wiki.resolve()`.
- Resolve symlinks before the containment check.
- Models receive IDs, never filesystem paths.
- Render links relative to the source page's parent using POSIX separators.
- Preserve `.md`; current local/GROWI layouts preserve generated filenames.

### 7.2 Canonical hashes

Canonical JSON means UTF-8, sorted keys, compact separators, `ensure_ascii=False`. Use
SHA-256:

```text
document_id(raw_rel)      = "ldoc-"  + sha256(normalized raw_rel)[:20]
page_id(document_id,path) = "lpage-" + sha256(document ID + NUL + wiki path)[:20]
pair_id(a,b)              = "llink-" + sha256(sorted page IDs joined by NUL)[:20]
entry_id(page,entry)      = "lmap-"  + sha256(page ID + canonical entry)[:20]
```

### 7.3 Managed-block stripping

`strip_managed_links(markdown)` removes only well-formed blocks:

```text
<!-- llm-wiki-link:<pair_id>:start --> ... <!-- llm-wiki-link:<pair_id>:end -->
<!-- llm-wiki-related:start --> ... <!-- llm-wiki-related:end -->
```

Do not remove GROWI chunk markers, source-reference markers, ordinary comments, or intra-doc
navigation. Compute body hashes and research views after stripping, preventing self-citation.
Malformed/nested markers are a hard error with exact file path.

---

## 8. Building page maps

### 8.1 Discover documents

Walk only `project.wiki.rglob("_planning/manifest.json")`. For each result:

1. document folder is `manifest.parent.parent`;
2. read planning metadata, coverage, and manifest;
3. obtain `raw_rel` from `original_file_name`;
4. compute `project.state_dir(raw_rel)`;
5. read `state/plan.json` when present;
6. read `work/observations/live/*.json` when present;
7. include only page files listed by plan/manifest;
8. exclude index, review, planning, hidden, and unlisted Markdown; and
9. reject duplicate normalized paths.

The just-published document is scanned by this same path, not a special current-document
shape.

### 8.2 Metadata precedence

For every page prefer:

1. `state/plan.json` for filename/title/chapter/summary/ranges;
2. manifest + coverage joined by canonical filename; then
3. deterministic Markdown fallback.

Title fallback is first H1 then filename stem. Summary fallback is the first ordinary
paragraph after H1, capped at 500 characters. Do not call a model to reconstruct metadata.

### 8.3 Assign observations

For every `ObservedRange`:

1. validate it is inside source bounds;
2. intersect it with every final owner range;
3. create one clipped entry per non-empty intersection;
4. preserve title/kind/summary/parent/family/flags;
5. mark mechanical observation-window entries `mechanical`;
6. normalize whitespace without paraphrasing; and
7. sort `(source_start, -source_end, title, kind, summary)`.

Deduplicate exactly in this order:

1. remove canonical duplicates;
2. same normalized title/kind/range: keep longer non-empty summary;
3. same title and summary with a contained range: keep containing range;
4. retain remaining overlapping/nested entries.

### 8.4 Degraded map fallback

Without detailed observations, derive entries from title/summary, each heading outside fences,
and the first paragraph below each heading capped at 300 characters. Use the page owner range
and mark quality `derived`. If the body is also missing, use coverage only and mark
`coverage_only`. Never invent ranges or model-generated topics during bootstrap.

### 8.5 Retrieval card

Render deterministically:

```text
PAGE ID: lpage-...
DOCUMENT: team/manual_docx.md
WIKI PATH: team/manual.docx/003-timeout.md
TITLE: Timeout handling
CHAPTER: Runtime > Errors
SUMMARY: ...
OWNED SOURCE: 201-285
IMPORTED SOURCE: 50-55
MAP QUALITY: observed
OBSERVATIONS:
- [lmap-...] lines 201-230 | failure-mode | Timer expiry | parent=Runtime | ...
- [lmap-...] lines 231-250 | constraint | Clock source | parent=Runtime | ...
```

Exclude full body, managed links, base64 media, and full tables.

### 8.6 Existing-link extraction

After all pages are known:

- parse Markdown links ending `.md` outside fences, inline code, images, and HTML;
- resolve relative targets and retain only active same-team page IDs;
- store same-document links as `intra`;
- store provenance imports as `reference`;
- store active linker relations bidirectionally as `managed`; and
- ignore external URLs/fragments.

---

## 9. Candidate discovery

Each candidate record contains target/candidate IDs, discovery sources, discovery path,
specific relationship hypothesis/type, priority, and supporting map-entry IDs. Deduplicate
by endpoint pair, union discovery sources, keep highest priority, and preserve shortest valid
path.

### 9.1 Lane A — exhaustive map scout (required)

For each new/changed target page and every other active same-team document:

1. render target card;
2. pack the old document's whole page cards into blocks capped at 24,000 characters;
3. never split a page card; one oversized card becomes its own block;
4. compare every block with a bounded structured call;
5. cache by target map hash, block hash, and prompt version; and
6. require all blocks to complete before marking the target scouted.

Each call nominates at most three pages. It must use allowed page/map IDs. Python rejects
unknown IDs and candidates outside the supplied document block.

The prompt must state:

- shared subject/words alone are insufficient;
- seek causal, prerequisite, workflow, constraint, failure/recovery, implementation,
  evidence, alternative, contradiction, or mechanism bridges;
- ask what the reader understands/does differently after following the link;
- nomination means “worth full reading”, not “publish”;
- plausible deep uncertainty is preferable to fabricated certainty;
- no specific bridge means no candidate; and
- infer nothing beyond supplied maps.

Map-scout candidates cannot be removed for low retrieval score.

This lane is linear in old documents per target page. Across corpus growth it can accumulate
quadratic compact-map comparisons. That is the explicit recall cost. Hash caching prevents
unchanged pairs being repeated.

### 9.2 Lane B — hybrid accelerator

For target `map_text`:

1. take up to 24 FTS results;
2. take up to 24 dense page-map results;
3. exclude same document/other teams;
4. fuse ranks with RRF `k=60`; and
5. retain up to 24 fused pages as additional seeds.

Embedding failure marks vectors pending and continues with exhaustive map scout + FTS.

### 9.3 Lane C — bridge probes

One structured call per target returns three to six questions, covering prerequisite,
downstream consequence, hidden mechanism/shared resource, failure/diagnosis/recovery,
constraint/tradeoff/alternative, and useful analogy/evidence.

Bad: `timeout processing`.

Good: `What independent clock or scheduling behaviour could make this timeout expire earlier than configured?`

Run each probe through FTS/dense search and add results with `bridge_probe` provenance.

### 9.4 Lane D — optional reranker

Rerank direct/bridge candidates using target card + bridge questions and an instruction to
prefer non-obvious mechanisms/prerequisites/consequences. Keep 12. Never rerank away map
candidates. On failure preserve stable RRF order.

### 9.5 Lane E — one/two-hop expansion

For each map seed and top retrieval seed:

1. take up to four hop-1 edges;
2. take up to four unique hop-2 endpoints total;
3. exclude same document/team violations/inactive/cycles/already selected;
4. prefer high-novelty managed edges, then references, then intra links;
5. record exact path; and
6. add endpoints even with zero retrieval score.

Never traverse past two hops. Existing links are clues, not final evidence.

### 9.6 Research budget

Schedule: map nominations, novel hops, bridge-probe results, then direct hybrid results.
Research at most 12 candidates per target. If map nominations alone exceed 12, record all
overflow IDs/reasons and set `overflow_review_required`; do not silently hide them.

---

## 10. Structured model contracts

Add these Pydantic contracts to `graph/wiki/wire.py`, with `Field` bounds.

### 10.1 Closed relationship type

Use a `Literal` containing only:

```text
prerequisite
upstream_input
downstream_effect
mechanism
shared_constraint
failure_cause
diagnostic_evidence
recovery_action
workflow_next_step
implementation_of
validation_of
alternative_to
tradeoff_with
contradicts
counterexample_to
useful_analogy
```

Never include generic relation names.

### 10.2 `MapLinkCandidate`

```text
candidate_page_id: str
relation_type: LinkRelationType
hypothesis: str                     20..500 chars
reader_value: str                   20..500 chars
target_map_entry_ids: list[str]     1..6
candidate_map_entry_ids: list[str]  1..6
bridge_questions: list[str]         1..4
priority: int                       0..100
```

### 10.3 `MapLinkScanResult`

```text
candidates: list[MapLinkCandidate]  max 3
no_candidate_reason: str
```

### 10.4 `BridgeProbeResult`

`probes: list[str]`, 3..6 unique normalized questions, each 20..300 characters. Retry if
fewer than three remain after deduplication.

### 10.5 `DeepLinkProposal`

```text
endpoint_page_id: str
relation_type: LinkRelationType
discovery_path: list[str]           2..4 IDs
target_evidence: str                exact excerpt, 10..500 chars
endpoint_evidence: str              exact excerpt, 10..500 chars
target_map_entry_ids: list[str]     1..6
endpoint_map_entry_ids: list[str]   1..6
relationship_explanation: str      30..800 chars
why_reader_needs_link: str          20..500 chars
why_not_shared_topic_only: str      20..500 chars
target_anchor_id: str
endpoint_anchor_id: str
target_bridge_template: str         contains {link} exactly once
endpoint_bridge_template: str       contains {link} exactly once
target_footer_reason: str           20..240 chars
endpoint_footer_reason: str         20..240 chars
novelty_score: int                  0..100
usefulness_score: int               0..100
confidence_score: int               0..100
```

Templates are one paragraph, at most two sentences, and direction-specific.

### 10.6 `DeepLinkResearchResult`

```text
proposals: list[DeepLinkProposal]
rejected_page_ids: list[str]
notes: str
```

Only fully supplied pages may be proposed as endpoints.

### 10.7 `LinkJudgeResult`

```text
is_grounded_in_both_pages: bool
is_specific_relationship: bool
is_more_than_shared_topic: bool
is_useful_to_target_reader: bool
is_useful_to_endpoint_reader: bool
bridge_text_adds_no_unsupported_claim: bool
recommended: bool
problems: list[str]
corrected target/endpoint anchor, bridge template, and footer reason fields
```

The judge may reject/tighten prose. It cannot change endpoints, relation type, evidence, or
discovery path.

---

## 11. Deep research protocol

### 11.1 Safe page views

For each page sent to the model:

1. strip linker-managed blocks;
2. compact base64 image media using existing image sanitization;
3. preserve headings, tables, code, identifiers, warnings, and ordinary Markdown;
4. assign allowlisted anchor IDs to `lead` and every heading outside fences;
5. render `anchor_id | exact heading | line number`;
6. attach the page map card; and
7. state page ID/title/document/path.

The model never returns raw paths.

### 11.2 Read bundles

For each primary candidate include:

- complete target page;
- complete primary candidate;
- discovery-path summaries (intermediate hop bodies are researched only when they are
  independently scheduled as endpoints); and
- all maps and anchor allowlists.

Cap the request at 60,000 characters before message overhead. Never truncate a page. If a
neighbour would exceed the cap, put it in a separate bundle with the complete target.

### 11.3 Oversized endpoint procedure

If target + one candidate cannot fit:

1. split only at Markdown heading boundaries;
2. send every section, in order, through a structured evidence-note call;
3. store notes with exact excerpts and section hashes;
4. let the model nominate relevant section IDs;
5. build final input from full maps/headings/notes, every nominated full section, and adjacent
   sections required for context;
6. record use of the oversized procedure; and
7. validate final evidence against supplied full sections.

Do not take the first N characters.

### 11.4 Research instructions

The prompt asks the model to:

- identify specific relationships among fully read pages;
- consider endpoints reached through supplied one/two-hop paths;
- distinguish discovery clues from final evidence;
- quote exact evidence in both endpoints;
- explain what a reader learns by crossing the link;
- explain why this is not shared-topic duplication;
- choose allowlisted anchors;
- write different one/two-sentence `{link}` templates for both directions;
- write concise footer reasons; and
- return nothing when the bridge is generic/speculative.

### 11.5 Mechanical validation

Reject before judge unless every condition passes:

1. endpoint was fully supplied and belongs to another document;
2. endpoint is same-team;
3. path begins at target, ends at endpoint, contains 2..4 IDs, and each step is an allowed
   seed/current catalog edge;
4. relation type is closed/allowed;
5. normalized exact target evidence occurs in stripped target Markdown;
6. normalized exact endpoint evidence occurs in stripped endpoint Markdown;
7. map-entry IDs belong to claimed pages;
8. anchor IDs belong to claimed pages;
9. each template contains `{link}` exactly once;
10. templates have no heading/list/table/fence/HTML/newline;
11. each template is <=2 sentences and <=400 characters;
12. templates contain no prebuilt path or URL;
13. identifiers, numbers, hex values, and error codes introduced by templates occur in at
    least one endpoint/evidence;
14. footer reasons contain no path/URL and fit bounds;
15. explanations are not generic patterns from section 2.1; and
16. novelty, usefulness, and confidence are each >=70.

Return exact validation errors for at most two correction attempts. After two invalid
responses, log and skip that candidate without caching it. Invalid output is not accepted as
a link and does not stop other candidates from completing.

### 11.6 Final pair judge

For each mechanically valid proposal, make one structured call with compact endpoint identity,
summary, anchor allowlists, exact evidence, bounded local evidence context, the proposal, and
anti-no-op rules. Full endpoints were already read by deep research and exact evidence was
already checked mechanically; do not resend them to the judge. Accept only if all semantic
booleans and `recommended` are true. Re-run mechanical checks on corrected prose.

Do not include retrieval scores in this prompt. Scores schedule work; they are not evidence.

### 11.7 Select final links

Sort by:

1. minimum of novelty/usefulness/confidence descending;
2. novelty descending;
3. usefulness descending;
4. shorter discovery path;
5. peer-document diversity; and
6. stable pair ID.

Accept at most three new links per target page. A peer may have at most twelve active
cross-document links. Do not auto-evict an old link; skip/record `endpoint_capacity`.

---

## 12. Rendering both files

### 12.1 Contextual managed block

Each direction renders:

```markdown
<!-- llm-wiki-link:<pair_id>:start -->
> 関連: <direction template with {link} replaced by [peer title](relative/path)>
<!-- llm-wiki-link:<pair_id>:end -->
```

Insertion rules:

1. `lead`: after H1 introduction and before first H2;
2. heading anchor: after that heading's first ordinary paragraph;
3. never inside fence/table/list/HTML/image/existing link block;
4. if no ordinary paragraph follows, insert before next heading/end;
5. multiple blocks at one anchor sort by peer title then pair ID;
6. preserve final-newline convention; and
7. use direction-specific text.

The blockquote adds visible context without pretending it came from source material.

### 12.2 Managed related-reading footer

After existing intra-document navigation render:

```markdown
<!-- llm-wiki-related:start -->
## 関連資料

- [Peer title](relative/path) — direction-specific reason
- [Another title](relative/path) — reason
<!-- llm-wiki-related:end -->
```

Rebuild the entire footer from desired active relations. Sort by title then pair ID. Remove
it when none remain.

### 12.3 Idempotent renderer

Never patch managed prose in place:

1. read current file;
2. remove all valid linker inline/footer blocks;
3. preserve all other bytes;
4. load desired relations;
5. insert inline blocks at current anchors;
6. rebuild footer;
7. validate relative targets; and
8. write only if bytes differ.

If a stored anchor disappeared, mark the relation stale/research it again; never silently
move it to `lead`.

### 12.4 Bilateral verification

Before activation verify:

- both files exist;
- each has exactly one inline block for pair ID;
- each footer has exactly one peer link;
- relative paths resolve to opposite endpoints;
- paths remain same-team/root;
- stripping blocks reproduces pre-render base text; and
- rerender is byte-identical.

Any failure aborts activation.

---

## 13. Commit, recovery, and concurrency

Research concurrency limits active model requests, not whole candidate workflows. Use one
shared request semaphore across all changed pages so a completed research or judge request
immediately releases its slot to the next queued request; local validation and cache writes do
not occupy a model slot.

### 13.1 Process lock

Hold exclusive `fcntl.flock` on `metadata/wiki-linker.lock` from recovery through activation.
Do not hold SQLite write transactions during model/embedding calls.

### 13.2 Normal commit

After research completes:

1. calculate stale and accepted/revalidated relations;
2. union all affected endpoints;
3. in one short DB transaction insert run=`committing`, new links=`pending`, stale
   links=`deleting`, and affected IDs;
4. retain original page bodies in memory;
5. render desired `active + pending - deleting` state;
6. atomically write every changed page with `wiki.storage.write_text_atomic`;
7. verify all affected bilateral state;
8. in a second DB transaction activate pending, delete deleting rows/edges, refresh hashes
   and edges, and complete the run;
9. atomically update `linker.json`; and
10. only then write source stamp.

### 13.3 Caught exception

On a live caught error:

1. atomically restore all in-memory originals;
2. revert pending/deleting DB states;
3. mark run failed with bounded error;
4. leave completion marker incomplete; and
5. re-raise for non-zero command exit.

### 13.4 Crash recovery

At next invocation:

1. find runs still `committing`;
2. render affected pages from `active + pending - deleting`;
3. atomically rewrite/verify them;
4. activate pending/delete deleting relations;
5. complete run; then
6. continue new work.

No permanent backup is needed because edits are additive/marker-owned. If base text changed
outside markers after the pending run began, stop and report the exact path.

---

## 14. Incremental behaviour

### 14.1 First document

Catalog maps/vectors/edges; make no scout/research calls; create no managed links; complete
normally. If embeddings are unavailable, leave vectors pending for next run.

### 14.2 Add document X

Treat every X page as new. Compare all its pages to all older same-team document maps.
Research selected pages/hops, edit X and peers bilaterally, then catalog final files.

### 14.3 Regenerate a document

Before replacing rows, snapshot old page IDs/relations. Classify:

- unchanged: same relative path + map hash + stripped body hash;
- changed: same path, changed map/body;
- new: new path;
- removed: old path absent.

Preserve only unchanged relations. Re-research changed endpoints. Delete relations for removed
pages from both surviving peers. Owner-range overlap may prioritize a replacement but never
auto-retarget a relation.

### 14.4 Rename and deletion

A rename is removed + new, with new page/pair IDs. Old reciprocal blocks are removed; the new
page must earn a new relationship.

Expose but do not yet wire:

```python
remove_document(project: Project, raw_rel: str) -> list[Path]
```

It marks all document relations deleting, rerenders surviving peers, then removes catalog
rows. Wiring raw deletion into `graph/sync.py` is out of this implementation scope.

### 14.5 Version changes

- Map prompt version invalidates map comparisons.
- Bridge version invalidates bridge probes for touched pages.
- Research/judge change revalidates touched-document relations, not whole corpus immediately.
- Embedding model/dimension change rebuilds vectors only.
- Map/body hash changes invalidate only caches naming those hashes.

---

## 15. Configuration

Add only:

| Setting | Default | Environment |
|---|---:|---|
| `wiki_linker_enabled` | `True` | `WIKI_LINKER_ENABLED` |
| `wiki_linker_map_concurrency` | `4` | `WIKI_LINKER_MAP_CONCURRENCY` |
| `wiki_linker_research_concurrency` | `2` | `WIKI_LINKER_RESEARCH_CONCURRENCY` |

Hardcode named constants in `linker.py`:

```text
MAP_BLOCK_CHARS = 24_000
READ_BUNDLE_CHARS = 60_000
DIRECT_K = 24
RERANK_K = 12
MAX_RESEARCH_CANDIDATES = 12
MAX_HOPS = 2
MAX_HOP1 = 4
MAX_HOP2 = 4
MAX_NEW_LINKS_PER_TARGET = 3
MAX_ACTIVE_LINKS_PER_PAGE = 12
MIN_ACCEPT_SCORE = 70
MODEL_ATTEMPTS = 3
CORRECTION_ATTEMPTS = 2
```

Do not add an environment variable for each constant. Disabled mode skips service creation
and writes `linker.json` with status `disabled`; it is not the default.

---

## 16. Progress and artifacts

Emit through the current progress callback:

```text
linker/bootstrap
linker/maps
linker/embed
linker/map_scout
linker/bridge_probe
linker/retrieve
linker/hops
linker/research
linker/judge
linker/commit
linker/done
```

Events include document, target page, current/total, cache status, and candidate/link counts.
Never log full bodies, API keys, or base64 media.

Write under `metadata/state/<document>/work/linker/<run-id>/` only:

```text
run.json
target-<number>/map-scout-<block-hash>-prompt.md
target-<number>/map-scout-<block-hash>-response.json
target-<number>/bridge-prompt.md
target-<number>/bridge-response.json
target-<number>/research-<candidate-id>-prompt.md
target-<number>/research-<candidate-id>-response.json
target-<number>/judge-<pair-id>-prompt.md
target-<number>/judge-<pair-id>-response.json
```

Errors use `-error.txt`. Cache hits go in `run.json` instead of duplicating old prompts.
`run.json` reports maps scanned/cached, candidates by lane, researched/rejected/accepted links,
stale/capacity/overflow results, degraded maps, optional service failures, and final status.

---

## 17. File-by-file implementation

### `graph/wiki/config.py`

Add only these versions; do not change existing phase versions:

```text
LINKER_MAP_PROMPT_VERSION = "wiki-link-map-1"
LINKER_BRIDGE_PROMPT_VERSION = "wiki-link-bridge-1"
LINKER_RESEARCH_PROMPT_VERSION = "wiki-link-research-2"
LINKER_JUDGE_PROMPT_VERSION = "wiki-link-judge-2"
```

### `graph/wiki/wire.py`

Add section 10's model-facing Pydantic contracts only. SQLite rows/renderer records do not
belong here.

### `graph/wiki/prompts.py`

Add pure builders:

```text
map_link_scan_prompt
bridge_probe_prompt
deep_link_research_prompt
link_pair_judge_prompt
```

Each uses its matching version, `COMMON_RULES`, JSON schema, anti-no-op rules, ID allowlist,
read/evidence bounds, and bilateral `{link}` requirements. Builders do no I/O.

### `graph/project.py`

Add only `linker_database` and reuse existing metadata/state/work/wiki paths.

### `graph/core.py`

Add the three settings/env mappings in section 15 using existing boolean parsing.

### New `graph/wiki/linker.py`

Keep version 1 in this one module; do not split repository/service/agent packages.

Internal records:

```text
MapEntryRecord
WikiPageRecord
WikiDocumentRecord
LinkCandidate
AcceptedLink
```

Catalog API:

```text
LinkCatalog.open/migrate/close
recover_pending_runs
sync_document_maps
active_documents / active_pages
fts_search / vector_search
comparison_get / comparison_put
research_get / research_put
edges_from / links_for_page
begin_relation_commit / finish_relation_commit / fail_relation_commit
```

Pure helpers:

```text
normalize_rel_path
strip_managed_links
extract_heading_anchors
extract_local_page_links
assign_observations
render_page_map
make_document_blocks
rrf_fuse
validate_map_scan
validate_proposal
render_managed_page
verify_bilateral_links
```

Orchestration:

```text
async discover_candidates(...)
async research_target_page(...)
async link_generated_document(...)
remove_document(...)
```

`link_generated_document` is the sole writer entry point and owns locking, recovery,
bootstrap, discovery, research, commit, artifacts, and marker.

### `graph/writers.py`

After `publish_output`, before `write_source_stamp`:

1. create/reuse `ModelPort` exactly as `run_wiki` does;
2. reuse passed embedder or lazily construct `Embedder(settings)`;
3. try `Reranker(settings)`, degrade to `None` with reported error;
4. invoke async linker through existing `_run_async_blocking`;
5. forward project/rel/model/settings/services/progress/cancellation; and
6. stamp only after success.

Disabled mode constructs no services. Update `up_to_date` for completion marker semantics.
Do not modify `wiki_one.py`; it already routes through `write_wiki`.

### Tests and documentation

All new tests go in one new `tests/test_wiki_linker.py`; update only writer integration
assertions in `test_writers.py`. After implementation update `DEV.md` and `RUNNING.md`.

---

## 18. Work packages

Complete one package at a time. Do not start the next until its verification passes.

### WP-0 — Freeze current behaviour

Goal: prove the existing writer is green before linker changes.

Steps:

1. Run writer, chunking, page, and incremental tests.
2. Record pre-existing failures.
3. Add no code.

```bash
cd llm-wiki-dist
.venv/bin/python -m unittest \
  tests.test_writers \
  tests.test_wiki_chunking \
  tests.test_wiki_page \
  tests.test_wiki_incremental
```

### WP-1 — Contracts and prompts

Goal: make weak-model output closed, bounded, and mechanically checkable.

Files: `config.py`, `wire.py`, `prompts.py`, new linker test file.

Steps:

1. Add prompt versions without changing current versions.
2. Add relation enum and result schemas.
3. Add four pure prompt builders.
4. Test field bounds and rejection of generic relation types.
5. Test prompts contain anti-no-op, exact-evidence, allowlist, bilateral, and `{link}` rules.
6. Test fingerprints change with versions.

No service calls in this package.

### WP-2 — Catalog and map extraction

Goal: reconstruct the project map from current writer artifacts.

Files: `project.py`, new `linker.py`, tests.

Steps:

1. Add database path and schema/migration.
2. Add IDs/hashes/path validation.
3. Discover generated documents.
4. Implement metadata precedence.
5. Assign/deduplicate observations.
6. Implement degraded derived maps.
7. Render deterministic cards/blocks.
8. Upsert documents/pages/entries/FTS by hash.
9. Extract safe page edges.
10. Prove a second sync changes no rows/files.

No model calls yet.

### WP-3 — Embedding and hybrid acceleration

Goal: add optional acceleration without making it a correctness gate.

Steps:

1. Create vec table after discovering dimension.
2. Embed only changed/new map cards.
3. Implement fingerprint/dimension rebuild.
4. Implement safe FTS query construction.
5. Implement dense search and team/document filters.
6. Implement deterministic RRF.
7. Add optional reranking.
8. Prove service failure leaves exhaustive discovery usable and vectors pending.

### WP-4 — Exhaustive scout and bridge probes

Goal: admit pages ordinary retrieval misses.

Steps:

1. Build 24k document blocks without splitting pages.
2. Compare every target against every same-team old block.
3. Validate all returned page/map IDs.
4. Cache valid completions by hashes/version.
5. Retry/resume; never cache failure as empty.
6. Generate/validate bridge questions.
7. search bridge questions by FTS/vector.
8. Merge lanes without score-gating map candidates.

Critical test: fake FTS/vector/reranker returns no calibration page, while map scout nominates
it for a timeout page. It must reach research.

### WP-5 — Hops and deep research

Goal: read complete pages and discover useful endpoints up to two links away.

Steps:

1. Expand bounded edges with exact paths.
2. Build full-body bundles split only by page.
3. Implement oversized evidence notes.
4. Run/cache structured deep research.
5. Implement every mechanical check.
6. Retry invalid output with exact errors.
7. Run final endpoint-only judge.
8. Revalidate corrected prose.
9. Select at most three high-quality links.

Critical tests:

- generic same-topic is rejected;
- invalid evidence is rejected before judge;
- zero-score two-hop endpoint can be accepted;
- full endpoint text appears in research input, while judge input contains verified evidence
  and bounded local context;
- unknown IDs and third-hop paths fail; and
- model-declared no relation yields zero links, not an error.

### WP-6 — Bilateral renderer

Goal: add explanation/footer while preserving all source-derived bytes.

Steps:

1. Strictly parse/strip managed blocks.
2. Extract safe anchor IDs.
3. Render direction-specific blockquotes.
4. Render portable relative links/footer.
5. Rebuild from relation state.
6. Verify both sides and idempotence.
7. Test Japanese/nested paths, fences, tables, HTML, images, and existing links.
8. Test relation removal restores exact base bytes.

### WP-7 — Transaction and recovery

Goal: never knowingly commit one side only.

Steps:

1. Add process lock.
2. Add run/pending/deleting transitions.
3. Render all affected pages from desired state.
4. Roll back caught write failures.
5. Recover interrupted commits next invocation.
6. Detect outside-marker base changes.
7. Refresh active relations/page edges.

Inject failure on the second page write, then simulate restart. Recovery must yield two valid
sides or neither, never an active one-sided pair.

### WP-8 — Writer integration

Goal: make `wiki_one.py` run the linker automatically.

Files: `core.py`, `writers.py`, writer/linker tests.

Steps:

1. Add settings/env mappings.
2. Add pending completion marker before link work.
3. Hook after publication and before stamp.
4. Reuse/construct model/embedder/reranker as specified.
5. Forward progress/cancellation.
6. Update `up_to_date` for incomplete runs.
7. Test disabled mode without service construction.
8. Test first document, second-document bilateral edit, idempotent rerun, failure/resume.

### WP-9 — Incremental reconciliation and docs

Goal: handle regeneration and document operations.

Steps:

1. Classify unchanged/changed/new/removed pages.
2. Preserve only unchanged relations.
3. Re-research changed endpoints.
4. remove stale reciprocal blocks.
5. Add unwired/tested `remove_document` API.
6. Update DEV/RUNNING documentation.
7. Run complete suite.

---

## 19. Required test matrix

`tests/test_wiki_linker.py` must cover every behaviour below.

### Map/catalog tests

- creates only declared runtime files;
- assigns observed ranges to correct owner pages;
- clips cross-boundary observations into both pages;
- deduplicates overlapping-window duplicates;
- preserves nested distinct observations;
- joins manifest/coverage canonical filenames;
- derives maps without observation state;
- ignores index/review/unlisted Markdown;
- rejects root escape/cross-team records;
- second sync is idempotent;
- changed map reindexes only changed page;
- dimension change rebuilds vectors but preserves links.

### Discovery tests

- scans every same-team old document block;
- skips another team;
- reuses cached map comparisons;
- retries/fails incomplete block;
- keeps map candidate with zero retrieval score;
- adds FTS/vector candidates;
- bridge probes are questions, unique, and searched;
- reranker failure preserves candidates;
- reranker cannot remove map candidates;
- hops stop at two and reject cycles/unknown pages;
- overflow is visible.

### Research/novelty tests

- both full endpoint bodies supplied;
- managed prose stripped before research;
- generic shared-topic proposal rejected;
- relation enum enforced;
- exact evidence required on each side;
- map-entry ownership enforced;
- unknown page/anchor rejected;
- one `{link}` required per direction;
- unsupported identifiers/numbers rejected;
- low scores rejected;
- all judge booleans required;
- retrieval score absent from judge prompt;
- lexically dissimilar two-hop relationship accepted;
- zero proposals yields zero links.

### Renderer/commit tests

- explanatory block exists on both pages;
- reverse prose differs from forward;
- footer exists on both pages;
- nested relative links resolve;
- stripped base prose remains byte-identical;
- fences/tables/images unchanged;
- rerender byte-idempotent;
- stale removal cleans both pages;
- missing anchor requires re-research;
- write failure rolls back;
- interrupted pending run recovers;
- active relation cannot be one-sided;
- process lock prevents overlap.

### Writer tests

- first document catalogs with no research;
- second document updates an old document;
- source stamp follows linker completion;
- pending/failed output is not up to date;
- disabled linker constructs no services;
- `wiki_one.py` needs no linker-specific option.

---

## 20. Deterministic smoke corpus

Create temporary fixtures inside the test; add no permanent fixture files.

### A — symptom

A page describes an operation timing out earlier than configured. It uses request-expiry and
retry vocabulary, not clock calibration.

### B — hidden mechanism

A page describes a physical/control clock running several percent fast and requiring a
calibration coefficient. It uses oscillator/measurement/correction vocabulary, not timeout.

### C — lexical distractor

A page repeatedly says timeout but only discusses UI session expiration and has no mechanism
connection to A.

### D — hop endpoint

A validation page is already linked from B and explains how measured drift confirms whether
calibration is correct.

Force fake services so:

- FTS/vector omit B and rank C;
- reranker ranks C first;
- exhaustive scout nominates B with a mechanism hypothesis;
- hop expansion exposes D;
- deep research rejects C, accepts A↔B, and accepts A↔D only with independent evidence.

Assert B reaches research despite zero score; C is rejected; A/B get distinct reciprocal
sentences/footers; paths resolve; rerun changes nothing; changing/removing B removes A's old
backlink.

This is the minimum proof that the feature does more than ordinary RAG.

---

## 21. Real-model acceptance

After unit tests:

1. generate real document A;
2. generate unrelated document C;
3. generate B with a known non-obvious relation to A;
4. inspect every artifact/page edit;
5. rerun B unchanged;
6. modify one B source section and regenerate;
7. rename/remove B's linked page through a regenerated plan; and
8. confirm A is reconciled.

Record map blocks, cache hits, candidates per lane, full-page reads, generic rejections,
direct versus hop links, optional service failures, wall time/calls, and each accepted pair's
evidence/prose.

For every accepted link ask:

1. Would ordinary retrieval obviously return this page?
2. Regardless, does explanation add a specific mechanism/prerequisite/consequence?
3. Are both direction sentences useful and grounded?
4. Does following the link fulfill its promise?
5. Should any weaker generic link be deleted?
6. Did any bytes outside markers change?

Do not accept a real run that produces only shared-topic links.

---

## 22. Complexity and calls

Let `P` be new/changed pages, `D` old same-team documents, `B_d` map blocks in document `d`,
and `C<=20` researched candidates per target.

```text
map scout calls     = sum(P * sum(B_d)), minus cache hits
bridge calls        = P
embedding queries   = changed maps + bridge probes
rerank calls        <= P
deep research calls <= P * C, normally less
judge calls         = mechanically valid proposals
file writes         = target pages + reciprocal old endpoints
```

There is no asymptotic claim that exhaustive recall is free. Building a corpus incrementally
can accumulate quadratic compact-map comparisons. Caching avoids repeat work; full-page work
is bounded to candidates, not all page pairs. If measured cost later becomes unacceptable,
the next upgrade is a tested durable taxonomy/bridge index. Do not remove exhaustive scanning
merely because vector search is faster.

---

## 23. Failure policy

| Failure | Behaviour |
|---|---|
| no old documents | complete with zero links |
| embedding unavailable | map scout + FTS; vector pending |
| reranker unavailable | stable pre-rerank order |
| map scout fails after retries | no edits; incomplete; resume |
| invalid IDs/evidence | correction retry, then incomplete |
| research says no relationship | valid rejection |
| judge rejects | no relation; continue |
| candidate overflow | cap, expose review status |
| endpoint disappears | abort/rollback/rescan |
| anchor disappears | re-research, never move silently |
| second file write fails | rollback; crash uses pending recovery |
| malformed marker | stop with exact path |
| cross-team/path escape | reject before access |
| cancellation | stop before commit or finish/rollback short commit |
| catalog deleted | rebuild maps/edges; existing markers remain parseable |

Unknown due to failure must never become “no link exists.”

---

## 24. Definition of done

- `wiki_one.py` automatically exercises the linker.
- No graph/GROWI/Librarian database is required.
- One standalone SQLite file stores maps/vectors/link state.
- Detailed observations map to final pages/ranges.
- Every old same-team document map is examined per changed target.
- Retrieval accelerates but does not gate.
- Bridge probes/two-hop expansion surface zero-similarity endpoints.
- Both full endpoints are read before acceptance.
- Generic shared-topic links are rejected.
- Evidence exists in both pages.
- Both pages receive direction-specific explanatory prose and footer entries.
- Existing bytes outside markers remain unchanged.
- Relative links are portable, same-team, and resolvable.
- Reruns are byte-idempotent and cached.
- Changed/removed pages clean stale reciprocal links.
- Interrupted writes recover without an active one-sided pair.
- Smoke test proves a non-obvious retrieval-missed link.
- Existing wiki tests and whole suite pass.
- DEV/RUNNING describe implemented reality.

---

## 25. Final review checklist

- [ ] No Librarian/Researcher/GraphStore/GROWI imports.
- [ ] No new dependency.
- [ ] Only declared source/runtime files created.
- [ ] Prompt versions isolated from rewrite versions.
- [ ] Schema/migration tested.
- [ ] Observation-to-owner mapping exact.
- [ ] Degraded maps visible.
- [ ] All old document maps scanned in tests.
- [ ] Failed map call cannot masquerade as empty.
- [ ] Map candidate survives zero retrieval score.
- [ ] Bridge probes are questions, not summaries.
- [ ] Hop limit enforced mechanically.
- [ ] Full endpoints/evidence validated.
- [ ] Generic relationship types impossible.
- [ ] Retrieval score absent from final judge.
- [ ] Both templates contain one Python-rendered link placeholder.
- [ ] Existing bytes preserved outside markers.
- [ ] Footer and inline explanation exist on both sides.
- [ ] Pending/deleting recovery tested.
- [ ] Failed linker cannot be hidden by up-to-date stamp.
- [ ] First-document and idempotent rerun paths are cheap.
- [ ] Complete suite green.
- [ ] Real run produces a genuinely useful non-obvious bridge.
