# Generic and llm-wiki Parser Split

This is the implementation plan for adding a generic Markdown parser route while
preserving the current llm-wiki-specific processing behind a dedicated route.
No implementation is included here.

## 1. Routes and profile selection

Keep URL_PREFIX as a deployment mount only:

URL_PREFIX=/agent/doc-parser

Expose:

- POST /agent/doc-parser/parse
- POST /agent/doc-parser/parse/llm-wiki

Do not infer behavior from URL_PREFIX text, filenames, headers, or manifests.

The generic /parse route must:

- Produce ordinary Markdown.
- Embed images as Markdown data URLs.
- Never call an LLM.
- Never emit image-unit or image-description tags.
- Preserve useful image alt/location text.
- Return PDF pages, PPTX slides, XLSX sheets, and XLSM units through pages.
- Keep each generic XLSX worksheet whole.
- Emit PPTX text followed by one complete-slide screenshot.
- Emit all XLSM worksheets, one VBA-code page, and one VBA-final-output page.

The /parse/llm-wiki route must preserve all current behavior:

- Image descriptions and image-unit blocks.
- Existing image stripping.
- PPTX individual-image descriptions.
- PPTX slide synthesis and judge/revision loop.
- XLSX/XLSM manifests, lineage, splitting, VBA references, and vba:// links.
- Existing LLM headers and manifest handling.

The graph pipeline must migrate to /parse/llm-wiki before deployment.

## 2. HTTP response contract

Modify doc-parser/formats/base.py::ParseResult to add:

    pages: list[str]

The JSON response must contain:

- markdown: complete document Markdown with the existing joiners.
- pages: ordered page/sheet/unit Markdown strings.
- parser.
- image_count.
- duration_s.
- meta.

Use these page rules:

| Format | pages |
|---|---|
| PDF | One item per PDF page |
| PPTX | One item per slide |
| XLSX | One item per worksheet |
| XLSM | All worksheets, then VBA code, then VBA final output |
| DOCX | Empty list |
| CSV | Empty list |

Keep pages as a list of strings. Do not add page objects or metadata in this
change. markdown remains the compatibility field used by existing consumers.

## 3. Internal extraction result

The current BaseParser._extract methods return only text. Add an internal
ExtractedDocument structure in formats/base.py with:

- markdown: raw full-document Markdown.
- pages: raw page/sheet Markdown.
- markdown_path: path used to resolve relative image references.
- asset_root: safe directory boundary for image resolution.

Update the abstract _extract return type and every concrete parser. The internal
structure must not be exposed directly in the HTTP API.

## 4. Profile-aware image embedding

The existing utils/markdown_images.py image pipeline creates image-unit blocks.
Keep that output for llm-wiki and add an explicit generic style.

Generic style:

    ![alt text](data:image/png;base64,...)

Generic rules:

- Never construct an LLMClient.
- Never schedule network work.
- Preserve alt text.
- Preserve worksheet-cell and slide-position context.
- Continue vector-to-PNG conversion when needed.
- Never create image-unit or image-description.

For generic images=false, remove the data URL but retain the image alt/location
text as readable Markdown/text. Apply this to markdown and every pages entry.
Keep the existing image-unit stripping behavior for llm-wiki.

Make image_count profile-aware:

- Generic counts embedded Markdown images.
- llm-wiki continues counting image-unit blocks.

## 5. BaseParser.parse flow

Refactor BaseParser.parse in formats/base.py:

1. Create the temporary working directory.
2. Call _extract.
3. Receive ExtractedDocument.
4. For generic:
   - Embed images in markdown as Markdown data URLs.
   - Embed images in every pages entry.
   - Apply generic images=false.
   - Count Markdown images.
5. For llm-wiki:
   - Preserve the current image-unit pipeline.
   - Apply current images=false behavior.
   - Count image units.
6. Return ParseResult with markdown and pages.

Do not embed the same image twice unnecessarily. Use markdown_path and asset_root
for all relative-image resolution, and never allow references outside the job
directory.

## 6. Server changes

Modify doc-parser/server.py.

Move the current request implementation into one internal handler that accepts a
profile argument. Add two thin FastAPI routes:

- /parse calls the handler with the generic profile.
- /parse/llm-wiki calls the handler with the llm-wiki profile.

Keep unchanged:

- Empty-file validation.
- Manifest JSON validation.
- Format detection.
- Worker creation and cancellation.
- PDF streaming heartbeats.
- HTTP 415 and parse-error handling.
- X-Parser response header.
- /health and /workers.
- URL-prefix middleware.

