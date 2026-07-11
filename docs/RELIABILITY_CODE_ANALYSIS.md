# Phase 1 — Reliability Code Analysis

Production service: `app.py` (~20k lines), FastAPI + uvicorn (workers=1).

---

## Request Lifecycle

1. **Admission** — `POST /split-and-extract` validates extension, increments `waiting_requests`, awaits `request_processing_semaphore` (max `REQUEST_QUEUE_TIMEOUT`, default 3600s).
2. **Progress tracking** — `begin_request_progress()` sets heartbeat; stages updated via `update_request_progress()`.
3. **File ingest** — upload / URL download / Azure blob → temp file.
4. **PDF open** — PyMuPDF `fitz.open`, page count recorded.
5. **Parallel OCR** — `ThreadPoolExecutor(max_workers=effective_ocr_pool_workers())` submits one `extract_full_invoice_data_combined()` per page; `future.result(timeout=120)`.
6. **Grouping** — pages grouped by invoice; multi-invoice re-extraction, Gemini Vision fallbacks, blob uploads.
7. **Response** — JSON returned; `finally` releases semaphore, clears progress, deletes temp files.

**Blocking model:** Single uvicorn worker runs sync OCR/LLM on the event loop thread inside the async handler (no `run_in_executor` for the full request). While processing, **all endpoints including `/health` block**.

---

## Uvicorn Startup

- `uvicorn.run(app, workers=1)` or `python -m uvicorn app:app --workers 1`.
- No FastAPI lifespan hook previously; config loaded at import time.
- Vertex AI client initialized at import.

---

## FastAPI Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /split-and-extract` | Production pipeline (semaphore-guarded) |
| `POST /test-extract` | Test pipeline (no semaphore) |
| `GET /health` | Status + heartbeat snapshot (blocks when processing) |
| `GET /` | Service info |

---

## Semaphores

| Semaphore | Type | Purpose | Release |
|-----------|------|---------|---------|
| `request_processing_semaphore` | `asyncio.Semaphore(1)` | One request at a time | `finally` in `/split-and-extract` ✓ |
| `tesseract_ocr_semaphore` | `threading.Semaphore(6)` | Limit Tesseract concurrency | `finally` in `tesseract_ocr_slot()` ✓ |

**Risk:** If an exception occurs before `slot_acquired=True` or after partial acquire, queue counters could drift — mitigated by `max(0, ...)` decrements.

**Risk:** `/test-extract` has **no** request semaphore — can run concurrently with production traffic.

---

## ThreadPoolExecutor

| Location | Workers | Future timeout |
|----------|---------|----------------|
| `/split-and-extract` OCR pool | `effective_ocr_pool_workers()` | 120s per page |
| `/test-extract` OCR pool | same | 120s per page |
| `vertex_gemini_client.generate_content_via_vertex` | 1 | model timeout |

**Risks:**
- Page timeout (120s) logs error and continues — good.
- Post-OCR phases (grouping, re-extract, blob upload, Gemini quota wait up to 300s) run **sequentially without overall request timeout** — primary hang vector.
- `acquire_model_slot_with_wait()` can `time.sleep()` up to 300s in a loop on Gemini 429/503.
- GPT OpenAI SDK call uses `GPT_TIMEOUT` (45s default).

---

## Tesseract / Subprocess

- **No direct `subprocess.run()`** in codebase.
- Tesseract invoked via **pytesseract** (spawns `/usr/bin/tesseract` subprocess internally).
- Gated by `tesseract_ocr_slot()` context manager with acquire/release logging.
- **No explicit timeout** on pytesseract calls — a hung Tesseract subprocess can block a thread until OS kill.

---

## Heartbeat (Pre-Enhancement)

- `_request_progress` dict + `request_progress_lock`.
- Updated via `update_request_progress(stage, page, invoice, ...)`.
- `get_runtime_health_snapshot()` exposes `seconds_since_last_progress`, `processing_status`.
- Stuck threshold: `REQUEST_STUCK_THRESHOLD_SECONDS` (1800) — **detection only, no auto-recovery**.

---

## Observed Production Failure Mode

- Process alive, CPU idle, no Tesseract, py-spy shows uvicorn idle.
- Laravel waits until HTTP timeout.
- Manual restart fixes.

**Likely causes (from code review):**

1. **Event loop blocked** waiting on sync I/O (Gemini quota sleep, Azure blob, GPT, or deadlock in threading).
2. **Thread pool thread hung** inside pytesseract with no timeout (main thread may be waiting on another future or post-OCR step).
3. **Gemini `call_gemini_with_quota` infinite loop** — bounded by `MAX_WAIT_TIME` (300s) but can repeat across many calls in one request.
4. **No watchdog** to exit process when heartbeat stalls.
5. **`/health` blocked** — cannot distinguish stuck vs busy externally.

---

## Exception Handling Gaps

- Hundreds of `except Exception: pass` / `continue` in vendor-specific FIX blocks — intentional for resilience but can hide failures.
- Page OCR failures caught and replaced with empty result — good.
- Top-level `/split-and-extract` `except` re-raises HTTPException; `finally` always runs cleanup ✓.

---

## Deadlock / Wait-Forever Inventory

| Location | Risk | Severity |
|----------|------|----------|
| `await semaphore.acquire()` | Bounded by REQUEST_QUEUE_TIMEOUT | Low |
| `tesseract_ocr_semaphore.acquire()` | Unbounded wait | Medium |
| `pytesseract.image_to_*` | Unbounded subprocess | **High** |
| `future.result(timeout=120)` | Bounded | Low |
| `acquire_model_slot_with_wait` | Bounded 300s per call | Medium |
| `call_gemini_with_quota` while loop | Bounded total MAX_WAIT_TIME per call | Medium |
| `requests.get/post` without timeout | Some calls have timeout, verify all | Medium |
| Post-OCR grouping/re-extract | **No timeout** | **High** |

---

## Recommendations Implemented (Phases 2–10)

See `docs/RELIABILITY_CHANGE_REPORT.md` — all behind feature flags default **off**.
