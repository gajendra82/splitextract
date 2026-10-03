"""Stock document OCR provider abstraction (benchmark / future hybrid only).

Not wired into POST /extract-sales-statement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# bbox: [x1, y1, x2, y2] in page pixel coordinates when known.
BBox = Tuple[float, float, float, float]


@dataclass
class OCRCell:
    text: str
    bbox: Optional[BBox] = None
    confidence: Optional[float] = None
    page: int = 1
    block_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OCRBlock:
    text: str
    bbox: Optional[BBox] = None
    confidence: Optional[float] = None
    page: int = 1
    block_type: str = "text"
    table_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class OCRTable:
    table_id: str
    format: str  # html | markdown
    content: str
    page: int = 1
    word_confidence_scores: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OCRPage:
    page_number: int
    width: Optional[float] = None
    height: Optional[float] = None
    dpi: Optional[float] = None
    markdown: str = ""
    blocks: List[OCRBlock] = field(default_factory=list)
    tables: List[OCRTable] = field(default_factory=list)
    cells: List[OCRCell] = field(default_factory=list)
    raw_text: str = ""
    confidence: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_number": self.page_number,
            "width": self.width,
            "height": self.height,
            "dpi": self.dpi,
            "markdown": self.markdown,
            "blocks": [b.to_dict() for b in self.blocks],
            "tables": [t.to_dict() for t in self.tables],
            "cells": [c.to_dict() for c in self.cells],
            "raw_text": self.raw_text,
            "confidence": self.confidence,
            "extra": self.extra,
        }


@dataclass
class OCRDocumentResult:
    provider: str
    pages: List[OCRPage] = field(default_factory=list)
    latency_ms: float = 0.0
    model: Optional[str] = None
    error: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    request_metadata: Dict[str, Any] = field(default_factory=dict)
    raw_response: Any = None
    image_width: Optional[int] = None
    image_height: Optional[int] = None

    def to_dict(self, *, include_raw: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "provider": self.provider,
            "pages": [p.to_dict() for p in self.pages],
            "latency_ms": self.latency_ms,
            "model": self.model,
            "error": self.error,
            "usage": self.usage,
            "request_metadata": self.request_metadata,
            "image_width": self.image_width,
            "image_height": self.image_height,
        }
        if include_raw:
            out["raw_response"] = self.raw_response
        return out


class StockDocumentOCRProvider(ABC):
    """Coordinate-preserving document OCR for stock-statement benchmarks."""

    name: str = "base"

    @abstractmethod
    def extract_document(
        self,
        file_bytes: bytes,
        *,
        filename: str = "document",
        mime_type: Optional[str] = None,
    ) -> OCRDocumentResult:
        ...

    def extract_page(
        self,
        file_bytes: bytes,
        *,
        page_index: int = 0,
        filename: str = "document",
        mime_type: Optional[str] = None,
    ) -> OCRDocumentResult:
        """Default: extract full document then keep one page."""
        result = self.extract_document(
            file_bytes, filename=filename, mime_type=mime_type
        )
        if result.error or not result.pages:
            return result
        kept = [p for p in result.pages if int(p.page_number) == int(page_index) + 1]
        if not kept and 0 <= page_index < len(result.pages):
            kept = [result.pages[page_index]]
        result.pages = kept
        return result

    def extract_regions(
        self,
        file_bytes: bytes,
        regions: Sequence[BBox],
        *,
        filename: str = "document",
        mime_type: Optional[str] = None,
    ) -> OCRDocumentResult:
        """Optional crop-then-OCR; default providers may not support."""
        raise NotImplementedError(
            f"{self.name} does not implement extract_regions in this benchmark"
        )


def guess_mime(filename: str, file_bytes: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf") or file_bytes[:4] == b"%PDF":
        return "application/pdf"
    if name.endswith(".jpg") or name.endswith(".jpeg") or file_bytes[:2] == b"\xff\xd8":
        return "image/jpeg"
    if name.endswith(".png") or file_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if name.endswith(".webp"):
        return "image/webp"
    return "application/octet-stream"
