"""
Self-healing and observability helpers for split-extract.

All behaviour changes are gated by environment flags (default off).
Does not modify OCR, parsing, or business logic.
"""

from __future__ import annotations

import contextvars
import logging
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import psutil
    _PSUTIL = True
except Exception:
    psutil = None
    _PSUTIL = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (defaults preserve existing production behaviour)
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


OCR_WATCHDOG_ENABLED = _env_bool("OCR_WATCHDOG_ENABLED", False)
OCR_WATCHDOG_INTERVAL_SECONDS = _env_int("OCR_WATCHDOG_INTERVAL_SECONDS", 30)
OCR_WATCHDOG_STUCK_SECONDS = _env_int("OCR_WATCHDOG_STUCK_SECONDS", 300)

OCR_STRUCTURED_LOGGING_ENABLED = _env_bool("OCR_STRUCTURED_LOGGING_ENABLED", False)
OCR_SEMAPHORE_LOGGING_ENABLED = _env_bool("OCR_SEMAPHORE_LOGGING_ENABLED", False)
OCR_THREADPOOL_LOGGING_ENABLED = _env_bool("OCR_THREADPOOL_LOGGING_ENABLED", False)
OCR_TESSERACT_CALL_TIMEOUT_ENABLED = _env_bool("OCR_TESSERACT_CALL_TIMEOUT_ENABLED", False)
TESSERACT_CALL_TIMEOUT_SECONDS = _env_int("TESSERACT_CALL_TIMEOUT_SECONDS", 180)
OCR_PAGE_FUTURE_TIMEOUT_SECONDS = _env_int("OCR_PAGE_FUTURE_TIMEOUT_SECONDS", 120)

# Skip starting new heavy Tesseract when host CPU steal is extreme (configurable).
OCR_CPU_STEAL_GUARD_ENABLED = _env_bool("OCR_CPU_STEAL_GUARD_ENABLED", False)
OCR_CPU_STEAL_THRESHOLD_PERCENT = _env_int("OCR_CPU_STEAL_THRESHOLD_PERCENT", 80)

OCR_MID_EXECUTION_HEARTBEAT_ENABLED = _env_bool("OCR_MID_EXECUTION_HEARTBEAT_ENABLED", False)
OCR_HEARTBEAT_TICK_SECONDS = _env_int("OCR_HEARTBEAT_TICK_SECONDS", 30)
OCR_TESSERACT_LOGGING_ENABLED = _env_bool("OCR_TESSERACT_LOGGING_ENABLED", False)

_PROCESS_START_MONO = time.monotonic()
_WATCHDOG_THREAD: Optional[threading.Thread] = None

# Temp files registered for watchdog best-effort cleanup (paths only, no content change)
_temp_files_lock = threading.Lock()
_temp_files: set = set()

# Optional callback to read tesseract slot counters from app.py
_tesseract_slot_probe_fn: Optional[Callable[[], Dict[str, int]]] = None

_tesseract_call_count = 0
_tesseract_call_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Request context (structured logging)
# ---------------------------------------------------------------------------

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
_request_stage_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_stage", default="")
_request_invoice_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_invoice", default="")
_request_page_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_page", default="")
_request_start_mono_var: contextvars.ContextVar[float] = contextvars.ContextVar(
    "request_start_mono", default=0.0
)


class _RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not OCR_STRUCTURED_LOGGING_ENABLED:
            return True
        try:
            rid = _request_id_var.get("") or "-"
            stage = _request_stage_var.get("") or "-"
            invoice = _request_invoice_var.get("") or "-"
            page = _request_page_var.get("") or "-"
            start = _request_start_mono_var.get(0.0)
            elapsed = round(time.monotonic() - start, 2) if start else 0.0
            thread = threading.current_thread().name
            prefix = (
                f"request_id={rid} invoice={invoice} stage={stage} "
                f"page={page} thread={thread} elapsed={elapsed}s"
            )
            record.msg = f"{prefix} | {record.getMessage()}"
            record.args = ()
        except Exception:
            pass
        return True


def install_structured_logging() -> None:
    if not OCR_STRUCTURED_LOGGING_ENABLED:
        return
    root = logging.getLogger()
    filt = _RequestContextFilter()
    for handler in root.handlers:
        if not any(isinstance(f, _RequestContextFilter) for f in handler.filters):
            handler.addFilter(filt)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.addFilter(filt)
        root.addHandler(handler)
    logger.info("Structured request logging enabled")


