"""Fast photo-quality gate for stock-statement image uploads.

When STOCK_PHOTO_QUALITY_GATE is on, hopeless photos are rejected before
OCR/Gemini: blurry, too far, too dark/small, or a camera photo of a screen.

Default OFF. Spreadsheets, Word, TXT, and PDFs are never gated here.
Does not rewrite quantities. Does not add a per-stockist route.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STOCK_PHOTO_QUALITY_GATE_FLAG = "STOCK_PHOTO_QUALITY_GATE"
STOCK_PHOTO_QUALITY_TYPES_FLAG = "STOCK_PHOTO_QUALITY_TYPES"

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

_TRUE = {"1", "true", "yes", "on"}

DEFAULT_RETRY_MESSAGE = (
    "Photo is not clear. Please take a closer, in-focus picture of the "
    "printed statement (do not photograph a screen) and retry."
)

_REASON_MESSAGES = {
    "blurry": (
        "Photo is not clear. Keep the camera steady, in focus, and retry."
    ),
    "too_far": (
        "Move closer so the full table fills the photo, then retry."
    ),
    "too_small": (
        "Photo is too small or too far. Move closer and retry."
    ),
    "too_dark": (
        "Take the photo in better light and retry."
    ),
    "screen_photo": (
        "Do not photograph a computer or phone screen. Upload a screenshot "
        "or a clear photo of the printed statement, then retry."
    ),
    "unreadable": DEFAULT_RETRY_MESSAGE,
}


def stock_photo_quality_gate_enabled() -> bool:
    """Default OFF — no production behaviour change until flipped."""
    value = os.getenv(STOCK_PHOTO_QUALITY_GATE_FLAG, "")
    return value.strip().lower() in _TRUE


def stock_photo_quality_types() -> set:
    raw = os.getenv(STOCK_PHOTO_QUALITY_TYPES_FLAG, "image")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def stock_photo_quality_applies(ext: Optional[str]) -> bool:
    if not stock_photo_quality_gate_enabled():
        return False
    extension = (ext or "").strip().lower()
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    if extension not in _IMAGE_EXTS:
        return False
    return "image" in stock_photo_quality_types()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _decode_bgr(file_bytes: bytes):
    import cv2
    import numpy as np

    if not file_bytes:
        return None
    arr = np.frombuffer(file_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _downscale(bgr, max_side: int = 1280):
    import cv2

    h, w = bgr.shape[:2]
    side = max(h, w)
    if side <= max_side:
        return bgr
    scale = max_side / float(side)
    return cv2.resize(
        bgr,
        (max(1, int(w * scale)), max(1, int(h * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _laplacian_var(gray) -> float:
    import cv2

    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _edge_fill_ratio(gray) -> float:
    import cv2
    import numpy as np

    edges = cv2.Canny(gray, 40, 120)
    ys, xs = np.where(edges > 0)
    if xs.size < 80:
        return 0.0
    h, w = gray.shape[:2]
    box = (int(xs.max()) - int(xs.min()) + 1) * (int(ys.max()) - int(ys.min()) + 1)
    return float(box) / float(max(h * w, 1))


def _chroma_hf(bgr) -> float:
    import cv2
    import numpy as np

    ycrcb = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
    cr = ycrcb[:, :, 1].astype(np.float32)
    cb = ycrcb[:, :, 2].astype(np.float32)
    return float(
        (
            np.mean(np.abs(cv2.Laplacian(cr, cv2.CV_32F)))
            + np.mean(np.abs(cv2.Laplacian(cb, cv2.CV_32F)))
        )
        / 2.0
    )


def _lcd_speckle_ratio(bgr) -> float:
    """Fine chroma speckle vs coarse color structure.

    Camera photos of LCD/OLED screens show RGB-subpixel rainbow noise at
    native resolution that collapses when downscaled. Printed paper and
    digital screenshots keep color structure at both scales.
    """
    import cv2

    h, w = bgr.shape[:2]
    if h < 32 or w < 32:
        return 0.0
    fine = _chroma_hf(bgr)
    coarse_img = cv2.resize(
        bgr,
        (max(8, w // 4), max(8, h // 4)),
        interpolation=cv2.INTER_AREA,
    )
    coarse = _chroma_hf(coarse_img)
    return float(fine / (coarse + 1.0))


def _rgb_stripe_score(bgr) -> float:
    """Period-2/3 chroma energy from photographing an LCD subpixel grid."""
    import cv2
    import numpy as np

    if bgr.shape[1] < 32 or bgr.shape[0] < 32:
        return 0.0
    chroma = bgr[:, :, 2].astype(np.float32) - bgr[:, :, 1].astype(np.float32)
    chroma = chroma - cv2.blur(chroma, (5, 5))
    h = chroma.shape[0]
    rows = chroma[h // 4 : (3 * h) // 4]
    spec = np.abs(np.fft.rfft(rows, axis=1)).mean(axis=0)
    width = chroma.shape[1]
    peak = 0.0
    for period in (2.0, 3.0):
        idx = int(round(width / period))
        if idx < 2 or idx >= spec.size:
            continue
        lo, hi = max(1, idx - 1), min(spec.size, idx + 2)
        peak = max(peak, float(np.max(spec[lo:hi])))
    if spec.size < 8:
        return 0.0
    med = float(np.median(spec[1:]))
    return float(peak / (med + 1e-6))


def assess_stock_photo_quality(file_bytes: bytes) -> Dict[str, Any]:
    """Cheap pixel checks. No Tesseract, no Gemini.

    Returns {reject, reasons, metrics}.
    """
    reasons: List[str] = []
    metrics: Dict[str, Any] = {}
    min_side = _env_int("STOCK_PHOTO_MIN_SIDE", 400)
    blur_max = _env_float("STOCK_PHOTO_BLUR_VAR", 45.0)
    dark_max = _env_float("STOCK_PHOTO_DARK_MEAN", 28.0)
    fill_min = _env_float("STOCK_PHOTO_FILL_RATIO", 0.38)
    speckle_min = _env_float("STOCK_PHOTO_SCREEN_SPECKLE", 6.5)
    stripe_min = _env_float("STOCK_PHOTO_SCREEN_STRIPE", 4.0)

    try:
        bgr = _decode_bgr(file_bytes)
    except Exception as exc:
        logger.info(
            "STOCK_PHOTO_QUALITY decision=REJECT reason=unreadable error=%s",
            type(exc).__name__,
        )
        return {
            "reject": True,
            "reasons": ["unreadable"],
            "metrics": {"decode_error": type(exc).__name__},
        }
    if bgr is None or getattr(bgr, "size", 0) == 0:
        return {
            "reject": True,
            "reasons": ["unreadable"],
            "metrics": {"decode_error": "imdecode_none"},
        }

    import cv2

    full_h, full_w = bgr.shape[:2]
    metrics["width"] = int(full_w)
    metrics["height"] = int(full_h)
    if min(full_w, full_h) < min_side:
        reasons.append("too_small")

    bgr = _downscale(bgr)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    blur = _laplacian_var(gray)
    mean_lum = float(gray.mean())
    fill = _edge_fill_ratio(gray)
    speckle = _lcd_speckle_ratio(bgr)
    stripe = _rgb_stripe_score(bgr)
    metrics.update(
        {
            "blur_var": round(blur, 3),
            "mean_luminance": round(mean_lum, 2),
            "fill_ratio": round(fill, 4),
            "screen_speckle": round(speckle, 3),
            "screen_stripe": round(stripe, 4),
        }
    )

    if mean_lum < dark_max:
        reasons.append("too_dark")
    if blur < blur_max:
        reasons.append("blurry")
    if fill < fill_min and min(full_w, full_h) >= min_side:
        reasons.append("too_far")
    if speckle >= speckle_min or stripe >= stripe_min:
        reasons.append("screen_photo")

    reject = bool(reasons)
    logger.info(
        "STOCK_PHOTO_QUALITY decision=%s reasons=%s blur=%.1f fill=%.3f "
        "speckle=%.2f stripe=%.3f size=%sx%s",
        "REJECT" if reject else "PASS",
        ",".join(reasons) or "none",
        blur,
        fill,
        speckle,
        stripe,
        full_w,
        full_h,
    )
    return {"reject": reject, "reasons": reasons, "metrics": metrics}


def _retry_message(reasons: List[str]) -> str:
    for key in (
        "screen_photo",
        "too_far",
        "too_small",
        "blurry",
        "too_dark",
        "unreadable",
    ):
        if key in reasons:
            return _REASON_MESSAGES[key]
    return DEFAULT_RETRY_MESSAGE


def apply_stock_photo_quality_gate(
    file_bytes: bytes,
    filename: str,
    ext: str,
) -> Optional[Dict[str, Any]]:
    """Return a fail-closed empty result when the photo must not be processed.

    None means continue with the normal image path.
    """
    if not stock_photo_quality_applies(ext):
        return None
    assessment = assess_stock_photo_quality(file_bytes)
    if not assessment.get("reject"):
        return None

    from services.sales_statement_extractor import empty_result

    reasons = list(assessment.get("reasons") or ["unreadable"])
    result = empty_result(filename, "image")
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["extraction_failed"] = True
    extra["extraction_failed_reason"] = "poor_photo_quality"
    extra["extraction_failed_reasons"] = ["poor_photo_quality"] + reasons
    extra["extraction_failed_message"] = _retry_message(reasons)
    extra["photo_quality_rejected"] = True
    extra["photo_quality"] = assessment.get("metrics") or {}
    extra["extraction_method"] = "photo_quality_gate"
    extra["final_selected_extraction"] = "failed"
    extra["extraction_quality"] = {
        "quality": "poor",
        "should_fallback": False,
        "reasons": extra["extraction_failed_reasons"],
    }
    logger.info(
        "STOCK_PHOTO_QUALITY file=%s status=rejected reasons=%s",
        filename,
        ",".join(extra["extraction_failed_reasons"]),
    )
    return result


def photo_quality_client_detail(
    extra: Optional[Dict[str, Any]],
    filename: str,
) -> Optional[Dict[str, Any]]:
    """422 body for the existing Laravel error UI. None if not a photo reject."""
    bag = extra or {}
    if not bag.get("photo_quality_rejected"):
        return None
    reasons = list(bag.get("extraction_failed_reasons") or ["poor_photo_quality"])
    return {
        "error": "extraction_failed",
        "message": bag.get("extraction_failed_message") or DEFAULT_RETRY_MESSAGE,
        "reasons": reasons,
        "source_file": filename,
    }
