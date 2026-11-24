# PaddleOCR-VL 1.6 Latency Profile Report

**Date:** 2026-10-15  
**Dataset sample:** 5 images from `C:\Users\amnsa\Downloads\image dataset stock`  
**Tesseract operational baseline (full set):** `20261013T385tess` (~55 min primary OCR for 385 docs)  
**VL partial run preserved:** `20261013T385vl` (~116/385 complete when paused for profiling)  
**Full 385 VL batch:** **not** launched during this task.

---

## 1. Root causes (evidence-backed)

| Finding | Evidence |
|---|---|
| **Dominant cost is warm VL inference**, not startup | Warm e2e **112–164 s/image**; worker ready **~0.2 s**; host preprocess **6–343 ms** |
| Keep-alive worker **already** reuses one model load | First `init_latency_ms` ≈ **13.6–15.7 s**; subsequent images reuse subprocess (`backend=subprocess`, `device=gpu:0`) |
| GPU is used; not CPU fallback | `resolved_device=gpu:0`, VRAM ~5.4–6.0 GiB during infer |
| CUDNN 9.9 (Paddle) vs 9.5 (host) warning persists | Logged in `vl_worker.stderr.log`; inference still succeeds — mismatch is risk, not measured root of the 2 min/page |
| Preprocess / IPC are minor | Downscale + base64 are seconds at most vs minutes of `predict` |
| Sequential only | One JSON request per image; no safe multi-image batch API in installed PaddleOCR 3.7 VL |

Code path (audit): keep-alive `_SERVE_PROC` in `services/ocr/paddle_vl_engine.py`; worker loop in `services/ocr/vl_worker.py`; batch timeout fence `max_workers=1` in `scripts/run_secondary_repair_controlled_batch.py`.

**Why slower than Tesseract:** VL is a generative layout/HTML OCR model on a 6GB laptop GPU. Raw Tesseract on the same five images is **0.5–10 s**. The tess baseline artifacts for those docs are **0.9–20 s**. VL is ~**15–240×** slower depending on the image, while producing much larger HTML/table text.

---

## 2. Five-image timings

Images (by size band): small → large (`0756`, `1924`, `6984`, `9726`, `8964`).

### Before (tag `baseline_max2048`) vs after pipeline flags (tag `opt_skip_orient_unwarp_chart`)

| Doc | Label | VL before (s) | VL after (s) | Δ (s) | Raw Tess (s) | Tess baseline artifact (s) | VL chars | Tess chars |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `...0756...` | cold | 122.1 | 126.4 | +4.3 | 0.5 | 0.9 | 5750 | 128 |
| `...1924...` | warm | 150.1 | 150.3 | +0.2 | 2.1 | 4.0 | 6316 | 2226 |
| `...6984...` | warm | 150.3 | 163.9 | +13.6 | 2.2 | 4.2 | 7978 | 2205 |
| `...9726...` | warm | 111.8 | 121.5 | +9.7 | 4.9 | 11.9 | 4318 | 1103 |
| `...8964...` | warm | 137.7 | 133.2 | −4.5 | 10.1 | 20.0 | 7071 | 1146 |

- **Warm mean:** 137.5 s → 142.2 s (**no material speedup**; within noise / document variance).
- **Cold:** worker ready ~211 ms; model init ~13.6 s (folded into first e2e).
- Artifacts:  
  - `phase4/paddle_vl_benchmark/latency_profile/profile_baseline_max2048.{json,md}`  
  - `phase4/paddle_vl_benchmark/latency_profile/profile_opt_skip_orient_unwarp_chart.{json,md}`

**Note on earlier fake Tess comparison:** `TesseractEngine` → `_a2z_tesseract()` → `PrimaryOCRPytesseractShim` routes `image_to_string` to VL when `OCR_ENGINE=paddleocr-vl`. The profiler was fixed to call **raw pytesseract** for fair latency compare. Do not use shimmed Tess timings.

---

## 3. GPU / CUDNN

- **GPU:** NVIDIA RTX 3050 Laptop 6GB, Driver 551.76 — actively used (`gpu:0`).
- **CUDNN:** Paddle built for **9.9**, machine reports **9.5** — warning only in this run; no measured CPU fallback.
- **VRAM:** ~5.4–6.0 GiB during infer (tight on 6GB).

---

## 4. Changes made

| Change | Rationale | Result |
|---|---|---|
| Keep-alive worker + UTF-8 `_emit` + process-tree kill (prior hang fix) | Crash/reload loops & timeout deadlock | Still required for stability |
| `PADDLE_VL_MAX_SIDE=2048` downscale (prior hang fix) | 12MP pages hung / poisoned cache | Prep cheap; avoids worst cases |
| Disable unused VL sub-pipelines by default: orientation / unwarping / chart (`_vl_pipeline_speed_kwargs`) | Avoid extra models on stock photos; keep layout ON | **No measurable warm latency win** on these 5 |
| Empty/error primary OCR cache not reused | Prevent poisoned resume | Guard + unit test |
| Profiler `scripts/profile_paddle_vl_latency.py` + raw Tess compare | Measurable cold/warm split | Used for this report |
| Tests `tests/test_paddle_vl_latency_guards.py` (+ existing primary safety) | Downscale, flags, cache, emit | **25 tests OK** |

**Not done (by design):** speculative concurrency, model weight swaps, blind further resolution cuts, accuracy claims without source GT.

---

## 5. Is a full 385 VL run safe to launch?

**Operationally: yes, with eyes open on wall-clock.**

| Gate | Status |
|---|---|
| Primary engine enforced | `--require-primary-engine paddleocr-vl` |
| Worker reuse | Confirmed warm path |
| Hang / Unicode / timeout deadlock mitigations | In tree |
| Empty-cache poison | Fixed |
| Pipeline flag “speedup” | Neutral on latency — keep as safe defaults, not a speed claim |
| Expected duration (warm ~130 s/doc) | ~**12–14 h** for remaining ~269 docs if mean holds; full 385 from scratch ~**14–16 h** |
| Accuracy | **Not** established for 385 (no source GT) |

Preserve `20261013T385tess`. Resume existing VL run rather than overwriting:

```powershell
$env:OCR_ENGINE = 'paddleocr-vl'
$env:PYTHONUNBUFFERED = '1'
$env:SECONDARY_REPAIR_PRIMARY_TIMEOUT_SEC = '1200'
$env:PADDLE_VL_TIMEOUT_S = '1200'
$env:PADDLE_VL_MAX_SIDE = '2048'
$env:OCR_ALLOW_UNVERIFIED_FALLBACK = 'false'
$env:OCR_ENSEMBLE_ENABLED = 'false'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

python -u scripts/run_secondary_repair_controlled_batch.py `
  --image-root "C:\Users\amnsa\Downloads\image dataset stock" `
  --all-385 `
  --require-primary-engine paddleocr-vl `
  --run-id "20261013T385vl" `
  --resume `
  --skip-gt20
```

**Remaining non-blockers:** CUDNN 9.5 vs 9.9 mismatch (reinstall/recompile Paddle for a clean stack if desired); intrinsic VL `predict` cost on 6GB GPU (needs different hardware or a different model class for Tess-like speed).

---

## 6. Bottom line

PaddleOCR-VL is slow here because **each page’s generative VL inference takes ~2 minutes on this GPU**, not because the worker reloads every image. Keeping the worker warm and skipping unused sub-pipelines are correct hygiene; they do **not** close the gap to Tesseract’s ~1–20 s/page. A full VL 385 soak is **safe to resume** for operational comparison artifacts; it will not finish in Tess-like wall time.
