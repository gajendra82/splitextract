"""Process-level OCR runtime. Reuses one orchestrator / VL model across pages."""

from __future__ import annotations

import io
import logging
import threading
from typing import Any, Dict, Optional, Tuple

from services.ocr.base import OCRResult
from services.ocr.contract import OCRDocumentEvidence, page_evidence_from_result
from services.ocr.orchestrator import OCRBundle, OCROrchestrator, ocr_settings

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_ORCHESTRATOR: Optional[OCROrchestrator] = None


def get_orchestrator() -> OCROrchestrator:
    global _ORCHESTRATOR
    if _ORCHESTRATOR is not None:
        return _ORCHESTRATOR
    with _LOCK:
        if _ORCHESTRATOR is None:
            _ORCHESTRATOR = OCROrchestrator(settings=ocr_settings())
        return _ORCHESTRATOR


def reset_orchestrator() -> None:
    """Test helper. Releases the process singleton."""
    global _ORCHESTRATOR
    with _LOCK:
        _ORCHESTRATOR = None


def recognize_image(
    image_bytes: bytes,
    *,
    page: int = 1,
    metadata: Optional[Dict[str, Any]] = None,
    use_fallback: bool = True,
) -> OCRBundle:
    from services.ocr.paddle_vl_engine import paddle_init_blocked

    if paddle_init_blocked() and primary_is_paddle_vl() and not use_fallback:
        blocked = paddle_init_blocked() or "paddle_init_failed"
        primary = OCRResult(
            text="",
            words=[],
            confidence=None,
            page=page,
            engine="paddleocr-vl",
            metadata={
                "status": "error",
                "error": blocked,
                "available": False,
                "persistent_init_failure": True,
            },
        )
        return OCRBundle(
            primary=primary,
            engines_used=[primary.engine],
            metadata={"status": "ocr_error", "error": blocked, "persistent_init_failure": True},
        )
    return get_orchestrator().recognize(
        image_bytes, page=page, metadata=metadata, use_fallback=use_fallback
    )


def ocr_image_text(
    image_bytes: bytes,
    *,
    page: int = 1,
    metadata: Optional[Dict[str, Any]] = None,
    use_fallback: bool = True,
) -> Tuple[str, Dict[str, Any]]:
    """Return ``(text, meta)`` from the configured primary OCR engine.

    Meta always records engine, unverified_fallback, and status so a Gemini
    fallback is not reported as PaddleOCR output.
    """
    bundle = recognize_image(
        image_bytes, page=page, metadata=metadata, use_fallback=use_fallback
    )
    primary = bundle.primary
    text = (primary.text if primary else "") or ""
    meta = {
        "engine": (primary.engine if primary else "") or "",
        "status": bundle.metadata.get("status")
        or ((primary.metadata or {}).get("status") if primary else "error"),
        "unverified_fallback": bundle.unverified_fallback,
        "engines_used": list(bundle.engines_used),
        "error": bundle.metadata.get("error")
        or ((primary.metadata or {}).get("error") if primary else None),
        "disagreement": bundle.disagreement,
        "fallback_reason": bundle.metadata.get("fallback_reason"),
        "fallback_from": bundle.metadata.get("fallback_from"),
        "fallback_to": bundle.metadata.get("fallback_to"),
    }
    return text, meta


def ocr_pil_image_text(
    image: Any, *, page: int = 1, use_fallback: bool = True
) -> Tuple[str, Dict[str, Any]]:
    """OCR a PIL image through the primary engine."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return ocr_image_text(buffer.getvalue(), page=page, use_fallback=use_fallback)


def primary_is_classic_paddle() -> bool:
    name = (ocr_settings().primary_engine or "").strip().lower()
    return name in {"paddle", "paddleocr"}


def primary_is_paddle() -> bool:
    return primary_is_classic_paddle() or primary_is_paddle_vl()


def primary_is_paddle_vl() -> bool:
    name = (ocr_settings().primary_engine or "").strip().lower()
    return name in {"paddleocr-vl", "paddleocr-vl-1.6", "paddle_vl"}


def document_evidence_from_bundle(
    bundle: OCRBundle,
    *,
    document_id: str = "",
) -> OCRDocumentEvidence:
    pages = []
    if bundle.primary is not None:
        pages.append(page_evidence_from_result(bundle.primary))
    engine = ""
    model = None
    version = None
    if bundle.primary is not None:
        engine = bundle.primary.engine
        meta = bundle.primary.metadata or {}
        model = meta.get("model")
        version = meta.get("model_version")
    return OCRDocumentEvidence(
        document_id=document_id,
        engine=engine,
        model=model,
        model_version=version,
        pages=pages,
        unverified_fallback=bundle.unverified_fallback,
        metadata=dict(bundle.metadata),
    )
