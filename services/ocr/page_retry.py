"""Single-page OCR retry helpers (invert / zoom / tesseract second opinion)."""

from __future__ import annotations

import logging
import os
from typing import Any, List

from services.ocr.cv2_compat import HAS_CV2, cv2

logger = logging.getLogger(__name__)


def page_retry_enabled() -> bool:
    return os.getenv("OCR_PAGE_RETRY", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def max_page_retries() -> int:
    """Default 1 retry after the primary attempt (env ``OCR_PAGE_RETRY_COUNT``)."""
    raw = os.getenv("OCR_PAGE_RETRY_COUNT", "1")
    try:
        return max(0, min(3, int(raw)))
    except (TypeError, ValueError):
        return 1


def should_retry_page(bundle: Any) -> bool:
    """True when primary OCR looks empty / failed / low confidence."""
    primary = getattr(bundle, "primary", None)
    if primary is None:
        return True
    text = str(getattr(primary, "text", "") or "").strip()
    if not text:
        return True
    conf = getattr(primary, "confidence", None)
    if conf is not None and float(conf) < 0.7:
        return True
    meta = getattr(primary, "metadata", None) or {}
    status = str(meta.get("status") or "").casefold()
    if "error" in status or "timeout" in status:
        return True
    if getattr(primary, "timed_out", False):
        return True
    return False


def invert_image_bytes(image_bytes: bytes) -> bytes:
    if not HAS_CV2 or cv2 is None:
        return image_bytes
    import numpy as np  # type: ignore

    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return image_bytes
    inverted = cv2.bitwise_not(img)
    ok, encoded = cv2.imencode(".png", inverted)
    return encoded.tobytes() if ok else image_bytes


def zoom_table_region(image_bytes: bytes, scale: float = 2.0) -> bytes:
    """2x zoom on central table band (heuristic crop)."""
    if not HAS_CV2 or cv2 is None:
        return image_bytes
    import numpy as np  # type: ignore

    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return image_bytes
    h, w = img.shape[:2]
    y0, y1 = int(h * 0.12), int(h * 0.95)
    x0, x1 = int(w * 0.02), int(w * 0.98)
    crop = img[y0:y1, x0:x1]
    zoomed = cv2.resize(
        crop,
        (max(1, int(crop.shape[1] * scale)), max(1, int(crop.shape[0] * scale))),
        interpolation=cv2.INTER_CUBIC,
    )
    ok, encoded = cv2.imencode(".png", zoomed)
    return encoded.tobytes() if ok else image_bytes


def low_confidence_cells(bundle: Any, threshold: float = 0.7) -> List[dict]:
    """Return metadata markers for cells/words below confidence threshold."""
    primary = getattr(bundle, "primary", None)
    if primary is None:
        return []
    flagged = []
    words = getattr(primary, "words", None) or []
    for word in words:
        conf = getattr(word, "confidence", None)
        if conf is None:
            continue
        try:
            if float(conf) < threshold:
                flagged.append(
                    {
                        "text": getattr(word, "text", ""),
                        "confidence": float(conf),
                        "bbox": getattr(word, "bbox", None),
                    }
                )
        except (TypeError, ValueError):
            continue
    return flagged
