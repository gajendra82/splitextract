# Phase 4 — Document Intelligence Architecture

## Goal

Any previously unseen **supported** document should:

1. Be routed and understood structurally  
2. Produce evidence (native and/or OCR)  
3. Select an extraction strategy  
4. Extract without inventing values  
5. Validate independently of the extractor  
6. Recover only when bounded and revalidated  
7. End as **VERIFIED**, **RECOVERED_AND_VERIFIED**, **UNRESOLVED**, **PROVIDER_ERROR**, or **EXTRACTION_ERROR**

Priority: **CORRECTNESS > GENERALIZATION > MAINTAINABILITY > SPEED**

A wrong value marked PASS is worse than UNRESOLVED.

---

## Final architecture

```
document
    → DocumentRouter
    → EvidenceCollector (evidence_for_route)
    → LayoutUnderstanding (services/layout)
    → ExtractionStrategySelector (parser_registry)
    → Extraction (existing named parser cascade)
    → Normalization (schemas.normalize_sales_statement)
    → Schema / Evidence / Business validation (base_validator + stack view)
    → IndependentVerification
    → Targeted Recovery (Phase 2, unchanged)
    → Re-validation
    → Final decision + confidence + observability
```

Entry point: `extract_sales_statement` → `DocumentIntelligencePipeline.run`.

Core parsers remain in `extract_sales_statement_core` (unchanged cascade).

---

## Component responsibilities

| Component | Path | Responsibility |
|-----------|------|----------------|
| DocumentRouter | `services/document_router.py` | File kind, digital/scanned, OCR need. No business extraction. |
| Evidence | `services/evidence.py` | Immutable document/page/token evidence. |
| OCR base | `services/ocr/base.py` | `OCREngine` / `OCRResult` / `OCRWord` contract. |
| Tesseract | `services/ocr/tesseract_engine.py` | Production baseline OCR adapter. |
| Paddle | `services/ocr/paddle_engine.py` | Optional second evidence source. |
| OCR orchestrator | `services/ocr/orchestrator.py` | Engine policy, ensemble, disagreement. |
| Layout | `services/layout/*` | Headers, columns, rows, tables for unknown layouts. |
| Parser registry | `services/extraction/parser_registry.py` | Strategy ranking; wraps named parsers. |
| Pipeline | `services/document_intelligence/pipeline.py` | End-to-end orchestration. |
| Validation stack | `services/document_intelligence/validation_stack.py` | Layered view; base_validator stays authoritative. |
| Verification | `services/document_intelligence/verification.py` | Independent checks vs extraction. |
| Confidence | `services/document_intelligence/confidence.py` | Multi-dimension, rule-based overall. |
| Decisions | `services/document_intelligence/decisions.py` | Final status model. |
| Observability | `services/document_intelligence/observability.py` | Per-document debug record. |
| Column mapping | `services/column_mapping.py` | Layout-scoped header → canonical. |
| Hybrid schema | `services/hybrid_schema.py` | Fixed + dynamic fields; null≠0. |
| Phase 2 recovery | `services/recovery_runner.py` | Bounded recovery; unchanged. |

---

## Data flow

1. **Route** — classify upload (`file_kind`, `pdf_kind`, `requires_ocr`).  
2. **Evidence** — lightweight native evidence (no image byte retention by default).  
3. **OCR policy** — digital native PDFs short-circuit; images/scanned PDFs mark OCR lane. Document-page OCR uses PaddleOCR-VL, then Gemini only when that output is unusable. Parser crops and whitelist reads stay on Tesseract. Ensemble comparison is not used.  
4. **Layout** — structural sketch from tokens/pages; known layouts remain preferred via named parsers.  
5. **Strategy** — registry ranks named_parser > known_layout > generic > vision > recovery.  
6. **Extract** — existing `_parse_*` cascade + sanitize + stock identity + Gemini fallback (existing rules).  
7. **Phase 1** — normalize + `validate_sales_result`.  
8. **Verify** — independent checks (identity, null integrity, dynamic/canonical guard, OCR disagreement).  
9. **Recover** — Phase 2 only when enabled; must revalidate.  
10. **Decide** — attach `document_intelligence` / `phase4` block on the response.

---

## OCR strategy

