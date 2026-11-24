# Phase 3.7 — Next failure cluster

PHASE 3.7 DECISION: TARGETED_INVESTIGATION

No production code was changed. The full dataset was not run. Gemini was not called. Phase 2 and OCR were not changed.

## 1. Remaining failure count

32 of the original 37 `STOCK_IDENTITY_FAILURE` documents are still open.

Closed and not reopened:

- Phase 3.3: `0000700017`, `0000734248`, `0000701555`
- Phase 3.6: `0000700283`, `0000700398`

The original baseline also had 44 FAIL results: those 37 identity failures, 4 `PROVIDER_ERROR`, and 3 `EMPTY_OUTPUT`. The 7 non-identity failures are unchanged. They are not one layout, so they are not ranked below.

The 32 still-open identity failures:

| Group | Documents | Parser / layout | Profile | Failure type | File type | Suspected cause |
|---|---|---|---|---|---|---|
| Product-wise photo free/goods fold | 2 | `product_wise_stock_statement_image` / `product_wise_stock_statement_photo` | `product_wise_image` | `MAPPING_ERROR` | JPEG | Free and goods are added into canonical qty |
| Zandra stock-sale vision | 4 | `zandra_stock_sale_vision` | generic | `UNKNOWN` | PDF / no extension | Not established |
| Gemini extraction fallback | 4 | `gemini_extraction_fallback` | generic | `UNKNOWN` | image / PDF | Not one layout |
| Swil receipt vision | 3 | `swil_receipt_pur_value_vision` / `swil_receipt_pur_value` | generic | `UNKNOWN` | PDF | Not established |
| Split-page images | 3 | `pdf_split_page_images` | generic | `UNKNOWN` | PDF | Not established |
| Pack / op / pur / bal vision | 3 | `pack_op_pur_bal_stock_sale_vision` | generic | `UNKNOWN` | image | Not established |
| Gemini vision | 3 | `gemini_vision` | generic | `UNKNOWN` | image | Not established |
| Pharma Hub photo | 2 | `pharma_hub_jun_jul_photo` | generic | `UNKNOWN` | JPEG | Not established |
| Product-wise PDF free columns | 1 | `product_wise_stock_statement_pdf` | generic | `MAPPING_ERROR` | PDF | Free stays on `extra`; separate from the photo fold |
| Sale return / Other_Qty | 1 | semantic spreadsheet | `saleret` | one `Other_Qty` row | xlsx | `0000728682` VASAKA still fails |
| Closstock one row | 1 | `psr_closstock_columns` | `saleret` | `PARSER_ERROR` | PDF | One digital row, delta −3 |
| Swil ERP one row | 1 | `swilerp_retrn_sales_stock` | generic | `UNKNOWN` | PDF | One row off by 1 |
| Single vision / order-form leftovers | 4 | marg photo, pack M.EXP, order form, main stock | generic or `unknown` | `UNKNOWN` or `PROFILE_ERROR` | image | Not established. `0000701789` stays fail-closed on purpose |

The 26 vision documents that Phase 3.1 left `UNKNOWN` are still `UNKNOWN`. An image file is not an `OCR_ERROR`.

## 2. Top 3 clusters with counts

1. **Product-wise photo free/goods fold — 2 documents.** Same parser, same layout, same profile, source-compared `MAPPING_ERROR`, and a local fold that can be checked on both files.
2. **`zandra_stock_sale_vision` — 4 documents.** Largest single method still failing. Phase 3.1 compared one page (`0000700084`) and left it `UNKNOWN`: the four visible columns do not close, and no fifth movement was identified. No shared cause is assigned to the other three.
3. **`swil_receipt_pur_value_vision` — 3 documents.** Same method and layout label. No row-level source comparison. Cause stays `UNKNOWN`.

`gemini_extraction_fallback` is also 4 documents. It is a fallback bucket, not one printed layout, so it is not ranked above a named parser.

## 3. The single recommended next cluster

Product-wise photo free/goods fold.

