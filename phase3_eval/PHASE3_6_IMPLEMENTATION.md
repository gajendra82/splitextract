# Phase 3.6 — Product-wise Issues boundary fix

Final decision: SAFE_TO_CONTINUE

PASS → WRONG = 0

## 1. Root cause

`_parse_product_wise_stock_statement` cut the Issues band at the midpoint between the Issues header (ends at 310.6) and the Closing header (starts at 323.9). That midpoint is 317.2.

On `0000700283` and `0000700398` the printed order is Opening, Receipt, Issues, Closing, Sh.Exp, Liquidation. Every Issues quantity is right-aligned at x1 319.4, two points past that midpoint, so it was stored in `extra.sh_exp` and `sales_qty` stayed 0. Closing quantities at x1 367.4 were already in `closing_qty`. The Sh.Exp header (x 369.6–398.6) has no quantity. Liquidation values such as `567 Days` are informational.

`issues_expected_closing` was already `opening + receipt - issues`. With `sales_qty` at 0, the gap matched the mis-filed Issues number.

## 2. Exact production change

The change is local to `_pws_header_bounds` and the nested `_assign` in `_parse_product_wise_stock_statement`.

When the Sh.Exp header starts at or to the right of the Closing header:

- Issues extends to the Closing header's left edge, so a right-aligned Issues digit that has passed the Issues header and is still left of Closing stays in Issues.
- Closing runs up to the Sh.Exp header's left edge, so a closing digit at x1 367.4 stays `closing_qty`.
- A quantity whose right edge falls under the Sh.Exp header, and left of Liquidation, is `sh_exp`.
- Tokens under the Liquidation header, through the start of Sales, are skipped. `567 Days` does not become `sales_qty` or `sh_exp`.

When the Sh.Exp header is absent or sits left of Closing, the previous midpoint bands remain.

No filename, product name, or the literal 319.4 is hard-coded. `issues_expected_closing` is unchanged. `sh_exp` is not subtracted and is not mapped to `stock_out_qty`. An empty Sh.Exp column still stores 0 through the existing `_qty` helper.

## 3. Files changed

- `services/sales_statement_extractor.py` — `_pws_header_bounds` and `_assign` only
- `tests/test_phase3_6_product_wise_issues_boundary.py` — new regression tests
- `phase3_eval/PHASE3_6_IMPLEMENTATION.md` — this report

## 4. Tests added

`tests/test_phase3_6_product_wise_issues_boundary.py`

1. Synthetic header geometry: Issues digit just past the Issues header becomes `sales_qty`; a row with no Issues digit stays `sales_qty` 0; closing at x1 367.4 stays `closing_qty`; `567 Days` stays out of `sales_qty` and `sh_exp`; a quantity under the Sh.Exp header is `sh_exp` and is not subtracted.
2. `0000700283`: CONFIDO TABS 66 / 3 / 63 and LIV-52 SYP 200ML 42 + 70 − 69 = 43, with `sh_exp` 0. Every row passes the existing equation.
3. `0000700398`: HIMCOCID SUSP (MINT) 23 − 6 = 17 and LIV 52 TAB 1 + 30 − 1 = 30, with `sh_exp` 0. Every row passes.
4. `0000700238` is not claimed by this parser. The free-column PDF parser still keeps `receipts_free` 2 and `issues_free_repl` 2 on `extra`, and the document stays invalid.

## 5. Test results

```
124 passed, 5 skipped
```

Modules: `test_phase3_6_product_wise_issues_boundary`, `test_phase1_architecture`, `test_phase2_recovery`, `test_phase2b_recovery_contract`, `test_stock_identity_profiles`, `test_column_mapping`, `test_phase3_3_layout_fixes`, `test_asha_product_wise_box_column`, `test_bindal_product_wise_stock_statement`, `test_product_wise_stock_sale`.

Skipped because the fixture file is not on this machine:

- Asha Traders live PDF
- three Bindal product-wise PDF tests
- PRODUCT WISE STOCK & SALE fixture

The Asha detector test, which does not need the PDF, passed. It still requires the Sales amount header, and the qty-only detector still refuses that header.

Replay used `PHASE2_RECOVERY_ENABLED=false`. Gemini was blocked and was not called.

## 6. 0000700283 before / after

23 rows. Method `product_wise_stock_statement`. Profile `product_wise`. Formula remains `opening + receipt - issues = closing`.

| Product | Before | After |
|---|---|---|
| CONFIDO TABS | opening 66, sales 0, sh_exp 3, closing 63 | opening 66, sales 3, sh_exp 0, closing 63 |
| LIV-52 SYP 200ML | opening 42, receipts 70, sales 0, sh_exp 69, closing 43 | opening 42, receipts 70, sales 69, sh_exp 0, closing 43 |

Document validation: previously 9 identity failures. After the fix, calculated closing 385 equals extracted closing 385, `is_valid` true, `sh_exp` nonzero rows 0. CONFIDO sales amount stays 550.15.

## 7. 0000700398 before / after

36 rows. Same parser, profile, and formula.

| Product | Before | After |
|---|---|---|
| HIMCOCID SUSP (MINT) | opening 23, sales 0, sh_exp 6, closing 17 | opening 23, sales 6, sh_exp 0, closing 17 |
| LIV 52 TAB | opening 1, receipts 30, sales 0, sh_exp 1, closing 30 | opening 1, receipts 30, sales 1, sh_exp 0, closing 30 |
| HIMCOCID SUSP (BANANA) | no Issues digit | opening 11, sales 0, sh_exp 0, closing 11 |

Document validation: previously 6 identity failures. After the fix, calculated closing 117 equals extracted closing 117, `is_valid` true, `sh_exp` nonzero rows 0.

## 8. 0000700238 regression check

Unchanged. This parser does not claim the file. Extraction method remains `product_wise_stock_statement_pdf`. 50 rows. `is_valid` false. The same two rows still fail:

- DIAREX TABLETS: receipts 48, sales 50, `receipts_free` 2, closing 0, expected −2
- LIV.52 TABLETS: receipts 200, sales 172, `issues_free_repl` 2, closing 26, expected 28

Free quantities stay on `extra`. The equation was not changed to make this document pass.

## 9. Previously passing product-wise regression check

The Phase 3 sample has two passing documents whose recorded method is `product_wise_stock_statement`:

| Document | This parser claims it | Rows | Validation |
|---|---|---|---|
| 0000700224 | no | 98, same as the stored PASS | `is_valid` true, 0 failures |
| 0000700288 | no | 80, same as the stored PASS | `is_valid` true, 0 failures |

They still go through the qty-only product-wise reader. Their row counts and acceptance are unchanged.

## 10. PASS → WRONG count

0

No previously passing document became invalid. No row count changed on `0000700224` or `0000700288`. `0000700238` stayed a failure with the same free-column values. Closing digits were not moved into Issues. Liquidation days were not moved into `sales_qty` or `sh_exp`. `sh_exp` is 0 on the corrected rows and is not part of the expected closing (CONFIDO expected closing is 63, which is 66 − 3).

## 11. Final decision

SAFE_TO_CONTINUE
