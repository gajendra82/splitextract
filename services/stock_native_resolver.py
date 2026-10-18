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


_NUM_TOKEN = re.compile(r"^-?\d[\d,]*(?:\.\d+)?$")
_AMOUNT_GRAND = re.compile(r"(?i)amount\s+grand\s+total")
_TOTAL_LABEL = re.compile(
    r"(?i)^(qty|amount|amt)\s+(grand\s+)?total\b"
)
_SKIP_CONTINUATION = re.compile(
    r"(?i)^(for\b|authorised\b|authorized\b|page\b|\d+\s*/\s*\d+$)"
)


def _free_column_layout(columns: List[Dict[str, Any]]) -> bool:
    canons = {str(c.get("canonical") or "") for c in columns}
    return {
        "purchase_qty",
        "free_in_qty",
        "sales_qty",
        "free_out_qty",
        "closing_qty",
        "closing_value",
    } <= canons


def _expand_glued_numbers(tokens: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for tok in tokens:
        if not isinstance(tok, dict):
            continue
        text = str(tok.get("text") or "").strip()
        parts = text.split()
        if (
            len(parts) == 2
            and _NUM_TOKEN.match(parts[0].replace(" ", ""))
            and _NUM_TOKEN.match(parts[1].replace(" ", ""))
        ):
            for part in parts:
                nxt = dict(tok)
                nxt["text"] = part
                nxt.pop("x", None)
                out.append(nxt)
        else:
            out.append(dict(tok))
    return out


def _align_token_indexes(
    tokens: List[Dict[str, Any]], width: int
) -> List[Dict[str, Any]]:
    if width <= 0 or len(tokens) != width:
        return tokens
    aligned: List[Dict[str, Any]] = []
    for idx, tok in enumerate(tokens):
        nxt = dict(tok)
        nxt.pop("x", None)
        nxt["col_index"] = idx
        aligned.append(nxt)
    return aligned


def _align_by_header_x(
    tokens: List[Dict[str, Any]], header_cells: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Use column order when each token's x sits on header column i.

    Trailing Rate / Amt cells are often blank. Nearest-centre assignment then
    spills the last printed qty into closing because an ignored Shortage column
    is not a band. Left-align only when x already agrees with column order.
    """
    width = len(header_cells)
    if not tokens or not width or len(tokens) > width:
        return tokens
    if len(tokens) == width:
        return _align_token_indexes(tokens, width)
    centers: List[Optional[float]] = []
    for cell in header_cells:
        raw = cell.get("x_center")
        if raw is None:
            return tokens
        centers.append(float(raw))
    if any(tok.get("x") is None for tok in tokens):
        return tokens
    for idx, tok in enumerate(tokens):
        x = float(tok.get("x"))
        nearest = min(range(width), key=lambda j: abs(float(centers[j]) - x))
        if nearest != idx:
            return tokens
    aligned: List[Dict[str, Any]] = []
    for idx, tok in enumerate(tokens):
        nxt = dict(tok)
        nxt.pop("x", None)
        nxt["col_index"] = idx
        aligned.append(nxt)
    return aligned


def _row_label(tokens: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for tok in tokens:
        text = str(tok.get("text") or "").strip()
        if not text:
            continue
        if _NUM_TOKEN.match(text.replace(",", "")):
            break
        parts.append(text)
    return " ".join(parts).strip()


def _row_numbers(tokens: List[Dict[str, Any]]) -> List[str]:
    nums: List[str] = []
    for tok in tokens:
        text = str(tok.get("text") or "").strip().replace(",", "")
        if _NUM_TOKEN.match(text):
            nums.append(text)
    return nums


def items_from_free_column_rows(
    header_cells: List[Dict[str, Any]],
    data_rows: List[List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """Map word rows whose header has Purchase Free and Sales Free.

    Returns None when that column set is not printed. Does not invent qtys.
    """
    from services.stock_native_tables import resolver_ready

    ready = resolver_ready(header_cells)
    columns = list(ready.get("columns") or [])
    if not ready.get("resolved") or not _free_column_layout(columns):
        return None
    width = len(header_cells)
    items: List[Dict[str, Any]] = []
    closing_total: Optional[float] = None
    for raw in data_rows or []:
        tokens = _expand_glued_numbers(list(raw or []))
        if not tokens:
            continue
        label = _row_label(tokens)
        numbers = _row_numbers(tokens)
        if label and _TOTAL_LABEL.search(label):
            if _AMOUNT_GRAND.search(label) and numbers:
                from services.stock_header_resolver import parse_number

                closing_total = parse_number(numbers[-1])
            continue
        if not numbers:
            if (
                items
                and label
                and not _SKIP_CONTINUATION.search(label)
                and "opening" not in label.lower()
            ):
                prev = str(items[-1].get("product_name") or "").strip()
                items[-1]["product_name"] = (prev + " " + label).strip()
            continue
        aligned = _align_by_header_x(tokens, header_cells)
        built = build_line_items_from_tokens(columns, [aligned])
        if not built:
            continue
        item = built[0]
        name = str(item.get("product_name") or "").strip()
        if not name or _TOTAL_LABEL.search(name):
            continue
        items.append(item)
    if len(items) < 3:
        return None
    return {"line_items": items, "closing_value": closing_total}


_ISSUE_LAYOUT = frozenset(
    {
        "opening_qty",
        "purchase_qty",
        "sales_qty",
        "sales_value",
        "closing_qty",
        "closing_value",
    }
)
_HEADER_TOKEN = re.compile(
    r"(?i)^(product|pack|packing|opening|receipt|issue|i/?free|sales|amount|"
    r"closing|division|qty|value|rate|stock)$"
)


def _join_wrapped_name(parts: List[str]) -> str:
    """Join a product name split across lines without inventing words.

    A broken token such as ``60TA`` + ``B)`` or ``20`` + ``0ML)`` is glued.
    Separate words stay separated.
    """
    out = ""
    for part in parts:
        piece = str(part or "").strip()
        if not piece:
            continue
        if not out:
            out = piece
            continue
        glue = False
        if piece in {")", ")"} or re.fullmatch(r"[A-Za-z]\)?", piece):
            glue = True
        elif out.endswith("(") or piece.startswith(")"):
            glue = True
        elif out[-1:].isdigit() and piece[:1].isdigit():
            glue = True
        elif "(" in out and out.count("(") > out.count(")") and piece.endswith(")"):
            glue = True
        out = out + piece if glue else out + " " + piece
    return re.sub(r"\s+", " ", out).strip()


def _issue_column_layout(columns: List[Dict[str, Any]]) -> bool:
    canons = {str(c.get("canonical") or "") for c in columns}
    return _ISSUE_LAYOUT <= canons


def _band_for_x(x: float, bands: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not bands:
        return None
    centers = [
        (float(b.get("x0") or 0) + float(b.get("x1") or 0)) / 2.0 for b in bands
    ]
    idx = 0
    for i in range(len(centers) - 1):
        edge = (centers[i] + centers[i + 1]) / 2.0
        if x >= edge:
            idx = i + 1
        else:
            break
    return bands[idx]


def _leading_number(text: str) -> Optional[float]:
    from services.stock_header_resolver import parse_number

    raw = str(text or "").strip()
    number = parse_number(raw)
    if number is not None:
        return number
    match = re.match(r"^(-?\d[\d,]*(?:\.\d+)?)\b", raw)
    if not match:
        return None
    return parse_number(match.group(1))


def _line_band_texts(
    words: List[Dict[str, Any]], bands: List[Dict[str, Any]]
) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    ordered = sorted(words, key=lambda w: float(w.get("x0") or 0))
    for word in ordered:
        text = str(word.get("text") or "").strip()
        if not text:
            continue
        x = (float(word.get("x0") or 0) + float(word.get("x1") or 0)) / 2.0
        band = _band_for_x(x, bands)
        if not band:
            continue
        canon = str(band.get("canonical") or "")
        if not canon or canon == "ignore":
            continue
        grouped.setdefault(canon, []).append(text)
    return grouped


def items_from_receipt_issue_pages(
    pdf: Any, page_results: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Opening / Receipt / Issue / Sales Amount / Closing Amount word columns.

    Wrapped product names are joined from the product column. Division text
    is not part of the name. Printed negatives stay printed.
    """
    from services.stock_native_tables import resolver_ready

    bands: List[Dict[str, Any]] = []
    for page in page_results or []:
        header = page.get("header_cells") or []
        ready = resolver_ready(header)
        columns = list(ready.get("columns") or [])
        if not ready.get("resolved") or not _issue_column_layout(columns):
            continue
        by_idx = {int(c.get("col_index") or 0): c for c in columns}
        built: List[Dict[str, Any]] = []
        for cell in header:
            col = by_idx.get(int(cell.get("col_index") or 0))
            if not col or cell.get("x0") is None:
                continue
            built.append(
                {
                    "canonical": col.get("canonical"),
                    "x0": float(cell.get("x0") or 0),
                    "x1": float(cell.get("x1") or 0),
                }
            )
        if built:
            built.sort(key=lambda b: (b["x0"] + b["x1"]) / 2.0)
            bands = built
            break
    if not bands:
        return None

    lines: List[Dict[str, Any]] = []
    for page_index, page in enumerate(getattr(pdf, "pages", []) or []):
        try:
            words = page.extract_words(keep_blank_chars=False, use_text_flow=False) or []
        except Exception:
            words = []
        ordered = sorted(words, key=lambda w: (float(w.get("top") or 0), float(w.get("x0") or 0)))
        for word in ordered:
            top = float(word.get("top") or 0)
            if lines and abs(top - float(lines[-1]["top"])) <= 2.2:
                lines[-1]["words"].append(word)
            else:
                lines.append({"top": top, "page": page_index, "words": [word]})

    header_tops = []
    for line in lines:
        blob = " ".join(str(w.get("text") or "") for w in line["words"])
        if re.search(r"\bOpening\b", blob) and re.search(r"\bProduct\b", blob):
            header_tops.append(float(line["top"]))

    anchors: List[Dict[str, Any]] = []
    for line in lines:
        grouped = _line_band_texts(line["words"], bands)
        opening_bits = grouped.get("opening_qty") or []
        opening = next(
            (n for n in (_leading_number(bit) for bit in opening_bits) if n is not None),
            None,
        )
        if opening is None:
            continue
        anchors.append({"top": float(line["top"]), "page": line["page"], "grouped": grouped, "opening": opening})

    if len(anchors) < 3:
        return None

    items: List[Dict[str, Any]] = []
    closing_total: Optional[float] = None
    sales_total: Optional[float] = None
    for index, anchor in enumerate(anchors):
        prev_top = (
            float(anchors[index - 1]["top"])
            if index and anchors[index - 1]["page"] == anchor["page"]
            else float(anchor["top"]) - 28.0
        )
        next_top = (
            float(anchors[index + 1]["top"])
            if index + 1 < len(anchors) and anchors[index + 1]["page"] == anchor["page"]
            else float(anchor["top"]) + 28.0
        )
        if index == 0 or anchors[index - 1]["page"] != anchor["page"]:
            page_headers = [t for t in header_tops if t < float(anchor["top"])]
            if page_headers:
                prev_top = max(page_headers)
        lo = (prev_top + float(anchor["top"])) / 2.0
        hi = (float(anchor["top"]) + next_top) / 2.0
        name_parts: List[str] = []
        pack_parts: List[str] = []
        for line in lines:
            if line["page"] != anchor["page"]:
                continue
            top = float(line["top"])
            if top < lo or top > hi:
                continue
            grouped = _line_band_texts(line["words"], bands)
            for text in grouped.get("product_name") or []:
                if _HEADER_TOKEN.match(text.strip()):
                    continue
                name_parts.append(text)
            for text in grouped.get("pack") or []:
                if _HEADER_TOKEN.match(text.strip()):
                    continue
                pack_parts.append(text)
        name = _join_wrapped_name(name_parts)
        pack = re.sub(r"\s+", " ", " ".join(pack_parts)).strip()
        grouped = anchor["grouped"]

        def _band_number(canon: str) -> Optional[float]:
            for bit in grouped.get(canon) or []:
                number = _leading_number(bit)
                if number is not None:
                    return number
            return None

        if not re.search(r"[A-Za-z]", name):
            closing_total = _band_number("closing_value")
            sales_total = _band_number("sales_value")
            continue
        item = _empty_line_item()
        item["product_name"] = name
        item["packing"] = pack or None
        item["opening_qty"] = anchor["opening"]
        item["receipts_qty"] = _band_number("purchase_qty")
        item["sales_qty"] = _band_number("sales_qty")
        item["sales_value"] = _band_number("sales_value")
        item["closing_qty"] = _band_number("closing_qty")
        item["closing_value"] = _band_number("closing_value")
        free_out = _band_number("free_out_qty")
        if free_out is not None:
            item["extra"]["free_out_qty"] = free_out
        items.append(item)
    if len(items) < 3:
        return None
    return {
        "line_items": items,
        "closing_value": closing_total,
        "sales_value": sales_total,
    }


