"""GPT text-extraction cache keyed by SHA256 of OCR text."""

import hashlib
import json
import os
import time
from typing import Any, Dict, Optional


def gpt_cache_key(ocr_text: str) -> str:
    """SHA256 hex digest of the OCR text used for GPT (normalized when enabled)."""
    return hashlib.sha256((ocr_text or "").encode("utf-8")).hexdigest()


class GPTResponseCache:
    """File-backed cache for successful GPT JSON extraction results."""

    def __init__(self, cache_dir: str, ttl_seconds: int = 0):
        self.cache_dir = cache_dir
        self.ttl_seconds = ttl_seconds
        os.makedirs(self.cache_dir, exist_ok=True)

    def _path(self, cache_key: str) -> str:
        return os.path.join(self.cache_dir, f"gpt_{cache_key}.json")

    def get(self, cache_key: str) -> Optional[Dict[str, Any]]:
        path = self._path(cache_key)
        if not os.path.exists(path):
            return None
        if self.ttl_seconds > 0:
            age = time.time() - os.path.getmtime(path)
            if age > self.ttl_seconds:
                try:
                    os.remove(path)
                except OSError:
                    pass
                return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
                return payload["result"]
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        return None

    def set(self, cache_key: str, result: Dict[str, Any]) -> None:
        path = self._path(cache_key)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"ts": time.time(), "result": result}, handle, ensure_ascii=False)
        except OSError:
            pass
