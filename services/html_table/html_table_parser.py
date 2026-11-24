"""Unified multi-vendor HTML table parser for PaddleOCR-VL output."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from services.html_table.column_matcher import match_headers
from services.html_table.llm_column_mapper import llm_map_columns
from services.html_table.templates import route_template

logger = logging.getLogger(__name__)

EXTRACTION_METHOD = "vl_html_table_parse"

_LINE_ITEM_FIELDS = {
    "product_name",
    "opening_qty",
    "opening_value",
    "receive_qty",
    "receive_value",
    "sales_qty",
    "sales_value",
    "closing_qty",
    "closing_value",
}
_EXTRA_FIELDS = {"last_month_sale", "expiry_stock"}
_FIELD_ALIASES = {
    "receive_qty": "receipts_qty",
    "receive_value": "receipts_value",
    # Existing statement schema / secondary-repair overlays use this name.
    "expiry_stock": "expiry_breakage_qty",
}
_NUMERIC_FIELDS = (_LINE_ITEM_FIELDS | _EXTRA_FIELDS) - {"product_name"}

_TOTAL_RE = re.compile(
    r"(?i)^\s*(?:grand\s+)?totals?\b|^\s*g\.?\s*t\.?\s*$|^\s*gt\b"
)
# Value/summary footers that must not become product line items.
_SUMMARY_LABEL_RE = re.compile(
    r"(?i)^\s*(?:last\s*month\s*sales|open(?:ing)?\.?\s*value|receipt\s*value|"
    r"sales\s*value|clos(?:e|ing)\.?\s*value|order\s*value|"
    r"amount\s*total|net\s*value|total\s*value)\b"
)
_PACKING_RE = re.compile(r"(?i)\bpack(?:ing|size)?\b|\buom\b|\bsize\b")

_SUMMARY_FIELD_MAP = (
    (re.compile(r"(?i)last\s*month\s*sales"), "last_month_sales_value"),
    (re.compile(r"(?i)open(?:ing)?\.?\s*value"), "opening_value"),
    (re.compile(r"(?i)receipt\s*value"), "receipts_value"),
    (re.compile(r"(?i)^sales\s*value"), "sales_value"),
    (re.compile(r"(?i)clos(?:e|ing)\.?\s*value"), "closing_value"),
    (re.compile(r"(?i)order\s*value"), "order_value"),
)


def parse_numeric(raw: Any) -> Optional[float | int]:
    """Indian-aware numeric parse. Empty/dash → None (not 0)."""
    from services.indian_normalize import parse_indian_number

    return parse_indian_number(raw)


def _cell_text(el: Any) -> str:
    text = "".join(el.itertext()) if el is not None else ""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _expand_row_cells(cells: List[Any]) -> List[str]:
    """Expand colspan (fill-forward) into a flat list of cell texts."""
    out: List[str] = []
    for cell in cells:
        text = _cell_text(cell)
        try:
            span = max(1, int(cell.get("colspan") or 1))
        except (TypeError, ValueError):
            span = 1
        out.append(text)
        for _ in range(span - 1):
            out.append(text)
    return out


def _extract_table_rows(html_str: str) -> Tuple[List[str], List[List[str]]]:
    from lxml import html as lxml_html

    root = lxml_html.fromstring(html_str)
    tables = root.xpath(".//table") if root.tag != "table" else [root]
    if not tables:
        for frag in lxml_html.fragments_fromstring(html_str, no_leading_text=True):
            if getattr(frag, "tag", None) == "table":
                tables.append(frag)
            elif hasattr(frag, "xpath"):
                tables.extend(frag.xpath(".//table"))
    if not tables:
        return [], []

    headers: List[str] = []
    body: List[List[str]] = []
    for table in tables:
        rows = table.xpath("./thead/tr|./tbody/tr|./tr") or table.xpath(".//tr")
        for row in rows:
            cells = row.xpath("./th|./td")
            texts = _expand_row_cells(cells)
            if not any(texts):
                continue
            is_header = any(c.tag == "th" for c in cells)
            if not headers:
                headers = texts
                # If first row has no <th>, still treat as header row.
                if not is_header:
                    continue
                continue
            body.append(texts)
        if headers:
            break
    return headers, body


def _is_total_row(first_cell: str, row: List[str]) -> bool:
    if _TOTAL_RE.match(first_cell or ""):
        return True
    joined = " ".join(row).casefold()
    return bool(re.search(r"\bgrand\s+total\b", joined))


def _is_summary_label_row(first_cell: str) -> bool:
    return bool(_SUMMARY_LABEL_RE.match(str(first_cell or "").strip()))


def _first_numeric_in_row(row: List[str]) -> Optional[float | int]:
    for cell in row:
        num = parse_numeric(cell)
        if num is not None:
            return num
    return None


def _fold_summary_row(
    totals: Optional[Dict[str, Any]],
    product_name: str,
    row: List[str],
) -> Dict[str, Any]:
    """Keep summary labels out of line_items; stash known values on totals."""
    bucket: Dict[str, Any] = dict(totals or {})
    extra = bucket.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        bucket["extra"] = extra
    summaries = extra.setdefault("summary_values", {})
    if not isinstance(summaries, dict):
        summaries = {}
        extra["summary_values"] = summaries

    value = _first_numeric_in_row(row)
    label = str(product_name or "").strip()
    field = None
    for pattern, name in _SUMMARY_FIELD_MAP:
        if pattern.search(label):
            field = name
            break
    if field and value is not None:
        # Prefer first authoritative footer value; do not invent.
        if bucket.get(field) is None and field in {
            "opening_value",
            "receipts_value",
            "sales_value",
            "closing_value",
        }:
            bucket[field] = value
        summaries.setdefault(field or label, value)
    elif label:
        summaries.setdefault(label, value)
    extra["summary_rows_filtered"] = int(extra.get("summary_rows_filtered") or 0) + 1
    return bucket


def _critical_enough(col_map: Dict[int, str]) -> bool:
    fields = set(col_map.values())
    if "product_name" not in fields:
        return False
    qty = {"opening_qty", "sales_qty", "closing_qty", "receive_qty"}
    return bool(fields & qty)


def resolve_column_mapping(
    headers: List[str],
    *,
    allow_llm: bool = True,
) -> Tuple[Dict[int, str], str, List[str]]:
    """Return ``(col_map, mapping_source, unmatched_headers)``."""
    name, template_map = route_template(headers)
    if name and template_map:
        unmatched = [
            headers[i]
            for i in range(len(headers))
            if i not in template_map and str(headers[i] or "").strip()
        ]
        return template_map, "template", unmatched

    fuzzy = match_headers(headers)
    fuzzy_fields = set(fuzzy.values())
    qty_fields = {"opening_qty", "sales_qty", "closing_qty", "receive_qty"}

    def _unmatched(col_map: Dict[int, str]) -> List[str]:
        return [
            headers[i]
            for i in range(len(headers))
            if i not in col_map and str(headers[i] or "").strip()
        ]

    if _critical_enough(fuzzy) or not allow_llm:
        return fuzzy, "fuzzy", _unmatched(fuzzy)

    # Product matched but qty headers look like OCR garbage ("0g 0tv") — do not
    # ask the LLM to invent columns; caller falls back to statement_from_ocr_html.
    if "product_name" in fuzzy_fields and not (fuzzy_fields & qty_fields):
        return fuzzy, "fuzzy", _unmatched(fuzzy)

    # Completely unknown synonym headers → LLM fallback.
    llm_map = llm_map_columns(headers) if allow_llm else {}
    if llm_map and _critical_enough(llm_map):
        return llm_map, "llm", _unmatched(llm_map)

    return fuzzy, "fuzzy", _unmatched(fuzzy)


def parse_html_table(
    html_str: str,
    *,
    allow_llm: bool = True,
) -> Dict[str, Any]:
    """Parse a VL HTML table into line items using template → fuzzy → LLM mapping."""
    html = (html_str or "").strip()
    empty = {
        "line_items": [],
        "totals": None,
        "extraction_method": EXTRACTION_METHOD,
        "columns_matched": {},
        "columns_unmatched": [],
        "mapping_source": "fuzzy",
    }
    if not html or "<table" not in html.lower():
        return empty

    try:
        headers, body_rows = _extract_table_rows(html)
    except Exception:
        logger.warning("parse_html_table: lxml parse failed", exc_info=True)
        return empty

    if not headers:
        return empty

    col_map, mapping_source, unmatched = resolve_column_mapping(
        headers, allow_llm=allow_llm
    )
    logger.info(
        "html_table_parser mapping_source=%s matched=%s unmatched=%s",
        mapping_source,
        len(col_map),
        len(unmatched),
    )
    columns_matched = {int(k): v for k, v in col_map.items()}
    packing_col = next(
        (
            i
            for i, h in enumerate(headers)
            if i not in columns_matched and _PACKING_RE.search(str(h or ""))
        ),
        None,
    )

    product_cols = [i for i, f in columns_matched.items() if f == "product_name"]
    product_col = product_cols[0] if product_cols else (0 if headers else None)

    line_items: List[Dict[str, Any]] = []
    totals: Optional[Dict[str, Any]] = None

    for row in body_rows:
        if not any(str(c or "").strip() for c in row):
            continue
        if product_col is None or product_col >= len(row):
            continue
        product_name = str(row[product_col] or "").strip()
        if not product_name:
            continue

        values: Dict[str, Any] = {}
        for col_idx, field in columns_matched.items():
            if field == "product_name" or col_idx >= len(row):
                continue
            raw = row[col_idx]
            if field in _NUMERIC_FIELDS:
                num = parse_numeric(raw)
                if num is None and str(raw or "").strip():
                    continue
                values[field] = num
            else:
                values[field] = str(raw or "").strip() or None

        packing = ""
        if packing_col is not None and packing_col < len(row):
            packing = str(row[packing_col] or "").strip()

        if _is_total_row(product_name, row):
            totals = {}
            for field, num in values.items():
                alias = _FIELD_ALIASES.get(field, field)
                totals[alias] = num
                if alias != field:
                    totals[field] = num
            continue

        if _is_summary_label_row(product_name):
            totals = _fold_summary_row(totals, product_name, row)
            continue

        item: Dict[str, Any] = {"product_name": product_name, "extra": {}}
        if packing:
            item["packing"] = packing
        for field, num in values.items():
            if field in _EXTRA_FIELDS:
                item["extra"][field] = num
                alias = _FIELD_ALIASES.get(field)
                if alias:
                    item["extra"][alias] = num
            elif field in _LINE_ITEM_FIELDS:
                item[field] = num
                alias = _FIELD_ALIASES.get(field)
                if alias:
                    item[alias] = num
            else:
                item["extra"][field] = num
        line_items.append(item)

    return {
        "line_items": line_items,
        "totals": totals,
        "extraction_method": EXTRACTION_METHOD,
        "columns_matched": columns_matched,
        "columns_unmatched": unmatched,
        "mapping_source": mapping_source,
    }