Generic requests must never create an LLM client. Ignore describe_images for
compatibility, or reject it only if the API contract is explicitly changed.
Caller-supplied manifests must not select or split generic workbook content.

llm-wiki requests continue accepting manifest and all X-LLM-* headers.

## 7. DOCX

Modify doc-parser/formats/docx.py.

Generic DOCX keeps:

- Pandoc conversion.
- Markdown structure.
- Tables.
- Extracted images.
- Vector-image conversion.
- Original image alt text.

Generic DOCX removes only LLM description/image-unit processing and returns
pages as an empty list.

llm-wiki DOCX keeps the current Pandoc, vector conversion, LLM description,
image-unit, and media-policy flow exactly.

## 8. PDF

Modify doc-parser/formats/pdf.py.

Generic PDF keeps MinerU, tables, formulas, layout, extracted images, and
streaming. Remove LLM descriptions and image-unit output.

Add a page extraction helper:

1. Run MinerU as today.
2. Locate its page-aware output artifact, preferably the content-list or
   equivalent JSON containing page_idx.
3. Group Markdown blocks by page index.
4. Preserve image references in the proper page group.
5. Return full MinerU Markdown as markdown.
6. Return ordered page Markdown strings as pages.
7. Apply generic Markdown image embedding to both.

Do not guess page boundaries from headings. Support explicit backend page markers
as a fallback. If no reliable page boundary source exists, raise a clear
MineruError rather than returning incorrect pages.

llm-wiki PDF keeps current full Markdown and image-description behavior. Add pages
around the final processed content so page entries contain the same image-unit
output as markdown.

## 9. PPTX

Modify doc-parser/formats/pptx.py.

### Generic PPTX

Do not emit individual pictures. For every slide:

1. Extract all slide text.
2. Extract table cell text.
3. Preserve existing title/heading structure.
4. Preserve speaker notes if currently included.
5. Render the complete slide to one PNG.
6. Append one Markdown image reference for that screenshot.
7. Do not emit individual picture references.
8. Do not inspect individual-picture size.
9. Do not call an image-description model.
10. Do not run slide synthesis or judge iterations.

A generic slide consists of slide text followed by one complete-slide image.
pages contains one fully embedded Markdown string per slide. markdown keeps the
existing slide separators.

Because the screenshot is required, generic PPTX rendering must be required.
Missing LibreOffice or PDFium must produce a clear parse error rather than
silently returning text-only slides. Preserve current auto/required behavior for
llm-wiki.

llm-wiki PPTX keeps individual-picture extraction, tiny-image omission,
position labels, deduplication, descriptions, overview images, multi-draft
descriptions, judge/revision, and image-unit output. Populate pages only after
the final llm-wiki slide content is complete.

## 10. XLSX

Modify doc-parser/formats/xlsx.py.

### Generic XLSX

For each worksheet:

1. Render the entire worksheet as one Markdown unit.
2. Preserve merged cells.
3. Preserve formulas and cached values.
4. Preserve images and worksheet positions.
5. Preserve vector-image conversion.
6. Keep sparse-cell fallback for pathological ranges, but never split a sheet.
7. Do not emit lineage or chart context.
8. Do not emit VBA content.
9. Use ordinary Markdown data URLs.

Do not call the worksheet-parts splitter in generic mode. pages must contain
exactly one item per worksheet. Formula recalculation remains a separate
configurable concern and must not be confused with worksheet splitting.

llm-wiki XLSX keeps manifest-driven selection, worksheet splitting, formula
recalculation, chart context, lineage, VBA references, custom markers, image
units, and LLM descriptions. Populate pages from the final llm-wiki units.

## 11. XLSM

### Generic XLSM

Output order:

1. Every worksheet, including hidden worksheets.
2. One consolidated VBA-code page.
3. One consolidated VBA-final-output page.

Worksheet pages are unsplit and contain worksheet data, formulas, cached values,
and embedded images. They must not contain vba:// links or llm-wiki lineage
markers.

The VBA-code page must:

- Include every module and procedure.
- Use fenced VB code blocks.
- Include the static warning that macros are never executed.
- Preserve declarations and procedure names.
- Never execute macros.

The VBA-final-output page must:

- List each procedure.
- List final worksheet outputs affected by each procedure.
- Include unresolved dynamic references where available.
- Use the existing static lineage analysis.

