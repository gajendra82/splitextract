"""Page preprocessing before PaddleOCR-VL (orientation, DPI, contrast, stamps)."""

from __future__ import annotations

import io
import logging
import os
from typing import Any, Dict, List, Optional, Tuple, Union

from services.ocr.cv2_compat import HAS_CV2, cv2

logger = logging.getLogger(__name__)

_MIN_WIDTH_PX = 2550  # ≈300 DPI A4 — used only when upscaling thin scans
_OCR_TARGET_WIDTH = 1240  # ~150 DPI for faster CPU OCR


def preprocess_enabled() -> bool:
    return os.getenv("OCR_PAGE_PREPROCESS", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def prepare_for_ocr(
    page_image: Union[bytes, Any],
    target_width: int = _OCR_TARGET_WIDTH,
) -> Union[bytes, Any]:
    """Downscale to ~150 DPI if larger (faster OCR on CPU).

    Accepts PNG/JPEG ``bytes`` or a BGR ``ndarray``. Returns the same type.
    """
    if not HAS_CV2 or cv2 is None:
        return page_image

    import numpy as np  # type: ignore

    as_bytes = isinstance(page_image, (bytes, bytearray))
    if as_bytes:
        arr = np.frombuffer(page_image, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return page_image
    else:
        img = page_image

    h, w = img.shape[:2]
    if w > target_width + 200:
        scale = target_width / float(w)
        new_w, new_h = int(w * scale), int(h * scale)
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
        logger.info(
            "prepare_for_ocr downscaled %sx%s → %sx%s", w, h, new_w, new_h
        )

    if as_bytes:
        ok, encoded = cv2.imencode(".png", img)
        return encoded.tobytes() if ok else page_image
    return img


def preprocess_page_image(
    image_bytes: bytes,
    *,
    page: int = 1,
    suppress_stamps: bool = True,
    adaptive_threshold: bool = False,
) -> Tuple[bytes, Dict[str, Any]]:
    """Apply rotate → resize → CLAHE → stamp suppress → optional threshold.

    Returns ``(processed_bytes, meta)`` where meta lists steps applied.
    On any failure, returns the original bytes with ``meta["error"]``.
    """
    steps: List[str] = []
    meta: Dict[str, Any] = {"page": page, "steps": steps, "enabled": True}
    if not preprocess_enabled():
        meta["enabled"] = False
        return image_bytes, meta

    if not HAS_CV2 or cv2 is None:
        meta["enabled"] = False
        meta["skipped"] = "no_cv2"
        return image_bytes, meta

    try:
        import numpy as np  # type: ignore
        from PIL import Image, ImageOps
    except Exception as exc:
        meta["error"] = f"deps_unavailable:{exc}"
        meta["enabled"] = False
        return image_bytes, meta

    try:
        # EXIF orientation first.
        pil = Image.open(io.BytesIO(image_bytes))
        pil = ImageOps.exif_transpose(pil)
        if pil.mode not in {"RGB", "L"}:
            pil = pil.convert("RGB")
        arr = np.array(pil)
        if arr.ndim == 2:
            bgr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
        else:
            bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        steps.append("exif_transpose")

        bgr, rot_step = _fix_orientation(bgr)
        if rot_step:
            steps.append(rot_step)

        # Upscale only very thin scans (still downscaled later by prepare_for_ocr).
        h, w = bgr.shape[:2]
        if w < 900:
            scale = min(2.0, _MIN_WIDTH_PX / float(w))
            bgr = cv2.resize(
                bgr,
                (int(w * scale), int(h * scale)),
                interpolation=cv2.INTER_CUBIC,
            )
            bgr = cv2.GaussianBlur(bgr, (1, 1), 0)
            steps.append(f"upscale_{scale:.2f}x")

        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        mean_brightness = float(gray.mean())
        meta["mean_brightness"] = round(mean_brightness, 2)
        if mean_brightness < 128 or mean_brightness > 200:
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            gray = clahe.apply(gray)
            bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
            steps.append("clahe")

        if suppress_stamps and _stamp_suppress_enabled():
            bgr, stamped = _suppress_red_stamps(bgr)
            if stamped:
                steps.append("stamp_suppress")
                gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        if adaptive_threshold or _auto_noisy(gray):
            gray = cv2.adaptiveThreshold(
                gray,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                11,
                2,
            )
            bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
            steps.append("adaptive_threshold")

        ok, encoded = cv2.imencode(".png", bgr)
        if not ok:
            meta["error"] = "encode_failed"
            return image_bytes, meta
        out = encoded.tobytes()
        logger.info(
            "page_preprocess page=%s steps=%s width=%s height=%s",
            page,
            steps,
            bgr.shape[1],
            bgr.shape[0],
        )
        return out, meta
    except Exception as exc:
        meta["error"] = str(exc)
        logger.info("page_preprocess page=%s failed: %s", page, exc, exc_info=True)
        return image_bytes, meta


def _stamp_suppress_enabled() -> bool:
    return os.getenv("OCR_STAMP_SUPPRESS", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _fix_orientation(bgr: Any) -> Tuple[Any, Optional[str]]:
    """Detect 90/180/270 rotation via minAreaRect on ink pixels."""
    if not HAS_CV2 or cv2 is None:
        return bgr, None
    try:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        coords = cv2.findNonZero(bw)
        if coords is None or len(coords) < 100:
            return bgr, None
        rect = cv2.minAreaRect(coords)
        angle = float(rect[-1])
        (rw, rh) = rect[1]
        if rw < rh:
            angle = angle + 90
        if abs(angle) < 5 or abs(abs(angle) - 90) < 5 or abs(abs(angle) - 180) < 5:
            # Dense portrait tables often have a landscape ink bounding box
            # (wide grid). Do NOT rotate upright portrait pages onto their side —
            # that destroys column alignment for VL / Gemini-as-OCR.
            return bgr, None
        if 45 < abs(angle) <= 135:
            bgr = cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
            return bgr, "rotate_90_cw"
        if abs(angle) > 135:
            bgr = cv2.rotate(bgr, cv2.ROTATE_180)
            return bgr, "rotate_180"
        return bgr, None
    except Exception:
        return bgr, None


def _suppress_red_stamps(bgr: Any) -> Tuple[Any, bool]:
    if not HAS_CV2 or cv2 is None:
        return bgr, False
    import numpy as np  # type: ignore

    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lower1 = np.array([0, 100, 100])
    upper1 = np.array([10, 255, 255])
    lower2 = np.array([160, 100, 100])
    upper2 = np.array([180, 255, 255])
    mask = cv2.inRange(hsv, lower1, upper1) | cv2.inRange(hsv, lower2, upper2)
    if int(mask.sum()) < 500 * 255:
        return bgr, False
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)
    repaired = cv2.inpaint(bgr, mask, 3, cv2.INPAINT_TELEA)
    return repaired, True


def _auto_noisy(gray: Any) -> bool:
    """Heuristic: high local variance → enable adaptive threshold."""
    try:
        import numpy as np  # type: ignore

        if gray is None or gray.size == 0:
            return False
        sample = gray[::8, ::8]
        return float(np.std(sample)) > 70.0 and bool(
            os.getenv("OCR_ADAPTIVE_THRESHOLD", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
    except Exception:
        return False