def set_request_context(
    request_id: str = "",
    stage: str = "",
    invoice: str = "",
    page: str = "",
    start_mono: Optional[float] = None,
) -> None:
    if request_id:
        _request_id_var.set(request_id)
    if stage:
        _request_stage_var.set(stage)
    if invoice:
        _request_invoice_var.set(invoice)
    if page:
        _request_page_var.set(page)
    if start_mono is not None:
        _request_start_mono_var.set(start_mono)


def get_request_id() -> str:
    return _request_id_var.get("") or ""


# ---------------------------------------------------------------------------
# Enhanced heartbeat / resource state
# ---------------------------------------------------------------------------

_heartbeat_lock = threading.Lock()
_heartbeat_state: Dict[str, Any] = {
    "active": False,
    "request_id": None,
    "invoice": None,
    "stage": None,
    "page": None,
    "total_pages": None,
    "last_completed_step": None,
    "timestamp": None,
    "last_progress_mono": None,
    "active_ocr_threads": 0,
    "active_gpt_threads": 0,
    "active_gemini_threads": 0,
    "request_start_mono": None,
    "ocr_duration_seconds": 0.0,
    "gpt_duration_seconds": 0.0,
    "cache_hits": 0,
    "cache_misses": 0,
}

_ocr_active = 0
_gpt_active = 0
_gemini_active = 0
_gpt_inflight = False
_gemini_inflight = False
_progress_sync_fn: Optional[Callable[..., None]] = None


def register_request_progress_sync(fn: Callable[..., None]) -> None:
    """Register app.py callback to keep _request_progress monotonic in sync."""
    global _progress_sync_fn
    _progress_sync_fn = fn


def _sync_request_progress(
    stage: str,
    page: Optional[int] = None,
    total_pages: Optional[int] = None,
    invoice: Optional[str] = None,
) -> None:
    if _progress_sync_fn:
        try:
            _progress_sync_fn(
                stage=stage, page=page, total_pages=total_pages, invoice=invoice,
            )
        except Exception:
            pass


def release_gpt_inflight_counter() -> None:
    """Decrement GPT counter if a request stage was entered without a response."""
    global _gpt_inflight
    if _gpt_inflight:
        decrement_gpt_active()
        _gpt_inflight = False


def release_gemini_inflight_counter() -> None:
    """Decrement Gemini counter if a request stage was entered without a response."""
    global _gemini_inflight
    if _gemini_inflight:
        decrement_gemini_active()
        _gemini_inflight = False


def reset_active_worker_counters() -> None:
    """Force in-flight worker counters to zero (request completion safety net)."""
    global _ocr_active, _gpt_active, _gemini_active, _gpt_inflight, _gemini_inflight
    with _heartbeat_lock:
        _ocr_active = 0
        _gpt_active = 0
        _gemini_active = 0
        _heartbeat_state["active_ocr_threads"] = 0
        _heartbeat_state["active_gpt_threads"] = 0
        _heartbeat_state["active_gemini_threads"] = 0
    _gpt_inflight = False
    _gemini_inflight = False

# Semaphore observability (injected from app.py at startup)
_request_semaphore_value_fn: Optional[Callable[[], Dict[str, int]]] = None


def register_request_semaphore_probe(fn: Callable[[], Dict[str, int]]) -> None:
    global _request_semaphore_value_fn
    _request_semaphore_value_fn = fn


def register_tesseract_slot_probe(fn: Callable[[], Dict[str, int]]) -> None:
    global _tesseract_slot_probe_fn
    _tesseract_slot_probe_fn = fn


def register_watchdog_temp_file(path: str) -> None:
    if path:
        with _temp_files_lock:
            _temp_files.add(path)


def unregister_watchdog_temp_file(path: str) -> None:
    if path:
        with _temp_files_lock:
            _temp_files.discard(path)


def pulse_heartbeat(stage: str = "ocr_in_progress") -> None:
    """Refresh last_progress_mono without changing business logic (liveness tick)."""
    page_raw = _request_page_var.get("")
    page: Optional[int] = None
    if page_raw and page_raw.isdigit():
        page = int(page_raw)
    elif page_raw and "/" in page_raw:
        try:
            page = int(page_raw.split("/", 1)[0])
        except ValueError:
            page = None
    on_heartbeat_update(stage=stage, page=page)
    _sync_request_progress(stage=stage, page=page)