Generic pages ordering:

    [sheet 1, sheet 2, ..., VBA code page, VBA final-output page]

### Reuse existing XLSM analysis

Existing lineage logic is in graph/workspace/xlsm.py, but doc-parser is deployed
separately. Extract the pure analysis into a parser-side module such as
doc-parser/formats/xlsm_lineage.py.

Then:

1. Move or extract the static analysis.
2. Keep graph/workspace/xlsm.py as a compatibility wrapper/importer.
3. Use the same analysis for generic final-output pages.
4. Use the same analysis for llm-wiki manifest generation.
5. Keep the existing manifest schema stable.

This avoids different parser and graph definitions of VBA final outputs.

llm-wiki XLSM keeps manifest-driven selection, splitting, lineage, VBA
references, vba:// links, per-procedure pages, apply_manifest ordering and
translations, image units, and descriptions.

If apply_manifest remains client-side, apply equivalent transformations to both
markdown and pages so the two response fields cannot disagree.

## 12. CSV

Modify doc-parser/formats/csv.py only enough to return ExtractedDocument with
markdown and pages=[].

Keep encoding detection, delimiter detection, header handling, HTML table
output, row truncation, and title behavior. CSV has no llm-wiki-specific branch.

## 13. Parse options

Add an explicit ParseProfile enum in formats/base.py:

- GENERIC = generic
- LLM_WIKI = llm-wiki

Add profile to ParseOptions, defaulting to GENERIC.

Any direct caller that expects image-unit behavior must explicitly select
LLM_WIKI. Never infer profile from URL, filename, headers, or manifest.

## 14. Graph client

Modify graph/workspace/parser_client.py.

Change the graph request from:

    {base_url}/parse

to:

    {base_url}/parse/llm-wiki

Continue forwarding LLM headers and XLSM manifests. Continue returning markdown
to the graph pipeline. Validate pages when present, but do not require the graph
pipeline to consume pages yet.

If manifest application remains client-side, apply it consistently to pages.

## 15. Frontend

The frontend represents generic parsing.

Modify doc-parser/frontend/src/App.jsx:

- Continue calling /parse.
- Keep images control.
- Remove or disable describe_images control.
- Stop sending describe_images=true.
- Keep the existing Markdown renderer.
- Let ordinary Markdown data URLs render naturally.
- Keep image-unit preprocessing only as harmless compatibility if desired.

Displaying pages.length is optional.

Verify the existing Vite /parse proxy also forwards /parse/llm-wiki before
adding any proxy configuration.

## 16. Documentation and configuration

Update:

- doc-parser/README.md
- doc-parser/SETUP.md
- README.md

Document both routes, URL_PREFIX as deployment-only, generic Markdown data
URLs, markdown/pages response fields, PDF pages, PPTX text-plus-screenshot
output, XLSX whole-sheet output, XLSM VBA pages, and the fact that generic
parsing never calls an LLM.

Generic request example:

    curl -X POST 'http://127.0.0.1:8000/parse' \
      -F 'file=@document.pptx'

llm-wiki request example:

    curl -X POST \
      'http://127.0.0.1:8000/parse/llm-wiki?images=true&describe_images=true' \
      -H 'X-LLM-Base-URL: http://llm.example/v1' \
      -H 'X-LLM-Model: model-name' \
      -F 'file=@document.pptx'

Keep .env as:

    URL_PREFIX=/agent/doc-parser/

Do not add llm-wiki to URL_PREFIX.

## 17. Automated tests

Add tests for both profiles.

### Routes

Verify:

- /parse selects generic.
- /parse/llm-wiki selects llm-wiki.
- Unprefixed routes are rejected when a prefix is configured.
- /health remains available.
- Prefix text never changes profile selection.

### Generic image policy

Verify:

- No image-unit.
- No image-description.
- Images use Markdown data URLs.
- No LLM client is created.
- images=false removes media but preserves alt/location text.
- image_count counts Markdown images.

### llm-wiki regression

Verify image units, descriptions, existing stripping, LLM headers,
description-failure handling, PPTX descriptions/judge loop, and XLSM manifest
behavior remain unchanged.

### DOCX

Verify generic Markdown images and pages=[]; verify llm-wiki image units.

### PDF

Verify page count, page-local content, no generic descriptions, streaming
heartbeats, and unchanged llm-wiki output.

### PPTX

