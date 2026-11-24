# ZA August 2026 — 10-document extraction audit

Date: 2026-10-28. Pipeline entry: `extract_sales_statement` in the project virtual environment. Source files were not modified. No parser, schema, API, or database change was made. This file is new; existing accuracy reports were not overwritten.

The folder `C:\Users\amnsa\Downloads\ZA_2026_August` contains 1,104 PDF, 832 JPG, 285 XLSX, 210 JPEG, 169 XLS, 61 PNG, 20 TXT, 7 HTML, 5 CSV, 2 DOCX, 2 HTM, 1 DOC, plus HEIC, JFIF, and one unrecognized extension. Ten files were selected, one real statement of each major type the pipeline accepts.

Recovery did not run. The process environment had `PHASE2_RECOVERY_ENABLED=false`, and `load_dotenv()` does not override an existing variable. The project `.env` contains `PHASE2_RECOVERY_ENABLED=true`. Gemini was called only as the OCR fallback after Paddle failed. No recovery success rate was measured.

PaddleOCR-VL 1.6 is the primary OCR engine. Tesseract was not used. Paddle failed on every OCR attempt in this run: first `cudnn_cnn64_9.dll` (`WinError 127`), then `paddle has no attribute 'tensor'`. Dollar cost is not reported because the Gemini responses did not include token counts.

## Documents

| File | Format | Result | Decision | Rows | Seconds | OCR calls | Gemini calls |
|---|---|---|---|---|---|---|---|
| `0000734513_2026_08_ZA_13_704_06092026081229.pdf` | PDF | REVIEW | UNRESOLVED | 49 | 54.7383 | 4 | 0 |
| `0000711172_2026_08_ZA_30_313_04092026034007.xlsx` | XLSX | FAIL | UNRESOLVED | 16 | 0.7408 | 0 | 0 |
| `0000700848_2026_08_ZA_30_578_05092026144147.XLS` | XLS | FAIL | UNRESOLVED | 20 | 0.1789 | 0 | 0 |
| `0000732705_2026_08_ZA_24_270_04092026051409.TXT` | TXT | FAIL | UNRESOLVED | 20 | 0.0112 | 0 | 0 |
| `0000734246_2026_08_ZA_13_249_06092026162117.html` | HTML | FAIL | UNRESOLVED | 45 | 0.2316 | 0 | 0 |
| `0000713511_2026_08_ZA_24_255_07092026171904.docx` | DOCX | REVIEW | UNRESOLVED | 55 | 0.2324 | 0 | 0 |
| `0000736672_2026_08_ZA_37_215_05092026161500.doc` | DOC | FAIL | VERIFIED | 58 | 0.042 | 0 | 0 |
| `0000701478_2026_08_ZA_01_224_08092026054726.jpg` | JPG | FAIL | UNRESOLVED | 2 | 29.8843 | 66 | 1 (2.1797s, vision) |
| `0000733955_2026_08_ZA_04_286_05092026143428.jpeg` | JPEG | REVIEW | UNRESOLVED | 26 | 44.6515 | 66 | 1 (7.7594s, vision) |
| `0000727190_2026_08_ZA_06_333_04092026081416.png` | PNG | FAIL | UNRESOLVED | 23 | 21.3902 | 66 | 1 (6.3098s, vision) |

No file raised an extraction exception. Per-file JSON is in this same folder. None of the ten outputs contain a `phase2` status.

## False verification

`0000736672...doc` is the only `VERIFIED` result (`verified: true`, reason `schema_evidence_business_ok`). Its grounding status is `insufficient_source_evidence`, reader `.doc`, discrepancy count 0. Verification was granted without a completed source re-read.

The Word text for the first six products was read independently. Printed columns are Pack, O.Stk, Purc, Tot, Sale, Qoh, Value. Those quantity cells match the JSON:

| Product | Source O.Stk / Purc / Sale / Qoh | Extracted opening / receipts / sales / closing |
|---|---|---|
| ARJUNA CAP. | 44 / 0 / 9 / 35 | 44 / 0 / 9 / 35 |
| BONNISAN DROPS | 0 / 40 / 2 / 38 | 0 / 40 / 2 / 38 |
| BONNISAN SYP.\<B\> | 59 / 0 / 3 / 56 | 59 / 0 / 3 / 56 |
| BONNISAN SYP.\<S\> | 121 / 0 / 10 / 111 | 121 / 0 / 10 / 111 |
| BONNISPAZ DROPS | 62 / 0 / 0 / 62 | 62 / 0 / 0 / 62 |
| BRESOL NS SPRAY | 10 / 0 / 2 / 8 | 10 / 0 / 2 / 8 |

