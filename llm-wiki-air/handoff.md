# Update pipeline implementation status

Date: 2026-09-24

Implemented the tiered wiki update flow, incremental linker scope, forced rebuild handling, human-edit reporting, PPTX image-description de-duplication, and `.doc` / `.xls` parser support. The optional Phase 9 slide-delimiter change remains skipped.

Full air discovery passed (85 tests, 27 opt-in live skips), and all 89 parser tests passed. Local parser checks reflected mutations to the Japanese DOCX and PPTX. PDF content includes its marker, but the parser's MinerU request timed out. The live wiki/GROWI acceptance run was stopped during seed planning after more than 23 minutes; the surgical handoff edit and live tests 19–22 remain unverified. See Section 12 of `plan_diff_2.md`.

Test sources: [Digital Agency PDL 1.0 DOCX](https://www.digital.go.jp/assets/contents/node/basic_page/field_ref_resources/f7fde41d-ffca-4b2a-9b25-94b8a701a037/41722b25/20240705_resources_data_outline_06.docx), [NIES/A-PLAT presentation](https://adaptation-platform.nies.go.jp/tools/files/presentation_guidebook_June2020.pptx), and [Japanese Constitution PDF](https://www.japaneselawtranslation.go.jp/ja/laws/download/174/04/s21Ak000010101ja3.0.pdf). Only the DOCX fixture is retained in the repository; its test edits and the PPTX/PDF copies stayed under `/tmp`.
