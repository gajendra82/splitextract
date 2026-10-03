"""Stock-statement OCR execution policy.

One policy for every stock Tesseract call that enters the sales OCR gate:

    INITIAL OCR
        if usable -> continue
        if unusable -> ONE targeted recovery (a reason is required)
        if still unusable -> stop
        Gemini fallback stays the caller's existing decision

Exact same input (image/page/crop/cell + operation + config) that already
finished is reused. A timeout halts further OCR for that request so a later
stage cannot start a hidden retry. Cell-scoped work cannot replay an image
that was already OCR'd as a page.

The request deadline and the Tesseract concurrency slot stay in
sales_extraction_runtime. This module does not start Tesseract and does not
touch the Gemini rate limiter.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Initial execution plus this many recovery executions. Default is one recovery.
_DEFAULT_MAX_RECOVERY = 1

ALLOWED_RECOVERY_REASONS = frozenset(
    {
        "header_detection_failed",
        "numeric_cell_missing",
        "low_confidence_cell",
        "required_column_missing",
        "unusable_ocr",
    }
)

# Validation / column-mapping failures are not OCR-quality retries.
REJECTED_RETRY_REASONS = frozenset(
    {
        "column_mapping",
        "column_mapping_failed",
        "validation_failed",
        "validation_failure",
    }
)

# A cell crop this large, relative to the page, is the page again.
_FULL_PAGE_CELL_RATIO = 0.85

_LOG_FIELDS = (
    "request_id",
    "page",
    "region",
    "cell",
    "operation",
    "attempt",
    "reason",
    "elapsed_ms",
    "status",
    "cache_hit",
    "policy_decision",
)


def stock_ocr_max_recovery_attempts() -> int:
    """Configured recovery limit. Zero disables recovery. Default is one."""
    raw = os.getenv("STOCK_OCR_MAX_RECOVERY_ATTEMPTS", str(_DEFAULT_MAX_RECOVERY)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid STOCK_OCR_MAX_RECOVERY_ATTEMPTS=%r, defaulting to %s",
            raw,
            _DEFAULT_MAX_RECOVERY,
        )
        return _DEFAULT_MAX_RECOVERY
    if value < 0:
        return 0
    return value


def cell_crop_covers_page(cell_area: int, page_area: int) -> bool:
    """True when a supposed cell crop is the page image again."""
    if page_area <= 0 or cell_area <= 0:
        return False
    return cell_area >= int(page_area * _FULL_PAGE_CELL_RATIO)


def empty_ocr_result(operation: str) -> Any:
    if operation == "image_to_data":
        return {
            "text": [],
            "left": [],
            "top": [],
            "width": [],
            "height": [],
            "conf": [],
        }
    return ""


def ocr_result_usable(operation: str, value: Any) -> bool:
    """Usable means the OCR call returned readable characters, not a mapped column."""
    if operation == "image_to_data":
        if not isinstance(value, dict):
            return False
        parts = value.get("text") or []
        text = " ".join(str(part or "") for part in parts)
    else:
        text = "" if value is None else str(value)
    return len(re.findall(r"[A-Za-z0-9]", text)) >= 1


def current_stock_request_id() -> str:
    try:
        from services.sales_extraction_runtime import _deadline_local

        rid = getattr(_deadline_local, "request_id", None)
        return str(rid) if rid else "-"
    except Exception:
        return "-"


def log_stock_ocr(event: str, **fields: Any) -> None:
    payload = {name: fields.get(name, "-") for name in _LOG_FIELDS}
    if payload.get("elapsed_ms") in (None, "", "-"):
        payload["elapsed_ms"] = 0
    else:
        try:
            payload["elapsed_ms"] = int(payload["elapsed_ms"])
        except (TypeError, ValueError):
            payload["elapsed_ms"] = 0
    if payload.get("cache_hit") in (None, "", "-"):
        payload["cache_hit"] = 0
    if payload.get("policy_decision") in (None, "", "-"):
        payload["policy_decision"] = "-"
    if payload.get("operation") in (None, "", "-"):
        payload["operation"] = "-"
    logger.info(
        "%s request_id=%s page=%s region=%s cell=%s operation=%s attempt=%s "
        "reason=%s elapsed_ms=%s status=%s cache_hit=%s policy_decision=%s",
        event,
        payload["request_id"],
        payload["page"],
        payload["region"],
        payload["cell"],
        payload["operation"],
        payload["attempt"],
        payload["reason"],
        payload["elapsed_ms"],
        payload["status"],
        payload["cache_hit"],
        payload["policy_decision"],
    )


def resolve_stock_ocr_region_key(
    *,
    scope: str,
    page: str,
    region: str,
    operation: str,
    pixel_key: str,
) -> str:
    """Stable region identity for page OCR; pixel identity for cells.

    Anonymous page-scoped crops (default region=\"page\") share one bucket so
    another extraction stage cannot mint a fresh attempt=1 via a new preprocess.
    Explicit named regions stay independent. Cells stay pixel-keyed.
    """
    op = (operation or "image_to_string").strip() or "image_to_string"
    page_id = (page or "-").strip() or "-"
    region_id = (region or "page").strip() or "page"
    if (scope or "page") == "cell":
        return pixel_key or f"cell:{page_id}:{region_id}:{op}"
    if region_id != "page":
        return f"named:{page_id}:{region_id}:{op}"
    return f"page:{page_id}:{op}"


@dataclass
class StockOcrMeta:
    page: Optional[str] = None
    region: Optional[str] = None
    cell: Optional[str] = None
    reason: Optional[str] = None
    scope: str = "page"


_meta_var: contextvars.ContextVar[Optional[StockOcrMeta]] = contextvars.ContextVar(
    "stock_ocr_meta",
    default=None,
)


def current_stock_ocr_meta() -> StockOcrMeta:
    meta = _meta_var.get()
    if isinstance(meta, StockOcrMeta):
        return meta
    return StockOcrMeta()


@contextmanager
def stock_ocr_context(
    *,
    page: Optional[str] = None,
    region: Optional[str] = None,
    cell: Optional[str] = None,
    reason: Optional[str] = None,
    scope: Optional[str] = None,
):
    prev = current_stock_ocr_meta()
    merged = StockOcrMeta(
        page=prev.page if page is None else page,
        region=prev.region if region is None else region,
        cell=prev.cell if cell is None else cell,
        reason=prev.reason if reason is None else reason,
        scope=prev.scope if scope is None else scope,
    )
    token = _meta_var.set(merged)
    try:
        yield merged
    finally:
        _meta_var.reset(token)


@dataclass
class StockOcrCall:
    request_id: str
    operation: str
    input_key: str
    region_key: str
    page: str = "-"
    region: str = "page"
    cell: str = "-"
    reason: Optional[str] = None
    scope: str = "page"


@dataclass
class StockOcrDecision:
    action: str
    value: Any = None
    error: Optional[BaseException] = None
    attempt: int = 0
    reason: str = "initial"
    recovery: bool = False


@dataclass
class _InputRecord:
    status: str
    attempt: int
    reason: str
    value: Any = None
    error: Optional[BaseException] = None
    elapsed_ms: int = 0


@dataclass
class _OpState:
    executions: int = 0
    recovery_used: bool = False
    last_status: str = ""
    last_value: Any = None
    last_attempt: int = 0
    last_reason: str = "initial"


class _RequestLedger:
    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.halted = False
        self.halt_error: Optional[BaseException] = None
        self.inputs: Dict[str, _InputRecord] = {}
        self.ops: Dict[str, _OpState] = {}
        self.page_regions: set = set()

    def op(self, region_key: str, operation: str) -> _OpState:
        key = f"{operation}:{region_key}"
        state = self.ops.get(key)
        if state is None:
            state = _OpState()
            self.ops[key] = state
        return state


def _is_ocr_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return type(exc).__name__ in {
        "SalesExtractionDeadlineExceeded",
        "FuturesTimeoutError",
    }


class StockOcrPolicy:
    """Request-scoped stock OCR attempts, reuse, and the one-recovery limit."""

    def __init__(self, max_recovery_attempts: Optional[int] = None):
        if max_recovery_attempts is None:
            self.max_recovery = stock_ocr_max_recovery_attempts()
        else:
            self.max_recovery = max(0, int(max_recovery_attempts))
        self._guard = threading.Lock()
        self._requests: Dict[str, _RequestLedger] = {}
        self._local = threading.local()

    def clear(self, request_id: Optional[str] = None) -> None:
        with self._guard:
            if request_id:
                self._requests.pop(request_id, None)
            else:
                self._requests.clear()
        self._local.ledger = None

    def _ledger(self, request_id: str) -> _RequestLedger:
        # No request id: dedupe on this thread only, so workers do not share
        # OCR results across unrelated calls.
        if not request_id or request_id == "unknown":
            ledger = getattr(self._local, "ledger", None)
            if ledger is None:
                ledger = _RequestLedger()
                self._local.ledger = ledger
            return ledger
        with self._guard:
            ledger = self._requests.get(request_id)
            if ledger is None:
                ledger = _RequestLedger()
                self._requests[request_id] = ledger
            return ledger

    def note_deadline(self, call: StockOcrCall, exc: BaseException) -> None:
        """Request deadline already fired. Do not start Tesseract."""
        ledger = self._ledger(call.request_id)
        with ledger.cv:
            ledger.halted = True
            ledger.halt_error = exc
            ledger.cv.notify_all()
        log_stock_ocr(
            "STOCK_OCR_TIMEOUT",
            request_id=call.request_id,
            page=call.page,
            region=call.region,
            cell=call.cell,
            operation=call.operation,
            attempt=0,
            reason="request_deadline",
            elapsed_ms=0,
            status="timeout",
            cache_hit=0,
            policy_decision="deadline_halt",
        )

    def prepare(self, call: StockOcrCall) -> StockOcrDecision:
        ledger = self._ledger(call.request_id)
        with ledger.cv:
            while True:
                pending = ledger.inputs.get(call.input_key)
                if pending is not None and pending.status == "pending":
                    ledger.cv.wait(timeout=1.0)
                    continue
                return self._decide(ledger, call)

    def finish_success(self, call: StockOcrCall, value: Any, elapsed_ms: float) -> None:
        usable = ocr_result_usable(call.operation, value)
        status = "ok" if usable else "unusable"
        ledger = self._ledger(call.request_id)
        elapsed = int(elapsed_ms)
        with ledger.cv:
            rec = ledger.inputs.get(call.input_key)
            attempt = rec.attempt if rec is not None else 0
            reason = rec.reason if rec is not None else (call.reason or "initial")
            if rec is None:
                rec = _InputRecord(status=status, attempt=attempt, reason=reason)
                ledger.inputs[call.input_key] = rec
            rec.status = status
            rec.value = value
            rec.elapsed_ms = elapsed
            op = ledger.op(call.region_key, call.operation)
            op.last_status = status
            op.last_attempt = attempt
            op.last_reason = reason
            if usable:
                op.last_value = value
            if call.scope != "cell":
                ledger.page_regions.add(call.region_key)
            ledger.cv.notify_all()
        log_stock_ocr(
            "STOCK_OCR_END",
            request_id=call.request_id,
            page=call.page,
            region=call.region,
            cell=call.cell,
            operation=call.operation,
            attempt=attempt,
            reason=reason,
            elapsed_ms=elapsed,
            status=status,
            cache_hit=0,
            policy_decision="executed",
        )

    def finish_failure(
        self, call: StockOcrCall, exc: BaseException, elapsed_ms: float
    ) -> None:
        timeout = _is_ocr_timeout(exc)
        ledger = self._ledger(call.request_id)
        elapsed = int(elapsed_ms)
        with ledger.cv:
            rec = ledger.inputs.get(call.input_key)
            attempt = rec.attempt if rec is not None else 0
            reason = rec.reason if rec is not None else (call.reason or "initial")
            status = "timeout" if timeout else "failed"
            if rec is None:
                rec = _InputRecord(status=status, attempt=attempt, reason=reason)
                ledger.inputs[call.input_key] = rec
            rec.status = status
            rec.error = exc
            rec.elapsed_ms = elapsed
            op = ledger.op(call.region_key, call.operation)
            op.last_status = status
            op.last_attempt = attempt
            op.last_reason = reason
            if timeout:
                ledger.halted = True
                ledger.halt_error = exc
            ledger.cv.notify_all()
        log_stock_ocr(
            "STOCK_OCR_TIMEOUT" if timeout else "STOCK_OCR_END",
            request_id=call.request_id,
            page=call.page,
            region=call.region,
            cell=call.cell,
            operation=call.operation,
            attempt=attempt,
            reason=reason if not timeout else "tesseract_timeout",
            elapsed_ms=elapsed,
            status=status,
            cache_hit=0,
            policy_decision="timeout" if timeout else "failed",
        )

    def execute(self, call: StockOcrCall, run: Callable[[], Any]) -> Any:
        """Run `run` only when the policy allows this attempt."""
        decision = self.prepare(call)
        if decision.action == "reuse":
            return decision.value
        if decision.action == "stop":
            if decision.error is not None:
                raise decision.error
            return decision.value
        started = time.monotonic()
        try:
            value = run()
        except BaseException as exc:
            self.finish_failure(call, exc, (time.monotonic() - started) * 1000.0)
            raise
        self.finish_success(call, value, (time.monotonic() - started) * 1000.0)
        return value

    def _decide(self, ledger: _RequestLedger, call: StockOcrCall) -> StockOcrDecision:
        rec = ledger.inputs.get(call.input_key)
        if rec is not None and rec.status in ("ok", "unusable"):
            log_stock_ocr(
                "STOCK_OCR_REUSE",
                request_id=call.request_id,
                page=call.page,
                region=call.region,
                cell=call.cell,
                operation=call.operation,
                attempt=rec.attempt,
                reason=rec.reason,
                elapsed_ms=rec.elapsed_ms,
                status="reused",
                cache_hit=1,
                policy_decision="exact_input_reuse",
            )
            return StockOcrDecision(
                "reuse",
                value=rec.value,
                attempt=rec.attempt,
                reason=rec.reason,
            )
        if rec is not None and rec.status in ("timeout", "failed"):
            log_stock_ocr(
                "STOCK_OCR_RETRY_STOP",
                request_id=call.request_id,
                page=call.page,
                region=call.region,
                cell=call.cell,
                operation=call.operation,
                attempt=rec.attempt,
                reason="timeout" if rec.status == "timeout" else "ocr_failed",
                elapsed_ms=rec.elapsed_ms,
                status="stopped",
                cache_hit=0,
                policy_decision="prior_input_failed",
            )
            return StockOcrDecision(
                "stop",
                value=empty_ocr_result(call.operation),
                error=rec.error,
                attempt=rec.attempt,
                reason=rec.reason,
            )
        if ledger.halted:
            log_stock_ocr(
                "STOCK_OCR_RETRY_STOP",
                request_id=call.request_id,
                page=call.page,
                region=call.region,
                cell=call.cell,
                operation=call.operation,
                attempt=0,
                reason="timeout",
                elapsed_ms=0,
                status="stopped",
                cache_hit=0,
                policy_decision="request_halted",
            )
            return StockOcrDecision(
                "stop",
                value=empty_ocr_result(call.operation),
                error=ledger.halt_error or TimeoutError("stock OCR halted after timeout"),
                attempt=0,
                reason="timeout",
            )
        if call.scope == "cell" and call.region_key in ledger.page_regions:
            log_stock_ocr(
                "STOCK_OCR_RETRY_STOP",
                request_id=call.request_id,
                page=call.page,
                region=call.region,
                cell=call.cell,
                operation=call.operation,
                attempt=0,
                reason="cell_recovery_not_full_page",
                elapsed_ms=0,
                status="stopped",
                cache_hit=0,
                policy_decision="cell_blocked_after_page",
            )
            return StockOcrDecision(
                "stop",
                value=empty_ocr_result(call.operation),
                attempt=0,
                reason="cell_recovery_not_full_page",
            )

        op = ledger.op(call.region_key, call.operation)
        recovery = False
        reason = call.reason or "initial"
        if op.executions > 0:
            allowed_reason = (
                call.reason in ALLOWED_RECOVERY_REASONS
                and call.reason not in REJECTED_RETRY_REASONS
            )
            limit_reached = op.recovery_used or op.executions >= 1 + self.max_recovery
            # Same region already produced usable text: never mint attempt=1 for a
            # new crop/PSM — reuse that result (production cascade #46/#47/#48).
            if op.last_status == "ok" and not allowed_reason:
                log_stock_ocr(
                    "STOCK_OCR_REUSE",
                    request_id=call.request_id,
                    page=call.page,
                    region=call.region,
                    cell=call.cell,
                    operation=call.operation,
                    attempt=op.last_attempt or op.executions,
                    reason=op.last_reason or "usable_ocr",
                    elapsed_ms=0,
                    status="reused",
                    cache_hit=1,
                    policy_decision="region_shared_reuse",
                )
                return StockOcrDecision(
                    "reuse",
                    value=op.last_value
                    if op.last_value is not None
                    else empty_ocr_result(call.operation),
                    attempt=op.last_attempt or op.executions,
                    reason=op.last_reason or "usable_ocr",
                )
            if op.last_status in ("failed", "timeout") or limit_reached:
                stop_reason = "retry_limit"
                if op.last_status == "timeout":
                    stop_reason = "timeout"
                elif op.last_status == "failed":
                    stop_reason = "ocr_failed"
                return self._stop(call, op.executions + 1, stop_reason)
            if op.last_status == "unusable":
                # OCR quality failed. One recovery, even if the caller did not
                # label it. Column-mapping labels are not used as the reason.
                recovery = True
                reason = call.reason if allowed_reason else "unusable_ocr"
            elif allowed_reason:
                recovery = True
                reason = str(call.reason)
            else:
                refused = (
                    call.reason
                    if call.reason in REJECTED_RETRY_REASONS
                    else (call.reason or "usable_ocr")
                )
                return self._stop(call, op.executions + 1, str(refused))

        attempt = op.executions + 1
        op.executions = attempt
        if recovery:
            op.recovery_used = True
            log_stock_ocr(
                "STOCK_OCR_RETRY",
                request_id=call.request_id,
                page=call.page,
                region=call.region,
                cell=call.cell,
                operation=call.operation,
                attempt=attempt,
                reason=reason,
                elapsed_ms=0,
                status="recovery",
                cache_hit=0,
                policy_decision="recovery_allowed",
            )
        rec = _InputRecord(status="pending", attempt=attempt, reason=reason)
        ledger.inputs[call.input_key] = rec
        log_stock_ocr(
            "STOCK_OCR_START",
            request_id=call.request_id,
            page=call.page,
            region=call.region,
            cell=call.cell,
            operation=call.operation,
            attempt=attempt,
            reason=reason,
            elapsed_ms=0,
            status="started",
            cache_hit=0,
            policy_decision="run_initial" if not recovery else "run_recovery",
        )
        return StockOcrDecision(
            "run",
            attempt=attempt,
            reason=reason,
            recovery=recovery,
        )

    def _stop(self, call: StockOcrCall, attempt: int, reason: str) -> StockOcrDecision:
        log_stock_ocr(
            "STOCK_OCR_RETRY_STOP",
            request_id=call.request_id,
            page=call.page,
            region=call.region,
            cell=call.cell,
            operation=call.operation,
            attempt=attempt,
            reason=reason,
            elapsed_ms=0,
            status="stopped",
            cache_hit=0,
            policy_decision="stop",
        )
        # Cascade stages that would have started another Tesseract call.
        if reason in {
            "retry_limit",
            "usable_ocr",
            "column_mapping",
            "column_mapping_failed",
            "validation_failed",
            "validation_failure",
        }:
            log_stock_ocr(
                "STOCK_OCR_DIRECT_CALL_BLOCKED",
                request_id=call.request_id,
                page=call.page,
                region=call.region,
                cell=call.cell,
                operation=call.operation,
                attempt=attempt,
                reason=reason,
                elapsed_ms=0,
                status="blocked",
                cache_hit=0,
                policy_decision="direct_call_blocked",
            )
        return StockOcrDecision(
            "stop",
            value=empty_ocr_result(call.operation),
            attempt=attempt,
            reason=reason,
        )


_policy: Optional[StockOcrPolicy] = None
_policy_lock = threading.Lock()


def get_stock_ocr_policy() -> StockOcrPolicy:
    global _policy
    with _policy_lock:
        if _policy is None:
            _policy = StockOcrPolicy()
        return _policy


def clear_stock_ocr_request(request_id: Optional[str] = None) -> None:
    get_stock_ocr_policy().clear(request_id)
