# GPT Cost Analysis Report

Generated as part of the production-safe GPT cost measurement implementation.
**No prompts, extraction logic, or output schema were modified.**

---

## Executive Summary

The dominant GPT cost driver is the **fixed instruction template** in `build_invoice_prompt()` (~**25,945 characters / ~6,500 estimated tokens** per request), not the OCR invoice text itself. For typical single-page invoices (~2,000–8,000 OCR chars), the static prompt accounts for **~75–90%** of input tokens.

Safe optimizations implemented (all feature-flagged, default **off**):

| Optimization | Flag | Expected savings | Risk |
|---|---|---|---|
| Duplicate OCR cache | `ENABLE_GPT_CACHE=true` | 100% GPT cost on cache hits | None (identical OCR → identical JSON) |
| Lossless whitespace normalization | `ENABLE_OCR_NORMALIZATION=true` | 1–5% input tokens on noisy OCR | None (content-preserving) |
| Token metrics logging | `ENABLE_GPT_TOKEN_LOGGING=true` | N/A (measurement only) | None |

---

## Static Analysis (Codebase)

### Average prompt size

| Component | Typical size |
|---|---|
| Static instruction template | ~25,945 chars (~6,486 tokens est.) |
| OCR text (single page, PDFPlumber) | 2,000–8,000 chars (~500–2,000 tokens est.) |
| OCR text (multi-page / Tesseract) | 8,000–60,000 chars (~2,000–15,000 tokens est.) |
| **Total prompt (typical)** | **~28,000–35,000 chars (~7,000–9,000 tokens est.)** |

Measured via `build_invoice_prompt("INVOICE 123\nLine item 1\n")` → 25,969 chars total.

### Average OCR size (observed ranges)

| Source | Typical OCR chars |
|---|---|
| PDFPlumber (typed PDF) | 2,000–10,000 |
| Tesseract (scanned) | 1,500–3,000 per page |
| Multi-page batch (27 pages) | 50,000+ chars across job |

### Average total tokens (estimated per GPT call)

| Scenario | Input tokens (est.) | Output tokens (typ.) | Total (est.) |
|---|---|---|---|
| Single-page good OCR | 7,000–9,000 | 1,500–3,000 | 8,500–12,000 |
| Multi-section re-extract | 7,500–10,000 | 1,000–2,500 | 8,500–12,500 |
| Large OCR (truncated at 60k) | 12,000–18,000 | 2,000–4,000 | 14,000–22,000 |

Cost estimate uses env-configurable rates (`GPT_INPUT_COST_PER_1M_USD`, `GPT_OUTPUT_COST_PER_1M_USD`).

---

## Top Reasons for High Token Usage

1. **Large static prompt template** (~26k chars) sent on every GPT call — includes all vendor scenario examples and column-mapping rules.
2. **Re-extraction passes** — multi-invoice PDFs trigger additional GPT calls per detected section (`RE-EXTRACTING invoice …`).
3. **Long OCR text** — Tesseract output on scanned multi-page PDFs increases input size (though still smaller than the template).
4. **Duplicate processing** — same OCR sections may be sent to GPT again on retries/re-runs without cache.
5. **Gemini fallback path unchanged** — when GPT fails validation, Gemini Text receives the same large prompt (separate cost, not GPT).

---

## Potential Savings (For Later Review — NOT Implemented)

These were **identified only** per requirements; they may affect extraction accuracy and require A/B testing:

| Idea | Est. savings | Risk |
|---|---|---|
| Shorter prompt / vendor-conditional scenarios | 40–70% input tokens | **High** — may reduce accuracy |
| Send only table region OCR (strip headers/footers) | 10–30% input tokens | **Medium** — may lose invoice metadata |
| Truncate OCR below 60k more aggressively | 5–20% on large docs | **Medium** — may miss line items |
| Skip GPT on re-extract when first pass succeeded | 10–30% on multi-invoice PDFs | **Medium** — needs validation |
| Use cheaper model for simple single-item invoices | 50–80% cost | **Medium** — accuracy trade-off |

---

## Implemented Features

### Feature flags (`.env`)

```env
ENABLE_GPT_CACHE=false          # SHA256(normalized OCR) → skip GPT on hit
ENABLE_OCR_NORMALIZATION=false  # Lossless whitespace only
ENABLE_GPT_TOKEN_LOGGING=false  # Metrics log block per GPT call
GPT_CACHE_TTL_SECONDS=86400
GPT_CACHE_DIR=.gpt_cache
GPT_INPUT_COST_PER_1M_USD=0.15
GPT_OUTPUT_COST_PER_1M_USD=0.60
```

All default **false** — production behaviour unchanged until explicitly enabled.

### Logging format

When `ENABLE_GPT_TOKEN_LOGGING=true`, each GPT request emits:

```
================================================
Invoice: <invoice_no>
OCR Size: <chars> (~<est> tokens est.)
Prompt Size: <chars> (~<est> tokens est.)
Prompt Tokens: <actual>
Input Tokens: <actual>
Output Tokens: <actual>
Total Tokens: <actual>
Estimated Cost: $<amount>
Response Time: <ms> ms
Cache Hit / Cache Miss
================================================
```

### Runtime metrics

In-process aggregator: `services.gpt_cost_metrics.GPT_METRICS.snapshot()` returns averages after logging is enabled.

---

## Rollout Recommendation

1. Enable `ENABLE_GPT_TOKEN_LOGGING=true` for 24–48 hours — collect real averages.
2. Enable `ENABLE_GPT_CACHE=true` — safe, eliminates duplicate OCR spend.
3. Enable `ENABLE_OCR_NORMALIZATION=true` — safe, minor token reduction on noisy OCR.
4. Review "Potential Savings" table with real metrics before any prompt changes.

---

## Files Modified

| File | Change |
|---|---|
| `services/ocr_text_normalize.py` | Lossless whitespace normalization |
| `services/gpt_response_cache.py` | SHA256 OCR cache with TTL |
| `services/gpt_cost_metrics.py` | Token/cost logging and aggregation |
| `services/gpt_text_extractor.py` | Return token usage from OpenAI Responses API |
| `app.py` | Wire flags, cache, normalization, metrics in GPT path only |
| `services/config.py` | Feature flag config fields |
| `.env.example` | Document new env vars |
| `tests/test_gpt_cost_reduction.py` | Unit tests for normalization + cache |
| `.gitignore` | Ignore `.gpt_cache/` |

**Unchanged:** prompts, JSON schema, callbacks, validations, vendor matching, Gemini Vision, OCR routing, API endpoints, response structure.
