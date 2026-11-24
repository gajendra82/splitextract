# Phase 3.5 — Product-wise Sh.Exp investigation

SH_EXP_DECISION: ALREADY_REPRESENTED_BUT_MISPARSED

The closing gap equals `extra.sh_exp` because the Issues quantity was written into that field. The printed Sh.Exp column on these two files has no quantity. The product-wise equation should not start subtracting `sh_exp`.

No production code was changed.

## 1. Exact root cause

`_parse_product_wise_stock_statement` splits Issues and Sh.Exp at `iss_right = 317.2`, the midpoint between the Issues header (ends at 310.6) and the Closing header (starts at 323.9).

Every Issues digit on these files is right-aligned at `x1 = 319.4`. That is 2 points past the midpoint, so the parser stores it as `extra.sh_exp` and leaves `sales_qty` at 0. Closing digits are right-aligned at `x1 = 367.4` and are stored correctly as `closing_qty`.

`issues_expected_closing` then computes `opening + receipt - sales_qty`. With `sales_qty` still 0, the gap equals the mis-filed Issues number. All 23 rows of `0000700283` and all 36 rows of `0000700398` show that equality. No row has both a nonzero `sales_qty` and a nonzero `sh_exp`.

The printed header order, left to right, is:

Product Name, Packing, Opening, Receipt, Issues, Closing, Sh.Exp, Liquidation, Sales Amount, Closing Amount.

Sh.Exp the header is at x 369.6–398.6. No quantity token sits in that span. The next token is Liquidation, a day count such as `567 Days`, under x 405–445. It is not a stock movement.

This is not option A (Sh.Exp dropped), C (a missing alias), D (the profile wrongly ignoring a real short-expiry movement), or E proven from a filled Sh.Exp column. The profile ignores `extra.sh_exp`, and on these files that field is not short expiry.

## 2. Evidence from 0000700283

Digital PDF. Method and layout `product_wise_stock_statement`. Profile `product_wise`. Formula executed: `opening + receipt - issues = closing`. OCR was not used.

| Product | Printed position | Parser |
|---|---|---|
| CONFIDO TABS | Opening 66 at x1 228.6; Issues 3 at x1 319.4; Closing 63 at x1 367.4; Liquidation 567 Days | opening 66, receipts 0, sales 0, sh_exp 3, closing 63. Expected 66. `66 - 3 = 63` once 3 is Issues. |
| HAIR ZONE SOLN | Opening 32; Issues 3 at x1 319.4; Closing 29 | opening 32, sales 0, sh_exp 3, closing 29. `32 - 3 = 29`. |
| LIV-52 SYP 200ML | Opening 42; Receipt 70 at x1 274.4; Issues 69 at x1 319.4; Closing 43 | opening 42, receipts 70, sales 0, sh_exp 69, closing 43. Expected 112. `42 + 70 - 69 = 43`. |

9 of 23 rows fail. Those 9 are exactly the rows whose Issues digit was stored as `sh_exp`.

## 3. Evidence from 0000700398

Same header coordinates, same parser, same profile, same band.

| Product | Parser | If the stored sh_exp is Issues |
|---|---|---|
| HIMCOCID SUSP. (MINT) | opening 23, sales 0, sh_exp 6, closing 17 | `23 - 6 = 17`. The 6 is the token at x1 319.4. |
| LIV 52 SYR | opening 23, sales 0, sh_exp 23, closing 0 | `23 - 23 = 0` |
| PILEX FORTE TAB | opening 20, sales 0, sh_exp 5, closing 15 | `20 - 5 = 15` |
| LIV 52 TAB | opening 1, receipts 30, sales 0, sh_exp 1, closing 30 | `1 + 30 - 1 = 30` |

6 of 36 rows fail. Same pattern. Confidence is high for both files.

## 4. Whether Sh.Exp is a stock movement

Not on these two documents. The column header is printed, and the cells under it are empty. Liquidation days are a separate informational column. There is no filled Sh.Exp quantity here from which to judge a sign.

## 5. Correct sign if applicable

Do not subtract `extra.sh_exp`. The number currently in that field is an outflow only because it is the Issues quantity. Its sign belongs on `sales_qty`: `opening + receipt - issues = closing`. A later real Sh.Exp value, if one appears under x 370–399, is not evidenced by these files.

## 6. Whether 0000700238 is related or separate

Separate.

`0000700238` uses `_parse_product_wise_stock_statement_pdf`, profile generic. Its failing rows keep `receipts_free` or `issues_free_repl` on `extra`, and `sh_exp` is null. The headers include free and goods columns. It is not this Sales-amount layout, and it is not this Issues-band bug.

The two previously passing product-wise files `0000700224` and `0000700288` also do not use `_parse_product_wise_stock_statement`. Their text has free/goods columns and no Sales Amount header, so this Sh.Exp band is not their parser.

## 7. Smallest production change, if a later phase implements it

Do not add `sh_exp` to `issues_expected_closing`. Do not map Sh.Exp to `stock_out_qty`.

In `_pws_header_bounds` / `_assign`, keep a right-aligned Issues digit at `x1 ≈ 319` in the Issues band. The current midpoint (317.2) is left of that edge. Closing digits at `x1 ≈ 367` must stay in Closing. Leave an empty Sh.Exp header unmapped until a file actually has a quantity under x 370–399.

That change was not made in this phase.

## 8. Regression risk

Subtracting `extra.sh_exp` would make these 15 rows pass for the wrong reason. If Issues were later moved into `sales_qty` as well, the same quantity would be subtracted twice.

A boundary-only change is limited to `_parse_product_wise_stock_statement`. It does not touch the generic equation, the free-column PDF parser, Phase 2, or OCR. The Asha Traders fixture (`tests/test_asha_product_wise_box_column.py`) uses this parser and expects every row to satisfy `opening + receipt - issues = closing`. That fixture file is not on this machine, so it was not re-run. Any later boundary edit has to pass that test before it is kept.

## 9. Tests required before implementation

- These two PDFs: Issues digits at x1 319.4 become `sales_qty`, `extra.sh_exp` stays 0 or empty, and `66 - 3 = 63`, `42 + 70 - 69 = 43`, `23 - 6 = 17`, `1 + 30 - 1 = 30`.
- A row with no Issues digit still has `sales_qty` 0 and still balances.
- Closing at x1 367.4 is not pulled into Issues or Sh.Exp.
- Liquidation `N Days` does not become `sales_qty` or `sh_exp`.
- `0000700238` free columns stay on `extra` and the document stays unresolved.
- The Asha fixture, when present, still passes with Box not read as Opening.
- Existing product-wise profile tests still expect `issues_expected_closing` to ignore `sh_exp`.

SH_EXP_DECISION: ALREADY_REPRESENTED_BUT_MISPARSED
