"""Route general ``image_to_string`` calls through the OCR orchestrator.

Layout-specific ``image_to_data`` bridges to PaddleOCR when classic PaddleOCR is primary,
preserving word geometry and confidence while avoiding duplicate inference.
Digit-whitelist crops and other engine configurations remain on Tesseract.
Those calls do not invoke the Gemini document fallback.
"""

from __future__ import annotations

from collections import OrderedDict
import hashlib
import io
import logging
import re
import threading
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

_WHITELIST_RE = re.compile(r"tessedit_char_whitelist\s*=", re.I)

_CACHE_LOCK = threading.Lock()
_PADDLE_RESULT_CACHE: OrderedDict[str, Any] = OrderedDict()
_CACHE_MAX_ENTRIES = 64


def clear_paddle_bridge_cache() -> None:
    """Testing helper to clear cached OCR results."""
    with _CACHE_LOCK:
        _PADDLE_RESULT_CACHE.clear()


def _get_cached_paddle_ocr(image_bytes: bytes) -> Any:
    key = hashlib.sha1(image_bytes).hexdigest()
    with _CACHE_LOCK:
        if key in _PADDLE_RESULT_CACHE:
            _PADDLE_RESULT_CACHE.move_to_end(key)
            return _PADDLE_RESULT_CACHE[key]

    try:
        from services.ocr.paddle_engine import PaddleEngine

        engine = PaddleEngine()
        result = engine.recognize(image_bytes)
    except Exception as exc:
        logger.warning("PaddleEngine.recognize failed in pytesseract_bridge: %s", exc)
        result = None

    with _CACHE_LOCK:
        _PADDLE_RESULT_CACHE[key] = result
        if len(_PADDLE_RESULT_CACHE) > _CACHE_MAX_ENTRIES:
            _PADDLE_RESULT_CACHE.popitem(last=False)

    return result


def _empty_tesseract_dict() -> Dict[str, List[Any]]:
    return {
        "level": [],
        "page_num": [],
        "block_num": [],
        "par_num": [],
        "line_num": [],
        "word_num": [],
        "left": [],
        "top": [],
        "width": [],
        "height": [],
        "conf": [],
        "text": [],
    }


def _words_to_tesseract_dict(words: Sequence[Any]) -> Dict[str, List[Any]]:
    texts: List[str] = []
    lefts: List[int] = []
    tops: List[int] = []
    widths: List[int] = []
    heights: List[int] = []
    confs: List[Any] = []
    levels: List[int] = []
    page_nums: List[int] = []
    block_nums: List[int] = []
    par_nums: List[int] = []
    line_nums: List[int] = []
    word_nums: List[int] = []

    for idx, w in enumerate(words or [], start=1):
        txt = str(getattr(w, "text", "") or "").strip()
        bbox = getattr(w, "bbox", None)
        if bbox is not None and len(bbox) >= 4:
            x0, y0, x1, y1 = bbox[:4]
            left = int(round(float(x0)))
            top = int(round(float(y0)))
            width = max(0, int(round(float(x1) - float(x0))))
            height = max(0, int(round(float(y1) - float(y0))))
        else:
            left, top, width, height = 0, 0, 0, 0

        raw_conf = getattr(w, "confidence", None)
        if raw_conf is not None:
            try:
                # Paddle confidence is 0.0 to 1.0; pytesseract uses integer 0 to 100.
                conf_val = int(round(float(raw_conf) * 100))
            except (TypeError, ValueError):
                conf_val = -1
        else:
            conf_val = -1

        texts.append(txt)
        lefts.append(left)
        tops.append(top)
        widths.append(width)
        heights.append(height)
        confs.append(conf_val)
        levels.append(5)
        page_nums.append(getattr(w, "page", 1) or 1)
        block_nums.append(1)
        par_nums.append(1)
        line_nums.append(1)
        word_nums.append(idx)

    return {
        "level": levels,
        "page_num": page_nums,
        "block_num": block_nums,
        "par_num": par_nums,
        "line_num": line_nums,
        "word_num": word_nums,
        "left": lefts,
        "top": tops,
        "width": widths,
        "height": heights,
        "conf": confs,
        "text": texts,
    }