ARJUNA's printed Value is 8189.23 and the JSON stores that as `closing_value`. The same row stores `sales_value` 0.0. The sheet has one Value column, not a sales-value column. The remaining 52 rows were not checked one by one, so this is not a document accuracy percentage.

The other nine documents stayed `UNRESOLVED`. Their wrong quantities were not marked verified.

## Mismatches checked against the source

Values below were read from the file text, cells, or the image. Agreement between the parser and grounding is not treated as proof.

### XLSX `0000711172` — FAIL

Sheet `Stock_And_Sales_Report`. A printed dash is stored as 0. Grounding compared 16 rows, reported 9 discrepancies and 8 `blank_read_as_zero`, and the decision stayed unresolved.

| Product | Field | Source | Extracted |
|---|---|---|---|
| CYSTONE TABLETS | PURCHASE | dash | 0 |
| CYSTONE TABLETS | SALE | dash | 0 |
| EVECARE SYRUP 200 ML | CLOSING | dash | 0 |
| EVECARE SYRUP 200 ML | OPENING / SALE | 196 / 196 | 196 / 196 |

CYSTONE opening 700 and closing 700 match. EVECARE's closing dash was flagged.

### XLS `0000700848` — FAIL

Sheet columns are `op_stock`, `rec_qty`, `iss_qty`, `clos_qty`. Grounding did not read the sheet (`insufficient_source_evidence`, reader `.xls`), so these mismatches were not flagged as source discrepancies. The decision is unresolved because the stock identity failed.

| Product | Source op / rec / iss / clos | Extracted opening / receipts / sales / closing |
|---|---|---|
| BONNISAN DROP 30ML | 6 / 0 / 1 / 5 | 6 / 0 / 0 / 0 |
| BONNISAN SYP 100ml | 5 / 16 / 11 / 10 | 5 / 0 / 0 / 0 |
| BRESOL SYP 200ML | 2 / 0 / 1 / 1 | 2 / 0 / 0 / 0 |

### HTML `0000734246` — FAIL

Header: Op, Purc, Sale, WSale, Jul Sa, Jun Sa, Sa val, Cl qty. 45 data rows were counted in the HTML table, matching the extracted row count. Blank purchase cells are stored as 0.

| Product | Field | Source | Extracted |
|---|---|---|---|
| BONNISAN DROPS 30ML | Op / Sale / Cl | 55 / 10 / 45 | 55 / 10 / 45 |
| BONNISAN DROPS 30ML | Purc | blank | 0 |
| BONNISAN LIQ 100ML | Sale | 52 | 20 |
| BONNISAN LIQ 100ML | Jun Sa | 20 | stored in `sales_qty` |
| BONNISAN LIQ 100ML | Op / Cl | 59 / 7 | 59 / 7 |
| BONNISAN LIQ 200ML | Sale | blank | 0 |
| BONNISAN LIQ 200ML | Op / Cl | 13 / 13 | 13 / 13 |

Sale was taken from Jun Sa on the second row. That is a wrong column mapping. The document was not verified.

### TXT `0000732705` — FAIL

The printed header is `DESCRIPTION/PACKING`, `Pro. Age`, `OPENING QTY`, `PUR. QTY`, `SALE QTY`, `CLOSING QTY`.

| Product | Field | Source | Extracted |
|---|---|---|---|
| BLEMINOR CREAM | Pro. Age | 51 | `packing` 51 |
| BLEMINOR CREAM | OPENING / PUR / SALE / CLOSING | 8 / 0 / 0 / 8 | 8 / 0 / 0 / 8 |
| BONNISAN DROPS | OPENING / PUR / SALE / CLOSING | 118 / 0 / 80 / 38 | 118 / 0 / 80 / 38 |

The age column was stored as packing. The quantity columns on these two rows match. The other 18 rows were not checked cell by cell.

### DOCX `0000713511` — REVIEW

The statement is paragraph text, not a Word table. The printed header is `SALE QTY/VALUE` and `CLOSING QTY/VALUE`. There is no opening or receipt column.