def increment_ocr_active() -> None:
    global _ocr_active
    with _heartbeat_lock:
        _ocr_active += 1
        _heartbeat_state["active_ocr_threads"] = _ocr_active


def decrement_ocr_active() -> None:
    global _ocr_active
    with _heartbeat_lock:
        _ocr_active = max(0, _ocr_active - 1)
        _heartbeat_state["active_ocr_threads"] = _ocr_active


def increment_gpt_active() -> None:
    global _gpt_active
    with _heartbeat_lock:
        _gpt_active += 1
        _heartbeat_state["active_gpt_threads"] = _gpt_active


def decrement_gpt_active() -> None:
    global _gpt_active
    with _heartbeat_lock:
        _gpt_active = max(0, _gpt_active - 1)
        _heartbeat_state["active_gpt_threads"] = _gpt_active


def increment_gemini_active() -> None:
    global _gemini_active
    with _heartbeat_lock:
        _gemini_active += 1
        _heartbeat_state["active_gemini_threads"] = _gemini_active


def decrement_gemini_active() -> None:
    global _gemini_active
    with _heartbeat_lock:
        _gemini_active = max(0, _gemini_active - 1)
        _heartbeat_state["active_gemini_threads"] = _gemini_active


def on_request_start(
    request_id: str,
    source_filename: str = "",
    stage: str = "request_admitted",
) -> None:
    now_mono = time.monotonic()
    set_request_context(
        request_id=request_id,
        stage=stage,
        start_mono=now_mono,
    )
    with _heartbeat_lock:
        _heartbeat_state.update({
            "active": True,
            "request_id": request_id,
            "invoice": None,
            "stage": stage,
            "page": None,
            "total_pages": None,
            "last_completed_step": stage,
            "timestamp": _iso_now(),
            "last_progress_mono": now_mono,
            "request_start_mono": now_mono,
            "source_filename": source_filename,
            "ocr_duration_seconds": 0.0,
            "gpt_duration_seconds": 0.0,
            "cache_hits": 0,
            "cache_misses": 0,
        })


def on_heartbeat_update(
    stage: str,
    page: Optional[int] = None,
    total_pages: Optional[int] = None,
    invoice: Optional[str] = None,
    last_completed_step: Optional[str] = None,
) -> None:
    now_mono = time.monotonic()
    page_str = f"{page}/{total_pages}" if page is not None and total_pages else (
        str(page) if page is not None else ""
    )
    set_request_context(stage=stage, invoice=invoice or "", page=page_str)
    with _heartbeat_lock:
        if not _heartbeat_state.get("active"):
            return
        _heartbeat_state["stage"] = stage
        _heartbeat_state["last_progress_mono"] = now_mono
        _heartbeat_state["timestamp"] = _iso_now()
        if page is not None:
            _heartbeat_state["page"] = page
        if total_pages is not None:
            _heartbeat_state["total_pages"] = total_pages
        if invoice is not None:
            _heartbeat_state["invoice"] = invoice
        if last_completed_step:
            _heartbeat_state["last_completed_step"] = last_completed_step
        else:
            _heartbeat_state["last_completed_step"] = stage


def on_request_end(summary: Optional[Dict[str, Any]] = None) -> None:
    if summary and OCR_STRUCTURED_LOGGING_ENABLED:
        logger.info(
            "REQUEST COMPLETE | pages=%s ocr_duration=%ss gpt_duration=%ss "
            "cache_hits=%s cache_misses=%s total_time=%ss",
            summary.get("pages_processed"),
            summary.get("ocr_duration_seconds"),
            summary.get("gpt_duration_seconds"),
            summary.get("cache_hits", 0),
            summary.get("cache_misses", 0),
            summary.get("total_execution_seconds"),
        )
    reset_active_worker_counters()
    with _heartbeat_lock:
        _heartbeat_state.update({
            "active": False,
            "request_id": None,
            "invoice": None,
            "stage": None,
            "page": None,
            "total_pages": None,
            "last_completed_step": None,
            "timestamp": None,
            "last_progress_mono": None,
            "request_start_mono": None,
            "source_filename": None,
            "ocr_duration_seconds": 0.0,
            "gpt_duration_seconds": 0.0,
            "cache_hits": 0,
            "cache_misses": 0,
        })
    set_request_context(
        request_id="", stage="", invoice="", page="", start_mono=0.0,
    )


