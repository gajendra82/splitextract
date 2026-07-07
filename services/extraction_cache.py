"""Configurable extraction result cache keyed by file hash."""

import hashlib
import json
import os
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from services.config import EXTRACTION_CONFIG


class ExtractionCacheBackend(ABC):
    @abstractmethod
    def get(self, cache_key: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def set(self, cache_key: str, value: Dict[str, Any]) -> None:
        raise NotImplementedError


class FileExtractionCache(ExtractionCacheBackend):
    def __init__(self, cache_dir: str, ttl_seconds: int = 0):
        self.cache_dir = cache_dir
        self.ttl_seconds = ttl_seconds
        os.makedirs(self.cache_dir, exist_ok=True)

    def _path(self, cache_key: str) -> str:
        return os.path.join(self.cache_dir, f"{cache_key}.json")

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
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, cache_key: str, value: Dict[str, Any]) -> None:
        path = self._path(cache_key)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False)
        except OSError:
            pass


class MemoryExtractionCache(ExtractionCacheBackend):
    def __init__(self, ttl_seconds: int = 0):
        self.ttl_seconds = ttl_seconds
        self._store: Dict[str, Dict[str, Any]] = {}

    def get(self, cache_key: str) -> Optional[Dict[str, Any]]:
        entry = self._store.get(cache_key)
        if not entry:
            return None
        if self.ttl_seconds > 0 and (time.time() - entry["ts"]) > self.ttl_seconds:
            self._store.pop(cache_key, None)
            return None
        return entry["value"]

    def set(self, cache_key: str, value: Dict[str, Any]) -> None:
        self._store[cache_key] = {"ts": time.time(), "value": value}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_cache_key(file_hash: str, page_num: int = 0, suffix: str = "") -> str:
    base = f"{file_hash}:page:{page_num}"
    if suffix:
        base = f"{base}:{suffix}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


class ExtractionCache:
    """Facade for extraction cache with configurable backend."""

    def __init__(self, config=EXTRACTION_CONFIG):
        self.config = config
        self._backend = self._create_backend()

    def _create_backend(self) -> ExtractionCacheBackend:
        if self.config.cache_backend == "memory":
            return MemoryExtractionCache(self.config.cache_ttl_seconds)
        return FileExtractionCache(self.config.cache_dir, self.config.cache_ttl_seconds)

    def get(self, cache_key: str) -> Optional[Dict[str, Any]]:
        if not self.config.enable_extraction_cache:
            return None
        return self._backend.get(cache_key)

    def set(self, cache_key: str, value: Dict[str, Any]) -> None:
        if not self.config.enable_extraction_cache:
            return
        self._backend.set(cache_key, value)
