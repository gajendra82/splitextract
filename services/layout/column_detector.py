"""Column / header candidate detection from evidence."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from services.evidence import DocumentEvidence


def detect_columns(
    evidence: DocumentEvidence,
    tables: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    del tables  # reserved for future table-scoped columns
    columns: List[Dict[str, Any]] = []
    seen = set()
    for page in evidence.pages:
        # Prefer top-of-page tokens as header candidates.
        ranked = sorted(
            [t for t in page.tokens if t.text and t.bbox],
            key=lambda t: (t.bbox.y0, t.bbox.x0),
        )
        for token in ranked[:40]:
            key = token.text.strip().casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            columns.append(
                {
                    "header": token.text.strip(),
                    "page": page.page_number,
                    "bbox": [
                        token.bbox.x0,
                        token.bbox.y0,
                        token.bbox.x1,
                        token.bbox.y1,
                    ]
                    if token.bbox
                    else None,
                    "confidence": token.confidence,
                    "source": token.source_engine or evidence.extraction_engine,
                }
            )
    return columns