def get_heartbeat_age_seconds() -> Optional[float]:
    with _heartbeat_lock:
        if not _heartbeat_state.get("active"):
            return None
        mono = _heartbeat_state.get("last_progress_mono")
        if mono is None:
            return None
        return round(time.monotonic() - mono, 2)


def get_enhanced_heartbeat_snapshot() -> Dict[str, Any]:
    with _heartbeat_lock:
        snap = dict(_heartbeat_state)
    age = get_heartbeat_age_seconds()
    snap["heartbeat_age"] = age
    return snap


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_resource_snapshot(
    include_open_files: bool = False,
    lightweight: bool = False,
) -> Dict[str, Any]:
    mem_mb = None
    cpu_pct = None
    open_files = None
    thread_count = threading.active_count()
    if _PSUTIL and not lightweight:
        try:
            proc = psutil.Process()
            mem_mb = round(proc.memory_info().rss / (1024 * 1024), 1)
            cpu_pct = proc.cpu_percent(interval=None)
            if include_open_files:
                try:
                    open_files = len(proc.open_files())
                except Exception:
                    open_files = None
        except Exception:
            mem_mb = None
    uptime = round(time.monotonic() - _PROCESS_START_MONO, 1)
    sem_info = _request_semaphore_value_fn() if _request_semaphore_value_fn else {}
    tess_info = _tesseract_slot_probe_fn() if _tesseract_slot_probe_fn else {}
    with _heartbeat_lock:
        ocr_active = _ocr_active
        gpt_active = _gpt_active
        gemini_active = _gemini_active
    with _tesseract_call_lock:
        tess_calls = _tesseract_call_count
    return {
        "memory_usage_mb": mem_mb,
        "cpu_percent": cpu_pct,
        "open_files": open_files,
        "thread_count": thread_count,
        "uptime_seconds": uptime,
        "active_ocr_threads": ocr_active,
        "active_gpt_threads": gpt_active,
        "active_gemini_threads": gemini_active,
        "tesseract_calls_total": tess_calls,
        **sem_info,
        **tess_info,
    }


def get_ready_snapshot(runtime_health: Dict[str, Any]) -> Dict[str, Any]:
    hb = get_enhanced_heartbeat_snapshot()
    resources = get_resource_snapshot(lightweight=True)
    age = hb.get("heartbeat_age")
    active = hb.get("active", False)
    active_ocr = int(resources.get("active_ocr_threads") or 0)
    active_gpt = int(resources.get("active_gpt_threads") or 0)
    active_gemini = int(resources.get("active_gemini_threads") or 0)
    ready = True
    if (
        active
        and age is not None
        and age > OCR_WATCHDOG_STUCK_SECONDS
        and active_ocr == 0
        and active_gpt == 0
        and active_gemini == 0
    ):
        ready = False
    return {
        "ready": ready,
        "active_request": active,
        "request_id": hb.get("request_id"),
        "active_ocr": active_ocr,
        "active_gpt": active_gpt,
        "active_gemini": active_gemini,
        "heartbeat_age": age,
        "current_stage": hb.get("stage") or runtime_health.get("current_stage"),
        "current_page": hb.get("page") or runtime_health.get("current_page"),
        "total_pages": hb.get("total_pages") or runtime_health.get("total_pages"),
        "current_invoice": hb.get("invoice") or runtime_health.get("current_invoice"),
        "last_completed_step": hb.get("last_completed_step"),
        "memory_usage_mb": resources.get("memory_usage_mb"),
        "uptime_seconds": resources.get("uptime_seconds"),
        "thread_count": resources.get("thread_count"),
        "waiting_requests": runtime_health.get("waiting_requests", 0),
        "processing_status": runtime_health.get("processing_status"),
    }


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

def _flush_logs() -> None:
    try:
        for handler in logging.root.handlers:
            try:
                handler.flush()
            except Exception:
                pass
        logging.shutdown()
    except Exception:
        pass


def _cleanup_temp_files() -> None:
    with _temp_files_lock:
        paths = list(_temp_files)
    removed = 0
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
                removed += 1
        except Exception as exc:
            logger.error("WATCHDOG: failed to remove temp file %s: %s", path, exc)
    logger.error("WATCHDOG: temp files removed=%s attempted=%s", removed, len(paths))


