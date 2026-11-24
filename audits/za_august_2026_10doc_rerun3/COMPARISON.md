# ZA August 2026 — third rerun after DOC column alignment

Date: 2026-10-28. This folder does not replace `audits/za_august_2026_10doc/AUDIT.md`, `audits/za_august_2026_10doc_rerun/COMPARISON.md`, or `audits/za_august_2026_10doc_rerun2/COMPARISON.md`.

The same ten files were extracted again with `PHASE2_RECOVERY_ENABLED=true` set in the process before import. `.env` was not edited. This pass is a live extraction. The guarded unit suite was a separate run with Vertex and Paddle startup blocked; it is not this table.

## Decision and grounding

| File | Rerun 2 | This rerun | Reason | Grounding | Rows | Fail | OCR calls | Seconds |
|---|---|---|---|---|---|---|---|---|
| PDF `0000734513` | UNRESOLVED | UNRESOLVED | `stock_identity_failure` | checked, 0 discrepancies | 49 | 9 | 0 | 0.7778 |
| XLSX `0000711172` | VERIFIED | VERIFIED | `schema_evidence_business_ok` | checked, 0 discrepancies | 16 | 0 | 0 | 0.9115 |
| XLS `0000700848` | VERIFIED | VERIFIED | `schema_evidence_business_ok` | checked, 0 discrepancies | 20 | 0 | 0 | 0.1974 |
| TXT `0000732705` | VERIFIED | VERIFIED | `schema_evidence_business_ok` | checked, 0 discrepancies | 20 | 0 | 0 | 0.0129 |
| HTML `0000734246` | UNRESOLVED | UNRESOLVED | `stock_identity_failure` | checked, 0 discrepancies | 44 | 3 | 0 | 0.2168 |
| DOCX `0000713511` | VERIFIED | VERIFIED | `schema_evidence_business_ok` | checked, 0 discrepancies | 55 | 0 | 0 | 0.2884 |
| DOC `0000736672` | UNRESOLVED, 6 discrepancies | VERIFIED | `schema_evidence_business_ok` | checked, 0 discrepancies | 58 | 0 | 0 | 0.0711 |
| JPG `0000701478` | UNRESOLVED | UNRESOLVED | `insufficient_source_evidence` | insufficient, blocks verification | 2 | 0 | 1 | 48.8394 |
| JPEG `0000733955` | UNRESOLVED | UNRESOLVED | `insufficient_source_evidence` | insufficient, blocks verification | 28 | 16 | 1 | 11.4046 |
| PNG `0000727190` | UNRESOLVED | UNRESOLVED | `insufficient_source_evidence` | insufficient, blocks verification | 23 | 2 | 1 | 11.7512 |

PDF still has the nine printed closing-0 rows. HTML still has the three blank closings. Neither was coerced.

## DOC

The printed CYSTONE FORTE TAB.<STRIP> line is Pack `30`S`, O.Stk 12, Purc 15, Tot 27, Sale 12, Qoh 15, Value 1717.16, Age 27. The extractor already stored opening 12, receipts 15, sales 12, closing 15, total 27, and closing value 1717.16. Grounding had been aligning by whitespace index, so Pack glued to the name shifted Purc into opening and Value into Qoh. It also treated `Opening Value` and `Purchase Value` as missing products. Grounding now reads the same seven trailing cells: O.Stk, Purc, Tot, Sale, Qoh, Value, Age. Those two footer lines do not have seven trailing numbers, so they are not products. This live pass has 0 discrepancies, fail count 0, and 0 recovery attempts. That is source re-read agreement for the rows the reader compared, not a hand score of all 58 lines.

## Images and Paddle

The production worker still fails with WinError 127 on `cudnn_cnn64_9.dll` and makes one orchestrator call per image, then Gemini. This rerun logged five `gemini-2.5-flash-lite` `generateContent` HTTP 200 responses, all on the three images. Digital files, including the DOC, made no Gemini call.

A separate subprocess trial, not this rerun, loaded Torch before Paddle and skipped Paddle cuDNN DLLs that were already loaded. That got past WinError 127. `PaddleOCRVL` then loaded PP-DocLayoutV3 and PaddleOCR-VL-1.6. `nvidia-smi` sees an RTX 3050, but Torch CUDA is unavailable and Paddle reports `cudaErrorNoDevice` against driver 551.76. Forcing the capability probe to return none let the model finish loading on CPU. `predict` on the PNG was still running after more than 15 minutes of CPU time and had not returned text, so that shim was not added to the worker.

PNG HIORA SG GEL is still opening 5, receipts 50, sales 1, closing 55, `total_stock` null. The screenshot CloseStock is 54. The text parser maps CloseStock when the header is present. This live row came from Gemini vision after empty OCR text. 54 was not written in.

JPG still has no column headers. SPEMAN Tab is stored as quantities 0 / 0 / 0 / 0 with sales value and closing value 15. The printed 15.00 cells were not copied into quantity fields.

JPEG names in the saved rows use CYSTONE. Quantities were not accepted. The document stays unresolved because OCR text is empty and 16 rows fail the stock equation.
