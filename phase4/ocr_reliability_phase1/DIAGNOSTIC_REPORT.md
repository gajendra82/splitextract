# OCR Reliability Phase 1 — Diagnostic Report

Date: 2026-11-24

## Architecture (current)

```
pipeline.run
  └─ per page (_ocr_page_images already page-level)
       └─ _recognize_page_resilient
            ├─ preprocess + prepare_for_ocr (~150 DPI downscale)
            ├─ call_bounded(OCR_PER_PAGE_TIMEOUT_S, default 120s)
            │    └─ OCROrchestrator.recognize(use_fallback=False)
            │         └─ call_bounded(OCR_EXECUTION_TIMEOUT_S / PADDLE_VL_TIMEOUT_S, default 300s)
            │              └─ PaddleVLEngine (subprocess keep-alive VL worker preferred)
            ├─ soft retries (invert/zoom) only if NOT hard-timeout
            └─ recovery_chain: tesseract → gemini transcription
```

Recovery order **PaddleOCR-VL → Tesseract → Gemini** is already correct and must stay.

## Actual timeout causes (not guesses)

1. **Effective deadline is 120s, not 300s**  
   `_recognize_page_resilient` wraps the primary call in `OCR_PER_PAGE_TIMEOUT_S` (default **120**). Nested engine/orchestrator deadlines (300s) never get that long. Smoke/bench logs: `ocr page=1 timed out after 120.0s`.

2. **Cold VL load + CPU inference exceeds 120s**  
   Keep-alive worker loads PaddleOCR-VL once; first page pays model init + layout+OCR. On CPU (or after `CUDA_VISIBLE_DEVICES=-1` poison recovery) a single dense stock page routinely needs >120s. Default page timeout is too aggressive for cold/CPU.

3. **Timeout abandons the worker without killing it (page path)**  
   Orchestrator `_recognize_bounded` kills the VL serve process on its own timeout. Page-level `call_bounded` does **not**. An abandoned predict keeps the serve lock/process busy while Tess/Gemini recovery starts → resource contention and serial delays.

4. **Timeout → kill → next page cold-starts again**  
   When the serve *is* killed, the next page reloads the model, often timing out again in a loop.

5. **No durable page OCR cache at DI layer**  
   Request-scoped caches exist in `sales_extraction_runtime` / a tiny image cache in the extractor, but identical page bytes across A/B arms, retries, or re-runs re-infer. Phase2 A/B therefore used **stored VL text** because live OCR was unreliable.

6. **Rasterize then downscale waste**  
   PDFs are pixmap’d at `Matrix(2.0)` then `prepare_for_ocr` shrinks to ~1240px width. Extra CPU/memory before VL starts (secondary cost, not the main timeout).

## What is already good

- Page-level loop for multi-page PDFs (`_ocr_page_images`)
- Soft page retry + Tess→Gemini recovery chain
- Empty/error primary treated unusable (`_unusable_reason`)
- `di_ocr_override` + VL HTML table path preserved when OCR succeeds
- Phase 2 still default-off

## Smallest safe improvement set

| Change | Why |
|---|---|
| Kill VL serve on **page-level** timeout | Stop abandoned predicts poisoning recovery |
| Configurable timeout policy helper | One place for page/execution/retry env |
| Raise default `OCR_PER_PAGE_TIMEOUT_S` to **180** (still overrideable) | Give cold/GPU VL room; on this CPU host VL still exceeds 180s |
| Optional `OCR_RETRY_ON_TIMEOUT=1` single restart retry | Recover after kill without changing primary engine |
| Durable page-hash OCR cache (memory + optional disk) | Skip repeat inference; **never** cache empty as success |
| Structured per-page logs | engine, duration_ms, timeout, cache, recovery |

### Measured follow-up (this host)

Live page bench (2 GT docs): primary VL timed out at **both** 120s and 180s; Tess recovery kept final usable reliability at 1.0; warm cache cut mean latency from ~129–188s to **~0.34s**. On CPU-only boxes that never finish VL, operators may prefer a lower page timeout to fail over to Tess faster; keep 180s when GPU/cold-start headroom is the goal.

Non-goals for this phase: replace VL, Tess-primary, new LLM, weaken stock validation, schema breaks.
