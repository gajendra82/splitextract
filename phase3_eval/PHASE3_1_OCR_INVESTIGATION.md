# Executive Summary

The 37 `STOCK_IDENTITY_FAILURE` documents are not Tesseract failures.

Eleven documents were compared to the source file and to the parser quantities that identity validation actually scored. None of those eleven fail because Tesseract substituted a wrong digit for a printed quantity. They fail because a printed movement column is dropped or folded (`MAPPING_ERROR`, 5), the applied equation omits a printed movement that exactly explains the gap (`IDENTITY_EQUATION_ERROR`, 3), a non-stock row or a single digital row is wrong (`PARSER_ERROR`, 2), or the layout has no identity profile and is forced invalid (`PROFILE_ERROR`, 1).

The other 26 were not given an OCR label. On 25 of them the canonical quantities come from a Gemini image read, or from embedded text that was not row-compared. Tesseract, when it runs, is a header peek used to choose that reader. One photo (`pack_opening_receipt_issue_mexp`) does use Tesseract word geometry as the number source, and that file was not row-compared, so its cause stays `UNKNOWN`.

One secondary OCR defect exists inside a mapping failure: on `0000700259` the product-wise photo reader glued three issue subcolumns into `sales_qty` 3337. The same document already fails on rows whose printed digits were read separately, because free and goods quantities are folded into the canonical fields. Fixing that cell would not make the document pass.

PaddleOCR was not installed and was not run. Replacing Tesseract would not change the eleven source-compared failures, and it would not change the Gemini vision reads.

Phase 2 was not modified. The production OCR path was not modified. Validation was not loosened.

**Recommendation: INVESTIGATE_PARSER_PROFILE**

# 37 STOCK_IDENTITY_FAILURE Analysis

Source of the set: `phase3_eval/records`, `failure_type == STOCK_IDENTITY_FAILURE`. Count: 37. The August folder was not reprocessed.

Manifest: `phase3_eval/identity_failure_manifest.json`.
Machine-readable causes: `phase3_eval/identity_failure_root_causes.json`.

Every document used recovery strategy `targeted_stock_extraction`. Baseline validation is `FAIL` on all 37. Recovery is `RECOVERY_REJECTED` on 36 and `RECOVERY_UNRESOLVED` on `0000701789` (profile `unknown`, 0 attempts).

QA extracts were used only when row count and extraction method both matched the Phase 3 record (8 documents). Stale and missing QA JSON was not treated as the Phase 3 result. Fresh local extracts, with Phase 2 off, were used for the digital PDFs, the xlsx, the xls, and the docx.

# Failure Distribution

## By profile

| Profile | Failures |
| --- | ---: |
| generic | 31 |
| product_wise_image | 2 |
| product_wise | 2 |
| saleret | 1 |
| unknown | 1 |

The failures are concentrated in `generic` (31 of 37). That concentration is a profile-selection fact, not an OCR fact. `product_wise_stock_statement_pdf` is in the known-generic method list, so a product-wise PDF is scored with `opening + receipts - sales - scheme - stock_out` instead of the product-wise equation.

## By layout / extraction method

| Extraction method | Failures | Number source for identity |
| --- | ---: | --- |
| gemini_extraction_fallback | 4 | Gemini image |
| zandra_stock_sale_vision | 4 | Gemini image |
| swil_receipt_pur_value_vision | 3 | Gemini image |
| pdf_split_page_images | 3 | Gemini page images |
| pack_op_pur_bal_stock_sale_vision | 3 | Gemini image |
| gemini_vision | 3 | Gemini image |
| product_wise_stock_statement_image | 2 | Tesseract geometry |
| product_wise_stock_statement | 2 | Embedded PDF text |
| pharma_hub_jun_jul_photo | 2 | Gemini image; Tesseract is a header peek |
| generic (xlsx, docx) | 2 | Spreadsheet / Word |
| pdf_text_gemini | 1 | Embedded PDF text, then Gemini text |
| psr_closstock_columns | 1 | Embedded PDF text |
| product_wise_stock_statement_pdf | 1 | Embedded PDF text |
| swilerp_retrn_sales_stock | 1 | PyMuPDF text |
| marg_closing_mexp_photo | 1 | Gemini image; Tesseract is a header peek |
| pack_opening_receipt_issue_mexp | 1 | Tesseract word boxes |
| zandra_two_column_order_form | 1 | Layout has no profile |
| main_stock_sales_statement | 1 | Not row-compared |
| marg_erp_xls_receive_issue_value | 1 | Spreadsheet |