_LMS_QTY_LAYOUT = frozenset(
    {
        "opening_qty",
        "purchase_qty",
        "lms",
        "sales_qty",
        "closing_qty",
    }
)
_DASH_CELL = {"-", "--", "—", "–"}


def _lms_qty_layout(columns: List[Dict[str, Any]]) -> bool:
    canons = {str(c.get("canonical") or "") for c in columns}
    if not _LMS_QTY_LAYOUT <= canons:
        return False
    if canons & {"closing_value", "sales_value", "free_in_qty", "free_out_qty"}:
        return False
    return True


def items_from_lms_qty_rows(
    header_cells: List[Dict[str, Any]],
    data_rows: List[List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """PRODUCT / OPENING / PURCHASE / LMS / SALES / CLOSING, qty only.

    LMS is last-month sales and stays out of sales_qty. The unlabeled pack
    sits left of the opening header. A row of money with no product name is
    the statement total.
    """
    from services.stock_header_resolver import parse_number
    from services.stock_native_tables import resolver_ready

    ready = resolver_ready(header_cells)
    columns = list(ready.get("columns") or [])
    if not ready.get("resolved") or not _lms_qty_layout(columns):
        return None
    by_idx = {int(c.get("col_index") or 0): c for c in columns}
    bands: List[Dict[str, Any]] = []
    for cell in header_cells:
        col = by_idx.get(int(cell.get("col_index") or 0))
        if not col or cell.get("x_center") is None:
            continue
        bands.append(
            {
                "canonical": str(col.get("canonical") or ""),
                "x_center": float(cell.get("x_center") or 0),
                "x0": float(cell.get("x0") or 0),
                "x1": float(cell.get("x1") or 0),
            }
        )
    opening = next((b for b in bands if b["canonical"] == "opening_qty"), None)
    if not opening or float(opening["x_center"]) <= 0:
        return None
    width = ((float(opening["x0"]) + float(opening["x1"])) / 2.0) / float(
        opening["x_center"]
    )
    open_left = float(opening["x0"]) / width if width else float(opening["x_center"])

    def _nearest(x: float) -> Dict[str, Any]:
        return min(bands, key=lambda b: abs(float(b["x_center"]) - x))

    qty_canons = {"opening_qty", "purchase_qty", "sales_qty", "closing_qty", "lms"}
    items: List[Dict[str, Any]] = []
    sales_total: Optional[float] = None
    closing_total: Optional[float] = None
    for raw in data_rows or []:
        tokens = [t for t in (raw or []) if str(t.get("text") or "").strip()]
        if not tokens:
            continue
        texts = [str(t.get("text") or "").strip() for t in tokens]
        if not any(re.search(r"[A-Za-z]", text) for text in texts):
            for tok in tokens:
                text = str(tok.get("text") or "").strip().replace(",", "")
                if tok.get("x") is None or not re.fullmatch(r"-?\d+\.\d+", text):
                    continue
                canon = str(_nearest(float(tok["x"])).get("canonical") or "")
                number = parse_number(text)
                if number is None:
                    continue
                if canon == "sales_qty":
                    sales_total = number
                elif canon == "closing_qty":
                    closing_total = number
            continue
        left: List[Tuple[float, str]] = []
        nums: Dict[str, float] = {}
        for tok in tokens:
            text = str(tok.get("text") or "").strip()
            if tok.get("x") is None:
                continue
            xf = float(tok["x"])
            if xf < open_left - 0.01:
                left.append((xf, text))
                continue
            canon = str(_nearest(xf).get("canonical") or "")
            if canon not in qty_canons:
                continue
            if text in _DASH_CELL:
                nums[canon] = 0.0
                continue
            number = parse_number(text.replace(",", ""))
            if number is not None:
                nums[canon] = number
        if "opening_qty" not in nums or not left:
            continue
        left.sort()
        if len(left) >= 2:
            pack = left[-1][1]
            name = " ".join(part for _, part in left[:-1])
        else:
            pack = None
            name = left[0][1]
        name = re.sub(r"\s+", " ", name).strip()
        if not re.search(r"[A-Za-z]{3,}", name):
            continue
        if re.search(r"(?i)^(sales\s+amount|stock\s+value|last\s+period)\b", name):
            continue
        item = _empty_line_item()
        item["product_name"] = name
        item["packing"] = pack
        item["opening_qty"] = nums.get("opening_qty")
        item["receipts_qty"] = nums.get("purchase_qty")
        item["sales_qty"] = nums.get("sales_qty")
        item["closing_qty"] = nums.get("closing_qty")
        if "lms" in nums:
            item["extra"]["lms_qty"] = nums["lms"]
        items.append(item)
    if len(items) < 3:
        return None
    logger.info(
        "STOCK_PDF_WORDS_LMS_QTY items=%s sales_value=%s closing_value=%s",
        len(items),
        sales_total,
        closing_total,
    )
    return {
        "line_items": items,
        "closing_value": closing_total,
        "sales_value": sales_total,
        "letterhead_is_stockist_only": True,
    }


_OPEN_RCVD_LAYOUT = frozenset(
    {
        "opening_qty",
        "purchase_qty",
        "free_in_qty",
        "sales_qty",
        "free_out_qty",
        "closing_qty",
    }
)
_PACK_TAIL = re.compile(r"(?i)^(?P<name>.*?)\s+(?P<pack>\d+\s*X\s*\S+)\s*$")
_SKIP_WORD_LINE = re.compile(
    r"(?i)^(generated\b|page\b|company\b|from\b|sale\s+and\s+stock|phone\b|stock\s+value|code$)"
)


def _open_rcvd_layout(columns: List[Dict[str, Any]]) -> bool:
    canons = {str(c.get("canonical") or "") for c in columns}
    if not _OPEN_RCVD_LAYOUT <= canons:
        return False
    if canons & {"closing_value", "sales_value", "lms"}:
        return False
    return True


def _header_bands(header_cells: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from services.stock_native_tables import resolver_ready

    ready = resolver_ready(header_cells)
    columns = list(ready.get("columns") or [])
    if not ready.get("resolved"):
        return []
    by_idx = {int(c.get("col_index") or 0): c for c in columns}
    bands: List[Dict[str, Any]] = []
    for cell in header_cells:
        col = by_idx.get(int(cell.get("col_index") or 0))
        if not col or cell.get("x0") is None:
            continue
        bands.append(
            {
                "canonical": str(col.get("canonical") or ""),
                "header": str(cell.get("text") or ""),
                "x0": float(cell.get("x0") or 0),
                "x1": float(cell.get("x1") or 0),
            }
        )
    return bands


def _band_at(x: float, bands: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not bands:
        return None
    inside = [b for b in bands if float(b["x0"]) - 2.0 <= x <= float(b["x1"]) + 2.0]
    pool = inside or bands

    def _center(band: Dict[str, Any]) -> float:
        return (float(band["x0"]) + float(band["x1"])) / 2.0

    return min(pool, key=lambda b: abs(_center(b) - x))


def _value_summary_from_pages(
    page_results: List[Dict[str, Any]],
) -> Dict[str, Optional[float]]:
    """Opening / receive / sold / closing value block on a summary page."""
    summary: Dict[str, Optional[float]] = {}
    for page in page_results or []:
        header = page.get("header_cells") or []
        blob = " ".join(str(c.get("text") or "") for c in header).lower()
        if "opening" not in blob or "closing" not in blob or "value" not in blob:
            continue
        bands = []
        for cell in header:
            text = str(cell.get("text") or "")
            if cell.get("x_center") is None:
                continue
            bands.append(
                {
                    "header": text.lower(),
                    "x_center": float(cell.get("x_center") or 0),
                    "x0": float(cell.get("x0") or 0),
                    "x1": float(cell.get("x1") or 0),
                }
            )
        if not bands:
            continue
        width = 0.0
        for band in bands:
            xc = float(band["x_center"] or 0)
            if xc:
                width = ((float(band["x0"]) + float(band["x1"])) / 2.0) / xc
                break
        chosen: Dict[str, Tuple[float, float, str]] = {}
        for row in page.get("data_rows") or []:
            for tok in row:
                raw = str(tok.get("text") or "").strip()
                if tok.get("x") is None:
                    continue
                number = _leading_number(raw)
                if number is None:
                    continue
                xf = float(tok["x"])
                abs_x = xf * width if width else xf
                inside = [
                    b
                    for b in bands
                    if float(b["x0"]) - 3.0 <= abs_x <= float(b["x1"]) + 3.0
                ]
                pool = inside or bands
                band = min(
                    pool,
                    key=lambda b: abs(
                        ((float(b["x0"]) + float(b["x1"])) / 2.0) - abs_x
                    ),
                )
                header = str(band["header"])
                if "opening" in header:
                    key = "opening_value"
                elif "receive" in header or "rcvd" in header:
                    key = "receipts_value"
                elif "sold" in header or header.startswith("sale"):
                    key = "sales_value" if "ret" not in header and "rtn" not in header else "sale_return_value"
                elif "prch" in header or "purchase" in header:
                    key = "purchase_return_value"
                elif "expir" in header:
                    key = "expiry_value"
                elif "closing" in header or "clos" in header:
                    key = "closing_value"
                else:
                    continue
                dist = abs(float(band["x_center"]) - xf)
                prev = chosen.get(key)
                prefer = "ptr" in raw.lower()
                if prev is None or prefer or (dist < prev[0] and "ptr" not in prev[2].lower()):
                    chosen[key] = (dist, number, raw)
        for key, (_dist, number, _raw) in chosen.items():
            summary[key] = number
        if summary.get("closing_value") is not None:
            break
    return summary


def items_from_open_rcvd_sold_pages(
    pdf: Any, page_results: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """HSN / product / Open / Rcvd / Free / Sold / Clos qty columns.

    Total Rcvd and Total Sold are subtotals and are not copied into a second
    qty. An empty qty cell stays missing. HSN is the product code.
    """
    bands: List[Dict[str, Any]] = []
    qty_pages = set()
    for index, page in enumerate(page_results or []):
        header = page.get("header_cells") or []
        built = _header_bands(header)
        canons = [b["canonical"] for b in built]
        if not _open_rcvd_layout([{"canonical": c} for c in canons]):
            continue
        if not bands:
            bands = built
        qty_pages.add(index)
    if not bands:
        return None
    opening = next((b for b in bands if b["canonical"] == "opening_qty"), None)
    if not opening:
        return None
    open_left = float(opening["x0"])
    qty_canons = {
        "opening_qty": "opening_qty",
        "purchase_qty": "receipts_qty",
        "sales_qty": "sales_qty",
        "closing_qty": "closing_qty",
    }
    extra_canons = {
        "free_in_qty": "free_in_qty",
        "free_out_qty": "free_out_qty",
        "sales_return_qty": "sale_return",
        "purchase_return_qty": "purchase_return",
        "expiry_damage_qty": "exp_damage",
    }

    lines: List[Dict[str, Any]] = []
    for page_index, page in enumerate(getattr(pdf, "pages", []) or []):
        if page_index not in qty_pages:
            continue
        try:
            words = page.extract_words(x_tolerance=1, y_tolerance=2, keep_blank_chars=False) or []
        except Exception:
            words = []
        ordered = sorted(words, key=lambda w: (float(w.get("top") or 0), float(w.get("x0") or 0)))
        for word in ordered:
            top = float(word.get("top") or 0)
            if lines and lines[-1]["page"] == page_index and abs(top - float(lines[-1]["top"])) <= 2.2:
                lines[-1]["words"].append(word)
            else:
                lines.append({"top": top, "page": page_index, "words": [word]})

    items: List[Dict[str, Any]] = []
    pending_parts: List[str] = []

    def _finish() -> None:
        if not items or not pending_parts:
            return
        text = re.sub(r"\s+", " ", " ".join(pending_parts)).strip()
        pending_parts.clear()
        match = _PACK_TAIL.match(text)
        if match and re.search(r"[A-Za-z]{3,}", match.group("name") or ""):
            items[-1]["product_name"] = re.sub(r"\s+", " ", match.group("name")).strip()
            items[-1]["packing"] = re.sub(r"\s+", " ", match.group("pack")).strip()
        else:
            items[-1]["product_name"] = text

    for line in lines:
        words = line["words"]
        blob = " ".join(str(w.get("text") or "") for w in words).strip()
        if not blob or _SKIP_WORD_LINE.match(blob):
            continue
        if re.search(r"(?i)\b(openqty|productname|closqty|hsn)\b", blob):
            continue
        hsn = next(
            (
                str(w.get("text") or "").strip()
                for w in words
                if re.fullmatch(r"\d{6,8}", str(w.get("text") or "").strip())
                and float(w.get("x0") or 0) < open_left
            ),
            None,
        )
        name_bits: List[str] = []
        nums: Dict[str, float] = {}
        extras: Dict[str, float] = {}
        for word in words:
            text = str(word.get("text") or "").strip()
            if not text or text == hsn:
                continue
            x = (float(word.get("x0") or 0) + float(word.get("x1") or 0)) / 2.0
            if x < open_left:
                name_bits.append(text)
                continue
            band = _band_at(x, bands)
            if not band:
                continue
            canon = str(band.get("canonical") or "")
            number = _leading_number(text)
            if number is None:
                continue
            if canon in qty_canons:
                nums[qty_canons[canon]] = number
            elif canon in extra_canons:
                extras[extra_canons[canon]] = number
        if hsn and (name_bits or nums):
            _finish()
            item = _empty_line_item()
            item["product_code"] = hsn
            item["opening_qty"] = nums.get("opening_qty")
            item["receipts_qty"] = nums.get("receipts_qty")
            item["sales_qty"] = nums.get("sales_qty")
            item["closing_qty"] = nums.get("closing_qty")
            for key, value in extras.items():
                item["extra"][key] = value
            items.append(item)
            pending_parts = name_bits
            continue
        if items and name_bits and not nums:
            pending_parts.extend(name_bits)
    _finish()
    items = [i for i in items if re.search(r"[A-Za-z]{3,}", str(i.get("product_name") or ""))]
    if len(items) < 3:
        return None
    summary = _value_summary_from_pages(page_results)
    logger.info(
        "STOCK_PDF_WORDS_OPEN_RCVD items=%s sales_value=%s closing_value=%s",
        len(items),
        summary.get("sales_value"),
        summary.get("closing_value"),
    )
    return {
        "line_items": items,
        "sales_value": summary.get("sales_value"),
        "closing_value": summary.get("closing_value"),
        "value_summary": summary,
        "letterhead_is_stockist_only": True,
    }


def _grouped_qty_value_enabled() -> bool:
    return os.getenv("STOCK_PDF_GROUP_QTY_VALUE", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _drop_grouped_banner_item(item: Dict[str, Any]) -> bool:
    name = str(item.get("product_name") or "").strip()
    if not name:
        return True
    if re.match(r"^(?:GRAND\s+)?TOTAL\b", name, re.I):
        return True
    if re.search(
        r"stock\s+statement|^\s*date\b|^\s*page\b|wellness\s+company|"
        r"\bopening\b.*\bpurchase\b|\bqty\b.*\bvalue\b",
        name,
        re.I,
    ):
        return True
    return False


def parse_grouped_qty_value_pdf(
    file_bytes: bytes, filename: str
) -> Optional[Dict[str, Any]]:
    """Product | Rate | Opening/Purchase/Sales/Free/Closing Qty+value by word x.

    The group label sits above a Qty/value pair. Reading the group row alone
    shifts Rate into opening_qty. Returns None unless that two-row header is present.
    """
    import io

    if not _grouped_qty_value_enabled():
        return None
    try:
        import pdfplumber
    except Exception:
        return None
    from services.sales_statement_extractor import empty_result
    from services.stock_native_tables import from_pdf_words

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            if not pdf.pages:
                return None
            built = from_pdf_words(pdf)
            peek = ""
            if pdf.pages:
                peek = pdf.pages[0].extract_text() or ""
    except Exception as exc:
        logger.info(
            "STOCK_PDF_GROUP_QTY_VALUE file=%s status=skipped error=%s",
            filename,
            type(exc).__name__,
        )
        return None
    pages = built.get("page_results") or []
    if not any(p.get("group_qty_value_header") for p in pages):
        return None
    items = [
        item
        for item in build_items_from_pdf_page_results(pages)
        if isinstance(item, dict) and not _drop_grouped_banner_item(item)
    ]
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if item.get("receipts_value") in (None, "") and extra.get("receipts_value") not in (
            None,
            "",
        ):
            item["receipts_value"] = extra.get("receipts_value")
        key = (
            _norm_name(item.get("product_name")),
            item.get("opening_qty"),
            item.get("receipts_qty"),
            item.get("sales_qty"),
            item.get("closing_qty"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    if len(deduped) < 3:
        logger.info(
            "STOCK_PDF_GROUP_QTY_VALUE file=%s status=too_few items=%s",
            filename,
            len(deduped),
        )
        return None
    result = empty_result(filename, "pdf")
    result["line_items"] = deduped
    result["report_title"] = "Stock Statement"
    first = ""
    for line in peek.splitlines():
        if line.strip():
            first = line.strip()
            break
    if first and re.search(r"[A-Za-z]", first) and not re.search(
        r"stock\s+statement", first, re.I
    ):
        result["stockist_name"] = first
    company = re.search(
        r"((?:[A-Z][A-Z&. ]{2,})\s+COMPANY(?:\s+[A-Z][A-Z]+)?)",
        peek,
    )
    if company:
        result["company_name"] = re.sub(r"\s+", " ", company.group(1)).strip()
    period = re.search(
        r"(?:From|Duration)\s*:?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\s+"
        r"(?:To|to)\s*:?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})",
        peek,
        re.I,
    )
    if period:
        def _iso(day: str, month: str, year: str) -> str:
            yr = year if len(year) == 4 else ("20" + year)
            return f"{yr}-{int(month):02d}-{int(day):02d}"

        result["period_from"] = _iso(period.group(1), period.group(2), period.group(3))
        result["period_to"] = _iso(period.group(4), period.group(5), period.group(6))
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["extraction_method"] = "pdf_group_qty_value"
    extra["statement_count"] = 1
    logger.info(
        "STOCK_PDF_GROUP_QTY_VALUE file=%s items=%s period=%s..%s",
        filename,
        len(deduped),
        result.get("period_from"),
        result.get("period_to"),
    )
    return result


def parse_pdf_free_column_statement(
    file_bytes: bytes, filename: str
) -> Optional[Dict[str, Any]]:
    """Text PDF whose words print Purchase Free and Sales Free as their own columns."""
    import io

    try:
        import pdfplumber
    except Exception:
        return None
    from services.sales_statement_extractor import empty_result
    from services.stock_native_tables import from_pdf_words

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            built = from_pdf_words(pdf)
            peek = ""
            if pdf.pages:
                peek = pdf.pages[0].extract_text() or ""
            parsed = items_from_free_column_rows(
                built.get("header_cells") or [],
                built.get("data_rows") or [],
            )
            if not parsed or not parsed.get("line_items"):
                parsed = items_from_receipt_issue_pages(
                    pdf, built.get("page_results") or []
                )
            if not parsed or not parsed.get("line_items"):
                parsed = items_from_lms_qty_rows(
                    built.get("header_cells") or [],
                    built.get("data_rows") or [],
                )
            if not parsed or not parsed.get("line_items"):
                parsed = items_from_open_rcvd_sold_pages(
                    pdf, built.get("page_results") or []
                )
            if (
                parsed
                and parsed.get("letterhead_is_stockist_only")
                and pdf.pages
            ):
                peek = pdf.pages[0].extract_text(x_tolerance=1, y_tolerance=3) or peek
    except Exception as exc:
        logger.info(
            "STOCK_PDF_WORDS_TABLE file=%s status=skipped error=%s",
            filename,
            type(exc).__name__,
        )
        return None
    if not parsed or not parsed.get("line_items"):
        return None
    result = empty_result(filename, "pdf")
    result["line_items"] = parsed["line_items"]
    if re.search(r"Sale\s+And\s+Stock\s+Statement", peek, re.I):
        result["report_title"] = "Sale And Stock Statement"
    elif re.search(r"Stock\s+Statement", peek, re.I):
        result["report_title"] = "Stock Statement Report"
    elif re.search(r"Statements\s+of", peek, re.I):
        result["report_title"] = "Statements of"
    first = ""
    for line in peek.splitlines():
        if line.strip():
            first = line.strip()
            break
    stated = re.search(r"Statements\s+of\s+(.+)", peek, re.I)
    company = re.search(r"Company\s*:\s*(.+)", peek, re.I)
    if stated:
        result["stockist_name"] = stated.group(1).strip()
    elif first and re.search(r"[A-Za-z]", first) and not re.search(r"\d{2,}", first):
        result["stockist_name"] = first
        if not parsed.get("letterhead_is_stockist_only"):
            result["company_name"] = first
    if company:
        result["company_name"] = company.group(1).strip()
    period = re.search(
        r"(?:From|Duration)\s*:?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\s+"
        r"(?:To|to)\s*:?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})",
        peek,
        re.I,
    )
    if period:
        def _iso(day: str, month: str, year: str) -> str:
            yr = year if len(year) == 4 else ("20" + year)
            return f"{yr}-{int(month):02d}-{int(day):02d}"

        result["period_from"] = _iso(period.group(1), period.group(2), period.group(3))
        result["period_to"] = _iso(period.group(4), period.group(5), period.group(6))
    if not result.get("period_from"):
        compact = re.search(
            r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})\s*[-–]\s*"
            r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})",
            peek,
        )
        if compact:
            def _iso_compact(day: str, month: str, year: str) -> str:
                return f"{year}-{int(month):02d}-{int(day):02d}"

            result["period_from"] = _iso_compact(
                compact.group(1), compact.group(2), compact.group(3)
            )
            result["period_to"] = _iso_compact(
                compact.group(4), compact.group(5), compact.group(6)
            )
    if parsed.get("letterhead_is_stockist_only"):
        lines = [ln.strip() for ln in peek.splitlines() if ln.strip()]
        if (
            len(lines) >= 2
            and re.search(r"[A-Za-z]", lines[1])
            and not re.search(r"\d{1,2}[./-]\d{1,2}[./-]\d{4}", lines[1])
            and "PRODUCT" not in lines[1].upper()
        ):
            result["stockist_address"] = lines[1]
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["extraction_method"] = "pdf_words_free_columns"
    extra["statement_count"] = 1
    if parsed.get("closing_value") is not None:
        result["totals"]["closing_value"] = parsed["closing_value"]
    if parsed.get("sales_value") is not None:
        result["totals"]["sales_value"] = parsed["sales_value"]
    for key, value in (parsed.get("value_summary") or {}).items():
        if key in {"sales_value", "closing_value"} or value is None:
            continue
        extra[key] = value
    logger.info(
        "STOCK_PDF_WORDS_TABLE file=%s items=%s closing_value=%s",
        filename,
        len(result["line_items"]),
        result["totals"].get("closing_value"),
    )
    return result
