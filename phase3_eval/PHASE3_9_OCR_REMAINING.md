# Phase 3.9 — Remaining failures on 0000700259

PHASE 3.9 DECISION: OCR_ERROR

No production code was changed. Tesseract was not replaced. Gemini was not called. Phase 2 stayed off. The Phase 3.8 fold fix was not modified.

Document: `0000700259_2026_08_ZL_18_379_03092026174312.jpg`  
Method: `product_wise_stock_statement_image`  
Layout: `product_wise_stock_statement_photo`  
Failing rows after Phase 3.8: 3

## Summary

| Product | Printed Issues | Tesseract token | Parser sales_qty | Class |
|---|---|---|---|---|
| LIV 52 SYP 200ML | 52 (Jul 37 separate) | `3752` (one box) | 3752 | OCR_ERROR |
| PILEX TAB | 37 (Jul 33 separate) | `3337` (one box) | 3337 | OCR_ERROR |
| TENTAX FORTE | 18 | `«18` | 0 (token dropped) | OCR_ERROR (polluted string); parser also refuses non-pure digits |

Two rows are classic adjacent-column glue. The third is a garbage prefix on an otherwise correct Issues digit. Column geometry for Issues is not the primary break on these three rows.

## 1. LIV 52 SYP 200ML

Printed source (qty columns):

| Opening | Receipt Qty | Receipt Free | Jul | Issues Qty | Closing |
|---|---|---|---|---|---|
| 103 | (empty) | 43 | 37 | 52 | 51 |

Identity: `103 + 0 - 52 = 51`.

Raw Tesseract (production 2x contrast, PSM 6), numeric tokens on the row:

- `103` → opening
- `43` → nearest Jun/Free zone (`jun_qty` 43; Free header is not OCR'd on this page)
- `3752` → one token, x0=1003 … x1=1121, assigned to sales
- `51` → closing

The same Jul/Issues x-span on the previous LIV 52 SYP 100ML row is two tokens (`15` and `62`). On the 200ML row Tesseract merges Jul 37 and Issues 52 into `3752`.

Parser value: `sales_qty=3752`, `receipts_qty=0`, expected closing −3649.

Discrepancy: Issues should be 52; parser stores 3752.

- OCR produced the wrong string: yes.
- Parser could split it using only the stock equation: no. `_pwss_photo_split_glued` subtracts both halves; `103 - 37 - 52 = 14 ≠ 51`. Jul is informational and must not be subtracted.
- Column mapping: the glued box lands in Issues/sales; boundaries are not the cause.

Row class: OCR_ERROR.

## 2. PILEX TAB

Printed source:

| Opening | Receipt Qty | Receipt Free | Jul | Issues Qty | Closing |
|---|---|---|---|---|---|
| 29 | 100 | 30 | 33 | 37 | 92 |

Identity: `29 + 100 - 37 = 92`.

Raw Tesseract numeric tokens:

- `29` → opening
- `100` → receipts
- `30` → Free zone (`jun_qty` 30)
- `3337` → one token, x0=1003 … x1=1121, assigned to sales
- `92` → closing

Jul 33 and Issues 37 are glued into `3337` in the same x-span as the LIV `3752` glue.

Parser value: `sales_qty=3337`, `receipts_qty=100`, expected closing −3208.

Discrepancy: Issues should be 37; parser stores 3337.

- OCR produced the wrong string: yes.
- Existing glue repair cannot recover the right Issues-only value: splitting `33|37` and subtracting both yields `29+100-33-37=59 ≠ 92`. The needed value is Issues 37 alone.
- Column mapping did not invent 3337; Tesseract emitted it.

Row class: OCR_ERROR.

## 3. TENTAX FORTE

Printed source:

| Opening | Receipt Qty | Receipt Free | Jul | Issues Qty | Closing |
|---|---|---|---|---|---|
| 332 | (empty) | 26 | 50 | 18 | 314 |

Identity: `332 + 0 - 18 = 314`.

Raw Tesseract numeric/near-numeric tokens:

- `332` → opening
- `26` → Free zone
- `50` → Jul zone (second hit on jun band; first Free value is kept)
- `«18` → Issues/sales column (x0=1093 … x1=1121, `col=sales`)
- `314` → closing

Tesseract did not emit a clean `18`. It emitted a guillemet/garbage prefix plus the digits (`«18`, U+00AB).

Parser value: `sales_qty=0`. The photo panel only accepts pure numeric tokens:

`re.fullmatch(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", tok)`

`«18` fails that gate, so the Issues quantity never enters the bands even though nearest-column already says sales.

Discrepancy: Issues should be 18; parser stores 0.

- OCR produced a wrong/polluted string: yes.
- Parser could interpret the OCR output: yes, by stripping non-digits before the numeric gate; the digits `18` are present and the column is already Issues.
- Column boundaries: correct for this token; mapping is not the failure.

Row class: OCR_ERROR on the token, with a secondary parser intolerance of non-digit noise. Not a Free/Goods fold failure and not an identity-equation failure.

## Cross-checks

Header centres on this page (2x image): packing 494, opening 628, receipts 755, jun 974, sales/Issues 1098, closing 1441. Free and Jul headers are not detected; Free values sit in the jun band. That residual mapping noise does not create the three failures above.

Neighboring rows with the same layout and separated OCR tokens (e.g. PILEX FORTE OINT `20` / `26` / `20`, LIV SYP 100ML `15` / `62`) pass after Phase 3.8. The fold fix is not the cause of these three leftovers.

## Final decision

OCR_ERROR

Two of three rows fail because Tesseract glued Jul + Issues into one false quantity. The third fails because Tesseract prefixed the Issues digit and the parser then discarded the token.

## Smallest safe next step (do not implement in this phase)

Do not replace Tesseract. Do not install PaddleOCR. Do not reopen Phase 3.8.

If a later phase addresses these leftovers:

1. Prefer a minimal photo-parser cleanup that strips leading/trailing non-digit junk from an otherwise numeric Issues token (`«18` → `18`), after column assignment. That is local and does not change the OCR engine.
2. Do not “fix” `3752` / `3337` by subtracting both glued halves in the stock equation. Jul is not Issues. Any glue recovery would need layout-aware Jul vs Issues splitting with source proof, and is higher risk than (1).
3. Keep `opening + receipt - issues = closing` unchanged.

PHASE 3.9 DECISION: OCR_ERROR
