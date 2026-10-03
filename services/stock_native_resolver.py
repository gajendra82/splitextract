"""Phase 3b: STOCK_NATIVE_RESOLVER orchestration (off | shadow | on).

Native lanes never call Gemini. Flag default OFF → byte-identical to today.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_CANON_TO_ITEM = {
    "product_name": ("product_name", False),
    "pack": ("packing", False),
    "batch": ("batch", True),
    "opening_qty": ("opening_qty", False),
    "opening_value": ("opening_value", False),
    "purchase_qty": ("receipts_qty", False),
    "purchase_value": ("receipts_value", True),
    "sales_qty": ("sales_qty", False),
    "sales_value": ("sales_value", False),
    "closing_qty": ("closing_qty", False),
    "closing_value": ("closing_value", False),
    "sales_return_qty": ("sale_return", True),
    "expiry_damage_qty": ("exp_damage", True),
    "free_in_qty": ("free_in_qty", True),
    "free_out_qty": ("free_out_qty", True),
    "total_qty": ("total_stock", True),
    "order_qty": ("order_qty", True),
    "rate": ("unit_rate", True),
}


def stock_native_resolver_mode() -> str:
    raw = os.getenv("STOCK_NATIVE_RESOLVER", "off").strip().lower()
    if raw in {"shadow", "on", "off", "false", "0", ""}:
        if raw in {"false", "0", ""}:
            return "off"
        return raw if raw in {"shadow", "on", "off"} else "off"
    return "off"


def native_resolver_active() -> bool:
    return stock_native_resolver_mode() in {"shadow", "on"}


def _norm_name(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _is_total_row_name(name: Any) -> bool:
    key = _norm_name(name).lower()
    if not key:
        return True
    return key in {
        "total",
        "grandtotal",
        "subtotal",
        "pagetotal",
        "gtotal",
    } or (key.startswith("total") and len(key) <= 12)


def _row_match_key(item: Dict[str, Any]) -> Tuple[str, str, str]:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    return (
        _norm_name(item.get("product_name")),
        _norm_name(item.get("packing") or extra.get("pack")),
        _norm_name(item.get("batch") or extra.get("batch")),
    )


def _row_key(item: Dict[str, Any]) -> Tuple[str, str, str]:
    return _row_match_key(item)


def native_row_coverage(
    old_items: List[Dict[str, Any]], new_items: List[Dict[str, Any]]
) -> float:
    """Fraction of old non-total product rows matched in new (exact, then difflib >= 0.85)."""
    import difflib

    def _product_rows(items: List[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
        out: List[Tuple[str, str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if _is_total_row_name(item.get("product_name")):
                continue
            key = _row_match_key(item)
            if not key[0]:
                continue
            out.append(key)
        return out

    old_keys = _product_rows(old_items)
    new_keys = _product_rows(new_items)
    if not old_keys:
        return 1.0
    remaining = list(new_keys)
    matched = 0
    for okey in old_keys:
        # Exact on full key, then on product_name alone with pack/batch fuzzy.
        if okey in remaining:
            remaining.remove(okey)
            matched += 1
            continue
        best_i = -1
        best_ratio = 0.0
        for i, nkey in enumerate(remaining):
            # Require pack/batch exact when present on old.
            if okey[1] and okey[1] != nkey[1]:
                continue
            if okey[2] and okey[2] != nkey[2]:
                continue
            ratio = difflib.SequenceMatcher(None, okey[0], nkey[0]).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_i = i
        if best_i >= 0 and best_ratio >= 0.85:
            remaining.pop(best_i)
            matched += 1
    return float(matched) / float(len(old_keys))


def _empty_line_item() -> Dict[str, Any]:
    return {
        "product_code": None,
        "product_name": None,
        "packing": None,
        "opening_qty": None,
        "receipts_qty": None,
        "sales_qty": None,
        "sales_value": None,
        "closing_qty": None,
        "closing_value": None,
        "extra": {},
    }


def fields_to_line_item(
    fields: Dict[str, Any],
    *,
    formula_tokens: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Map resolver field texts onto a stock line_item."""
    from services.stock_header_resolver import parse_number
    from services.stock_row_classifier import RowStatus

    item = _empty_line_item()
    extra = item["extra"]
    field_source: Dict[str, str] = {}
    formula_tokens = formula_tokens or []

    for canon, raw in (fields or {}).items():
        mapping = _CANON_TO_ITEM.get(canon)
        if not mapping:
            continue
        dest, into_extra = mapping
        if canon == "product_name":
            item["product_name"] = str(raw).strip() if raw not in (None, "") else None
            continue
        if canon == "pack":
            item["packing"] = str(raw).strip() if raw not in (None, "") else None
            continue
        if raw is None or raw == "":
            # Missing printed value.
            if into_extra:
                extra[dest] = None
            else:
                item[dest] = None
            field_source[dest] = "missing"
            continue
        number = parse_number(raw)
        if number is None and str(raw).strip().startswith("="):
            # Uncached formula string in the cell text.
            if into_extra:
                extra[dest] = None
            else:
                item[dest] = None
            field_source[dest] = "missing"
            cands = extra.setdefault("candidates", [])
            cands.append(
                {
                    "source": "xlsx_formula",
                    "fields": {dest: None},
                    "reason": str(raw),
                }
            )
            continue
        if into_extra:
            extra[dest] = number
        else:
            item[dest] = number

    for tok in formula_tokens:
        if not isinstance(tok, dict) or not tok.get("formula"):
            continue
        # Mark as missing; never coerce to 0.
        cands = extra.setdefault("candidates", [])
        cands.append(
            {
                "source": "xlsx_formula",
                "fields": {},
                "reason": str(tok.get("formula")),
            }
        )
        extra["row_status"] = RowStatus.MISSING_VALUE.value

    if field_source:
        extra["field_source"] = field_source
        if any(v == "missing" for v in field_source.values()):
            # Only force MISSING_VALUE when a qty column is missing.
            qty_keys = {
                "opening_qty",
                "receipts_qty",
                "sales_qty",
                "closing_qty",
            }
            if any(k in field_source and field_source[k] == "missing" for k in qty_keys):
                extra.setdefault("row_status", RowStatus.MISSING_VALUE.value)
    return item


