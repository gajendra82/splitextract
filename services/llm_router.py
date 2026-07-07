"""LLM routing between text and vision extraction providers."""

import logging
import time
from typing import Callable, Dict, Optional, Tuple

from services.config import EXTRACTION_CONFIG
from services.extraction_cache import ExtractionCache, build_cache_key
from services.extraction_logger import log_extraction_event
from services.extraction_metrics import EXTRACTION_METRICS
from services.gpt_text_extractor import GPTTextExtractor
from services.gemini_vision_extractor import GeminiVisionExtractor
from services.ocr_quality import analyze_ocr_quality

logger = logging.getLogger(__name__)


class LLMRouter:
    """
    Choose processing strategy based on OCR quality.

    GOOD OCR  -> text model (GPT by default)
    POOR OCR  -> vision model (Gemini by default)
    GPT fail  -> retry once -> vision fallback
    """

    def __init__(
        self,
        config=EXTRACTION_CONFIG,
        gpt_extractor: Optional[GPTTextExtractor] = None,
        vision_extractor: Optional[GeminiVisionExtractor] = None,
        gemini_text_fallback: Optional[Callable] = None,
        cache: Optional[ExtractionCache] = None,
    ):
        self.config = config
        self.gpt_extractor = gpt_extractor or GPTTextExtractor(config=config)
        self.vision_extractor = vision_extractor
        self.gemini_text_fallback = gemini_text_fallback
        self.cache = cache or ExtractionCache(config)

    def extract_from_text(
        self,
        ocr_text: str,
        *,
        ocr_confidence: float = 95.0,
        document_id: str = "",
        document_type: str = "invoice",
        file_hash: str = "",
        page_num: int = 0,
        ocr_stats=None,
        ocr_stats_lock=None,
    ) -> Tuple[Optional[dict], Dict]:
        """
        Route OCR text to the appropriate text model with retry and metrics.

        Returns:
            (parsed_json_or_none, routing_metadata)
        """
        started = time.time()
        quality = analyze_ocr_quality(ocr_text, confidence=ocr_confidence)
        routing_meta = {
            "ocr_quality": quality,
            "selected_strategy": "none",
            "text_model_used": "",
            "vision_model_used": "",
            "fallback_reason": "",
            "retries": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_hit": False,
            "cache_miss": False,
        }

        cache_key = ""
        if file_hash:
            cache_key = build_cache_key(file_hash, page_num, suffix="text")
            cached = self.cache.get(cache_key)
            if cached:
                routing_meta["cache_hit"] = True
                routing_meta["selected_strategy"] = cached.get("strategy", "cache")
                EXTRACTION_METRICS.increment("cache_hits")
                self._log(document_id, document_type, quality, routing_meta, started)
                return cached.get("full_data"), routing_meta

        routing_meta["cache_miss"] = True
        EXTRACTION_METRICS.increment("cache_misses")

        if quality["quality"] != "GOOD":
            routing_meta["fallback_reason"] = quality.get("reason", "poor OCR quality")
            self._log(document_id, document_type, quality, routing_meta, started)
            return None, routing_meta

        if not self.config.enable_text_model:
            if self.gemini_text_fallback and ocr_stats is not None and ocr_stats_lock:
                routing_meta["selected_strategy"] = "gemini_text_legacy"
                routing_meta["text_model_used"] = self.config.vision_model
                parsed = self.gemini_text_fallback(ocr_text, ocr_stats, ocr_stats_lock)
                self._log(document_id, document_type, quality, routing_meta, started)
                return parsed, routing_meta
            return None, routing_meta

        parsed, token_usage = self._extract_text_with_retry(ocr_text, routing_meta)
        routing_meta["input_tokens"] = token_usage.get("input_tokens", 0)
        routing_meta["output_tokens"] = token_usage.get("output_tokens", 0)

        if parsed and cache_key:
            self.cache.set(
                cache_key,
                {"full_data": parsed, "strategy": routing_meta["selected_strategy"]},
            )

        if parsed:
            EXTRACTION_METRICS.increment("gpt_calls")
            EXTRACTION_METRICS.increment("ocr_success")
            EXTRACTION_METRICS.record_ocr_score(quality.get("score", 0))

        self._log(document_id, document_type, quality, routing_meta, started)
        return parsed, routing_meta

    def extract_from_vision(
        self,
        image_bytes: bytes,
        *,
        document_id: str = "",
        document_type: str = "invoice",
        file_hash: str = "",
        page_num: int = 0,
        fallback_reason: str = "",
        ocr_quality: Optional[Dict] = None,
    ) -> Tuple[Optional[dict], Dict]:
        """Route to vision extractor with optional cache."""
        started = time.time()
        routing_meta = {
            "ocr_quality": ocr_quality or {},
            "selected_strategy": "vision",
            "text_model_used": "",
            "vision_model_used": self.config.vision_model,
            "fallback_reason": fallback_reason,
            "retries": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_hit": False,
            "cache_miss": False,
        }

        if not self.config.enable_vision_fallback or not self.vision_extractor:
            self._log(document_id, document_type, ocr_quality or {}, routing_meta, started)
            return None, routing_meta

        cache_key = ""
        if file_hash:
            cache_key = build_cache_key(file_hash, page_num, suffix="vision")
            cached = self.cache.get(cache_key)
            if cached:
                routing_meta["cache_hit"] = True
                routing_meta["selected_strategy"] = "vision_cache"
                EXTRACTION_METRICS.increment("cache_hits")
                self._log(document_id, document_type, ocr_quality or {}, routing_meta, started)
                return cached, routing_meta

        routing_meta["cache_miss"] = True
        EXTRACTION_METRICS.increment("cache_misses")
        EXTRACTION_METRICS.increment("vision_calls")
        if fallback_reason:
            EXTRACTION_METRICS.increment("fallback_count")

        result, token_usage = self.vision_extractor.extract(image_bytes)
        routing_meta["input_tokens"] = token_usage.get("input_tokens", 0)
        routing_meta["output_tokens"] = token_usage.get("output_tokens", 0)

        if result and cache_key:
            self.cache.set(cache_key, result)

        self._log(document_id, document_type, ocr_quality or {}, routing_meta, started)
        return result, routing_meta

    def _extract_text_with_retry(self, ocr_text: str, routing_meta: Dict):
        max_attempts = 1 + max(self.config.text_retry_count, 0)
        last_usage = {"input_tokens": 0, "output_tokens": 0}

        for attempt in range(max_attempts):
            parsed, token_usage = self.gpt_extractor.extract(ocr_text)
            last_usage = token_usage
            if parsed:
                routing_meta["selected_strategy"] = "gpt_text"
                routing_meta["text_model_used"] = self.config.text_model
                routing_meta["retries"] = attempt
                return parsed, last_usage
            routing_meta["retries"] = attempt + 1

        routing_meta["fallback_reason"] = (
            routing_meta.get("fallback_reason")
            or f"GPT text extraction failed after {max_attempts} attempt(s)"
        )
        return None, last_usage

    def _log(self, document_id, document_type, quality, routing_meta, started):
        elapsed_ms = (time.time() - started) * 1000
        EXTRACTION_METRICS.record_processing_time(elapsed_ms)
        log_extraction_event(
            document_id=document_id,
            document_type=document_type,
            ocr_confidence=quality.get("confidence", 0.0),
            ocr_quality=quality.get("quality", ""),
            text_length=quality.get("text_length", 0),
            readable_ratio=quality.get("readable_ratio", 0.0),
            selected_strategy=routing_meta.get("selected_strategy", ""),
            text_model_used=routing_meta.get("text_model_used", ""),
            vision_model_used=routing_meta.get("vision_model_used", ""),
            fallback_reason=routing_meta.get("fallback_reason", ""),
            processing_time_ms=elapsed_ms,
            input_tokens=routing_meta.get("input_tokens", 0),
            output_tokens=routing_meta.get("output_tokens", 0),
            retries=routing_meta.get("retries", 0),
            cache_hit=routing_meta.get("cache_hit", False),
            cache_miss=routing_meta.get("cache_miss", False),
        )
