# Phase 3.3 — Controlled implementation of verified layout fixes

FINAL DECISION: SAFE_TO_CONTINUE_WITH_INVESTIGATION

The four verified layout fixes are in place. Focused tests passed. Replay of the five source-compared documents and the local Phase 3 PASS documents produced no PASS → WRONG. The generic identity equation, OCR, and Phase 2 recovery were not changed. Product-wise free quantities were not folded. `Other_Qty` is preserved and still has no identity term, so `0000728682` remains a document-level failure. The 26 vision documents classified UNKNOWN in Phase 3.2 were not replayed and were not changed.

## 1. Changes implemented

A. Named layout `op_pur_orec_sale_oiss` for the printed header Op / Pur / ORec / Sale / OIss / Cl. Detection requires those six header lines in that order. A later page may reprint the same six lines. A statement missing any of them stays off this layout. Purchase stays `receipts_qty`. ORec is `extra.other_receipt_qty`. Sale stays `sales_qty`. OIss is `extra.other_issue_qty`. The layout equation is `closing = opening + purchase + other_receipt - sale - other_issue`. A missing term stays missing; it is not coerced to zero. `generic_expected_closing` is not used for this method.

B. Spreadsheet alias `Sale_Ret_Qty` → `extra.sale_return`, scoped to the existing semantic spreadsheet header map. Presence of `sale_return` selects the existing saleret profile. `Other_Qty` is stored as `extra.other_qty`. It is not mapped to `stock_out_qty` or `sale_return`.

C. `_parse_marg_erp_xls` stops when the row is `PURCHASE DETAIL`, or when the row is a supplier-name plus invoice header. It does not stop at the first `TOTAL`. A later stock grid that uses the same column indexes still parses.

D. The existing Profitmaker reader still prefers a header on table row 0. If row 0 is not that header, it accepts the glued Profitmaker header in the paragraph immediately above the table, including Word alternate-content runs. One company line (`Company :` or `DAXINSOFT`) may sit between that header and the table. A merged product-name cell is counted once, so the table must still be the nine Profitmaker fields. An unrelated paragraph between the header and the table does not claim the table. `Value` stays `closing_value`.

Product-wise parsing and the product-wise equation were not modified.

## 2. Files and functions changed

- `services/sales_statement_extractor.py`
  - `_is_op_pur_orec_sale_oiss_text`, `_parse_op_pur_orec_sale_oiss`
  - `_parse_pdf` calls that parser before the generic PDF path
  - `_structure_sales_text` keeps the same parser ahead of the generic text reader
  - `_apply_stock_identity_validation` returns early for method `op_pur_orec_sale_oiss`
  - `_XLS_HEADER_ALIASES`: `saleretqty` → `sale_return`, `otherqty` → `other_qty`
  - `_xls_fill_from_rows` writes those two extras
  - `_parse_marg_erp_xls` section break
  - `_paragraph_is_profitmaker_header`, `_docx_element_text`, `_profitmaker_company_paragraph`, `_profitmaker_row_texts`, `_parse_profitmaker_ostk_docx`
- `services/stock_identity_profiles.py`
  - profile `op_pur_orec_sale_oiss`
  - `op_pur_orec_expected_closing`
  - `_choose_profile` and `_row_ok` for that profile only
  - `generic_expected_closing` unchanged
- `services/column_mapping.py`
  - layout-scoped definition `op_pur_orec_sale_oiss` for Op, Pur, Sale, and Cl only
  - ORec and OIss are not mapped onto `receipts_qty` or `sales_qty`
- `services/recovery_projection.py` was not modified

## 3. Tests added

`tests/test_phase3_3_layout_fixes.py`

- Op / Pur / ORec / Sale / OIss / Cl: AACTARIL `40 + 0 + 2 - 11 - 0 = 31`, OIss nonzero, both zero, null other-receipt is not zero, unrelated statement stays off the layout, a reprinted header does not drop later rows
- `Sale_Ret_Qty`: return greater than zero selects saleret, return of zero stays saleret, negative `Other_Qty` is not `stock_out_qty`
- Marg: purchase-detail invoice is not a stock row; a second stock section after `TOTAL` still parses
- Profitmaker: paragraph header, company line plus merged product cell, unrelated paragraph does not claim the table, headerless table is not Profitmaker
- Product-wise: `receipts_free` stays out of `receipts_qty`; generic and issues equations are unchanged

## 4. Tests passed

`173 passed, 1 skipped` in:

- `tests/test_phase3_3_layout_fixes.py`
- `tests/test_profitmaker_ostk_docx.py`
- `tests/test_phase1_architecture.py`
- `tests/test_phase2_recovery.py`
- `tests/test_phase2b_recovery_contract.py`
- `tests/test_stock_identity_profiles.py`
- `tests/test_column_mapping.py`
- `tests/test_evidence_mapping.py`
- `tests/test_evidence_selector.py`
- `tests/test_recovery_projection.py`

