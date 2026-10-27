"""Typed OCR evidence contract.

Preserves engine output separately from normalized business values.
Never fabricates confidence or coordinates when the engine omits them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from services.ocr.base import OCRResult, OCRWord


BBoxTuple = Tuple[float, float, float, float]


@dataclass(frozen=True)
class OCRCellEvidence:
    text: str
    page: int = 1
    bbox: Optional[BBoxTuple] = None
    polygon: Optional[Tuple[BBoxTuple, ...]] = None
    confidence: Optional[float] = None
    row_index: Optional[int] = None
    col_index: Optional[int] = None
    header: Optional[str] = None
    reading_order: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "page": self.page,
            "bbox": list(self.bbox) if self.bbox else None,
            "polygon": [list(p) for p in self.polygon] if self.polygon else None,
            "confidence": self.confidence,
            "row_index": self.row_index,
            "col_index": self.col_index,
            "header": self.header,
            "reading_order": self.reading_order,
        }


@dataclass(frozen=True)
class OCRBlockEvidence:
    text: str
    page: int = 1
    bbox: Optional[BBoxTuple] = None
    confidence: Optional[float] = None
    label: Optional[str] = None
    reading_order: Optional[int] = None
    table_index: Optional[int] = None
    html: Optional[str] = None
    cells: Tuple[OCRCellEvidence, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "page": self.page,
            "bbox": list(self.bbox) if self.bbox else None,
            "confidence": self.confidence,
            "label": self.label,
            "reading_order": self.reading_order,
            "table_index": self.table_index,
            "html": self.html,
            "cells": [cell.to_dict() for cell in self.cells],
        }


@dataclass
class OCRPageEvidence:
    page: int
    text: str = ""
    engine: str = ""
    model: Optional[str] = None
    model_version: Optional[str] = None
    words: List[OCRWord] = field(default_factory=list)
    blocks: List[OCRBlockEvidence] = field(default_factory=list)
    tables: List[Dict[str, Any]] = field(default_factory=list)
    reading_order: Optional[List[Any]] = None
    confidence_mean: Optional[float] = None
    confidence_available: bool = False
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    preprocessing: Dict[str, Any] = field(default_factory=dict)
    raw_ref: Optional[str] = None
    raw_keys: List[str] = field(default_factory=list)
    status: str = "unknown"
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page": self.page,
            "text": self.text,
            "engine": self.engine,
            "model": self.model,
            "model_version": self.model_version,
            "words": [w.to_dict() for w in self.words],
            "blocks": [b.to_dict() for b in self.blocks],
            "tables": list(self.tables),
            "reading_order": self.reading_order,
            "confidence": {
                "mean": self.confidence_mean,
                "available": self.confidence_available,
            },
            "image_width": self.image_width,
            "image_height": self.image_height,
            "preprocessing": dict(self.preprocessing),
            "raw_ref": self.raw_ref,
            "raw_keys": list(self.raw_keys),
            "status": self.status,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


@dataclass
class OCRDocumentEvidence:
    """Document-level OCR evidence. Separate from business JSON."""

    document_id: str = ""
    engine: str = ""
    model: Optional[str] = None
    model_version: Optional[str] = None
    pages: List[OCRPageEvidence] = field(default_factory=list)
    unverified_fallback: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "engine": self.engine,
            "model": self.model,
            "model_version": self.model_version,
            "pages": [page.to_dict() for page in self.pages],
            "unverified_fallback": self.unverified_fallback,
            "metadata": dict(self.metadata),
        }

    def summary(self) -> Dict[str, Any]:
        """API-safe view without full page text."""
        payload = self.to_dict()
        for page in payload["pages"]:
            text = page.pop("text", "") or ""
            page["text_length"] = len(text)
            page.pop("words", None)
            page.pop("blocks", None)
            # Keep table HTML refs/counts but drop large cell dumps in summary.
            tables = page.get("tables") or []
            page["table_count"] = len(tables) if isinstance(tables, list) else 0
            page["tables"] = {
                "available": bool(tables),
                "count": page["table_count"],
            }
        return payload


def page_evidence_from_result(
    result: OCRResult,
    *,
    preprocessing: Optional[Dict[str, Any]] = None,
    raw_ref: Optional[str] = None,
) -> OCRPageEvidence:
    """Map an OCRResult into the typed evidence contract without inventing fields."""
    meta = dict(result.metadata or {})
    conf_meta = meta.get("confidence") if isinstance(meta.get("confidence"), dict) else {}
    mean = conf_meta.get("mean")
    if mean is None:
        mean = result.confidence
    available = bool(conf_meta.get("available")) if conf_meta else (mean is not None)

    blocks: List[OCRBlockEvidence] = []
    layout = meta.get("layout") if isinstance(meta.get("layout"), dict) else {}
    regions = layout.get("regions") if isinstance(layout.get("regions"), list) else []
    for index, region in enumerate(regions):
        if not isinstance(region, dict):
            continue
        bbox = _as_bbox(region.get("bbox"))
        blocks.append(
            OCRBlockEvidence(
                text=str(region.get("text") or ""),
                page=result.page,
                bbox=bbox,
                confidence=None,  # VL layout blocks do not supply per-block conf
                label=str(region.get("label")) if region.get("label") is not None else None,
                reading_order=_optional_int(region.get("order"), default=index),
            )
        )

    tables_meta = meta.get("tables") if isinstance(meta.get("tables"), dict) else {}
    table_items = tables_meta.get("items") if isinstance(tables_meta.get("items"), list) else []
    if not table_items and isinstance(meta.get("tables"), list):
        table_items = meta.get("tables") or []

    return OCRPageEvidence(
        page=result.page,
        text=result.text or "",
        engine=result.engine or str(meta.get("engine") or ""),
        model=meta.get("model"),
        model_version=meta.get("model_version") or meta.get("pipeline_version"),
        words=list(result.words or []),
        blocks=blocks,
        tables=[item for item in table_items if isinstance(item, dict)],
        reading_order=meta.get("reading_order")
        if isinstance(meta.get("reading_order"), list)
        else None,
        confidence_mean=float(mean) if isinstance(mean, (int, float)) else None,
        confidence_available=available,
        image_width=_optional_int(meta.get("image_width")),
        image_height=_optional_int(meta.get("image_height")),
        preprocessing=dict(preprocessing or meta.get("preprocessing") or {}),
        raw_ref=raw_ref or meta.get("raw_ref"),
        raw_keys=list(meta.get("raw_keys") or []),
        status=str(meta.get("status") or ("success" if result.text else "error")),
        error=meta.get("error"),
        metadata={
            k: v
            for k, v in meta.items()
            if k
            not in {
                "layout",
                "tables",
                "confidence",
                "reading_order",
                "boxes",
                "lines",
                "words",
            }
        },
    )


def _as_bbox(value: Any) -> Optional[BBoxTuple]:
    if not value:
        return None
    try:
        if isinstance(value, (list, tuple)) and len(value) == 4:
            x0, y0, x1, y1 = (float(v) for v in value)
            return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    except (TypeError, ValueError):
        return None
    return None


def _optional_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
