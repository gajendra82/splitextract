# ZA August 2026 — second rerun after dash, identity, and OCR diagnosis

Date: 2026-10-28. This folder is a new rerun. It does not replace `audits/za_august_2026_10doc/AUDIT.md` or `audits/za_august_2026_10doc_rerun/COMPARISON.md`.

The same ten files under `C:\Users\amnsa\Downloads\ZA_2026_August` were extracted with `PHASE2_RECOVERY_ENABLED=true` set in the process before import. `.env` was not edited. Images, DOC, XLS, XLSX, PDF, and HTML in this folder are from that live pass. TXT and DOCX decisions were refreshed after the stock-identity stamp was aligned with the layout profiles. Their row values did not change; only the decision and Phase 2 status did.

No accuracy percentage is claimed. Grounding with zero discrepancies means the extractor and a source re-read agreed on the compared cells. It is not a hand score of every printed cell. A balanced stock equation is not, by itself, proof of a correct read.

## Decision and grounding

| File | Original audit | Previous rerun | This rerun | Reason | Grounding | Rows | OCR calls |
|---|---|---|---|---|---|---|---|
| PDF `0000734513` | UNRESOLVED | UNRESOLVED `stock_identity_failure` | UNRESOLVED | `stock_identity_failure` | checked, `pdf_header_positions`, 49 compared, 0 discrepancies | 49 | 0 |
| XLSX `0000711172` | UNRESOLVED | UNRESOLVED, 12 discrepancies, 11 `blank_read_as_zero` | VERIFIED | `schema_evidence_business_ok` | checked, 0 discrepancies, 0 `blank_read_as_zero` | 16 | 0 |
| XLS `0000700848` | UNRESOLVED | VERIFIED | VERIFIED | `schema_evidence_business_ok` | checked, 0 discrepancies | 20 | 0 |
| TXT `0000732705` | UNRESOLVED | UNRESOLVED `stock_identity_failure` | VERIFIED | `schema_evidence_business_ok` | checked, `delimited_text`, 20 compared, 0 discrepancies | 20 | 0 |
| HTML `0000734246` | UNRESOLVED | UNRESOLVED, 1 discrepancy | UNRESOLVED | `stock_identity_failure` | checked, `html_table`, 44 compared, 0 discrepancies | 44 | 0 |
| DOCX `0000713511` | UNRESOLVED | UNRESOLVED `stock_identity_failure` | VERIFIED | `schema_evidence_business_ok` | checked, `docx_paragraphs`, 55 compared, 0 discrepancies | 55 | 0 |
| DOC `0000736672` | VERIFIED (false) | UNRESOLVED, 6 discrepancies | UNRESOLVED | `validation_not_accepted` | checked, `legacy_doc_text`, 58 compared, 6 discrepancies | 58 | 0 |
| JPG `0000701478` | UNRESOLVED | UNRESOLVED | UNRESOLVED | `insufficient_source_evidence` | insufficient, blocks verification | 2 | 1 |
| JPEG `0000733955` | UNRESOLVED | UNRESOLVED | UNRESOLVED | `insufficient_source_evidence` | insufficient, blocks verification | 28 | 1 |
| PNG `0000727190` | UNRESOLVED | UNRESOLVED | UNRESOLVED | `insufficient_source_evidence` | insufficient, blocks verification | 23 | 1 |

PDF elapsed 0.7533s. XLSX 0.7933s. XLS 0.1697s. HTML 0.1875s. DOC 22.5862s with 2 recovery attempts. JPG 30.2289s. JPEG 23.3827s. PNG 12.0809s. TXT and DOCX Phase 2 status after the stamp refresh is `not_needed`, 0 attempts. PDF and HTML Phase 2 attempts are 0 because source grounding already matched.

## What changed relative to the previous rerun

### XLSX

Printed dashes are stored as null with `qty_cell_state` `dash`. Explicit numeric zero stays 0. CYSTONE TABLETS opening 700, receipts null, sales null, closing 700. EVECARE SYRUP 200 ML opening 196, sales 196, closing null (`dash`). The identity check treats a dash as no movement and a blank cell as incomplete. Grounding no longer reports `blank_read_as_zero`. The `Division : 00` banner is not a product row.

