# Blocking Operations Audit — split-extract

**Date:** 2026-07-11  
**Scope:** Full project (`app.py`, `services/*`, `tests/*`)  
**Purpose:** Identify indefinite waits that could wedge the process or trigger false-positive watchdog restarts.  
**Code changes:** None (audit only).

---

## Executive summary

| Category | Count (approx.) | With explicit timeout | High risk (no timeout, long-running) |
|----------|-----------------|----------------------|--------------------------------------|
| `future.result()` | 5 call sites | 4 yes, 1 hard-coded | 1 medium (`/test-extract`) |
| `pytesseract.*` | 12+ call sites | 2 wrapped (opt-in flag) | **10+ HIGH** |
| HTTP (requests / SDK) | 4 paths | 4 yes | 0 |
| Azure Blob SDK | 2 paths | 0 explicit | 2 MEDIUM |
| `threading.Semaphore.acquire` | 1 (Tesseract) | No | MEDIUM (bounded by slot + finally) |
| `asyncio.Semaphore.acquire` | 1 (request gate) | Yes | LOW |
| `subprocess.run` / `Popen.wait` | 0 direct | N/A | Indirect via pytesseract |
| `queue.get` / `Event.wait` | 0 | N/A | N/A |
| `httpx` | 0 | N/A | N/A |
| `time.sleep` (quota/retry) | 4 loops | Bounded by env/max | LOW |
| PDF / PIL / OpenCV CPU | Many | No | LOW–MEDIUM (CPU-bound, not I/O hang) |

**Primary residual risk:** Per-page Tesseract OCR (via pytesseract subprocess) on most code paths has **no timeout** unless `OCR_TESSERACT_CALL_TIMEOUT_ENABLED=true`, and only two helpers use `run_tesseract_call()`. A wedged Tesseract subprocess can block a thread until the page `future.result()` timeout (120s default) — but if all pool workers are wedged on Tesseract, **heartbeat may not advance** until a page completes or times out.

**Watchdog interaction:** Watchdog correctly uses **heartbeat age** (`last_progress_mono`), not total request duration. However, heartbeat during parallel OCR only advances on **`ocr_page_completed`** (and downstream stages), not when individual pages *start* OCR. A batch where all workers are busy on slow-but-active Tesseract for >300s without any page finishing could theoretically trigger the watchdog even though work is in progress.

---

## Watchdog condition verification

**Implementation:** `services/reliability.py` lines 389–409

```python
# Pseudocode of actual logic
if not active_request:
    continue
age = now - last_progress_mono   # NOT request_start_mono
if age >= OCR_WATCHDOG_STUCK_SECONDS:
    os._exit(1)
```

✅ **Correct:** Uses stalled progress (`last_progress_mono`), not `request_start_mono` / total request duration.