Empty layout string: 20 documents. Those are mostly vision and generic methods that did not set `totals.extra.layout`.

## By extraction family

| Family | Failures |
| --- | ---: |
| Gemini vision / image fallback | 25 |
| Embedded digital text | 6 |
| Spreadsheet | 2 |
| Tesseract geometry as the stored numbers | 3 |
| Word | 1 |

## By OCR requirement

| OCR required | Failures | OCR path |
| --- | ---: | --- |
| false | 9 | digital_text 5, spreadsheet 2, pymupdf_fallback 1, native_text 1 |
| true | 28 | ocr_required 28 |

The 9 documents with `ocr_required: false` cannot be Tesseract digit errors. File types: pdf 17, jpg 12, jpeg 4, docx 1, xlsx 1, xls 1, no extension 1.

## By OCR quality

Telemetry quality is 100 on 20 documents and 75 on 17. Quality 100 includes digital PDFs and spreadsheets that never passed through Tesseract. It is not a measure of numeric accuracy.

# Root Cause Distribution

| Root cause | Documents |
| --- | ---: |
| OCR_ERROR | 0 |
| MAPPING_ERROR | 5 |
| IDENTITY_EQUATION_ERROR | 3 |
| PARSER_ERROR | 2 |
| PROFILE_ERROR | 1 |
| EVIDENCE_ERROR | 0 |
| PROVIDER_ERROR | 0 |
| UNKNOWN | 26 |

`UNKNOWN` means the source row was not compared to the stored quantities. It does not mean the cause might still be Tesseract. For 25 of those 26, the quantities identity validation scored were produced by Gemini or by uncompared embedded text. One remaining photo uses Tesseract geometry and was not opened row by row.

# Tesseract Analysis

## Call graph

`sales_image_to_string` is not a separate function. It is the log label on `GatedPytesseract.image_to_string` in `services/sales_extraction_runtime.py`. That wrapper calls pytesseract, counts the call, and caches by image and config. `image_to_data` is a second label, `sales_image_to_data`.

`_ocr_image_to_text` (`sales_statement_extractor.py`) is the shared string OCR helper:

1. Open the bytes with PIL.
2. Convert to RGB when the mode is not RGB or L.
3. Do not apply EXIF orientation in this helper.
4. Resize with Lanczos only when the longest side exceeds `SALES_OCR_MAX_IMAGE_SIDE`. Otherwise log the original size and OCR it unchanged.
5. Optional autocontrast and sharpness only when `enhance=True`.
6. One `image_to_string` call, PSM 6 by default.

Many other readers open the image themselves, call `ImageOps.exif_transpose`, and then call `image_to_string` or `image_to_data` on the full page, a header crop, or a column crop. Those sites do apply EXIF. The shared helper does not.

## Where OCR sits relative to layout detection

OCR often runs before the parser is chosen. Photo readers call `_ocr_image_to_text` to peek at the header, and only then decide whether the image matches that layout. The identity numbers are filled later:

- Product-wise photos: peek string, then a second Tesseract geometry pass (`image_to_data` on panels, with a 2x resize). Those word boxes become opening, receipt, issues, and closing. Gemini fallback is skipped for this method.
- Pharma Hub Jun/Jul and Marg closing M.EXP: the peek decides the layout. The quantities sent to validation are Gemini's reading of the original image bytes, not the Tesseract string.
- Vision methods (`gemini_vision`, `zandra_stock_sale_vision`, `swil_receipt_pur_value_vision`, `pack_op_pur_bal_stock_sale_vision`, `pdf_split_page_images`, `gemini_extraction_fallback`) send image bytes to the model.

