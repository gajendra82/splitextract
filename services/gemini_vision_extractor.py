"""Gemini Vision-only invoice extractor."""

import base64
import json
import logging
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class GeminiVisionExtractor:
    """
    Extract structured invoice JSON from images using Gemini Vision.
    Accepts image bytes only — no OCR text routing or business logic.
    """

    def __init__(
        self,
        config,
        call_gemini_fn: Callable,
        get_model_config_fn: Callable,
        vision_url_template: str,
        vision_prompt: str,
        increment_stat_fn: Optional[Callable] = None,
        ocr_stats=None,
        ocr_stats_lock=None,
    ):
        self.config = config
        self._call_gemini = call_gemini_fn
        self._get_model_config = get_model_config_fn
        self._vision_url_template = vision_url_template
        self._vision_prompt = vision_prompt
        self._increment_stat = increment_stat_fn
        self._ocr_stats = ocr_stats
        self._ocr_stats_lock = ocr_stats_lock

    def extract(self, image_bytes: bytes) -> Tuple[Optional[dict], Dict[str, int]]:
        """
        Run Gemini Vision on image bytes.

        Returns:
            (result_dict, token_usage) where result_dict matches legacy shape:
            {"invoice_no", "full_data", "extraction_method", "ocr_text"}
        """
        if self._increment_stat and self._ocr_stats is not None and self._ocr_stats_lock:
            self._increment_stat(
                self._ocr_stats, self._ocr_stats_lock, "total_gemini_calls", 1
            )
            self._increment_stat(
                self._ocr_stats, self._ocr_stats_lock, "gemini_vision_calls", 1
            )

        model_config = self._get_model_config()
        model_name = self.config.vision_model or model_config["name"]
        api_key = self.config.gemini_api_key
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        url = self._vision_url_template.format(model=model_name, key=api_key)
        payload = {
            "contents": [
                {
                    "parts": [
                        {"inline_data": {"mime_type": "image/png", "data": encoded}},
                        {"text": self._vision_prompt},
                    ]
                }
            ],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 8192},
        }

        try:
            response = self._call_gemini(
                url=url,
                payload=payload,
                timeout=self.config.vision_timeout or model_config["timeout"],
                request_type="vision",
            )
            if not response:
                return {
                    "invoice_no": None,
                    "full_data": None,
                    "extraction_method": "failed",
                }, {"input_tokens": 0, "output_tokens": 0}

            data = response.json()
            usage_meta = data.get("usageMetadata", {})
            token_usage = {
                "input_tokens": int(usage_meta.get("promptTokenCount", 0)),
                "output_tokens": int(usage_meta.get("candidatesTokenCount", 0)),
            }
            response_text = data["candidates"][0]["content"]["parts"][0]["text"]
            response_text = response_text.strip()
            if response_text.startswith("```"):
                response_text = (
                    response_text.replace("```json", "").replace("```", "").strip()
                )
            parsed = json.loads(response_text)
            if isinstance(parsed, dict):
                parsed.pop("ocr_text", None)
                if isinstance(parsed.get("data"), dict):
                    parsed["data"].pop("ocr_text", None)
            return {
                "invoice_no": parsed.get("invoice_no", ""),
                "full_data": parsed,
                "extraction_method": "gemini_vision",
                "ocr_text": "",
            }, token_usage
        except Exception as exc:
            logger.error("Gemini vision extraction failed: %s", exc)
            return {
                "invoice_no": None,
                "full_data": None,
                "extraction_method": "failed",
            }, {"input_tokens": 0, "output_tokens": 0}
