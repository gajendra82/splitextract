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
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import contextmanager
from typing import Any, Callable, Dict, Optional, Tuple

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
SALES_OCR_MAX_IMAGE_SIDE = _env_positive_int("SALES_OCR_MAX_IMAGE_SIDE", 10000)

_sales_extraction_async_sem = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)
_sales_extraction_waiters_lock = threading.Lock()
_sales_extraction_waiting = 0
_sales_extraction_active = 0

_gemini_call_semaphore = threading.Semaphore(MAX_CONCURRENT_GEMINI_REQUESTS)
_gemini_call_lock = threading.Lock()
_gemini_call_active = 0
_gemini_call_waiting = 0
_gemini_slot_depth = threading.local()

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
    stage = "completed" if final_status == "completed" else "failed"
    update_sales_progress(
        request_id,
        stage,
        final_status=final_status,
        status="completed" if final_status == "completed" else "failed",
        label=stage_label(stage),
        **fields,
    )
    with _cancel_lock:
        _cancel_flags.pop(request_id, None)
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
    elif out.get("final_status"):
        out["status"] = "failed"
    elif out.get("stage") == "queued":
        out["status"] = "queued"
    else:
        out["status"] = out.get("status") or "processing"
    return out


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
    MAX_CONCURRENT_GEMINI_REQUESTS and the request deadline.
    """
    check_sales_deadline("gemini_request")
    rem = sales_deadline_remaining_seconds()
    if rem is not None and rem < 3.0:
        raise SalesExtractionDeadlineExceeded(
            getattr(_deadline_local, "request_id", "unknown"),
            "gemini_request",
            float(getattr(_deadline_local, "limit_seconds", 0) or 0),
        )
    eff_timeout = sales_gemini_timeout_seconds(int(timeout or 120))
    from services.vertex_gemini_client import generate_content_via_vertex as _raw

    with gemini_call_slot(label):
        return _raw(model=model, payload=payload, timeout=eff_timeout)


@contextmanager
def sales_tesseract_slot(task_label: str = "sales_tesseract"):
    try:
        from app import tesseract_ocr_slot

        with tesseract_ocr_slot(task_label):
            yield
    except Exception:
        yield


class GatedPytesseract:
    def __init__(self, real: Any):
        self._real = real

    def _run_gated(self, label: str, fn: Any, *, cache_key: Optional[str] = None) -> Any:
        check_sales_deadline("ocr")
        if sales_ocr_budget_exhausted():
            raise SalesOcrBudgetExceeded(
                getattr(_deadline_local, "request_id", "unknown"),
                sales_ocr_calls(),
                int(getattr(_deadline_local, "ocr_budget", 0) or 0),
            )
        if cache_key:
            cached = _ocr_cache_get(cache_key)
            if cached is not _OCR_CACHE_MISS:
                started = getattr(_deadline_local, "started_mono", None)
                elapsed = (
                    round(time.monotonic() - float(started), 3) if started else None
                )
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
                    ctx.ocr_cache_hits = int(
                        getattr(_deadline_local, "ocr_cache_hits", 0) or 0
                    )
                return cached

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
            finally:
                elapsed_ms = (time.monotonic() - started) * 1000.0
                if hasattr(_deadline_local, "ocr_duration_ms"):
                    _deadline_local.ocr_duration_ms = float(
                        getattr(_deadline_local, "ocr_duration_ms", 0) or 0
                    ) + elapsed_ms
                    ctx = getattr(_deadline_local, "ctx", None)
                    if isinstance(ctx, SalesExtractionRuntimeContext):
                        ctx.ocr_duration_ms = float(_deadline_local.ocr_duration_ms)
            if cache_key:
                _ocr_cache_put(cache_key, result)
            return result

    def image_to_string(self, *args: Any, **kwargs: Any) -> Any:
        try:
            key = _ocr_cache_key(args, kwargs)
        except Exception:
            key = None
        return self._run_gated(
            "sales_image_to_string",
            lambda: self._real.image_to_string(*args, **kwargs),
            cache_key=key,
        )

    def image_to_data(self, *args: Any, **kwargs: Any) -> Any:
        try:
            key = "data:" + _ocr_cache_key(args, kwargs)
        except Exception:
            key = None
        return self._run_gated(
            "sales_image_to_data",
            lambda: self._real.image_to_data(*args, **kwargs),
            cache_key=key,
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