Digital PDFs in this failure set that contain embedded text (`0000700017`, `0000700026`, `0000700238`, `0000700283`, `0000700398`) did not need Tesseract for the numbers that failed. Spreadsheet and Word files did not need it either.

## Duplicate OCR

Duplicate OCR is real and was not changed:

- Product-wise photos OCR once to peek and again for column geometry.
- Header-gated vision readers OCR to classify, then send the original image to Gemini, so the Tesseract string is discarded for identity.
- Several other image readers call `image_to_string` on the header, on crops, and on the full page. The gate cache avoids an identical image-and-config pair. A crop is a different call.

These are performance observations. They are not the cause of the 37 identity failures.

## Was OCR required for each failure?

| Group | Count | Required for the failing numbers? |
| --- | ---: | --- |
| ocr_required false | 9 | No |
| Gemini image is the stored result | 25 | No. A peek may still run. |
| Tesseract geometry is the stored result | 3 | Yes for the two product-wise photos and the one pack M.EXP photo |

# Source-compared failure table

OCR engine below is the engine that produced the stored quantities. Where that engine is not Tesseract, the Tesseract columns are not applicable.

## 0000700017 — MAPPING_ERROR

- filename: `0000700017_2026_08_ZL_06_476_02092026121836.pdf`
- format: digital PDF
- layout: (empty)
- profile: generic
- OCR required: false
- OCR engine: none for these numbers (`pdf_text_gemini` on embedded text)
- OCR quality: 75

Printed columns include Op.Qty, Pur.Qty, ORec.Qty, Sale.Qty, OIss.Qty, Cl.Qty.

AACTARIL SOAP, source: opening 40, purchase dash, ORec 2, sale 11, closing 31. `40 + 2 - 11 = 31`.

Parser: opening 40, receipts 0, sales 11, closing 31. Expected closing 29. Delta +2, which is the unmapped ORec.Qty.

21 of 49 matched rows miss the equation. 28 hold. Tesseract did not read this file's quantities.

## 0000700026 — PARSER_ERROR

- filename: `0000700026_2026_08_ZL_06_313_03092026130418.pdf`
- format: digital PDF
- layout: `closstock_saleret_expdmg`
- profile: saleret
- OCR required: false
- OCR engine: none
- OCR quality: 100

24 of 25 rows satisfy the saleret equation. The failing row is opening 3, receipts 210, sales 113, sale return 0, expiry/damage 0, closing 97. Expected closing 100. Delta -3. Both adjustment columns the profile knows about are zero, so this is one misread digital row, not a missing column and not OCR.

## 0000700238 — MAPPING_ERROR

- filename: `0000700238_2026_08_ZL_18_377_03092026154026.pdf`
- format: digital PDF
- layout: (empty); method `product_wise_stock_statement_pdf`
- profile: generic, because that method name is on the known-generic list
- OCR required: false
- OCR engine: none
- OCR quality: 100

Failing row: opening 0, receipts 48, sales 50, receipts_free 2, closing 0. Generic expected closing is -2. Delta +2 equals `receipts_free`. The product-wise equation `opening + receipts - sales` has the same gap, so selecting `product_wise` would not make this row pass. The free quantity has to be included in the canonical receipt or issue before either equation closes.

Second failing row: opening 0, receipts 200, sales 172, issues_free_repl 2, closing 26. Expected 28. Delta -2 equals the free-issues quantity.

## 0000700259 — MAPPING_ERROR

- filename: `0000700259_2026_08_ZL_18_379_03092026174312.jpg`
- format: JPEG photo
- layout: `product_wise_stock_statement_photo`
- profile: `product_wise_image`
- OCR required: true
- OCR engine: Tesseract, two passes (peek string, then geometry)
- OCR quality: 100

