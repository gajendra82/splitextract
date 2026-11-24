"""Layout engines that may be adopted only after a measured benchmark.

PaddleOCR-VL remains the primary OCR engine. PP-StructureV3 and Docling are
recorded here so they are not wired in before a comparison against manually
verified documents exists. This module does not import or run either engine.
"""

from __future__ import annotations

import importlib.util
from typing import Any, Dict, List, Optional


def layout_candidates() -> List[Dict[str, Any]]:
    """Return adoption state. Unmeasured engines stay not adopted."""
    return [
        {
            "name": "paddleocr-vl",
            "role": "primary_ocr",
            "status": "adopted",
            "benchmark": "existing_primary",
            "importable": _importable("paddleocr"),
        },
        {
            "name": "pp-structurev3",
            "role": "layout",
            "status": "not_adopted",
            "benchmark": None,
            "reason": "no_measured_benchmark",
            "importable": None,
        },
        {
            "name": "docling",
            "role": "layout",
            "status": "not_adopted",
            "benchmark": None,
            "reason": "no_measured_benchmark",
            "importable": _importable("docling"),
        },
    ]


def select_layout_engine() -> str:
    """The only engine allowed to run is one that is already adopted."""
    adopted = [item["name"] for item in layout_candidates() if item["status"] == "adopted"]
    if adopted != ["paddleocr-vl"]:
        raise RuntimeError("layout engine adoption is not limited to the measured primary")
    return adopted[0]


def _importable(module: str) -> Optional[bool]:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False
