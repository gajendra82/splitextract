# Reliability Self-Healing — Change Report

This document covers Phases 2–11 of the split-extract reliability work. Phase 1 analysis is in [RELIABILITY_CODE_ANALYSIS.md](./RELIABILITY_CODE_ANALYSIS.md).

All new behaviour is **feature-flagged** and **defaults to off**, preserving existing production behaviour until flags are enabled.

---

## 1. Internal watchdog (`services/reliability.py`)

| | |
|---|---|
| **WHY** | Process can remain alive with idle CPU and no Tesseract while a request is wedged; manual restart is the only recovery today. |
| **WHAT** | Daemon thread checks every `OCR_WATCHDOG_INTERVAL_SECONDS`. If a request is active and heartbeat age ≥ `OCR_WATCHDOG_STUCK_SECONDS`, logs diagnostics and calls `os._exit(1)` for process manager restart. |
| **RISK** | False positive if a legitimate long stage exceeds stuck threshold without heartbeat updates. Mitigated by rich heartbeat updates and configurable threshold. |
| **ROLLBACK** | Set `OCR_WATCHDOG_ENABLED=false` or remove env var; no code rollback required. |
| **CONFIG** | `OCR_WATCHDOG_ENABLED`, `OCR_WATCHDOG_INTERVAL_SECONDS`, `OCR_WATCHDOG_STUCK_SECONDS` |
| **TEST PLAN** | Enable watchdog with low stuck threshold in staging; verify exit + restart. Confirm disabled in prod until validated. |

---

## 2. Enhanced heartbeat (`services/reliability.py` + `app.py`)

| | |
|---|---|
| **WHY** | Existing heartbeat lacked resource counters and structured fields needed for watchdog and `/ready`. |
| **WHAT** | Parallel enhanced state: `request_id`, `invoice`, `stage`, `page`, `total_pages`, OCR/GPT/Gemini thread counts, `last_completed_step`, `heartbeat_age`. Updated via existing `update_request_progress()` hook. |
| **RISK** | Minimal — additive; legacy `_request_progress` unchanged. |
| **ROLLBACK** | No flag; data is ignored when watchdog/structured logging disabled. |
| **CONFIG** | Always on (read-only metadata). |
| **TEST PLAN** | Run `/split-and-extract`; inspect `/health` and `/ready` for stage progression. |

---

## 3. Health endpoints (`app.py`)

| | |
|---|---|
| **WHY** | Laravel needs non-blocking probes; `/health` can block behind sync OCR on single worker. |
| **WHAT** | `GET /live` → `{"alive": true}`. `GET /ready` → JSON readiness snapshot via `get_ready_snapshot()` (no semaphore acquire, no I/O). |
| **RISK** | `/ready` may report `ready: false` during long jobs — intended for orchestration, not user traffic. |
| **ROLLBACK** | Remove routes or ignore in load balancer. |
| **CONFIG** | None (always available). |
| **TEST PLAN** | `curl /live` and `/ready` during idle and during active OCR; confirm sub-100ms response. |

---

## 4. Semaphore safety logging (`app.py` + `reliability.py`)

| | |
|---|---|
| **WHY** | Leaked permits are a primary hang vector; need visibility without changing acquire/release logic. |
| **WHAT** | Optional acquire/release logging for `request_processing` and `tesseract_ocr` semaphores with active/waiting counts. Existing `finally` release paths unchanged. |
| **RISK** | Log volume when enabled. |
| **ROLLBACK** | `OCR_SEMAPHORE_LOGGING_ENABLED=false` |
| **CONFIG** | `OCR_SEMAPHORE_LOGGING_ENABLED` |
| **TEST PLAN** | Enable flag; run request; verify paired acquire/release lines in logs. |

---

## 5. ThreadPool safety (`reliability.py` + `/split-and-extract`)

| | |
|---|---|
| **WHY** | Page futures could fail silently or block with hard-coded 120s timeout. |
| **WHAT** | `collect_page_futures()` logs start/finish/duration/exceptions; uses `OCR_PAGE_FUTURE_TIMEOUT_SECONDS` (default 120, same as before). |
| **RISK** | Lower timeout via env could increase failed pages — opt-in only. |
| **ROLLBACK** | Disable logging flag; keep default timeout at 120. |
| **CONFIG** | `OCR_THREADPOOL_LOGGING_ENABLED`, `OCR_PAGE_FUTURE_TIMEOUT_SECONDS` |
| **TEST PLAN** | Process multi-page PDF; verify page completion heartbeats and optional threadpool logs. |

---

## 6. Tesseract call timeout wrapper (`reliability.py` + `app.py`)

