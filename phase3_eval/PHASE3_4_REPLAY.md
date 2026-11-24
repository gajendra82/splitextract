# Phase 3.4 — Fast controlled replay

PHASE 3.4 DECISION: TARGETED_INVESTIGATION

The Phase 3.3 fixes are safe on the documents that were re-extracted. PASS → WRONG is 0. No production code was changed. The next investigation target is the digital product-wise statement whose closing gap equals `sh_exp`.

## 1. Replay scope

Stored Phase 3 baseline: `phase3_eval/records` and `identity_failure_manifest.json` (the original 37 `STOCK_IDENTITY_FAILURE` documents). Root causes are the Phase 3.1 file `identity_failure_root_causes.json`. Current results were compared with that baseline. This rerun is not a new baseline.

`PHASE2_RECOVERY_ENABLED=false`. Gemini was blocked. Image and OCR-required documents were not re-read.

Scope:

- The exact 37 identity-failure documents.
- 19 previously PASS local documents from the same representative set whose parsers overlap the four fixes (Marg, product-wise, generic spreadsheets, Word). This is the regression check. It is not a new sample and it is not the full 100.

## 2. Number of documents replayed

| Group | Count |
|---|---|
| Original 37 in scope | 37 |
| Re-extracted locally from those 37 | 9 |
| Not remeasured (OCR or image; provider path left as stored) | 28 |
| Previously PASS documents checked | 19 |
| PASS documents re-extracted and still valid | 18 |
| PASS document left on its Gemini path (`0000701432`) | 1 |
| Full dataset of 2,774 | not run |

## 3. Previous vs current result counts

Original 37, against the stored baseline:

| Comparison | Count | Meaning |
|---|---|---|
| FAIL → PASS | 3 | Re-extracted and now valid |
| FAIL → FAIL | 6 | Re-extracted and still invalid |
| FAIL → FAIL, not remeasured | 28 | Stored FAIL stands; OCR or Gemini was not run |
| PASS → PASS | 19 | 18 re-extracted, 1 provider path unchanged |
| PASS → WRONG | 0 | |
| PASS → FAIL | 0 | |
| ERROR → PASS | 0 | |
| ERROR → ERROR | 0 | |
| ERROR → FAIL | 0 | |

## 4. PASS → WRONG count

0

## 5. FAIL → PASS count

3

- `0000700017` Op / Pur / ORec / Sale / OIss / Cl
- `0000734248` Marg stock table stops before PURCHASE DETAIL
- `0000701555` Profitmaker paragraph header

## 6. FAIL → FAIL count

6 re-extracted documents are still invalid: `0000700026`, `0000700238`, `0000700283`, `0000700379`, `0000700398`, `0000728682`.

28 more remain the stored FAIL because they were not remeasured.

## 7. ERROR → PASS count

0

## 8. ERROR → ERROR count

0

## 9. Four Phase 3.3 fix results

### Op / Pur / ORec / Sale / OIss / Cl — `0000700017`

FAIL → PASS. Method changed from `pdf_text_gemini` to `op_pur_orec_sale_oiss`. Profile is `op_pur_orec_sale_oiss`. Formula is `closing=opening+purchase+other_receipt-sale-other_issue`. The generic equation is not the formula on this document. 49 rows, 0 failures.

AACTARIL SOAP: opening 40, purchase 0, other_receipt 2, sale 11, other_issue 0, closing 31. Expected closing 31.

### Sale_Ret_Qty — `0000728682`

FAIL → FAIL at document level. Profile changed from generic to `saleret`. Row count stays 52. Formula is `closing=total-sale+sale_return-exp_damage`.

HERBOLAX TABS: 47, purchase 0, sale 4, sale_return 1, other_qty 0, closing 44. Valid.

LIV-52 SYRUP: 82 + 105 - 77 + sale_return 2 = 112. Valid.

VASAKA SYRUP: sale_return 0, other_qty -27, stock_out_qty unset, closing 28, expected 55. Invalid. `Other_Qty` was not treated as `Sale_Ret_Qty`.

### Marg PURCHASE DETAIL — `0000734248`

