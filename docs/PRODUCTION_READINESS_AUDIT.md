# Production Readiness Audit — split-extract Reliability (Updated)

**Date:** 2026-07-11 (post-fix)  
**Scope:** Reliability layer after audit remediation  
**Code changes:** Fixes only (no new features)

---

## Executive verdict

**Ready for staged production rollout** with all reliability flags **disabled** (unchanged behaviour).

**Watchdog can be enabled in production with `OCR_WATCHDOG_STUCK_SECONDS=600`** after also enabling `OCR_MID_EXECUTION_HEARTBEAT_ENABLED=true` during a short observation window. Do **not** enable `OCR_TESSERACT_CALL_TIMEOUT_ENABLED` unless you accept changed extraction routing for slow pages.

---

## Fixes applied

| ID | Issue | Fix | Files |
|----|-------|-----|-------|
| C-1 | Gemini counter leak on error/retry | `release_gemini_inflight_counter()` on all non-success paths in `call_gemini_with_quota` | `app.py:706-748`, `reliability.py:209-214` |
| C-2 | Counters not reset at request end | `reset_active_worker_counters()` + full heartbeat/metadata reset in `on_request_end()` | `reliability.py:217-228`, `:320-352` |
| H-3 | Timeout vs OCR behaviour unclear | Documented in `.env.example`, `run_tesseract_call` docstring; default remains **off** | `.env.example:64-66`, `reliability.py:716-727` |
| M-4 | Dual-lock heartbeat race | All `_heartbeat_state` + worker counters under `_heartbeat_lock` only | `reliability.py:271-310` |
| M-5 | `_request_progress` / heartbeat divergence | `register_request_progress_sync()` + `_sync_request_progress()` from `pulse_heartbeat` | `reliability.py:180-198`, `app.py:488-507` |
| M-6 | `request_id` missing in worker logs | `copy_context()` for OCR thread pool submissions | `app.py:18729-18740` |
| M-7 | `/ready` expensive psutil | `get_resource_snapshot(lightweight=True)`; no `open_files()` unless watchdog diagnostics | `reliability.py:365-407`, `:402-428` |

**Tests:** 18 pass (`tests/test_reliability.py` includes counter reset and Gemini release cases).

---

## Checklist (updated)

| # | Criterion | Result |
|---|-----------|--------|
| 1 | `update_request_progress()` thread-safe | ✅ Pass |
| 2 | Heartbeat fields protected | ✅ Pass (single `_heartbeat_lock`) |
| 3 | OCR/GPT counters paired | ✅ Pass (Gemini release + request-end reset) |
| 4 | Semaphore acquire/release | ✅ Pass (unchanged) |
| 5 | Tesseract child cleanup | ⚠️ Partial (watchdog only; timeout orphan subprocess if timeout flag enabled) |
| 6 | Watchdog/worker races | ✅ Pass (with accurate counters) |
| 7 | No exit during live OCR/GPT | ✅ Pass when counters accurate + mid-heartbeat enabled |
| 8 | `/live` `/ready` non-blocking | ✅ `/live` instant; `/ready` lightweight |
| 9 | Structured logging safe | ✅ Pass (filter wrapped in try/except) |
| 10 | Watchdog cleanup deadlock-free | ✅ Pass (bounded 2s child wait) |
| 11 | Flags default safe | ✅ Pass |
| 12 | All flags → same OCR/parsing | ❌ Fail only if `OCR_TESSERACT_CALL_TIMEOUT_ENABLED=true` or watchdog kills process |

---

## Remaining issues

### Medium

| Issue | Location | Notes |
|-------|----------|-------|
| Tesseract timeout orphan subprocess | `reliability.py:768-771` | Only when `OCR_TESSERACT_CALL_TIMEOUT_ENABLED=true`. Inner thread may continue after timeout. |
| `request_progress_lock` held during logging | `app.py:361-417` | Performance only; does not affect correctness. |
| `/ready` still acquires brief locks + psutil memory | `reliability.py:402-428` | Much lighter than before; acceptable for probes with timeout. |

### Low

| Issue | Location | Notes |
|-------|----------|-------|
| `tesseract_waiting_count` if semaphore blocks forever | `app.py:567-578` | Pre-existing; unrelated to reliability flags. |
| `ocr_page_started` always emitted | `app.py:15323` | Observability only. |
| Process metric `tesseract_calls_total` not reset per request | `reliability.py:737-739` | Intentional (process-lifetime counter). |

---

## Watchdog production guidance

### Safe to enable (recommended sequence)

1. Deploy with all flags **OFF** — no behaviour change.
2. Enable `OCR_STRUCTURED_LOGGING_ENABLED=true`.
3. Enable `OCR_MID_EXECUTION_HEARTBEAT_ENABLED=true` and `OCR_HEARTBEAT_TICK_SECONDS=30`.
4. Monitor `/ready` heartbeat ages for several days on real traffic.
5. Enable watchdog:

```bash
OCR_WATCHDOG_ENABLED=true
OCR_WATCHDOG_STUCK_SECONDS=600
OCR_WATCHDOG_INTERVAL_SECONDS=30
```

### Why 600 seconds

- Allows long per-page Tesseract without false termination when mid-OCR heartbeat is enabled.
- Watchdog only exits when heartbeat is stale **and** OCR/GPT/Gemini counters are all zero (`reliability.py:553-573`).
- Tune down toward 300s only after observing P99 `heartbeat_age` in production logs.

### Do not enable without understanding

```bash
OCR_TESSERACT_CALL_TIMEOUT_ENABLED=true   # CHANGES extraction behaviour
```

---

## Explicit statement

> **Watchdog can be enabled in production with `OCR_WATCHDOG_STUCK_SECONDS=600`**, provided `OCR_MID_EXECUTION_HEARTBEAT_ENABLED=true` is enabled first and `OCR_TESSERACT_CALL_TIMEOUT_ENABLED` remains **false**.

Critical counter-leak blockers (Gemini release, request-end reset, unified locking) are resolved. Remaining items are documented limitations, not deployment blockers for observability + watchdog at 600s.
