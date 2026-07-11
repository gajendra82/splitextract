"""GPT token/cost metrics, estimation, and aggregation (logging only)."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (~4 chars per token for mixed OCR text)."""
    if not text:
        return 0
    return max(1, int(len(text) / 4))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def estimate_gpt_cost_usd(
    input_tokens: int,
    output_tokens: int,
    model: str = "",
) -> float:
    """Estimate USD cost using configurable per-1M-token rates."""
    input_rate = _env_float("GPT_INPUT_COST_PER_1M_USD", 0.15)
    output_rate = _env_float("GPT_OUTPUT_COST_PER_1M_USD", 0.60)
    _ = model  # reserved for future per-model pricing tables
    return (input_tokens / 1_000_000.0) * input_rate + (
        output_tokens / 1_000_000.0
    ) * output_rate


@dataclass
class GPTRequestMetrics:
    invoice_no: str = ""
    ocr_chars: int = 0
    ocr_tokens_est: int = 0
    prompt_chars: int = 0
    prompt_tokens_est: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    response_time_ms: float = 0.0
    cache_hit: bool = False
    cache_miss: bool = False
    model: str = ""


@dataclass
class GPTMetricsAggregator:
    """Thread-safe in-process aggregator for analysis reporting."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _records: List[GPTRequestMetrics] = field(default_factory=list, repr=False)

    def record(self, metrics: GPTRequestMetrics) -> None:
        with self._lock:
            self._records.append(metrics)

    def record_count(self) -> int:
        with self._lock:
            return len(self._records)

    def snapshot_since(self, offset: int = 0) -> Dict[str, Any]:
        with self._lock:
            records = list(self._records[offset:])

        if not records:
            return {
                "request_count": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "total_gpt_response_ms": 0.0,
            }

        api_calls = [r for r in records if not r.cache_hit]
        return {
            "request_count": len(records),
            "cache_hits": sum(1 for r in records if r.cache_hit),
            "cache_misses": sum(1 for r in records if r.cache_miss),
            "total_gpt_response_ms": round(
                sum(r.response_time_ms for r in api_calls), 2
            ),
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            records = list(self._records)

        if not records:
            return {
                "request_count": 0,
                "average_ocr_chars": 0,
                "average_prompt_chars": 0,
                "average_total_tokens": 0,
                "average_estimated_cost_usd": 0,
                "cache_hits": 0,
                "cache_misses": 0,
            }

        api_calls = [r for r in records if not r.cache_hit]
        return {
            "request_count": len(records),
            "api_call_count": len(api_calls),
            "average_ocr_chars": round(
                sum(r.ocr_chars for r in records) / len(records), 1
            ),
            "average_prompt_chars": round(
                sum(r.prompt_chars for r in api_calls) / max(len(api_calls), 1), 1
            ),
            "average_total_tokens": round(
                sum(r.total_tokens for r in api_calls) / max(len(api_calls), 1), 1
            ),
            "average_estimated_cost_usd": round(
                sum(r.estimated_cost_usd for r in api_calls) / max(len(api_calls), 1), 6
            ),
            "total_estimated_cost_usd": round(
                sum(r.estimated_cost_usd for r in api_calls), 4
            ),
            "cache_hits": sum(1 for r in records if r.cache_hit),
            "cache_misses": sum(1 for r in records if r.cache_miss),
        }


GPT_METRICS = GPTMetricsAggregator()


def log_gpt_request_block(metrics: GPTRequestMetrics) -> None:
    """Emit the standardized GPT metrics log block."""
    invoice = metrics.invoice_no or "UNKNOWN"
    cache_status = "Cache Hit" if metrics.cache_hit else (
        "Cache Miss" if metrics.cache_miss else "N/A"
    )
    logger.info(
        "\n"
        "================================================\n"
        f"Invoice: {invoice}\n"
        f"OCR Size: {metrics.ocr_chars} chars (~{metrics.ocr_tokens_est} tokens est.)\n"
        f"Prompt Size: {metrics.prompt_chars} chars (~{metrics.prompt_tokens_est} tokens est.)\n"
        f"Prompt Tokens: {metrics.input_tokens}\n"
        f"Input Tokens: {metrics.input_tokens}\n"
        f"Output Tokens: {metrics.output_tokens}\n"
        f"Total Tokens: {metrics.total_tokens}\n"
        f"Estimated Cost: ${metrics.estimated_cost_usd:.6f}\n"
        f"Response Time: {metrics.response_time_ms:.0f} ms\n"
        f"{cache_status}\n"
        "================================================"
    )


def log_pre_gpt_sizes(
    *,
    invoice_no: str,
    ocr_text: str,
    prompt: str,
    enabled: bool,
) -> None:
    if not enabled:
        return
    logger.info(
        "GPT pre-request sizes | invoice=%s | ocr_chars=%s (~%s tokens est.) | "
        "prompt_chars=%s (~%s tokens est.)",
        invoice_no or "UNKNOWN",
        len(ocr_text or ""),
        estimate_tokens(ocr_text or ""),
        len(prompt or ""),
        estimate_tokens(prompt or ""),
    )
