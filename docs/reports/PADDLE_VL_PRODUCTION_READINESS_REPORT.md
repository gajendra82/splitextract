# PaddleOCR-VL Production Readiness Report

**Date:** 2026-10-09 (updated after blocker fixes)  
**Branch:** `sahil_new_architecture`  
**Scope:** Sales-statement OCR path readiness for a small pilot batch (not full dataset)

---

## Decision

**GO — SMALL PILOT BATCH**

**Should I start batch testing now?** **YES — small pilot only (≤5 OCR-required docs), not the full dataset.**

Live PaddleOCR-VL inference now returns text via the isolated keep-alive worker (~47s cold / ~4s warm). Recursive subprocess + stderr deadlock root causes are fixed. General `image_to_string` paths route through the primary orchestrator; digit-whitelist and `image_to_data` remain on Tesseract by design. Regression tests pass; worker processes do not leak after OCR.

---

## 1. Fixes applied

### 1.1 PaddleOCR-VL timeout (root cause)

| Cause | Fix |
|-------|-----|
| **Recursive subprocess:** `vl_worker` called `PaddleVLEngine.recognize()`, which selected `backend=subprocess` because `sys.executable` was the VL venv python → infinite self-spawn until timeout | Worker sets `PADDLE_VL_WORKER_CHILD=1` + `PADDLE_VL_ALLOW_INPROCESS=1`; engine refuses subprocess when child / same-python / force-inprocess |
| **stderr PIPE deadlock:** parent only read stdout; paddle logs filled stderr pipe and blocked the worker | Worker stderr redirected to `phase4/paddle_vl_benchmark/vl_worker.stderr.log` (not PIPE) |
| **Cold reload every page** | Keep-alive `--serve` worker: one model load, JSON-line IPC; `-u` unbuffered stdout |

### 1.2 Tesseract bypasses

| Change | Behavior |
|--------|----------|
| `services/ocr/pytesseract_bridge.PrimaryOCRPytesseractShim` | Wraps `_a2z_tesseract()` |
| General `image_to_string` | Routes through OCR orchestrator when primary is PaddleOCR-VL |
| `tessedit_char_whitelist=…` | Stays on Tesseract (intentional digit crops) |
| `image_to_data` | Stays on Tesseract (layout geometry) |
| VL primary failure | Does **not** silently fall through to Tesseract |

### 1.3 Host process overload

| Finding | Action |
|---------|--------|
| Thousands of pythons earlier were consistent with **recursive `vl_worker` self-spawn** | Recursion fix removes the leak source |
| During this fix pass: confirmed project workers (`vl_worker.py --serve`, hung smoke parent) | Stopped only those PIDs (command-line matched) |
| After successful smoke + `_kill_serve_proc()` | `vl_worker` count = **0** |

---

## 2. Live smoke test (single document)

**Sample:** `C:\Users\amnsa\Downloads\ZL_2026_August\0000700379_2026_08_ZL_18_375_05092026174136.PDF` page 0 (OCR-required)

**Command (host Python → VL venv keep-alive worker):**

```bash
python -u -c "<PaddleVLEngine.recognize on rendered page>"
```

| Metric | Result |
|--------|--------|
| Primary setting | `paddleocr-vl` |
| Backend | `subprocess` → `.venvs\paddleocr_vl\Scripts\python.exe` |
| Engine | `paddleocr-vl-1.6` |
| Status | **success** |
| Cold wall time | **47419 ms** (~47.4 s) |
| Warm (2nd page, same worker) | **4083 ms** (~4.1 s) |
| Text length | **217** chars (HTML table fragment returned) |
| Error | `None` |
| Workers after cleanup | **0** |

---

## 3. Tests

**Targeted + regression:**

```bash
python -m pytest tests/test_paddle_vl_primary_safety.py tests/test_pytesseract_bridge.py \
  tests/test_paddle_vl_engine.py tests/test_phase4_architecture.py \
  tests/test_phase1_architecture.py tests/test_phase2_recovery.py \
  tests/test_phase2b_recovery_contract.py tests/test_ground_truth_eval.py -q
```

| Result |
|--------|
| **132 passed**, 0 failed |

New coverage: recursive-subprocess block, same-python→inprocess, pytesseract bridge routing / whitelist / `image_to_data`.

---

## 4. Routing & safety (preserved)

| Check | Status |
|-------|--------|
| Default primary `paddleocr-vl` | PASS |
| Isolated venv subprocess keep-alive | PASS (live) |
| `OCR_ALLOW_UNVERIFIED_FALLBACK=false` | PASS |
| Rollback `OCR_ENGINE=tesseract` | PASS (unit) |
| Native digital PDF / Excel skip OCR | PASS |
| Column/cell gates + Phase 1/2 | Preserved |
| No silent VL→Tesseract verify | PASS |

---

## 5. Remaining non-blocking risks (pilot OK)

1. **Legacy stock-equation inference** still exists in some named parsers (`_ssa_dump_infer_opening_qty`, etc.). Phase 2 recovery contract forbids inference; pilot should flag identity failures as UNRESOLVED, not “fix by equation.”
2. **`image_to_data` still Tesseract** — intentional for bbox layout parsers; mixed-engine geometry vs VL text can disagree → prefer UNRESOLVED on conflict.
3. **Invoice `app.py` path** still Tesseract/Gemini — out of scope for sales VL pilot.
4. Cold start ~45–120s per worker process; keep-alive mitigates multi-page cost. Size pilot timeouts accordingly (`PADDLE_VL_TIMEOUT_S`, default now 300).

---

## 6. Changed files (this fix pass)

- `services/ocr/paddle_vl_engine.py` — recursion guards, keep-alive serve IPC, stderr-to-file
- `services/ocr/vl_worker.py` — in-process child + `--serve` mode
- `services/ocr/pytesseract_bridge.py` — **new** primary-OCR shim
- `services/sales_statement_extractor.py` — `_a2z_tesseract` wraps shim
- `tests/test_paddle_vl_primary_safety.py` — recursion / same-python tests
- `tests/test_pytesseract_bridge.py` — **new**
- `PADDLE_VL_PRODUCTION_READINESS_REPORT.md` — this update

Unrelated dirty/untracked files on the branch were left untouched.

---

## 7. Recommended pilot procedure

1. Ensure no leftover workers: no `vl_worker.py` processes.
2. Run 1–5 OCR-required docs with checkpointing (`scripts/test_paddle_vl_5docs.py` or extract pipeline with resume).
3. Settings: `OCR_ENGINE=paddleocr-vl`, `OCR_ALLOW_UNVERIFIED_FALLBACK=false`, `PHASE2_RECOVERY_ENABLED` as currently configured.
4. Stop if cold latency >>120s, worker count grows, or silent-wrong quantities appear without UNRESOLVED flags.
5. Do **not** start the full 100-doc / full dataset batch until the 5-doc pilot is clean.

---

## Final answers

| Question | Answer |
|----------|--------|
| Decision | **GO — SMALL PILOT BATCH** |
| Start full batch now? | **NO** |
| Start small pilot (≤5 OCR docs)? | **YES** |
