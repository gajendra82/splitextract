# RAD7B368 PDF Extraction Fix Audit

**PDF:** `/var/www/html/splitextract/rad7B368.pdf`  
**Date:** 2026-11-18  
**Scope:** Saleable Stock Report packing-column alignment (qty/value pairs)

## 1. PDF format and structural observations

- Digitally generated text PDF (bioPDF / ReckonPharma), not scanned; **0 images**.
- **2 pages**, selectable text, pipe-delimited table.
- Title: **Saleable Stock Report**; stockist **M/S RENUKA ENTERPRISE**; company **THE HIMALAYA DRUG COMP**; period **01/10/2026–31/10/2026**.
- Header (both pages):
  - `Particular | Packing | Opn | Opn | Rec | Rec | Issue | Issue | Bal | Bal`
  - Sub-header: `(Q+F) | Amt(Q+F) | (Q+F) | Amt | (Q+F) | Amt | (Q+F) | Amt(Q+F)`
- Empty movement cells print as `——`.
- Packing values include compact codes: `75GM`, `60T`, `10T`, `30C`, `PH 60'S`, `JAR`, bare `T`, etc.
- Page 2 repeats header; Firm Total / Grand Total at end.
- Printed Firm/Grand Total qty: Opening **17**, Receipts **5175**, Issue/Sales **298**, Closing **4894** (with matching amount columns).

## 2. Root cause of incorrect extraction

The document correctly selected the existing **saleable_stock_report** parser with layout `saleable_opn_rec_issue_bal_qty_value` (header-driven Opn/Rec/Issue/Bal qty+amount pairs).

`_saleable_pair_cells` drops a leading Packing cell only when `_saleable_leading_pack_cell` returns true. That helper recognized packs like `75GM` / `200ML` / bare `1`, but **not**:

- Compact unit codes: `60T`, `10T`, `30C`, `T`, `JAR`, `PH 60'S`
- **Empty** Packing on Firm/Grand Total rows

When Packing was not dropped, the eight qty/value fields shifted left by one:

- Packing (`60T`) → treated as `opening_qty` (uncertain / None)
- Opening qty → `opening_value`
- Opening amount → `receipts_qty`
- Receipt qty → `receipts_value`
- … amounts landed in sales/closing qty

Firm Total with empty Packing had the same shift (receipts became 1909.65 instead of 5175).

No OCR issue; no wrong parser family; no filename-based route.

## 3. Incorrect versus expected examples

| Product | Printed | Before fix (wrong) |
|---|---|---|
| CONFIDO TAB \|60T\| | Opn 0, Rec 100, Issue 11, Bal 89 | opening uncertain; receipts 19428 (amt); sales 2137.08 |
| SPEMAN TAB 60 TAB \|60T\| | Opn 5, Rec 50, Issue 0, Bal 55 | receipts 823.45; sales 9448 |
| TENTEX FORTE TABS \|10T\| | Opn 0, Rec 300, Issue 5, Bal 295 | receipts 31998; sales 533.3 |
| Firm Total | Opn 17, Rec 5175, Issue 298, Bal 4894 | receipts 1909.65; sales 766553.1; closing 48109.54 |

After fix: all **74** product rows match printed qty/value in document order; Firm/Grand totals match.

## 4. Existing parser selected before correction

- Method: `saleable_stock_report`
- Layout: `saleable_opn_rec_issue_bal_qty_value`
- Path: `_parse_pdf` → `_parse_saleable_stock_report_doc` → `_saleable_pair_item` / `_saleable_pair_cells`

## 5. Targeted correction implemented

Within the existing saleable pair path only:

1. **`STOCK_SALEABLE_PACK_UNIT_CODES`** (default **true**): recognize compact Packing codes (`60T`, `10T`, `30C`, `T`, `JAR`, `PH 60'S`, bare `G` units). Log prefix: `SALEABLE_PACK_UNIT_PEEL`.
2. **`_saleable_pair_cells`**: when there is exactly one extra cell before the eight pair fields, drop it if empty **or** recognized as Packing (fixes Firm/Grand Total).
3. **`_saleable_pair_item`**: prefer the Packing column over peeling size tokens out of the product name when that column is present.

No new stockist/filename detector. No new parser route. No shared vision/native/header-resolver changes.

## 6. Files modified

- `services/sales_statement_extractor.py` — pack-unit peel + empty packing + prefer pack column
- `tests/test_saleable_stock_report.py` — packing-column unit-code tests + `rad7B368.pdf` fixture tests
- `storage/app/secondary_sales/ai_mapping_evaluation/RAD7B368_PDF_EXTRACTION_FIX_AUDIT.md` — this audit

## 7. Why unrelated formats are unaffected

- Changes run only inside `_saleable_leading_pack_cell` / `_saleable_pair_cells` / `_saleable_pair_item`, which execute only after saleable qty/value pair headers are detected.
- Four-column saleable layouts and non-saleable parsers are untouched.
- Prince-style pair layouts without a Packing column still use eight cells as before (verified on `radF6468.pdf`).
- Flag kill-switch: `STOCK_SALEABLE_PACK_UNIT_CODES=false`.

## 8. Extracted row count

- Printed product rows: **74**
- Extracted line items: **74**
- No Firm/Grand/Company Total as products

## 9. Field-level validation results

Validated against PDF text for fields present on this statement:

| Field | Result |
|---|---|
| Product name | OK (Packing column preferred; names kept) |
| Packing | OK (`75GM`, `60T`, `PH 60'S`, …) |
| Opening / Receipt / Issue(Sales) / Closing qty | **74/74** match in row order |
| Opening/Receipt value (extra) + sales/closing value | Match printed Amt columns |
| Batch / expiry / rate / tax | Not printed on this statement |
| Dash `——` | Treated as printed 0.0 |

## 10. PDF total reconciliation

| | Opening | Receipts | Sales (Issue) | Closing |
|---|---|---|---|---|
| Printed Firm/Grand | 17.00 | 5175.00 | 298.00 | 4894.00 |
| Extracted totals | 17.00 | 5175.00 | 298.00 | 4894.00 |
| Amounts | 1909.65 / 766553.10 / 48109.54 / 720325.70 | same | same | same |

Printed numbers were not rewritten to force identity; row identity checks pass for product rows.

## 11. Existing-format regression test results

```
tests.test_saleable_stock_report — 18 tests, OK (4 skipped: missing Paras fixture)
tests.test_pdf_open_recover — 5 tests, OK
```

Prince same-layout sample `radF6468.pdf`: method `saleable_stock_report`, **178** rows, Firm totals 39873.5 / 31721 / 32923 / 38690.5 unchanged.

## 12. Same-format test results

- Primary: `rad7B368.pdf` — fixture class `TestSaleableRad7B368Fixture` passed.
- Synthetic packing-header unit-code cases in `TestSaleablePackingColumnUnitCodes` passed.
- Adjacent same family: `radF6468.pdf` (Saleable qty/value pairs) still correct.
- No second Renuka/ReckonPharma sample with `60T`-style packing was available beyond `rad7B368.pdf`; limitation noted below.

## 13. Remaining limitations

- Production service was **not** restarted; live traffic still needs an approved restart to load this fix.
- Only one live sample with compact `60T`/`10T` packing codes was available (`rad7B368.pdf`).
- Filename is not used for detection; future statements need the same Saleable header + Packing + Opn/Rec/Issue/Bal qty/Amt structure.
- Kill-switch / related flags: `STOCK_SALEABLE_PACK_UNIT_CODES` (default true), existing `STOCK_SALEABLE_QTY_VALUE_PAIRS`, `STOCK_SALEABLE_COMBO_PACK_PEEL`.