def _read_cpu_steal_total() -> Optional[Tuple[int, int]]:
    """Return (steal_jiffies, total_jiffies) from aggregate /proc/stat cpu line."""
    try:
        with open("/proc/stat", encoding="ascii") as fh:
            for line in fh:
                if not line.startswith("cpu "):
                    continue
                parts = line.split()
                if len(parts) < 8:
                    return None
                steal = int(parts[7])
                total = sum(int(x) for x in parts[1:8])
                return steal, total
    except Exception:
        return None
    return None


def get_cpu_steal_percent(sample_interval: float = 0.05) -> Optional[float]:
    """Approximate CPU steal % over a short sample window."""
    first = _read_cpu_steal_total()
    if first is None:
        return None
    steal1, total1 = first
    time.sleep(max(0.01, sample_interval))
    second = _read_cpu_steal_total()
    if second is None:
        return None
    steal2, total2 = second
    dsteal = steal2 - steal1
    dtotal = total2 - total1
    if dtotal <= 0:
        return None
    return round(100.0 * dsteal / dtotal, 1)


def cpu_steal_blocks_heavy_tesseract() -> bool:
    """True when steal guard is on and host steal exceeds threshold."""
    if not OCR_CPU_STEAL_GUARD_ENABLED:
        return False
    steal = get_cpu_steal_percent()
    if steal is None:
        return False
    if steal >= OCR_CPU_STEAL_THRESHOLD_PERCENT:
        logger.warning(
            "POD OCR routing: cpu_steal=%s%% threshold=%s%% "
            "strategy=skip_tesseract reason=cpu_steal_guard",
            steal, OCR_CPU_STEAL_THRESHOLD_PERCENT,
        )
        return True
    return False


def log_pod_ocr_routing(
    *,
    pdf_type: str = "",
    pages: Optional[int] = None,
    strategy: str = "",
    reason: str = "",
    stockist_fallback: str = "",
    result: str = "",
    fallback: str = "",
) -> None:
    """Structured routing decision log (no sensitive payload data)."""
    parts = ["POD OCR routing:"]
    if pdf_type:
        parts.append(f"type={pdf_type}")
    if pages is not None:
        parts.append(f"pages={pages}")
    if stockist_fallback:
        parts.append(f"stockist_fallback={stockist_fallback}")
    if strategy:
        parts.append(f"strategy={strategy}")
    if reason:
        parts.append(f"reason={reason}")
    if result:
        parts.append(f"result={result}")
    if fallback:
        parts.append(f"fallback={fallback}")
    logger.info(" ".join(parts))


def terminate_tesseract_children(context: str = "") -> int:
    """Best-effort terminate orphaned Tesseract child processes."""
    if not _PSUTIL:
        return 0
    terminated = 0
    try:
        proc = psutil.Process()
        tess_children = []
        for child in proc.children(recursive=True):
            try:
                name = (child.name() or "").lower()
                cmdline = " ".join(child.cmdline() or []).lower()
                if "tesseract" in name or "tesseract" in cmdline:
                    tess_children.append(child)
            except Exception:
                continue
        for child in tess_children:
            try:
                child.terminate()
                terminated += 1
            except Exception:
                pass
        if tess_children:
            _, alive = psutil.wait_procs(tess_children, timeout=2)
            for child in alive:
                try:
                    child.kill()
                except Exception:
                    pass
    except Exception as exc:
        label = context or "tesseract_cleanup"
        logger.error("%s: tesseract child termination error: %s", label, exc)
    if terminated:
        logger.warning(
            "POD OCR routing: strategy=tesseract result=timeout "
            "fallback=gemini tesseract_children_terminated=%s context=%s",
            terminated, context or "ocr",
        )
    return terminated


def _terminate_tesseract_children() -> None:
    terminated = terminate_tesseract_children(context="watchdog")
    logger.error("WATCHDOG: tesseract children terminated=%s", terminated)


