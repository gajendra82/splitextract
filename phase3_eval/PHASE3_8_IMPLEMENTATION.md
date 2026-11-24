# Phase 3.8 — Product-wise photo free/goods fold

Final decision: SAFE_TO_CONTINUE

PASS → WRONG = 0

## Root cause

`_parse_product_wise_stock_statement_photo_panel` always added Receipt Free, Issues Free, and Goods Issue into `receipts_qty` / `sales_qty`. On these photos the printed qty columns already satisfy `opening + receipt qty - issues qty = closing`, so the fold corrupted the canonical fields.

Two layout details made that worse:

1. When the header had no Free token, the reader invented a Free band between Receipt and Issues. On `0000700325` (Prev.Sales Jul/Jun, no Receipt Free) that magnet stole Issues digits into `receipts_free`, then folded them into receipts.
2. Right-aligned Issues digits often sit left of the Issues header centre. After Jul/Jun centres were added, those digits fell in a gap more than 90px from every column and were dropped.

`issues_expected_closing` was already correct. OCR was not replaced.

## Exact code change

Local to the product-wise photo reader only:

1. Detect Jul/Jun header tokens and keep those values on `extra` (`jul_qty` / `jun_qty`).
2. Stop inventing Free / sales_free / goods centres when those headers are absent.
3. If printed `opening + receipts - sales ≈ closing`, do not fold Free/Goods into canonical qty. Keep Free/Goods/Jul/Jun on `extra`.
4. If the qty columns do not close, keep the historical fold and `_pwss_photo_repair_row` path.
5. Claim the gap after Jul/Jun and before Closing for Issues only when the token would otherwise be dropped (so Jul/Jun on the Free/Jul/Jun layout are not stolen).

No change to the digital Issues/Sh.Exp parser, `0000700238`, generic identity, column-mapping aliases, Phase 2, or Tesseract.

## Why the change is safe

All nine previously PASS `product_wise_image` photos in the Phase 3 sample were re-extracted. None had Free/Goods rows that depended on the fold. All nine stayed valid with the same 35-row counts.

The fold is still used when the qty columns do not close on their own. Filenames and product names are not hard-coded.

## Before / after — 0000700259

Method `product_wise_stock_statement_image`, layout `product_wise_stock_statement_photo`, profile `product_wise_image`.

| Product | Before | After |
|---|---|---|
| PILEX FORTE OINT | receipts 130, sales 46, expected 94, fail | receipts 110, sales 20, closing 100, ok (`10+110-20=100`) |
| CLARINA ANTIACNE CREAM | receipts 2 (folded free), sales 8, expected 12, fail | receipts 0, sales 8, closing 10, ok |

Document: 14 identity failures → 3. Still invalid at document level. The three leftovers are OCR/glued-digit rows (PILEX TAB `sales_qty` 3337, one LIV SYP glue, TENTAX FORTE missing Issues). Tesseract was not redesigned.

On this photo OCR often misses the Free header word, so Free quantities may land in `extra.jun_qty`. They are no longer folded into `receipts_qty` / `sales_qty`.

## Before / after — 0000700325

| Product | Before | After |
|---|---|---|
| AACTARIL SOAP | receipts 21 (mis-filed Issues), sales 0, closing 51, fail | receipts 0, sales 21, closing 51, ok |
| CONFIDO TAB | receipts/free noise, sales 0, fail | sales 15, Jul/Jun on extra, ok |
| LIV SYP (200ml) | receipts folded/wrong, sales 0, fail | receipts 420, sales 380, closing 443, ok |

Document: 10 identity failures → 0. `is_valid` true. 44 rows.

## Regression — existing PASS photos

Re-extracted with Phase 2 off and Gemini blocked:

| Document | Rows | Validation |
|---|---|---|
| 0000700206 | 35 | valid |
| 0000700221 | 35 | valid |
| 0000700223 | 35 | valid |
| 0000700253 | 35 | valid |
| 0000700345 | 35 | valid |
| 0000700365 | 35 | valid |
| 0000700374 | 35 | valid |
| 0000700381 | 35 | valid |
| 0000700400 | 35 | valid |

PASS → WRONG = 0.

`0000700238` still uses `product_wise_stock_statement_pdf`, keeps `receipts_free` 2 on `extra`, and stays unresolved.

## Tests

`tests/test_phase3_8_product_wise_photo_fold.py`

- Synthetic: canonical balance keeps Free/Goods/Jul out of qty; fold retained when qty columns do not close.
- Live 0259: PILEX FORTE and CLARINA.
- Live 0325: document valid; AACTARIL and LIV SYP.
- Three representative PASS photos stay valid at 35 rows.
- 0238 free-column PDF unchanged.

Full run:

```
132 passed, 5 skipped
```

Modules: phase 3.8, bindal/asha/product-wise, phase 3.3, phase 3.6, stock identity, column mapping, Phase 1, Phase 2, Phase 2b. Skips are missing fixtures only.

## PASS → WRONG count

0

## Final decision

SAFE_TO_CONTINUE
