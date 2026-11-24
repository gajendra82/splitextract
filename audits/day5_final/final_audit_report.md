# Day 5 — Final Audit & Production-Readiness Decision

**Decision: B — READY WITH DOCUMENTED LIMITATIONS**  
**Day 5 status: CLOSED**  
**HEAD:** `cc0bd73` (all Days 1–5 work remains uncommitted by policy)

---

## Executive summary

The extraction engine was re-measured end-to-end on representative digital, scanned, image, and schema-class documents under the 600 s sales deadline. Digital formats that map cleanly verify with source grounding. Scanned and difficult OCR inputs stay **UNRESOLVED** when evidence is insufficient — including zero-row DI-override results that do **not** invent stock quantities. One P0 deadline-cleanup defect in `sales_tesseract_slot` was fixed and covered by regression tests. Full suite: **Ran 1,268, skipped 159, failures 0, 153.269 s**.

This is not a claim of 100% accuracy on arbitrary documents.

---

## Repository and environment baseline

| Item | Value |
| --- | --- |
| Git HEAD | `cc0bd73` |
| Prior work | Days 0–4 changes preserved (no reset/clean/stash/commit/push) |
| Python | 3.12.6 (`.venv`) |
| CPU | AMD Ryzen 5 7535HS (12 logical) |
| RAM | 15.31 GB |
| GPU | RTX 3050 6 GB, driver 551.76 |
| Sales deadline | 600 s |
| Day 5 Phase 2 | `false` (live Gemini not timed; failure paths tested separately) |
| Day 4 reference | Ran 1,259 / skipped 159 / 0 failures / 152.413 s |
| Day 4 evidence | `audits/day4_performance/report.json` |

---

## Day 0–4 milestone summary

| Day | Focus | Outcome |
| --- | --- | --- |
| 0–1 | Accuracy/safety: nullable numbers, evidence before VERIFIED, health/live/ready | CLOSED |
| 2 | OCR runtime (PaddleOCR-VL), cache, ClosStock, 0124 override, production images | CLOSED |
| 3 | Schema integrity; predefined/hybrid/fully dynamic; disposable JSONB | CLOSED |
| 4 | Performance: digital concurrency, VL cold/warm, LRU cache, shared orchestrator | CLOSED |
| 5 | Final e2e audit, multipage scanned, regression, readiness decision | **CLOSED** |

---

## End-to-end extraction measurements

All wall times below are complete `extract_sales_statement` runs (route → OCR if needed → parse → validate → decide). Phase 2 Gemini was disabled. Scanned/image runs used `OCR_SECONDARY_REPAIR=true` to exercise DI OCR override.

| Label | File | Seconds | Rows | Status | Reason | Verified | Within 600s |
| --- | --- | ---: | ---: | --- | --- | --- | --- |
| xlsx | `0000711172…xlsx` | 1.749 | 16 | VERIFIED | schema_evidence_business_ok | true | yes |
| xls | `0000700848…XLS` | 0.264 | 20 | VERIFIED | schema_evidence_business_ok | true | yes |
| docx | `0000713511…docx` | 0.419 | 55 | VERIFIED | schema_evidence_business_ok | true | yes |
| txt | `0000732705…TXT` | 0.011 | 20 | VERIFIED | schema_evidence_business_ok | true | yes |
| html | `0000734246…html` | 0.288 | 44 | UNRESOLVED | stock_identity_failure | false | yes |
| digital_pdf | `0000734513…pdf` | 0.425 | 49 | UNRESOLVED | stock_identity_failure | false | yes |
| scanned_pdf_1page | `0000700124…pdf` | 276.394 | 0 | UNRESOLVED | insufficient_source_evidence | false | yes |
| image_png | `0000727190…png` | 109.487 | 25 | UNRESOLVED | identity_profile_unknown | false | yes |
| multipage_scanned_2p | `0000700037…pdf` | 343.238 | 0 | UNRESOLVED | insufficient_source_evidence | false | yes |

Notes:

- HTML / digital PDF unresolved matches Day 2 printed-zero / identity policy — not coerced to VERIFIED.
- Scanned 0124 method `di_ocr_html_override`, 0 rows: override prevented crop inventing quantities.
- PNG used DI override; unresolved, not verified.
- Pipeline timings for digital docs show extraction dominating; OCR policy near zero when native text short-circuits.

Schema-class synthetic documents (predefined / hybrid / fully dynamic) were validated via API builders (see schema results). Live Gemini recovery latency was not measured in this pass.

---

## Multi-page PDF results

### Live 2-page scanned fixture

- File: `0000700037_2026_08_ZA_06_609_04092026172645.pdf`
- Pages: **2**, native text chars `[0, 0]`
- Wall: **343.238 s**, within 600 s deadline
- Result: 0 rows, `UNRESOLVED` / `insufficient_source_evidence`, verified false
- DI override env on; method `di_ocr_html_override`
- No fabricated stock zeros from failed pages

A 5-page zero-text fixture exists on disk but was **not** fully VL-extracted in Day 5 to avoid uncontrolled GPU/time cost (~87 s/page class cold OCR from Day 4). Limitation documented.

