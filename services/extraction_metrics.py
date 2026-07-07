"""Processing metrics for hybrid OCR + LLM extraction."""

from threading import Lock
from typing import Dict


class ExtractionMetrics:
    """Thread-safe counters for extraction pipeline observability."""

    def __init__(self):
        self._lock = Lock()
        self._counters: Dict[str, float] = {
            "ocr_success": 0,
            "vision_calls": 0,
            "gpt_calls": 0,
            "gemini_text_calls": 0,
            "fallback_count": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "total_processing_time_ms": 0,
            "total_ocr_score": 0,
            "ocr_score_samples": 0,
        }

    def increment(self, key: str, amount: float = 1.0) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def record_ocr_score(self, score: float) -> None:
        with self._lock:
            self._counters["total_ocr_score"] += score
            self._counters["ocr_score_samples"] += 1

    def record_processing_time(self, ms: float) -> None:
        with self._lock:
            self._counters["total_processing_time_ms"] += ms

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            data = dict(self._counters)
            samples = data.get("ocr_score_samples", 0) or 0
            data["average_ocr_score"] = (
                data["total_ocr_score"] / samples if samples else 0.0
            )
            total_docs = (
                data.get("ocr_success", 0)
                + data.get("vision_calls", 0)
                + data.get("gpt_calls", 0)
            )
            vision_calls = data.get("vision_calls", 0)
            if total_docs > 0:
                data["vision_saved_pct"] = max(
                    0.0, (1.0 - (vision_calls / total_docs)) * 100.0
                )
            else:
                data["vision_saved_pct"] = 0.0
            # Rough estimate: vision ~$0.002, text ~$0.0003 per page
            data["estimated_cost_saved_usd"] = max(
                0.0,
                (data.get("gpt_calls", 0) + data.get("ocr_success", 0)) * 0.0017,
            )
            if samples > 0:
                data["average_processing_time_ms"] = (
                    data["total_processing_time_ms"] / samples
                )
            else:
                data["average_processing_time_ms"] = 0.0
            return data


# Global metrics instance
EXTRACTION_METRICS = ExtractionMetrics()
