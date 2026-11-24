"""Shared OpenCV import with one-time graceful fallback."""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import cv2 as _cv2  # type: ignore

    cv2: Any = _cv2
    HAS_CV2 = True
except ImportError:
    cv2 = None
    HAS_CV2 = False
    logger.warning("cv2 not available — preprocessing disabled")


def require_cv2() -> Optional[Any]:
    """Return the cv2 module, or ``None`` when unavailable."""
    return cv2 if HAS_CV2 else None