### Override / page-order probe (no GPU)

- Mocked OCR pages `[1,2,3]` preserved order; no page drop (`reliability_results.json`).

### Unit: unreadable override skips crop cascade

- Covered in `tests/test_day5_final_audit.py` / existing secondary-repair wiring tests.

---

## Schema integrity (predefined / hybrid / fully dynamic)

From `schema_integrity_results.json`:

| Class | Result |
| --- | --- |
| Predefined | Validates known fields; null opening omitted; printed 0 kept; verified flag false at accept_document envelope |
| Hybrid | Unknown JSON (`हिंदी`, flags, nested) preserved; bad `sales_qty` → schema_error, not zero |
| Fully dynamic | `fixed is None`; original document preserved; API returns empty invoices + dynamic payload; not VERIFIED |
| Hard rejects | Unknown fixed field rejected; `NaN` rejected |

Production JSONB: **not claimed** (Day 3 disposable only).

---

## Source grounding and verification

From `source_grounding_results.json`:

- **Critical findings: 0**
- No document was VERIFIED with `blocks_verification` or `insufficient_source_evidence`
- VERIFIED digital docs had grounding `status=checked`, discrepancies 0 (where recorded)
- UNRESOLVED paths retained null openings / did not invent VERIFIED from schema alone
- Footer-as-product protection remains from Day 2 (`Amount TOTAL` skip) — not re-broken in this audit

---

## Reliability and security

| Check | Result |
| --- | --- |
| OCR bounded timeout | Pass |
| Gemini 429/503 error type | Pass |
| Retry bound ≤ 5 | Pass |
| ContextVar OCR override isolation | Pass |
| Partial batch (one bad, two good) | Pass |
| Upload max body 200 MB | Pass |
| Deadline default 600 s | Pass |
| OCR cache max 8 (LRU) | Pass |
| Pipeline reuses process orchestrator | Pass |
| **P0 fix:** `sales_tesseract_slot` exception cleanup | Fixed + tested |

---

## Full regression comparison

| Suite | Ran | Skipped | Failures | Errors | Duration |
| --- | ---: | ---: | ---: | ---: | ---: |
| Day 4 baseline | 1,259 | 159 | 0 | 0 | 152.413 s |
| Day 5 final | 1,268 | 159 | 0 | 0 | 153.269 s |

- Delta ran: **+9** (`tests/test_day5_final_audit.py`)
- Delta skipped: **0**
- No failures after the deadline-slot fix

---

## Files modified and reasons

| File | Reason |
| --- | --- |
| `services/sales_extraction_runtime.py` | P0: `sales_tesseract_slot` caught body exceptions and yielded again → `RuntimeError: generator didn't stop after throw()` on deadline; import failure still falls back once |
| `tests/test_day5_final_audit.py` | Deadline, multipage order, override skip, schema, reliability |
| `scripts/day5_final_audit.py` | E2E audit harness |
| `audits/day5_final/*` | Evidence artifacts |

Day 1–4 code changes were preserved; no architecture rewrite; no new DB/queue/OCR-artifact store.

---

## Remaining risks and limitations

See `known_limitations.md`. Highest operational risks:

1. VL OCR cost and 6 GB VRAM ceiling  
2. Scanned multipage may OCR successfully yet still yield 0 mapped rows → correctly unresolved  
3. No production JSONB  
4. Phase 2 Gemini not load-tested live in Day 5  

---

## Reproducible commands

```powershell
cd "C:\Users\amnsa\OneDrive\Desktop\zydus v1\splitextract"
Remove-Item Env:OCR_EXECUTION_TIMEOUT_S, Env:OCR_ENGINE, Env:OCR_SECONDARY_REPAIR, Env:PHASE2_RECOVERY_ENABLED, Env:RUN_LIVE_EXTRACTION -ErrorAction SilentlyContinue
$env:PHASE2_RECOVERY_ENABLED='false'
.\.venv\Scripts\python.exe -m unittest tests.test_day5_final_audit -v
.\.venv\Scripts\python.exe scripts\day5_final_audit.py
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Optional skips: `DAY5_SKIP_IMAGE=1`, `DAY5_SKIP_MULTIPAGE_LIVE=1`.  
Live Gemini: `DAY5_LIVE_GEMINI=1` (not used for Day 5 timing evidence).

---

## Production-readiness decision

### B — READY WITH DOCUMENTED LIMITATIONS

**Why not A:** GPU/OCR throughput, multipage scanned mapping gaps, absent production DB, and Phase 2 live cost/latency are material operational limits. Accuracy on arbitrary scans is not guaranteed.

**Why not C:** No blocking defect remains after the deadline-slot fix. Critical grounding integrity held (0 critical). Digital verification paths work. Unsafe OCR outcomes stay unresolved. Full regression is green.

Deploy as a **controlled production** service: concurrency 1 for VL / API default, digital concurrency ≤ 2, monitor unresolved rates, keep Phase 2 optional, and do not treat unresolved or zero-row OCR documents as verified stock.
