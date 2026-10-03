"""Secondary Sales extraction runtime: admission, Gemini call slots, progress.

Does NOT change parsers, quality gates, or Gemini prompts.
Reuses app.tesseract_ocr_slot and app.call_gemini_with_quota / RPM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _env_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, defaulting to %s", name, raw, default)
        return default
    if value < 1:
        logger.warning("%s=%s is below 1, defaulting to %s", name, value, default)
        return default
    return value


MAX_CONCURRENT_EXTRACTIONS = _env_positive_int("MAX_CONCURRENT_EXTRACTIONS", 2)
MAX_CONCURRENT_GEMINI_REQUESTS = _env_positive_int(
    "MAX_CONCURRENT_GEMINI_REQUESTS", 2
)
SALES_EXTRACTION_QUEUE_TIMEOUT = _env_positive_int(
    "SALES_EXTRACTION_QUEUE_TIMEOUT",
    int(os.getenv("REQUEST_QUEUE_TIMEOUT", "3600") or "3600"),
)
# Hard wall-clock budget for one extraction after admission (not queue wait).
# Live image+vision jobs commonly finish in ~200-285s; 600s leaves headroom
# for Gemini 429 backoff without approaching Laravel's 3600s HTTP timeout.
SALES_EXTRACTION_MAX_EXECUTION_SECONDS = _env_positive_int(
    "SALES_EXTRACTION_MAX_EXECUTION_SECONDS", 600
)
# Bound each sales OCR subprocess even when global OCR timeout flag is off.
SALES_TESSERACT_CALL_TIMEOUT_SECONDS = _env_positive_int(
    "SALES_TESSERACT_CALL_TIMEOUT_SECONDS",
    int(os.getenv("TESSERACT_CALL_TIMEOUT_SECONDS", "180") or "180"),
)
# Soft/hard ceiling on Tesseract calls per extraction (pathological loops).
# Portrait cell grids can legitimately need a few hundred calls.
SALES_OCR_MAX_CALLS_PER_REQUEST = _env_positive_int(
    "SALES_OCR_MAX_CALLS_PER_REQUEST", 500
)
SALES_OCR_CACHE_MAX_ENTRIES = _env_positive_int("SALES_OCR_CACHE_MAX_ENTRIES", 64)
# OCR-only side bound for pathological megapixel phone photos (Vision keeps original).
SALES_OCR_MAX_IMAGE_SIDE = _env_positive_int("SALES_OCR_MAX_IMAGE_SIDE", 4000)

# Vertex provider-side 429 RESOURCE_EXHAUSTED circuit breaker (process-local).
# Independent of local RPM/RPD counters — Vertex can 429 while RPM is low.
GEMINI_PROVIDER_429_COOLDOWN_SECONDS = _env_positive_int(
    "GEMINI_PROVIDER_429_COOLDOWN_SECONDS", 30
)
GEMINI_PROVIDER_429_MAX_COOLDOWN_SECONDS = _env_positive_int(
    "GEMINI_PROVIDER_429_MAX_COOLDOWN_SECONDS", 120
)
GEMINI_PROVIDER_429_MAX_RETRIES = _env_positive_int(
    "GEMINI_PROVIDER_429_MAX_RETRIES", 3
)

_sales_extraction_async_sem = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)
_sales_extraction_waiters_lock = threading.Lock()
_sales_extraction_waiting = 0
_sales_extraction_active = 0

_gemini_call_semaphore = threading.Semaphore(MAX_CONCURRENT_GEMINI_REQUESTS)
_gemini_call_lock = threading.Lock()
_gemini_call_active = 0
_gemini_call_waiting = 0
_gemini_slot_depth = threading.local()

# Process-wide Vertex 429 cooldown (shared across concurrent sales/invoice calls).
_provider_cooldown_lock = threading.Lock()
_provider_cooldown_until_mono = 0.0
_provider_429_streak = 0
_provider_cooldown_active = False
_gemini_provider_metrics: Dict[str, int] = {
    "gemini_429_count": 0,
    "gemini_503_count": 0,
    "gemini_timeout_count": 0,
    "gemini_success_count": 0,
    "gemini_provider_cooldown_count": 0,
    "gemini_retry_count": 0,
}

_progress_lock = threading.Lock()
_sales_request_progress: Dict[str, Dict[str, Any]] = {}
_last_runtime_stats: Dict[str, Dict[str, Any]] = {}

_deadline_local = threading.local()
_cancel_lock = threading.Lock()
_cancel_flags: Dict[str, threading.Event] = {}


class SalesExtractionRuntimeContext:
    """Single request-local runtime context shared across OCR/Gemini/PDF work."""

    __slots__ = (
        "request_id",
        "deadline_mono",
        "limit_seconds",
        "started_mono",
        "cancel_event",
        "ocr_calls",
        "ocr_cache_hits",
        "ocr_duration_ms",
        "ocr_budget",
        "ocr_cache",
        "gemini_calls",
        "gemini_retries",
        "gemini_duration_ms",
        "stage",
        "file_size",
    )

    def __init__(
        self,
        request_id: str,
        limit_seconds: float,
        cancel_event: threading.Event,
        file_size: Optional[int] = None,
    ):
        self.request_id = request_id
        self.limit_seconds = float(limit_seconds)
        self.started_mono = time.monotonic()
        self.deadline_mono = self.started_mono + self.limit_seconds
        self.cancel_event = cancel_event
        self.ocr_calls = 0
        self.ocr_cache_hits = 0
        self.ocr_duration_ms = 0.0
        self.ocr_budget = int(SALES_OCR_MAX_CALLS_PER_REQUEST)
        self.ocr_cache: Dict[str, Any] = {}
        self.gemini_calls = 0
        self.gemini_retries = 0
        self.gemini_duration_ms = 0.0
        self.stage = "preparing"
        self.file_size = file_size

    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_mono)

    def remaining_seconds(self) -> float:
        return self.deadline_mono - time.monotonic()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "ocr_calls": int(self.ocr_calls),
            "ocr_cache_hits": int(self.ocr_cache_hits),
            "ocr_seconds": round(float(self.ocr_duration_ms) / 1000.0, 3),
            "gemini_calls": int(self.gemini_calls),
            "gemini_retries": int(self.gemini_retries),
            "vision_seconds": round(float(self.gemini_duration_ms) / 1000.0, 3),
            "total_seconds": round(self.elapsed_seconds(), 3),
            "remaining_seconds": round(self.remaining_seconds(), 3),
            "stage": self.stage,
            "file_size": self.file_size,
        }

STAGE_LABELS = {
    "queued": "Waiting in extraction queue",
    "preparing": "Preparing document",
    "file_detection": "Detecting file type",
    "pdf_processing": "Processing PDF",
    "image_processing": "Processing image",
    "parsing": "Parsing stock statement",
    "ocr": "Running OCR",
    "quality_check": "Checking extraction quality",
    "vision_fallback": "Analyzing document with Vision AI",
    "gemini_request": "Calling Gemini Vision",
    "gemini_retry": "Retrying Gemini after provider limit",
    "merging": "Merging extraction results",
    "validating": "Validating extraction",
    "completed": "Completed",
    "failed": "Extraction failed",
}


def resolve_request_id(header_value: Optional[str] = None) -> str:
    raw = (header_value or "").strip()
    if raw:
        return raw[:128]
    return uuid.uuid4().hex[:12]


def stage_label(stage: Optional[str]) -> str:
    key = (stage or "").strip() or "preparing"
    return STAGE_LABELS.get(key, key.replace("_", " ").title())


class SalesExtractionBusy(Exception):
    def __init__(self, waited: float, limit: int, timeout: float):
        self.waited = waited
        self.limit = limit
        self.timeout = timeout
        super().__init__(
            f"Sales extraction busy: waited {waited:.1f}s for one of "
            f"{limit} slots (timeout={timeout}s)"
        )


class SalesExtractionDeadlineExceeded(Exception):
    def __init__(self, request_id: str, stage: str, limit_seconds: float):
        self.request_id = request_id
        self.stage = stage
        self.limit_seconds = limit_seconds
        super().__init__(
            f"Sales extraction deadline exceeded at stage={stage} "
            f"(limit={limit_seconds}s, request_id={request_id})"
        )


class SalesOcrBudgetExceeded(Exception):
    def __init__(self, request_id: str, calls: int, budget: int):
        self.request_id = request_id
        self.calls = calls
        self.budget = budget
        super().__init__(
            f"Sales OCR budget exceeded calls={calls} budget={budget} "
            f"request_id={request_id}"
        )


def classify_sales_document(filename: str, file_bytes: Optional[bytes] = None) -> str:
    """Cheap extension/magic-based classification. No OCR."""
    lower = (filename or "").lower()
    if lower.endswith((".xlsx",)):
        return "xlsx"
    if lower.endswith((".xls",)):
        return "xls"
    if lower.endswith((".docx",)):
        return "docx"
    if lower.endswith((".doc",)):
        return "doc"
    if lower.endswith((".txt",)):
        return "txt"
    if lower.endswith((".html", ".htm")):
        return "html"
    if lower.endswith((".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp")):
        return "image"
    if lower.endswith(".pdf"):
        if file_bytes:
            try:
                import fitz

                doc = fitz.open(stream=file_bytes, filetype="pdf")
                try:
                    embedded = 0
                    for i in range(min(int(doc.page_count or 0), 3)):
                        embedded += len(
                            (doc[i].get_text("text") or "").replace(" ", "")
                        )
                    if embedded >= 40:
                        return "text_pdf"
                    return "image_pdf"
                finally:
                    doc.close()
            except Exception:
                return "pdf"
        return "pdf"
    return "unknown"


def begin_sales_progress(
    request_id: str,
    filename: str,
    stage: str = "queued",
    batch_id: Optional[str] = None,
) -> None:
    now = time.time()
    with _progress_lock:
        _sales_request_progress[request_id] = {
            "request_id": request_id,
            "filename": filename,
            "batch_id": batch_id,
            "stage": stage,
            "status": "queued" if stage == "queued" else "processing",
            "label": stage_label(stage),
            "started_at": now,
            "updated_at": now,
            "stage_started_at": now,
            "stage_duration_seconds": 0.0,
            "queue_wait_seconds": None,
            "extraction_duration_seconds": None,
            "final_status": None,
            "file_type": None,
            "page_count": None,
            "extraction_method": None,
            "ocr_quality": None,
            "vision_fallback": None,
        }
    with _cancel_lock:
        _cancel_flags[request_id] = threading.Event()
    try:
        from app import begin_request_progress

        begin_request_progress(
            source_filename=filename,
            batch_id=batch_id,
            stage=stage,
            request_id=request_id,
        )
    except Exception:
        pass


def update_sales_progress(request_id: str, stage: str, **fields: Any) -> None:
    now = time.time()
    with _progress_lock:
        entry = _sales_request_progress.get(request_id)
        if entry is None:
            entry = {
                "request_id": request_id,
                "started_at": now,
                "stage_started_at": now,
            }
            _sales_request_progress[request_id] = entry
        prev_stage = entry.get("stage")
        stage_started = float(entry.get("stage_started_at") or now)
        if prev_stage != stage:
            prev_duration = round(now - stage_started, 3)
            logger.info(
                "sales_stage request_id=%s stage=%s duration=%.3fs next=%s",
                request_id,
                prev_stage,
                prev_duration,
                stage,
            )
            entry["stage_started_at"] = now
            entry["stage_duration_seconds"] = 0.0
        else:
            entry["stage_duration_seconds"] = round(now - stage_started, 3)
        entry["stage"] = stage
        entry["label"] = stage_label(stage)
        entry["updated_at"] = now
        final_status = fields.get("final_status", entry.get("final_status"))
        if final_status == "completed" or stage == "completed":
            entry["status"] = "completed"
        elif final_status or stage == "failed":
            entry["status"] = "failed"
        elif stage == "queued":
            entry["status"] = "queued"
        else:
            entry["status"] = "processing"
        for key, value in fields.items():
            if value is not None:
                entry[key] = value
    try:
        from app import update_request_progress

        update_request_progress(stage, force_log=True)
    except Exception:
        pass


def finish_sales_progress(
    request_id: str,
    final_status: str,
    **fields: Any,
) -> None:
    # Map terminal outcomes for status polling.
    # Do NOT clear cancel flags here: disconnect/cancel may mark progress
    # terminal while the worker thread is still winding down and must keep
    # seeing the cancel Event until clear_sales_deadline() runs.
    if final_status == "completed":
        stage = "completed"
        status = "completed"
    elif final_status == "cancelled":
        stage = "failed"
        status = "cancelled"
    else:
        stage = "failed"
        status = "failed"
    update_sales_progress(
        request_id,
        stage,
        final_status=final_status,
        status=status,
        label=stage_label(stage),
        **fields,
    )
    try:
        from app import clear_request_progress

        clear_request_progress({"final_status": final_status})
    except Exception:
        pass


def get_sales_progress(request_id: str) -> Optional[Dict[str, Any]]:
    with _progress_lock:
        entry = _sales_request_progress.get(request_id)
        if not entry:
            return None
        out = dict(entry)
    stage_started = float(out.get("stage_started_at") or out.get("updated_at") or time.time())
    out["stage_duration_seconds"] = round(time.time() - stage_started, 3)
    out["label"] = stage_label(out.get("stage"))
    if out.get("final_status") == "completed":
        out["status"] = "completed"
    elif out.get("final_status") == "cancelled":
        out["status"] = "cancelled"
    elif out.get("final_status"):
        out["status"] = "failed"
    elif out.get("stage") == "queued":
        out["status"] = "queued"
    else:
        out["status"] = out.get("status") or "processing"
    return out


def sales_job_is_active(request_id: str) -> bool:
    """True when a request is queued or still extracting (not terminal)."""
    progress = get_sales_progress(request_id)
    if not progress:
        return False
    if progress.get("final_status"):
        return False
    return True


# ---------------------------------------------------------------------------
# Process-local background submission (no Redis/Celery).
# Fixed worker count == MAX_CONCURRENT_EXTRACTIONS; extra jobs wait in queue.
# Workers share acquire_sales_extraction_slot with the sync HTTP endpoint.
# In-memory only: jobs are lost if the process restarts.
# ---------------------------------------------------------------------------

_bg_queue: Optional[asyncio.Queue] = None
_bg_workers: List[asyncio.Task] = []
_bg_start_lock: Optional[asyncio.Lock] = None
_bg_loop_id: Optional[int] = None
_bg_scheduled_lock = threading.Lock()
_bg_scheduled_ids: set = set()


def _bg_mark_scheduled(request_id: str) -> bool:
    """Return True if newly scheduled; False if already scheduled/active."""
    with _bg_scheduled_lock:
        if request_id in _bg_scheduled_ids:
            return False
        if sales_job_is_active(request_id):
            # Progress exists from a prior submit; treat as already running.
            _bg_scheduled_ids.add(request_id)
            return False
        _bg_scheduled_ids.add(request_id)
        return True


def _bg_unmark_scheduled(request_id: str) -> None:
    with _bg_scheduled_lock:
        _bg_scheduled_ids.discard(request_id)


def reset_sales_background_runtime_for_tests() -> None:
    """Drop process-local queue/workers (tests only)."""
    global _bg_queue, _bg_workers, _bg_start_lock, _bg_loop_id
    for task in list(_bg_workers):
        try:
            task.cancel()
        except Exception:
            pass
    _bg_workers = []
    _bg_queue = None
    _bg_start_lock = None
    _bg_loop_id = None
    with _bg_scheduled_lock:
        _bg_scheduled_ids.clear()


async def ensure_sales_background_workers() -> None:
    """Start a fixed pool of background workers once per process/event-loop."""
    global _bg_queue, _bg_start_lock, _bg_loop_id, _bg_workers
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    if _bg_start_lock is None or _bg_loop_id != loop_id:
        # New event loop (e.g. TestClient restart): recreate lock/queue/workers.
        _bg_start_lock = asyncio.Lock()
        _bg_loop_id = loop_id
        for task in list(_bg_workers):
            try:
                task.cancel()
            except Exception:
                pass
        _bg_workers = []
        _bg_queue = None

    async with _bg_start_lock:
        alive = [t for t in _bg_workers if not t.done()]
        if _bg_queue is not None and len(alive) >= MAX_CONCURRENT_EXTRACTIONS:
            _bg_workers = alive
            return
        _bg_workers = alive
        if _bg_queue is None:
            _bg_queue = asyncio.Queue()
        needed = MAX_CONCURRENT_EXTRACTIONS - len(_bg_workers)
        for index in range(needed):
            task = asyncio.create_task(
                _sales_background_worker(len(_bg_workers) + index),
                name=f"sales-bg-worker-{len(_bg_workers) + index}",
            )
            _bg_workers.append(task)
        logger.info(
            "sales_background_workers_started count=%s limit=%s",
            len(_bg_workers),
            MAX_CONCURRENT_EXTRACTIONS,
        )


async def enqueue_sales_background_job(job: Dict[str, Any]) -> None:
    await ensure_sales_background_workers()
    assert _bg_queue is not None
    await _bg_queue.put(job)


async def _sales_background_worker(worker_index: int) -> None:
    assert _bg_queue is not None
    while True:
        job = await _bg_queue.get()
        try:
            await _run_sales_background_job(job, worker_index=worker_index)
        except Exception:
            logger.exception(
                "sales_background_worker_crash worker=%s request_id=%s",
                worker_index,
                (job or {}).get("request_id"),
            )
        finally:
            _bg_queue.task_done()


async def _run_sales_background_job(
    job: Dict[str, Any],
    *,
    worker_index: int = 0,
) -> None:
    """Run one extraction job using the shared extraction slot + existing extractor."""
    # Same extract_sales_statement binding the sync endpoint uses (app module).
    try:
        from app import extract_sales_statement
    except Exception:  # pragma: no cover - fallback if app not loaded
        from services.sales_statement_extractor import extract_sales_statement

    request_id = str(job.get("request_id") or "")
    filename = str(job.get("filename") or "upload")
    file_bytes = job.get("file_bytes") or b""
    file_type = job.get("file_type")
    batch_id = job.get("batch_id")
    deadline_seconds = float(
        job.get("deadline_seconds") or SALES_EXTRACTION_MAX_EXECUTION_SECONDS
    )
    total_started = time.time()
    queue_wait_seconds = 0.0
    extraction_duration_seconds = None
    slot_acquired = False
    final_status = "failed"

    logger.info(
        "sales_background_started request_id=%s worker=%s filename=%s file_type=%s",
        request_id,
        worker_index,
        filename,
        file_type,
    )
    try:
        try:
            queue_wait_seconds = await acquire_sales_extraction_slot()
            slot_acquired = True
        except SalesExtractionBusy as busy:
            final_status = "queue_timeout"
            update_sales_progress(
                request_id,
                "failed",
                error={
                    "error": "queue_timeout",
                    "message": (
                        f"Server busy. Queue wait exceeded {busy.timeout}s."
                    ),
                },
            )
            logger.warning(
                "sales_background_failed request_id=%s reason=queue_timeout",
                request_id,
            )
            return

        update_sales_progress(
            request_id,
            "preparing",
            queue_wait_seconds=queue_wait_seconds,
            file_type=file_type,
            batch_id=batch_id,
        )
        extraction_started = time.time()
        update_sales_progress(request_id, "parsing")
        try:
            sales_result = await asyncio.wait_for(
                asyncio.to_thread(
                    run_sales_extract_with_deadline,
                    extract_sales_statement,
                    file_bytes,
                    filename,
                    request_id,
                    deadline_seconds,
                ),
                timeout=deadline_seconds + 15.0,
            )
        except asyncio.TimeoutError:
            request_cancel_sales_extraction(request_id)
            terminate_sales_tesseract_children()
            final_status = "extraction_failed"
            update_sales_progress(
                request_id,
                "failed",
                error={
                    "error": "extraction_failed",
                    "message": (
                        "Stock statement extraction exceeded the maximum "
                        f"execution time of {int(deadline_seconds)}s."
                    ),
                    "reasons": ["extraction_deadline_exceeded"],
                },
            )
            raise
        except SalesExtractionDeadlineExceeded as exc:
            request_cancel_sales_extraction(request_id)
            terminate_sales_tesseract_children()
            final_status = "extraction_failed"
            update_sales_progress(
                request_id,
                "failed",
                error={
                    "error": "extraction_failed",
                    "message": (
                        "Stock statement extraction exceeded the maximum "
                        f"execution time of {int(exc.limit_seconds or deadline_seconds)}s."
                    ),
                    "reasons": ["extraction_deadline_exceeded"],
                    "stage": exc.stage,
                },
            )
            raise

        extraction_duration_seconds = time.time() - extraction_started
        extra = ((sales_result.get("totals") or {}).get("extra") or {})
        quality = extra.get("extraction_quality") or {}
        update_sales_progress(
            request_id,
            "validating",
            file_type=file_type,
            page_count=extra.get("page_count"),
            extraction_method=extra.get("extraction_method"),
            ocr_quality=(
                quality.get("score") if isinstance(quality, dict) else None
            ),
            vision_fallback=bool(extra.get("gemini_fallback")),
            extraction_duration_seconds=extraction_duration_seconds,
        )
        if extra.get("extraction_failed"):
            reasons = []
            if isinstance(quality, dict):
                reasons = list(quality.get("reasons") or [])
            final_status = "extraction_failed"
            update_sales_progress(
                request_id,
                "failed",
                error={
                    "error": "extraction_failed",
                    "message": (
                        "Could not reliably extract stock statement products. "
                        "OCR/parser and Gemini Vision fallback both failed validation."
                    ),
                    "reasons": reasons,
                    "gemini_fallback": extra.get("gemini_fallback"),
                    "source_file": filename,
                },
            )
            raise RuntimeError("extraction_failed")

        # Optional POD wrapper — same rule as the sync endpoint.
        response_payload: Any = sales_result
        if extra.get("extraction_method") == "pod_hospital_wise_sales_xlsx":
            try:
                from app import build_split_extract_response_from_sales_statement

                response_payload = build_split_extract_response_from_sales_statement(
                    sales_result=sales_result,
                    source_filename=filename,
                    batch_id=batch_id,
                    split_id=job.get("split_id"),
                    file_name=job.get("file_name") or filename,
                    use_blob_storage=bool(job.get("use_blob_storage")),
                    container_name=job.get("blob_container"),
                    target_invoices_blob_folder=job.get(
                        "target_invoices_blob_folder"
                    ),
                    start_time=datetime_now_fallback(),
                )
            except Exception:
                logger.exception(
                    "sales_background_pod_wrapper_failed request_id=%s", request_id
                )
                response_payload = sales_result

        final_status = "completed"
        update_sales_progress(request_id, "completed", result=response_payload)
        logger.info(
            "sales_background_completed request_id=%s worker=%s "
            "extraction_duration=%.3f",
            request_id,
            worker_index,
            float(extraction_duration_seconds or 0.0),
        )
    except Exception as exc:
        progress = get_sales_progress(request_id) or {}
        if not progress.get("error"):
            update_sales_progress(
                request_id,
                "failed",
                error={
                    "error": "extraction_failed",
                    "message": str(exc)[:500],
                },
            )
        if final_status == "failed" and not isinstance(
            exc, (SalesExtractionDeadlineExceeded, asyncio.TimeoutError)
        ):
            final_status = "failed"
        logger.warning(
            "sales_background_failed request_id=%s worker=%s final_status=%s "
            "error=%s",
            request_id,
            worker_index,
            final_status,
            type(exc).__name__,
        )
    finally:
        if slot_acquired:
            release_sales_extraction_slot()
        total_duration_seconds = time.time() - total_started
        finish_sales_progress(
            request_id,
            final_status,
            queue_wait_seconds=queue_wait_seconds,
            extraction_duration_seconds=extraction_duration_seconds,
            file_type=file_type,
        )
        log_sales_extraction_event(
            request_id,
            filename=filename,
            file_type=file_type if isinstance(file_type, str) else None,
            queue_wait_seconds=queue_wait_seconds,
            extraction_duration_seconds=extraction_duration_seconds,
            total_duration_seconds=total_duration_seconds,
            final_status=final_status,
        )
        _bg_unmark_scheduled(request_id)


def datetime_now_fallback():
    from datetime import datetime

    return datetime.now()


def store_sales_progress_result(request_id: str, result: Any) -> None:
    update_sales_progress(request_id, "completed", result=result)


async def submit_sales_extraction_background(
    *,
    request_id: str,
    filename: str,
    file_bytes: bytes,
    file_type: Optional[str] = None,
    batch_id: Optional[str] = None,
    deadline_seconds: Optional[float] = None,
    split_id: Optional[str] = None,
    file_name: Optional[str] = None,
    use_blob_storage: bool = False,
    blob_container: Optional[str] = None,
    target_invoices_blob_folder: Optional[str] = None,
) -> Dict[str, Any]:
    """Schedule extraction for already-buffered bytes; return accept metadata.

    Does not run OCR/Gemini inline. Process-local queue only — lost on restart.
    """
    with _bg_scheduled_lock:
        already = request_id in _bg_scheduled_ids
        active = sales_job_is_active(request_id)
        if already or active:
            progress = get_sales_progress(request_id) or {}
            logger.info(
                "sales_submit_idempotent request_id=%s stage=%s status=%s",
                request_id,
                progress.get("stage"),
                progress.get("status"),
            )
            return {
                "accepted": True,
                "already_queued": True,
                "request_id": request_id,
                "status": progress.get("status") or "queued",
                "stage": progress.get("stage") or "queued",
            }
        _bg_scheduled_ids.add(request_id)

    begin_sales_progress(
        request_id, filename, stage="queued", batch_id=batch_id
    )
    update_sales_progress(request_id, "queued", file_type=file_type)

    job = {
        "request_id": request_id,
        "filename": filename,
        "file_bytes": file_bytes,
        "file_type": file_type,
        "batch_id": batch_id,
        "deadline_seconds": float(
            deadline_seconds or SALES_EXTRACTION_MAX_EXECUTION_SECONDS
        ),
        "split_id": split_id,
        "file_name": file_name,
        "use_blob_storage": use_blob_storage,
        "blob_container": blob_container,
        "target_invoices_blob_folder": target_invoices_blob_folder,
    }
    try:
        await enqueue_sales_background_job(job)
    except Exception:
        _bg_unmark_scheduled(request_id)
        raise
    logger.info(
        "sales_submit_accepted request_id=%s filename=%s file_type=%s bytes=%s",
        request_id,
        filename,
        file_type,
        len(file_bytes or b""),
    )
    return {
        "accepted": True,
        "already_queued": False,
        "request_id": request_id,
        "status": "queued",
        "stage": "queued",
    }


def start_sales_deadline(
    request_id: str,
    limit_seconds: Optional[float] = None,
    file_size: Optional[int] = None,
) -> float:
    limit = float(
        SALES_EXTRACTION_MAX_EXECUTION_SECONDS
        if limit_seconds is None
        else limit_seconds
    )
    with _cancel_lock:
        flag = _cancel_flags.get(request_id)
        if flag is None:
            flag = threading.Event()
            _cancel_flags[request_id] = flag
        flag.clear()
    ctx = SalesExtractionRuntimeContext(
        request_id=request_id,
        limit_seconds=limit,
        cancel_event=flag,
        file_size=file_size,
    )
    _deadline_local.ctx = ctx
    # Mirror fields for existing helpers that read attrs directly.
    _deadline_local.request_id = ctx.request_id
    _deadline_local.limit_seconds = ctx.limit_seconds
    _deadline_local.deadline_mono = ctx.deadline_mono
    _deadline_local.ocr_calls = 0
    _deadline_local.ocr_cache_hits = 0
    _deadline_local.ocr_duration_ms = 0.0
    _deadline_local.gemini_calls = 0
    _deadline_local.gemini_retries = 0
    _deadline_local.gemini_duration_ms = 0.0
    _deadline_local.ocr_cache = ctx.ocr_cache
    _deadline_local.ocr_budget = ctx.ocr_budget
    _deadline_local.started_mono = ctx.started_mono
    logger.info(
        "sales_deadline_started request_id=%s limit=%ss ocr_budget=%s file_size=%s",
        request_id,
        limit,
        SALES_OCR_MAX_CALLS_PER_REQUEST,
        file_size,
    )
    try:
        from services.stock_ocr_policy import clear_stock_ocr_request

        clear_stock_ocr_request(request_id)
    except Exception:
        logger.warning("stock_ocr_policy_reset_failed request_id=%s", request_id)
    return limit


def get_sales_runtime_context() -> Optional[SalesExtractionRuntimeContext]:
    return getattr(_deadline_local, "ctx", None)


def clear_sales_deadline() -> None:
    rid = getattr(_deadline_local, "request_id", None)
    ctx = getattr(_deadline_local, "ctx", None)
    stats: Dict[str, Any] = {}
    if isinstance(ctx, SalesExtractionRuntimeContext):
        stats = ctx.snapshot()
    elif rid is not None:
        started = float(getattr(_deadline_local, "started_mono", 0) or 0)
        stats = {
            "request_id": rid,
            "ocr_calls": int(getattr(_deadline_local, "ocr_calls", 0) or 0),
            "ocr_cache_hits": int(getattr(_deadline_local, "ocr_cache_hits", 0) or 0),
            "ocr_seconds": round(
                float(getattr(_deadline_local, "ocr_duration_ms", 0) or 0) / 1000.0, 3
            ),
            "gemini_calls": int(getattr(_deadline_local, "gemini_calls", 0) or 0),
            "gemini_retries": int(getattr(_deadline_local, "gemini_retries", 0) or 0),
            "vision_seconds": round(
                float(getattr(_deadline_local, "gemini_duration_ms", 0) or 0) / 1000.0,
                3,
            ),
            "total_seconds": (
                round(time.monotonic() - started, 3) if started else None
            ),
        }
    if rid and stats:
        with _progress_lock:
            _last_runtime_stats[rid] = dict(stats)
            entry = _sales_request_progress.get(rid)
            if entry is not None:
                entry.update(stats)
        logger.info(
            "sales_performance_breakdown request_id=%s ocr_calls=%s ocr_cache_hits=%s "
            "gemini_calls=%s gemini_retries=%s ocr_seconds=%s vision_seconds=%s "
            "total_seconds=%s",
            rid,
            stats.get("ocr_calls"),
            stats.get("ocr_cache_hits"),
            stats.get("gemini_calls"),
            stats.get("gemini_retries"),
            stats.get("ocr_seconds"),
            stats.get("vision_seconds"),
            stats.get("total_seconds"),
        )
    if rid:
        with _cancel_lock:
            _cancel_flags.pop(rid, None)
        try:
            from services.stock_ocr_policy import clear_stock_ocr_request

            clear_stock_ocr_request(rid)
        except Exception:
            logger.warning("stock_ocr_policy_clear_failed request_id=%s", rid)
    for attr in (
        "ctx",
        "request_id",
        "limit_seconds",
        "deadline_mono",
        "ocr_calls",
        "ocr_cache_hits",
        "ocr_duration_ms",
        "gemini_calls",
        "gemini_retries",
        "gemini_duration_ms",
        "ocr_cache",
        "ocr_budget",
        "started_mono",
    ):
        if hasattr(_deadline_local, attr):
            delattr(_deadline_local, attr)


def get_last_runtime_stats(request_id: str) -> Optional[Dict[str, Any]]:
    with _progress_lock:
        stats = _last_runtime_stats.get(request_id)
        return dict(stats) if stats else None


def request_cancel_sales_extraction(request_id: str) -> None:
    with _cancel_lock:
        flag = _cancel_flags.get(request_id)
        if flag is not None:
            flag.set()
    logger.warning("sales_extraction_cancel_requested request_id=%s", request_id)


def sales_extraction_cancelled(request_id: Optional[str] = None) -> bool:
    rid = request_id or getattr(_deadline_local, "request_id", None)
    if not rid:
        return False
    with _cancel_lock:
        flag = _cancel_flags.get(rid)
        return bool(flag and flag.is_set())


def sales_deadline_remaining_seconds() -> Optional[float]:
    deadline = getattr(_deadline_local, "deadline_mono", None)
    if deadline is None:
        return None
    return deadline - time.monotonic()


def sales_deadline_active() -> bool:
    return getattr(_deadline_local, "deadline_mono", None) is not None


def check_sales_deadline(stage: str = "parsing") -> None:
    if sales_extraction_cancelled():
        raise SalesExtractionDeadlineExceeded(
            getattr(_deadline_local, "request_id", "unknown"),
            stage,
            float(getattr(_deadline_local, "limit_seconds", 0) or 0),
        )
    remaining = sales_deadline_remaining_seconds()
    if remaining is None:
        return
    if remaining <= 0:
        raise SalesExtractionDeadlineExceeded(
            getattr(_deadline_local, "request_id", "unknown"),
            stage,
            float(getattr(_deadline_local, "limit_seconds", 0) or 0),
        )


def mark_sales_stage(stage: str, **fields: Any) -> None:
    request_id = getattr(_deadline_local, "request_id", None)
    if not request_id:
        return
    check_sales_deadline(stage)
    ctx = getattr(_deadline_local, "ctx", None)
    if isinstance(ctx, SalesExtractionRuntimeContext):
        ctx.stage = stage
    update_sales_progress(request_id, stage, **fields)


def sales_tesseract_timeout_seconds() -> float:
    configured = float(SALES_TESSERACT_CALL_TIMEOUT_SECONDS)
    remaining = sales_deadline_remaining_seconds()
    if remaining is None:
        return configured
    return max(1.0, min(configured, remaining))


def sales_gemini_timeout_seconds(requested: int) -> int:
    try:
        base = int(requested or 1)
    except (TypeError, ValueError):
        base = 1
    remaining = sales_deadline_remaining_seconds()
    if remaining is None:
        return max(1, base)
    return max(1, min(base, int(max(1.0, remaining))))


def sales_ocr_calls() -> int:
    return int(getattr(_deadline_local, "ocr_calls", 0) or 0)


def sales_ocr_budget_remaining() -> Optional[int]:
    budget = getattr(_deadline_local, "ocr_budget", None)
    if budget is None:
        return None
    return max(0, int(budget) - sales_ocr_calls())


def sales_ocr_budget_exhausted() -> bool:
    remaining = sales_ocr_budget_remaining()
    return remaining is not None and remaining <= 0


def note_sales_gemini_call(duration_ms: float = 0.0, *, retry: bool = False) -> None:
    if not hasattr(_deadline_local, "gemini_calls"):
        return
    _deadline_local.gemini_calls = int(
        getattr(_deadline_local, "gemini_calls", 0) or 0
    ) + 1
    _deadline_local.gemini_duration_ms = float(
        getattr(_deadline_local, "gemini_duration_ms", 0) or 0
    ) + float(duration_ms or 0)
    if retry:
        _deadline_local.gemini_retries = int(
            getattr(_deadline_local, "gemini_retries", 0) or 0
        ) + 1
    ctx = getattr(_deadline_local, "ctx", None)
    if isinstance(ctx, SalesExtractionRuntimeContext):
        ctx.gemini_calls = int(_deadline_local.gemini_calls)
        ctx.gemini_duration_ms = float(_deadline_local.gemini_duration_ms)
        ctx.gemini_retries = int(getattr(_deadline_local, "gemini_retries", 0) or 0)


def sales_sleep_respecting_deadline(seconds: float, stage: str = "gemini_retry") -> None:
    """Sleep for backoff but never past the extraction deadline."""
    check_sales_deadline(stage)
    remaining = sales_deadline_remaining_seconds()
    sleep_for = max(0.0, float(seconds or 0.0))
    if remaining is not None:
        # Leave a tiny margin so the next check fails cleanly.
        sleep_for = min(sleep_for, max(0.0, remaining - 0.25))
    if sleep_for <= 0:
        check_sales_deadline(stage)
        return
    if stage == "gemini_retry" and hasattr(_deadline_local, "gemini_retries"):
        _deadline_local.gemini_retries = int(
            getattr(_deadline_local, "gemini_retries", 0) or 0
        ) + 1
        ctx = getattr(_deadline_local, "ctx", None)
        if isinstance(ctx, SalesExtractionRuntimeContext):
            ctx.gemini_retries = int(_deadline_local.gemini_retries)
    time.sleep(sleep_for)
    check_sales_deadline(stage)


def _ocr_cache_key(args: tuple, kwargs: dict) -> str:
    import hashlib

    image = args[0] if args else kwargs.get("image")
    config = str(kwargs.get("config", "") or "")
    h = hashlib.sha256()
    h.update(config.encode("utf-8", errors="ignore"))
    try:
        if hasattr(image, "size") and hasattr(image, "mode"):
            w, ht = image.size
            h.update(f"{w}x{ht}:{image.mode}".encode())
            raw = image.tobytes()
            h.update(str(len(raw)).encode())
            h.update(raw[:8192])
            if len(raw) > 16384:
                mid = len(raw) // 2
                h.update(raw[mid : mid + 4096])
            if len(raw) > 8192:
                h.update(raw[-4096:])
        elif isinstance(image, (bytes, bytearray, memoryview)):
            blob = bytes(image)
            h.update(str(len(blob)).encode())
            h.update(blob[:8192])
            if len(blob) > 8192:
                h.update(blob[-4096:])
        else:
            h.update(repr(type(image)).encode())
            h.update(str(id(image)).encode())
    except Exception:
        h.update(str(id(image)).encode())
    return h.hexdigest()


class _OcrCacheMiss:
    pass


_OCR_CACHE_MISS = _OcrCacheMiss()


def _ocr_cache_get(key: str) -> Any:
    cache = getattr(_deadline_local, "ocr_cache", None)
    if not isinstance(cache, dict):
        return _OCR_CACHE_MISS
    if key not in cache:
        return _OCR_CACHE_MISS
    _deadline_local.ocr_cache_hits = int(
        getattr(_deadline_local, "ocr_cache_hits", 0) or 0
    ) + 1
    return cache[key]


def _ocr_cache_put(key: str, value: Any) -> None:
    cache = getattr(_deadline_local, "ocr_cache", None)
    if not isinstance(cache, dict):
        return
    if len(cache) >= int(SALES_OCR_CACHE_MAX_ENTRIES):
        # Drop an arbitrary oldest-ish entry (FIFO via insertion order).
        try:
            cache.pop(next(iter(cache)))
        except Exception:
            cache.clear()
    cache[key] = value


def terminate_sales_tesseract_children() -> None:
    try:
        from services.reliability import _terminate_tesseract_children

        _terminate_tesseract_children()
    except Exception as exc:
        logger.warning("sales_tesseract_terminate_failed error=%s", exc)


def run_bounded_sales_tesseract(fn: Callable[[], Any], label: str) -> Any:
    check_sales_deadline("ocr")
    mark_sales_stage("ocr")
    timeout = sales_tesseract_timeout_seconds()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="sales-tess") as pool:
        fut = pool.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except FuturesTimeoutError as exc:
            logger.error(
                "sales_tesseract_timeout label=%s timeout=%ss request_id=%s",
                label,
                timeout,
                getattr(_deadline_local, "request_id", None),
            )
            terminate_sales_tesseract_children()
            raise TimeoutError(
                f"Sales Tesseract call timed out after {timeout}s"
            ) from exc


@contextmanager
def sales_extraction_slot_sync(timeout: Optional[float] = None):
    global _sales_extraction_waiting, _sales_extraction_active
    timeout = (
        float(SALES_EXTRACTION_QUEUE_TIMEOUT)
        if timeout is None
        else float(timeout)
    )
    with _sales_extraction_waiters_lock:
        _sales_extraction_waiting += 1
    started = time.monotonic()
    if not hasattr(sales_extraction_slot_sync, "_sem"):
        sales_extraction_slot_sync._sem = threading.Semaphore(  # type: ignore[attr-defined]
            MAX_CONCURRENT_EXTRACTIONS
        )
    sem: threading.Semaphore = sales_extraction_slot_sync._sem  # type: ignore[attr-defined]
    acquired = sem.acquire(timeout=timeout)
    with _sales_extraction_waiters_lock:
        _sales_extraction_waiting = max(0, _sales_extraction_waiting - 1)
    if not acquired:
        raise SalesExtractionBusy(
            time.monotonic() - started,
            MAX_CONCURRENT_EXTRACTIONS,
            timeout,
        )
    with _sales_extraction_waiters_lock:
        _sales_extraction_active += 1
    try:
        yield time.monotonic() - started
    finally:
        with _sales_extraction_waiters_lock:
            _sales_extraction_active = max(0, _sales_extraction_active - 1)
        sem.release()


async def acquire_sales_extraction_slot(
    timeout: Optional[float] = None,
) -> float:
    global _sales_extraction_waiting, _sales_extraction_active
    timeout = (
        float(SALES_EXTRACTION_QUEUE_TIMEOUT)
        if timeout is None
        else float(timeout)
    )
    with _sales_extraction_waiters_lock:
        _sales_extraction_waiting += 1
        waiting = _sales_extraction_waiting
        active = _sales_extraction_active
    logger.info(
        "sales_extraction_queued waiting=%s active=%s limit=%s timeout=%s",
        waiting,
        active,
        MAX_CONCURRENT_EXTRACTIONS,
        timeout,
    )
    started = time.monotonic()
    try:
        await asyncio.wait_for(
            _sales_extraction_async_sem.acquire(), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        with _sales_extraction_waiters_lock:
            _sales_extraction_waiting = max(0, _sales_extraction_waiting - 1)
        raise SalesExtractionBusy(
            time.monotonic() - started,
            MAX_CONCURRENT_EXTRACTIONS,
            timeout,
        ) from exc
    wait_s = time.monotonic() - started
    with _sales_extraction_waiters_lock:
        _sales_extraction_waiting = max(0, _sales_extraction_waiting - 1)
        _sales_extraction_active += 1
        active = _sales_extraction_active
    logger.info(
        "sales_extraction_started queue_wait=%.2fs active=%s limit=%s",
        wait_s,
        active,
        MAX_CONCURRENT_EXTRACTIONS,
    )
    return wait_s


def release_sales_extraction_slot() -> None:
    global _sales_extraction_active
    with _sales_extraction_waiters_lock:
        _sales_extraction_active = max(0, _sales_extraction_active - 1)
        active = _sales_extraction_active
    _sales_extraction_async_sem.release()
    logger.info(
        "sales_extraction_finished active=%s limit=%s",
        active,
        MAX_CONCURRENT_EXTRACTIONS,
    )


def gemini_active_slots() -> int:
    with _gemini_call_lock:
        return int(_gemini_call_active)


def get_gemini_provider_metrics() -> Dict[str, int]:
    with _provider_cooldown_lock:
        return dict(_gemini_provider_metrics)


def get_gemini_provider_cooldown_remaining() -> float:
    with _provider_cooldown_lock:
        return max(0.0, float(_provider_cooldown_until_mono) - time.monotonic())


def _bump_gemini_metric(name: str, amount: int = 1) -> None:
    with _provider_cooldown_lock:
        _gemini_provider_metrics[name] = int(
            _gemini_provider_metrics.get(name, 0) or 0
        ) + int(amount)


def _compute_429_cooldown_seconds(streak: int) -> float:
    """Exponential backoff with jitter; capped by GEMINI_PROVIDER_429_MAX_COOLDOWN_SECONDS.

    Tuned so streak 1 ≈ 10–15s, streak 2 ≈ 20–30s, streak 3+ ≈ 45–60s+ when
    GEMINI_PROVIDER_429_COOLDOWN_SECONDS=30.
    """
    base = float(GEMINI_PROVIDER_429_COOLDOWN_SECONDS)
    cap = float(GEMINI_PROVIDER_429_MAX_COOLDOWN_SECONDS)
    streak_i = max(1, int(streak or 1))
    delay = base * (0.4 * (2 ** (streak_i - 1)))
    delay = min(delay, cap)
    jitter = random.uniform(0.85, 1.15)
    return max(1.0, min(cap, delay * jitter))


def note_gemini_provider_429(
    *,
    model: str = "",
    label: str = "gemini",
    attempt: int = 1,
) -> float:
    """Record a Vertex 429 and extend the process-wide cooldown. Returns cooldown seconds."""
    global _provider_cooldown_until_mono, _provider_429_streak, _provider_cooldown_active
    request_id = getattr(_deadline_local, "request_id", None) or "unknown"
    with _provider_cooldown_lock:
        _provider_429_streak = int(_provider_429_streak or 0) + 1
        streak = _provider_429_streak
        cooldown = _compute_429_cooldown_seconds(streak)
        new_until = time.monotonic() + cooldown
        if new_until > _provider_cooldown_until_mono:
            _provider_cooldown_until_mono = new_until
        started = not _provider_cooldown_active
        _provider_cooldown_active = True
        _gemini_provider_metrics["gemini_429_count"] = int(
            _gemini_provider_metrics.get("gemini_429_count", 0) or 0
        ) + 1
        _gemini_provider_metrics["gemini_provider_cooldown_count"] = int(
            _gemini_provider_metrics.get("gemini_provider_cooldown_count", 0) or 0
        ) + 1
        rem = max(0.0, _provider_cooldown_until_mono - time.monotonic())
    deadline_rem = sales_deadline_remaining_seconds()
    logger.warning(
        "gemini_provider_429 request_id=%s gemini_label=%s model=%s attempt=%s "
        "provider_status=429 active_gemini_slots=%s gemini_limit=%s "
        "provider_cooldown_remaining=%.1f remaining_deadline=%s streak=%s",
        request_id,
        label,
        model,
        attempt,
        gemini_active_slots(),
        MAX_CONCURRENT_GEMINI_REQUESTS,
        rem,
        None if deadline_rem is None else round(deadline_rem, 1),
        streak,
    )
    if started:
        logger.warning(
            "gemini_provider_cooldown_started request_id=%s cooldown=%.1f "
            "remaining_deadline=%s",
            request_id,
            rem,
            None if deadline_rem is None else round(deadline_rem, 1),
        )
    return rem


def note_gemini_provider_success() -> None:
    global _provider_429_streak
    with _provider_cooldown_lock:
        _provider_429_streak = 0
        _gemini_provider_metrics["gemini_success_count"] = int(
            _gemini_provider_metrics.get("gemini_success_count", 0) or 0
        ) + 1


def note_gemini_provider_transient(code: str) -> None:
    if code == "503":
        _bump_gemini_metric("gemini_503_count")
    elif code == "timeout":
        _bump_gemini_metric("gemini_timeout_count")


def reset_gemini_provider_cooldown_state_for_tests() -> None:
    """Test helper — clears process-wide cooldown and metrics."""
    global _provider_cooldown_until_mono, _provider_429_streak, _provider_cooldown_active
    with _provider_cooldown_lock:
        _provider_cooldown_until_mono = 0.0
        _provider_429_streak = 0
        _provider_cooldown_active = False
        for key in list(_gemini_provider_metrics.keys()):
            _gemini_provider_metrics[key] = 0


def wait_gemini_provider_cooldown(
    *,
    label: str = "gemini",
    model: str = "",
) -> None:
    """Block until shared provider cooldown ends. Does NOT hold the Gemini semaphore."""
    global _provider_cooldown_active, _provider_cooldown_until_mono
    request_id = getattr(_deadline_local, "request_id", None) or "unknown"
    waited = False
    while True:
        if sales_deadline_active():
            check_sales_deadline("gemini_retry")
            deadline_rem = sales_deadline_remaining_seconds() or 0.0
            rem = get_gemini_provider_cooldown_remaining()
            # Cannot wait out provider cooldown within the remaining extraction budget.
            if rem > 0.0 and deadline_rem < max(3.0, min(rem, 3.0)):
                raise SalesExtractionDeadlineExceeded(
                    request_id,
                    "gemini_retry",
                    float(getattr(_deadline_local, "limit_seconds", 0) or 0),
                )
        rem = get_gemini_provider_cooldown_remaining()
        if rem <= 0:
            if waited:
                with _provider_cooldown_lock:
                    _provider_cooldown_active = False
                deadline_rem = sales_deadline_remaining_seconds()
                logger.info(
                    "gemini_provider_cooldown_finished request_id=%s gemini_label=%s "
                    "model=%s remaining_deadline=%s",
                    request_id,
                    label,
                    model,
                    None if deadline_rem is None else round(deadline_rem, 1),
                )
            return
        waited = True
        deadline_rem = sales_deadline_remaining_seconds()
        logger.info(
            "gemini_provider_waiting_cooldown request_id=%s gemini_label=%s model=%s "
            "provider_cooldown_remaining=%.1f active_gemini_slots=%s gemini_limit=%s "
            "remaining_deadline=%s",
            request_id,
            label,
            model,
            rem,
            gemini_active_slots(),
            MAX_CONCURRENT_GEMINI_REQUESTS,
            None if deadline_rem is None else round(deadline_rem, 1),
        )
        chunk = min(rem, 5.0)
        before = time.monotonic()
        if sales_deadline_active():
            # Raises SalesExtractionDeadlineExceeded if cooldown exceeds remaining budget.
            sales_sleep_respecting_deadline(chunk, "gemini_retry")
        else:
            time.sleep(max(0.05, chunk))
        # If sleep was mocked or returned early, still consume cooldown budget so
        # waiters cannot spin forever while holding no progress.
        elapsed = time.monotonic() - before
        if elapsed < (chunk * 0.5):
            with _provider_cooldown_lock:
                _provider_cooldown_until_mono = min(
                    _provider_cooldown_until_mono,
                    time.monotonic() + max(0.0, rem - chunk),
                )


@contextmanager
def gemini_call_slot(label: str = "gemini_call"):
    """Acquire Gemini concurrency slot (re-entrant for nested sales wrappers)."""
    global _gemini_call_active, _gemini_call_waiting
    depth = int(getattr(_gemini_slot_depth, "n", 0) or 0)
    if depth > 0:
        _gemini_slot_depth.n = depth + 1
        try:
            yield
        finally:
            _gemini_slot_depth.n = max(0, int(getattr(_gemini_slot_depth, "n", 1)) - 1)
        return

    check_sales_deadline("gemini_request")
    is_retry = "retry" in (label or "").lower()
    if is_retry:
        mark_sales_stage("gemini_retry")
    else:
        mark_sales_stage("gemini_request")
    with _gemini_call_lock:
        _gemini_call_waiting += 1
        waiting = _gemini_call_waiting
        active = _gemini_call_active
    logger.info(
        "gemini_call_queued label=%s active=%s waiting=%s limit=%s",
        label,
        active,
        waiting,
        MAX_CONCURRENT_GEMINI_REQUESTS,
    )
    _gemini_call_semaphore.acquire()
    with _gemini_call_lock:
        _gemini_call_waiting = max(0, _gemini_call_waiting - 1)
        _gemini_call_active += 1
        active = _gemini_call_active
    logger.info(
        "gemini_call_started label=%s active=%s limit=%s",
        label,
        active,
        MAX_CONCURRENT_GEMINI_REQUESTS,
    )
    _gemini_slot_depth.n = 1
    started = time.monotonic()
    try:
        # Re-check after waiting for the slot.
        check_sales_deadline("gemini_request")
        yield
    finally:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        note_sales_gemini_call(elapsed_ms, retry=is_retry)
        _gemini_slot_depth.n = 0
        with _gemini_call_lock:
            _gemini_call_active = max(0, _gemini_call_active - 1)
            active = _gemini_call_active
        _gemini_call_semaphore.release()
        rem = sales_deadline_remaining_seconds()
        logger.info(
            "gemini_call_finished label=%s active=%s limit=%s duration_ms=%.1f "
            "remaining_deadline=%s",
            label,
            active,
            MAX_CONCURRENT_GEMINI_REQUESTS,
            elapsed_ms,
            None if rem is None else round(rem, 1),
        )


def sales_generate_content_via_vertex(
    *,
    model: str,
    payload: dict,
    timeout: int = 120,
    label: str = "sales_vertex",
) -> Any:
    """All sales-statement Gemini HTTP calls must go through the Gemini slot.

    Preserves Vertex client error semantics (GeminiProviderError) while enforcing
    MAX_CONCURRENT_GEMINI_REQUESTS, the request deadline, and a process-wide
    Vertex 429 cooldown. The Gemini semaphore is never held during cooldown sleep.
    """
    from services.vertex_gemini_client import (
        GeminiProviderError,
        generate_content_via_vertex as _raw,
    )

    request_id = getattr(_deadline_local, "request_id", None) or "unknown"
    attempt_429 = 0
    attempt_transient = 0
    max_retries = int(GEMINI_PROVIDER_429_MAX_RETRIES)

    while True:
        check_sales_deadline("gemini_request")
        rem = sales_deadline_remaining_seconds()
        if rem is not None and rem < 3.0:
            raise SalesExtractionDeadlineExceeded(
                request_id,
                "gemini_request",
                float(getattr(_deadline_local, "limit_seconds", 0) or 0),
            )

        # Shared cooldown wait — outside the semaphore so slots stay free.
        wait_gemini_provider_cooldown(label=label, model=model)

        rem = sales_deadline_remaining_seconds()
        if rem is not None and rem < 3.0:
            raise SalesExtractionDeadlineExceeded(
                request_id,
                "gemini_request",
                float(getattr(_deadline_local, "limit_seconds", 0) or 0),
            )

        eff_timeout = sales_gemini_timeout_seconds(int(timeout or 120))
        deadline_rem = sales_deadline_remaining_seconds()
        logger.info(
            "gemini_request_attempt request_id=%s gemini_label=%s model=%s "
            "attempt=%s provider_status=calling active_gemini_slots=%s "
            "gemini_limit=%s provider_cooldown_remaining=%.1f remaining_deadline=%s",
            request_id,
            label,
            model,
            attempt_429 + attempt_transient + 1,
            gemini_active_slots(),
            MAX_CONCURRENT_GEMINI_REQUESTS,
            get_gemini_provider_cooldown_remaining(),
            None if deadline_rem is None else round(deadline_rem, 1),
        )
        try:
            with gemini_call_slot(label):
                response = _raw(model=model, payload=payload, timeout=eff_timeout)
            note_gemini_provider_success()
            return response
        except GeminiProviderError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            if code == 429:
                attempt_429 += 1
                _bump_gemini_metric("gemini_retry_count")
                note_gemini_provider_429(
                    model=model, label=label, attempt=attempt_429
                )
                if attempt_429 >= max_retries:
                    logger.warning(
                        "gemini_provider_retry_exhausted request_id=%s "
                        "gemini_label=%s model=%s attempt=%s provider_status=429 "
                        "max_retries=%s remaining_deadline=%s",
                        request_id,
                        label,
                        model,
                        attempt_429,
                        max_retries,
                        None
                        if sales_deadline_remaining_seconds() is None
                        else round(sales_deadline_remaining_seconds() or 0.0, 1),
                    )
                    raise
                continue
            if code == 503:
                attempt_transient += 1
                note_gemini_provider_transient("503")
                _bump_gemini_metric("gemini_retry_count")
                if attempt_transient >= max_retries:
                    raise
                backoff = min(
                    float(GEMINI_PROVIDER_429_MAX_COOLDOWN_SECONDS),
                    (2 ** min(attempt_transient, 5))
                    + random.uniform(0.0, 1.0),
                )
                sales_sleep_respecting_deadline(backoff, "gemini_retry")
                continue
            raise
        except TimeoutError:
            attempt_transient += 1
            note_gemini_provider_transient("timeout")
            _bump_gemini_metric("gemini_retry_count")
            if attempt_transient >= max_retries:
                raise
            backoff = min(
                float(GEMINI_PROVIDER_429_MAX_COOLDOWN_SECONDS),
                (2 ** min(attempt_transient, 5)) + random.uniform(0.0, 1.0),
            )
            sales_sleep_respecting_deadline(backoff, "gemini_retry")
            continue
        except Exception as exc:
            code = getattr(exc, "code", None)
            try:
                code_i = int(code) if code is not None else None
            except (TypeError, ValueError):
                code_i = None
            if code_i in (400, 401, 403):
                raise
            if code_i is not None and 500 <= code_i <= 599:
                attempt_transient += 1
                _bump_gemini_metric("gemini_retry_count")
                if attempt_transient >= max_retries:
                    raise
                backoff = min(
                    float(GEMINI_PROVIDER_429_MAX_COOLDOWN_SECONDS),
                    (2 ** min(attempt_transient, 5))
                    + random.uniform(0.0, 1.0),
                )
                sales_sleep_respecting_deadline(backoff, "gemini_retry")
                continue
            raise


@contextmanager
def sales_tesseract_slot(task_label: str = "sales_tesseract"):
    try:
        from app import tesseract_ocr_slot

        with tesseract_ocr_slot(task_label):
            yield
    except Exception:
        yield


def _ocr_region_key(args: tuple, kwargs: dict) -> str:
    """Image/crop identity without PSM or operation, so retries share a region."""
    region_kwargs = dict(kwargs)
    region_kwargs["config"] = ""
    return _ocr_cache_key(args, region_kwargs)


def _stock_ocr_call(operation: str, input_key: str, region_key: str) -> Any:
    from services.stock_ocr_policy import (
        StockOcrCall,
        current_stock_ocr_meta,
        resolve_stock_ocr_region_key,
    )

    meta = current_stock_ocr_meta()
    scope = meta.scope or "page"
    page = str(meta.page or "1")
    region = str(meta.region or ("cell" if scope == "cell" else "page"))
    stable_region = resolve_stock_ocr_region_key(
        scope=scope,
        page=page,
        region=region,
        operation=operation,
        pixel_key=region_key or input_key,
    )
    return StockOcrCall(
        request_id=str(getattr(_deadline_local, "request_id", None) or "unknown"),
        operation=operation,
        input_key=input_key,
        region_key=stable_region,
        page=page,
        region=region,
        cell=str(meta.cell or "-"),
        reason=meta.reason,
        scope=scope,
    )


def _log_sales_ocr_cache_hit(label: str) -> None:
    started = getattr(_deadline_local, "started_mono", None)
    elapsed = round(time.monotonic() - float(started), 3) if started else None
    logger.info(
        "sales_ocr_cache_hit request_id=%s label=%s ocr_call_number=%s "
        "ocr_cache_hit=1 ocr_budget_remaining=%s elapsed_seconds=%s",
        getattr(_deadline_local, "request_id", None),
        label,
        sales_ocr_calls(),
        sales_ocr_budget_remaining(),
        elapsed,
    )
    ctx = getattr(_deadline_local, "ctx", None)
    if isinstance(ctx, SalesExtractionRuntimeContext):
        ctx.ocr_cache_hits = int(getattr(_deadline_local, "ocr_cache_hits", 0) or 0)


class GatedPytesseract:
    def __init__(self, real: Any):
        self._real = real

    def _run_gated(
        self,
        label: str,
        fn: Any,
        *,
        cache_key: Optional[str] = None,
        region_key: Optional[str] = None,
        operation: str = "image_to_string",
    ) -> Any:
        from services.stock_ocr_policy import empty_ocr_result, get_stock_ocr_policy

        policy = get_stock_ocr_policy()
        call = None
        if cache_key:
            call = _stock_ocr_call(operation, cache_key, region_key or cache_key)
        try:
            check_sales_deadline("ocr")
        except SalesExtractionDeadlineExceeded as exc:
            if call is not None:
                policy.note_deadline(call, exc)
            raise
        if sales_ocr_budget_exhausted():
            raise SalesOcrBudgetExceeded(
                getattr(_deadline_local, "request_id", "unknown"),
                sales_ocr_calls(),
                int(getattr(_deadline_local, "ocr_budget", 0) or 0),
            )
        if call is not None:
            decision = policy.prepare(call)
            if decision.action == "reuse":
                cached = _ocr_cache_get(cache_key)
                if cached is not _OCR_CACHE_MISS:
                    _log_sales_ocr_cache_hit(label)
                    return cached
                return decision.value
            if decision.action == "stop":
                if decision.error is not None:
                    raise decision.error
                return decision.value if decision.value is not None else empty_ocr_result(
                    operation
                )

        with sales_tesseract_slot(label):
            def _call() -> Any:
                try:
                    from services.reliability import (
                        OCR_TESSERACT_CALL_TIMEOUT_ENABLED,
                        run_tesseract_call,
                    )

                    if OCR_TESSERACT_CALL_TIMEOUT_ENABLED:
                        return fn()
                    return run_tesseract_call(fn, label=label)
                except ImportError:
                    return fn()

            started = time.monotonic()
            # Count toward budget only for real OCR work (not cache hits).
            if hasattr(_deadline_local, "ocr_calls"):
                _deadline_local.ocr_calls = int(
                    getattr(_deadline_local, "ocr_calls", 0) or 0
                ) + 1
                rem = sales_deadline_remaining_seconds()
                wall = getattr(_deadline_local, "started_mono", None)
                elapsed = (
                    round(time.monotonic() - float(wall), 3) if wall else None
                )
                logger.info(
                    "sales_ocr_call request_id=%s label=%s ocr_call_number=%s "
                    "ocr_cache_hit=0 ocr_budget_remaining=%s elapsed_seconds=%s "
                    "remaining_deadline=%s",
                    getattr(_deadline_local, "request_id", None),
                    label,
                    sales_ocr_calls(),
                    sales_ocr_budget_remaining(),
                    elapsed,
                    None if rem is None else round(rem, 1),
                )
                ctx = getattr(_deadline_local, "ctx", None)
                if isinstance(ctx, SalesExtractionRuntimeContext):
                    ctx.ocr_calls = int(_deadline_local.ocr_calls)
            try:
                result = run_bounded_sales_tesseract(_call, label)
            except BaseException as exc:
                elapsed_ms = (time.monotonic() - started) * 1000.0
                if hasattr(_deadline_local, "ocr_duration_ms"):
                    _deadline_local.ocr_duration_ms = float(
                        getattr(_deadline_local, "ocr_duration_ms", 0) or 0
                    ) + elapsed_ms
                    ctx = getattr(_deadline_local, "ctx", None)
                    if isinstance(ctx, SalesExtractionRuntimeContext):
                        ctx.ocr_duration_ms = float(_deadline_local.ocr_duration_ms)
                if call is not None:
                    policy.finish_failure(call, exc, elapsed_ms)
                raise
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if hasattr(_deadline_local, "ocr_duration_ms"):
                _deadline_local.ocr_duration_ms = float(
                    getattr(_deadline_local, "ocr_duration_ms", 0) or 0
                ) + elapsed_ms
                ctx = getattr(_deadline_local, "ctx", None)
                if isinstance(ctx, SalesExtractionRuntimeContext):
                    ctx.ocr_duration_ms = float(_deadline_local.ocr_duration_ms)
            if call is not None:
                policy.finish_success(call, result, elapsed_ms)
            if cache_key:
                _ocr_cache_put(cache_key, result)
            return result

    def image_to_string(self, *args: Any, **kwargs: Any) -> Any:
        try:
            key = _ocr_cache_key(args, kwargs)
        except Exception:
            key = None
        try:
            region_key = _ocr_region_key(args, kwargs) if key else None
        except Exception:
            region_key = key
        return self._run_gated(
            "sales_image_to_string",
            lambda: self._real.image_to_string(*args, **kwargs),
            cache_key=key,
            region_key=region_key,
            operation="image_to_string",
        )

    def image_to_data(self, *args: Any, **kwargs: Any) -> Any:
        try:
            key = "data:" + _ocr_cache_key(args, kwargs)
        except Exception:
            key = None
        try:
            region_key = _ocr_region_key(args, kwargs) if key else None
        except Exception:
            region_key = key
        return self._run_gated(
            "sales_image_to_data",
            lambda: self._real.image_to_data(*args, **kwargs),
            cache_key=key,
            region_key=region_key,
            operation="image_to_data",
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def wrap_pytesseract_module(real: Any) -> GatedPytesseract:
    if isinstance(real, GatedPytesseract):
        return real
    return GatedPytesseract(real)


def sales_effective_ocr_pool_workers(requested: int) -> int:
    try:
        from app import effective_ocr_pool_workers

        return effective_ocr_pool_workers(requested)
    except Exception:
        limit = _env_positive_int("MAX_TESSERACT_CONCURRENCY", 6)
        try:
            requested_i = int(requested or 1)
        except (TypeError, ValueError):
            requested_i = 1
        return max(1, min(requested_i, limit))


def run_sales_extract_with_deadline(
    extract_fn: Callable[..., Any],
    file_bytes: bytes,
    filename: str,
    request_id: str,
    limit_seconds: Optional[float] = None,
) -> Any:
    start_sales_deadline(
        request_id,
        limit_seconds,
        file_size=len(file_bytes) if file_bytes is not None else None,
    )
    lower = (filename or "").lower()
    try:
        doc_class = classify_sales_document(filename, file_bytes)
        mark_sales_stage(
            "file_detection",
            file_type=lower.rsplit(".", 1)[-1] if "." in lower else None,
            document_class=doc_class,
            file_size=len(file_bytes) if file_bytes is not None else None,
        )
        logger.info(
            "sales_document_classified request_id=%s filename=%s class=%s file_size=%s",
            request_id,
            filename,
            doc_class,
            len(file_bytes) if file_bytes is not None else None,
        )
        if lower.endswith((".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp")):
            mark_sales_stage("image_processing")
        elif lower.endswith(".pdf"):
            mark_sales_stage("pdf_processing")
        else:
            mark_sales_stage("parsing")
        result = extract_fn(file_bytes, filename)
        check_sales_deadline("merging")
        mark_sales_stage("quality_check")
        return result
    except Exception:
        # Ensure progress becomes terminal even if caller forgets.
        try:
            update_sales_progress(request_id, "failed")
        except Exception:
            pass
        raise
    finally:
        clear_sales_deadline()


def log_sales_extraction_event(
    request_id: str,
    *,
    filename: str,
    file_type: Optional[str] = None,
    page_count: Optional[int] = None,
    extraction_method: Optional[str] = None,
    ocr_quality: Optional[Any] = None,
    vision_fallback: Optional[bool] = None,
    queue_wait_seconds: Optional[float] = None,
    extraction_duration_seconds: Optional[float] = None,
    total_duration_seconds: Optional[float] = None,
    final_status: Optional[str] = None,
    failure_reason: Optional[str] = None,
) -> None:
    stats = get_last_runtime_stats(request_id) or {}
    logger.info(
        "sales_extraction_event request_id=%s filename=%s file_type=%s "
        "file_size=%s page_count=%s extraction_method=%s ocr_quality=%s "
        "vision_fallback=%s queue_wait=%.3f preparation_time=%s "
        "ocr_time=%s vision_time=%s validation_time=%s "
        "extraction_duration=%.3f total_duration=%.3f "
        "ocr_calls=%s ocr_cache_hits=%s gemini_calls=%s gemini_retries=%s "
        "final_status=%s failure_reason=%s",
        request_id,
        filename,
        file_type,
        stats.get("file_size"),
        page_count,
        extraction_method,
        ocr_quality,
        vision_fallback,
        float(queue_wait_seconds or 0.0),
        round(float(queue_wait_seconds or 0.0), 3),
        stats.get("ocr_seconds"),
        stats.get("vision_seconds"),
        None,
        float(extraction_duration_seconds or 0.0),
        float(total_duration_seconds or 0.0),
        stats.get("ocr_calls"),
        stats.get("ocr_cache_hits"),
        stats.get("gemini_calls"),
        stats.get("gemini_retries"),
        final_status,
        failure_reason,
    )
    logger.info(
        "sales_performance_summary %s",
        {
            "request_id": request_id,
            "ocr_calls": stats.get("ocr_calls"),
            "ocr_cache_hits": stats.get("ocr_cache_hits"),
            "gemini_calls": stats.get("gemini_calls"),
            "gemini_retries": stats.get("gemini_retries"),
            "ocr_seconds": stats.get("ocr_seconds"),
            "vision_seconds": stats.get("vision_seconds"),
            "total_seconds": round(float(total_duration_seconds or 0.0), 3),
            "final_status": final_status,
        },
    )