FAIL → PASS. Method stays `marg_erp_xls_receive_issue_value`. Profile stays generic. Row count changed from 35 to 32. Those 32 stock rows are valid. AACTARIL SOAP is opening 40, receipts 0, sales 0, closing 40. Invoice `5126107167` is not present in the extracted rows.

Previously PASS Marg workbooks in the representative set kept their row counts and stayed valid: `0000701808` (11), `0000701845` (56), `0000709799` (19), `0000723050` (55), `0000729823` (46).

### Profitmaker paragraph header — `0000701555`

FAIL → PASS. Method changed from generic to `profitmaker_ostk_docx`. Profile stays generic because Qoh matches opening + receipts - sales. 68 product rows, 0 failures.

ABANA TABLETS: opening 7, receipts 0, sales 2, closing 5, closing_value 756.80, sales_value 0. No sales scheme.

## 10. `0000700238` status

FAIL → FAIL. Unchanged. Method `product_wise_stock_statement_pdf`. Profile generic. Formula `closing=opening+receipts-sales`. 50 rows, 2 failures.

DIAREX TABLETS: receipts 48, sales 50, receipts_free 2, closing 0. Free is still on `extra`, not in `receipts_qty`.

LIV.52 TABLETS: receipts 200, sales 172, issues_free_repl 2, closing 26. Expected closing without the free column is 28.

This remains an unresolved product-wise free-field case. It was not fixed.

Two previously PASS product-wise statements stayed PASS: `0000700224` (98 rows) and `0000700288` (80 rows).

## 11. Remaining failure clusters

| Cluster | Documents | Classification |
|---|---|---|
| OCR or vision, not remeasured | 28 | UNKNOWN as a group. They do not share one parser. Largest stored methods are `zandra_stock_sale_vision` (4) and `gemini_extraction_fallback` (4). |
| Product-wise `sh_exp` | `0000700283` (9 failing rows), `0000700398` (6 failing rows) | IDENTITY_EQUATION_ERROR. Same parser `product_wise_stock_statement`, same profile `product_wise`. |
| Product-wise free columns | `0000700238` | PRODUCT_WISE_FREE_FIELDS. Different method and different missing field from `sh_exp`. |
| `Other_Qty` after sale-return mapping | `0000728682` | SALE_RETURN_OTHER_QTY. One row. |
| One saleret row still short | `0000700026` | PARSER_EXTRACTION_ERROR. LIV 52 SYP 200 ML expected 100, closing 97, sale_return 0. Same gap as Phase 3.1. |
| One Swil row off by 1 | `0000700379` | UNKNOWN. Opening 17, sales 15, closing 3, expected 2. Parser and row count match the baseline. |

Two product-wise photos (`0000700259`, `0000700325`) stay in the not-remeasured set. Phase 3.1 already classed them as a free/goods fold. They were not replayed here, so that classification was not re-opened.

## 12. Largest reusable cluster

`product_wise_sh_exp`.

It is the largest remaining cluster that was re-extracted and shares one parser, one profile, and one missing field. CONFIDO TABS on `0000700283` is opening 66, receipts 0, sales 0, `sh_exp` 3, closing 63. The product-wise equation expects 66.

The 28 vision documents are a larger count, but they were not remeasured and they do not share one layout. They are not the next reusable target.

## 13. Recommended Phase 3.5 investigation target

Investigate only the digital `product_wise_stock_statement` documents whose closing gap equals `sh_exp`: `0000700283` and `0000700398`.

Do not fold `0000700238` free quantities into that change. Do not change the generic equation, OCR, or Phase 2. Do not start that investigation in this phase.

## 14. Tests executed

```
tests/test_phase3_3_layout_fixes.py
tests/test_phase1_architecture.py
tests/test_phase2_recovery.py
tests/test_phase2b_recovery_contract.py
tests/test_stock_identity_profiles.py
tests/test_column_mapping.py
```

Passed: 110. Failed: 0. Skipped: 0.

## 15. Production files changed

None.

Artifacts:

- `splitextract/phase3_eval/PHASE3_4_REPLAY.md`
- `splitextract/phase3_eval/phase3_4_replay_results.json`
- `splitextract/phase3_eval/phase3_4_failure_clusters.json`
- `splitextract/phase3_eval/phase3_4_changed_documents.json`

PHASE 3.4 DECISION: TARGETED_INVESTIGATION