⚠️ **Caveat:** `last_progress_mono` is updated by `update_request_progress()`. During the OCR thread pool phase, updates occur per **completed** page, not per page start or per Tesseract call. See [Heartbeat coverage gaps](#heartbeat-coverage-gaps).

---

## Heartbeat coverage gaps

### Stages WITH heartbeat updates (production `/split-and-extract`)

| Stage | When |
|-------|------|
| `request_admitted` | Semaphore acquired |
| `file_upload` / `file_upload_complete` | Upload read |
| `url_download` / `url_download_complete` | URL fetch |
| `azure_blob_download` / `azure_blob_download_complete` | Blob download |
| `local_file_created` | Temp file written |
| `pdf_opened` | PDF loaded |
| `ocr_started` | Before thread pool |
| `ocr_page_completed` | After each page future completes |
| `ocr_complete` | All pages done |
| `multi_invoice_reextract_*` | Multi-invoice re-extract |
| `invoice_grouping_complete` | Grouping done |
| `multi_page_reextract_*` | Multi-page re-extract |
| `split_pdf_generated` | Per-invoice PDF built |
| `blob_upload_completed` | Azure upload done |
| `openai_request` / `openai_response_received` | GPT calls |
| `gemini_*_request` / `gemini_*_response_received` | Gemini calls |
| `response_generated` / `request_completed` | Response ready |

### Stages WITHOUT heartbeat (long-running, inside page workers)

| Activity | Location | Typical duration | Risk for watchdog |
|----------|----------|------------------|-------------------|
| PDFPlumber extract | `extract_text_with_pdfplumber` | 0.1–5s | Low |
| PyMuPDF `page.get_text` | `extract_full_invoice_data_combined` | 0.1–2s | Low |
| Image render (`get_pixmap`) | Tesseract path | 1–10s | Low–medium |
| OpenCV preprocessing | `extract_text_with_tesseract`, crops | 1–5s | Low |
| **Tesseract OCR (full page)** | `extract_text_with_tesseract` (wrapped), many crop paths (not wrapped) | **30–120s+** | **HIGH** |
| Page quality probe Tesseract | `_page_header_quality_probe` | 5–30s | Medium |
| Gemini Vision per page | `extract_via_gemini_vision` | 10–60s (timeout on API) | Low (API timeout) |
| GPT/Gemini text per page | `extract_full_data_from_text_gemini` | 5–45s | Low (API timeout) |

**Not present:** No database save in this service (stateless Python API; Laravel owns DB). Blob upload has heartbeat only on **completion**, not during upload.

**Recommendation (future, not in this audit):** Add `ocr_page_started` heartbeat inside `extract_full_invoice_data_combined` (or a lightweight periodic tick during Tesseract) before enabling watchdog at 300s in production.

---

## Per-request UUID verification

✅ **Implemented:** `begin_request_progress()` assigns `request_id = uuid.uuid4().hex[:12]` (12-char hex).

| Surface | Includes request_id? |
|---------|---------------------|
| `_request_progress` / `/health` | Yes |
| Enhanced heartbeat / `/ready` | Yes |
| Structured logs | Yes, when `OCR_STRUCTURED_LOGGING_ENABLED=true` |
| Default `logger.info` lines | **No** (unless structured logging enabled) |

To get `request_id=... invoice=... stage=... page=...` on **every** log line, enable `OCR_STRUCTURED_LOGGING_ENABLED=true` in rollout step 3.

---

## `os._exit(1)` review

| Concern | Status |
|---------|--------|
| Process manager restart | Assumes systemd/supervisor/tmux restarts uvicorn — **verify in your deployment** |
| Temp file cleanup | `finally` in `/split-and-extract` removes temp PDF paths on normal exceptions; **`os._exit` bypasses `finally`** — temp files may remain on watchdog kill |
| Semaphore consistency | In-process semaphores reset on new process — **OK** after restart |
| Counters / heartbeat | Reset on new process — **OK** |
| In-flight Laravel request | Will timeout; client must retry — **expected** |

---

## Detailed blocking operation inventory

Risk levels:
- **HIGH** — Can block indefinitely or for minutes with no timeout
- **MEDIUM** — Bounded by retry/timeout in some paths, or thread-pool timeout indirect bound
- **LOW** — Short critical sections, explicit timeout, or bounded sleep

---

### `services/reliability.py`

| Line | Operation | Timeout? | Finally/cleanup? | Risk | Recommendation |
|------|-----------|----------|------------------|------|----------------|
| 391 | `time.sleep(OCR_WATCHDOG_INTERVAL_SECONDS)` | Bounded (config) | N/A (daemon) | LOW | OK |
| 464 | `future.result(timeout=timeout)` in `collect_page_futures` | Yes (config, default 120s) | Exception logged | LOW | Used by `/split-and-extract` |
| 503 | `future.result(timeout=timeout)` in `run_tesseract_call` | Yes (opt-in flag) | Pool context manager | LOW | Only when `OCR_TESSERACT_CALL_TIMEOUT_ENABLED=true` |
| 137–161 | `threading.Lock` in heartbeat/resource | Short `with` blocks | Auto-release | LOW | OK |

---

### `services/vertex_gemini_client.py`

| Line | Operation | Timeout? | Finally/cleanup? | Risk | Recommendation |
|------|-----------|----------|------------------|------|----------------|
| 186–199 | `client.models.generate_content` via `future.result(timeout=timeout)` | Yes (caller `model_config["timeout"]`) | ThreadPoolExecutor CM | LOW | OK |
| 196–201 | `FuturesTimeoutError` → `TimeoutError` | Yes | Raised to caller | LOW | OK |

---

### `services/gpt_text_extractor.py`

| Line | Operation | Timeout? | Finally/cleanup? | Risk | Recommendation |
|------|-----------|----------|------------------|------|----------------|
| 49–62 | `OpenAI(..., timeout=timeout).responses.create(...)` | Yes (`GPT_TIMEOUT`, default 45s) | try/except | LOW | Primary GPT path |
| 148–153 | `requests.post(..., timeout=self.config.text_timeout)` | Yes | try/except | LOW | Legacy `GPTTextExtractor` class only |

---

### `app.py` — Concurrency & I/O

| Line | Operation | Timeout? | Finally/cleanup? | Risk | Recommendation |
|------|-----------|----------|------------------|------|----------------|
| 565 | `tesseract_ocr_semaphore.acquire()` | **No** | **Yes** (`finally` release in `tesseract_ocr_slot`) | MEDIUM | Slot limits concurrency; wedged Tesseract still blocks one worker |
| 644–675 | `acquire_model_slot_with_wait` loop + `time.sleep` | Yes (`max_wait_seconds`, default 300s) | Returns None on timeout | LOW | OK |
| 678–714 | `call_gemini_with_quota` + `time.sleep(2)` retry | Yes (`MAX_WAIT_TIME` 300s overall) | Exception paths return None | LOW | OK |
| 18562 | `asyncio.wait_for(semaphore.acquire(), REQUEST_QUEUE_TIMEOUT)` | Yes (default 3600s) | `finally` release | LOW | OK |
| 18627 | `requests.get(split_raw_url, timeout=120)` | Yes (120s) | Exception → HTTP 500 | LOW | OK |
| 18643 | `blob_client.download_blob().readall()` | **No** explicit SDK timeout | Exception → HTTP 500 | MEDIUM | Add Azure SDK timeout / retry policy |
| 18444 | `blob_client.upload_blob(...)` | **No** explicit SDK timeout | Retry loop (3 attempts) + sleep | MEDIUM | Add SDK timeout; heartbeat only on complete |
| 18484 | `time.sleep(wait_s)` blob retry | Bounded (1.5×attempt) | In retry loop | LOW | OK |
| 18682–18714 | `ThreadPoolExecutor` + `collect_page_futures` | Yes (120s default per page) | Failed page dict fallback | MEDIUM | OK for `/split-and-extract` |
| 19601–19618 | `/test-extract` `future.result(timeout=120)` | Yes (hard-coded 120) | except → failed dict | MEDIUM | Align with `OCR_PAGE_FUTURE_TIMEOUT_SECONDS` (future) |

---

### `app.py` — OCR / image (pytesseract, PIL, OpenCV, PyMuPDF)

| Line | Operation | Timeout? | Finally/cleanup? | Risk | Recommendation |
|------|-----------|----------|------------------|------|----------------|
| 741–746 | `pdfplumber.open` + `extract_text` | No | try/except | LOW | Usually fast |
| 789–791 | `page.get_pixmap` | No | Manual `pix = None` | LOW–MEDIUM | Large pages can be slow |
| 794 | `PILImage.open` | No | del/close in path | LOW | Memory bound |
| 802–808 | `cv2.cvtColor`, `threshold` | No | del intermediates | LOW | CPU bound |
| 814–816 | `pytesseract.image_to_data/string` in `run_tesseract_call` | **Opt-in** (`OCR_TESSERACT_CALL_TIMEOUT_ENABLED`) | `tesseract_ocr_slot` finally | **HIGH** if flag off | Enable timeout flag after measuring P99 OCR time |
| 887–889 | Same (relaxed path) | Opt-in | Same | HIGH if flag off | Same |
| 15046–15071 | Page quality probe Tesseract | **No** | `tesseract_ocr_slot` finally | **HIGH** | Wrap with `run_tesseract_call` (future) |
| 15123, 15182, 15228 | Date crop Tesseract | **No** | `tesseract_ocr_slot` finally | MEDIUM | Wrap (future) |
| 17249–17250 | `_ocr_text_from_image_crop` Tesseract | **No** | `tesseract_ocr_slot` finally | MEDIUM | Wrap (future) |
| 18663, 19591 | `fitz.open(pdf_path)` | No | `doc.close()` in finally | LOW | Large PDFs slow to open |
| 18655, 19583 | `PILImage.open(temp_path)` image→PDF | No | img.close() | LOW | OK |

**Note:** No direct `subprocess.run()` in codebase. Tesseract is invoked **indirectly** through pytesseract (spawns `tesseract` subprocess internally).

---

### `app.py` — LLM calls (within request path)

| Line | Operation | Timeout? | Finally/cleanup? | Risk | Recommendation |
|------|-----------|----------|------------------|------|----------------|
| 16817–16802 | `extract_invoice_via_gpt(..., timeout=GPT_TIMEOUT)` | Yes (45s default) | Progress heartbeat + except | LOW | OK |
| 16892–16905 | `call_gemini_with_quota` text | Yes (model timeout + MAX_WAIT) | Heartbeat stages | LOW | OK |
| 17889–17894 | Gemini Vision | Yes | call_gemini_with_quota | LOW | OK |
| 17971+ | Recovery vision Gemini calls | Yes | try/except | LOW | OK |

---

### Other services

| File | Line | Operation | Timeout? | Risk | Notes |
|------|------|-----------|----------|------|-------|
| `gpt_response_cache.py` | 39, 50 | `open()` file I/O | No | LOW | Local disk, fast |
| `extraction_cache.py` | 45, 53 | `open()` file I/O | No | LOW | Local disk |
| `image_preprocessing.py` | 47–139 | OpenCV ops | No | LOW | **Not imported by app.py** (unused in main path) |
| `gemini_vision_extractor.py` | — | Delegates to router/vertex | Yes (via vertex) | LOW | Alternate module |

---

### Not found in project

| Pattern | Result |
|---------|--------|
| `subprocess.run` | **0** direct calls |
| `subprocess.Popen` | **0** |
| `httpx` | **0** |
| `queue.get` | **0** |
| `threading.Event.wait` | **0** |
| `Lock.acquire()` without `with` | **0** (all use `with lock:` or semaphore CM) |

---

## Heartbeat vs watchdog false-positive scenario

**Scenario:** 27-page PDF, 6 parallel workers, each page Tesseract takes 90s, no page completes for first 90s.

| Time | Heartbeat stage | `heartbeat_age` |
|------|-----------------|-----------------|
| 0s | `ocr_started` | 0s |
| 1–89s | (no update) | grows to 89s |
| 90s | first `ocr_page_completed` | resets to ~0s |

With `OCR_WATCHDOG_STUCK_SECONDS=300`, this scenario is **safe**.

**Failure scenario:** All workers wedged on Tesseract subprocess (not CPU-active hang, true deadlock) for >300s with zero pages completing → watchdog **correctly** terminates.

**Borderline scenario:** Single worker (`MAX_CONCURRENT_REQUESTS=1`), one page Tesseract actively running 400s (slow scan), no intermediate heartbeat → watchdog kills at 300s despite active OCR. **Mitigation:** raise `OCR_WATCHDOG_STUCK_SECONDS` to 600+ or add per-page started heartbeat before enabling watchdog.

---

## Recommended priority fixes (future work — not implemented here)

1. **Heartbeat:** Emit `ocr_page_started` (page N) at entry to `extract_full_invoice_data_combined` before Tesseract.
2. **Tesseract:** Route **all** `pytesseract` calls through `run_tesseract_call()` behind `OCR_TESSERACT_CALL_TIMEOUT_ENABLED`.
3. **Azure:** Add explicit timeout on `download_blob().readall()` and `upload_blob()`.
4. **Resume checkpoint:** Persist `{invoice, current_page, completed_pages}` for large PDFs (user-requested future feature).
5. **Watchdog pre-exit:** Best-effort temp file cleanup in `_dump_diagnostics()` before `os._exit(1)`.

---

## Rollout alignment

This audit supports the suggested rollout:

1. Deploy flags **disabled**
2. Monitor `/live` + `/ready`
3. Enable structured logging → confirm `request_id` on all lines
4. Observe `heartbeat_age` P99 during normal OCR
5. Enable watchdog at **600s** initially, then tune down
6. Enable Tesseract timeout flag after P99 measurement

---

*Generated as part of reliability review. No source code was modified.*