Production defaults are `OCR_ENGINE=paddleocr-vl` and `OCR_FALLBACK_ENGINE=gemini`.

- Document-page OCR runs PaddleOCR-VL. Gemini transcribes the page only when that output is unavailable, raises, returns an error, or is blank. The Gemini result is labeled `engine=gemini` and `unverified_fallback=true`.
- Low confidence does not start a second engine. `OCR_ENSEMBLE_ENABLED` is ignored.
- Sales-statement image probes and layout crops disable that Gemini transcription fallback so the existing vision cascade and Tesseract geometry reads stay in place.
- Explicit `OCR_ENGINE=tesseract` remains a diagnostic primary. Digit-whitelist crops and `image_to_data` stay on Tesseract.
- Digital PDF with reliable native text: **do not** run OCR in the pipeline.

The original Phase 4 draft used `OCR_ENGINE=tesseract` and `OCR_FALLBACK_ENGINE=paddle`. That is not the current production policy. The ZA 181 accuracy note in `phase4/za_181_accuracy/final_engine_decision.md` is a historical record and still names Tesseract as the rollback considered by that study.

---

## Layout strategy

- Visual docs: bbox-oriented column/row banding.  
- Digital PDFs: native evidence coordinates when present.  
- Spreadsheets: cell-oriented layout id.  
- Known layouts: named parsers win; layout module is observability + unknown fallback.

---

## Parser strategy

1. Explicit / high-confidence existing parser  
2. Known layout strategy  
3. Generic structured extraction  
4. Vision/LLM structural extraction (targeted only)  
5. Recovery  

Registry **wraps** parsers; it does not rewrite them.

---

## Validation strategy

Layers (view only; authority unchanged):

A. Schema  
B. Evidence  
C. Structural  
D. Column mapping  
E. Numeric  
F. Business identity  
G. Duplicate-row  
H. Cross-field  
I. OCR/evidence consistency  

`base_validator.validate_sales_result` and stock identity profiles remain authoritative. Validators are never weakened for PASS rate.

---

## Recovery strategy

Existing Phase 2 (`recovery_policy` + `recovery_runner`) stays.

Conceptual order: better evidence → better OCR → targeted re-extraction → vision/LLM → re-validation.

Provider failures remain provider failures. Recovery candidates must revalidate before VERIFIED.

---

## Unknown-document strategy

If no named parser is confident:

Evidence → LayoutUnderstanding → generic_structured strategy → dynamic fields for unknown headers.

Gemini/vision only when visual reasoning is required. Blank → null. Printed zero → zero. Never fabricate quantities.

---

## Confidence model

Dimensions:

- `schema_confidence`  
- `evidence_confidence`  
- `ocr_confidence`  
- `layout_confidence`  
- `mapping_confidence`  
- `business_validation`  
- `cross_engine_agreement`  

Overall is **rule-based**. High business score cannot compensate for missing evidence or OCR disagreement.

---

## Final decision model

| Status | Meaning |
|--------|---------|
| VERIFIED | Schema + evidence + business + verification OK; no unresolved contradiction |
| RECOVERED_AND_VERIFIED | Recovery applied and revalidated |
| UNRESOLVED | Correctness cannot be proven (valid outcome) |
| PROVIDER_ERROR | External provider failure |
| EXTRACTION_ERROR | Extraction hard failure |

Never: “PASS because parser returned data.”

---

## Safety (non-negotiable)

- Never invent values  
- Never turn null into zero  
- Never present calculated qty as printed  
- Never borrow neighboring row/column  
- Never globally promote canonical aliases  
- Never use arithmetic alone to justify unsupported OCR  
- Never silently overwrite parser output with LLM output  
- Printed evidence wins over inference  

---

## Backward compatibility

- API / route contracts unchanged  
- Named parsers unchanged  
- Phase 1 / 2 / 3 regression suites must continue to pass  
- Invoice extraction untouched  
- Phase 3 layout fixes remain stable  

---

## Performance short-circuits

- Digital reliable PDF → no pipeline OCR  
- Known high-confidence parser → no vision  
- Strong extract + validation → no recovery  
- Secondary OCR only when ensemble policy requires it  
- Gemini only for targeted recovery / unknown structural cases  
