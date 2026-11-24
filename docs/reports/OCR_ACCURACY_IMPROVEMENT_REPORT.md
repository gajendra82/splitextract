# OCR Accuracy Improvement Report

**Date:** 2026-10-12 (audit + TOTAL-footer harden + feature flag)  
**Decision:** **WIRING VERIFIED — READY FOR 385-DOCUMENT EVALUATION**  
**Prior gates:** feature-flagged canary → expanded GT-20 OFF/ON canary (`EXPANDED CANARY PASS`) → production wiring + real DI entrypoint proofs per repair type.  
**Readiness:** `phase4/secondary_repair_canary/PRODUCTION_READINESS_REPORT.md`  
**Scope:** Stock-statement PaddleOCR-VL pipeline — correctness first.  
**Claim discipline:** Metrics are **SOURCE_VERIFIED GT-20** (388 fields, 20 docs). Not 100% extraction accuracy. Not a score for the 385 `prerana` images (no source GT yet).

---

## 1. Git / workspace

- Branch: `sahil_new_architecture` (repo root = `splitextract/`)
- Unrelated dirty files left untouched (`app.py`, `sales_statement_extractor.py`, QA modules, etc.)
- This pass touched: `services/ocr/secondary_repair.py`, `services/document_intelligence/pipeline.py`, `tests/test_secondary_repair.py`, `.env.example`, this report, GT eval artifacts under `phase4/`

---

## 2. Audit findings (secondary OCR)

| Rule | Evidence / gate | False-positive risk | Status |
| --- | --- | --- | --- |
| TOTAL footer | Requires **positive Himalaya qty layout** (Op.Bal + Issue + Closing Balance); prefers bottom-band OCR; rejects sales-value TOTAL context; no invented headers; no generic `opening`/`closing` aliases | Was highest risk — **hardened this pass** | OK on GT-20 (`1151` only) |
| Himalaya Closing/Expiry | Requires `Expiry Breakage` + VL Op/Issue anchors + digit pattern | Short layouts skipped | OK (`1078`) |
| LIV-52 100ML Closing | Product+packing; page OCR; reject lone `162` bleed; clear on ambiguity | Single wrong candidate still possible in theory | OK (`0275`); no GT-20 regression |

**Production wiring:** DI applies `maybe_apply_secondary_repairs` (flag-gated), supplies repaired text via `ocr_text_override`, and overlays marked cells onto `line_items`/`totals`. Gated by `OCR_SECONDARY_REPAIR` (**default `false`**). Offline canaries may still call `apply_secondary_repairs` directly for scoring.

---

## 3. Fixes this pass

1. **TOTAL footer layout ID** — refuse unless VL headers prove Himalaya qty layout.  
2. **No default header invention** — missing VL headers → unresolved.  
3. **Sales-value TOTAL reject** — context regex blocks fabrication from amount footers.  
4. **Bottom-band first** — page-wide TOTAL match only after layout ID.  
5. **Feature flag** — `OCR_SECONDARY_REPAIR=false` in `.env.example`; DI pipeline no-op when off.  
6. **Negative tests** — unrelated sales TOTAL, incompatible Opening/Closing layout, blank cells, flag-off, layout ID unit checks.

---

## 4. Commands actually run

```text
python -m unittest \
  tests.test_secondary_repair \
  tests.test_ground_truth_eval \
  tests.test_paddle_vl_primary_safety \
  tests.test_phase4_architecture \
  tests.test_paddle_vl_engine \
  tests.test_tesseract_vs_paddle_vl_gt20 \
  tests.test_ssa_opening_receipt_dump_image
→ Ran 97 tests — OK

python scripts/diagnostics/_verify_secondary_gt20.py
→ VL-only 380/388; with secondary 385/388; improvements 5; regressions 0

python scripts/evaluate_paddle_vl_ground_truth.py
python scripts/score_paddle_vl_silent_wrong_gate.py
```

---

## 5. GT-20 metrics (reconfirmed from this run)

| Metric | Value |
| --- | ---: |
| Field accuracy | **99.2%** (385/388) |
| Quantity accuracy | **99.3%** |
| Accepted-field silent-wrong rate | **0.0%** (0/385) |
| Unresolved / missing-value rate | **0.8%** (3/388 MISSING) |
| Explicit gated-unresolved flag rate | **0.0%** |
| FULLY_CORRECT documents | **18 / 20** |
| PARTIALLY_CORRECT | **2** (`0209`, `0124`) |
| Secondary regressions vs VL-only | **0** |

Remaining MISSING (left UNRESOLVED — not guessed):

- `0209` `aact_op` / `aact_cl` (blank row; no reliable secondary `69`)
- `0124` `h_op` (unreadable header tokens)

Artifacts: `phase4/PADDLE_VL_GROUND_TRUTH_20_REPORT.md`, `phase4/paddle_vl_ground_truth_20_results.json`, `phase4/paddle_vl_silent_wrong_gate/`.

---

## 6. Remaining blockers (non-blocking for flagged canary)

| Item | Notes |
| --- | --- |
| `0209` / `0124` | Honest MISSING; do not equation-fill or invent headers |
| 385 `prerana` images | No SOURCE_VERIFIED GT — **no accuracy claim** until labeled |
| Production canary | Enable `OCR_SECONDARY_REPAIR=true` only on a small set; keep default off |

Shared HTML repairs (`parse_html_tables` / `iter_expanded_tables`) unchanged; HTM/HTML-Excel parser paths still share them.

---

## 7. Controlled canary (executed)

Command: `python scripts/run_secondary_repair_canary.py`  
Evidence: `phase4/secondary_repair_canary/CANARY_REPORT.md`

| Check | Result |
| --- | --- |
| Flag unset / false defaults OFF | Pass |
| DI pipeline OFF path unchanged | Pass |
| Accidental `0`/`no` do not enable | Pass |
| 5-doc OFF vs ON | 5 improvements, 0 regressions, 0 stop conditions |
| Ordinary `0084` unmodified | Pass |
| Negative `0209` blanks stay MISSING | Pass |
| Flag left OFF after run | Pass |

## 8. Expanded controlled canary (executed)

Command: `python scripts/run_secondary_repair_expanded_canary.py`  
Evidence: `phase4/secondary_repair_canary/expanded/EXPANDED_CANARY_REPORT.md`

| Metric (GT-20 OFF→ON) | Value |
| --- | ---: |
| Docs | **20** |
| Fields correct | **380 → 385** / 388 |
| Verified corrections | **5** |
| Incorrect repairs | **0** |
| Regressions | **0** |
| Unchanged fields | **383** |
| Unresolved MISSING (ON) | **3** |
| Docs with repairs | **3** (1078, 1151, 0275 only) |
| Secondary OCR time (sum ON) | ~57.6 s (~2.9 s/doc avg) |
| Flag restored OFF | **yes** |

No new failure modes → no additional regression tests beyond existing `tests/test_secondary_repair.py`.

## 9. Production readiness (this follow-up)

Wiring blocker cleared: repaired quantities reach final DI extraction/`line_items` when flag ON (GT-20 DI eval: 5 corrections, 0 regressions). Flag remains default OFF.

See: `phase4/secondary_repair_canary/PRODUCTION_READINESS_REPORT.md`, `CONTROLLED_BATCH_PLAN.md`.

## 10. Decision

**WIRING VERIFIED — READY FOR 385-DOCUMENT EVALUATION**

Next: run the 385-document evaluation (optional ≤65 controlled sample first per `CONTROLLED_BATCH_PLAN.md`). Flag remains OFF by default.
