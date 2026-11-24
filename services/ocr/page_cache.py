"""Durable page-level OCR result cache.

Caches only usable non-empty OCR output. Empty, timeout, and error results are
never treated as successful cache entries — callers must re-run inference.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from services.ocr.base import OCRResult, OCRWord
from services.ocr.orchestrator import OCRBundle

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_MEMORY: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_CACHE_VERSION = "v1"


def page_cache_enabled() -> bool:
    raw = os.getenv("OCR_PAGE_CACHE", "true")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _memory_max() -> int:
    try:
        return max(8, min(512, int(os.getenv("OCR_PAGE_CACHE_MEMORY_MAX", "64") or "64")))
    except (TypeError, ValueError):
        return 64


def _disk_dir() -> Optional[Path]:
    raw = (os.getenv("OCR_PAGE_CACHE_DIR") or "").strip()
    if not raw:
        # Default under repo phase4 so benches can reuse across process restarts.
        root = Path(__file__).resolve().parents[2]
        return root / "phase4" / "ocr_page_cache"
    if raw.lower() in {"off", "none", "0", "false"}:
        return None
    return Path(raw)


def page_cache_key(
    image_bytes: bytes,
    *,
    engine: str,
    page: int = 1,
) -> str:
    digest = hashlib.sha256(image_bytes).hexdigest()
    eng = (engine or "paddleocr-vl").strip().lower() or "paddleocr-vl"
    return f"{_CACHE_VERSION}:{eng}:p{int(page)}:{digest}"


def _usable_bundle(bundle: OCRBundle) -> bool:
    primary = getattr(bundle, "primary", None)
    if primary is None:
        return False
    text = str(getattr(primary, "text", "") or "").strip()
    if not text:
        return False
    if getattr(primary, "timed_out", False):
        return False
    meta = getattr(primary, "metadata", None) or {}
    status = str(meta.get("status") or "").casefold()
    if status in {"timeout", "error", "ocr_error"}:
        return False
    if meta.get("available") is False:
        return False
    # Short scraps are incomplete — do not cache as success.
    if len(text) < 20:
        return False
    return True


def _bundle_to_payload(bundle: OCRBundle) -> Dict[str, Any]:
    return {
        "primary": bundle.primary.to_dict() if bundle.primary else None,
        "secondary": bundle.secondary.to_dict() if bundle.secondary else None,
        "disagreement": bool(bundle.disagreement),
        "engines_used": list(bundle.engines_used),
        "unverified_fallback": bool(bundle.unverified_fallback),
        "metadata": dict(bundle.metadata or {}),
        "cached_at": time.time(),
    }


def _result_from_dict(data: Optional[Dict[str, Any]]) -> Optional[OCRResult]:
    if not isinstance(data, dict):
        return None
    words = []
    for raw in data.get("words") or []:
        if not isinstance(raw, dict):
            continue
        bbox = raw.get("bbox")
        words.append(
            OCRWord(
                text=str(raw.get("text") or ""),
                bbox=tuple(bbox) if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else None,
                confidence=raw.get("confidence"),
                page=int(raw.get("page") or 1),
            )
        )
    return OCRResult(
        text=str(data.get("text") or ""),
        words=words,
        confidence=data.get("confidence"),
        page=int(data.get("page") or 1),
        engine=str(data.get("engine") or ""),
        metadata=dict(data.get("metadata") or {}),
        timed_out=bool(data.get("timed_out")),
        timeout_s=data.get("timeout_s"),
        retry_count=int(data.get("retry_count") or 0),
    )


def _bundle_from_payload(payload: Dict[str, Any]) -> OCRBundle:
    meta = dict(payload.get("metadata") or {})
    meta["ocr_cache_hit"] = True
    return OCRBundle(
        primary=_result_from_dict(payload.get("primary")),
        secondary=_result_from_dict(payload.get("secondary")),
        disagreement=bool(payload.get("disagreement")),
        engines_used=list(payload.get("engines_used") or []),
        unverified_fallback=bool(payload.get("unverified_fallback")),
        metadata=meta,
    )


def get_cached_page(key: str) -> Optional[OCRBundle]:
    if not page_cache_enabled() or not key:
        return None
    with _LOCK:
        hit = _MEMORY.get(key)
        if hit is not None:
            _MEMORY.move_to_end(key)
            bundle = _bundle_from_payload(hit)
            if _usable_bundle(bundle):
                logger.info("ocr_page_cache_hit layer=memory key=%s", key[:24])
                return bundle
            # Stale unusable entry — drop.
            _MEMORY.pop(key, None)

    disk = _disk_dir()
    if disk is None:
        return None
    path = disk / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.info("ocr_page_cache_disk_read_failed error=%s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    bundle = _bundle_from_payload(payload)
    if not _usable_bundle(bundle):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        return None
    with _LOCK:
        _MEMORY[key] = payload
        _MEMORY.move_to_end(key)
        while len(_MEMORY) > _memory_max():
            _MEMORY.popitem(last=False)
    logger.info("ocr_page_cache_hit layer=disk key=%s", key[:24])
    return bundle


def put_cached_page(key: str, bundle: OCRBundle) -> bool:
    """Store usable OCR only. Returns False when skipped (empty/error/timeout)."""
    if not page_cache_enabled() or not key:
        return False
    if not _usable_bundle(bundle):
        logger.info("ocr_page_cache_skip reason=unusable key=%s", key[:24])
        return False
    payload = _bundle_to_payload(bundle)
    with _LOCK:
        _MEMORY[key] = payload
        _MEMORY.move_to_end(key)
        while len(_MEMORY) > _memory_max():
            _MEMORY.popitem(last=False)

    disk = _disk_dir()
    if disk is not None:
        try:
            disk.mkdir(parents=True, exist_ok=True)
            path = disk / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.info("ocr_page_cache_disk_write_failed error=%s", exc)
    logger.info("ocr_page_cache_store key=%s text_len=%s", key[:24], len(bundle.primary.text or ""))
    return True


def clear_page_cache(*, disk: bool = False) -> None:
    """Test helper. Clears memory cache; optionally wipes disk dir contents."""
    with _LOCK:
        _MEMORY.clear()
    if not disk:
        return
    root = _disk_dir()
    if root is None or not root.is_dir():
        return
    for path in root.glob("*.json"):
        try:
            path.unlink()
        except Exception:
            pass


def cache_stats() -> Dict[str, Any]:
    with _LOCK:
        return {
            "enabled": page_cache_enabled(),
            "memory_entries": len(_MEMORY),
            "memory_max": _memory_max(),
            "disk_dir": str(_disk_dir()) if _disk_dir() else None,
        }
