"""Tesseract adapter over the existing gated pytesseract path."""

from __future__ import annotations

import io
import logging
from typing import Any, Dict, Optional

from services.ocr.base import OCREngine, OCRResult, OCRWord

logger = logging.getLogger(__name__)


class TesseractEngine(OCREngine):
    name = "tesseract"

    def available(self) -> bool:
        try:
            from services.sales_statement_extractor import _a2z_tesseract

            _a2z_tesseract()
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

        from services.sales_statement_extractor import _a2z_tesseract

        pytesseract = _a2z_tesseract()
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        data = pytesseract.image_to_data(
            image, config="--psm 6", output_type=pytesseract.Output.DICT
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
        text = pytesseract.image_to_string(image, config="--psm 6") or ""
        mean_conf = (
            sum(confidences) / len(confidences) if confidences else None
        )
        meta = dict(metadata or {})
        meta["word_count"] = len(words)
        return OCRResult(
            text=text,
            words=words,
            confidence=mean_conf,
            page=page,
            engine=self.name,
            metadata=meta,
        )
