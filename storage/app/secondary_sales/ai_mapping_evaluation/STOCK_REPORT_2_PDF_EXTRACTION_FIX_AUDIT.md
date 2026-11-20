# STOCK_REPORT_2_PDF_EXTRACTION_FIX_AUDIT

**Date:** 2026-11-18  
**PDF:** `/var/www/html/splitextract/Stock_Report (2).PDF`  
**Scope:** Op.Qnt/Pur.Qnt/Sl.Qnt/Cl.Qnt digital STOCK REPORT via `pdf_words_free_columns`  
**Independence:** Analyzed separately from `rad7B368.pdf` (Saleable Stock Report). Different title, columns, and parser path.

## 1. Original PDF characteristics

- Digitally generated text PDF (selectable words; **0** images).
- PDF 1.3, A4 portrait `594.7 × 841.5` pt.
- Title **STOCK REPORT**; stockist **PROGRESIVE PHARMA**, Chapra.
- Period line: `01/10/26  To :31/10/26`.
- Header: `Particular | Op.Qnt. | Op.Value | Pur.Qnt. | Pur.Value | Sl.Qnt. | Sl.Value | Cl.Qnt. | Cl.Value` (no Pack column).
- Division banners between companies: `HIMALAYA ZENITH`, `HIMALYA DRUG`, `HIMALYA ZANDRA`, `HIMALYA ZINDEL` (note printed misspelling **HIMALYA**).
- `COMPANY Total :` rows and footer `5 COMPANY 128 Prod.` with Value/Qnt OB/Purchase/Sale/Closing.

## 2. Number of pages

**3** pages.

## 3. Document layout observations

- Fixed-width space-aligned table; blank purchase/sales cells when zero movement.
- Product names sometimes include trailing size in the Particular column (`BONNISAN 200ML`, `GENTLE BABY POWDER 400GM`); there is no separate Packing header.
- Section banners are letter-only lines with no numbers, immediately above the first SKU of that block.
- Page 2 continues mid-company; page 3 ends with company total + grand footer.
- Not the Saleable Opn/Rec/Issue/Bal pipe layout; not the ZEAL Particular+Packing photo path (`_particular_packing_stock_report_text` requires ZEAL and rejects ZANDRA).

## 4. Root cause of incorrect extraction

Path selected: **`pdf_words_free_columns`** (`parse_pdf_free_column_statement` → header-resolved word columns). Headers resolved correctly to opening/purchase/sales/closing qty+value.

Two generic bugs in that shared path:

1. **Brand/division pending merge** — `HIMALAYA ZENITH` / `HIMALYA ZANDRA` (qty-empty letter-only lines) were stored as `pending_name` and prepended to the next product (`HIMALAYA ZENITH AMALAKI TAB`). The resolved-column filter then dropped any name matching `^himalaya\b`, so **AMALAKI TAB** vanished. Misspelled **HIMALYA** banners were not dropped and remained glued onto ABANA / ASHVAGANDHA / BABY CARE.

2. **Pack-size dead zone** — `STOCK_PDF_RIGHT_ALIGN_QTY` split `GENTLE BABY POWDER` | `400GM`. Token `400GM` sat between Particular and Op.Qnt centres → `AMBIGUOUS_CELL` nulled **product_name** and **opening_qty** → those two rows were lost.

Before fix: **125** products; qty sums 6235 / 1413 / 3498 / 4158 vs printed 6267 / 1413 / 3509 / 4179.

## 5. Before-and-after extraction examples

| Product (PDF) | Before | After |
|---|---|---|
| AMALAKI TAB 7 / 1070.24 → Cl 7 / 1070.24 | **Missing** (merged+dropped) | opening 7, closing 7, values OK |
| GENTLE BABY POWDER 400GM 14→10→4 | **Missing** (ambiguous 400GM) | opening 14, sales 10, closing 4 |
| GENTLE BABY POWDER 700GM 11→1→10 | **Missing** | opening 11, sales 1, closing 10 |
| ABANA TAB under HIMALYA ZANDRA | name `HIMALYA ZANDRA ABANA TAB` | `ABANA TAB` |
| BABY CARE GIFT PACK(JAR) | name `HIMALYA DRUG BABY CARE…` | `BABY CARE GIFT PACK(JAR)` |
| BONNISAN 200ML | Already correct quantities | Unchanged |

## 6. Parser selected before the fix

- Method: `pdf_words_free_columns`
- Layout match log: `STOCK_PDF_WORDS_RESOLVED_COLUMNS match=op_pur_sl_cl`
- Not saleable_stock_report; not particular_packing vision; not Nilakantha OB/PUR/SAL/CB text parser.

## 7. Targeted correction implemented

Generic native/header fixes only (no filename route, no stockist-specific parser):

