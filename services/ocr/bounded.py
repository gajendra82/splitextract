"""Bound one OCR call so a stuck engine returns instead of holding the request."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


class OcrExecutionTimeout(TimeoutError):
    """The OCR call exceeded the configured deadline."""

    def __init__(self, timeout_s: float, label: str = "ocr") -> None:
        self.timeout_s = float(timeout_s)
        self.label = label
        super().__init__(f"{label} timed out after {timeout_s}s")


def ocr_execution_timeout_s() -> float:
    """Seconds allowed for one OCR recognize call.

    ``OCR_EXECUTION_TIMEOUT_S`` wins. ``PADDLE_VL_TIMEOUT_S`` remains the
    previous paddle deadline so existing deployments keep the same bound.
    """
    for name in ("OCR_EXECUTION_TIMEOUT_S", "PADDLE_VL_TIMEOUT_S"):
        raw = os.getenv(name)
        if raw is None or not str(raw).strip():
            continue
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            continue
    return 300.0


def call_bounded(
    fn: Callable[[], T],
    timeout_s: Optional[float] = None,
    *,
    label: str = "ocr",
) -> T:
    """Run ``fn`` and raise ``OcrExecutionTimeout`` when the deadline passes.

    The worker thread is not joined. A native OCR call cannot be cancelled,
    but the caller returns an explicit timeout instead of blocking the request.
    """
    limit = ocr_execution_timeout_s() if timeout_s is None else float(timeout_s)
    if limit <= 0:
        return fn()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr-bound")
    future = pool.submit(fn)
    try:
        return future.result(timeout=limit)
    except FuturesTimeoutError as exc:
        raise OcrExecutionTimeout(limit, label) from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