## 5. Tests failed

None of the tests above failed.

The one skip is `tests/test_profitmaker_ostk_docx.py`. The table-row-0 fixture `0000722839_2026_08_ZL_37_390_05092026064113.docx` is not in this workspace or in Downloads. The row-0 branch is still the first branch: it maps row 0 and does not collapse merged cells. The new paragraph path runs only when that row-0 map fails.

## 6. Five-document before / after

Replay used `PHASE2_RECOVERY_ENABLED=false`. Gemini was not used as ground truth.

### 0000700017

| | |
|---|---|
| BEFORE | FAIL. Method `pdf_text_gemini`. Profile generic. 49 rows. ORec and OIss were dropped before validation. |
| AFTER | Valid. Method `op_pur_orec_sale_oiss`. Profile `op_pur_orec_sale_oiss`. 49 rows. Fail count 0. |
| WHY | The named text parser reads the six printed headers before the shared PDF text prompt. |
| SOURCE IDENTITY | AACTARIL SOAP: `40 + 0 + 2 - 11 - 0 = 31` |
| PARSER OUTPUT | opening 40, receipts 0, other_receipt 2, sales 11, other_issue 0, closing 31 |
| PROFILE | `op_pur_orec_sale_oiss` |
| EQUATION | `closing=opening+purchase+other_receipt-sale-other_issue` |

Transition: FAIL → PASS.

### 0000728682

| | |
|---|---|
| BEFORE | FAIL. Method generic. Profile generic. 52 rows. `Sale_Ret_Qty` and `Other_Qty` were dropped. |
| AFTER | Document still invalid. 52 rows. Profile `saleret`. One failing row. `stock_out_qty` is unset on every row. |
| WHY | `Sale_Ret_Qty` now reaches `extra.sale_return`, so the existing saleret profile is selected. `Other_Qty` is kept as `extra.other_qty` and is not an identity term. |
| SOURCE IDENTITY | HERBOLAX TABS: `47 - 4 + 1 = 44`. LIV-52 SYRUP: `82 + 105 - 77 + 2 = 112`. VASAKA SYRUP: `55 + (-27) = 28`. |
| PARSER OUTPUT | HERBOLAX TABS: opening 47, receipts 0, sales 4, sale_return 1, other_qty 0, closing 44, row valid. LIV-52 SYRUP: opening 82, receipts 105, sales 77, sale_return 2, other_qty 0, closing 112, row valid. VASAKA SYRUP: opening 55, receipts 0, sales 0, sale_return 0, other_qty -27, closing 28, expected 55, row invalid. |
| PROFILE | `saleret` |
| EQUATION | `total=opening+purchase; closing=total-sale+sale_return-exp_damage` |

Transition: FAIL → STILL FAIL. The sale-return rows now satisfy the existing saleret equation. The document stays failed because VASAKA's signed `Other_Qty` is not represented by that equation. That is the intended unresolved case.

### 0000734248

| | |
|---|---|
| BEFORE | FAIL. Method `marg_erp_xls_receive_issue_value`. Profile generic. 35 rows. Invoice `5126107167` became opening quantity and date `21` became receipts. |
| AFTER | Valid. Same method. Profile generic. 32 rows. Fail count 0. |
| WHY | The parser stops at `PURCHASE DETAIL` and at the supplier/invoice header. Stock column indexes are unchanged. The 32 rows are sheet rows 7–38, the stock products above `TOTAL`. The three removed rows were the purchase-detail block, not stock products. |
| SOURCE IDENTITY | AACTARIL SOAP: opening 40, receipts 0, sales 0, closing 40 |
| PARSER OUTPUT | opening 40, receipts 0, sales 0, closing 40. Invoice `5126107167` is not an opening quantity. |
| PROFILE | generic |
| EQUATION | `closing=opening+receipts-sales` |

Transition: FAIL → PASS.

### 0000701555

| | |
|---|---|
| BEFORE | FAIL. Method generic. Profile generic. 69 rows. Pack `60` was stored as opening and `756.80` was stored as sales scheme. |
| AFTER | Valid. Method `profitmaker_ostk_docx`. Profile generic. 68 product rows. Fail count 0. |
| WHY | The glued header is in the paragraph above the table, inside a Word alternate-content run, with one company line between that paragraph and the table. The product-name cell is merged, so the logical width is the nine Profitmaker fields. |
| SOURCE IDENTITY | ABANA TABLETS: `7 + 0 - 2 = 5` |
| PARSER OUTPUT | opening 7, receipts 0, sales 2, closing 5, closing_value 756.80, sales_value 0, age 148, no sales_scheme |
| PROFILE | generic, because this layout's Qoh matches opening + receipts - sales |
| EQUATION | `closing=opening+receipts-sales` |