Phase 3.8 should investigate only this cluster.

## 4. Exact documents in that cluster

- `0000700259_2026_08_ZL_18_379_03092026174312.jpg`
- `0000700325_2026_08_ZL_18_379_03092026175634.jpg`

## 5. Why they belong to the same cluster

Both use method `product_wise_stock_statement_image`, layout `product_wise_stock_statement_photo`, and profile `product_wise_image`. Both are JPEG photos. Both are `STOCK_IDENTITY_FAILURE` / `MAPPING_ERROR`. The photo reader adds Receipt Free, Issues Free, and Goods Issue into `receipts_qty` and `sales_qty`, then scores `opening + receipt - issues = closing`.

They are not the digital Issues/Sh.Exp bug fixed in Phase 3.6, and they are not `0000700238`. That PDF stores free on `extra` and does not fold it. These photos do the opposite: they fold free and goods into the canonical quantities.

## 6. Evidence supporting the suspected root cause

Phase 3.1 compared both files to the source and to the stored extract. Tesseract is the number source, and the document root cause is the fold, not a wrong engine.

`0000700259`, 17 rows, 14 failing. Printed PILEX FORTE is opening 10, receipt qty 110, issues qty 20, closing 100. `10 + 110 - 20 = 100`. The parser stored receipts 130 and sales 46 after folding free and goods, so the profile expected 94 against closing 100. One cell, PILEX TAB `sales_qty` 3337, is a glued issue-subcolumn string. Phase 3.1 called that cell a secondary OCR defect. Other rows already fail after a separated read, so that cell is not the document cause.

`0000700325`, 44 rows, 10 failing, 34 holding. Sample row: opening 72, receipts 21, `receipts_free` 21, sales 0, closing 51, expected 93. The free quantity was copied into the canonical receipt and the equation adds it again.

The reader does this on purpose. `_parse_product_wise_stock_statement_photo_panel` sets `receipts_qty` to receipt qty plus receipt free, and `sales_qty` to issues qty plus issues free plus goods. The image identity block then uses those folded totals.

## 7. What source documents need to be inspected

- The two JPEGs above, in the August dataset.
- The matched extracts already stored for them:
  - `splitextract/qa_results_images/extracted/0000700259_2026_08_ZL_18_379_03092026174312.json`
  - `splitextract/qa_results_images/extracted/0000700325_2026_08_ZL_18_379_03092026175634.json`
- The 9 `product_wise_image` documents in the same 100-document set that already PASS. They use this same reader. They are the regression set, not members of the failure cluster.

Do not reopen `0000700238`, `0000700283`, or `0000700398`.

## 8. Expected smallest safe fix, without implementing it

Only inside the photo reader (`_parse_product_wise_stock_statement_photo_panel` and `_pwss_photo_repair_row` if that repair depends on the folded totals):

When the printed qty columns already satisfy `opening + receipt qty - issues qty = closing`, keep Receipt Free, Issues Free, and Goods Issue on `extra` and do not add them into `receipts_qty` or `sales_qty`.

Leave `issues_expected_closing` as `opening + receipt - issues`. Do not subtract `sh_exp`. Do not change the digital product-wise PDF parser, the Phase 3.6 Issues boundary, generic identity, column-mapping aliases, Phase 2, or the OCR engine. Do not treat the glued `3337` cell as a reason to replace Tesseract.

A blanket removal of the fold is not that fix. Some of the 9 passing photos may balance only after free and goods are included. Phase 3.8 has to confirm that before any edit.

## 9. Regression risk

Medium until the 9 passing `product_wise_image` photos are compared.

The change, if it stays inside the photo reader and only skips the fold when the qty columns already close, does not touch the digital product-wise parser, `0000700238`, spreadsheets, Marg, Profitmaker, or the generic equation. The risk is the other photos of this same layout. PASS → WRONG must stay 0. If a passing photo only closes because free or goods were folded in, that row must keep the fold.

PHASE 3.7 DECISION: TARGETED_INVESTIGATION