def _dump_diagnostics(reason: str = "heartbeat_stale") -> None:
    hb = get_enhanced_heartbeat_snapshot()
    resources = get_resource_snapshot(include_open_files=True)
    age = hb.get("heartbeat_age")
    logger.error("=" * 60)
    logger.error("=== WATCHDOG REPORT ===")
    logger.error("Reason: %s", reason)
    logger.error("Request ID: %s", hb.get("request_id"))
    logger.error("Invoice: %s", hb.get("invoice"))
    logger.error("Current stage: %s", hb.get("stage"))
    logger.error("Current page: %s / %s", hb.get("page"), hb.get("total_pages"))
    logger.error("Last completed step: %s", hb.get("last_completed_step"))
    logger.error("Heartbeat age: %ss (threshold=%ss)", age, OCR_WATCHDOG_STUCK_SECONDS)
    logger.error("OCR workers active: %s", resources.get("active_ocr_threads", 0))
    logger.error("GPT workers active: %s", resources.get("active_gpt_threads", 0))
    logger.error("Gemini workers active: %s", resources.get("active_gemini_threads", 0))
    logger.error("Thread count: %s", resources.get("thread_count"))
    logger.error(
        "Semaphore: active_requests=%s waiting_requests=%s",
        resources.get("semaphore_active_requests"),
        resources.get("semaphore_waiting_requests"),
    )
    logger.error(
        "Tesseract slot: active=%s waiting=%s",
        resources.get("tesseract_slot_active"),
        resources.get("tesseract_slot_waiting"),
    )
    logger.error("Memory MB: %s", resources.get("memory_usage_mb"))
    logger.error("CPU %%: %s", resources.get("cpu_percent"))
    logger.error("Open files: %s", resources.get("open_files"))
    logger.error("Uptime seconds: %s", resources.get("uptime_seconds"))
    logger.error("Source filename: %s", hb.get("source_filename"))
    logger.error("Full heartbeat: %s", hb)
    logger.error("Full resources: %s", resources)
    logger.error("Active threads (%s):", threading.active_count())
    for t in threading.enumerate():
        logger.error(
            "  thread name=%s ident=%s daemon=%s alive=%s",
            t.name, t.ident, t.daemon, t.is_alive(),
        )
    logger.error("=" * 60)


def _watchdog_graceful_shutdown(reason: str) -> None:
    """Best-effort cleanup before hard exit. Does not block indefinitely."""
    try:
        on_heartbeat_update(
            "watchdog_terminating",
            last_completed_step=reason,
        )
    except Exception:
        pass
    _dump_diagnostics(reason)
    _cleanup_temp_files()
    _terminate_tesseract_children()
    _flush_logs()
    os._exit(1)


def _watchdog_should_terminate(
    active: bool,
    last_mono: Optional[float],
    resources: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, float]:
    """
    Returns (should_terminate, heartbeat_age_seconds).
    Terminates only when heartbeat is stale AND no OCR/GPT/Gemini workers are active.
    """
    if not active or last_mono is None:
        return False, 0.0
    age = time.monotonic() - last_mono
    if age < OCR_WATCHDOG_STUCK_SECONDS:
        return False, age
    resources = resources or get_resource_snapshot()
    active_ocr = int(resources.get("active_ocr_threads") or 0)
    active_gpt = int(resources.get("active_gpt_threads") or 0)
    active_gemini = int(resources.get("active_gemini_threads") or 0)
    if active_ocr > 0 or active_gpt > 0 or active_gemini > 0:
        return False, age
    return True, age


def _watchdog_loop() -> None:
    while True:
        time.sleep(max(5, OCR_WATCHDOG_INTERVAL_SECONDS))
        if not OCR_WATCHDOG_ENABLED:
            continue
        with _heartbeat_lock:
            active = _heartbeat_state.get("active", False)
            last_mono = _heartbeat_state.get("last_progress_mono")
            stage = _heartbeat_state.get("stage")
            request_id = _heartbeat_state.get("request_id")
        should_exit, age = _watchdog_should_terminate(active, last_mono)
        if not should_exit:
            if active and last_mono is not None and age >= OCR_WATCHDOG_STUCK_SECONDS:
                resources = get_resource_snapshot()
                logger.warning(
                    "WATCHDOG: heartbeat stale %.1fs but workers active "
                    "(ocr=%s gpt=%s gemini=%s request_id=%s stage=%s) — skipping exit",
                    age,
                    resources.get("active_ocr_threads", 0),
                    resources.get("active_gpt_threads", 0),
                    resources.get("active_gemini_threads", 0),
                    request_id,
                    stage,
                )
            continue

        logger.error(
            "WATCHDOG: no heartbeat progress for %.1fs (threshold=%ss) "
            "and no active workers (request_id=%s stage=%s)",
            age, OCR_WATCHDOG_STUCK_SECONDS, request_id, stage,
        )
        _watchdog_graceful_shutdown(
            reason=f"heartbeat_stale_{int(age)}s_no_active_workers",
        )


