# OCR Reliability Phase 1 — SOURCE_VERIFIED Results

Created: 2026-11-24

Phase 2 remained **disabled** (`PHASE2_RECOVERY_ENABLED=false`).

## Diagnostic (confirmed live)

| Cause | Evidence |
|---|---|
| Effective page deadline was 120s | `.env` had `OCR_PER_PAGE_TIMEOUT_S=120`; smoke: `ocr page=1 timed out after 120.0s` |
| Nested 300s engine deadline unused when page bound fires first | Page `call_bounded(120)` wraps orchestrator `call_bounded(300)` |
| CPU PaddleOCR-VL exceeds both 120s **and** 180s on this host | Live page bench: VL timed out every page; Tess recovery accepted |
| Page timeout now kills VL serve | Logs: `paddle_vl_serve_killed reason=page_or_execution_timeout` |
| No durable page cache before | Repeat benches re-inferred; warm cache now ~0.3s |

## Before → After (page OCR, 2 GT docs)

Scope: `_recognize_page_resilient` only (VL → Tesseract → Gemini). Full extract live A/B was aborted after >35min/doc due to nested re-OCR on CPU.

| Metric | Before (120s, cache off) | After (180s, cache on) | After warm cache |
|---|---:|---:|---:|
| Final usable OCR reliability | **1.0** (Tess recovery) | **1.0** (Tess recovery) | **1.0** |
| Primary VL timeout rate | **1.0** (2/2 pages) | **1.0** (2/2 pages) | 0 (cached) |
| Final page timeout rate (post-recovery) | 0.0 | 0.0 | 0.0 |
| Mean wall latency | 128.9 s | 188.1 s | **0.34 s** |
| Cache hits | 0 | 0 | **2** |
| VL HTML table pages (live) | 0 | 0 | 0 |

Notes:
- Raising the page deadline alone does **not** make CPU VL succeed on this machine (still >180s). It does add wait before Tess recovery when VL cannot finish.
- Reliability win on this host is **recovery + serve kill + cache**, not VL completion.
- On GPU / faster hosts, 180s gives cold VL room that 120s denied.

## SOURCE_VERIFIED accuracy (GT-20, stored VL, Phase2 OFF)

Extraction plumbing unchanged; scores match prior Phase2-OFF A/B within noise.

| Metric | Prior COMPARISON_REPORT (Phase2 OFF) | This run (`--baseline-stored`) |
|---|---:|---:|
| Field accuracy | 0.518 | **0.515** |
| Quantity / numeric accuracy | 0.465 | **0.444** |
| Table structure (line_items or HTML present) | n/a | **1.0** |
| Mean extract latency | n/a | **455 ms** (no live OCR) |

Artifacts:
- `aggregate.json` — stored GT-20 accuracy
- `page_reliability_aggregate.json` — live page before/after
- `DIAGNOSTIC_REPORT.md` — bottleneck analysis
- `BENCHMARK_REPORT.md` — this file

## Code changes shipped

1. `services/ocr/timeout_policy.py` — centralized timeouts; default page **180s**; optional `OCR_RETRY_ON_TIMEOUT`
2. `services/ocr/page_cache.py` — durable page cache; **never** caches empty/timeout/error
3. `pipeline._recognize_page_resilient` — kill VL serve on page timeout; cache; structured `ocr_page_done` logs; timeout restart hook
4. Stop rewriting OCR `timeout`/`error` page status to `success` after extract
5. `.env`: `PHASE2_RECOVERY_ENABLED=false`, `OCR_PER_PAGE_TIMEOUT_S=180`, `OCR_PAGE_CACHE=true`
6. Tests: `tests/test_ocr_reliability_phase1.py` (all passing with recovery-chain suite)

## Constraints preserved

- PaddleOCR-VL remains primary (not replaced; Tess not primary)
- Recovery order VL → Tesseract → Gemini unchanged
- No new LLM; stock identity validation untouched
- Extraction APIs / schemas unchanged
- Phase 2 default off
