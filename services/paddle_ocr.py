"""
Optional PaddleOCR Tier-3 scan OCR (feature-flagged, fail-soft).

Default OFF. When enabled and usable, returns page text + mean confidence.
Any import/init/runtime failure returns (None, 0.0) so callers fall back to Tesseract.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Config (read at call time so .env changes apply without code edits)
# ---------------------------------------------------------------------------

def is_paddle_ocr_enabled() -> bool:
    """Master feature flag — must be explicitly enabled."""
    return _env_bool("PADDLE_OCR_ENABLED", False)


def paddle_ocr_lang() -> str:
    return (os.getenv("PADDLE_OCR_LANG") or "en").strip() or "en"


def paddle_ocr_use_gpu() -> bool:
    return _env_bool("PADDLE_OCR_USE_GPU", False)


def paddle_ocr_max_concurrency() -> int:
    return max(1, min(_env_int("PADDLE_OCR_MAX_CONCURRENCY", 2), 4))


def paddle_ocr_min_chars() -> int:
    return max(1, _env_int("PADDLE_OCR_MIN_CHARS", 100))


def paddle_ocr_min_confidence() -> float:
    """Accept threshold on 0–100 scale (aligned with Tesseract logging)."""
    return _env_float("PADDLE_OCR_MIN_CONFIDENCE", 50.0)


def paddle_ocr_render_scale() -> float:
    return max(1.0, min(_env_float("PADDLE_OCR_RENDER_SCALE", 2.0), 3.0))


# ---------------------------------------------------------------------------
# Lazy engine + concurrency
# ---------------------------------------------------------------------------

_engine_lock = threading.Lock()
_engine: Any = None
_engine_failed = False
_engine_error: Optional[str] = None

_slot_lock = threading.Lock()
_slot_semaphore: Optional[threading.Semaphore] = None
_slot_limit: int = 0
_active = 0
_waiting = 0


def reset_paddle_ocr_engine_for_tests() -> None:
    """Test helper: clear cached engine / failure state."""
    global _engine, _engine_failed, _engine_error, _slot_semaphore, _slot_limit
    with _engine_lock:
        _engine = None
        _engine_failed = False
        _engine_error = None
        _slot_semaphore = None
        _slot_limit = 0


def paddle_ocr_slot_probe() -> dict:
    with _slot_lock:
        return {
            "paddleocr_slot_active": _active,
            "paddleocr_slot_waiting": _waiting,
            "paddleocr_enabled": is_paddle_ocr_enabled(),
            "paddleocr_engine_ready": _engine is not None and not _engine_failed,
            "paddleocr_engine_failed": _engine_failed,
        }


@contextmanager
def _paddle_ocr_slot(label: str = "paddle_ocr"):
    global _active, _waiting, _slot_semaphore, _slot_limit
    limit = paddle_ocr_max_concurrency()
    with _slot_lock:
        if _slot_semaphore is None or _slot_limit != limit:
            _slot_semaphore = threading.Semaphore(limit)
            _slot_limit = limit
        sem = _slot_semaphore
        _waiting += 1
    try:
        sem.acquire()
        with _slot_lock:
            _waiting = max(0, _waiting - 1)
            _active += 1
        logger.debug("PaddleOCR slot acquired label=%s active=%s", label, _active)
        try:
            yield
        finally:
            with _slot_lock:
                _active = max(0, _active - 1)
            sem.release()
    except Exception:
        with _slot_lock:
            _waiting = max(0, _waiting - 1)
        raise


def _build_engine() -> Any:
    """Create a PaddleOCR instance. Supports paddleocr 2.x and 3.x constructors."""
    # Windows: paddle DLLs conflict with torch if paddle loads first.
    # albumentations (pulled by paddleocr 2.x) imports torch — preload torch first.
    try:
        import torch  # noqa: F401
    except Exception:
        pass

    # Paddle 3.x + classic PP-OCR inference: OneDNN fused_conv2d often breaks on Windows.
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    os.environ.setdefault("FLAGS_onednn", "0")
    try:
        import paddle

        paddle.set_flags({"FLAGS_use_mkldnn": False})
    except Exception:
        pass

    from paddleocr import PaddleOCR  # type: ignore

    lang = paddle_ocr_lang()
    use_gpu = paddle_ocr_use_gpu()
    # Prefer quiet, CPU-safe defaults. Newer APIs drop some kwargs.
    attempts = (
        {
            "use_angle_cls": True,
            "lang": lang,
            "use_gpu": use_gpu,
            "show_log": False,
            "enable_mkldnn": False,
        },
        {"use_angle_cls": True, "lang": lang, "use_gpu": use_gpu, "show_log": False},
        {"use_angle_cls": True, "lang": lang, "show_log": False, "enable_mkldnn": False},
        {"use_angle_cls": True, "lang": lang, "show_log": False},
        {"lang": lang, "use_gpu": use_gpu},
        {"lang": lang},
        {},
    )
    last_err: Optional[BaseException] = None
    for kwargs in attempts:
        try:
            engine = PaddleOCR(**kwargs)
            logger.info(
                "PaddleOCR engine initialized kwargs=%s",
                {k: v for k, v in kwargs.items() if k != "show_log"},
            )
            return engine
        except TypeError as exc:
            last_err = exc
            continue
        except Exception as exc:
            last_err = exc
            break
    raise RuntimeError(f"PaddleOCR init failed: {last_err}")


def get_paddle_ocr_engine() -> Optional[Any]:
    """Lazy singleton. Returns None if disabled, missing, or previously failed."""
    global _engine, _engine_failed, _engine_error

    if not is_paddle_ocr_enabled():
        return None
    if _engine_failed:
        return None

    with _engine_lock:
        if _engine_failed:
            return None
        if _engine is not None:
            return _engine
        try:
            _engine = _build_engine()
            return _engine
        except Exception as exc:
            _engine_failed = True
            _engine_error = str(exc)
            logger.warning(
                "PaddleOCR unavailable (fail-soft → Tesseract): %s",
                exc,
            )
            return None


def is_paddle_ocr_available() -> bool:
    """True when flag is on and engine can be obtained (or already cached)."""
    if not is_paddle_ocr_enabled():
        return False
    return get_paddle_ocr_engine() is not None


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def _is_ocr_line(obj: Any) -> bool:
    """True for classic PaddleOCR line: [box, (text, score)] or [box, [text, score]]."""
    if not isinstance(obj, (list, tuple)) or len(obj) < 2:
        return False
    payload = obj[1]
    if isinstance(payload, (list, tuple)) and len(payload) >= 2:
        return isinstance(payload[0], str) or payload[0] is None
    if isinstance(payload, dict):
        return True
    return False


def _parse_ocr_result(raw: Any) -> Tuple[str, float]:
    """
    Normalize PaddleOCR 2.x / 3.x outputs to (text, avg_confidence_0_100).
    """
    lines: list[str] = []
    confs: list[float] = []

    if raw is None:
        return "", 0.0

    # paddleocr 3.x predict() often returns list of dict-like results
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        for item in raw:
            texts = item.get("rec_texts") or item.get("texts") or []
            scores = item.get("rec_scores") or item.get("scores") or []
            if isinstance(texts, str):
                texts = [texts]
            for i, t in enumerate(texts):
                if t and str(t).strip():
                    lines.append(str(t).strip())
                    if i < len(scores):
                        try:
                            s = float(scores[i])
                            confs.append(s * 100.0 if s <= 1.0 else s)
                        except (TypeError, ValueError):
                            pass
        text = "\n".join(lines)
        avg = sum(confs) / len(confs) if confs else 0.0
        return text, float(avg)

    # Flatten classic layouts:
    #  - [[ [box,(t,c)], ... ]]           (pages)
    #  - [ [box,(t,c)], ... ]             (single page)
    #  - [[[ [box,(t,c)], ... ]]]         (extra nest)
    def _walk(node: Any) -> None:
        if node is None:
            return
        if _is_ocr_line(node):
            try:
                payload = node[1]
                if isinstance(payload, dict):
                    t = payload.get("text") or payload.get("transcription") or ""
                    c = payload.get("score") or payload.get("confidence") or 0.0
                else:
                    t, c = payload[0], payload[1]
                if t and str(t).strip():
                    lines.append(str(t).strip())
                    try:
                        s = float(c)
                        confs.append(s * 100.0 if s <= 1.0 else s)
                    except (TypeError, ValueError):
                        pass
            except Exception:
                return
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                _walk(child)

    _walk(raw)
    text = "\n".join(lines)
    avg = sum(confs) / len(confs) if confs else 0.0
    return text, float(avg)


def _run_engine_on_image(engine: Any, img_bgr_or_path: Any) -> Any:
    """Call ocr()/predict() with API variance."""
    errors: list[str] = []
    # Prefer classic .ocr
    if hasattr(engine, "ocr"):
        try:
            return engine.ocr(img_bgr_or_path, cls=True)
        except TypeError as exc:
            errors.append(f"ocr(cls=True) TypeError: {exc}")
            try:
                return engine.ocr(img_bgr_or_path)
            except Exception as exc2:
                errors.append(f"ocr(): {exc2}")
        except Exception as exc:
            errors.append(f"ocr(cls=True): {exc}")
            try:
                return engine.ocr(img_bgr_or_path)
            except Exception as exc2:
                errors.append(f"ocr(): {exc2}")
    if hasattr(engine, "predict"):
        try:
            return engine.predict(img_bgr_or_path)
        except Exception as exc:
            errors.append(f"predict(): {exc}")
    raise RuntimeError(
        "PaddleOCR engine has no usable ocr/predict method; "
        + " | ".join(errors[-3:])
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_text_with_paddleocr(
    page,
    page_num: Optional[int] = None,
) -> Tuple[Optional[str], float]:
    """
    Render a PyMuPDF page and run PaddleOCR.

    Returns:
        (text, confidence_0_100) on success meeting min thresholds,
        (None, confidence_or_0) on low quality / any failure (fail-soft).
    """
    if not is_paddle_ocr_enabled():
        return None, 0.0

    engine = get_paddle_ocr_engine()
    if engine is None:
        return None, 0.0

    try:
        import io

        import fitz  # PyMuPDF
        import numpy as np
        from PIL import Image

        ocr_start = time.time()
        scale = paddle_ocr_render_scale()
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
        img_bytes = pix.tobytes("png")
        pix = None

        img = Image.open(io.BytesIO(img_bytes))
        img_arr = np.array(img.convert("RGB"))
        img.close()
        del img_bytes

        # Paddle typically expects BGR ndarray or path; RGB often works too.
        # Convert to BGR for OpenCV-style engines when cv2 is present.
        try:
            import cv2

            img_cv = cv2.cvtColor(img_arr, cv2.COLOR_RGB2BGR)
            del img_arr
            run_img = img_cv
        except Exception:
            run_img = img_arr

        with _paddle_ocr_slot("page_full_ocr"):
            raw = _run_engine_on_image(engine, run_img)

        del run_img
        text, avg_confidence = _parse_ocr_result(raw)
        elapsed = time.time() - ocr_start
        char_count = len((text or "").strip())
        min_chars = paddle_ocr_min_chars()
        min_conf = paddle_ocr_min_confidence()

        logger.info(
            "    PaddleOCR: page=%s chars=%s conf=%.1f%% time=%.1fs",
            (page_num + 1) if isinstance(page_num, int) else page_num,
            char_count,
            avg_confidence,
            elapsed,
        )

        if char_count >= min_chars and (
            avg_confidence >= min_conf or avg_confidence <= 0.0 and char_count >= min_chars * 2
        ):
            # avg_confidence==0 can happen if API omits scores but text is rich
            if avg_confidence <= 0.0 and char_count >= min_chars:
                avg_confidence = 70.0  # nominal when scores missing but text ok
            logger.info(
                "    ✅ PaddleOCR accepted: %s chars (conf: %.1f%%)",
                char_count,
                avg_confidence,
            )
            return text, float(avg_confidence)

        logger.info(
            "    ⚠️ PaddleOCR low quality: %s chars, %.1f%% conf — falling back to Tesseract",
            char_count,
            avg_confidence,
        )
        return None, float(avg_confidence)

    except Exception as exc:
        logger.warning(
            "    ⚠️ PaddleOCR failed (fail-soft → Tesseract): %s",
            exc,
        )
        return None, 0.0


def extract_text_with_paddleocr_relaxed(
    page,
    page_num: Optional[int] = None,
) -> Tuple[Optional[str], float]:
    """
    Like extract_text_with_paddleocr but does not reject low-confidence output
    when any non-trivial text is returned (mirrors Tesseract relaxed path).
    """
    if not is_paddle_ocr_enabled():
        return None, 0.0

    engine = get_paddle_ocr_engine()
    if engine is None:
        return None, 0.0

    try:
        import io

        import fitz
        import numpy as np
        from PIL import Image

        ocr_start = time.time()
        scale = paddle_ocr_render_scale()
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
        img_bytes = pix.tobytes("png")
        pix = None

        img = Image.open(io.BytesIO(img_bytes))
        img_arr = np.array(img.convert("RGB"))
        img.close()
        del img_bytes

        try:
            import cv2

            run_img = cv2.cvtColor(img_arr, cv2.COLOR_RGB2BGR)
            del img_arr
        except Exception:
            run_img = img_arr

        with _paddle_ocr_slot("page_full_ocr_relaxed"):
            raw = _run_engine_on_image(engine, run_img)
        del run_img

        text, avg_confidence = _parse_ocr_result(raw)
        elapsed = time.time() - ocr_start
        char_count = len((text or "").strip())

        if avg_confidence <= 0.0 and char_count >= 50:
            avg_confidence = 60.0

        logger.info(
            "    ✅ PaddleOCR(relaxed): page=%s chars=%s conf=%.1f%% time=%.1fs",
            (page_num + 1) if isinstance(page_num, int) else page_num,
            char_count,
            avg_confidence,
            elapsed,
        )
        if char_count < 20:
            return None, float(avg_confidence)
        return text or "", float(avg_confidence)

    except Exception as exc:
        logger.warning(
            "    ⚠️ PaddleOCR(relaxed) failed (fail-soft → Tesseract): %s",
            exc,
        )
        return None, 0.0
