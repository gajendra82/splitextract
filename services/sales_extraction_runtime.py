"""Secondary Sales extraction runtime: admission, Gemini call slots, progress.

Does NOT change parsers, quality gates, or Gemini prompts.
Reuses app.tesseract_ocr_slot and app.call_gemini_with_quota / RPM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Optional

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

# Async admission for POST /extract-sales-statement (event-loop safe).
_sales_extraction_async_sem = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)
_sales_extraction_waiters_lock = threading.Lock()
_sales_extraction_waiting = 0
_sales_extraction_active = 0

# Caps simultaneous Gemini HTTP calls; RPM/RPD stay in call_gemini_with_quota.
_gemini_call_semaphore = threading.Semaphore(MAX_CONCURRENT_GEMINI_REQUESTS)
_gemini_call_lock = threading.Lock()
_gemini_call_active = 0
_gemini_call_waiting = 0

# Per-request observability (does not alter response JSON).
_progress_lock = threading.Lock()
_sales_request_progress: Dict[str, Dict[str, Any]] = {}


def resolve_request_id(header_value: Optional[str] = None) -> str:
    raw = (header_value or "").strip()
    if raw:
        return raw[:128]
    return uuid.uuid4().hex[:12]


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
            "started_at": now,
            "updated_at": now,
            "queue_wait_seconds": None,
            "extraction_duration_seconds": None,
            "final_status": None,
            "file_type": None,
            "page_count": None,
            "extraction_method": None,
            "ocr_quality": None,
            "vision_fallback": None,
        }
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
    with _progress_lock:
        entry = _sales_request_progress.get(request_id)
        if entry is None:
            entry = {"request_id": request_id, "started_at": time.time()}
            _sales_request_progress[request_id] = entry
        entry["stage"] = stage
        entry["updated_at"] = time.time()
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
    update_sales_progress(
        request_id,
        "completed" if final_status == "completed" else "failed",
        final_status=final_status,
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
        return dict(entry) if entry else None


class SalesExtractionBusy(Exception):
    """Raised when the extraction admission queue wait times out."""

    def __init__(self, waited: float, limit: int, timeout: float):
        self.waited = waited
        self.limit = limit
        self.timeout = timeout
        super().__init__(
            f"Sales extraction busy: waited {waited:.1f}s for one of "
            f"{limit} slots (timeout={timeout}s)"
        )


@contextmanager
def sales_extraction_slot_sync(timeout: Optional[float] = None):
    """Threading helper for tests; production endpoint uses async acquire."""
    global _sales_extraction_waiting, _sales_extraction_active
    timeout = (
        float(SALES_EXTRACTION_QUEUE_TIMEOUT)
        if timeout is None
        else float(timeout)
    )
    with _sales_extraction_waiters_lock:
        _sales_extraction_waiting += 1
    started = time.monotonic()
    # Mirror async limit with a threading semaphore for unit tests.
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
    """Wait for an extraction slot. Returns queue_wait_seconds."""
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


@contextmanager
def gemini_call_slot(label: str = "gemini_call"):
    """Limit simultaneous Gemini HTTP calls; RPM stays in call_gemini_with_quota."""
    global _gemini_call_active, _gemini_call_waiting
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
    try:
        yield
    finally:
        with _gemini_call_lock:
            _gemini_call_active = max(0, _gemini_call_active - 1)
            active = _gemini_call_active
        _gemini_call_semaphore.release()
        logger.info(
            "gemini_call_finished label=%s active=%s limit=%s",
            label,
            active,
            MAX_CONCURRENT_GEMINI_REQUESTS,
        )


@contextmanager
def sales_tesseract_slot(task_label: str = "sales_tesseract"):
    """Reuse app.tesseract_ocr_slot; never a separate sales OCR pool."""
    try:
        from app import tesseract_ocr_slot

        with tesseract_ocr_slot(task_label):
            yield
    except Exception:
        # Import-time / test environments without full app wiring.
        yield


class GatedPytesseract:
    """Proxy that runs image_to_string / image_to_data inside tesseract_ocr_slot."""

    def __init__(self, real: Any):
        self._real = real

    def _run_gated(self, label: str, fn: Any) -> Any:
        with sales_tesseract_slot(label):
            try:
                from services.reliability import run_tesseract_call
            except ImportError:
                return fn()
            return run_tesseract_call(fn, label=label)

    def image_to_string(self, *args: Any, **kwargs: Any) -> Any:
        return self._run_gated(
            "sales_image_to_string",
            lambda: self._real.image_to_string(*args, **kwargs),
        )

    def image_to_data(self, *args: Any, **kwargs: Any) -> Any:
        return self._run_gated(
            "sales_image_to_data",
            lambda: self._real.image_to_data(*args, **kwargs),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def wrap_pytesseract_module(real: Any) -> GatedPytesseract:
    if isinstance(real, GatedPytesseract):
        return real
    return GatedPytesseract(real)


def sales_effective_ocr_pool_workers(requested: int) -> int:
    """Cap nested OCR thread pools by global Tesseract concurrency."""
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
) -> None:
    logger.info(
        "sales_extraction_event request_id=%s filename=%s file_type=%s "
        "page_count=%s extraction_method=%s ocr_quality=%s vision_fallback=%s "
        "queue_wait=%.3f extraction_duration=%.3f total_duration=%.3f "
        "final_status=%s",
        request_id,
        filename,
        file_type,
        page_count,
        extraction_method,
        ocr_quality,
        vision_fallback,
        float(queue_wait_seconds or 0.0),
        float(extraction_duration_seconds or 0.0),
        float(total_duration_seconds or 0.0),
        final_status,
    )
