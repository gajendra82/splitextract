"""Common OCR interface. Engines are evidence producers only."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class OCRWord:
    text: str
    bbox: Optional[Tuple[float, float, float, float]] = None
    confidence: Optional[float] = None
    page: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "bbox": list(self.bbox) if self.bbox else None,
            "confidence": self.confidence,
            "page": self.page,
        }


@dataclass
class OCRResult:
    text: str
    words: List[OCRWord] = field(default_factory=list)
    confidence: Optional[float] = None
    page: int = 1
    engine: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Resilience fields (optional; engines may leave defaults).
    timed_out: bool = False
    timeout_s: Optional[float] = None
    retry_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "words": [word.to_dict() for word in self.words],
            "confidence": self.confidence,
            "page": self.page,
            "engine": self.engine,
            "metadata": dict(self.metadata),
            "timed_out": self.timed_out,
            "timeout_s": self.timeout_s,
            "retry_count": self.retry_count,
        }


class OCREngine(ABC):
    """Adapter around one OCR implementation."""

    name: str = "ocr"

    @abstractmethod
    def available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def recognize(
        self,
        image_bytes: bytes,
        *,
        page: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OCRResult:
        raise NotImplementedError
