"""Configurable OCR timeout and retry policy.

Centralizes env knobs used by the DI page path and orchestrator so cold-start
CPU VL pages are not silently cut at an aggressive nested deadline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from services.ocr.bounded import ocr_execution_timeout_s


@dataclass(frozen=True)
class OcrTimeoutPolicy:
    """Resolved deadlines for one page OCR attempt."""

    per_page_timeout_s: float
    execution_timeout_s: float
    retry_on_timeout: bool
    max_timeout_retries: int
    soft_page_retries: int

    @property
    def effective_page_timeout_s(self) -> float:
        """Outer page deadline (never exceeds execution budget)."""
        return min(self.per_page_timeout_s, self.execution_timeout_s)


def _env_float(name: str, default: float, *, minimum: float = 1.0) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 5) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(minimum, min(maximum, int(raw)))
    except (TypeError, ValueError):
        return default


def ocr_timeout_policy() -> OcrTimeoutPolicy:
    """Load timeout/retry policy from environment.

    Defaults:
    - ``OCR_PER_PAGE_TIMEOUT_S=180`` — headroom for cold/CPU PaddleOCR-VL
    - ``OCR_EXECUTION_TIMEOUT_S`` / ``PADDLE_VL_TIMEOUT_S`` — inner engine bound (300)
    - ``OCR_RETRY_ON_TIMEOUT=false`` — optional single restart after kill
    - ``OCR_TIMEOUT_RETRY_COUNT=1`` — max hard-timeout restarts when enabled
    - ``OCR_PAGE_RETRY_COUNT`` — soft invert/zoom retries (unchanged semantics)
    """
    execution = ocr_execution_timeout_s()
    # 180s default: prior 120s cut off cold CPU VL before inference finished.
    per_page = _env_float("OCR_PER_PAGE_TIMEOUT_S", 180.0, minimum=5.0)
    soft = _env_int("OCR_PAGE_RETRY_COUNT", 1, minimum=0, maximum=3)
    return OcrTimeoutPolicy(
        per_page_timeout_s=per_page,
        execution_timeout_s=execution,
        retry_on_timeout=_env_bool("OCR_RETRY_ON_TIMEOUT", False),
        max_timeout_retries=_env_int("OCR_TIMEOUT_RETRY_COUNT", 1, minimum=0, maximum=3),
        soft_page_retries=soft,
    )


def kill_primary_vl_on_timeout(*, label: str = "ocr") -> bool:
    """Terminate the keep-alive VL serve after a page-level timeout.

    Returns True when the kill helper ran without raising.
    """
    try:
        from services.ocr.paddle_vl_engine import kill_paddle_vl_serve

        kill_paddle_vl_serve()
        return True
    except Exception:
        return False


def describe_policy(policy: Optional[OcrTimeoutPolicy] = None) -> dict:
    p = policy or ocr_timeout_policy()
    return {
        "per_page_timeout_s": p.per_page_timeout_s,
        "execution_timeout_s": p.execution_timeout_s,
        "effective_page_timeout_s": p.effective_page_timeout_s,
        "retry_on_timeout": p.retry_on_timeout,
        "max_timeout_retries": p.max_timeout_retries,
        "soft_page_retries": p.soft_page_retries,
    }
