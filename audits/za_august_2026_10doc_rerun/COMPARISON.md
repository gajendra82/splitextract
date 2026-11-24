# ZA August 2026 — rerun compared with the original 10-document audit

Date: 2026-10-28. The original report `audits/za_august_2026_10doc/AUDIT.md` and its per-file JSON were not overwritten. This folder is the rerun.

The same ten files under `C:\Users\amnsa\Downloads\ZA_2026_August` were extracted again. `PHASE2_RECOVERY_ENABLED=true` was set in the process environment before import, matching `.env` line 110. `.env` was not edited. TXT, JPG, JPEG, and PNG were extracted a second time after the split-line Pro. Age detector and the crop short-circuit; the numbers below for those four files are from that second pass. The other six files are from the first pass.

No accuracy percentage is claimed. A checked grounding report with zero discrepancies means the extractor and the source re-read agreed on the compared cells. It is not a manual score of every printed cell.

## Decision and grounding

| File | Original decision | Rerun decision | Rerun reason | Grounding | Rows | Seconds | OCR calls |
|---|---|---|---|---|---|---|---|
| PDF `0000734513` | UNRESOLVED | UNRESOLVED | `stock_identity_failure` | checked, `pdf_header_positions`, 45 compared, 0 discrepancies | 49 | 61.3348 | 4 |
| XLSX `0000711172` | UNRESOLVED | UNRESOLVED | `validation_not_accepted` | checked, `spreadsheet_cells`, 16 compared, 12 discrepancies, 11 `blank_read_as_zero` | 16 | 6.2998 | 0 |
| XLS `0000700848` | UNRESOLVED | VERIFIED | `schema_evidence_business_ok` | checked, `spreadsheet_cells`, 20 compared, 0 discrepancies | 20 | 0.2635 | 0 |
| TXT `0000732705` | UNRESOLVED | UNRESOLVED | `stock_identity_failure` | checked, `delimited_text`, 16 compared, 0 discrepancies | 20 | 0.1303 | 0 |
| HTML `0000734246` | UNRESOLVED | UNRESOLVED | `stock_identity_failure` | checked, `html_table`, 44 compared, 1 discrepancy | 44 | 14.5521 | 0 |
| DOCX `0000713511` | UNRESOLVED | UNRESOLVED | `stock_identity_failure` | checked, `docx_paragraphs`, 55 compared, 0 discrepancies | 55 | 0.2796 | 0 |
| DOC `0000736672` | VERIFIED | UNRESOLVED | `validation_not_accepted` | checked, `legacy_doc_text`, 58 compared, 6 discrepancies | 58 | 17.5659 | 0 |
| JPG `0000701478` | UNRESOLVED | UNRESOLVED | `insufficient_source_evidence` | insufficient, `ocr_text`, 0 compared | 2 | 38.2484 | 1 |
| JPEG `0000733955` | UNRESOLVED | UNRESOLVED | `insufficient_source_evidence` | insufficient, `ocr_text`, 0 compared | 28 | 11.7732 | 1 |
| PNG `0000727190` | UNRESOLVED | UNRESOLVED | `insufficient_source_evidence` | insufficient, `ocr_text`, 0 compared | 23 | 12.6087 | 1 |

The original DOC result was `verified: true` while grounding was `insufficient_source_evidence`. The rerun is `verified: false`.

## Rows that the original audit checked

### XLS

The three rows that were wrong in the original audit now match the source cells that audit recorded:

| Product | Source op / rec / iss / clos | Rerun opening / receipts / sales / closing |
|---|---|---|
| BONNISAN DROP 30ML | 6 / 0 / 1 / 5 | 6 / 0 / 1 / 5 |
| BONNISAN SYP 100ml | 5 / 16 / 11 / 10 | 5 / 16 / 11 / 10 |
| BRESOL SYP 200ML | 2 / 0 / 1 / 1 | 2 / 0 / 1 / 1 |

Grounding compared all 20 rows and reported 0 discrepancies. The method field in the slim JSON is null. This is source re-read agreement, not a hand score of every cell.

### HTML

| Product | Original defect | Rerun |
|---|---|---|
| BONNISAN LIQ 100ML Sale | extracted 20 (Jun Sa) | `sales_qty` 52, `june_sale_qty` 20 |
| BONNISAN LIQ 100ML Purc | blank stored as 0 | `receipts_qty` null |
| BONNISAN LIQ 200ML Sale | blank stored as 0 | `sales_qty` null |
| BONNISAN DROPS 30ML | 55 / 10 / 45 | 55 / 10 / 45, receipts null |

Row count is 44. The original audit counted 45 HTML data rows. One grounding discrepancy remains, and stock identity is inconsistent, so the document stays unresolved.

### TXT

