"""Multi-page VL HTML table continuation + multi-statement segmentation."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from services.html_table.html_table_parser import parse_html_table
from services.indian_normalize import parse_indian_number

logger = logging.getLogger(__name__)

_NEW_STATEMENT_RE = re.compile(
    r"(?i)stock\s*(?:&|and)?\s*sales\s*statement|product\s*wise\s*stock\s*statement"
)
_STOCKIST_BANNER_RE = re.compile(
    r"(?i)\b(distributors?|pharmacy|traders?|suppliers?|medicals?)\b"
)


def merge_page_tables(
    page_htmls: List[str],
    *,
    allow_llm: bool = True,
) -> Dict[str, Any]:
    """Parse each page HTML and merge continuation rows / segment statements.

    Returns the same shape as ``parse_html_table``, plus ``page_count`` and
    ``statements`` when multiple independent statements are detected.
    """
    pages = [h for h in (page_htmls or []) if h and "<table" in h.lower()]
    if not pages:
        return {
            "line_items": [],
            "totals": None,
            "extraction_method": "vl_html_table_parse",
            "columns_matched": {},
            "columns_unmatched": [],
            "mapping_source": "fuzzy",
            "page_count": 0,
        }

    segments: List[List[str]] = _segment_pages(pages)
    if len(segments) > 1:
        statements = [
            _merge_continuation_pages(seg, allow_llm=allow_llm) for seg in segments
        ]
        primary = statements[0]
        primary["statements"] = statements
        primary["page_count"] = len(pages)
        return primary

    merged = _merge_continuation_pages(pages, allow_llm=allow_llm)
    merged["page_count"] = len(pages)
    return merged


def _segment_pages(pages: List[str]) -> List[List[str]]:
    """Split when a page looks like a new statement (new title / stockist)."""
    segments: List[List[str]] = [[pages[0]]]
    for html in pages[1:]:
        prefix = html[: html.lower().find("<table")] if "<table" in html.lower() else html[:400]
        if _NEW_STATEMENT_RE.search(prefix) or (
            _STOCKIST_BANNER_RE.search(prefix) and _NEW_STATEMENT_RE.search(html[:800])
        ):
            segments.append([html])
        else:
            segments[-1].append(html)
    return segments


def _merge_continuation_pages(
    pages: List[str],
    *,
    allow_llm: bool,
) -> Dict[str, Any]:
    first = parse_html_table(pages[0], allow_llm=allow_llm)
    col_map = dict(first.get("columns_matched") or {})
    mapping_source = str(first.get("mapping_source") or "fuzzy")
    unmatched = list(first.get("columns_unmatched") or [])
    items: List[Dict[str, Any]] = list(first.get("line_items") or [])
    totals = first.get("totals")

    for page_html in pages[1:]:
        nxt = parse_html_table(page_html, allow_llm=allow_llm)
        nxt_items = list(nxt.get("line_items") or [])
        if _is_continuation(nxt_items, nxt.get("columns_matched") or {}):
            # Carry forward page-1 mapping already applied by parse; just append.
            logger.info(
                "multi_page: continuation page appended rows=%s mapping_source=%s",
                len(nxt_items),
                mapping_source,
            )
            items.extend(nxt_items)
        else:
            items.extend(nxt_items)
            # Prefer richer mapping if later page has headers.
            if len(nxt.get("columns_matched") or {}) > len(col_map):
                col_map = dict(nxt.get("columns_matched") or {})
                mapping_source = str(nxt.get("mapping_source") or mapping_source)
                unmatched = list(nxt.get("columns_unmatched") or [])
        if nxt.get("totals"):
            totals = _merge_totals(totals, nxt.get("totals"))

    return {
        "line_items": items,
        "totals": totals,
        "extraction_method": "vl_html_table_parse",
        "columns_matched": col_map,
        "columns_unmatched": unmatched,
        "mapping_source": mapping_source,
    }


def _is_continuation(
    items: List[Dict[str, Any]],
    col_map: Dict[Any, str],
) -> bool:
    """True when first row looks like orphaned qty cells (no product name)."""
    if not items:
        return True
    first = items[0]
    name = str(first.get("product_name") or "").strip()
    has_qty = any(
        first.get(k) is not None
        for k in ("opening_qty", "sales_qty", "closing_qty", "receive_qty")
    )
    # Continuation: blank/numeric-only "product" with quantities present.
    if not name and has_qty:
        return True
    if name and parse_indian_number(name) is not None and has_qty:
        return True
    # No product column mapped on this page → continuation of prior headers.
    if "product_name" not in set(col_map.values()) and has_qty:
        return True
    return False


def _merge_totals(
    a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    if not a:
        return b
    if not b:
        return a
    out = dict(a)
    for key, value in b.items():
        if key == "extra":
            continue
        # Prefer later totals row (usually printed on last page).
        if value is not None:
            out[key] = value
    return out