The two omitted table rows are the opening/sale value footer and the purchase/closing value footer. They are not products. Transition: FAIL → PASS.

### 0000700238 (frozen product-wise check)

| | |
|---|---|
| BEFORE | FAIL. Method `product_wise_stock_statement_pdf`. Profile generic. 50 rows. Two identity failures. |
| AFTER | Same method, same profile, 50 rows, same two failures. |
| WHY | No product-wise or generic-equation change. |
| SOURCE / PARSER | DIAREX TABLETS: receipts 48, sales 50, receipts_free 2, closing 0. LIV.52 TABLETS: receipts 200, sales 172, issues_free 2, closing 26. Free quantities remain on `extra`. |
| PROFILE | generic |
| EQUATION | `closing=opening+receipts-sales` |

Transition: FAIL → STILL FAIL.

## 7. PASS → PASS

16 local Phase 3 PASS documents stayed valid with the same row counts:

- Product-wise PDF: `0000700224` (98), `0000700288` (80)
- Marg XLS: `0000701808` (11), `0000701845` (56), `0000709799` (19), `0000723050` (55), `0000729823` (46)
- Spreadsheet PASS: `0000702233` (77), `0000703919` (77), `0000704386` (77), `0000715858` (77), `0000719211` (77), `0000732488` (71), `0000734357` (71), `0000735303` (24)
- Word PASS: `0000732713` (53), still `himalaya_wellness_dump_docx`

Two further PASS spreadsheets stayed valid with the same row counts. Their recorded method was `generic` and the current totals method field is empty, which is how this semantic spreadsheet path already reports itself: `0000728510` (54) and `0000730907` (119). They did not gain `stock_out_qty`.

No PASS PDF text contained the Op / Pur / ORec / Sale / OIss / Cl header, so the new PDF parser did not claim a previous PASS PDF.

`0000701432` was a Gemini PASS (42 rows). The Profitmaker reader returns None for it. With Gemini blocked, the older Word fallback is invalid. That fallback is not this change. The file was not counted as PASS → WRONG.

Vision and Gemini PASS documents were not replayed, because a provider call is not evidence about these parser changes.

## 8. PASS → WRONG

0

## 9. FAIL → PASS

- `0000700017`
- `0000734248`
- `0000701555`

## 10. FAIL → STILL FAIL

- `0000728682` (sale-return rows pass; VASAKA `Other_Qty` -27 remains unresolved)
- `0000700238` (product-wise free columns, frozen)

ERROR → PASS: 0
ERROR → STILL ERROR: 0

The full set of 2,774 documents was not replayed.

## 11. Remaining unresolved cases

- `Other_Qty` on `0000728682`. It is a signed adjustment. It is stored on `extra.other_qty`. No existing profile can take it without treating it as stock-out or sale-return. VASAKA stays an identity failure on purpose.
- Product-wise free and goods columns. One product-wise PDF is not enough evidence to change that equation.
- The 26 Phase 3.2 UNKNOWN vision documents. They do not share these four headers.
- The table-row-0 Profitmaker fixture file is absent here, so that historical fixture was skipped rather than executed.

## 12. Product-wise status

Frozen. Parser unchanged. Free quantities stay on extra fields. `0000700238` is still FAIL with the same two rows. Two product-wise PASS documents stayed PASS.

## 13. Generic equation status

Frozen. `generic_expected_closing` was not modified. The Op / Pur / ORec / Sale / OIss / Cl identity is a separate profile and returns before the generic equation.

## 14. OCR status

Frozen. Tesseract was not replaced. PaddleOCR was not installed. `0000700017` is read from digital text and does not go through OCR.

## 15. Phase 2 status

Frozen. Recovery stayed disabled for the replay. `recovery_projection.py` was not modified.

## 16. Regression risks

- A spreadsheet that already has a `Sale_Ret_Qty` column now selects saleret for the whole sheet. Rows with a zero return still match opening + receipts - sales when there is no expiry or damage quantity. The replayed PASS spreadsheets stayed valid.
- The Marg break is the purchase-detail section, not `TOTAL`. PASS Marg files kept their previous row counts.
- The Profitmaker paragraph rule looks at the paragraph above the table and at most one company line. An unrelated paragraph blocks the claim. `0000701432` is not claimed.
- The missing table-row-0 fixture was not executed in this environment.

## 17. Recommendation for Phase 3.4

Keep these four fixes. Do not rerun all 2,774 documents yet.

Investigate, without changing the generic equation, OCR, or Phase 2:

1. A signed representation for `Other_Qty`, only if more than this one workbook shows the same meaning.
2. Product-wise free, goods, and short-expiry columns across multiple product-wise documents. Do not fold free into receipts or sales from `0000700238` alone.
3. The 26 UNKNOWN vision documents, one layout group at a time, from printed headers rather than from the generic equation.

FINAL DECISION: SAFE_TO_CONTINUE_WITH_INVESTIGATION