BLEMINOR CREAM packing is null. `product_age` is 51. Quantities are 8 / 0 / 0 / 8. BONNISAN DROPS is 118 / 0 / 80 / 38 with age null. Grounding compared 16 of 20 rows with 0 discrepancies. Identity is still inconsistent, so the decision stays unresolved. The recovery log for this pass is `PHASE2_RECOVERY ... attempt=0 result=SKIPPED reason=source_already_matches`. The wrapper recorded no Gemini call and elapsed 0.1303s.

### DOCX

ARJUNA TAB opening and receipts are null. Sales 15, closing 1. BONNISAN 200ML is 17 / 11 and BONNISAN DROP is 9 / 87, with opening and receipts null. Grounding compared 55 rows with 0 discrepancies. Identity fails because the sheet prints sale and closing only, so the document stays unresolved. The wrapper recorded no Gemini call.

### DOC

ARJUNA CAP. is still 44 / 0 / 9 / 35, with `closing_value` 8189.23 and `sales_value` 0. BONNISAN DROPS is still 0 / 40 / 2 / 38. Grounding is now checked and reports 6 discrepancies. Those six were not itemized in the slim JSON. Two recovery calls ran (8.8854s and 8.3927s) and the decision stayed unresolved. The false `VERIFIED` result is gone.

### PDF

The first three BONNISAN rows are still 50 / 50 / 50 / 50, 138 / 0 / 4 / 134, and 27 / 0 / 0 / 27. Grounding is checked through `pdf_header_positions` (45 compared, 0 discrepancies). Business validation is inconsistent, so the decision stays unresolved. The recovery log is `attempt=0 result=SKIPPED reason=source_already_matches`. The wrapper's `gemini_calls` list is empty. The extraction log recorded one Vertex `gemini-2.5-flash-lite` `generateContent` HTTP 200, label `sales_vertex`, text length 15595, duration 12453 ms, then `KEEP_EXTRACTED_ROWS`. Four OCR recognize calls still ran.

### XLSX

CYSTONE TABLETS opening 700 and closing 700. Purchase and sale dashes are still stored as 0. EVECARE SYRUP opening 196, sales 196, closing dash still 0. Grounding reports 12 discrepancies and 11 `blank_read_as_zero`. Two recovery calls ran (2.8776s and 2.6462s). The decision stayed unresolved.

### JPG

SPEMAN Tab packing `60s`. Quantities are still 0 / 0 / 0 / 0. `sales_value` and `closing_value` are 15. That is not the printed quantity row 15 / 0 / 0 / 15. Grounding is insufficient. Decision unresolved.

### PNG

HIORA SG GEL is still opening 5, receipts 50, sales 0, closing 55. CloseStock 54 was not stored. The text parser maps CloseStock in unit tests. This live result came from Gemini vision after Paddle returned no text, so that parser did not see the header. Decision unresolved.

### JPEG

Decision unresolved, grounding insufficient, 28 rows. Names in the JSON include `CYSTONE SYRUP` and `CYSTONE FORTE TAB.`. They were not re-checked against the photograph in this pass. No field-accuracy figure.

## OCR and recovery calls that actually ran

PaddleOCR-VL stayed the primary engine. Tesseract was configured at startup and was not the reader of these files. Every live OCR attempt logged `WinError 127` loading `cudnn_cnn64_9.dll`. The CPU retry used the same error and produced no OCR text. The `paddle.tensor` error from the original audit did not reappear after the worker was killed. An isolated import with `CUDA_VISIBLE_DEVICES=-1` printed paddle 3.2.1 on CPU. That import was not a document read.

Image orchestrator calls dropped from 66 to 1 per image on the second pass. Each of those three images then made one OCR Gemini HTTP 200 and one vision `generateContent` HTTP 200. The wrapper recorded only the OCR fallback (`kind: ocr`): JPG 2.5272s, JPEG 3.4397s, PNG 3.9137s. Log durations for the vision calls on that same pass were 1812 ms, 6921 ms, and 6672 ms. Phase 2 skipped all three with `UNVERIFIED_OCR_FALLBACK`.

Recovery Gemini calls recorded by the wrapper: XLSX two, HTML two (7.0991s and 7.0575s), DOC two. PDF and TXT were skipped because the source re-read already matched. Dollar cost is not reported. Some vision events show `output_tokens` 0, which is not a cost.

## Still unresolved

- PaddleOCR-VL does not produce usable text. The cuDNN load fails on the GPU attempt and on the one CPU retry.
- The PDF still makes four OCR recognize calls and one `sales_vertex` Gemini call after native text has already grounded the file.
- XLSX dashes remain numeric 0. Existing item-pack tests require that parser to treat a dash as 0. Grounding flags `blank_read_as_zero` and the document stays unresolved.
- Live PNG still stores Total 55 as closing and sale as 0.
- JPG SPEMAN quantities are still 0.
- JPEG names were not re-validated against the photo.
- HTML has one remaining discrepancy, 44 rows against the original count of 45, and a stock-identity failure.
- DOC has six unitemized source discrepancies after two recovery attempts.
- TXT, PDF, and DOCX can match the source re-read and stay unresolved because the stock-identity equation does not balance. That block is intentional.
