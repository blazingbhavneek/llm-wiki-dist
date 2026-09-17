# Doc-parser llm-wiki response contract

This document describes the data that llm-wiki receives from the specialized
doc-parser endpoint. It is intentionally kept in this repository because the
`doc-parser/` directory may be moved to a separate service later.

## Endpoint

The llm-wiki integration calls:

```text
POST {WIKI_PARSER_BASE_URL}/parse/llm-wiki
Content-Type: multipart/form-data
```

`WIKI_PARSER_BASE_URL` may already include the deployment prefix, such as
`/agent/doc-parser`. The prefix is a deployment concern; the profile is selected
by the `/parse/llm-wiki` suffix.

The multipart request contains:

| Field | Required | Meaning |
|---|---:|---|
| `file` | yes | PDF, DOCX, PPTX, XLSX, XLSM, or CSV file |
| `manifest` | no | JSON manifest used by the XLSM/VBA lineage pipeline |

Supported query parameters:

| Parameter | Default | Meaning |
|---|---:|---|
| `images` | `true` | Include extracted image media in each image unit |
| `describe_images` | `true` | Ask the configured vision model to describe images |

When image descriptions are enabled, llm-wiki supplies these headers:

| Header | Meaning |
|---|---|
| `X-LLM-Base-URL` | OpenAI-compatible API base URL |
| `X-LLM-API-Key` | API key |
| `X-LLM-Model` | vision-capable model name |

## Response envelope

A successful response is JSON with this shape:

```json
{
  "markdown": "# Complete document\n...",
  "pages": ["first logical unit", "second logical unit"],
  "parser": "pdf",
  "image_count": 3,
  "duration_s": 12.34,
  "meta": {}
}
```

| Field | Contract |
|---|---|
| `markdown` | Complete parser output. Use this when ingesting the whole document. |
| `pages` | Already-separated logical units. Do not recover these units by splitting `markdown` on headings or separators. |
| `parser` | `pdf`, `docx`, `pptx`, `xlsx`, or `csv`. XLSM also reports `xlsx`. |
| `image_count` | Number of extracted image units. In the llm-wiki profile this can remain nonzero when `images=false`, because media stripping happens after extraction. |
| `duration_s` | Server-side parse duration in seconds. |
| `meta` | Parser metadata. Treat unknown keys as optional and forward-compatible. |

`pages` is always an array. Its meaning depends on the format:

| File type | `pages` entries |
|---|---|
| PDF | pages |
| PPTX | slides |
| XLSX | emitted worksheet parts |
| XLSM | emitted worksheet parts and VBA procedure pages |
| DOCX | always `[]` |
| CSV | always `[]` |

The entries are logical units, not independent documents. A title or other
document-level preamble from `markdown` is not guaranteed to be repeated in
each entry.

PDF responses may be sent with leading JSON whitespace as a heartbeat while
MinerU is running. Consumers must wait for the response to finish and then
parse the body as ordinary JSON.

Unsupported or unrecognized input returns HTTP `415`. Invalid request data
returns a `4xx` response.

## Image units

The llm-wiki endpoint does not return ordinary Markdown images. It wraps every
embedded image in an atomic custom block:

```html
<image-unit>
  <image-media><img src="data:image/png;base64,..." alt="..."></image-media>
  <image-description>LLM-generated description</image-description>
</image-unit>
```

The media MIME type can differ from PNG when the original extracted image is
retained.

Consumer rules:

- Preserve the entire `<image-unit>...</image-unit>` block without splitting,
  escaping, reformatting, or editing its base64 payload.
- `images=false` removes `<image-media>` but keeps the unit and its
  description.
- `describe_images=false` keeps the image unit but leaves its description
  empty. It does not imply `images=false`.
- A vision request can fail while parsing succeeds. In that case the image unit
  can have an empty description.
- Descriptions are model-generated and therefore are not deterministic source
  facts.
