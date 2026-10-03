# Stock OCR benchmark design (isolated)

**Status:** BENCHMARK ONLY. Does **not** change `POST /extract-sales-statement`.

## Current production flow (unchanged)

```
POST /extract-sales-statement (app.py)
  → extract_sales_statement (sales_statement_extractor.py)
    → _parse_image / scanned PDF
      → [STOCK_VISION_TABLE] run_vision_table_path
        → extract_stock_table_vision   # Gemini full-table JSON
        → map_vision_table             # header resolver + assign_cells
        → apply_closing_derived        # must NOT invent Balance when column printed
        → apply_stock_reconciliation   # validation only
        → optional recon recovery / identity reread (≤ gemini_budget)
      → else legacy OCR / stock_direct_vision
    → sanitize → API JSON for Laravel
```

Known failure mode on dense Monthly Stock & Sales screenshots: Gemini misreads
or shifts numeric cells; arithmetic must never replace printed Balance/Sale/etc.

## Benchmark goal

Compare **coordinate-aware OCR** (Mistral OCR 4.x with blocks + tables) against
the existing Gemini vision-table path on the same file, **without** wiring Mistral
into production.

## Components (new, isolated)

| Module | Role |
|---|---|
| `services/stock_ocr_providers/` | Provider abstraction + Mistral (+ Gemini adapter for compare) |
| `services/stock_ocr_table_reconstructor.py` | BBox / HTML table → column-aligned rows (blank-preserving) |
| `services/stock_ocr_benchmark.py` | Orchestration, expected fixtures, cell-level report |
| `benchmark_stock_ocr.py` | CLI entry |

Google Document AI is **not** implemented (no existing wiring / SDK in this repo).

## Provider contract

`StockDocumentOCRProvider.extract_document(file_bytes, *, filename, mime_type)` →
normalized `OCRDocumentResult` with pages, blocks (bbox + text), optional HTML
tables, word confidence when available, dimensions, latency, provider metadata.

Do **not** coerce into Gemini vision JSON.

## Table reconstruction

1. Prefer HTML/markdown tables from OCR when present (empty `<td>` ⇒ blank cell; no left-shift).
2. Else cluster text blocks by Y into rows and assign by X to header column centres
   (reuse `stock_header_resolver.resolve_columns` / `assign_cells` geometry mode).
3. Map aliases via existing header resolver (`Purc. Ret` → `purchase_return_qty`, never `purchase_qty`).
4. Build line_items with `field_source=printed|missing`; call `apply_closing_derived`
   (Balance column present ⇒ never invent `closing_qty`) and `apply_stock_reconciliation`.

## Fallback design (NOT enabled in production)

```
Mistral OCR + coordinate reconstruct → reconcile
  PASS → accept
  FAIL → crop failed row → optional Vision → reconcile again
  FAIL → mark manual review
```

Never re-send the full document to Gemini in a loop.

## Config (env only; not enabled by default)

```
MISTRAL_API_KEY=
MISTRAL_OCR_MODEL=mistral-ocr-latest
STOCK_OCR_BENCHMARK_ENABLED=false
```

## Safety

- No changes to `/extract-sales-statement` behaviour.
- No production `.env` / model / concurrency / Laravel / DB changes.
- CLI/benchmark only until explicitly promoted.
