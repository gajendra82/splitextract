"""Straighten a mildly tilted photo of a ruled stock statement.

Camera tilt makes one product row slope across the next row, so OCR and
Vision collapse several products into one jumbled line. This only rotates
when long near-horizontal rules share a small angle. Level images and
sideways pages are returned unchanged.

STOCK_PHOTO_DESKEW defaults on. Set it to false to skip.
Does not rewrite quantities. Does not add a per-stockist route.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

STOCK_PHOTO_DESKEW_FLAG = "STOCK_PHOTO_DESKEW"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def stock_photo_deskew_enabled() -> bool:
    raw = os.getenv(STOCK_PHOTO_DESKEW_FLAG, "true").strip().lower()
    if raw in _FALSE:
        return False
    return raw in _TRUE or raw == ""


def _table_rule_angle(gray) -> Optional[float]:
    """Median angle (degrees) of long near-horizontal rules. None if unclear."""
    import cv2
    import numpy as np

    h, w = gray.shape[:2]
    if h < 80 or w < 80:
        return None
    scale = 1000.0 / float(max(h, w))
    if scale < 1.0:
        small = cv2.resize(
            gray,
            (max(1, int(w * scale)), max(1, int(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        small = gray
    bw = cv2.adaptiveThreshold(
        small,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        31,
        8,
    )
    min_len = max(80, int(small.shape[1] * 0.18))
    lines = cv2.HoughLinesP(
        bw, 1, np.pi / 180, threshold=60, minLineLength=min_len, maxLineGap=16
    )
    if lines is None:
        return None
    angles = []
    for seg in lines.reshape(-1, 4):
        x1, y1, x2, y2 = (int(v) for v in seg)
        dx = x2 - x1
        if abs(dx) < min_len * 0.6:
            continue
        ang = float(np.degrees(np.arctan2(y2 - y1, dx)))
        if abs(ang) <= 12.0:
            angles.append(ang)
    if len(angles) < 8:
        return None
    return float(np.median(angles))


def maybe_deskew_stock_photo(file_bytes: bytes) -> bytes:
    """Return straightened JPEG/PNG bytes, or the original when not tilted."""
    if not file_bytes or not stock_photo_deskew_enabled():
        return file_bytes
    try:
        import cv2
        import numpy as np
    except Exception:
        logger.info("STOCK_PHOTO_DESKEW status=skipped reason=no_cv2")
        return file_bytes
    arr = np.frombuffer(file_bytes, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        return file_bytes
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    angle = _table_rule_angle(gray)
    if angle is None or abs(angle) < 0.8 or abs(angle) > 8.0:
        logger.info(
            "STOCK_PHOTO_DESKEW status=skipped angle=%s",
            None if angle is None else round(angle, 2),
        )
        return file_bytes
    h, w = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
    rotated = cv2.warpAffine(
        image,
        matrix,
        (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    ext_ok, encoded = cv2.imencode(
        ".jpg", rotated, [int(cv2.IMWRITE_JPEG_QUALITY), 92]
    )
    if not ext_ok:
        logger.info("STOCK_PHOTO_DESKEW status=skipped reason=encode_failed")
        return file_bytes
    logger.info("STOCK_PHOTO_DESKEW status=applied angle=%.2f", angle)
    return encoded.tobytes()


def measure_table_rule_angle(file_bytes: bytes) -> Optional[float]:
    """Test helper: angle of table rules, or None."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    arr = np.frombuffer(file_bytes, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return _table_rule_angle(gray)