Verify generic page count equals slide count, each page has slide text and one
complete-slide image, individual pictures are absent, icon/arrow noise is
absent, missing rendering is reported, and llm-wiki still emits individual
images/descriptions.

### XLSX

Verify one generic page per worksheet, a very large sheet remains one page, no
split markers appear, merged cells/formulas/cached values remain, and images are
Markdown data URLs.

### XLSM

Verify every worksheet, no worksheet splitting, one consolidated VBA-code page,
one consolidated final-output page, all procedures, correct final outputs, no
macro execution, no generic vba:// links, and unchanged llm-wiki manifest output.

### Response contract

For every parser verify markdown is a string, pages is always a list, metadata
remains valid, and DOCX/CSV return empty page lists.

## 18. Manual verification and rollout

After implementation:

1. Start the parser with URL_PREFIX=/agent/doc-parser.
2. Confirm /health.
3. Confirm /agent/doc-parser/parse.
4. Confirm /agent/doc-parser/parse/llm-wiki.
5. Confirm unprefixed /parse is rejected.
6. Upload representative PDF, DOCX, PPTX, XLSX, and XLSM files.
7. Confirm generic output uses only ordinary Markdown images.
8. Confirm generic PPTX has slide text plus one screenshot per slide.
9. Confirm generic XLSX keeps every sheet whole.
10. Confirm generic XLSM has all sheets, VBA code, and final outputs.
11. Confirm llm-wiki retains image units and descriptions.
12. Confirm graph ingestion calls /parse/llm-wiki.
13. Build the frontend.
14. Run parser tests.
15. Run graph conversion and ingestion tests.

Completion criteria:

- Generic /parse produces plain Markdown.
- pages is correct for every page-capable format.
- Generic PPTX emits only text plus one slide screenshot.
- Generic XLSX does not split worksheets.
- Generic XLSM emits all sheets, VBA code, and final-output information.
- llm-wiki behavior is unchanged behind /parse/llm-wiki.
## 19. Locked decisions for implementation

The following decisions are final for this change. The implementer must not
invent alternate behavior.

### Route behavior

1. /parse is always generic.
2. /parse/llm-wiki is always llm-wiki.
3. URL_PREFIX remains /agent/doc-parser and is never used as a profile switch.
4. Generic requests with a non-empty caller-supplied manifest return HTTP 400:
   "manifest is only supported by /parse/llm-wiki".
5. Generic XLSM lineage is generated internally from the uploaded workbook.
6. llm-wiki accepts its existing caller manifest. If none is supplied for an
   XLSM, the parser generates the same manifest internally as a fallback.
7. Generic describe_images is ignored. It is not an error and never causes an
   LLM request.
8. Generic PPTX requires slide rendering. PPTX_RENDER_SLIDES=false is an error
   for the generic route because one screenshot per slide is mandatory.
9. Generic XLSX never calls the worksheet-parts splitter.
10. Generic XLSM emits all sheets, including hidden sheets, without splitting.

### Result behavior

1. ParseResult.pages is always present and always a list.
2. DOCX and CSV return an empty pages list.
3. image_count counts images in the returned full markdown, not the sum of
   images across pages.
4. markdown is the compatibility output. Existing full-document joiners remain.
5. pages is generated by the parser, never by clients or graph code.
6. Generic pages contain the same final Markdown image form as markdown.
7. llm-wiki pages contain the same final image-unit form as markdown.

### XLSM behavior

Generic XLSM has exactly two synthetic units after sheet units:

1. One consolidated VBA-code Markdown page containing every module/procedure.
2. One consolidated VBA-final-output Markdown page containing the static
   final-output mapping for every procedure.

Do not create one generic page per VBA procedure. Per-procedure pages remain an
llm-wiki-only behavior.

## 20. Exact data types and function contracts

Implement these contracts before changing individual formats.

### ParseProfile

In formats/base.py:

    class ParseProfile(StrEnum):
        GENERIC = "generic"
        LLM_WIKI = "llm-wiki"

### ParseOptions

Add:

    profile: ParseProfile = ParseProfile.GENERIC

Keep all existing fields unchanged.

### ExtractedDocument

Use:

    @dataclass(slots=True)
    class ExtractedDocument:
        markdown: str
        pages: list[str] = field(default_factory=list)
        markdown_path: Path | None = None
        asset_root: Path | None = None

Every _extract method returns this type.

### BaseParser method contracts

