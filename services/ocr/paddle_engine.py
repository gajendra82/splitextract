"""PaddleOCR adapter. Optional; unavailable when the package is not installed."""

from __future__ import annotations

import io
import logging
from typing import Any, Dict, Optional

from services.ocr.base import OCREngine, OCRResult, OCRWord

logger = logging.getLogger(__name__)


class PaddleEngine(OCREngine):
    name = "paddle"

    def __init__(self) -> None:
        self._ocr = None
        self._init_error: Optional[str] = None

    def available(self) -> bool:
        """Cheap presence check. Do not import paddleocr/torch here."""
        try:
            import importlib.util

            if importlib.util.find_spec("paddleocr") is None:
                self._init_error = "paddleocr_not_installed"
                return False
            if importlib.util.find_spec("numpy") is None:
                self._init_error = "numpy_not_installed"
                return False
            return True
        except Exception as exc:
            self._init_error = str(exc)
            return False

    def _client(self):
        if self._ocr is not None:
            return self._ocr
        from paddleocr import PaddleOCR

        # Lightweight defaults; production policy decides when to call.
        self._ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
        return self._ocr

    def recognize(
        self,
        image_bytes: bytes,
        *,
        page: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OCRResult:
        if not self.available():
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **dict(metadata or {}),
                    "available": False,
                    "error": self._init_error or "paddleocr_not_installed",
                },
            )
        try:
            from PIL import Image
            import numpy as np

            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            array = np.array(image)
            client = self._client()
            raw = client.ocr(array, cls=True)
        except BaseException as exc:
            # DLL / torch load failures must not crash the process.
            logger.warning("paddleocr recognize failed: %s", exc)
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **dict(metadata or {}),
                    "available": False,
                    "error": str(exc),
                },
            )
        words = []
        texts = []
        confidences = []
        blocks = raw[0] if raw else []
        for block in blocks or []:
            if not block or len(block) < 2:
                continue
            box, payload = block[0], block[1]
            text = str(payload[0] or "").strip()
            try:
                conf = float(payload[1])
            except (TypeError, ValueError, IndexError):
                conf = None
            if not text:
                continue
            texts.append(text)
            if conf is not None:
                confidences.append(conf)
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            words.append(
                OCRWord(
                    text=text,
                    bbox=(min(xs), min(ys), max(xs), max(ys)),
                    confidence=conf,
                    page=page,
                )
            )
        mean_conf = (
            sum(confidences) / len(confidences) if confidences else None
        )
        meta = dict(metadata or {})
        meta["word_count"] = len(words)
        meta["available"] = True
        return OCRResult(
            text="\n".join(texts),
            words=words,
            confidence=mean_conf,
            page=page,
            engine=self.name,
            metadata=meta,
        )