def start_watchdog() -> None:
    global _WATCHDOG_THREAD
    if not OCR_WATCHDOG_ENABLED:
        logger.info("OCR watchdog disabled (OCR_WATCHDOG_ENABLED=false)")
        return
    if _WATCHDOG_THREAD and _WATCHDOG_THREAD.is_alive():
        return
    _WATCHDOG_THREAD = threading.Thread(
        target=_watchdog_loop, name="ocr-watchdog", daemon=True)
    _WATCHDOG_THREAD.start()
    logger.info(
        "OCR watchdog started (interval=%ss stuck_threshold=%ss)",
        OCR_WATCHDOG_INTERVAL_SECONDS, OCR_WATCHDOG_STUCK_SECONDS,
    )


# ---------------------------------------------------------------------------
# Semaphore logging helpers
# ---------------------------------------------------------------------------

def log_semaphore_acquire(name: str, active: int, waiting: int) -> None:
    if OCR_SEMAPHORE_LOGGING_ENABLED:
        logger.info(
            "SEMAPHORE acquire %s | active=%s waiting=%s", name, active, waiting)


def log_semaphore_release(name: str, active: int, waiting: int) -> None:
    if OCR_SEMAPHORE_LOGGING_ENABLED:
        logger.info(
            "SEMAPHORE release %s | active=%s waiting=%s", name, active, waiting)


# ---------------------------------------------------------------------------
# ThreadPool future collection
# ---------------------------------------------------------------------------

def collect_page_futures(
    futures: List[Tuple[int, Any]],
    timeout_seconds: Optional[int] = None,
    request_id: str = "",
) -> List[Any]:
    """Collect ThreadPoolExecutor futures with optional logging."""
    timeout = timeout_seconds if timeout_seconds is not None else OCR_PAGE_FUTURE_TIMEOUT_SECONDS
    results: List[Any] = []
    for i, future in futures:
        started = time.monotonic()
        if OCR_THREADPOOL_LOGGING_ENABLED:
            logger.info(
                "THREADPOOL page=%s start request_id=%s timeout=%ss",
                i + 1, request_id or get_request_id(), timeout,
            )
        try:
            result = future.result(timeout=timeout)
            duration = round(time.monotonic() - started, 2)
            if OCR_THREADPOOL_LOGGING_ENABLED:
                logger.info(
                    "THREADPOOL page=%s finish duration=%ss request_id=%s",
                    i + 1, duration, request_id or get_request_id(),
                )
            results.append(result)
        except FuturesTimeoutError:
            logger.error(
                "THREADPOOL page=%s TIMEOUT after %ss request_id=%s",
                i + 1, timeout, request_id or get_request_id(),
            )
            results.append(None)
        except Exception as exc:
            duration = round(time.monotonic() - started, 2)
            logger.error(
                "THREADPOOL page=%s FAILED duration=%ss error=%s\n%s",
                i + 1, duration, exc, traceback.format_exc(),
            )
            results.append(None)
    return results


# ---------------------------------------------------------------------------
# Unified Tesseract call wrapper
# ---------------------------------------------------------------------------

def _parse_page_arg(page: Optional[int]) -> Optional[int]:
    if page is not None:
        return page
    page_raw = _request_page_var.get("")
    if not page_raw:
        return None
    if page_raw.isdigit():
        return int(page_raw)
    if "/" in page_raw:
        try:
            return int(page_raw.split("/", 1)[0])
        except ValueError:
            return None
    return None


def _tesseract_features_enabled() -> bool:
    return (
        OCR_MID_EXECUTION_HEARTBEAT_ENABLED
        or OCR_TESSERACT_CALL_TIMEOUT_ENABLED
        or OCR_TESSERACT_LOGGING_ENABLED
    )


