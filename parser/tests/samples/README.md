# Sample documents for end-to-end parsing

Real-world (and one synthetic) documents for exercising the full
`detect -> parser.parse -> Workers` pipeline against every supported format.

| File | Format | Source | What it exercises |
| --- | --- | --- | --- |
| `pdf_transformer_paper.pdf` | PDF | arXiv 1706.03762 ("Attention Is All You Need"), 15 pp | Multi-column text, 4 tables, 5 figures, inline math — MinerU API pipeline backend |
| `pdf_medical_report_scanned.pdf` | PDF | microsoft/markitdown test files, 3 pp | Image-only scan — MinerU API OCR path |
| `pdf_repair_invoice_multipage.pdf` | PDF | microsoft/markitdown test files, 3 pp | Multi-page invoice, line-item tables, 2 logo images |
| `docx_handbook_872p.docx` | DOCX | Unstructured-IO/unstructured example-docs (US DoJ Ch.13 Handbook), 872 pp | Huge document (5 MB `document.xml`), deep heading tree, HTML tables, TOC, 2 images |
| `docx_contains_pictures.docx` | DOCX | Unstructured-IO/unstructured example-docs | Inline + floating `<img>` images with sizing attributes |
| `docx_grid_offset_tables.docx` | DOCX | Unstructured-IO/unstructured example-docs | Offset / irregular table grids |
| `xlsx_financial_analyses.xlsx` | XLSX | Unstructured-IO/unstructured example-docs | 5 sheets, ~40 cross-sheet formulas, real Excel-saved cached values |
| `xlsx_kitchen_sink.xlsx` | XLSX | **synthetic** (`make_kitchen_sink_xlsx.py`) | Cross-sheet formulas, dense table, sparse-cell view, 2 embedded PNGs, empty sheet, pipe/newline/unicode escaping |
| `xlsx_subtable_cases.xlsx` | XLSX | Unstructured-IO/unstructured example-docs | Multiple stacked sub-tables in one sheet |
| `xlsx_more_than_1k_cells.xlsx` | XLSX | Unstructured-IO/unstructured example-docs | Large used range + an embedded chart (not a raster image) |
| `pptx_sample_presentation.pptx` | PPTX | Unstructured-IO/unstructured example-docs | 4 slides: titles, nested bullets, a table; also proves it is not mis-claimed by the DOCX/XLSX zip detectors |
| `csv_long_lines.csv` | CSV | Unstructured-IO/unstructured example-docs | 350-column Spearman correlation matrix — wide-table rendering, content-sniff detection |

## Running everything

```bash
LD_LIBRARY_PATH=~/.local/lib/lo-shim \
  .venv/bin/python /path/to/scratchpad/run_samples.py --dummy-describe
```

`--dummy-describe` substitutes a no-network describer; drop it (or use
`--describe`) once a vision endpoint is reachable. Regenerate the synthetic
workbook with `.venv/bin/python tests/samples/make_kitchen_sink_xlsx.py`.

## Issue surfaced by these samples — FIXED

`docx_contains_pictures.docx` and `docx_handbook_872p.docx` originally came out
with `image_count == 0`: Pandoc's `gfm` writer emits DOCX images that carry
size/position attributes as raw `<img>` HTML tags, and the embed step only
matched Markdown `![alt](path)` syntax, so those images were never
base64-embedded or described. `gfm-raw_html` was rejected as the fix because it
also drops complex HTML tables (the handbook has two). Instead
`utils/markdown_images.py` now recognises both `![](…)` and `<img src=…>`
(`_IMAGE_REF_RE`); regression coverage is in `tests/test_markdown_images.py`.

`docx_grid_offset_tables.docx` legitimately renders to ~32 characters — its
828 KB is almost entirely Word style definitions; `word/document.xml` is 2.3 KB
containing one small malformed table.