| Product | Source sale qty / closing qty | Extracted sales / closing | Extracted opening / receipts |
|---|---|---|---|
| ARJUNA TAB | 15 / 1 | 15 / 1 | 0 / 0 |
| BONNISAN 200ML | 17 / 11 | 17 / 11 | 0 / 0 |
| BONNISAN DROP | 9 / 87 | 9 / 87 | 0 / 0 |

Sale and closing match on these three rows. Opening and receipts are zeros for columns the page does not print. Grounding status is `insufficient_source_evidence`. The decision is unresolved because the stock identity fails. The other 52 rows were not checked.

### PDF `0000734513` — REVIEW

Native text, 2 pages. First rows, columns Op / Purc / Sale / Cl qty:

| Product | Source | Extracted |
|---|---|---|
| BONNISAN DROP 30ML | 50 / 50 / 50 / 50 | 50 / 50 / 50 / 50 |
| BONNISAN LIQUID 100ML | 138 / 0 / 4 / 134 | 138 / 0 / 4 / 134 |
| BONNISAN LIQUID 200ML | 27 / 0 / 0 / 27 | 27 / 0 / 0 / 27 |

The other printed columns (scheme, sales return, sales value, closing value) were not scored. Grounding did not confirm the file: four Paddle calls failed and the grounding reader is `ocr_text` with `insufficient_source_evidence`. The decision is unresolved because business validation is inconsistent. No field-accuracy percentage is claimed for all 49 rows.

### JPG `0000701478` — FAIL

The image shows one product row, SPEMAN Tab, 60s, with 15.00, 0.00, 0.00, 15.00, and a group total of 2,150.06. The JSON has two rows, HIM ZANDRA and SPEMAN Tab, and every quantity is 0. The group total is absent. Paddle failed; one Gemini vision call returned text that grounding still judged insufficient. The decision is unresolved, so the zeros were not verified.

### PNG `0000727190` — FAIL

The screenshot header is Opening, Purchase, Total, Sale, SaleRet, Exp/Dmg, CloseStock.

| Product | Source opening / purchase / total / sale / close | Extracted opening / receipts / sales / closing |
|---|---|---|
| HIORA SG GEL | 5 / 50 / 55 / 1 / 54 | 5 / 50 / 0 / 55 |

Sale 1 is missing. Closing is the Total column, 55, rather than CloseStock 54. The decision is unresolved. The other screenshot rows were not scored one by one.

### JPEG `0000733955` — REVIEW

The photograph is a stock and sales sheet with Opening, Receipt, Issue, and Closing. Extracted names include `CISTON SYRUP` and `CYSTINE FORTE TAB`. Those are not the printed names visible on the page. The photo is too uneven to assign a checked quantity to every row, so no field-accuracy figure is reported. Business validation is inconsistent and the decision is unresolved. Paddle failed; one Gemini vision call did not produce usable grounding text.

## Source evidence

Images and the PDF grounding path report `insufficient_source_evidence` and `blocks_verification: true`. The XLS, TXT, HTML, DOCX, and DOC results also say `insufficient_source_evidence`, with `blocks_verification` left false. Only the XLSX grounding status is `checked`. No `source_document` table was truncated. Batch was not a column on these ten files. The XLSX keeps an unmapped `ITEMCODE` column in the sheet; it was not emitted as a dynamic Batch field.

## Recommended fixes, by severity

1. Do not grant `VERIFIED` when grounding status is `insufficient_source_evidence`. The DOC result shows a completed stock equation being treated as proof while the source was not re-read.
2. Repair the PaddleOCR-VL runtime. `cudnn_cnn64_9.dll` fails to load, and the following calls fail with `paddle.tensor`. Each image then made 66 OCR attempts and one Gemini vision call without a usable source table.
3. On the HTML layout, map Sale to the Sale column. Jun Sa was stored as `sales_qty` for BONNISAN LIQ 100ML (20 instead of 52).
4. Read `.xls` cells in source grounding. Issue and closing on the three checked rows were extracted as 0 and the grounding report did not compare them.
5. Keep a printed dash, and a column the page does not contain, as null. The XLSX dash and the DOCX missing opening column were stored as 0.
6. On the Krishna text layout, keep Pro. Age out of packing. BLEMINOR's age 51 was stored as packing.
7. Run this audit again with `PHASE2_RECOVERY_ENABLED=true` in the process environment. Recovery behavior and recovery cost were not measured.
