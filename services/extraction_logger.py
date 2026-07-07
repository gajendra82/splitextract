"""Structured logging for document extraction pipeline."""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("extraction.pipeline")


def log_extraction_event(
    *,
    document_id: str,
    document_type: str,
    ocr_confidence: float = 0.0,
    ocr_quality: str = "",
    text_length: int = 0,
    readable_ratio: float = 0.0,
    selected_strategy: str = "",
    text_model_used: str = "",
    vision_model_used: str = "",
    fallback_reason: str = "",
    processing_time_ms: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    retries: int = 0,
    cache_hit: bool = False,
    cache_miss: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Emit a structured log entry and return the payload."""
    payload = {
        "document_id": document_id,
        "document_type": document_type,
        "ocr_confidence": ocr_confidence,
        "ocr_quality": ocr_quality,
        "text_length": text_length,
        "readable_ratio": readable_ratio,
        "selected_strategy": selected_strategy,
        "text_model_used": text_model_used,
        "vision_model_used": vision_model_used,
        "fallback_reason": fallback_reason,
        "processing_time_ms": round(processing_time_ms, 2),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "retries": retries,
        "cache_hit": cache_hit,
        "cache_miss": cache_miss,
    }
    if extra:
        payload.update(extra)
    logger.info("extraction_event %s", payload)
    return payload