Printed columns include Opening, Receipt Qty, Receipt Free, Issues Qty, Issues Free, Goods Issue, Closing. The reader comment states that free quantities are folded into `receipts_qty` and `sales_qty` so that `opening + receipt - issue = closing`.

PILEX FORTE on the page: opening 10, receipt qty 110, issues qty 20, closing 100. `10 + 110 - 20 = 100`. Parser stored receipts 130 and sales 46 after folding free and goods, so the profile equation expected 94 against closing 100.

That is a mapping fold. Tesseract had already separated the qty columns.

Secondary OCR defect on one row: PILEX TAB `sales_qty` 3337 is the glued digit string of issue subcolumns 30, 33, and 37. `_pwss_photo_split_glued` did not split it. This cell is an `OCR_ERROR`. It is not the document root cause, because other rows fail after a correct read.

## 0000700325 — MAPPING_ERROR

- filename: `0000700325_2026_08_ZL_18_379_03092026175634.jpg`
- format: JPEG photo
- layout: `product_wise_stock_statement_photo`
- profile: `product_wise_image`
- OCR required: true
- OCR engine: Tesseract geometry
- OCR quality: 100

Same reader as `0000700259`. Matched extract, sample row: opening 72, receipts 21, receipts_free 21, sales 0, closing 51. Expected 93. The free quantity was copied into the canonical receipt, and the equation then adds it. 9 equation mismatches, 34 rows hold, 1 row whose delta equals an unapplied column.

## 0000700283 — IDENTITY_EQUATION_ERROR

- filename: `0000700283_2026_08_ZL_18_377_01092026063405.pdf`
- format: digital PDF
- layout: `product_wise_stock_statement`
- profile: `product_wise`
- OCR required: false
- OCR engine: none
- OCR quality: 100

Equation used: `opening + receipts - sales`. Short-expiry is stored on the row and not subtracted.

CONFIDO TABS: opening 66, receipts 0, sales 0, sh_exp 3, closing 63. `66 - 3 = 63`.
HAIR ZONE SOLN: 32, 0, 0, sh_exp 3, closing 29.
LIV-52 SYP: opening 42, receipts 70, sh_exp 69, closing 43. `42 + 70 - 69 = 43`.

The parser preserved the short-expiry figure. The profile equation does not use it. This is not an OCR substitution.

## 0000700398 — IDENTITY_EQUATION_ERROR

- filename: `0000700398_2026_08_ZL_18_377_01092026063708.pdf`
- format: digital PDF
- layout: `product_wise_stock_statement`
- profile: `product_wise`
- OCR required: false
- OCR engine: none
- OCR quality: 100

Same gap as `0000700283`. HIMCOCID SUSP: opening 23, sales 0, sh_exp 6, closing 17. LIV 52 SYR: opening 23, sh_exp 23, closing 0. PILEX FORTE TAB: opening 20, sh_exp 5, closing 15.

## 0000728682 — IDENTITY_EQUATION_ERROR

- filename: `0000728682_2026_08_ZL _10_380_04092026092536.xlsx`
- format: xlsx
- layout: (empty)
- profile: generic (`opening + receipts - sales - scheme - stock_out`)
- OCR required: false
- OCR engine: none
- OCR quality: 100

Sheet columns include Opening_Qty, Purchase_Qty, Sale_Qty, Sale_Ret_Qty, Closing_Qty.

AACTARIL SOAP source: 122, purchase 0, sale 3, sale return 0, closing 119. Holds.
HERBOLAX TABS source: 47, sale 4, sale return 1, closing 44. `47 - 4 + 1 = 44`. Parser omits the return, expects 43, delta +1.
LIV-52 SYRUP: opening 82, purchase 105, sale 77, sale return 2, closing 112. Delta +2 equals the return.
PILEX FORTE TAB: opening 99, sale 1, sale return 1, closing 99. Delta +1 equals the return.

