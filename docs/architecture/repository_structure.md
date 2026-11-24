# Repository structure

Production-grade layout for `splitextract` after the repository cleanup.
Deep physical relocation of `services/` into new top-level packages was **deferred**
to avoid breaking the extraction engine, OCR workers, and the large FastAPI surface
in `app.py`. Conceptual boundaries map onto the existing modules below.

## Final directory tree (active)

```
splitextract/
├── app.py                      # FastAPI application + invoice/sales API surface
├── runner.py                   # Keepalive-aware uvicorn entrypoint
├── requirements*.txt
├── Dockerfile
├── docker-compose.yml
├── services/                   # Production engine (keep import paths stable)
│   ├── config.py
│   ├── schemas.py / hybrid_schema.py
│   ├── sales_statement_extractor.py
│   ├── sales_extraction_runtime.py
│   ├── recovery_runner.py
│   ├── source_grounding.py / evidence*.py
│   ├── document_intelligence/  # pipeline, decisions, verification
│   ├── extraction/             # parser_registry
│   ├── ocr/                    # VL / classic / tesseract / orchestrator / workers
│   ├── ground_truth/
│   └── layout/
├── cloud_ocr/                  # Optional OCR sidecar worker (tested)
├── api entry                 → app.py + runner.py
├── tests/
├── scripts/                    # Benchmarks, Day N audits, ops helpers
│   └── diagnostics/            # Former root _probe_* / _verify_* scripts
├── qa/                         # Batch QA tooling
├── phase3/ / phase3_eval/      # Historical Phase-3 evaluation (test/script coupled)
├── phase4/                     # Historical Phase-4 benchmarks + GT (test/script coupled)
├── audits/                     # Day 0–5 evidence + archive + cleanup reports
│   ├── day4_performance/
│   ├── day5_final/
│   ├── archive/scratch/        # Former scratch experiments
│   └── repository_cleanup/
├── docs/
│   ├── architecture/           # This document
│   └── reports/                # Former root OCR/Paddle readiness markdown
├── deploy/                     # systemd + tmux helpers
├── samples/                    # Ad-hoc samples (not canonical fixtures)
├── test_images/ / test_pdfs/   # Canonical test fixtures
└── .venvs/                     # Isolated OCR interpreter venvs (local)
```

## Responsibility map (conceptual → physical)

| Boundary | Physical location | Notes |
|----------|-------------------|--------|
| `api/` | `app.py`, `runner.py` | Monolithic FastAPI app; do not split without a dedicated migration |
| `core/` | `services/config.py`, logging helpers in runtime | Shared settings / env |
| `ingestion/` | `services/file_type_detector.py`, `source_document.py` | Format detection / load |
| `extraction/` | `services/extraction/`, `sales_statement_extractor.py`, DI `pipeline.py` | Parsers + orchestration |
| `ocr/` | `services/ocr/` | Engines, workers, cache, concurrency |
| `intelligence/` | `recovery_runner.py`, `prompts.py`, `vertex_gemini_client.py`, Gemini modules | LLM recovery |
| `schemas/` | `schemas.py`, `hybrid_schema.py` | Predefined / hybrid / dynamic JSON |
| `validation/` | `source_grounding.py`, `document_intelligence/verification.py`, `base_validator.py` | Grounding + reconcile |
| `services/` | remaining application helpers | Metrics, cache, excel, layout |
| `tests/` | `tests/` | Unit / integration / regression |
| `scripts/` | `scripts/` | Diagnostics + Day audits |
| `audits/` | `audits/` | Historical evidence (preserve) |
| `docs/` | `docs/` | Ops + architecture |
| `deploy/` | `deploy/` | Docker/systemd/tmux |

## Main extraction execution flow

1. HTTP / CLI enters via `app.py` (`extract_sales_statement` and related routes).
2. `services.sales_extraction_runtime` owns request slots, deadlines, progress, and cancellation.
3. `DocumentIntelligencePipeline` (`services.document_intelligence.pipeline`) orchestrates accept → extract → verify → recover.
4. Specialized parsers live primarily in `sales_statement_extractor.py` and are selected via `services.extraction.parser_registry` / document routing.
5. Response shaping uses hybrid / dynamic schema helpers (`hybrid_schema.py`, `schemas.py`, app builders).

## OCR execution flow

1. Engine selection via env (`OCR_ENGINE`, default PaddleOCR-VL).
2. `services.ocr.orchestrator.get_orchestrator()` singleton coordinates engines.
3. PaddleOCR-VL (`paddle_vl_engine.py` + `vl_worker.py`); classic Paddle in isolated `.venvs/paddleocr` via `classic_worker.py` / `paddle_engine.py`.
4. Digit crops / fallbacks via Tesseract (`tesseract_engine.py`, `pytesseract_bridge.py`).
5. Image OCR LRU cache and GPU concurrency limits enforced in runtime / extractor paths.
6. Timeouts / cancellation / worker cleanup remain in OCR runtime modules (behavior unchanged by this cleanup).

## LLM recovery flow

1. Phase 1 / Phase 2 recovery through `recovery_runner.py` and DI decisions.
2. Gemini / Vertex clients and prompts under `services/` (`vertex_gemini_client.py`, `prompts.py`, fallbacks).
3. Evidence selection (`evidence_selector.py`) gates recovery; unresolved / provider error statuses preserved.
4. Secondary OCR repair (`services.ocr.secondary_repair`) can feed text overrides when enabled.

## Schema and verification flow

1. Predefined / hybrid acceptance: `hybrid_schema.accept_document` / `SalesExtractionOutput`.
2. Fully dynamic JSON preservation via app + recovery helpers (`ensure_json_value`, depth limits, NaN/Inf reject).
3. Source grounding (`source_grounding.py`) + stock identity profiles.
4. DI `verification.py` / `decisions.py` assign VERIFIED / RECOVERED_AND_VERIFIED / UNRESOLVED / errors.
5. Null vs zero, footer handling, and unresolved rules are unchanged by cleanup.

## Import / dependency boundaries

- Prefer `from services...` absolute imports (production + tests + scripts).
- Do **not** import production modules from `scripts/diagnostics`, `phase3*`, `phase4`, or `audits/`.
- `app` may be imported by runtime/tests for response builders and slots (existing coupling).
- `cloud_ocr` is a sibling package (`import cloud_ocr.app`) used by sidecar tests.
- `phase3_eval` / `phase4` paths are hard-coded in many scripts and a few tests — leave in place until a dedicated path-constant migration.

## Deferred migrations

Moving `app.py` into `api/`, or splitting `services/` into top-level `extraction/` / `ocr/` / `intelligence/` packages, requires a dedicated import-graph migration with full regression after each package. Deferred intentionally.