def build_line_items_from_tokens(
    columns: List[Dict[str, Any]],
    token_rows: List[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    from services.stock_header_resolver import assign_cells
    from services.stock_row_classifier import RowStatus, record_candidate

    items: List[Dict[str, Any]] = []
    for tokens in token_rows:
        structural = any(
            isinstance(t, dict) and t.get("_structural") for t in tokens
        )
        if structural:
            texts = [
                str(t.get("text") or "").strip()
                for t in tokens
                if isinstance(t, dict) and t.get("text") not in (None, "")
            ]
            if not texts:
                continue
            name = texts[0]
            if not re.search(r"[A-Za-z]", name):
                continue
            if re.match(r"^(TOTAL|GRAND|SUB\s*TOTAL)\b", name, re.I):
                continue
            item = _empty_line_item()
            item["product_name"] = name
            item["extra"]["row_status"] = RowStatus.STRUCTURAL_ERROR.value
            record_candidate(
                item,
                {"printed_tokens": texts},
                "native_pdf_no_header",
                "page_without_header_or_carry",
            )
            items.append(item)
            continue
        assigned = assign_cells(tokens, columns)
        fields = assigned.get("fields") or {}
        formula_toks = [t for t in tokens if isinstance(t, dict) and t.get("formula")]
        item = fields_to_line_item(fields, formula_tokens=formula_toks)
        name = item.get("product_name")
        if not name or not re.search(r"[A-Za-z]", str(name)):
            continue
        if re.match(r"^(TOTAL|GRAND|SUB\s*TOTAL)\b", str(name), re.I):
            continue
        items.append(item)
    return items


def build_items_from_pdf_page_results(
    page_results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build line items across multi-page PDF native results."""
    from services.stock_native_tables import resolver_ready

    items: List[Dict[str, Any]] = []
    for page in page_results or []:
        rows = page.get("data_rows") or []
        if page.get("structural"):
            items.extend(build_line_items_from_tokens([], rows))
            continue
        header = page.get("header_cells") or []
        ready = resolver_ready(header)
        if not ready.get("resolved"):
            # Treat as structural if header present but unusable.
            tagged = []
            for tokens in rows:
                row = [dict(t) for t in tokens]
                if row:
                    row[0] = dict(row[0])
                    row[0]["_structural"] = True
                tagged.append(row)
            items.extend(build_line_items_from_tokens([], tagged))
            continue
        items.extend(
            build_line_items_from_tokens(ready.get("columns") or [], rows)
        )
    return items


def _valid_ratio(items: List[Dict[str, Any]]) -> float:
    from services.stock_row_classifier import classify_result

    classified = classify_result({"line_items": items})
    try:
        return float(classified.get("valid_ratio") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _field_diff_counts(
    old_items: List[Dict[str, Any]],
    new_items: List[Dict[str, Any]],
) -> Dict[str, int]:
    qty_fields = (
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "sales_value",
        "closing_qty",
        "closing_value",
    )
    old_by = {_row_key(i): i for i in old_items if isinstance(i, dict)}
    new_by = {_row_key(i): i for i in new_items if isinstance(i, dict)}
    counts: Dict[str, int] = {}
    matched = 0
    for key, old in old_by.items():
        new = new_by.get(key)
        if new is None:
            continue
        matched += 1
        for field in qty_fields:
            ov = old.get(field)
            nv = new.get(field)
            try:
                same = (
                    ov is None and nv is None
                ) or (
                    ov is not None
                    and nv is not None
                    and abs(float(ov) - float(nv)) < 0.051
                )
            except (TypeError, ValueError):
                same = str(ov) == str(nv)
            if not same:
                counts[field] = counts.get(field, 0) + 1
    counts["_matched"] = matched
    return counts


def compare_and_maybe_apply(
    old_result: Dict[str, Any],
    *,
    input_type: str,
    parser: str,
    header_cells: Optional[List[Dict[str, Any]]] = None,
    token_rows: Optional[List[List[Dict[str, Any]]]] = None,
    request_id: str = "-",
    new_items_override: Optional[List[Dict[str, Any]]] = None,
    pages_total: Optional[int] = None,
    pages_resolved: Optional[int] = None,
) -> Dict[str, Any]:
    """Shadow or on-mode native resolver pass. Never calls Gemini."""
    mode = stock_native_resolver_mode()
    if mode == "off" or not isinstance(old_result, dict):
        return old_result

    from services.stock_native_tables import resolver_ready

    header_cells = header_cells or []
    token_rows = token_rows or []
    ready = resolver_ready(header_cells) if header_cells else {
        "resolved": bool(new_items_override),
        "columns": [],
        "errors": [] if new_items_override else ["NO_HEADER"],
    }
    resolved = bool(ready.get("resolved")) or bool(new_items_override)
    errors = ready.get("errors") or []
    error_codes = [
        e.get("code") if isinstance(e, dict) else str(e) for e in errors
    ]
    new_items: List[Dict[str, Any]] = []
    if new_items_override is not None:
        new_items = list(new_items_override)
        resolved = bool(new_items) or resolved
    elif ready.get("resolved"):
        new_items = build_line_items_from_tokens(
            ready.get("columns") or [], token_rows
        )

    old_items = [
        i for i in (old_result.get("line_items") or []) if isinstance(i, dict)
    ]
    old_ratio = _valid_ratio(old_items)
    new_ratio = _valid_ratio(new_items) if new_items else 0.0
    diffs = _field_diff_counts(old_items, new_items)
    matched = int(diffs.pop("_matched", 0))
    coverage = native_row_coverage(old_items, new_items)

    page_bits = ""
    page_args: List[Any] = []
    if pages_total is not None:
        page_bits = " pages_total=%s pages_resolved=%s"
        page_args = [pages_total, pages_resolved if pages_resolved is not None else 0]

    logger.info(
        "NATIVE_RESOLVER_SHADOW request_id=%s input_type=%s parser=%s "
        "resolved=%s errors=%s rows_old=%s rows_new=%s matched=%s "
        "field_diffs=%s old_valid_ratio=%s new_valid_ratio=%s coverage=%s"
        + page_bits,
        request_id,
        input_type,
        parser or "-",
        str(resolved).lower(),
        json.dumps(error_codes),
        len(old_items),
        len(new_items),
        matched,
        json.dumps(diffs, sort_keys=True),
        round(old_ratio, 4),
        round(new_ratio, 4),
        round(coverage, 4),
        *page_args,
    )

    if mode != "on":
        return old_result

    # On mode: coverage first (same rule as Phase 1 OCR-vs-Gemini pick).
    if coverage < 0.90:
        if new_items:
            extra = old_result.setdefault("totals", {}).setdefault("extra", {})
            if isinstance(extra, dict):
                extra["native_resolver_candidates"] = new_items
                extra["native_resolver_picked"] = "old"
                extra["native_resolver_coverage"] = round(coverage, 4)
        logger.info(
            "NATIVE_RESOLVER_PICK request_id=%s picked=old reason=LOW_ROW_COVERAGE "
            "coverage=%s input_type=%s parser=%s resolved=%s "
            "old_valid_ratio=%s new_valid_ratio=%s rows_old=%s rows_new=%s",
            request_id,
            round(coverage, 4),
            input_type,
            parser or "-",
            str(resolved).lower(),
            round(old_ratio, 4),
            round(new_ratio, 4),
            len(old_items),
            len(new_items),
        )
        return old_result

    pick_new = resolved and new_items and new_ratio >= old_ratio
    logger.info(
        "NATIVE_RESOLVER_PICK request_id=%s picked=%s input_type=%s parser=%s "
        "resolved=%s old_valid_ratio=%s new_valid_ratio=%s rows_old=%s rows_new=%s "
        "coverage=%s",
        request_id,
        "new" if pick_new else "old",
        input_type,
        parser or "-",
        str(resolved).lower(),
        round(old_ratio, 4),
        round(new_ratio, 4),
        len(old_items),
        len(new_items),
        round(coverage, 4),
    )
    if pick_new:
        out = dict(old_result)
        out["line_items"] = new_items
        extra = out.setdefault("totals", {}).setdefault("extra", {})
        if isinstance(extra, dict):
            extra["extraction_method"] = (
                str(extra.get("extraction_method") or parser or "native")
                + "+native_resolver"
            )
            extra["native_resolver_picked"] = "new"
            extra["native_resolver_coverage"] = round(coverage, 4)
        return out

    # Keep old; attach resolver rows as candidates on the statement.
    if new_items:
        extra = old_result.setdefault("totals", {}).setdefault("extra", {})
        if isinstance(extra, dict):
            extra["native_resolver_candidates"] = new_items
            extra["native_resolver_picked"] = "old"
            extra["native_resolver_coverage"] = round(coverage, 4)
    return old_result


def run_native_pass_from_sheet(
    old_result: Dict[str, Any],
    rows: List[List[Any]],
    *,
    input_type: str = "spreadsheet",
    parser: str = "xls",
    request_id: str = "-",
) -> Dict[str, Any]:
    if stock_native_resolver_mode() == "off":
        return old_result
    from services.stock_native_tables import from_sheet, sheet_data_rows_to_tokens

    blocks = from_sheet(rows)
    if not blocks:
        logger.info(
            "NATIVE_RESOLVER_SHADOW request_id=%s input_type=%s parser=%s "
            "resolved=false errors=[\"NO_HEADER\"] rows_old=%s rows_new=0 "
            "matched=0 field_diffs={} old_valid_ratio=%s new_valid_ratio=0",
            request_id,
            input_type,
            parser,
            len(old_result.get("line_items") or []),
            round(_valid_ratio(old_result.get("line_items") or []), 4),
        )
        return old_result
    block = blocks[0]
    tokens = sheet_data_rows_to_tokens(block.get("data_rows") or [])
    return compare_and_maybe_apply(
        old_result,
        input_type=input_type,
        parser=parser,
        header_cells=block.get("header_cells") or [],
        token_rows=tokens,
        request_id=request_id,
    )


def run_native_pass_from_text(
    old_result: Dict[str, Any],
    lines: List[str],
    *,
    column_plan: Optional[Dict[str, Any]] = None,
    input_type: str = "text",
    parser: str = "txt",
    request_id: str = "-",
) -> Dict[str, Any]:
    if stock_native_resolver_mode() == "off":
        return old_result
    from services.stock_native_tables import from_fixed_width, from_text_header

    if column_plan:
        built = from_fixed_width(lines, column_plan)
    else:
        built = from_text_header(lines)
    return compare_and_maybe_apply(
        old_result,
        input_type=input_type,
        parser=parser,
        header_cells=built.get("header_cells") or [],
        token_rows=built.get("data_rows") or [],
        request_id=request_id,
    )
