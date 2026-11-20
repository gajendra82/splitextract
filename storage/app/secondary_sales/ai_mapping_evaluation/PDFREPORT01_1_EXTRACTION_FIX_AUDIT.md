# PDFREPORT01_1_EXTRACTION_FIX_AUDIT

**Date:** 2026-11-18  
**PDF:** `/var/www/html/splitextract/PDFReport01 (1).pdf`  
**Scope:** COMPANY STOCK STATEMENT (SR) — OP_QNTY / RCV / ISUUE / RQNTY / BAL pipe table  
**Independence:** Not Saleable (`rad7B368`) and not Op.Qnt Particular (`Stock_Report (2).PDF`).

## 1. Original PDF characteristics

- Digitally generated text PDF (Haru Free PDF Library; **0** images; selectable words).
- A4 portrait `595.3 × 841.9` pt.
- Stockist **KESHRI DRUG AGENCY**, Jamuna Chowk Bukar; GST `10ANEPK2975R2ZL`.
- Title **COMPANY STOCK STATEMENT ( SR )**; company **THE HIMALAYA ZANDRA**.
- Period `FROM 01/10/2026 TO 31/10/2026`.
- Pipe-ruled product rows: `PRODUCT | UNIT | OP_QNTY | OP-AMT | RCV-QNT | RCV-AMT | ISUUE-Q | ISSUE-AMT | RQNTY | RTN-AMT | BAL-QNT | BAL-AMT`.
- Footer value total line only (no qty footer): OP/RCV/ISSUE/RTN/BAL amounts.

## 2. Page count and layout observations

- **2** pages (page 2 continues with SPEMEN + TOTAL).
- Product names often include size (`H BONNISAN 100ML`) while UNIT holds `1X100M`.
- Printed typo **ISUUE-Q** (issue qty).
- All-zero movement rows are valid SKUs (e.g. FLORASANTE CAP).
- pdfplumber emits `(cid:10)` glyph placeholders before some product tokens; PyMuPDF does not.

## 3. Root cause of incorrect extraction

Before the fix, native `pdf_words_free_columns` **failed header resolution** and the pipeline fell through to **`tesseract_heuristic` OCR**, which misread pack sizes as quantities (e.g. BONNISAN opening=100).

Native failure chain:

1. **`STOCK_PDF_HEADER_UNGLUE`** split `OP_QNTY`→`OP|_QNTY`, `OP-AMT`→`OP|-AMT`, `ISSUE-AMT`→`ISS|UE-AMT`, `BAL-AMT`→`BAL|-AMT`.
2. Missing aliases for `RCV-QNT`, `RCV-AMT`, `ISUUE-Q`, `RQNTY` left purchase/issue/return as **ignore**.
3. `_items_from_resolved_word_columns` requires opening+purchase+sales+closing → returned **None**.
4. Secondary issues once headers worked: all-zero rows treated as pending name wraps; `(cid:10)` polluted names on the plumber path.

## 4. Before-and-after extraction examples

| Field / row | Before (`tesseract_heuristic`) | After (`pdf_words_free_columns`) |
|---|---|---|
| Method | tesseract_heuristic | pdf_words_free_columns |
| Rows | 36 (garbage + letterhead) | **54** (all products) |
| H BONNISAN 100ML | opening 100, closing 9051.85 | opening **151**, sales **12**, return **1**, closing **140**, pack **1X100M** |
| First “products” | GST NO…, FROM, dates | Real SKUs only |
| Opening value sum | ~0 | **332847.26** ≈ footer 332847.25 |

## 5. Existing parser selected before the fix

- Attempted native word columns → failed → **`tesseract_heuristic`** on rendered pages.
- Not saleable_stock_report; not Op.Qnt Particular path.

## 6. Targeted correction implemented

Generic header/native fixes only (no filename route):

1. **Aliases / direct values** in `stock_header_resolver` for OP_QNTY, OP-AMT, RCV-*, ISUUE-Q, ISSUE-AMT, RQNTY, BAL-*.
2. **`STOCK_PDF_KEEP_DELIMITED_MOVEMENT_HEADERS`** (default ON) — do not unglue `OP_QNTY` / `OP-AMT` / `ISSUE-AMT` / `BAL-AMT`-style tokens; still unglue `QtyLM` / `FreeSales`.
3. Strip pdfplumber **`(cid:N)`** in `decode_pdf_pseudo_digits`.
4. Pending-name wrap requires **truly missing** movement cells — printed `0` / `0.00` stays a product row.
5. Mirror `sale_return` onto the top-level line_item when mapped from `sales_return_qty`.

## 7. Files modified

- `services/stock_header_resolver.py`
- `services/stock_native_tables.py`
- `services/stock_native_resolver.py`
- `tests/test_company_stock_statement_sr_pdf.py`
- `storage/app/secondary_sales/ai_mapping_evaluation/PDFREPORT01_1_EXTRACTION_FIX_AUDIT.md`

## 8. Reason the fix is isolated

- Unglue guard only matches delimited `PREFIX[-_](QNTY|QNT|AMT|…)`.
- New aliases are additive for these header spellings.
- Pending change only stops treating printed zeros as wrap fragments.
- Kill switch: `STOCK_PDF_KEEP_DELIMITED_MOVEMENT_HEADERS=false`.
- No per-stockist / filename detector.

## 9. Extracted row count

**54** / 54 printed product lines.

## 10. Field-level validation

- Stockist KESHRI DRUG AGENCY; period 2026-10-01 → 2026-10-31.
- Sample BONNISAN 100ML / SPEMEN TAB match PDF qty, pack, values; RQNTY → sale_return.
- RTN-AMT has no dedicated value field (ignored); return **qty** captured.
- No `(cid:)` names; no multi-SKU merges.

## 11. Total reconciliation

| Metric | Extracted | Printed footer |
|---|---:|---:|
| Opening value | 332847.26 | 332847.25 |
| Purchase value | 116737.69 | 116737.70 |
| Issue (sales) value | 135122.39 | 135122.40 |
| Closing value | 315153.85 | 315153.85 |

Sub-cent drift only; values not rewritten to force match.

## 12. Existing-format regression results

- `tests.test_company_stock_statement_sr_pdf` — run with suite below
- `tests.test_stock_report_op_qnt_brand_pending` — Op.Qnt Stock_Report (2) path
- `tests.test_op_qnt_value_expiry_pdf_words`
- `tests.test_saleable_stock_report`

(See test run output in task completion notes.)

## 13. Same-format regression results

- Primary: `PDFReport01 (1).pdf` → 54 rows, footer values match.
- No second SR sample on server (limitation).

## 14. Downstream compatibility results

- Method `pdf_words_free_columns`; line_item keys unchanged.
- No `/split-and-extract` / `app.py` contract changes.
- No production restart performed.

## 15. Remaining limitations

- RTN-AMT (return value) not mapped to a first-class field.
- Product-name size tokens (`100ML`) may still be split then rejoined via dead-zone/product; UNIT remains authoritative packing.
- Live process must be restarted under approval to pick up the fix.