1. **`STOCK_HEADER_PACK_IN_PRODUCT_DEAD_ZONE`** (default ON) — in band `assign_cells`, a unit-pack token in the `product_name`↔`opening_qty` (or pack) dead zone assigns to pack if present, else **product_name**, instead of nulling both columns.

2. **`STOCK_PDF_WORDS_SKIP_BRAND_SECTION_PENDING`** (default ON) — skip `HIMALAYA`/`HIMALYA` + division-line banners as pending product titles; strip that prefix if a qty row still carries it; do not drop qty-bearing SKUs solely because the name starts with the brand.

## 8. Files modified

- `services/stock_header_resolver.py` — pack dead-zone assignment + flag
- `services/stock_native_resolver.py` — brand-section pending skip/strip + flag
- `tests/test_stock_report_op_qnt_brand_pending.py` — unit + fixture tests
- `storage/app/secondary_sales/ai_mapping_evaluation/STOCK_REPORT_2_PDF_EXTRACTION_FIX_AUDIT.md` — this audit

## 9. Reason the fix is isolated

- Changes activate only for (a) unit-pack tokens in a specific dead-zone geometry, or (b) brand/division banner lines matching `HIMA(LAYA|LYA)` with no digits/pack words.
- Saleable Opn/Rec/Issue/Bal path untouched.
- No new per-format detector or filename branch.
- Kill switches: `STOCK_HEADER_PACK_IN_PRODUCT_DEAD_ZONE=false`, `STOCK_PDF_WORDS_SKIP_BRAND_SECTION_PENDING=false`.

## 10. Extracted row count

**128** product rows (matches footer `128 Prod.`). Company totals and banners excluded.

## 11. Field-level validation

Spot-checked against PDF text/words:

- Opening/purchase/sales/closing qty+value for sparse rows (LUKOL sales-only; LIV 52 DS SYP no opening; CYSTONE 200ML sold out with blank closing).
- Division banners not product names.
- Pack sizes that sit in Particular remain on the product name when no Pack header exists.
- Stockist letterhead: `PROGRESIVE PHARMA`.

Period `01/10/26 To :31/10/26` is still not promoted to `period_from`/`period_to` on this path (pre-existing gap; not required to recover rows).

## 12. Total reconciliation

After fix (native resolved columns):

| Metric | Extracted | Printed footer | Diff |
|---|---:|---:|---:|
| Opening qty | 6267.0 | 6267.000 | 0 |
| Purchase qty | 1413.0 | 1413.000 | 0 |
| Sale qty | 3509.0 | 3509.000 | 0 |
| Closing qty | 4179.0 | 4179.000 | 0 |
| Opening value | 825714.69 | 825714.61 | +0.08 |
| Purchase value | 190701.82 | 190701.81 | +0.01 |
| Sale value | 467368.36 | 467368.36 | 0 |
| Closing value | 550176.33 | 550176.27 | +0.06 |

Qty footer matches exactly. Sub-cent value drift is floating-point / printed rounding; values were not rewritten to force reconciliation.

## 13. Existing-format regression results

- `tests.test_stock_report_op_qnt_brand_pending` — **OK**
- `tests.test_op_qnt_value_expiry_pdf_words` — **OK**
- `tests.test_op_stock_cl_stock_pdf_words` — **OK**
- `tests.test_saleable_stock_report` — **OK** (6 skipped: missing rad7B368 / Paras fixtures on this host)
- `radF6468.pdf` live extract: method `saleable_stock_report`, **178** rows (unchanged path)
- `tests.test_stock_header_resolver` — 3 failures (`test_cell_collision`, `test_op_qty_value_strip`, `test_two_row_header`) reproduce with this change **stashed**; pre-existing, unrelated.

## 14. Same-format regression results

- Primary fixture: `Stock_Report (2).PDF` → 128 rows, footer qty match, banners stripped.
- No second same-layout sample available on the server beyond this file (limitation noted).

## 15. Downstream compatibility results

- Top-level keys / line_item contract unchanged (`product_name`, movement qty/value fields, `extra`).
- Method remains `pdf_words_free_columns`.
- No `/split-and-extract`, `app.py`, or Laravel-facing key changes.
- No production restart, redeploy, or historical reprocess performed.

## 16. Remaining limitations

- Statement period still not filled on this PDF (`period_from`/`period_to` None).
- Value footer differs by &lt;0.1 from summed line values (printer rounding).
- Brand-section detector is intentionally limited to `HIMALAYA`/`HIMALYA` division banners already handled by the prior `^himalaya` skip; other manufacturers’ letter-only section titles are unchanged.
- Live uvicorn on `:8002` was not restarted; production will serve the fix only after an approved restart.