def run_tesseract_call(
    fn: Callable,
    label: str = "tesseract",
    page: Optional[int] = None,
    total_pages: Optional[int] = None,
):
    """
    Single entry point for all pytesseract invocations.

    When all feature flags are disabled, calls fn() directly (zero overhead).
    When enabled: optional reliability timeout (see OCR_TESSERACT_CALL_TIMEOUT_ENABLED),
    logging, duration metrics, and liveness ticks.

    WARNING: OCR_TESSERACT_CALL_TIMEOUT_ENABLED changes extraction behaviour when
    enabled — timed-out pages may fall back to Gemini Vision or fail.
    """
    if not _tesseract_features_enabled():
        return fn()

    global _tesseract_call_count
    page_num = _parse_page_arg(page)
    started = time.monotonic()
    stop_ticker = threading.Event()
    ticker_thread: Optional[threading.Thread] = None

    with _tesseract_call_lock:
        _tesseract_call_count += 1
        call_num = _tesseract_call_count

    timeout = TESSERACT_CALL_TIMEOUT_SECONDS if OCR_TESSERACT_CALL_TIMEOUT_ENABLED else None

    if OCR_TESSERACT_LOGGING_ENABLED or OCR_TESSERACT_CALL_TIMEOUT_ENABLED:
        logger.info(
            "TESSERACT start label=%s page=%s call=%s timeout=%s",
            label, page_num, call_num, timeout,
        )

    if OCR_MID_EXECUTION_HEARTBEAT_ENABLED:
        on_heartbeat_update(
            "ocr_tesseract_started",
            page=page_num,
            total_pages=total_pages,
        )
        _sync_request_progress(
            stage="ocr_tesseract_started", page=page_num, total_pages=total_pages,
        )

        def _heartbeat_ticker() -> None:
            while not stop_ticker.wait(OCR_HEARTBEAT_TICK_SECONDS):
                pulse_heartbeat("ocr_in_progress")

        ticker_thread = threading.Thread(
            target=_heartbeat_ticker,
            name=f"tess-heartbeat-{label}",
            daemon=True,
        )
        ticker_thread.start()

    try:
        if OCR_TESSERACT_CALL_TIMEOUT_ENABLED:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="tess-timeout") as pool:
                fut = pool.submit(fn)
                result = fut.result(timeout=timeout)
        else:
            result = fn()

        duration = round(time.monotonic() - started, 2)
        if OCR_TESSERACT_LOGGING_ENABLED or OCR_TESSERACT_CALL_TIMEOUT_ENABLED:
            logger.info(
                "TESSERACT finish label=%s page=%s duration=%ss call=%s",
                label, page_num, duration, call_num,
            )
        if OCR_MID_EXECUTION_HEARTBEAT_ENABLED:
            on_heartbeat_update(
                "ocr_tesseract_completed",
                page=page_num,
                total_pages=total_pages,
            )
            _sync_request_progress(
                stage="ocr_tesseract_completed", page=page_num, total_pages=total_pages,
            )
        return result

    except FuturesTimeoutError as exc:
        duration = round(time.monotonic() - started, 2)
        logger.error(
            "TESSERACT TIMEOUT label=%s page=%s duration=%ss after=%ss call=%s",
            label, page_num, duration, timeout, call_num,
        )
        terminate_tesseract_children(context=f"tesseract_timeout:{label}")
        log_pod_ocr_routing(
            strategy="tesseract",
            result="timeout",
            fallback="gemini",
            reason=f"label={label}",
        )
        raise TimeoutError(f"Tesseract call timed out after {timeout}s") from exc
    except Exception as exc:
        duration = round(time.monotonic() - started, 2)
        logger.error(
            "TESSERACT FAILED label=%s page=%s duration=%ss error=%s\n%s",
            label, page_num, duration, exc, traceback.format_exc(),
        )
        raise
    finally:
        stop_ticker.set()
        if ticker_thread and ticker_thread.is_alive():
            ticker_thread.join(timeout=1.0)


def track_llm_stage(stage: str) -> None:
    """Increment/decrement GPT/Gemini active counters (paired transitions only)."""
    global _gpt_inflight, _gemini_inflight
    s = (stage or "").lower()
    if s == "openai_request" and not _gpt_inflight:
        increment_gpt_active()
        _gpt_inflight = True
    elif s == "openai_response_received" and _gpt_inflight:
        decrement_gpt_active()
        _gpt_inflight = False
    elif "gemini" in s and s.endswith("_request") and not _gemini_inflight:
        increment_gemini_active()
        _gemini_inflight = True
    elif "gemini" in s and s.endswith("_response_received") and _gemini_inflight:
        decrement_gemini_active()
        _gemini_inflight = False