- Base64 media can be large. Do not log or duplicate it unnecessarily.

EMF and WMF assets may be converted to PNG before embedding when conversion is
available.

## DOCX

DOCX is converted with Pandoc to GitHub-flavored Markdown.

Typical complete output:

```markdown
# Heading

Paragraph text.

- List item

| Column A | Column B |
|---|---|
| Value | Value |

<image-unit>
  ...
</image-unit>
```

What future consumers should expect:

- headings, paragraphs, lists, links, and tables derived by Pandoc;
- embedded pictures represented as image units;
- occasional raw HTML, including HTML tables, when GFM cannot represent the
  source faithfully;
- no reliable document-page boundaries.

`pages` is `[]`. DOCX page layout depends on a word-processing renderer,
so the service does not invent page divisions.

## PDF

PDF is parsed by MinerU. The exact Markdown depends on the PDF's detected
layout and can include headings, paragraphs, formulas, tables, and image units.

Typical page entry:

```markdown
## Detected heading

Page text and tables.

<image-unit>
  ...
</image-unit>
```

`pages` normally contains one entry per PDF page, derived from MinerU's
`content_list.json` page indexes or explicit page markers. The same image-unit
processing is applied to both `markdown` and `pages`.

If MinerU produces no usable page metadata or markers, the llm-wiki endpoint
falls back to one entry containing the complete processed Markdown. Therefore,
consumers must not assume that `pages.length` equals the physical PDF page
count in every response.

## PPTX

PPTX output is organized by slide. The complete Markdown starts with a
presentation title and then repeats this general shape:

```markdown
# Presentation name

## スライド 1

### Slide title

Slide body text.

- Nested bullet

| Column A | Column B |
|---|---|
| Value | Value |

**画像位置:** 左 10.0%、上 20.0%、幅 30.0%、高さ 40.0%

<image-unit>
  ...
</image-unit>

> **スピーカーノート:**
> Notes text

### スライド全体

<image-unit>
  ...
</image-unit>

---
```

What is included:

- all extracted slide text;
- slide titles and list indentation;
- tables as Markdown pipe tables;
- non-trivial individual pictures, with approximate slide position;
- speaker notes when present;
- one full-slide screenshot under `### スライド全体` when slide rendering is
  available;
- optional LLM descriptions and synthesis for the individual pictures and
  full-slide screenshot.

Tiny icons and decorative fragments can be omitted from individual-image
extraction to avoid noisy output. They remain visible in the full-slide
screenshot. Repeated pictures can have duplicate descriptions suppressed while
their image units remain.

In the llm-wiki profile, slide rendering is best-effort. If LibreOffice or the
required renderer is unavailable, parsing can still return slide text and
individual pictures without `スライド全体`.

`pages` contains one fully processed slide section per slide. The
presentation-level `#` title is not guaranteed to appear in each entry.

## XLSX

XLSX output is organized by worksheet. In the llm-wiki profile, a large or
sparse worksheet may be split into bounded parts for downstream ingestion.

Typical worksheet part:

```markdown
# Workbook name

## シート: Sales-part1

_元範囲: A1:H100_

<table>
  <tr><th>Month</th><th>Total</th></tr>
  <tr><td>April</td><td>120</td></tr>
</table>

### 画像

**画像位置:** Sales シート、セル B2:F20

<image-unit>
  ...
</image-unit>
```

Important details:

- worksheet data uses HTML `<table>` output, not necessarily GFM pipe
  tables; HTML is required for merged cells with `rowspan` and `colspan`;
- formula cells can show both a formula and its cached value, using labels such
  as `数式` and `キャッシュ値`;
- `_元範囲: ..._` records the source cell range for an emitted part;
- worksheet images include their approximate sheet/cell position;
- large worksheets can be divided according to the llm-wiki parser's row,
  column, and rendered-size limits;