Change the abstract method to:

    async def _extract(
        self,
        data: bytes,
        image_dir: str,
        options: ParseOptions,
        workers: Workers,
    ) -> ExtractedDocument

Keep parse(data, options, workers) returning ParseResult.

### Markdown image helpers

Add these helpers in utils/markdown_images.py or utils/image_unit.py:

- embed_markdown_images(..., style="image-unit" | "markdown")
- strip_markdown_image_media(markdown: str) -> str
- count_markdown_images(markdown: str) -> int

The default style must remain image-unit so old direct callers do not change
accidentally. BaseParser selects style explicitly from ParseOptions.profile.

## 21. Exact full-markdown and pages assembly rules

Each parser must build pages before profile-specific image embedding. The same
raw image references must exist in both the full output and their corresponding
page entries.

### PDF

The full markdown remains the exact MinerU document. pages is derived from the
same MinerU page boundaries. Do not rebuild full markdown by joining pages,
because MinerU may contain document-level preamble or trailing material.

If a preamble is not associated with a page, keep it only in markdown. A page
may contain the page heading and all blocks assigned to that page.

### PPTX

run_pptx builds a list of slide strings while building the full document. It
returns those exact slide strings as pages and joins them using existing slide
separators for markdown.

Generic slide strings contain text and one screenshot reference. llm-wiki slide
strings are captured only after individual images, overview images, descriptions,
deduplication, and judge revisions are complete.

### XLSX

run_openpyxl builds one complete string per worksheet. Generic mode places the
complete string in pages without splitting. llm-wiki mode records the existing
worksheet parts and VBA units it already emits.

The full markdown is assembled from the same strings in the same order. Do not
derive pages later by regex over markdown.

### XLSM

Generic pages are:

1. One complete worksheet string for every worksheet in workbook order.
2. One consolidated VBA-code string.
3. One consolidated VBA-final-output string.

The full markdown is assembled from those exact strings with existing section
joiners.

## 22. Exact PDF page-boundary implementation

Implement formats/pdf.py::split_mineru_pages(markdown_path, output_dir).

Use this deterministic order:

1. Search output_dir recursively for the content-list JSON associated with the
   current input stem. Prefer a file ending in content_list.json.
2. Parse it as a list of blocks. Each block must provide page_idx or an
   equivalent page number.
3. Preserve block order and group blocks by integer page index.
4. Render each block using the Markdown/text/image fields already emitted by
   MinerU. Do not call an LLM.
5. If content-list JSON is absent, search Markdown for explicit page markers
   emitted by the installed MinerU backend.
6. If neither source exists, raise MineruError with output directory and the
   missing artifact names.

Do not use headings, blank-line counts, or PDF page count to guess where
Markdown belongs. A page count alone cannot map MinerU blocks reliably.

Before coding the helper, run MinerU once on a two-page fixture and record the
actual artifact filename and block keys in fixture documentation. The helper
must support the artifact emitted by the deployed backend.

Add tests with a mocked content-list artifact, explicit Markdown page markers,
and the error case where both are absent.

## 23. Exact generic PPTX implementation

Change run_pptx in formats/pptx.py to accept an explicit profile argument:

    run_pptx(
        pptx_path: str,
        output_dir: str,
        slide_images: list[str] | None,
        profile: ParseProfile,
    ) -> RenderedPresentation

RenderedPresentation contains markdown, pages, markdown_path, and asset_root.

For generic mode, skip the MSO_SHAPE_TYPE.PICTURE branch completely. Do not
write individual picture blobs. Continue processing text frames, tables, title
shapes, and notes. Append only the rendered slide image reference.

For llm-wiki mode, retain the existing picture branch and all current logic.

In the PPTX parser:

1. Force render_mode to required for generic.
2. Call render_slides_with_libreoffice.
3. Raise SlideRenderError if any slide image is missing.
4. Call run_pptx with the selected profile.
5. Embed images in markdown and pages using the selected style.

Generic acceptance assertion: complete-slide image references in pages equal the
number of slides, and no individual media/image references exist.

## 24. Exact generic XLSX and XLSM implementation

Change run_openpyxl to accept profile and return structured rendered output
instead of only a Markdown path. Keep it worker-callable and ensure its return
dataclass is picklable.

For generic XLSX:

1. Load formulas and cached values as today.
2. Do not call _selection.
3. Do not call _render_worksheet_parts.
4. Call _render_worksheet once per worksheet.
5. Attach that worksheet's image references to the same worksheet string.
6. Store one worksheet string in pages.
7. Join worksheet strings for markdown.

