"""Tesseract adapter over native pytesseract (not the primary-OCR shim).

Recovery and diagnostics need real Tesseract text. Routing through
``_a2z_tesseract`` / ``PrimaryOCRPytesseractShim`` would call PaddleOCR-VL
when VL is primary and defeat the Tesseract recovery step.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any, Dict, Optional

from services.ocr.base import OCREngine, OCRResult, OCRWord

logger = logging.getLogger(__name__)


def _page_segmentation_mode() -> int:
    """Read the optional Tesseract PSM; keep the existing mode as default."""
    raw = (os.getenv("TESSERACT_PSM") or "6").strip()
    try:
        mode = int(raw)
    except (TypeError, ValueError):
        return 6
    return mode if 0 <= mode <= 13 else 6


def _native_pytesseract():
    """Return pytesseract configured with TESSERACT_CMD, without the VL shim."""
    import pytesseract

    cmd = os.getenv("TESSERACT_CMD", "").strip()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    elif os.name == "nt":
        win_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(win_cmd):
            pytesseract.pytesseract.tesseract_cmd = win_cmd
    return pytesseract


class TesseractEngine(OCREngine):
    name = "tesseract"

    def available(self) -> bool:
        try:
            _native_pytesseract()
            return True
        except Exception:
            return False

    def recognize(
        self,
        image_bytes: bytes,
        *,
        page: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OCRResult:
        from PIL import Image

        pytesseract = _native_pytesseract()
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        psm = _page_segmentation_mode()
        config = f"--psm {psm}"
        data = pytesseract.image_to_data(
            image, config=config, output_type=pytesseract.Output.DICT
        )
        words = []
        confidences = []
        texts = data.get("text") or []
        for index, raw in enumerate(texts):
            text = str(raw or "").strip()
            if not text:
                continue
            try:
                conf = float((data.get("conf") or [])[index])
            except (TypeError, ValueError, IndexError):
                conf = None
            if conf is not None and conf < 0:
                continue
            if conf is not None:
                confidences.append(conf / 100.0 if conf > 1.0 else conf)
            left = float((data.get("left") or [0])[index] or 0)
            top = float((data.get("top") or [0])[index] or 0)
            width = float((data.get("width") or [0])[index] or 0)
            height = float((data.get("height") or [0])[index] or 0)
            words.append(
                OCRWord(
                    text=text,
                    bbox=(left, top, left + width, top + height),
                    confidence=None if conf is None else (
                        conf / 100.0 if conf > 1.0 else conf
                    ),
                    page=page,
                )
            )
        text = pytesseract.image_to_string(image, config=config) or ""
        if not str(text).strip() and words:
            # Geometry path succeeded; assemble line text rather than inventing.
            text = " ".join(w.text for w in words if w.text)
        mean_conf = (
            sum(confidences) / len(confidences) if confidences else None
        )
        meta = dict(metadata or {})
        meta["word_count"] = len(words)
        meta["page_segmentation_mode"] = psm
        meta["ocr_source"] = "tesseract"
        return OCRResult(
            text=text,
            words=words,
            confidence=mean_conf,
            page=page,
            engine=self.name,
            metadata=meta,
        )
