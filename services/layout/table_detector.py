"""Table region detection from evidence tokens / pages."""

from __future__ import annotations

from typing import Any, Dict, List

from services.evidence import DocumentEvidence


def detect_tables(evidence: DocumentEvidence) -> List[Dict[str, Any]]:
    tables: List[Dict[str, Any]] = []
    for page in evidence.pages:
        if not page.tokens:
            continue
        xs = [t.bbox.x0 for t in page.tokens if t.bbox]
        ys = [t.bbox.y0 for t in page.tokens if t.bbox]
        if not xs or not ys:
            tables.append(
                {
                    "page": page.page_number,
                    "token_count": len(page.tokens),
                    "bbox": None,
                }
            )
            continue
        x1s = [t.bbox.x1 for t in page.tokens if t.bbox]
        y1s = [t.bbox.y1 for t in page.tokens if t.bbox]
        tables.append(
            {
                "page": page.page_number,
                "token_count": len(page.tokens),
                "bbox": [min(xs), min(ys), max(x1s), max(y1s)],
            }
        )
    return tables
