# Phase 3.10 — Remaining failure reassessment

PHASE 3.10 DECISION: TARGETED_INVESTIGATION

No production code was changed. The full dataset was not run. Gemini was not called. Phase 2 stayed off. OCR was not changed. Closed clusters were not reopened.

## Current remaining failure count

31 of the original 37 `STOCK_IDENTITY_FAILURE` documents are still open.

Fully closed (do not reopen):

| Document | Closed in |
|---|---|
| 0000700017 | Phase 3.3 |
| 0000734248 | Phase 3.3 |
| 0000701555 | Phase 3.3 |
| 0000700283 | Phase 3.6 |
| 0000700398 | Phase 3.6 |
| 0000700325 | Phase 3.8 (document valid) |

Partially fixed, still open:

| Document | Status |
|---|---|
| 0000700259 | Phase 3.8 fixed the free/goods fold; Phase 3.9 left 3 `OCR_ERROR` rows. Do not change Tesseract. Do not reconstruct `3752` / `3337`. Optional `«18` cleanup deferred. |

The baseline also had 4 `PROVIDER_ERROR` and 3 `EMPTY_OUTPUT` fails. Those are unchanged and are not one identity-layout cluster.

An image or OCR-required flag alone is not `OCR_ERROR`. Only `0000700259`’s three leftover rows are evidence-backed OCR errors among the remaining identity set.

## Top 3 evidence-backed clusters

1. **Product-wise PDF free columns — 1 document** (`0000700238`)  
   Class: mapping. Parser `product_wise_stock_statement_pdf`, profile generic. Failing-row gaps equal `receipts_free` / `issues_free_repl` stored on `extra`. Digital source compared in Phase 3.1 / 3.2. Not the photo fold. Not Issues/Sh.Exp.

2. **Closstock one-row digital miss — 1 document** (`0000700026`)  
   Class: parser. Method `psr_closstock_columns`, profile saleret. 24 of 25 rows pass. One row is off by 3 with sale_return and exp_damage both 0. Source-compared; no missing adjustment column found yet.

3. **Other_Qty after Sale_Ret fix — 1 document** (`0000728682`)  
   Class: mapping. Spreadsheet now reaches saleret for `Sale_Ret_Qty`. VASAKA still fails on `Other_Qty` −27. Do not invent a global Other_Qty → stock_out alias.

Larger remaining groups (`zandra_stock_sale_vision` 4, `gemini_extraction_fallback` 4, Swil / split-page / pack-op vision 3 each) stay **unknown**. Same method label is not enough. `0000700084` shows page and parser agreeing on four quantities that do not close; that is not a proven reusable equation or mapping fix for the other three Zandra files.

## ONE recommended next cluster

Product-wise PDF free columns: `0000700238`.

## Exact documents

- `0000700238_2026_08_ZL_18_377_03092026154026.pdf`

## Why they belong together

Single-document cluster, but a concrete layout mechanism: the Bindal/Himalaya digital product-wise PDF parser keeps Free / free-repl on `extra` and leaves canonical `receipts_qty` / `sales_qty` without them. Identity (generic or issues-style) then misses exactly those amounts. It is the opposite of the photo reader’s old fold, and it is separate from `0000700259` OCR leftovers.

## Evidence supporting the common cause

Phase 3.1 / 3.2 / 3.4 / 3.8 all reconfirm the same two failing rows:

| Product | Canonical | Extra | Gap |
|---|---|---|---|
| DIAREX TABLETS | receipts 48, sales 50, closing 0 | receipts_free 2 | +2 |
| LIV.52 TABLETS | receipts 200, sales 172, closing 26 | issues_free_repl 2 | −2 |

Selecting `product_wise` would not close the gap by itself: `opening + receipts - sales` has the same miss. Method `product_wise_stock_statement_pdf` is on the known-generic list, so profile stays generic. That is a secondary selection mismatch, not the first numeric error.

## Smallest likely safe fix (investigation first — do not implement here)

Do not copy the Phase 3.8 photo “fold when needed” logic blindly onto this PDF.

Phase 3.11 should decide, from the printed form only, whether Free / free-repl are:

- period movements that belong in canonical qty or in the product-wise equation, or
- informational beside Opening / Receipt Qty / Issues Qty (like Jul on the photo layout).

Only after that semantic call: either a local PDF-parser mapping change, a product-wise free-aware profile for this method, or leave the document correctly unresolved. Do not change `generic_expected_closing`. Do not touch Tesseract, Phase 2, or the photo fold.

## Regression risk

Medium.

Bindal / product-wise PDF tests and any row that already balances with free ignored must stay valid. PASS → WRONG must stay 0. Prefer a safe unresolved FAIL over forcing free into the wrong side of the equation.

## Explicitly not chosen next

| Cluster | Why not |
|---|---|
| zandra_stock_sale_vision (4) | Largest named method, but mechanism unknown; one inspected page does not close on its printed four columns |
| swil / pack / split / gemini vision | Count without a shared proven cause |
| 0000700259 OCR leftovers | Confirmed OCR_ERROR; parked |
| 0000701789 order form | Correct fail-closed profile |
| 0000700026 / 0000728682 | Strong evidence, but one-off and less reusable than the free-column PDF question |

PHASE 3.10 DECISION: TARGETED_INVESTIGATION