def _format_output(data_dict: Dict[str, List[Any]], output_type: Any) -> Any:
    out_str = str(output_type or "").lower()
    if output_type is None or "dict" in out_str:
        return data_dict
    if "string" in out_str:
        tsv_lines = [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
        ]
        count = len(data_dict.get("text", []))
        for i in range(count):
            tsv_lines.append(
                f"{data_dict['level'][i]}\t{data_dict['page_num'][i]}\t"
                f"{data_dict['block_num'][i]}\t{data_dict['par_num'][i]}\t"
                f"{data_dict['line_num'][i]}\t{data_dict['word_num'][i]}\t"
                f"{data_dict['left'][i]}\t{data_dict['top'][i]}\t"
                f"{data_dict['width'][i]}\t{data_dict['height'][i]}\t"
                f"{data_dict['conf'][i]}\t{data_dict['text'][i]}"
            )
        return "\n".join(tsv_lines) + "\n"
    if "dataframe" in out_str:
        try:
            import pandas as pd

            return pd.DataFrame(data_dict)
        except Exception:
            return data_dict
    if "bytes" in out_str:
        tsv = _format_output(data_dict, "string")
        return tsv.encode("utf-8")
    return data_dict


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
            from services.ocr.runtime import (
                ocr_pil_image_text,
                primary_is_classic_paddle,
                primary_is_paddle_vl,
            )
        except Exception:
            return self._inner.image_to_string(image, config=config, **kwargs) or ""

        if not (primary_is_paddle_vl() or primary_is_classic_paddle()):
            return self._inner.image_to_string(image, config=config, **kwargs) or ""

        if primary_is_paddle_vl():
            from services.ocr.paddle_vl_engine import paddle_init_blocked

            if paddle_init_blocked():
                return ""

        pil = _as_pil(image)
        if pil is None:
            return self._inner.image_to_string(image, config=config, **kwargs) or ""

        if primary_is_classic_paddle():
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            img_bytes = buf.getvalue()
            ocr_res = _get_cached_paddle_ocr(img_bytes)
            if ocr_res and getattr(ocr_res, "text", None):
                return ocr_res.text

        text, meta = ocr_pil_image_text(pil, use_fallback=False)
        logger.debug(
            "primary_ocr_bridge engine=%s status=%s",
            meta.get("engine"),
            meta.get("status"),
        )
        # Do not silently fall through to Tesseract when VL is primary.
        return text or ""

    def image_to_data(
        self,
        image: Any,
        *args: Any,
        config: str = "",
        output_type: Any = None,
        **kwargs: Any,
    ) -> Any:
        cfg = str(config or "")
        # Digit/character whitelist crops are intentional Tesseract geometry reads.
        if _WHITELIST_RE.search(cfg):
            return self._inner.image_to_data(
                image, *args, config=config, output_type=output_type, **kwargs
            )

        try:
            from services.ocr.runtime import primary_is_classic_paddle
        except Exception:
            return self._inner.image_to_data(
                image, *args, config=config, output_type=output_type, **kwargs
            )

        if not primary_is_classic_paddle():
            return self._inner.image_to_data(
                image, *args, config=config, output_type=output_type, **kwargs
            )

        # Infer output_type if passed positionally (tesseract signature: lang, config, nice, output_type)
        resolved_output_type = output_type
        if resolved_output_type is None and len(args) >= 4:
            resolved_output_type = args[3]

        pil = _as_pil(image)
        if pil is None:
            return _format_output(_empty_tesseract_dict(), resolved_output_type)

        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        ocr_res = _get_cached_paddle_ocr(buf.getvalue())
        words = getattr(ocr_res, "words", None) if ocr_res else []
        data_dict = _words_to_tesseract_dict(words or [])
        return _format_output(data_dict, resolved_output_type)


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
