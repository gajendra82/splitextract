"""Row/cell-scoped grounding for VL HTML table extracts."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from services.indian_normalize import ground_value, totals_within_tolerance

logger = logging.getLogger(__name__)

_QTY_FIELDS = (
    "opening_qty",
    "opening_value",
    "receive_qty",
    "receipts_qty",
    "receive_value",
    "sales_qty",
    "sales_value",
    "closing_qty",
    "closing_value",
    "last_month_sale",
    "expiry_stock",
    "expiry_breakage_qty",
)


def apply_row_scoped_grounding(
    result: Dict[str, Any],
    *,
    table_html: Optional[str] = None,
) -> Dict[str, Any]:
    """Ground extracted values against row/cell text from VL HTML.

    Scope preference: cell → row → (caller may fall back to page).
    Does not change VERIFIED/UNRESOLVED decision logic — only stamps
    ``source_discrepancy`` like full-page grounding.
    """
    if not isinstance(result, dict):
        return result
    html = table_html or _html_from_result(result)
    if not html or "<table" not in html.lower():
        return result

    rows = _html_rows(html)
    if not rows:
        return result

    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    discrepancies: List[Dict[str, Any]] = []
    compared = 0
    scope_used = "row"

    for index, item in enumerate(items):
        product = str(item.get("product_name") or "").strip()
        if not product:
            continue
        row_text, cell_map = _find_row(rows, product, item.get("packing"))
        if not row_text:
            continue
        compared += 1
        for field in _QTY_FIELDS:
            value = item.get(field)
            if value is None and isinstance(item.get("extra"), dict):
                value = item["extra"].get(field)
            if value is None:
                continue
            cell_text = cell_map.get(field) if cell_map else None
            if cell_text is not None:
                scope = "cell"
                ok = ground_value(value, cell_text)
            else:
                scope = "row"
                ok = ground_value(value, row_text)
            scope_used = scope if scope == "cell" else scope_used
            if ok:
                continue
            # Totals-style soft tolerance for values.
            if isinstance(value, (int, float)) and totals_within_tolerance(
                value, _nearest_number(row_text, value)
            ):
                continue
            # Secondary-repair overlays intentionally replace VL HTML digits.
            sources = {}
            if isinstance(item.get("extra"), dict):
                sources = item["extra"].get("field_sources") or {}
            if str(sources.get(field) or "").startswith("secondary_repair"):
                continue
            overlay = (
                ((result.get("totals") or {}).get("extra") or {}).get(
                    "secondary_repair_overlay"
                )
                or {}
            )
            repaired_keys = {
                (str(e.get("product_key") or "").casefold(), str(e.get("field") or ""))
                for e in (overlay.get("fields") or [])
                if isinstance(e, dict)
            }
            if (product.casefold(), field) in repaired_keys:
                continue
            discrepancies.append(
                {
                    "index": index,
                    "product_name": product,
                    "field": field,
                    "extracted": value,
                    "source": cell_text if cell_text is not None else row_text[:80],
                    "code": "value_mismatch",
                    "grounding_scope": scope,
                }
            )

    # Totals reconciliation with ±0.5% tolerance.
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    ocr_totals = _totals_from_html(rows)
    for field in ("opening_qty", "sales_qty", "closing_qty", "receive_qty", "receipts_qty"):
        ext = totals.get(field)
        src = ocr_totals.get(field)
        if ext is None or src is None:
            continue
        if totals_within_tolerance(ext, src):
            continue
        if ground_value(ext, " ".join(str(v) for v in ocr_totals.values())):
            continue
        discrepancies.append(
            {
                "index": -1,
                "product_name": "TOTAL",
                "field": field,
                "extracted": ext,
                "source": src,
                "code": "totals_mismatch",
                "grounding_scope": "row",
            }
        )

    report = {
        "status": "checked",
        "reader": "vl_html_row",
        "compared_rows": compared,
        "discrepancy_count": len(discrepancies),
        "discrepancies": discrepancies[:30],
        "grounding_scope": scope_used,
        "blank_read_as_zero": 0,
    }
    _stamp(result, discrepancies)
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    if isinstance(extra, dict):
        extra["row_grounding"] = report
    logger.info(
        "row_grounding scope=%s compared=%s discrepancies=%s",
        scope_used,
        compared,
        len(discrepancies),
    )
    return result


def _html_from_result(result: Dict[str, Any]) -> str:
    extra = ((result.get("totals") or {}).get("extra") or {})
    return str(extra.get("ocr_table_html") or "")


def _html_rows(html: str) -> List[Dict[str, Any]]:
    from lxml import html as lxml_html

    try:
        root = lxml_html.fromstring(html)
    except Exception:
        return []
    tables = root.xpath(".//table") if root.tag != "table" else [root]
    out: List[Dict[str, Any]] = []
    for table in tables:
        for row in table.xpath(".//tr"):
            cells = row.xpath("./th|./td")
            texts = [
                re.sub(r"\s+", " ", "".join(c.itertext()).replace("\xa0", " ")).strip()
                for c in cells
            ]
            if not any(texts):
                continue
            out.append({"cells": texts, "text": " | ".join(texts)})
    return out


def _find_row(
    rows: List[Dict[str, Any]],
    product: str,
    packing: Any,
) -> Tuple[str, Dict[str, str]]:
    target = re.sub(r"\s+", " ", product).casefold()
    pack = re.sub(r"\s+", "", str(packing or "")).casefold()
    for row in rows:
        cells = row.get("cells") or []
        joined = row.get("text") or ""
        if target not in joined.casefold():
            continue
        if pack and pack not in re.sub(r"\s+", "", joined).casefold():
            # packing mismatch — keep searching
            if not any(pack in re.sub(r"\s+", "", c).casefold() for c in cells):
                continue
        # Without column map, expose all cells as anonymous; callers use row text.
        return joined, {}
    return "", {}


def _totals_from_html(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    from services.indian_normalize import parse_indian_number

    for row in reversed(rows):
        cells = row.get("cells") or []
        if not cells:
            continue
        if not re.search(r"(?i)^\s*(?:grand\s+)?totals?\b|^\s*gt\b", cells[0]):
            continue
        nums = [parse_indian_number(c) for c in cells[1:]]
        nums = [n for n in nums if n is not None]
        # Heuristic positional mapping when headerless TOTAL row.
        mapped: Dict[str, Any] = {}
        if len(nums) >= 1:
            mapped["opening_qty"] = nums[0]
        if len(nums) >= 3:
            mapped["sales_qty"] = nums[2] if len(nums) > 2 else nums[-2]
        if len(nums) >= 2:
            mapped["closing_qty"] = nums[-1] if len(nums) > 1 else None
        return mapped
    return {}


def _nearest_number(row_text: str, value: Any) -> Optional[Any]:
    from services.indian_normalize import parse_indian_number

    target = parse_indian_number(value)
    if target is None:
        return None
    best = None
    best_dist = None
    for token in re.findall(r"[\d,]+(?:\.\d+)?", row_text or ""):
        num = parse_indian_number(token)
        if num is None:
            continue
        dist = abs(float(num) - float(target))
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = num
    return best


def _stamp(result: Dict[str, Any], discrepancies: List[Dict[str, Any]]) -> None:
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    by_index: Dict[int, List[Dict[str, Any]]] = {}
    for d in discrepancies:
        idx = d.get("index")
        if isinstance(idx, int) and idx >= 0:
            by_index.setdefault(idx, []).append(d)
    for index, findings in by_index.items():
        if index >= len(items):
            continue
        extra = items[index].get("extra")
        if not isinstance(extra, dict):
            extra = {}
            items[index]["extra"] = extra
        extra["source_discrepancy"] = True
        extra["source_validation_error"] = "; ".join(
            f"row-ground: {f.get('field')} extracted {f.get('extracted')} "
            f"not in {f.get('grounding_scope')} text"
            for f in findings
        )
        extra["grounding_scope"] = findings[0].get("grounding_scope")