Four parser rows fail. The three small deltas match `Sale_Ret_Qty`. Tesseract is not involved.

## 0000734248 — PARSER_ERROR

- filename: `0000734248_2026_08_ZL_12_554_07092026051856.XLS`
- format: xls
- layout: (empty)
- profile: generic
- method: `marg_erp_xls_receive_issue_value`
- OCR required: false
- OCR engine: none
- OCR quality: 100

Sheet header: OPENING STOCK, RECEIVE QUANTITY, ISSUE QUANTITY, CLOSING STOCK. AACTARIL SOAP source: 40, 0, 0, 40. Holds.

Parser: 34 of 35 rows satisfy the generic equation. The failing row is the text `HIMALAYA WELLNESS COM INDORE` with opening `5126107167` and receipts 21. That is a header row emitted as a stock row. It is not an OCR error.

## 0000701555 — MAPPING_ERROR

- filename: `0000701555_2026_08_ZL_37_390_06092026182510.docx`
- format: docx
- layout: (empty)
- profile: generic
- OCR required: false
- OCR path: native_text
- OCR quality: 75

Sample parser rows store closing 0 against a positive opening (ABANA opening 60, sales 5, closing 0; ALTHEA LOTION opening 100, sales 0, closing 0). ABANA also carries `sales_scheme` 756.8, which the generic equation subtracts, producing expected closing -701.8. The scheme figure is not a quantity. Closing was not mapped from the Word table. No OCR step produced these numbers.

## 0000701789 — PROFILE_ERROR

- filename: `0000701789_2026_08_ZL_20_557_05092026163636.jpg`
- format: JPEG
- layout: `zandra_two_column_order_form`
- profile: unknown
- OCR required: true (file is a photo)
- OCR quality: 100
- recovery: `RECOVERY_UNRESOLVED`, attempts 0

`mark_unknown_identity` sets `stock_validation.is_valid` to false and `fail_count` to 0. Phase 1 reports that as `STOCK_IDENTITY_FAILURE` without scoring rows. The failure is the missing profile, not a misread digit.

# Documents left UNKNOWN

These 26 were not labeled `OCR_ERROR` because the identity check did not score a Tesseract string, or because the Tesseract geometry file was not compared row by row. Inventing a column cause for them would be a guess.

`0000700084` was inspected as a page image. The visible grid is Opstk, Rcpt Qty, Sale Qty, Clstk. The matched parser row for the face-wash/ointment line is opening 37, receipts 100, sales 40, closing 68, the same figures visible on the page. `37 + 100 - 40 = 97`, not 68. The model preserved the visible numbers, and those numbers do not close. Tesseract is not the stored-number source (`zandra_stock_sale_vision`). The row stays `UNKNOWN` because no fifth printed movement was identified, and the equation was not blamed for a four-column grid that itself does not balance.

| Document | Method | Why it is not an OCR_ERROR |
| --- | --- | --- |
| 0000700084 | zandra_stock_sale_vision | Gemini image; page and parser agree, row does not close |
| 0000700124 | swil_receipt_pur_value_vision | Gemini image |
| 0000700275 | pdf_split_page_images | Gemini page images |
| 0000700317 | swil_receipt_pur_value_vision | Gemini image |
| 0000700358 | pdf_split_page_images | Gemini page images |
| 0000700379 | swilerp_retrn_sales_stock | PyMuPDF text, OCR not required |
| 0000700434 | pack_op_pur_bal_stock_sale_vision | Gemini image |
| 0000700481 | pack_op_pur_bal_stock_sale_vision | Gemini image |
| 0000700536 | pharma_hub_jun_jul_photo | Tesseract peeks the header; Gemini reads the quantities |
| 0000700624 | pharma_hub_jun_jul_photo | Same peek-then-Gemini path |
| 0000701012 | gemini_vision | Gemini image |
| 0000701051 | gemini_extraction_fallback | Gemini image |
| 0000701078 | gemini_vision | Gemini image |
| 0000701116 | marg_closing_mexp_photo | Tesseract peeks; Gemini reads the quantities |
| 0000701209 | gemini_vision | Gemini image |
| 0000701273 | gemini_extraction_fallback | Gemini image |
| 0000701275 | pack_opening_receipt_issue_mexp | Tesseract geometry is the number source; row not compared |
| 0000701468 | pack_op_pur_bal_stock_sale_vision | Gemini image |
| 0000701904 | main_stock_sales_statement | Not row-compared |
| 0000702913 | zandra_stock_sale_vision | Gemini image |
| 0000704337 | gemini_extraction_fallback | Gemini image |
| 0000722000 | zandra_stock_sale_vision | Gemini image |
| 0000723881 | zandra_stock_sale_vision | Gemini image |
| 0000729951 | swil_receipt_pur_value_vision | Gemini image |
| 0000729971 | pdf_split_page_images | Gemini page images |
| 0000730089 | gemini_extraction_fallback | Gemini image |