### HTML

44 product rows is the parser skipping the `Totals:` banner, not a dropped product. Sale, Jun Sa, Jul Sa, and WSale stay separate. BONNISAN LIQ 100ML sales 52 and june sale 20. The previous missing-row discrepancy on `Totals:` is gone. Three rows still fail identity because closing is blank in the HTML while opening is printed: EVECARE FORTE LIQ 200ML opening 18, LIV.52 DS SUGAR FREE SYP 100ML opening 36, LUKOL SYP 200ML opening 16. Those closings were not coerced to 0.

### TXT

The trailing six numbers are opening, purchase, sale, credit note, debit note, closing. LIV 52 DROPS 100 ML is 52 / 0 / 1 / 51; 52 and 100 stay in the name. EVECARE SYRUP credit note 1, closing 2. LUKOL TAB debit note 3, closing 44. The validation stamp now uses `closing = opening + purchase - sale + credit note - debit note`. Fail count 0. Decision VERIFIED.

### DOCX

Sale and closing are the printed columns. Opening and receipts stay null, and the four-column equation is not applied. ARJUNA TAB sales 15, closing 1. Fail count 0. Decision VERIFIED.

### PDF

Native text uses both scheme columns and Cl qty rather than Cl val. EVECARE FORTE LIQUID is opening 18, purchase 28, scheme in 0, sales return 0, sale 14, scheme out 1, add 0, less 0, closing 31. That row passes. Nine rows have a printed closing of 0, a nonzero opening, and zero movements: FLORASANTE CAP, HARIDRA TAB, 52 100ML LIV DS SUGER FREE, LUKOL SYP, MENTAT GUMMIES, 100ML PLATENZA SYR, SEPTILIN GUMMIES, SHUDDHA GUGGULU TAB, STYPLON TAB. Those stay unresolved. Grounding discrepancy count is 0. OCR calls 0.

### DOC

Still unresolved. The six grounding discrepancies are CYSTONE FORTE TAB.<STRIP> opening 12 vs source 15, receipts 15 vs 27, sales 12 vs 15, closing 15 vs 1717.16, plus missing rows `Opening Value` and `Purchase Value :`. Two recovery attempts returned null quantities and were not applied.

### Images

PaddleOCR-VL did not read these files. Each image made 1 orchestrator call, then Gemini OCR and Gemini vision. Phase 2 was skipped as unverified OCR.

PNG HIORA SG GEL on the screenshot is Opening 5, Purchase 50, Total 55, Sale 1, SaleRet 0, Exp/Dmg 0, CloseStock 54. The saved row is opening 5, receipts 50, sales 1, closing 55, `total_stock` null. The text parser maps CloseStock when the header is present. This live row came from Gemini vision after empty OCR text, so that parser did not run. 54 was not written in.

JPG has no column headers. SPEMAN Tab shows 15.00, 0.00, 0.00, 15.00 and GroupTotal 2,150.06. The saved quantities are 0 / 0 / 0 / 0 with sales value and closing value 15. Those 15.00 cells were not copied into quantity fields.

JPEG is a photograph with headers OPENING, RECEIPT, ISSUE, CLOSING. Saved names use CYSTONE, not the earlier CISTON / CYSTINE spellings. Quantities were not hand-accepted. Several saved rows do not satisfy opening minus issue equals closing, and grounding is insufficient.

## OCR

PaddleOCR-VL remains the primary engine. It still cannot be constructed in this environment. `paddlepaddle-gpu` 3.2.1 and `torch` 2.8.0+cu126 each ship `cudnn_cnn64_9.dll`. Loading the second one raises WinError 127. PaddleOCR-VL construction imports paddlex, which imports torch, so the two loads collide. A CPU device flag does not stop the GPU wheel from loading its cuDNN DLL. The earlier `paddle.tensor` error is the poisoned process after that failed load. No wheel was installed and the engine was not switched.
