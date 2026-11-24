"""OCR orchestration. PaddleOCR-VL is the production primary.

Gemini transcribes the page only when PaddleOCR-VL is unavailable, raises, or
returns empty text. A Gemini result is labeled ``gemini`` and is not reported
as PaddleOCR output. Explicit ``OCR_ENGINE=tesseract`` remains available for
diagnostic runs; it is not the fallback.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.ocr.base import OCREngine, OCRResult
from services.ocr.bounded import OcrExecutionTimeout, call_bounded, ocr_execution_timeout_s
from services.ocr.gemini_fallback import GeminiOCRFallback
from services.ocr.paddle_engine import PaddleEngine
from services.ocr.paddle_vl_engine import PaddleVLEngine, paddle_init_blocked
from services.ocr.surya_engine import SuryaEngine
from services.ocr.tesseract_engine import TesseractEngine

logger = logging.getLogger(__name__)

_DEFAULT_PRIMARY = "paddleocr-vl"
_DEFAULT_FALLBACK = "gemini"


@dataclass
class OCRSettings:
    primary_engine: str = _DEFAULT_PRIMARY
    fallback_engine: str = _DEFAULT_FALLBACK
    ensemble_enabled: bool = False
    disagreement_threshold: float = 0.15
    allow_unverified_fallback: bool = False


def ocr_settings() -> OCRSettings:
    """Environment-driven OCR policy.

    Primary default is PaddleOCR-VL. Fallback default is Gemini, used only
    when the primary output is unusable. Ensemble comparison is not used.
    """
    return OCRSettings(
        primary_engine=os.getenv("OCR_ENGINE", _DEFAULT_PRIMARY).strip().lower()
        or _DEFAULT_PRIMARY,
        fallback_engine=os.getenv("OCR_FALLBACK_ENGINE", _DEFAULT_FALLBACK)
        .strip()
        .lower()
        or _DEFAULT_FALLBACK,
        ensemble_enabled=os.getenv("OCR_ENSEMBLE_ENABLED", "false").strip().lower()
        in {"1", "true", "yes", "on"},
        disagreement_threshold=float(
            os.getenv("OCR_DISAGREEMENT_THRESHOLD", "0.15") or "0.15"
        ),
        allow_unverified_fallback=os.getenv(
            "OCR_ALLOW_UNVERIFIED_FALLBACK", "false"
        )
        .strip()
        .lower()
        in {"1", "true", "yes", "on"},
    )


@dataclass
class OCRBundle:
    primary: Optional[OCRResult] = None
    secondary: Optional[OCRResult] = None
    disagreement: bool = False
    engines_used: List[str] = field(default_factory=list)
    unverified_fallback: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "primary": self.primary.to_dict() if self.primary else None,
            "secondary": self.secondary.to_dict() if self.secondary else None,
            "disagreement": self.disagreement,
            "engines_used": list(self.engines_used),
            "unverified_fallback": self.unverified_fallback,
            "metadata": dict(self.metadata),
        }


class OCROrchestrator:
    """Run PaddleOCR-VL, then Gemini only when that output is unusable."""

    def __init__(
        self,
        *,
        settings: Optional[OCRSettings] = None,
        tesseract: Optional[OCREngine] = None,
        paddle: Optional[OCREngine] = None,
        paddle_vl: Optional[OCREngine] = None,
        surya: Optional[OCREngine] = None,
        gemini: Optional[OCREngine] = None,
    ) -> None:
        self.settings = settings or ocr_settings()
        vl = paddle_vl or PaddleVLEngine()
        surya_engine = surya or SuryaEngine()
        gemini_engine = gemini or GeminiOCRFallback()
        self._engines = {
            "tesseract": tesseract or TesseractEngine(),
            "paddle": paddle or PaddleEngine(),
            "paddleocr-vl": vl,
            "paddleocr-vl-1.6": vl,
            "paddle_vl": vl,
            "surya": surya_engine,
            "surya-2": surya_engine,
            "surya_2": surya_engine,
            "gemini": gemini_engine,
        }

    def engine(self, name: str) -> Optional[OCREngine]:
        return self._engines.get((name or "").strip().lower())

    def recognize(
        self,
        image_bytes: bytes,
        *,
        page: int = 1,
        force_secondary: bool = False,
        low_confidence: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        use_fallback: bool = True,
    ) -> OCRBundle:
        del force_secondary, low_confidence
        settings = self.settings
        primary_name = settings.primary_engine
        primary_engine = self.engine(primary_name)
        bundle = OCRBundle(
            metadata={
                **dict(metadata or {}),
                "configured_primary": primary_name,
                "configured_fallback": settings.fallback_engine,
                "fallback_used": False,
            }
        )
        if primary_engine is None or not primary_engine.available():
            primary = _error_result(primary_name, page, f"primary_ocr_unavailable:{primary_name}")
        elif paddle_init_blocked() and str(primary_name).startswith("paddleocr-vl"):
            primary = _error_result(primary_name, page, paddle_init_blocked() or "paddle_init_failed")
            primary.metadata["persistent_init_failure"] = True
        else:
            primary = _recognize_bounded(
                primary_engine,
                image_bytes,
                page=page,
                metadata=metadata,
                label=primary_name,
            )
        bundle.primary = primary
        bundle.engines_used.append(primary.engine or primary_name)
        reason = _unusable_reason(primary)
        if reason is None:
            bundle.metadata["status"] = "success"
            logger.info("ocr_engine_used engine=%s fallback=false", primary.engine or primary_name)
            return bundle
        if not use_fallback:
            bundle.metadata["status"] = "ocr_error"
            bundle.metadata["error"] = reason
            bundle.metadata["fallback_skipped"] = "caller_disabled"
            logger.info(
                "ocr_engine_used engine=%s fallback=false reason=%s",
                primary.engine or primary_name,
                reason,
            )
            return bundle

        fallback_name = (settings.fallback_engine or "").strip().lower()
        bundle.metadata["fallback_reason"] = reason
        if not fallback_name or fallback_name in {"none", "off"} or fallback_name == (primary.engine or primary_name):
            bundle.metadata["status"] = "ocr_error"
            bundle.metadata["error"] = reason
            logger.info("ocr_engine_used engine=%s fallback=false reason=%s", primary.engine or primary_name, reason)
            return bundle
        fallback = self.engine(fallback_name)
        if fallback is None or not fallback.available():
            bundle.metadata["status"] = "ocr_error"
            bundle.metadata["error"] = reason
            bundle.metadata["fallback_unavailable"] = fallback_name
            logger.info(
                "ocr_fallback_unavailable from=%s to=%s reason=%s",
                primary_name,
                fallback_name,
                reason,
            )
            return bundle

        logger.info("ocr_fallback from=%s to=%s reason=%s", primary_name, fallback_name, reason)
        fallback_result = _recognize_bounded(
            fallback,
            image_bytes,
            page=page,
            metadata=metadata,
            label=fallback_name,
        )
        bundle.secondary = primary
        bundle.primary = fallback_result
        bundle.engines_used.append(fallback_result.engine or fallback_name)
        bundle.metadata["fallback_used"] = True
        bundle.metadata["fallback_from"] = primary.engine or primary_name
        bundle.metadata["fallback_to"] = fallback_result.engine or fallback_name
        if _unusable_reason(fallback_result) is None:
            bundle.unverified_fallback = True
            bundle.metadata["unverified_fallback"] = True
            bundle.metadata["status"] = "fallback"
            logger.info(
                "ocr_engine_used engine=%s fallback=true reason=%s",
                fallback_result.engine or fallback_name,
                reason,
            )
            return bundle
        bundle.metadata["status"] = "ocr_error"
        bundle.metadata["error"] = (fallback_result.metadata or {}).get("error") or "fallback_failed"
        logger.info(
            "ocr_engine_used engine=%s fallback=failed reason=%s",
            fallback_result.engine or fallback_name,
            bundle.metadata["error"],
        )
        return bundle


def ocr_readiness(settings: Optional[OCRSettings] = None) -> Dict[str, Any]:
    """Whether the configured primary engine can be called.

    This does not load Paddle or run a page. A persistent init failure, a
    missing runtime, or ``available() is False`` means the engine is not usable.
    """
    chosen = settings or ocr_settings()
    name = (chosen.primary_engine or _DEFAULT_PRIMARY).strip().lower() or _DEFAULT_PRIMARY
    try:
        if paddle_init_blocked() and name.startswith("paddleocr-vl"):
            return {
                "engine": name,
                "usable": False,
                "reason": paddle_init_blocked() or "paddle_init_failed",
            }
        engine = _readiness_engine(name)
        if engine is None:
            return {"engine": name, "usable": False, "reason": f"unknown_ocr_engine:{name}"}
        usable = bool(engine.available())
        reason = None
        if not usable:
            reason = getattr(engine, "_init_error", None) or f"primary_ocr_unavailable:{name}"
        return {"engine": name, "usable": usable, "reason": reason}
    except Exception as exc:
        return {"engine": name, "usable": False, "reason": str(exc)}


def _readiness_engine(name: str) -> Optional[OCREngine]:
    if name in {"paddleocr-vl", "paddleocr-vl-1.6", "paddle_vl"}:
        return PaddleVLEngine()
    if name == "tesseract":
        return TesseractEngine()
    if name == "paddle":
        return PaddleEngine()
    if name in {"surya", "surya-2", "surya_2"}:
        return SuryaEngine()
    if name == "gemini":
        return GeminiOCRFallback()
    return None


def _recognize_bounded(
    engine: OCREngine,
    image_bytes: bytes,
    *,
    page: int,
    metadata: Optional[Dict[str, Any]],
    label: str,
) -> OCRResult:
    try:
        return call_bounded(
            lambda: engine.recognize(image_bytes, page=page, metadata=metadata),
            ocr_execution_timeout_s(),
            label=label or "ocr",
        )
    except OcrExecutionTimeout as exc:
        logger.warning("ocr_timeout engine=%s timeout_s=%s", label, exc.timeout_s)
        if "paddle" in str(label):
            try:
                from services.ocr.paddle_vl_engine import kill_paddle_vl_serve

                kill_paddle_vl_serve()
            except Exception:
                logger.warning("ocr_timeout_kill_failed engine=%s", label, exc_info=True)
        result = _error_result(label, page, "ocr_timeout")
        result.metadata["provider_error"] = True
        result.metadata["timeout_s"] = exc.timeout_s
        return result
    except Exception as exc:
        logger.warning("ocr_exception engine=%s error=%s", label, exc)
        return _error_result(label, page, str(exc))


def _error_result(engine: str, page: int, error: str) -> OCRResult:
    return OCRResult(
        text="",
        words=[],
        confidence=None,
        page=page,
        engine=engine,
        metadata={"status": "error", "error": error, "available": False},
    )


def _unusable_reason(result: Optional[OCRResult]) -> Optional[str]:
    """Reasons that justify Gemini. Low confidence alone is not one of them."""
    if result is None:
        return "empty_output"
    meta = result.metadata or {}
    if meta.get("status") == "error" or meta.get("available") is False:
        return str(meta.get("error") or "ocr_error")
    if not str(result.text or "").strip():
        return "empty_output"
    return None

