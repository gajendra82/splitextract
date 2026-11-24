"""Structured OCR recovery after primary engine timeout / empty output.

Order (configurable via OCR_RECOVERY_CHAIN):
  1. tesseract — grounded local OCR with boxes/confidence
  2. gemini — transcription only (not quantity extraction)

Never invents missing quantities. Labels each result with ocr_source.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from services.ocr.base import OCRResult
from services.ocr.bounded import OcrExecutionTimeout, call_bounded
from services.ocr.orchestrator import OCRBundle

logger = logging.getLogger(__name__)

_DEFAULT_CHAIN = "tesseract,gemini"


def recovery_chain_engines() -> List[str]:
    raw = (os.getenv("OCR_RECOVERY_CHAIN") or _DEFAULT_CHAIN).strip().lower()
    if not raw or raw in {"none", "off", "0"}:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def tesseract_recovery_timeout_s() -> float:
    try:
        value = float(os.getenv("OCR_TESSERACT_RECOVERY_TIMEOUT_S", "45") or "45")
    except (TypeError, ValueError):
        value = 45.0
    return max(5.0, min(value, 120.0))


def recover_unusable_ocr(
    ocr: Any,
    image_bytes: bytes,
    *,
    page_no: int = 1,
    failed_primary: Optional[OCRResult] = None,
    fail_reason: str = "empty_or_timeout",
) -> Tuple[OCRBundle, Dict[str, Any]]:
    """Try Tesseract then Gemini transcription. Returns (bundle, meta)."""
    meta: Dict[str, Any] = {
        "recovery_attempted": True,
        "recovery_reason": fail_reason,
        "recovery_chain": recovery_chain_engines(),
        "recovery_log": [],
    }
    engines_used: List[str] = []
    if failed_primary is not None:
        engines_used.append(
            str(failed_primary.engine or failed_primary.metadata.get("engine") or "primary")
        )

    bundle = OCRBundle(
        primary=failed_primary,
        engines_used=list(engines_used),
        metadata={
            "status": "ocr_error",
            "error": fail_reason,
            "configured_primary": getattr(
                getattr(ocr, "settings", None), "primary_engine", None
            ),
        },
    )

    for name in recovery_chain_engines():
        engine = None
        try:
            engine = ocr.engine(name) if hasattr(ocr, "engine") else None
        except Exception:
            engine = None
        if engine is None or not engine.available():
            meta["recovery_log"].append({"engine": name, "ok": False, "error": "unavailable"})
            continue

        timeout_s = (
            tesseract_recovery_timeout_s()
            if name == "tesseract"
            else _gemini_timeout_s()
        )
        try:
            result = call_bounded(
                lambda eng=engine: eng.recognize(image_bytes, page=page_no),
                timeout_s=timeout_s,
                label=f"ocr_recovery_{name}_page_{page_no}",
            )
        except OcrExecutionTimeout as exc:
            meta["recovery_log"].append(
                {"engine": name, "ok": False, "error": f"timeout:{exc.timeout_s}"}
            )
            logger.warning(
                "ocr recovery engine=%s timed out after %ss page=%s",
                name,
                timeout_s,
                page_no,
            )
            continue
        except Exception as exc:
            meta["recovery_log"].append({"engine": name, "ok": False, "error": str(exc)[:200]})
            logger.warning("ocr recovery engine=%s failed: %s", name, exc)
            continue

        text = str(getattr(result, "text", "") or "").strip()
        usable = bool(text) and len(text) >= 20
        meta["recovery_log"].append(
            {
                "engine": name,
                "ok": usable,
                "text_length": len(text),
                "error": None if usable else "empty_or_short",
            }
        )
        if not usable:
            continue

        # Annotate evidence provenance — transcription ≠ verified native OCR.
        result_meta = dict(getattr(result, "metadata", None) or {})
        result_meta["ocr_source"] = name
        result_meta["ocr_recovery"] = True
        result_meta["recovery_from"] = fail_reason
        if name == "gemini":
            result_meta["transcription_only"] = True
            result_meta["unverified_fallback"] = True
        result.metadata = result_meta
        if not getattr(result, "engine", None):
            result.engine = name

        bundle.secondary = failed_primary or bundle.primary
        bundle.primary = result
        bundle.engines_used = list(dict.fromkeys(engines_used + [name]))
        bundle.metadata["status"] = "fallback" if name == "gemini" else "recovered"
        bundle.metadata["fallback_used"] = True
        bundle.metadata["fallback_from"] = (
            (failed_primary.engine if failed_primary else None) or "primary"
        )
        bundle.metadata["fallback_to"] = name
        bundle.metadata["fallback_reason"] = fail_reason
        bundle.metadata["ocr_source"] = name
        if name == "gemini":
            bundle.unverified_fallback = True
            bundle.metadata["unverified_fallback"] = True
        else:
            # Tesseract is grounded local OCR — still mark recovery for diagnostics.
            bundle.unverified_fallback = False
            bundle.metadata["unverified_fallback"] = False
        bundle.metadata.pop("error", None)
        meta["accepted_engine"] = name
        logger.info(
            "ocr recovery accepted engine=%s page=%s text_length=%s",
            name,
            page_no,
            len(text),
        )
        return bundle, meta

    meta["accepted_engine"] = None
    logger.warning(
        "ocr recovery exhausted page=%s reason=%s chain=%s",
        page_no,
        fail_reason,
        meta["recovery_chain"],
    )
    return bundle, meta


def _gemini_timeout_s() -> float:
    try:
        from services.ocr.gemini_fallback import gemini_fallback_timeout_s

        return float(gemini_fallback_timeout_s())
    except Exception:
        return 60.0