| | |
|---|---|
| **WHY** | pytesseract has no explicit timeout; subprocess can hang indefinitely. |
| **WHAT** | `run_tesseract_call()` wraps OCR calls in a 1-worker pool with optional timeout when `OCR_TESSERACT_CALL_TIMEOUT_ENABLED=true`. Default off = direct call, identical behaviour. |
| **RISK** | Enabled timeout may abort slow but valid OCR on heavy pages. |
| **ROLLBACK** | `OCR_TESSERACT_CALL_TIMEOUT_ENABLED=false` |
| **CONFIG** | `OCR_TESSERACT_CALL_TIMEOUT_ENABLED`, `TESSERACT_CALL_TIMEOUT_SECONDS` |
| **TEST PLAN** | With flag off, regression test existing invoices. With flag on, verify timeout logs on artificial hang. |

---

## 7. Structured logging (`reliability.py`)

| | |
|---|---|
| **WHY** | Correlating logs across threads/pages requires request context. |
| **WHAT** | ContextVar filter prepends `request_id`, `invoice`, `stage`, `page`, `thread`, `elapsed` to log lines. Request completion summary with pages/OCR/GPT/cache stats. |
| **RISK** | Log format change for operators when enabled. |
| **ROLLBACK** | `OCR_STRUCTURED_LOGGING_ENABLED=false` |
| **CONFIG** | `OCR_STRUCTURED_LOGGING_ENABLED` |
| **TEST PLAN** | Enable flag; verify prefixed lines and REQUEST COMPLETE block. |

---

## 8. Resource monitoring (`reliability.py`)

| | |
|---|---|
| **WHY** | Watchdog and `/ready` need memory, thread count, uptime, semaphore usage. |
| **WHAT** | `get_resource_snapshot()` via psutil (graceful degrade). Included in `/health`, `/ready`, watchdog dump. |
| **RISK** | psutil unavailable → fields null, no failure. |
| **ROLLBACK** | N/A (read-only). |
| **CONFIG** | None |
| **TEST PLAN** | Check `/ready` for `memory_usage_mb`, `thread_count`, `uptime_seconds`. |

---

## 9. GPT metrics per request (`gpt_cost_metrics.py` + `app.py`)

| | |
|---|---|
| **WHY** | Request completion summary needs accurate cache hits/misses and GPT duration. |
| **WHAT** | `GPT_METRICS.snapshot_since(offset)` at request end for cache stats and response time sum. |
| **RISK** | Metrics global — offset scopes to current request window. |
| **ROLLBACK** | Remove offset usage; summary fields default to 0. |
| **CONFIG** | Uses existing `ENABLE_GPT_TOKEN_LOGGING` / cache flags for recording. |
| **TEST PLAN** | Run with `ENABLE_GPT_CACHE=true`; verify cache_hits in completion log when enabled. |

---

## 10. Startup hook (`app.py`)

| | |
|---|---|
| **WHY** | Watchdog and structured logging must start with the process. |
| **WHAT** | `@app.on_event("startup")` calls `install_structured_logging()`, `register_request_semaphore_probe()`, `start_watchdog()`. |
| **RISK** | None when flags off (watchdog no-ops, logging filter skipped). |
| **ROLLBACK** | Remove startup handler. |
| **CONFIG** | Inherited from reliability flags. |
| **TEST PLAN** | Start uvicorn; confirm startup log lines when watchdog enabled. |

---

## Recommended production rollout

1. Deploy with **all flags disabled** — zero behaviour change.
2. Enable observability first: `OCR_SEMAPHORE_LOGGING_ENABLED`, `OCR_THREADPOOL_LOGGING_ENABLED`, `OCR_STRUCTURED_LOGGING_ENABLED`.
3. Point Laravel/monitoring at `GET /live` (liveness) and `GET /ready` (readiness).
4. After baseline heartbeat ages observed, enable `OCR_WATCHDOG_ENABLED=true` with `OCR_WATCHDOG_STUCK_SECONDS=300` (or higher if needed).
5. Optionally enable `OCR_TESSERACT_CALL_TIMEOUT_ENABLED` after measuring worst-case page OCR times.

---

## Files changed

| File | Change |
|------|--------|
| `services/reliability.py` | New module: watchdog, heartbeat, logging, futures, tesseract wrapper |
| `services/gpt_cost_metrics.py` | `record_count()`, `snapshot_since()` |
| `app.py` | Integration: startup, endpoints, semaphores, page pool, request summary |
| `tests/test_reliability.py` | Unit tests |
| `.env.example` | Reliability env vars |
| `docs/RELIABILITY_CODE_ANALYSIS.md` | Phase 1 analysis (prior) |
| `docs/RELIABILITY_CHANGE_REPORT.md` | This document |

---

## Out of scope (unchanged)

- OCR extraction algorithms
- Invoice parsing / vendor fixes
- GPT prompts
- API request/response JSON schema
- Gemini quota / RPM logic