For generic XLSM:

1. Perform the same full-sheet rendering for every worksheet.
2. Run static VBA analysis on the original upload, never a recalculated copy.
3. Build one consolidated code page from all modules and procedures.
4. Build one consolidated final-output page from final_outputs_affected.
5. Append both pages after all worksheet pages.
6. Never call _render_vba_references for generic worksheet pages.
7. Never emit vba:// links in generic mode.

For llm-wiki:

1. Keep _selection and existing manifest logic.
2. Keep _render_worksheet_parts.
3. Keep chart, lineage, VBA reference, and procedure-page output.
4. Record final units into pages before returning.

## 25. Exact XLSM lineage ownership

Move the complete static lineage builder, not only a wrapper, into the parser
implementation so standalone generic uploads can produce final-output data.

The parser-side builder exposes:

    build_manifest(path: Path) -> dict[str, Any] | None

It preserves the existing schema_version, mode, sheets, procedures,
final_outputs_affected, buttons, formula_cells, and unresolved-reference fields.

Update graph/workspace/xlsm.py:

1. Keep has_vba and apply_manifest public for existing graph callers.
2. Remove the duplicate build_manifest implementation where possible.
3. If direct imports are impossible in the separate deployment, retain a small
   compatibility implementation covered by a schema-equivalence test.
4. Compare normalized JSON results for the same XLSM fixture.

Do not silently change lineage semantics.

## 26. Exact server parameter behavior

Implement shared request-parameter validation for both routes.

Generic route:

- images is honored.
- describe_images is accepted and ignored.
- LLM headers are accepted but ignored.
- a non-empty manifest returns HTTP 400.
- profile is forced to GENERIC.

llm-wiki route:

- images is honored.
- describe_images is honored.
- LLM headers are forwarded.
- manifest is parsed and forwarded.
- profile is forced to LLM_WIKI.

Do not allow a client to override profile with a query parameter or header.

## 27. Exact graph-client migration

Change graph/workspace/parser_client.py:

1. Add a constant for the /parse/llm-wiki endpoint suffix.
2. Post all existing files to that endpoint.
3. Keep multipart field names unchanged.
4. Keep manifest generation unchanged initially.
5. Parse markdown and pages.
6. Apply existing manifest transformation to both fields.
7. Return markdown exactly as before.
8. Add a request-path regression test asserting the exact URL.

The generic frontend/client continues using /parse.

## 28. Exact file-by-file implementation order

1. formats/base.py: profile enum, ExtractedDocument, ParseResult.pages, policy
   dispatch.
2. Image helpers: generic embedding, stripping, counting.
3. CSV and DOCX: structured return and base-flow tests.
4. server.py: profile routes and response pages.
5. PPTX: screenshot-only generic mode and slide pages.
6. XLSX: unsplit generic sheets and structured output.
7. XLSM: consolidated VBA and final-output pages.
8. PDF: MinerU page extraction.
9. graph client: llm-wiki endpoint and pages synchronization.
10. Frontend: generic route and controls.
11. Documentation/configuration.
12. Full regression and manual verification.

After each format, run focused tests before changing the next format.

## 29. Required fixtures and assertions

Use fixtures for:

- Two-page PDF with one image on each page.
- DOCX with raster and vector images where available.
- Two-slide PPTX with text, tables, images, arrows, and icons.
- XLSX with two sheets, merged cells, formulas, cached values, and an image.
- XLSX with a worksheet large enough to trigger old splitting.
- XLSM with multiple sheets, an intermediate dependency, a final sheet, two
  VBA procedures, and one procedure affecting the final output.
- CSV with header and multiple rows.

For every fixture assert both markdown and pages:

- Full markdown has expected joiners.
- pages is source-ordered.
- Generic media is Markdown data URL syntax.
- llm-wiki media is image-unit syntax.
- image_count is based on full markdown and is not multiplied by pages.

## 30. Final stop conditions

Do not declare completion if:

- PDF pages are derived from guessed headings.
- Generic PPTX emits individual images.
- Generic XLSX invokes worksheet splitting.
- Generic XLSM omits a worksheet, VBA procedure, or final-output mapping.
- Generic route creates an LLM client.
- pages is reconstructed by downstream regex.
- Graph still calls /parse.
- llm-wiki loses descriptions, lineage, or existing markers.
- markdown and pages use different image policies.
- The generic manifest rule is ambiguous.
