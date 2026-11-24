# Phase 4 — Implementation Report

## Summary

Built the **DocumentIntelligencePipeline** foundation around existing Phase 1–3 components. Named parser cascade, Phase 2 recovery, and Phase 3 layout fixes are preserved. Default OCR policy remains Tesseract-only (`OCR_ENSEMBLE_ENABLED=false`).

---

## Files created

| File | Role |
|------|------|
| `services/ocr/__init__.py` | OCR package exports |
| `services/ocr/base.py` | OCREngine / OCRResult / OCRWord |
| `services/ocr/tesseract_engine.py` | Tesseract adapter via `_a2z_tesseract` |
| `services/ocr/paddle_engine.py` | Optional PaddleOCR adapter |
| `services/ocr/orchestrator.py` | Engine policy + disagreement |
| `services/layout/__init__.py` | Layout package |
| `services/layout/detector.py` | LayoutUnderstanding |
| `services/layout/table_detector.py` | Table regions from tokens |
| `services/layout/column_detector.py` | Header candidates |
| `services/layout/row_detector.py` | Row banding |
| `services/extraction/__init__.py` | Extraction package |
| `services/extraction/parser_registry.py` | Strategy registry |
| `services/document_intelligence/__init__.py` | DI package |
| `services/document_intelligence/pipeline.py` | Central orchestrator |
| `services/document_intelligence/confidence.py` | Multi-dimension confidence |
| `services/document_intelligence/decisions.py` | Final status model |
| `services/document_intelligence/verification.py` | Independent verification |
| `services/document_intelligence/validation_stack.py` | Layered validation view |
| `services/document_intelligence/observability.py` | Timing + audit record |
| `phase4/ARCHITECTURE.md` | Architecture document |
| `phase4/IMPLEMENTATION_REPORT.md` | This report |
| `tests/test_phase4_architecture.py` | Architecture unit tests |

---

## Files modified

| File | Change |
|------|--------|
| `services/sales_statement_extractor.py` | `extract_sales_statement` → pipeline; added `extract_sales_statement_core`, public `attach_phase1_architecture` / `maybe_apply_phase2_recovery`; Phase 1 attach accepts reused route/evidence |

---

## Existing components reused

- `document_router.py`
- `evidence.py`
- `schemas.py` / `normalize_sales_statement`
- `base_validator.py`
- `hybrid_schema.py`
- `failure_taxonomy.py` (via validation failure types)
- `recovery_policy.py` / `recovery_runner.py`
- `stock_identity_profiles.py` (via existing identity validation)
- `column_mapping.py` (layout-scoped)
- Named parsers inside `_parse_pdf` / `_parse_image` / `_parse_xls` / text/word
- Phase 3 fixes inside product-wise / marg / profitmaker / op_pur_orec paths (untouched)

---

## Tests

New: `tests/test_phase4_architecture.py`

Covers:

1. Digital PDF does not require OCR / pipeline short-circuit  
2. Image OCR policy can use Tesseract  
3. PaddleOCR common interface (without loading torch)  
4. OCR evidence bbox/confidence/engine  
5. OCR disagreement preserved  
6. Known parser preferred  
7. Unknown layout reaches generic strategy  
8. Null ≠ zero / printed zero remains zero  
9. Dynamic cannot overwrite canonical  
10. Decision model: VERIFIED / UNRESOLVED / PROVIDER_ERROR / RECOVERED_AND_VERIFIED  
11. Recovery must revalidate  
12. Layout-scoped ColumnMapping  

Regression suites to run:

- `tests/test_phase1_architecture.py`
- `tests/test_phase2_recovery.py`
- `tests/test_phase2b_recovery_contract.py`
- `tests/test_phase3_3_layout_fixes.py`
- `tests/test_phase3_6_product_wise_issues_boundary.py`
- `tests/test_phase3_8_product_wise_photo_fold.py`
- `tests/test_phase4_architecture.py`

Hard gate: **PASS → WRONG = 0** (no Phase 3 behavior changes intended).

### Regression results (local)

```
110 passed, 2 warnings, 3 subtests passed
```

Suites: phase4 + phase1 + phase2 + phase2b + phase3_3 + phase3_6 + phase3_8.

---

## Environment defaults

```
OCR_ENGINE=tesseract
OCR_FALLBACK_ENGINE=paddle
OCR_ENSEMBLE_ENABLED=false
OCR_DISAGREEMENT_THRESHOLD=0.15
PHASE2_RECOVERY_ENABLED=false   # unchanged
```

---

## Known limitations

1. **Generic structured extraction** is a strategy slot + layout signals; it does not yet replace named parsers with a full unknown-document extractor. Unknown docs still flow through the existing cascade + Gemini fallback rules.  
2. **OCR ensemble** is not injected into every parser call site yet; orchestrator is the shared API for recovery/tests. Parsers keep baseline Tesseract.  
3. **PaddleOCR** may be installed but unloadable on some Windows torch DLL setups; adapter fails soft without crashing extraction.  
4. **Independent verification** is rule-based (identity, null integrity, dynamic guard, OCR disagreement). Full per-cell evidence replay is future work.  
5. Open Phase 3 items (**0238** free columns, OCR leftovers **3752/3337/«18**) remain parked — not “fixed” by architecture hacks.

---

## Next integration step

1. Optionally wire `OCROrchestrator` into scanned/image recovery paths when ensemble is enabled.  
2. Grow `generic_structured` to consume layout headers/rows into hybrid dynamic fields for truly unknown layouts.  
3. Expand independent verification with column-scoped evidence replay.  
4. Do **not** reopen Phase 3 document loops unless a regression appears.

---

## Success criteria checklist

| # | Criterion | Status |
|---|-----------|--------|
| 1 | Existing functionality intact | Preserved via core cascade |
| 2 | Phase 1/2/3 tests | Run in CI / local suite |
| 3 | Shared OCR abstraction | Done |
| 4 | Evidence text+bbox+confidence+source | Done (OCRResult / existing TextToken) |
| 5 | Known parsers preferred | Done (registry) |
| 6 | Unknown layout path | Done (generic strategy + layout) |
| 7 | Dynamic unknown fields | Hybrid schema preserved |
| 8 | Canonical requires evidence/mapping | Layout-scoped mapping + verification guard |
| 9 | Validation independent of extraction | Verification layer |
| 10 | Recovery targeted/bounded | Phase 2 unchanged |
| 11 | No silent invention | Safety rules + null/zero tests |
| 12 | VERIFIED or UNRESOLVED | Decision model |
| 13 | PASS→WRONG = 0 | No Phase 3 fix changes |
| 14 | Reusable for unseen docs | Foundation in place |
