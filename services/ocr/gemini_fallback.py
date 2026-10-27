"""Gemini transcription fallback for unusable PaddleOCR-VL output.

This is not a second OCR engine for healthy pages. It returns plain text for
the existing extraction pipeline and labels the result as Gemini.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from typing import Any, Callable, Optional

from services.ocr.base import OCREngine, OCRResult

logger = logging.getLogger(__name__)

_PROMPT = (
    "Transcribe this document image into plain text. Keep the printed line breaks. "
    "Copy only text that is visible. Do not calculate missing numbers and do not "
    "treat a blank cell as zero. Return only the transcription."
)


def gemini_fallback_timeout_s() -> float:
    try:
        value = float(os.getenv("GEMINI_OCR_FALLBACK_TIMEOUT_S", "60") or "60")
    except (TypeError, ValueError):
        value = 60.0
    return max(1.0, min(value, 120.0))


def _image_mime(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return "image/png"


def _response_text(response: Any) -> str:
    payload = response.json() if hasattr(response, "json") else response
    if not isinstance(payload, dict):
        return str(payload or "")
    candidates = payload.get("candidates") or [{}]
    content = (candidates[0] or {}).get("content") or {}
    parts = content.get("parts") or [{}]
    return str((parts[0] or {}).get("text") or "")


def _default_invoke(image_bytes: bytes, timeout: float) -> str:
    from services.sales_extraction_runtime import sales_generate_content_via_vertex

    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip() or "gemini-2.5-flash-lite"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _PROMPT},
                    {
                        "inline_data": {
                            "mime_type": _image_mime(image_bytes),
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 8192},
    }
    response = sales_generate_content_via_vertex(
        model=model,
        payload=payload,
        timeout=int(timeout),
        label="ocr_gemini_fallback",
    )
    return _response_text(response)


def _run_bounded(fn: Callable[[], str], timeout: float) -> str:
    """Return when the deadline expires. Do not join a hung Gemini call."""
    outcome: dict = {}

    def target() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=target, name="gemini-ocr-fallback", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"Gemini OCR fallback timed out after {timeout}s")
    if "error" in outcome:
        raise outcome["error"]
    return str(outcome.get("value") or "")


class GeminiOCRFallback(OCREngine):
    name = "gemini"

    def __init__(
        self,
        *,
        invoke: Optional[Callable[[bytes, float], str]] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self._invoke = invoke or _default_invoke
        self._timeout_s = gemini_fallback_timeout_s() if timeout_s is None else timeout_s

    def available(self) -> bool:
        return True

    def recognize(
        self,
        image_bytes: bytes,
        *,
        page: int = 1,
        metadata: Optional[dict] = None,
    ) -> OCRResult:
        base = dict(metadata or {})
        timeout = self._timeout_s
        try:
            text = _run_bounded(lambda: self._invoke(image_bytes, timeout), timeout)
        except Exception as exc:
            logger.warning("ocr_fallback engine=gemini status=error error=%s", exc)
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **base,
                    "status": "error",
                    "error": str(exc),
                    "model": os.getenv("VISION_MODEL", "gemini-2.5-flash-lite"),
                },
            )
        cleaned = str(text or "").strip()
        if not cleaned:
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={**base, "status": "error", "error": "empty_output"},
            )
        return OCRResult(
            text=cleaned,
            words=[],
            confidence=None,
            page=page,
            engine=self.name,
            metadata={
                **base,
                "status": "success",
                "model": os.getenv("VISION_MODEL", "gemini-2.5-flash-lite"),
                "source": "gemini_ocr_fallback",
            },
        )
