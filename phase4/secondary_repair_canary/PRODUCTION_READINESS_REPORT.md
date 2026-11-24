# Secondary OCR repair — production readiness validation

**Date:** 2026-10-13  
**Baseline:** `expanded/EXPANDED_CANARY_REPORT.md` (`EXPANDED CANARY PASS`)  
**Wiring eval:** `production_wiring_eval/WIRING_EVAL_REPORT.md`  
**Final decision:** **`WIRING VERIFIED — READY FOR 385-DOCUMENT EVALUATION`**

---

## 1. Production flow (traced)

```text
extract_sales_statement(file_bytes, filename)
  → DocumentIntelligencePipeline.run
       1) route_document / evidence_for_route
       2) if requires_ocr:
            rasterize → ocr.recognize (primary VL)
            → _maybe_secondary_repair_bundle   # OCR_SECONDARY_REPAIR gate (default OFF)
                 PDFs: secondary Tesseract @ 2.5× from original PDF bytes
                 images: secondary on source image_bytes
                 mutates primary.text + returns marked cells
       3) layout / strategy
       4) if flag ON + OCR text:
            with ocr_text_override(repaired_text):
                extract_sales_statement_core(...)
                  → statement_from_ocr_html(override)  # skips Gemini/VL re-work
            overlay_secondary_repairs_onto_result(result, repaired_cells)
          else:
            extract_sales_statement_core(...)          # unchanged OFF path
       5) phase1 validate / verify
       6) phase2 recovery (if enabled)
            → re-apply overlay so recovery cannot wipe repairs
       7) decision / API payload (line_items / totals)
```

Native digital PDF / Excel: no OCR loop → no secondary → no overlay.

---

## 2. Wiring fixes this pass

| Change | Why |
| --- | --- |
| `statement_from_ocr_html` + core override short-circuit | DI OCR evidence reaches `line_items` without Gemini/VL hang |
| Overlay after core **and** after Phase 2 | Repaired qty survive recovery |
| PDF secondary re-raster @ 2.5× | 0275 LIV Closing fails at primary 2.0× |
| Secondary OCR uses raw Tesseract (no VL shim) | Prevents accidental Paddle VL worker start |
| Header aliases OpStk/Clstk | Ordinary layouts parse under override |

Unrelated dirty files preserved. Flag left **OFF**.

---

## 3. Real production-entrypoint proofs (per repair type)

Primary VL stubbed from stored benchmarks. **Real** secondary Tesseract. **Real** `extract_sales_statement_core` via DI override. Vision forbidden (raises if called). Timeout 120s/doc. **No** hung 1151 `extract_sales_statement_core` probe.

```text
python -m unittest tests.test_secondary_repair_production_wiring -v
→ Ran 14 tests — OK (~75s)

python -m unittest tests.test_secondary_repair -v
→ Ran 18 tests — OK
```

| Repair type | Doc | Final output proof | Result |
| --- | --- | --- | --- |
| Himalaya Closing/Expiry | 1078 | `line_items` Closing **69**, Expiry **12**; `di_ocr_override` | Pass |
| TOTAL footer | 1151 | `totals` Op **834578**, Cl **775274** | Pass |
| LIV printed Closing | 0275 | LIV-52 SYRUP 100ML Closing **37**; 200ML stays 37 | Pass |
| Ordinary | 0084 | OFF≡ON quantities | Pass |
| Unresolved | 0209 / 0124 | No fabricated 69 / no invented `h_op` | Pass |
| Flag OFF | 1078 | Closing stays 89; no overlay | Pass |
| Dup OCR prevention | 1078 | Vision call count = 0 when override set | Pass |
| Native PDF | synthetic | Secondary not invoked | Pass |
| Null vs zero | unit | Empty repair → `None` not `0` | Pass |

---

## 4. GT-20 OFF/ON (production DI entrypoint)

```text
python scripts/run_secondary_repair_production_wiring_eval.py
→ WIRING VERIFIED — READY FOR 385-DOCUMENT EVALUATION
```

| Metric | Value |
| --- | ---: |
| Scored fields | 331 |
| OFF correct | 142 |
| ON correct | 147 |
| Verified corrections | **5** |
| Regressions | **0** |
| Incorrect ON (with overlay) | **0** |

| Document | Field | OFF → ON |
| --- | --- | --- |
| 0275 | `liv100_cl` | 162 → **37** |
| 1078 | `aact_close` | 89 → **69** |
| 1078 | `talekt_exp` | 0 → **12** |
| 1151 | `tot_op` | None → **834578** |
| 1151 | `tot_cl` | None → **775274** |

Regressions: `tests.test_phase4_architecture` + `tests.test_paddle_vl_primary_safety` — 42 OK.

---

## 5. Flag / rollback

| Check | Result |
| --- | --- |
| Default OFF when unset | Pass |
| Truthiness matrix | Pass |
| OFF skips secondary OCR | Pass |
| `.env.example` | `OCR_SECONDARY_REPAIR=false` |
| Session restore | unset / `secondary_repair_enabled()=False` |

Rollback: `OCR_SECONDARY_REPAIR=false` (or unset) + restart workers.

---

## 6. Rollout checklist

- [x] Real DI entrypoint proofs for all three repair types  
- [x] Unresolved preserved; ordinary no inventive overlay  
- [x] Flag OFF default; duplicate vision prevented under override  
- [x] GT-20: 5 corrections, 0 regressions, 0 incorrect  
- [x] Native PDF short-circuit / null-vs-zero  
- [ ] **385-document evaluation** — gate cleared; run when ready  
- Prefer controlled ≤65 sample first (`CONTROLLED_BATCH_PLAN.md`) if ops risk is a concern  

---

## 7. Commands

```text
cd splitextract
set OCR_SECONDARY_REPAIR=
set OCR_ENGINE=tesseract

python -m unittest tests.test_secondary_repair_production_wiring tests.test_secondary_repair
python scripts/run_secondary_repair_production_wiring_eval.py

rem Optional controlled sample before full 385:
set SPLITEXTRACT_CANARY_IMAGE_ROOT=<prerana>
python scripts/run_secondary_repair_controlled_batch.py --max-docs 65 --resume

set OCR_SECONDARY_REPAIR=
```

Do **not** leave hung vision probes running; wiring tests enforce 120s timeouts.

---

## 8. Decision

**`WIRING VERIFIED — READY FOR 385-DOCUMENT EVALUATION`**

**Evidence:** For each repair type (Himalaya / TOTAL footer / LIV), a timed `DocumentIntelligencePipeline.run` with real secondary Tesseract and real `extract_sales_statement_core` (DI OCR override) delivered corrected quantities on final `line_items`/`totals`. GT-20 OFF/ON through the same entrypoint: 5 verified corrections, 0 regressions, 0 incorrect overlay changes. Unresolved fixtures stay unresolved. Flag remains OFF by default.