- sparse or unusually large sheets can use a compact cell-oriented table;
- normal XLSX parsing emits all worksheets.

`pages` contains one entry per emitted worksheet part. A single workbook
sheet can therefore produce multiple entries. Use these entries directly
instead of splitting `markdown` on `## シート:`.

## XLSM

XLSM uses the XLSX parser and reports `"parser": "xlsx"`. It adds static VBA
lineage information supplied by the llm-wiki manifest.

Macros are never executed. XLSM formula/VBA output is based on static source,
cached workbook values, and inferred references.

### Worksheet pages

Worksheet pages use the XLSX shape and can additionally contain lineage and
VBA-reference blocks:

```markdown
## シート: FinalReport

<!-- sheet-context:start -->
### シートの系譜

- 最終出力シート: FinalReport
- 直接参照シート: Calculations
- 上流シート: RawData

### グラフ

...
<!-- sheet-context:end -->

<table>
  ...
</table>

<!-- vba-references:start -->
### VBA参照

- [RunReport](vba://encoded-procedure-id)
<!-- vba-references:end -->
```

`vba://` links are internal cross-references to VBA procedure pages. They
are not network URLs and must not be fetched.

When an XLSM lineage manifest is present, only worksheets selected for full
emission by that manifest are guaranteed to appear. The manifest describes
final-output sheets and their relevant upstream context.

### VBA procedure pages

Each extracted declaration or procedure is appended as another sheet-shaped
page:

````markdown
## シート: マクロ-Module1-BuildReport

<!-- vba-id: encoded-procedure-id -->

> 警告: VBAは静的に抽出されたもので、実行されていません。

- モジュール: Module1
- 種別: Sub
- 使用箇所: ...
- 読み取りシート: RawData
- 書き込みシート: FinalReport
- 影響する最終出力: FinalReport
- 未解決の動的参照: ...

```vb
Sub BuildReport()
    ...
End Sub
```
````

The exact metadata lines depend on what static analysis can establish. Missing
or unresolved relationships must be treated as unknown, not as proof that no
relationship exists.

`pages` contains the emitted worksheet parts followed by the VBA procedure
pages.

### Raw endpoint versus llm-wiki ingestion

There are two relevant stages:

1. The parser endpoint returns parser-generated `markdown` and `pages`.
2. The current llm-wiki parser client applies
   `graph/workspace/xlsm.py::apply_manifest` to XLSM `markdown`.

That client-side step can reorder sheet sections, normalize or supplement
lineage and VBA references, and append procedure pages. It currently operates
on `markdown`; llm-wiki does not expose the endpoint's `pages` further
down that graph path.

Code that consumes the raw HTTP endpoint must use the endpoint contract.
Code that consumes llm-wiki's post-client Markdown must allow for this
additional XLSM normalization.

## CSV

CSV output contains one document title and one HTML table:

```markdown
# data

<table>
  <tr><th>Name</th><th>Value</th></tr>
  <tr><td>Example</td><td>42</td></tr>
</table>
```

The first CSV row is rendered as table headers. Cell content is HTML-escaped
and embedded newlines become `<br>`. Very large files are truncated at
the configured row limit, which defaults to 5,000 body rows, with a truncation
note appended.

CSV has no images and `pages` is `[]`.

## Consumer checklist

- Use `markdown` as the canonical complete parse.
- Use `pages` for provided PDF, slide, and worksheet boundaries; never
  reconstruct them with a heading regex.
- Treat `pages` as optional logical granularity, not proof of physical page
  count.
- Preserve image units atomically.
- Accept both Markdown and raw HTML tables.
- Accept Japanese synthetic labels and headings; do not parse semantics from
  display text when structured request/manifest data is available.
- Treat `vba://` as an internal identifier scheme.
- Treat all VBA analysis as static and never assume a macro was executed.
- Do not branch on undocumented `meta` keys.
- Do not assume XLSM has a distinct `parser` value.
