# Controlled batch evaluation plan (secondary OCR repair)

**Status:** Prepared — wiring gate cleared (`WIRING VERIFIED — READY FOR 385-DOCUMENT EVALUATION`).  
**Prerequisite:** See `PRODUCTION_READINESS_REPORT.md`. Prefer ≤65 controlled sample before full 385 if ops risk is a concern.

## 1. Datasets

| Dataset | Role | Notes |
| --- | --- | --- |
| SOURCE_VERIFIED GT-20 (`phase4/ground_truth_50/per_document`) | Accuracy + regression truth | Already used in expanded canary |
| 385-image stock archive (`prerana` folder from `image dataset stock.zip`) | Layout diversity / operational soak | **No source GT** — report predictions + repair metadata only; never claim field accuracy |

Locate the 385-image root via `--image-root` (preferred) or env:

```text
python scripts/run_secondary_repair_controlled_batch.py --image-root "C:\Users\amnsa\Downloads\image dataset stock" --all-385 --resume

rem optional fallback:
set SPLITEXTRACT_CANARY_IMAGE_ROOT=<path-to-prerana-or-extracted-zip>
```

If neither `--image-root` nor the env var is set, the batch runner fails closed (does not invent a path).

## 2. Cohorts (group before running)

Assign each document to exactly one primary cohort (first match wins):

1. `repair_himalaya` — VL/HTML has Op.Bal + Expiry Breakage  
2. `repair_total_footer` — Op.Bal + Issue + Closing Balance, missing numeric TOTAL row  
3. `repair_liv_strike` — LIV-52 SYRUP 100ML present with SSA-like Opening/Issues/Closing  
4. `compatible_himalaya_qty` — Op.Bal layout, no expected repair  
5. `compatible_ssa` — Opening/Issues/Closing layouts (incompatible with TOTAL footer repair)  
6. `ordinary` — other stock tables  
7. `ambiguous_poor_quality` — low OCR text length / high garbage token rate (heuristic)  
8. `negative_unresolved` — known GT MISSING fields (`0209`, `0124`) when present

Sample plan for the **next** controlled evaluation (not full 385):

| Cohort | Target count | Source |
| --- | ---: | --- |
| All GT-20 | 20 | mandatory baseline reconfirm |
| repair_* extras from 385 | up to 15 | heuristic pre-scan OFF text only |
| compatible_ssa / ordinary | up to 20 | random stratified |
| ambiguous_poor_quality | up to 10 | heuristic |
| **Total next batch** | **≤ 65** | checkpointed |

## 3. Protocol (identical inputs OFF vs ON)

For each document id:

1. Force `OCR_SECONDARY_REPAIR=false` → run entrypoint under test → save output A  
2. Force `OCR_SECONDARY_REPAIR=true` → same bytes/filename → save output B  
3. Restore `OCR_SECONDARY_REPAIR` unset/false  
4. Diff fields/quantities; attach repair metadata (redacted snippets in API logs; full evidence only in canary artifact store)

**Entrypoint choice (explicit):**

- Wiring verified (`PRODUCTION_READINESS_REPORT.md`: `WIRING VERIFIED — READY FOR 385-DOCUMENT EVALUATION`). Prefer DI / API entrypoint OFF vs ON on identical bytes so `line_items`/`totals` deltas are scored.  
- Offline fallback: evaluate `maybe_apply_secondary_repairs` + GT match (expanded canary).
- After wiring: evaluate `extract_sales_statement` / DI pipeline end-to-end product rows.

## 4. Artifacts (required per doc)

```text
phase4/secondary_repair_canary/controlled_batch/<run_id>/
  manifest.json              # ids, cohort, bytes sha256, source path
  checkpoints.jsonl          # resume cursor
  per_document/<id>.json     # off/on outputs, deltas, latency, repair meta
  summary.json
  REPORT.md
```

Deduplicate by `sha256(file_bytes)` + filename stem. Skip already-complete ids on resume.

## 5. Metrics (separate verified vs unverified)

**SOURCE_VERIFIED subset only:**

- field / quantity correct counts OFF vs ON  
- verified corrections, incorrect repairs, regressions  
- unresolved/missing rate  

**Unverified 385 sample:**

- repair trigger rate by cohort  
- delta counts (populated / changed / cleared)  
- errors / exceptions  
- secondary OCR latency  

Do **not** publish a single “accuracy %” mixing the two.

## 6. Stop conditions (halt batch immediately)

Stop and keep flag OFF if any of:

- incorrect repair vs SOURCE_VERIFIED expected value  
- regression (correct → incorrect)  
- incompatible-layout edit with corrected repair types  
- unresolved/missing field silently populated without source evidence  
- exception that replaces original OCR text  
- repair metadata missing type/field provenance for a changed value  

## 7. Commands (next controlled run — do not execute full 385)

```text
rem 1) GT-20 reconfirm only (production DI entrypoint)
python scripts/run_secondary_repair_controlled_batch.py --gt20-only

rem 2) Full 385-document evaluation
python scripts/run_secondary_repair_controlled_batch.py --image-root "C:\Users\amnsa\Downloads\image dataset stock" --all-385 --resume

rem Optional capped sample:
python scripts/run_secondary_repair_controlled_batch.py --image-root "C:\Users\amnsa\Downloads\image dataset stock" --max-docs 65 --resume

rem Flag is forced OFF inside the runner and restored OFF on exit.
```

## 8. Rollback

See rollback section in `PRODUCTION_READINESS_REPORT.md`: unset/set `OCR_SECONDARY_REPAIR=false` — no deploy required.
