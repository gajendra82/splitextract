"""Route general ``image_to_string`` calls through the OCR orchestrator.

Layout-specific ``image_to_data`` and digit-whitelist crops stay on Tesseract.
Those calls do not invoke the Gemini document fallback.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_WHITELIST_RE = re.compile(r"tessedit_char_whitelist\s*=", re.I)


class PrimaryOCRPytesseractShim:
    """Wrap a pytesseract module. Preserves Output / tesseract_cmd attributes."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def image_to_string(self, image: Any, config: str = "", **kwargs: Any) -> str:
        cfg = str(config or "")
        # Digit/character whitelist crops are intentional Tesseract geometry reads.
        if _WHITELIST_RE.search(cfg):
            return self._inner.image_to_string(image, config=config, **kwargs) or ""

        try:
            from services.ocr.runtime import ocr_pil_image_text, primary_is_paddle_vl
        except Exception:
            return self._inner.image_to_string(image, config=config, **kwargs) or ""

        if not primary_is_paddle_vl():
            return self._inner.image_to_string(image, config=config, **kwargs) or ""

        pil = _as_pil(image)
        if pil is None:
            return self._inner.image_to_string(image, config=config, **kwargs) or ""

        text, meta = ocr_pil_image_text(pil, use_fallback=False)
        logger.debug(
            "primary_ocr_bridge engine=%s status=%s",
            meta.get("engine"),
            meta.get("status"),
        )
        # Do not silently fall through to Tesseract when VL is primary.
        return text or ""

    def image_to_data(self, image: Any, *args: Any, **kwargs: Any) -> Any:
        # Word boxes / layout geometry remain on Tesseract by design.
        return self._inner.image_to_data(image, *args, **kwargs)


def _as_pil(image: Any) -> Optional[Any]:
    try:
        from PIL import Image
    except Exception:
        return None
    if isinstance(image, Image.Image):
        if image.mode not in ("RGB", "L"):
            return image.convert("RGB")
        return image.convert("RGB") if image.mode != "RGB" else image
    # numpy array / path-like: let pytesseract handle via fallback
    try:
        if hasattr(image, "shape"):
            return Image.fromarray(image).convert("RGB")
    except Exception:
        return None
    return None