# PaddleOCR Analysis

PaddleOCR was not evaluated.

The controlled comparison is required only when the investigation finds genuine Tesseract errors that explain the identity failures. The document-level count of `OCR_ERROR` is 0. The one glued-digit cell sits inside a document that still fails after free and goods are folded. Running another OCR engine through the same parser would reproduce the mapping, equation, and profile failures, and it would not replace the Gemini image reads.

No production default was changed. No isolated Paddle path was added. No Paddle tests were added.

# Tesseract vs PaddleOCR

Not run.

There is no Tesseract-pass to Paddle-fail count, and no Tesseract-fail to Paddle-pass count. Those transitions were not measured because the experiment would not test the cause that was found.

# Safety

Production parsers, identity profiles, projection guard, recovery policy, and the Tesseract call path were left as they were.

| Check | Result |
| --- | --- |
| PASS to WRONG | 0 |
| FAIL to WRONG | 0 |
| null to zero introduced by this investigation | 0 |
| unknown layout accepted | 0 |
| dynamic field changed | 0 |
| canonical values changed | 0 |
| Phase 2 modified | no |

Phase 3 already recorded the same safety zeros on the 100-document batch. This investigation did not re-score recovery.

# Performance notes

Not optimized. Recorded only.

- Product-wise photos run Tesseract twice: a peek via `_ocr_image_to_text`, then a geometry pass.
- Pharma Hub and Marg M.EXP photos run Tesseract to recognize the header, then ignore that string and send the image to Gemini.
- `_ocr_image_to_text` resizes only above `SALES_OCR_MAX_IMAGE_SIDE`. It does not apply EXIF. Other call sites do apply EXIF and sometimes a 2x upscale, so the same photo can be preprocessed more than once.
- Digital PDFs and spreadsheets in the source-compared set did not need OCR. The `ocr_required` flag already separated them (9 of 37).
- The gate cache collapses identical image-and-config calls. Crops and a second PSM are still separate calls.

# Recommendation

**INVESTIGATE_PARSER_PROFILE**

Scenario A. Most of the failures that were checked against the source document are mapping, equation, parser, or profile failures. OCR replacement is not the solution.

Next investigation, still without changing Phase 2 or loosening validation:

1. Stop folding product-wise free and goods into canonical quantities when the printed qty columns already close, and decide at profile level whether short-expiry is part of product-wise identity.
2. Map ORec-style other-receipt columns into `receipts_qty` for the digital layout used by `0000700017`.
3. Include sale returns in the identity rule for spreadsheets that print `Sale_Ret_Qty`, at profile level, not by filename.
4. Keep unknown layouts fail-closed. `0000701789` is that behavior working.
5. Treat the 26 uncompared vision documents as a Gemini-read investigation, not as an OCR-engine trial. `0000701275` is the only remaining file where a Tesseract geometry comparison is still worth doing, and only if that layout still fails after the mapping and equation work.
