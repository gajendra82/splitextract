# OCR Page Reliability — Before/After (Phase 1)

Created: 2026-11-24T07:32:29.268565+00:00
Scope: `_recognize_page_resilient` only (PaddleOCR-VL → Tesseract → Gemini).
Phase 2: disabled
Docs: 2 SOURCE_VERIFIED GT samples

## Before (OCR_PER_PAGE_TIMEOUT_S=120, cache off)
- Primary VL timeout rate: **1.0** (every page hit page deadline; Tess recovered)
- Final usable OCR reliability: **1.0**
- Final timeout rate (post-recovery): 0.0
- VL HTML table pages: 0.0
- Mean wall latency ms: 128895.0
- pages=2

## After (timeout=180, cache on)
- Primary VL timeout rate: **1.0** (CPU VL still exceeds 180s here)
- Final usable OCR reliability: **1.0**
- Final timeout rate (post-recovery): 0.0
- VL HTML table pages: 0.0
- Mean wall latency ms: 188068.5 (longer wait before Tess because deadline raised)
- pages=2

## After + cache warm
- OCR reliability: 1.0
- Cache hits: 2
- Mean wall latency ms: **337.5**

## Accuracy (stored VL SOURCE_VERIFIED GT-20)
- Field accuracy ≈ 0.515 / Quantity ≈ 0.444 / Table structure 1.0
  (from `benchmark_ocr_reliability_gt20.py --baseline-stored`)
