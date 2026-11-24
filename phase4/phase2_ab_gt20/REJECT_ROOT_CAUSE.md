# Why Phase 2 JSON stayed unresolved (GT-20)

## Verified discard path (not “strict validation alone”)

On `0000700084...pdf` (and the same shape on most `STOCK_IDENTITY_FAILURE` docs):

1. Phase 1 fuzzy mapping left **`Rcpt Qty` unmatched**, so Python `receipts_qty` stayed `0`.
2. Stock identity failed (e.g. Confido `8+0-40≠68`; true print is `Rcpt Qty=100` → `8+100-40=68`).
3. Phase 2 prompt used `extraction_contract(None)` → **“Leave canonical quantity fields null… Put each printed column in dynamic.”**
4. Gemini returned **valid** `SalesExtractionOutput` with `fixed.sales_qty` only and printed qtys under `dynamic` (`OpStk`, `Rcpt Qty`, `Clstk`).
5. `merge_stock_recovery` copied **only** `fixed.*` (skipping nulls) and ignored dynamic headers (except `stock_out_qty`).
6. Candidate still failed stock identity → **entire merge discarded** (`applied=False`). Healthy rows were never the issue.

So Gemini was often **correct in dynamic**, not “wrong JSON”. Acceptance stayed strict; the bug was **failing to apply printed dynamic qty headers**.

## Other unresolved classes (not fixed as validation weakenings)

| Class | Docs | Nature |
|---|---|---|
| Dynamic qty discard | most stock-identity | **Fixed** (promote + Rcpt matcher + stock prompt) |
| `EMPTY_OUTPUT` `parsed=False` | 0124, 0346, 1904 | Truncated / invalid JSON or empty parse — genuine provider/parse failure |
| Empty `product_name` | 0406 | Gemini returned blank names — match fails |
| Name mismatch | 0434 | Gemini names don’t exact-`_norm` match Python names |
| All-null qty rows | 0275, 0317, 0358 | Gemini returned names only — insufficient corrections |

## Fixes applied

- Match `Rcpt Qty` / `Rcpt` → `receive_qty` in `column_matcher`.
- `_promote_dynamic_qty_into_fixed` before merge/adopt (null fixed only).
- Stock prompt: fill canonical qtys; only listed failing products; declare document mapping when headers exist.
- OCR-evidence path still skips `project_sales_text` wipe of fixed fields.
- Healthy-row allowlist + field provenance unchanged.
- Phase 2 still defaults off.
