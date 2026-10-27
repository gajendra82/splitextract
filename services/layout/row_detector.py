"""Row candidate detection from evidence tokens."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from services.evidence import DocumentEvidence


def detect_rows(
    evidence: DocumentEvidence,
    tables: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    del tables
    rows: List[Dict[str, Any]] = []
    for page in evidence.pages:
        buckets: Dict[int, List[Any]] = defaultdict(list)
        for token in page.tokens:
            if not token.bbox or not token.text:
                continue
            # 8px y-banding is enough for a structural sketch.
            band = int(round(token.bbox.y0 / 8.0))
            buckets[band].append(token)
        for band, tokens in sorted(buckets.items()):
            if len(tokens) < 2:
                continue
            ordered = sorted(tokens, key=lambda t: t.bbox.x0)
            rows.append(
                {
                    "page": page.page_number,
                    "band": band,
                    "y0": min(t.bbox.y0 for t in ordered),
                    "texts": [t.text for t in ordered],
                    "token_count": len(ordered),
                }
            )
    return rows
