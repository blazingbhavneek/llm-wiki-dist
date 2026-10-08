# Fast repair midpoint review

The user wants a quick human-quality comparison of the fast repair output. This
is read-only. Do not run scripts, tests, linters, sync, repair, publish, or build
new tooling. Read the pages and source ranges directly, then give a concise
report with specific failures and examples.

The current repair may still be running. Pages are committed one at a time.
Only review a page when its mini sidecar has the current repair version
and `repair_status` is `clean`, `repaired`, or `review`. For a `repaired` page,
the mini pure page should differ from the baseline. For `clean` and `review`, it
should match. If uncertain whether a page finished, skip it and move to the
next committed page. Do not wait for the whole run.

## Where to read

Baseline generated page, untouched by repair:

`data/東北中給需給運用計画_test/metadata/state/<state-doc>/wiki/<page>.md`

Current repair output:

`data/東北中給需給運用計画_mini/metadata/state/<state-doc>/wiki/<page>.md`

The matching sidecar is:

`data/東北中給需給運用計画_mini/metadata/state/<state-doc>/state/pages/NNN.json`

The source snapshot is:

`data/東北中給需給運用計画_mini/metadata/state/<state-doc>/source/original.md`

In the sidecar, read `source_ranges` and `reference_ranges`; inspect only those
numbered source lines. Compare the baseline page, repaired page, and allowed
source evidence. Read the repair prompt/judge artifacts only when needed to
understand why a bad result passed or a good result was rejected:

`data/東北中給需給運用計画_mini/metadata/state/<state-doc>/work/repair/<version>/page-NNN/`

`<state-doc>` is the same document folder with the `.pdf` suffix (for example,
the raw file suffix `_pdf.md` maps to a state folder ending in `.pdf`). The
page filename remains unchanged when its H1 title is repaired.

## Review these known examples first

Use the filename prefix shown; read only examples whose sidecars are committed.

- `D版送付用資料(追加)/M要件-266D_【27年度制度対応_工程2】_需給計画_pdf.md`: `005-`, `011-`, `002-`, `014-`
- `D版送付用資料(追加)/#33136_予備率一定配分全般（需計画面）_r4_pdf.md`: `021-`, `003-`, `002-`, `016-`
- `B1/(社外秘)需給運用計画 週間需給運用計画 機能仕様書_pdf.md`: `017-`, `119-`, `134-`, `061-`, `097-`, `158-`
- `要求仕様書/要求定義書（2027年度制度対応）_pdf.md`: `125-`, `051-`, `015-`, `020-`, `084-`, `085-`

Page `061-` in the B1 document is a positive control: its merged-cell HTML
table should stay HTML and keep its `rowspan`/`colspan` structure.

The mini project has four documents. Its source mount is isolated to those four
files. Do not inspect the source PDFs; use the converted source snapshot and the
sidecar line ranges.

## What to judge

For each page, compare the old page with the new page and use only that page's
listed raw source lines to decide whether the new content is better and remains
grounded. Ignore TOC/index pages and all wiki-link differences: they will be
rebuilt after repair and are outside this review.

For each content page, answer plainly:

1. Did repair remove the bad repetition, Chinese insertion, refusal, off-topic
   content, or formatting error?
2. Did it preserve the important, useful facts, identifiers, code, and units in
   the listed ranges?
3. Did it add anything unsupported by those ranges?
4. Is the title concise, natural Japanese, and accurate?
5. Did it preserve image markup exactly? Ignore image meaning and descriptions.
6. If it failed, what is the concrete cause: judge false negative, writer
   ignored feedback, or a source oddity that should have been left alone?

Do not require source headings, section/figure/page/list numbers, ordering, or
raw table layout to appear in the Wiki. Judge factual coverage and Japanese
Wiki quality; the Wiki may reorganize facts and tables naturally.

Do not flag a missing section number, figure/table number, page number, empty
placeholder, OCR artifact, duplicated raw line, or other source noise as a Wiki
defect. Do not ask the writer to reproduce such material. Treat source ranges
as evidence for useful facts only; distinguish substantive technical values
and identifiers from document scaffolding and conversion noise.

When a bad page is marked `clean`, call that a judge false negative. When a page
is `review`, note whether its original text was retained. Do not expect the
partially processed document's promoted `wiki/`, plan, manifest, or review
report to be current; inspect the pure state pages above. Do not report missing
or changed wiki links as repair defects. Only report missing source-backed page
content or unsupported new content.

## Model comparison

Newly committed pages record `repair_model` in their sidecars and
`model.json` in the page artifact folder. Older commits from before that field
was added must be attributed using the model configured when that run was
started. Resuming the same mini project with a new model skips clean/repaired
pages and retries unfinished/review pages, so report it as a continuation, not
a fair A/B comparison. For a fair comparison, use a separate fresh copy of the
untouched `_test` baseline per model.

## Report

Keep the result short. Give a total reviewed count and one row per inspected
page with its status, attempt count, pass/fail, and one-sentence reason. End
with the most important missed defect or losslessness issue, if any. Refer to
private content by document/page/source line range; avoid pasting long source
passages.
