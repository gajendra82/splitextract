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
    "product_code": ("product_code", False),
    "pack": ("packing", False),
    "batch": ("batch", True),
    "opening_qty": ("opening_qty", False),
    "opening_value": ("opening_value", False),
    "purchase_qty": ("receipts_qty", False),
    # Top-level like opening_value/sales_value/closing_value; also mirrored
    # into extra.receipts_value for consumers that read money from extra.
    "purchase_value": ("receipts_value", False),
    "sales_qty": ("sales_qty", False),
    "sales_value": ("sales_value", False),
    "closing_qty": ("closing_qty", False),
    "closing_value": ("closing_value", False),
    "sales_return_qty": ("sale_return", True),
    "purchase_return_qty": ("purchase_return", True),
    "expiry_damage_qty": ("exp_damage", True),
    "other_outward_qty": ("others_qty", True),
    "lms": ("lms", True),
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
    if "totalvalue" in key or key.startswith("grandtotal"):
        return True
    if key.startswith("grouptotal"):
        return True
    return key in {
        "total",
        "totals",
        "grandtotal",
        "grandtotals",
        "grouptotal",
        "grouptotals",
        "subtotal",
        "subtotals",
        "pagetotal",
        "pagetotals",
        "gtotal",
        "gtotals",
    } or (key.startswith("total") and len(key) <= 12)


_SHEET_TOTAL_NAME = re.compile(
    r"(?:^|\b)(?:grand\s+|sub\s+|division\s+|company\s+|page\s+|group\s+)?"
    r"total\s+value\b"
    r"|^\s*(?:amount|qty|quantity)\s+(?:grand\s+)?total\b"
    r"|^\s*(?:division|company|group|firm)\s+totals?"
    r"(?:\s*(?:amt|amount|value))?\b"
    r"|^\s*grand\s+totals?(?:\s*(?:amt|amount|value))?\b"
    r"|^\s*group\s*totals?\b"
    r"|total\s+for\b"
    r"|^\s*total\s*count\b"
    r"|^\s*total\s*:-",
    re.I,
)


_SECTION_PACK_WORD = re.compile(
    r"(?i)\b(?:tabs?|syp|syup|syrup|ml|mg|gm|grm|caps?|gel|oint|cream|drops?|"
    r"inj|balm|soap|lozenges?)\b"
)


def _is_section_banner_item(item: Dict[str, Any]) -> bool:
    """Group label with no printed opening, issue, or closing quantity.

    Receipts of 0 do not make the row a product. A name that still carries a
    pack word (TAB, ML, SYP) is left alone.
    """
    if not isinstance(item, dict):
        return False
    if item.get("opening_qty") not in (None, ""):
        return False
    if item.get("sales_qty") not in (None, ""):
        return False
    if item.get("closing_qty") not in (None, ""):
        return False
    if item.get("receipts_qty") not in (None, "", 0, 0.0):
        return False
    name = str(item.get("product_name") or "")
    if _SECTION_PACK_WORD.search(name):
        return False
    return bool(re.search(r"[A-Za-z]", name))


def _brand_section_pending_skip_enabled() -> bool:
    """STOCK_PDF_WORDS_SKIP_BRAND_SECTION_PENDING — HIMALAYA ZENITH above SKUs.

    Op.Qnt/Pur/Sl/Cl STOCK REPORT prints a brand/division line with no numbers
    above the first product. Treating that line as a wrapped product title
    prepended it to the next SKU, then ``^himalaya`` dropped the whole row.
    Default ON. Kill switch STOCK_PDF_WORDS_SKIP_BRAND_SECTION_PENDING=false.
    """
    return os.getenv(
        "STOCK_PDF_WORDS_SKIP_BRAND_SECTION_PENDING", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


_BRAND_SECTION_PREFIX_RE = re.compile(r"(?i)^hima(?:laya|lya)\b")


def _is_brand_section_pending_name(frag: Any) -> bool:
    """Manufacturer / division banner — not a wrapped SKU title fragment."""
    s = re.sub(r"\s+", " ", str(frag or "")).strip()
    if not s or re.search(r"\d", s) or _SECTION_PACK_WORD.search(s):
        return False
    words = s.split()
    if not (2 <= len(words) <= 4):
        return False
    # HIMALAYA ZENITH / printed misspelling HIMALYA ZANDRA|DRUG|ZINDEL.
    return bool(_BRAND_SECTION_PREFIX_RE.match(s))


def _is_sheet_total_name(name: Any) -> bool:
    """Division / company / value totals — not a product 'Total :' lot row."""
    text = str(name or "").strip()
    if not text:
        return False
    if _SHEET_TOTAL_NAME.search(text):
        return True
    key = _norm_name(text).lower()
    return "totalvalue" in key


def _footer_row_label_blob(tokens: List[Dict[str, Any]]) -> str:
    parts = [
        str(t.get("text") or "").strip()
        for t in (tokens or [])
        if isinstance(t, dict) and str(t.get("text") or "").strip()
    ]
    return " ".join(parts)


def _footer_totals_from_page_results(
    page_results: List[Dict[str, Any]],
) -> Dict[str, float]:
    """Copy printed TOTAL QUANTITY / TOTAL VALUE cells onto statement totals.

    Column assignment uses the same header bands as product rows. TOTAL VALUE
    and GroupTotal money land in qty canons because the header is qty; remap
    those to values. When several TOTAL / GRAND TOTAL / GroupTotal bands print,
    prefer the last GRAND TOTAL; else the last GroupTotal / value footer.
    """
    from services.stock_header_resolver import assign_cells
    from services.stock_native_tables import (
        _footer_totals_enabled,
        _sheet_total_row_kind,
        resolver_ready,
    )

    if not _footer_totals_enabled():
        return {}
    # (priority, order, fragment) — higher priority wins; last among equals.
    # GRAND TOTAL = 2, GroupTotal / TOTAL VALUE = 1, qty TOTAL = 0.
    candidates: List[Tuple[int, int, Dict[str, float]]] = []
    order = 0
    for page in page_results or []:
        ready = resolver_ready(page.get("header_cells") or [])
        columns = ready.get("columns") or []
        if not columns:
            continue
        for row in page.get("data_rows") or []:
            kind = _sheet_total_row_kind(row)
            if not kind:
                continue
            assigned = assign_cells(row, columns)
            item = fields_to_line_item(assigned.get("fields") or {})
            frag: Dict[str, float] = {}
            label = _footer_row_label_blob(row)
            is_group_total = bool(re.search(r"(?i)\bgroup\s*totals?\b", label))

            def _footer_num(raw: Any) -> Optional[float]:
                if raw in (None, ""):
                    return None
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return None

            # Printed money columns (AMOUNT-I / AMOUNT-II, Sale Value, …).
            # Harvest these first so a nameless qty+value footer is never
            # remapped as if SALE/CL.STOCK qty were rupees.
            extra_item = (
                item.get("extra") if isinstance(item.get("extra"), dict) else {}
            )
            printed_values: Dict[str, float] = {}
            for src in (
                "opening_value",
                "receipts_value",
                "sales_value",
                "closing_value",
            ):
                val = _footer_num(item.get(src))
                if val is None:
                    val = _footer_num(extra_item.get(src))
                if val is not None:
                    printed_values[src] = val
            has_paired_value_cols = bool(
                printed_values.get("sales_value") is not None
                or printed_values.get("closing_value") is not None
                or printed_values.get("opening_value") is not None
                or printed_values.get("receipts_value") is not None
            )

            # Qty columns always keep qty identity when value columns exist.
            # Pure money-under-qty-header footers (no value canons) still remap.
            if kind == "value" and not has_paired_value_cols:
                # GroupTotal prints money under Opening/Received/Issued/Closing;
                # Issued is sales-at-cost, while "Sales Value On prate" often
                # sits in the trailing free column.
                sales_dest = (
                    "sales_issue_value" if is_group_total else "sales_value"
                )
                mapping = (
                    ("opening_qty", "opening_value"),
                    ("receipts_qty", "receipts_value"),
                    ("sales_qty", sales_dest),
                    ("closing_qty", "closing_value"),
                )
            else:
                mapping = (
                    ("opening_qty", "opening_qty"),
                    ("receipts_qty", "receipts_qty"),
                    ("sales_qty", "sales_qty"),
                    ("closing_qty", "closing_qty"),
                )
            for src, dest in mapping:
                val = _footer_num(item.get(src))
                if val is None:
                    continue
                frag[dest] = val
            for src, val in printed_values.items():
                frag[src] = val
            if kind == "value" and is_group_total and frag.get("sales_value") is None:
                free_val = None
                for key in ("free_out_qty", "free_in_qty", "sample_qty"):
                    raw = item.get(key)
                    if raw in (None, ""):
                        raw = extra_item.get(key)
                    free_val = _footer_num(raw)
                    if free_val is not None:
                        break
                if free_val is not None and abs(free_val) >= 1.0:
                    frag["sales_value"] = free_val
            if re.search(r"(?i)\bgrand\s*totals?\b", label):
                priority = 2
            elif is_group_total or kind == "value":
                priority = 1
            else:
                priority = 0
            candidates.append((priority, order, frag))
            order += 1
            logger.info(
                "STOCK_PDF_WORDS_FOOTER_TOTALS kind=%s paired_values=%s "
                "priority=%s opening=%s receipts=%s sales_qty=%s sales_value=%s "
                "closing_qty=%s closing_value=%s",
                kind,
                has_paired_value_cols,
                priority,
                frag.get("opening_qty", frag.get("opening_value")),
                frag.get("receipts_qty", frag.get("receipts_value")),
                frag.get("sales_qty"),
                frag.get("sales_value", frag.get("sales_issue_value")),
                frag.get("closing_qty"),
                frag.get("closing_value"),
            )
    if not candidates:
        return {}
    best_priority = max(c[0] for c in candidates)
    chosen = next(
        frag
        for pri, _ord, frag in reversed(candidates)
        if pri == best_priority
    )
    out = dict(chosen)
    # Promote issue-band money only when Sales Value On prate was absent.
    if out.get("sales_value") is None and out.get("sales_issue_value") is not None:
        out["sales_value"] = out["sales_issue_value"]
    return out


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
        "opening_value": None,
        "receipts_qty": None,
        "receipts_value": None,
        "sales_qty": None,
        "sales_value": None,
        "closing_qty": None,
        "closing_value": None,
        "extra": {},
    }


_DASH_QTY_ZERO = frozenset({"-", "--", "—", "–"})
_DASH_QTY_CANONS = frozenset(
    {
        "opening_qty",
        "purchase_qty",
        "sales_qty",
        "closing_qty",
        "sales_return_qty",
        "purchase_return_qty",
        "expiry_damage_qty",
        "free_in_qty",
        "free_out_qty",
        "total_qty",
        "order_qty",
        "lms",
        "other_outward_qty",
    }
)


def _native_dash_qty_zero_enabled() -> bool:
    """STOCK_NATIVE_DASH_QTY_ZERO — printed '-' in a qty column is 0.

    Particulars/Open/Purch/Sales&DC/Misc Out/Cls Stock PDFs print '-' for a
    zero movement. Kill switch: false. Default ON.
    """
    return os.getenv("STOCK_NATIVE_DASH_QTY_ZERO", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
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
    dash_qty_zero = _native_dash_qty_zero_enabled()

    for canon, raw in (fields or {}).items():
        mapping = _CANON_TO_ITEM.get(canon)
        if not mapping:
            continue
        dest, into_extra = mapping
        if canon == "product_name":
            item["product_name"] = str(raw).strip() if raw not in (None, "") else None
            continue
        if canon == "product_code":
            code = str(raw).strip() if raw not in (None, "") else None
            if code:
                item["product_code"] = code
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
        raw_s = str(raw).strip()
        if dash_qty_zero and canon in _DASH_QTY_CANONS and raw_s in _DASH_QTY_ZERO:
            number = 0.0
            logger.info(
                "STOCK_NATIVE_DASH_QTY_ZERO canon=%s dest=%s",
                canon,
                dest,
            )
        else:
            number = parse_number(raw)
        if number is None and raw_s.startswith("="):
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
            # Keep sale_return on the line_item contract as well as extra.
            if dest == "sale_return" and number is not None:
                item["sale_return"] = number
        else:
            item[dest] = number
            if dest == "receipts_value" and number is not None:
                extra["receipts_value"] = number
                extra["purchase_value"] = number

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
    _peel_leading_product_code(item)
    _peel_trailing_pack_from_product_code(item)
    _peel_trailing_rate_garbage_from_product_name(item)
    _recover_unit_rate_from_printed_pairs(item)
    return item


def _recover_unit_rate_enabled() -> bool:
    """STOCK_PDF_RECOVER_UNIT_RATE — fill unit_rate from printed value÷qty.

    When the Rate cell is OCR garbage (unparseable) but Opening/Sales/Closing
    value÷qty agree, store that consensus as extra.unit_rate with
    field_source=derived. Never rewrites qty/value. Default ON.
    Kill switch: STOCK_PDF_RECOVER_UNIT_RATE=false.
    """
    return os.getenv("STOCK_PDF_RECOVER_UNIT_RATE", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _recover_unit_rate_from_printed_pairs(item: Dict[str, Any]) -> None:
    """Derive unit_rate from agreeing printed value/qty pairs when Rate is missing."""
    if not _recover_unit_rate_enabled() or not isinstance(item, dict):
        return
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if extra.get("unit_rate") not in (None, ""):
        return
    pairs: List[float] = []
    for qty_key, val_key in (
        ("opening_qty", "opening_value"),
        ("receipts_qty", "receipts_value"),
        ("sales_qty", "sales_value"),
        ("closing_qty", "closing_value"),
    ):
        qty = item.get(qty_key)
        val = item.get(val_key)
        if val in (None, "") and isinstance(extra, dict):
            val = extra.get(val_key)
        try:
            qf = float(qty)
            vf = float(val)
        except (TypeError, ValueError):
            continue
        if abs(qf) < 1e-9:
            continue
        pairs.append(vf / qf)
    if not pairs:
        return
    # Require at least one pair; when 2+ exist they must agree within 0.05.
    rate = pairs[0]
    for other in pairs[1:]:
        if abs(other - rate) > 0.051:
            logger.info(
                "STOCK_PDF_RECOVER_UNIT_RATE status=disagree name=%s rates=%s",
                str(item.get("product_name") or "")[:40],
                [round(p, 4) for p in pairs],
            )
            return
    rate = round(sum(pairs) / len(pairs), 2)
    item.setdefault("extra", {})
    item["extra"]["unit_rate"] = rate
    fs = item["extra"].setdefault("field_source", {})
    if isinstance(fs, dict):
        fs["unit_rate"] = "derived"
    logger.info(
        "STOCK_PDF_RECOVER_UNIT_RATE name=%s rate=%s pairs=%s",
        str(item.get("product_name") or "")[:40],
        rate,
        len(pairs),
    )


def _peel_trailing_rate_garbage_enabled() -> bool:
    """STOCK_PDF_PEEL_RATE_COLUMN_GARBAGE — strip Rate OCR mash off product_name.

    Default ON. Kill switch STOCK_PDF_PEEL_RATE_COLUMN_GARBAGE=false.
    """
    return os.getenv(
        "STOCK_PDF_PEEL_RATE_COLUMN_GARBAGE", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _peel_trailing_rate_garbage_from_product_name(item: Dict[str, Any]) -> None:
    """Drop a trailing Rate-column OCR mash already glued into product_name."""
    if not _peel_trailing_rate_garbage_enabled() or not isinstance(item, dict):
        return
    name = str(item.get("product_name") or "").strip()
    if not name:
        return
    parts = name.rsplit(None, 1)
    if len(parts) != 2:
        return
    head, tail = parts
    try:
        from services.stock_native_tables import _looks_like_rate_column_garbage
    except Exception:
        return
    if not _looks_like_rate_column_garbage(tail):
        return
    if not re.search(r"[A-Za-z]{3,}", head):
        return
    item["product_name"] = head
    logger.info(
        "STOCK_PDF_PEEL_RATE_COLUMN_GARBAGE name=%s junk=%s",
        head[:40],
        tail,
    )


def _peel_product_code_enabled() -> bool:
    """STOCK_PDF_WORDS_PEEL_PRODUCT_CODE — split leading code out of product_name.

    StockStatement rows often glue Code into the Product cell
    (\"60300 ABANA TAB #\"). Default ON.
    """
    return os.getenv(
        "STOCK_PDF_WORDS_PEEL_PRODUCT_CODE", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


_LEADING_PRODUCT_CODE = re.compile(
    r"^(?:(?P<co>[A-Za-z][A-Za-z0-9 .&']{0,24}?)\s*-\s+)?"
    r"(?P<code>(?:S[O0]\d{4,}|\d{4,8}|PROD\d+))\s+(?P<rest>.+)$",
    re.I,
)


def _peel_leading_product_code(item: Dict[str, Any]) -> None:
    """Move a leading catalogue code from product_name into product_code."""
    if not _peel_product_code_enabled() or not isinstance(item, dict):
        return
    name = str(item.get("product_name") or "").strip()
    if not name:
        return
    match = _LEADING_PRODUCT_CODE.match(name)
    if not match:
        return
    rest = str(match.group("rest") or "").strip()
    code = str(match.group("code") or "").strip()
    if not rest or not code or not re.search(r"[A-Za-z]{2,}", rest):
        return
    if not item.get("product_code"):
        item["product_code"] = code
    item["product_name"] = rest
    logger.info(
        "STOCK_PDF_WORDS_PEEL_PRODUCT_CODE code=%s name=%s",
        code,
        rest[:40],
    )


_TRAILING_CODE_PACK = re.compile(
    r"^(?P<code>(?:S[O0]\d{4,}|\d{4,8}|PROD\d+))\s+"
    r"(?P<pack>\d{2,4}S?|\d+\s*(?:ML|GM|G|MG|TAB|TABS|CAP|CAPS)?)$",
    re.I,
)


def _peel_trailing_pack_from_product_code_enabled() -> bool:
    """STOCK_PDF_WORDS_PEEL_CODE_PACK — split \"SO39037 105\" into code + pack.

    Months SS Report often glues a pack fragment into the Item/code cell.
    Default ON. Kill switch STOCK_PDF_WORDS_PEEL_CODE_PACK=false.
    """
    return os.getenv(
        "STOCK_PDF_WORDS_PEEL_CODE_PACK", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _peel_trailing_pack_from_product_code(item: Dict[str, Any]) -> None:
    """Move a trailing pack token off product_code (keep existing packing)."""
    if not _peel_trailing_pack_from_product_code_enabled() or not isinstance(
        item, dict
    ):
        return
    raw = str(item.get("product_code") or "").strip()
    match = _TRAILING_CODE_PACK.match(raw)
    if not match:
        return
    code = str(match.group("code") or "").strip()
    pack = str(match.group("pack") or "").strip()
    if not code or not pack:
        return
    item["product_code"] = code
    if not str(item.get("packing") or "").strip():
        item["packing"] = pack
    logger.info(
        "STOCK_PDF_WORDS_PEEL_CODE_PACK code=%s pack=%s",
        code,
        pack,
    )


def build_line_items_from_tokens(
    columns: List[Dict[str, Any]],
    token_rows: List[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    from services.stock_header_resolver import assign_cells
    from services.stock_row_classifier import RowStatus, record_candidate

    from services.stock_native_tables import is_software_price_ad_name

    items: List[Dict[str, Any]] = []
    pending_name: Optional[str] = None
    for tokens in token_rows:
        blob = " ".join(
            str(t.get("text") or "").strip()
            for t in tokens
            if isinstance(t, dict) and t.get("text") not in (None, "")
        )
        if is_software_price_ad_name(blob):
            logger.info("STOCK_SOFTWARE_AD_SKIP blob=%s", blob[:80])
            continue
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
        token_blob = " ".join(
            str(t.get("text") or "") for t in tokens if isinstance(t, dict)
        )
        if _is_sheet_total_name(token_blob):
            logger.info(
                "STOCK_PDF_WORDS_TOTAL_SKIP sheet_total blob=%s",
                token_blob[:60],
            )
            continue
        try:
            from services.stock_native_tables import (
                _is_nameless_money_footer_tokens,
                _looks_like_legend_or_footer_header,
            )

            if _is_nameless_money_footer_tokens(tokens):
                logger.info(
                    "STOCK_PDF_WORDS_MONEY_FOOTER_SKIP blob=%s",
                    token_blob[:60],
                )
                continue
            if _looks_like_legend_or_footer_header(tokens):
                logger.info(
                    "STOCK_PDF_WORDS_FOOTER_CHROME_SKIP blob=%s",
                    token_blob[:60],
                )
                continue
        except Exception:
            pass
        # Product lot totals print "Total :" — not sheet "Total Count:" / "Total:-".
        is_product_total = bool(
            re.search(r"\bTotal\s*:", token_blob, re.I)
            and not re.search(
                r"\bTotal\s*Count\b|\bTotal\s*:-", token_blob, re.I
            )
        )
        assign_tokens = tokens
        if is_product_total:
            # Drop the "Total :" label so amounts keep Op/Pur/Sl/Cl geometry.
            assign_tokens = [
                t
                for t in tokens
                if isinstance(t, dict)
                and not re.search(r"\btotal\b", str(t.get("text") or ""), re.I)
            ]
        assign_tokens = _join_leading_particular_tokens(assign_tokens)
        assigned = assign_cells(assign_tokens, columns)
        fields = assigned.get("fields") or {}
        formula_toks = [t for t in tokens if isinstance(t, dict) and t.get("formula")]
        item = fields_to_line_item(fields, formula_tokens=formula_toks)
        name = item.get("product_name")
        if _is_sheet_total_name(name):
            logger.info("STOCK_PDF_WORDS_TOTAL_SKIP name=%s", str(name)[:60])
            continue
        # Pipe-ruled sheets print a product Total row after multi-lot lines.
        # Prefer those printed totals over the first lot line alone.
        total_label = str(name or "")
        if items and is_product_total:
            prev = items[-1]
            for key in (
                "opening_qty",
                "receipts_qty",
                "sales_qty",
                "closing_qty",
                "sales_value",
                "closing_value",
                "opening_value",
            ):
                val = item.get(key)
                if val not in (None, ""):
                    prev[key] = val
            prev_extra = prev.setdefault("extra", {})
            item_extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
            for key in ("opening_value", "receipts_value", "sales_value", "closing_value"):
                val = item_extra.get(key)
                if val in (None, ""):
                    val = item.get(key)
                if val not in (None, ""):
                    prev_extra[key] = val
            if item.get("closing_value") not in (None, ""):
                prev["closing_value"] = item.get("closing_value")
            # Printed totals replace lot-line gaps — drop stale missing flags.
            prev_extra.pop("row_status", None)
            fs = prev_extra.get("field_source")
            if isinstance(fs, dict):
                for key in (
                    "opening_qty",
                    "receipts_qty",
                    "sales_qty",
                    "closing_qty",
                    "opening_value",
                    "receipts_value",
                    "sales_value",
                    "closing_value",
                ):
                    if prev.get(key) not in (None, "") or prev_extra.get(key) not in (
                        None,
                        "",
                    ):
                        fs.pop(key, None)
            logger.info(
                "STOCK_PDF_WORDS_PRODUCT_TOTAL applied name=%s label=%s",
                str(prev.get("product_name") or "")[:40],
                (total_label or token_blob)[:40],
            )
            continue
        # Particular words sometimes arrive as separate tokens with collapsed
        # x_centers (OPHTHACARE|EYE|DROP) so product_name stays empty — rebuild.
        if (
            (not name or not re.search(r"[A-Za-z]", str(name)))
            and _recover_split_particular_name(tokens, item)
        ):
            name = item.get("product_name")
        # Nameless lot / batch lines under a product: sum into the previous SKU.
        if (
            (not name or not re.search(r"[A-Za-z]", str(name)))
            and items
            and _merge_nameless_lot_into_prev(items[-1], item, tokens)
        ):
            continue
        if re.match(r"^(TOTAL|GRAND|SUB\s*TOTAL)\b", str(name or ""), re.I):
            continue
        # Period banner "01/10/26 To :31/10/26" is never a product row.
        if re.match(
            r"(?i)^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\s+To\s*:?\s*"
            r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$",
            str(name or "").strip(),
        ):
            logger.info(
                "STOCK_PDF_WORDS_PERIOD_BANNER_SKIP name=%s", str(name or "")[:60]
            )
            pending_name = None
            continue
        # Footer chrome like "Qnt-> OB" / "Value->" after the table.
        if re.match(
            r"(?i)^(?:qnt|qty|value)\s*[-–>]+\s*(?:ob|op|cl|cb|pur|sl)?\b",
            str(name or "").strip(),
        ):
            logger.info(
                "STOCK_PDF_WORDS_FOOTER_ARROW_SKIP name=%s", str(name or "")[:60]
            )
            pending_name = None
            continue
        # Letterhead / address chrome on continuation pages is not a product.
        _name_for_skip = f"{name or ''} {item.get('product_code') or ''}"
        if re.search(
            r"(?i)\b(?:private\s+limited|pvt\.?\s*ltd|fssai|gstin|email|"
            r"pharmaceuticals?\s+private|contact:|mobile:-|"
            r"akkamahadev|vijayapura|vjayapura|karnataka|arnataka|"
            r"banashankari|suprabhat)\b",
            _name_for_skip,
        ) or re.match(
            r"(?i)^(?:cts\s*n[o0]\.?|year\s*:)", str(name or "")
        ):
            logger.info(
                "STOCK_PDF_WORDS_LETTERHEAD_SKIP name=%s", str(name or "")[:60]
            )
            pending_name = None
            continue
        # Wrapped product-name fragment with no movement numbers → append.
        # Only short letter-only wraps (CREAM / GEL ANTI ACNE), never codes or
        # footer chrome ("2 COMPANY 99 Prod.").
        # Printed 0 / 0.00 is a real STOCK STATEMENT row — not a wrap fragment.
        qty_missing = all(
            item.get(k) in (None, "")
            for k in (
                "opening_qty",
                "receipts_qty",
                "sales_qty",
                "closing_qty",
                "sales_value",
                "closing_value",
            )
        )
        qty_empty = all(
            item.get(k) in (None, "", 0, 0.0)
            for k in (
                "opening_qty",
                "receipts_qty",
                "sales_qty",
                "closing_qty",
                "sales_value",
                "closing_value",
            )
        )
        has_code = bool(re.search(r"[A-Za-z0-9]", str(item.get("product_code") or "")))
        frag = str(name or "").strip()
        # Code+qty row with name on the line above: apply pending before the
        # empty-name skip (Item|Item Name layouts park the SKU in product_code).
        if pending_name and (has_code or not qty_empty):
            if _brand_section_pending_skip_enabled() and _is_brand_section_pending_name(
                pending_name
            ):
                logger.info(
                    "STOCK_PDF_WORDS_BRAND_SECTION_PENDING_DROP pending=%s",
                    pending_name[:60],
                )
                pending_name = None
            else:
                cur = frag
                # Next row already has its own product title — emit the pending
                # name-only SKU (zero movement) instead of concatenating.
                if (
                    cur
                    and len(cur.split()) >= 2
                    and pending_name.lower() not in cur.lower()
                    and cur.upper() not in pending_name.upper()
                ):
                    empty = _empty_line_item()
                    empty["product_name"] = pending_name
                    items.append(empty)
                    logger.info(
                        "STOCK_PDF_WORDS_NAME_PENDING_EMIT pending=%s next=%s",
                        pending_name[:40],
                        cur[:40],
                    )
                    pending_name = None
                else:
                    if not cur or cur.upper() in pending_name.upper():
                        item["product_name"] = pending_name
                    elif pending_name.lower() not in cur.lower():
                        item["product_name"] = f"{pending_name} {cur}".strip()
                    else:
                        item["product_name"] = cur
                    logger.info(
                        "STOCK_PDF_WORDS_NAME_PENDING_APPLY name=%s",
                        str(item.get("product_name") or "")[:60],
                    )
                    pending_name = None
                    name = item.get("product_name")
                    frag = str(name or "").strip()
        # Name printed above the code/qty row (Months SS Report wraps).
        # Require truly missing movement cells — not printed zeros.
        if (
            qty_missing
            and frag
            and not has_code
            and not item.get("packing")
            and not _SKIP_CONTINUATION.search(frag)
            and re.search(r"[A-Za-z]{2,}", frag)
            and len(frag.split()) <= 6
            and not re.search(
                r"(?i)\b(?:company|total|page|software|report|manufacturer|"
                r"division|period|months?\s+ss|private\s+limited)\b",
                frag,
            )
        ):
            letter_only = not re.search(r"\d", frag)
            if letter_only:
                # Single-token wraps (CREAM / INDIA) may glue to the previous
                # SKU; multi-word lines are the next product's title banner.
                if (
                    items
                    and not pending_name
                    and len(frag.split()) == 1
                ):
                    prev = str(items[-1].get("product_name") or "").strip()
                    if frag.lower() not in prev.lower():
                        items[-1]["product_name"] = (prev + " " + frag).strip()
                        logger.info(
                            "STOCK_PDF_WORDS_NAME_CONTINUATION prev=%s frag=%s",
                            prev[:40],
                            frag[:40],
                        )
                    continue
                if re.search(
                    r"(?i)\b(?:akkamahadev|vijayapura|vjayapura|karnataka|"
                    r"suprabhat|private|limited|fssai)\b",
                    frag,
                ):
                    pending_name = None
                    continue
                if _brand_section_pending_skip_enabled() and _is_brand_section_pending_name(
                    frag
                ):
                    logger.info(
                        "STOCK_PDF_WORDS_BRAND_SECTION_SKIP name=%s", frag[:60]
                    )
                    pending_name = None
                    continue
                # Zero-movement product with a pack word (KARELA TAB) — keep row.
                if len(frag.split()) >= 2 and _SECTION_PACK_WORD.search(frag):
                    logger.info(
                        "STOCK_PDF_WORDS_ZERO_NAME_KEEP name=%s", frag[:60]
                    )
                else:
                    pending_name = (
                        f"{pending_name} {frag}".strip() if pending_name else frag
                    )
                    logger.info(
                        "STOCK_PDF_WORDS_NAME_PENDING name=%s", pending_name[:60]
                    )
                    continue
        elif qty_empty and not qty_missing and frag:
            logger.info(
                "STOCK_PDF_WORDS_ZERO_MOVEMENT_KEEP name=%s pack=%s",
                frag[:60],
                str(item.get("packing") or "")[:20],
            )
        if not frag and not has_code:
            continue
        if frag and not re.search(r"[A-Za-z]", frag) and not has_code:
            continue
        if not frag and has_code:
            item["product_name"] = str(item.get("product_code") or "").strip()
        _peel_trailing_pack_into_packing(item, columns)
        items.append(item)
    return items


def _pdf_words_lot_continuation_enabled() -> bool:
    """STOCK_PDF_WORDS_LOT_CONTINUATION — sum nameless batch lines into prior SKU.

    STOCK REPORT sheets print one Particular then several Op/Sl/Cl lot lines
    with no product name. Default ON. Kill switch: false.
    """
    return os.getenv(
        "STOCK_PDF_WORDS_LOT_CONTINUATION", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _pdf_words_split_particular_enabled() -> bool:
    """STOCK_PDF_WORDS_SPLIT_PARTICULAR — rebuild Particular from split tokens.

    Default ON. Kill switch: false.
    """
    return os.getenv(
        "STOCK_PDF_WORDS_SPLIT_PARTICULAR", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


_LOT_SKIP_ALPHA = re.compile(
    r"(?i)^(?:total|company|grand|sub|page|value|qnt|ob|pur|sale|closing|"
    r"prod\.?|i|gm|ml|mg|tab|cap|syp)$"
)


def _join_leading_particular_tokens(
    tokens: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge leading Particular word fragments and reindex so qty bands align.

    Plumber often emits OPHTHACARE|EYE|DROP as three tokens with col_index 0..2,
    which shifts Op.Qnt into sales_qty. Join them before assign_cells.
    """
    if not _pdf_words_split_particular_enabled() or not tokens:
        return tokens
    from services.stock_header_resolver import parse_number

    leading_idx: List[int] = []
    for i, tok in enumerate(tokens):
        if not isinstance(tok, dict):
            continue
        text = str(tok.get("text") or "").strip()
        if not text:
            continue
        if re.fullmatch(r"-?\d+(?:,\d{3})*(?:\.\d+)?", text.replace(",", "")):
            break
        # Tally qty+unit / accounting negatives: "1,344 NOs", "(-)232 NOs".
        if parse_number(text) is not None:
            break
        if re.fullmatch(r"\d{1,2}/\d{2,4}", text):
            break
        if re.fullmatch(r"-+", text):
            break
        if re.search(r"[A-Za-z]", text):
            leading_idx.append(i)
            continue
        break
    if len(leading_idx) < 2:
        return tokens
    parts = [str(tokens[i].get("text") or "").strip() for i in leading_idx]
    if not any(len(p) >= 3 and not _LOT_SKIP_ALPHA.fullmatch(p) for p in parts):
        return tokens
    name = re.sub(r"\s+", " ", " ".join(parts)).strip()
    if not name or _is_total_row_name(name) or _is_sheet_total_name(name):
        return tokens
    first = leading_idx[0]
    skip = set(leading_idx[1:])
    joined = dict(tokens[first])
    joined["text"] = name
    out: List[Dict[str, Any]] = []
    for i, tok in enumerate(tokens):
        if i == first:
            out.append(joined)
        elif i in skip:
            continue
        elif isinstance(tok, dict):
            out.append(dict(tok))
    for i, tok in enumerate(out):
        tok["col_index"] = i
    logger.info("STOCK_PDF_WORDS_SPLIT_PARTICULAR joined=%s", name[:48])
    return out


def _recover_split_particular_name(
    tokens: List[Dict[str, Any]], item: Dict[str, Any]
) -> bool:
    """Fallback: set product_name from leading alpha tokens after assign."""
    if not _pdf_words_split_particular_enabled():
        return False
    from services.stock_header_resolver import parse_number

    existing = str(item.get("product_name") or "").strip()
    if existing and re.search(r"[A-Za-z]", existing):
        return False
    leading: List[str] = []
    for tok in tokens or []:
        if not isinstance(tok, dict):
            continue
        text = str(tok.get("text") or "").strip()
        if not text:
            continue
        if re.fullmatch(r"-?\d+(?:,\d{3})*(?:\.\d+)?", text.replace(",", "")):
            break
        if parse_number(text) is not None:
            break
        if re.fullmatch(r"\d{1,2}/\d{2,4}", text):
            break
        if re.fullmatch(r"-+", text):
            break
        if re.search(r"[A-Za-z]", text):
            leading.append(text)
            continue
        break
    if not leading:
        return False
    if not any(len(w) >= 3 and not _LOT_SKIP_ALPHA.fullmatch(w) for w in leading):
        return False
    name = re.sub(r"\s+", " ", " ".join(leading)).strip()
    if not name or _is_total_row_name(name) or _is_sheet_total_name(name):
        return False
    item["product_name"] = name
    logger.info("STOCK_PDF_WORDS_SPLIT_PARTICULAR name=%s", name[:48])
    return True


def _item_has_stock_figures(item: Dict[str, Any]) -> bool:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    for key in (
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "closing_qty",
        "sales_value",
        "closing_value",
        "opening_value",
    ):
        if item.get(key) not in (None, ""):
            return True
        if extra.get(key) not in (None, ""):
            return True
    return False


def _sum_stock_fields(prev: Dict[str, Any], lot: Dict[str, Any], key: str) -> None:
    """Add lot[key] into prev[key] when either side has a printed number."""
    extra_prev = prev.setdefault("extra", {})
    if not isinstance(extra_prev, dict):
        extra_prev = {}
        prev["extra"] = extra_prev
    extra_lot = lot.get("extra") if isinstance(lot.get("extra"), dict) else {}

    def _read(src: Dict[str, Any], ex: Dict[str, Any]) -> Optional[float]:
        val = src.get(key)
        if val in (None, ""):
            val = ex.get(key)
        if val in (None, ""):
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    a = _read(prev, extra_prev)
    b = _read(lot, extra_lot)
    if a is None and b is None:
        return
    total = float(a or 0.0) + float(b or 0.0)
    if key in prev or key in {
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "closing_qty",
        "sales_value",
        "closing_value",
    }:
        prev[key] = total
    if key in extra_prev or key in {
        "opening_value",
        "receipts_value",
        "sales_value",
        "closing_value",
    }:
        extra_prev[key] = total
    fs = extra_prev.get("field_source")
    if isinstance(fs, dict):
        fs.pop(key, None)


def _merge_nameless_lot_into_prev(
    prev: Dict[str, Any],
    lot: Dict[str, Any],
    tokens: List[Dict[str, Any]],
) -> bool:
    """Sum a nameless Op/Sl/Cl lot line into the previous product row."""
    if not _pdf_words_lot_continuation_enabled():
        return False
    if not _item_has_stock_figures(lot):
        return False
    prev_name = str(prev.get("product_name") or "").strip()
    if not prev_name or not re.search(r"[A-Za-z]", prev_name):
        return False
    if _is_total_row_name(prev_name) or _is_sheet_total_name(prev_name):
        return False
    # Nameless money footer under qty headers is never a lot continuation.
    try:
        from services.stock_native_tables import _is_nameless_money_footer_tokens

        if _is_nameless_money_footer_tokens(tokens):
            logger.info(
                "STOCK_PDF_WORDS_LOT_CONTINUATION skip=money_footer prev=%s",
                prev_name[:40],
            )
            return False
    except Exception:
        pass
    # Reject rows that still carry alphabetic product fragments.
    for tok in tokens or []:
        if not isinstance(tok, dict):
            continue
        text = str(tok.get("text") or "").strip()
        if not text or _LOT_SKIP_ALPHA.fullmatch(text):
            continue
        if re.search(r"[A-Za-z]{3,}", text) and not re.fullmatch(
            r"-?\d+(?:,\d{3})*(?:\.\d+)?", text.replace(",", "")
        ):
            # Looks like a product / banner fragment, not a pure lot line.
            if not re.fullmatch(r"\d{6,}", text):  # batch codes ok
                if not re.fullmatch(r"[A-Za-z]?\d{5,}", text):
                    return False
    for key in (
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "closing_qty",
        "sales_value",
        "closing_value",
        "opening_value",
        "receipts_value",
    ):
        _sum_stock_fields(prev, lot, key)
    # Keep first packing/batch; do not overwrite with lot "I".
    flags = list((prev.get("extra") or {}).get("flags") or [])
    if "LOT_CONTINUATION_MERGED" not in flags:
        flags.append("LOT_CONTINUATION_MERGED")
    prev.setdefault("extra", {})["flags"] = flags
    logger.info(
        "STOCK_PDF_WORDS_LOT_CONTINUATION prev=%s op=%s is=%s cq=%s cv=%s",
        prev_name[:40],
        prev.get("opening_qty"),
        prev.get("sales_qty"),
        prev.get("closing_qty"),
        prev.get("closing_value"),
    )
    return True


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
    r"(?i)^(for\b|authorised\b|authorized\b|page\b|\d+\s*/\s*\d+$|"
    r"[\*\-#\s]*near\s+expiry|[\*\-#\s]*scheme\s+on\s+product)"
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
    from services.stock_native_tables import is_software_price_ad_name, resolver_ready

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
        if is_software_price_ad_name(label):
            logger.info("STOCK_SOFTWARE_AD_SKIP name=%s", label[:80])
            continue
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
        if is_software_price_ad_name(name):
            logger.info("STOCK_SOFTWARE_AD_SKIP name=%s", name[:80])
            continue
        if not name or _TOTAL_LABEL.search(name) or _is_total_row_name(name):
            logger.info("STOCK_PDF_WORDS_TOTAL_SKIP name=%s", name or label)
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
    r"closing|division|qty|value|rate|stock|name)$"
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


def _qty_value_grid_skips_receipt_issue() -> bool:
    """STOCK_PDF_QTY_VALUE_SKIP_RECEIPT_ISSUE — default ON.

    OPENING STOCK/VALUE + RECEIVE QUANT/VALUE grids already cluster as
    complete rows. The wrapped-name Receipt/Issue reader would steal packing
    digits as opening_qty and glue a TOTAL footer onto the last product.
    """
    return os.getenv(
        "STOCK_PDF_QTY_VALUE_SKIP_RECEIPT_ISSUE", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _issue_column_layout(columns: List[Dict[str, Any]]) -> bool:
    canons = {str(c.get("canonical") or "") for c in columns}
    if not (_ISSUE_LAYOUT <= canons):
        return False
    # Qty+value pair headers are not the wrapped-name Receipt/Issue layout.
    if _qty_value_grid_skips_receipt_issue() and {
        "opening_value",
        "purchase_value",
    } <= canons:
        logger.info("STOCK_PDF_WORDS_RECEIPT_ISSUE skip=qty_value_grid")
        return False
    # Require printed Receipt/Issue labels. Op.Qnt / Pur.Qnt / Sl.Qnt / Cl.Qnt
    # sheets share the same canons but must use resolved word columns instead.
    blob = " ".join(str(c.get("header_text") or "") for c in columns)
    if not re.search(r"\breceipt|\bissue\b", blob, re.I):
        logger.info(
            "STOCK_PDF_WORDS_RECEIPT_ISSUE skip=no_receipt_issue_header"
        )
        return False
    return True


def _page_rows_have_aligned_qty_value(page_results: List[Dict[str, Any]]) -> bool:
    """True when most clustered word rows already carry a full qty/value set."""
    from services.stock_header_resolver import parse_number

    rows: List[List[Dict[str, Any]]] = []
    for page in page_results or []:
        rows.extend(page.get("data_rows") or [])
    if len(rows) < 3:
        return False
    rich = 0
    for tokens in rows:
        nums = 0
        for tok in tokens:
            if not isinstance(tok, dict):
                continue
            if parse_number(str(tok.get("text") or "")) is not None:
                nums += 1
        if nums >= 6:
            rich += 1
    return rich >= max(3, int(0.6 * len(rows)))


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
        if re.search(r"\bOpening\b", blob) and re.search(
            r"\b(?:Product|Name)\b", blob
        ):
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

        if (
            not re.search(r"[A-Za-z]", name)
            or _is_total_row_name(name)
            or _TOTAL_LABEL.search(name)
        ):
            closing_total = _band_number("closing_value")
            sales_total = _band_number("sales_value")
            logger.info("STOCK_PDF_WORDS_TOTAL_SKIP name=%s", name)
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
    if canons & {"free_in_qty", "free_out_qty"}:
        return False
    return True


def _lms_qty_value_guard_on() -> bool:
    return os.getenv("STOCK_PDF_WORDS_LMS_QTY_VALUE_GUARD", "true").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def _lms_whole_qty(number: float) -> bool:
    return number >= -0.001 and abs(number - int(number)) <= 0.001


def _lms_money_token(number: float, text: str) -> bool:
    raw = str(text or "").strip()
    if re.search(r",\d{3}", raw):
        return True
    if abs(number - int(number)) > 0.001 and abs(number) >= 1.0:
        return True
    return abs(number) >= 500.0


def _lms_rate_not_pack(text: str) -> bool:
    """True when the token left of Opening is a unit rate (87.62 / 160), not 75GM."""
    raw = str(text or "").strip()
    if not raw or re.search(r"[A-Za-z]", raw):
        return False
    from services.stock_header_resolver import parse_number

    number = parse_number(raw.replace(",", ""))
    if number is None:
        return False
    return abs(number) >= 1.0


def _lms_small_qty(number: float) -> bool:
    return _lms_whole_qty(number) and 0 <= abs(number) < 500.0


def _lms_band_x(bands: List[Dict[str, Any]], canon: str) -> Optional[float]:
    for band in bands:
        if str(band.get("canonical") or "") == canon:
            return float(band["x_center"])
    return None


def _lms_rightmost_small(
    side: List[Tuple[float, float]], left_x: float, right_x: float
) -> Optional[float]:
    cands = [
        (x, n) for x, n in side if left_x < x < right_x and _lms_small_qty(n)
    ]
    if not cands:
        return None
    return max(cands, key=lambda item: item[0])[1]


def _lms_steal_tot_from_ignore(
    nums: Dict[str, float],
    side: List[Tuple[float, float]],
    bands: List[Dict[str, Any]],
) -> None:
    """Pharmacy / Cl-city sit left of Tot Sales / Tot Closing and eat the tot qty.

    Re-slot the rightmost printed small qty in that ignore band onto the tot
    column; money already on the tot column moves to the paired value field.
    """
    if not side or not _lms_qty_value_guard_on():
        return
    sales_x = _lms_band_x(bands, "sales_qty") or _lms_band_x(bands, "sales_value")
    if sales_x is not None:
        prev_xs = [
            x
            for x in (
                _lms_band_x(bands, "purchase_qty"),
                _lms_band_x(bands, "lms"),
                _lms_band_x(bands, "opening_qty"),
            )
            if x is not None and x < sales_x
        ]
        prev_x = max(prev_xs) if prev_xs else 0.0
        sq = nums.get("sales_qty")
        sv = nums.get("sales_value")
        sq_missing = sq is None or abs(float(sq)) < 0.001
        sq_money = sq is not None and _lms_money_token(float(sq), str(sq))
        sv_money = sv is not None and _lms_money_token(float(sv), str(sv))
        if (sq_missing and sv_money) or sq_money:
            stolen = _lms_rightmost_small(side, prev_x, sales_x)
            if stolen is not None:
                if sq_money and "sales_value" not in nums:
                    nums["sales_value"] = float(sq)
                nums["sales_qty"] = stolen
                logger.info(
                    "STOCK_PDF_WORDS_LMS_IGNORE_STEAL sales_qty=%s "
                    "sales_value=%s",
                    stolen,
                    nums.get("sales_value"),
                )
    closing_x = _lms_band_x(bands, "closing_qty")
    if closing_x is not None:
        prev_xs = [
            x
            for x in (
                _lms_band_x(bands, "sales_value"),
                _lms_band_x(bands, "sales_qty"),
            )
            if x is not None and x < closing_x
        ]
        prev_x = max(prev_xs) if prev_xs else sales_x or 0.0
        cq = nums.get("closing_qty")
        cq_missing = cq is None
        cq_money = cq is not None and _lms_money_token(float(cq), str(cq))
        if cq_missing or cq_money:
            stolen = _lms_rightmost_small(side, prev_x, closing_x)
            if stolen is not None:
                if cq_money and "closing_value" not in nums:
                    nums["closing_value"] = float(cq)
                nums["closing_qty"] = stolen
                logger.info(
                    "STOCK_PDF_WORDS_LMS_IGNORE_STEAL closing_qty=%s "
                    "closing_value=%s",
                    stolen,
                    nums.get("closing_value"),
                )


_PACK_WORD = re.compile(r"(?i)^\d[\w.'/-]*[A-Za-z][\w.'/-]*$")


def _lms_split_pack_from_name(name: str, pack: Optional[str]) -> Tuple[str, Optional[str]]:
    if pack or not name:
        return name, pack
    parts = name.split()
    if len(parts) >= 2 and _PACK_WORD.match(parts[-1]):
        return " ".join(parts[:-1]), parts[-1]
    return name, pack


_NEAR_EXPIRY_DETAIL_ROW = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9 .&'()/-]{1,}?)\s+"
    r"(?P<pack>\d+\s*(?:['’]?S\.?|ML|GM\.?S?|GMS\.?|G|L|TAB|CAP)[A-Za-z0-9.'/-]*)\s+"
    r"(?P<batch>#?[A-Za-z0-9-]+)\s+"
    r"(?P<mrp>\d+\.\d{2})\s+"
    r"(?P<rate>\d+\.\d{2})"
    r"(?:\s+(?P<prate>\d+\.\d{2}))?\s+"
    r"(?P<stock>\d+)\s+"
    r"(?P<expiry>\d{1,2}/\d{1,2}/\d{2,4})\s*$",
    re.I,
)


def _norm_product_key(name: Any) -> str:
    raw = re.sub(r"[^a-z0-9]+", "", str(name or "").lower())
    return raw


def parse_near_expiry_detail_section(text: str) -> List[Dict[str, Any]]:
    """Parse PRODUCT/PACK/BATCH/MRP/... rows under NEAR EXPIRY DETAIL."""
    if not text or not re.search(r"(?i)near\s+expir", text):
        return []
    chunk = re.split(r"(?i)near\s+expir(?:y)?\s+detail[^\n]*", text, maxsplit=1)
    if len(chunk) < 2:
        return []
    body = chunk[1]
    details: List[Dict[str, Any]] = []
    for raw in body.splitlines():
        line = re.sub(r"\s+", " ", (raw or "").strip())
        if not line:
            continue
        if re.search(r"(?i)^(product|pack|batch|mrp|rate|stock|expir)\b", line):
            continue
        if re.search(r"(?i)^page\b|prepared\s+by|computer\s+generated", line):
            break
        m = _NEAR_EXPIRY_DETAIL_ROW.match(line)
        if not m:
            continue
        from services.stock_header_resolver import parse_number

        stock_n = parse_number(m.group("stock"))
        details.append(
            {
                "product_name": re.sub(r"\s+", " ", m.group("name")).strip(),
                "packing": re.sub(r"\s+", " ", m.group("pack")).strip(),
                "batch": m.group("batch").strip(),
                "mrp": parse_number(m.group("mrp")),
                "rate": parse_number(m.group("rate")),
                "p_rate": parse_number(m.group("prate")) if m.group("prate") else None,
                "stock_qty": float(stock_n) if stock_n is not None else None,
                "expiry_date": m.group("expiry").strip(),
            }
        )
    return details


def _near_expiry_detail_record(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a near-expiry batch row for JSON consumers."""
    batch = row.get("batch") or row.get("batch_no")
    return {
        "product_name": row.get("product_name"),
        "packing": row.get("packing"),
        "batch_no": batch,
        "batch": batch,
        "mrp": row.get("mrp"),
        "rate": row.get("rate"),
        "p_rate": row.get("p_rate"),
        "stock_qty": row.get("stock_qty"),
        "expiry_date": row.get("expiry_date"),
    }


def annotate_near_expiry_discrepancies(
    items: List[Dict[str, Any]], details: List[Dict[str, Any]]
) -> None:
    """Attach batch details and promote NEAREXP when the detail table has stock.

    Main-grid NEAREXP can print '-' while NEAR EXPIRY DETAIL still lists batches
    (e.g. Pilex Forte Tab main 0, detail stock 3). Prefer the detail sum on the
    line item and keep the printed main qty under near_expiry_qty_main.
    """
    if not items or not details:
        return
    from collections import defaultdict

    sums: Dict[str, float] = defaultdict(float)
    batches: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in details:
        if not isinstance(row, dict):
            continue
        key = _norm_product_key(row.get("product_name"))
        qty = row.get("stock_qty")
        if key and qty is not None:
            sums[key] += float(qty)
        if key:
            batches[key].append(_near_expiry_detail_record(row))
    for item in items:
        if not isinstance(item, dict):
            continue
        extra = item.setdefault("extra", {})
        if not isinstance(extra, dict):
            continue
        key = _norm_product_key(item.get("product_name"))
        if not key:
            continue
        detail_sum = None
        matched_key = None
        if key in sums:
            detail_sum = sums[key]
            matched_key = key
        else:
            for dkey, dval in sums.items():
                if key in dkey or dkey in key:
                    detail_sum = dval
                    matched_key = dkey
                    break
        if matched_key and batches.get(matched_key):
            extra["near_expiry_batches"] = list(batches[matched_key])
        if detail_sum is None:
            continue
        extra["near_expiry_detail_qty"] = detail_sum
        main = extra.get("near_expiry_qty")
        try:
            main_f = float(main) if main is not None else None
        except (TypeError, ValueError):
            main_f = None
        # Detail table is authoritative when main is blank/zero or disagrees.
        if main_f is None or abs(main_f) < 0.001:
            if main_f is not None:
                extra["near_expiry_qty_main"] = main_f
            extra["near_expiry_qty"] = detail_sum
            if main_f is not None and abs(detail_sum - main_f) > 0.01:
                extra["near_expiry_qty_discrepancy"] = True
            logger.info(
                "STOCK_NEAR_EXPIRY_FROM_DETAIL product=%s main=%s detail=%s",
                item.get("product_name"),
                main_f,
                detail_sum,
            )
        elif abs(detail_sum - main_f) > 0.01:
            extra["near_expiry_qty_discrepancy"] = True
            extra["near_expiry_qty_main"] = main_f
            # Keep printed main qty, but expose detail sum for reconciliation.
            logger.info(
                "STOCK_NEAR_EXPIRY_DISCREPANCY product=%s main=%s detail=%s",
                item.get("product_name"),
                main_f,
                detail_sum,
            )


def _money_footer_from_text_before_near_expiry(text: str) -> Dict[str, Optional[float]]:
    """LMS / Sales / Closing money totals printed just above NEAR EXPIRY DETAIL."""
    out: Dict[str, Optional[float]] = {
        "lms_value": None,
        "sales_value": None,
        "closing_value": None,
    }
    if not text:
        return out
    head = re.split(r"(?i)near\s+expir(?:y)?\s+detail", text, maxsplit=1)[0]
    from services.stock_header_resolver import parse_number

    amounts: List[float] = []
    for ln in head.splitlines():
        raw = (ln or "").strip().replace(",", "")
        if re.fullmatch(r"-?\d+\.\d{2}", raw):
            number = parse_number(raw)
            if number is not None and abs(number) >= 1.0:
                amounts.append(float(number))
    if len(amounts) >= 3:
        out["lms_value"] = amounts[-3]
        out["sales_value"] = amounts[-2]
        out["closing_value"] = amounts[-1]
    elif len(amounts) == 2:
        out["sales_value"] = amounts[-2]
        out["closing_value"] = amounts[-1]
    elif len(amounts) == 1:
        out["closing_value"] = amounts[-1]
    return out


def _is_stacked_lms_nearexp_layout(text: str) -> bool:
    """STOCK STATEMENT with OPENING/PURCHASE/LMS/SALES/CLOSING(/NEAREXP)."""
    if not text:
        return False
    head = "\n".join(ln for ln in text.splitlines()[:40] if ln.strip())
    if not re.search(r"(?i)\bopening\b", head):
        return False
    if not re.search(r"(?i)\bpurchase\b", head):
        return False
    if not re.search(r"(?i)\blms\b", head):
        return False
    if not re.search(r"(?i)\bsales\b", head):
        return False
    if not re.search(r"(?i)\bclosing\b", head):
        return False
    return True


def _stacked_lms_qty_token(text: str) -> Optional[float]:
    raw = (text or "").strip().replace(",", "")
    if raw in _DASH_CELL or raw == "":
        return 0.0
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return None
    from services.stock_header_resolver import parse_number

    return parse_number(raw)


def items_from_stacked_lms_nearexp_text(text: str) -> Optional[Dict[str, Any]]:
    """Fallback when words miss columns: stacked name/pack + 5–6 qty lines.

    Handles StockHDZ-style Himalaya Zeal STOCK STATEMENT pages where each
    field prints on its own line under OPENING/PURCHASE/LMS/SALES/CLOSING/NEAREXP.
    """
    if not _is_stacked_lms_nearexp_layout(text):
        return None
    from services.stock_header_resolver import parse_number

    body = re.split(r"(?i)near\s+expir(?:y)?\s+detail", text, maxsplit=1)[0]
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    # Skip letterhead until PRODUCT header.
    start = 0
    for i, ln in enumerate(lines):
        if re.fullmatch(r"(?i)product", ln) or re.search(
            r"(?i)^product\b.*\bopening\b", ln
        ):
            start = i + 1
            # Consume following header tokens (OPENING, PURCHASE, LMS, ...).
            while start < len(lines) and re.fullmatch(
                r"(?i)product|opening|purchase|lms|sales|closing|nearexp|"
                r"near\s*exp(?:iry)?",
                lines[start],
            ):
                start += 1
            break
    items: List[Dict[str, Any]] = []
    i = start
    pack_re = re.compile(
        r"(?i)^\d[\w.'/-]*[A-Za-z][\w.'/-]*$|"
        r"^\d+\s*(?:['’]?S\.?|ML|GM|GMS?|G|L)\b|"
        # Bare pack counts like "100" / "60" (not a movement qty line).
        r"^\d{1,4}$"
    )
    while i < len(lines):
        ln = lines[i]
        if re.fullmatch(r"-?\d+\.\d{2}", ln.replace(",", "")):
            break
        if not re.search(r"[A-Za-z]{3,}", ln):
            i += 1
            continue
        if re.search(r"(?i)^(product|opening|purchase|lms|sales|closing)\b", ln):
            i += 1
            continue
        name = re.sub(r"\s+", " ", ln).strip()
        pack = None
        j = i + 1
        if j < len(lines) and pack_re.match(lines[j]):
            # Bare digits are packing only when enough qty lines follow.
            if re.fullmatch(r"\d{1,4}", lines[j]):
                look = []
                k = j + 1
                while k < len(lines) and len(look) < 6:
                    q = _stacked_lms_qty_token(lines[k])
                    if q is None:
                        break
                    look.append(q)
                    k += 1
                if len(look) >= 5:
                    pack = lines[j]
                    j += 1
            else:
                pack = lines[j]
                j += 1
        qtys: List[float] = []
        while j < len(lines) and len(qtys) < 6:
            q = _stacked_lms_qty_token(lines[j])
            if q is None:
                break
            qtys.append(float(q))
            j += 1
        # opening, purchase, lms, sales, closing [, nearexp]
        if len(qtys) < 5:
            i += 1
            continue
        if _is_total_row_name(name):
            i = j
            continue
        item = _empty_line_item()
        item["product_name"] = name
        item["packing"] = pack
        item["opening_qty"] = qtys[0]
        item["receipts_qty"] = qtys[1]
        item["lms"] = qtys[2]
        item["extra"]["lms"] = qtys[2]
        item["extra"]["lms_qty"] = qtys[2]
        item["sales_qty"] = qtys[3]
        item["closing_qty"] = qtys[4]
        item["sales_value"] = None
        item["closing_value"] = None
        if len(qtys) >= 6:
            item["extra"]["near_expiry_qty"] = qtys[5]
            item["extra"]["exp_damage"] = qtys[5]
        items.append(item)
        i = j
    if len(items) < 3:
        return None
    money = _money_footer_from_text_before_near_expiry(text)
    logger.info(
        "STOCK_PDF_STACKED_LMS_NEAREXP items=%s lms_value=%s sales_value=%s "
        "closing_value=%s",
        len(items),
        money.get("lms_value"),
        money.get("sales_value"),
        money.get("closing_value"),
    )
    out: Dict[str, Any] = {
        "line_items": items,
        "sales_value": money.get("sales_value"),
        "closing_value": money.get("closing_value"),
        "lms_value": money.get("lms_value"),
        "letterhead_is_stockist_only": True,
        "qty_only": True,
    }
    if money.get("lms_value") is not None:
        out["footer_totals"] = {
            "lms_value": money.get("lms_value"),
            "sales_value": money.get("sales_value"),
            "closing_value": money.get("closing_value"),
            "last_month_sales_value": money.get("lms_value"),
        }
    return out


def items_from_lms_qty_rows(
    header_cells: List[Dict[str, Any]],
    data_rows: List[List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """PRODUCT / OPENING / PURCHASE / LMS / SALES / CLOSING, qty only.

    LMS is last-month sales and stays out of sales_qty. The unlabeled pack
    sits left of the opening header. A row of money with no product name is
    the statement total. Qty-only layouts must not invent sales_value /
    closing_value from quantities — leave those null when no money column.
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

    qty_canons = {
        "opening_qty",
        "purchase_qty",
        "sales_qty",
        "closing_qty",
        "lms",
        "expiry_damage_qty",
    }
    value_canons = {
        "sales_value",
        "closing_value",
        "opening_value",
        "purchase_value",
        "rate",
    }
    qty_to_value = {
        "sales_qty": "sales_value",
        "closing_qty": "closing_value",
        "opening_qty": "opening_value",
        "purchase_qty": "purchase_value",
    }
    items: List[Dict[str, Any]] = []
    sales_total: Optional[float] = None
    closing_total: Optional[float] = None
    lms_total: Optional[float] = None
    # Printed footer money band before NEAR EXPIRY DETAIL:
    #   LMS value / Sales value / Closing value  (e.g. 250325.29 x2 + 179233.42)
    money_footer: List[float] = []
    past_money_footer = False
    for raw in data_rows or []:
        tokens = [t for t in (raw or []) if str(t.get("text") or "").strip()]
        if not tokens:
            continue
        texts = [str(t.get("text") or "").strip() for t in tokens]
        blob = " ".join(texts)
        # BATCH/MRP near-expiry detail shares the page — never treat as products.
        if re.search(r"(?i)near\s*expir", blob):
            break
        if re.search(r"(?i)^\s*batch\b|\bmrp\b|\bp\.?\s*rate\b", blob) and not re.search(
            r"(?i)\bopening\b|\blms\b", blob
        ):
            break
        if past_money_footer and any(re.search(r"[A-Za-z]", text) for text in texts):
            # Product-looking rows after the money footer are near-expiry detail.
            break
        if not any(re.search(r"[A-Za-z]", text) for text in texts):
            for tok in tokens:
                text = str(tok.get("text") or "").strip().replace(",", "")
                if tok.get("x") is None or not re.fullmatch(r"-?\d+\.\d+", text):
                    continue
                number = parse_number(text)
                if number is None:
                    continue
                if abs(number - int(number)) > 0.001 and abs(number) >= 1.0:
                    money_footer.append(float(number))
                    past_money_footer = True
                canon = str(_nearest(float(tok["x"])).get("canonical") or "")
                if canon in {"sales_qty", "sales_value", "lms"}:
                    if canon == "lms" and lms_total is None:
                        lms_total = number
                    elif canon != "lms":
                        sales_total = number
                elif canon in {"closing_qty", "closing_value"}:
                    closing_total = number
            continue
        # Detail rows often carry an expiry date token (dd/mm/yyyy).
        if any(re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", t) for t in texts):
            continue
        left: List[Tuple[float, str]] = []
        nums: Dict[str, float] = {}
        side: List[Tuple[float, float]] = []
        for tok in tokens:
            text = str(tok.get("text") or "").strip()
            if tok.get("x") is None:
                continue
            xf = float(tok["x"])
            if xf < open_left - 0.01:
                left.append((xf, text))
                continue
            canon = str(_nearest(xf).get("canonical") or "")
            if canon not in qty_canons and canon not in value_canons:
                if text not in _DASH_CELL:
                    number = parse_number(text.replace(",", ""))
                    if number is not None:
                        side.append((xf, float(number)))
                continue
            if text in _DASH_CELL:
                if canon in qty_canons and canon not in nums:
                    nums[canon] = 0.0
                continue
            number = parse_number(text.replace(",", ""))
            if number is None:
                continue
            if canon in value_canons:
                nums[canon] = number
                continue
            existing = nums.get(canon)
            # Qty column: Sales Value / Stock Value sit right of Tot Sales /
            # Tot Closing. Keep the first printed whole qty; later tokens are value.
            if _lms_qty_value_guard_on() and (
                existing is not None or _lms_money_token(number, text)
            ):
                if existing is None and _lms_whole_qty(number) and not re.search(
                    r",\d{3}", str(text or "")
                ):
                    nums[canon] = number
                    continue
                paired = qty_to_value.get(canon)
                if paired and paired not in nums:
                    nums[paired] = number
                    logger.info(
                        "STOCK_PDF_WORDS_LMS_QTY_VALUE_GUARD canon=%s "
                        "qty=%s value=%s",
                        canon,
                        existing,
                        number,
                    )
                continue
            nums[canon] = number
        _lms_steal_tot_from_ignore(nums, side, bands)
        if "opening_qty" not in nums or not left:
            continue
        left.sort()
        rate_from_pack = None
        if len(left) >= 2:
            pack = left[-1][1]
            name_parts = left[:-1]
            if _lms_qty_value_guard_on() and _lms_rate_not_pack(pack):
                rate_from_pack = pack
                if len(left) >= 3:
                    pack = left[-2][1]
                    name_parts = left[:-2]
                else:
                    pack = None
            name = " ".join(part for _, part in name_parts)
        else:
            pack = None
            name = left[0][1]
        name = re.sub(r"\s+", " ", name).strip()
        name, pack = _lms_split_pack_from_name(name, pack)
        if not re.search(r"[A-Za-z]{3,}", name):
            continue
        if re.search(r"(?i)^(sales\s+amount|stock\s+value|last\s+period)\b", name):
            continue
        if _is_total_row_name(name):
            logger.info("STOCK_PDF_WORDS_TOTAL_SKIP name=%s", name)
            continue
        item = _empty_line_item()
        item["product_name"] = name
        item["packing"] = pack
        item["opening_qty"] = nums.get("opening_qty")
        item["receipts_qty"] = nums.get("purchase_qty")
        item["sales_qty"] = nums.get("sales_qty")
        # Qty-only grid: never invent money from sales/closing qty.
        item["sales_value"] = nums.get("sales_value")
        item["closing_qty"] = nums.get("closing_qty")
        item["closing_value"] = nums.get("closing_value")
        if "lms" in nums:
            lms_n = nums["lms"]
            item["lms"] = lms_n
            item["extra"]["lms"] = lms_n
            item["extra"]["lms_qty"] = lms_n
        near_n = nums.get("expiry_damage_qty")
        if near_n is not None:
            item["extra"]["near_expiry_qty"] = near_n
            item["extra"]["exp_damage"] = near_n
        rate_n = nums.get("rate")
        if rate_n is None and rate_from_pack is not None:
            rate_n = parse_number(str(rate_from_pack).replace(",", ""))
        if rate_n is not None:
            item["extra"]["unit_rate"] = rate_n
        items.append(item)
    if len(items) < 3:
        return None
    # Prefer the three printed money footers (LMS / Sales / Closing values).
    if len(money_footer) >= 3:
        lms_total = money_footer[-3]
        sales_total = money_footer[-2]
        closing_total = money_footer[-1]
    elif len(money_footer) == 2:
        sales_total = money_footer[-2]
        closing_total = money_footer[-1]
    elif len(money_footer) == 1 and closing_total is None:
        closing_total = money_footer[0]
    logger.info(
        "STOCK_PDF_WORDS_LMS_QTY items=%s lms_value=%s sales_value=%s "
        "closing_value=%s",
        len(items),
        lms_total,
        sales_total,
        closing_total,
    )
    out: Dict[str, Any] = {
        "line_items": items,
        "closing_value": closing_total,
        "sales_value": sales_total,
        "letterhead_is_stockist_only": True,
        "qty_only": True,
    }
    if lms_total is not None:
        out["lms_value"] = lms_total
        out["footer_totals"] = {
            "lms_value": lms_total,
            "sales_value": sales_total,
            "closing_value": closing_total,
            "last_month_sales_value": lms_total,
        }
    return out


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
    items = [
        i
        for i in items
        if re.search(r"[A-Za-z]{3,}", str(i.get("product_name") or ""))
        and not _is_total_row_name(i.get("product_name"))
    ]
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


_OPENING_INWARDS_OUTWARDS_CLOSING_FAMILY = "opening_inwards_outwards_closing"


def is_opening_inwards_outwards_closing_layout(text: Any) -> bool:
    """Tally Stock Category Summary chrome — layout family, not a stockist name.

    Particulars | Opening Balance | Inwards | Outwards | Closing Balance with
    Quantity/Rate/Value children. Used to prefer pdf_group_qty_value and to
    keep Gemini from replacing that native read.
    """
    blob = str(text or "")
    if not blob.strip():
        return False
    low = blob.lower()
    if re.search(r"stock\s+category\s+summary", low):
        return True
    has_opening = bool(re.search(r"\bopening\s+balance\b", low))
    has_closing = bool(re.search(r"\bclosing\s+balance\b", low))
    has_in = bool(re.search(r"\binwards?\b", low))
    has_out = bool(re.search(r"\boutwards?\b", low))
    has_qrv = bool(
        re.search(r"\bquantity\b", low)
        and re.search(r"\brate\b", low)
        and re.search(r"\bvalue\b", low)
    )
    return has_opening and has_closing and has_in and has_out and has_qrv


def _drop_grouped_banner_item(item: Dict[str, Any]) -> bool:
    name = str(item.get("product_name") or "").strip()
    if not name:
        return True
    from services.stock_native_tables import is_software_price_ad_name

    if is_software_price_ad_name(name):
        return True
    if re.match(r"^(?:GRAND\s+)?TOTAL\b", name, re.I):
        return True
    # Page carry / bring-forward rows — not SKUs.
    if re.match(
        r"(?i)^(?:carried\s+over|brought\s+forward|c/?o\b|b/?f\b)",
        name,
    ):
        return True
    if re.search(
        r"stock\s+statement|stock\s+category\s+summary|^\s*date\b|^\s*page\b|"
        r"wellness\s+company|"
        r"\bopening\b.*\bpurchase\b|\bqty\b.*\bvalue\b",
        name,
        re.I,
    ):
        return True
    return False


def _group_qty_value_stub_cleanup_enabled() -> bool:
    """STOCK_PDF_GROUP_QTY_VALUE_STUB_CLEANUP — drop rate-less zero stubs.

    Product|Rate|Opening…Closing sheets list idle SKUs as ``NAME 0.00`` with the
    lone money parked in Closing value and no Rate. Same-name empties also
    duplicate a later movement row (e.g. CLARINA FACE WASH). Default ON.
    Kill switch: STOCK_PDF_GROUP_QTY_VALUE_STUB_CLEANUP=false.
    """
    return os.getenv(
        "STOCK_PDF_GROUP_QTY_VALUE_STUB_CLEANUP", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _group_item_movement_score(item: Dict[str, Any]) -> int:
    """How many printed movement slots a row carries (prefer richer duplicate)."""
    score = 0
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    for key in (
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "closing_qty",
        "opening_value",
        "sales_value",
        "closing_value",
    ):
        if item.get(key) not in (None, ""):
            score += 1
    if item.get("receipts_value") not in (None, "") or extra.get(
        "receipts_value"
    ) not in (None, ""):
        score += 1
    if extra.get("unit_rate") not in (None, ""):
        score += 1
    return score


def _is_rate_less_zero_stub(item: Dict[str, Any]) -> bool:
    """True for idle ``NAME 0.00`` stubs — not blank Tally catalog lines.

    Covers rate-less ``NAME 0.00`` and ``NAME rate 0.00`` when every qty/value
    is a printed zero. All-None rows (Stock Category Summary products with no
    movement) are real catalog lines and must stay.
    """
    if not isinstance(item, dict):
        return False
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    has_printed_zero = False
    for key in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
        val = item.get(key)
        if val in (0, 0.0):
            has_printed_zero = True
        elif val not in (None, ""):
            return False
    for key in ("opening_value", "receipts_value", "sales_value", "closing_value"):
        val = item.get(key)
        if val in (None, ""):
            val = extra.get(key) if isinstance(extra, dict) else None
        if val in (0, 0.0):
            has_printed_zero = True
        elif val not in (None, ""):
            return False
    return has_printed_zero


def _cleanup_group_qty_value_stubs(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop rate-less zero stubs and keep the richer same-name duplicate."""
    if not _group_qty_value_stub_cleanup_enabled():
        return items
    # Prefer the richest row per normalized product name.
    best_by_name: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = _norm_name(item.get("product_name"))
        if not key:
            continue
        if key not in best_by_name:
            best_by_name[key] = item
            order.append(key)
            continue
        prev = best_by_name[key]
        if _group_item_movement_score(item) > _group_item_movement_score(prev):
            logger.info(
                "STOCK_PDF_GROUP_QTY_VALUE_STUB_CLEANUP action=prefer_richer "
                "name=%s",
                str(item.get("product_name") or "")[:40],
            )
            best_by_name[key] = item
        else:
            logger.info(
                "STOCK_PDF_GROUP_QTY_VALUE_STUB_CLEANUP action=drop_duplicate "
                "name=%s",
                str(item.get("product_name") or "")[:40],
            )
    cleaned: List[Dict[str, Any]] = []
    dropped = 0
    for key in order:
        item = best_by_name[key]
        if _is_rate_less_zero_stub(item):
            dropped += 1
            logger.info(
                "STOCK_PDF_GROUP_QTY_VALUE_STUB_CLEANUP action=drop_stub name=%s",
                str(item.get("product_name") or "")[:40],
            )
            continue
        cleaned.append(item)
    if dropped or len(cleaned) != len(items):
        logger.info(
            "STOCK_PDF_GROUP_QTY_VALUE_STUB_CLEANUP before=%s after=%s dropped=%s",
            len(items),
            len(cleaned),
            len(items) - len(cleaned),
        )
    return cleaned


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
    deduped = _cleanup_group_qty_value_stubs(deduped)
    # Ensure purchase money is always on the top-level receipts_value field.
    for item in deduped:
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if item.get("receipts_value") in (None, "") and extra.get(
            "receipts_value"
        ) not in (None, ""):
            item["receipts_value"] = extra.get("receipts_value")
        elif item.get("receipts_value") in (None, "") and extra.get(
            "purchase_value"
        ) not in (None, ""):
            item["receipts_value"] = extra.get("purchase_value")
            extra["receipts_value"] = item["receipts_value"]
    if len(deduped) < 3:
        logger.info(
            "STOCK_PDF_GROUP_QTY_VALUE file=%s status=too_few items=%s",
            filename,
            len(deduped),
        )
        return None
    result = empty_result(filename, "pdf")
    result["line_items"] = deduped
    if re.search(r"stock\s+category\s+summary", peek, re.I):
        result["report_title"] = "Stock Category Summary"
    else:
        result["report_title"] = "Stock Statement"
    lines = [ln.strip() for ln in peek.splitlines() if ln.strip()]
    first = lines[0] if lines else ""
    if first and re.search(r"[A-Za-z]", first) and not re.search(
        r"stock\s+(?:statement|category)", first, re.I
    ):
        result["stockist_name"] = first
    # Address block: lines after stockist name until company / report title.
    addr_parts: List[str] = []
    for ln in lines[1:8]:
        if re.search(
            r"(?i)stock\s+(?:statement|category)|^page\b|"
            r"\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4}\s+to\s+",
            ln,
        ):
            break
        if re.search(r"(?i)^e-?mail\b|^phone\b|^tel\b|^gstin\b", ln):
            continue
        if re.fullmatch(r"[A-Z0-9][A-Z0-9 .&-]{0,12}", ln) and len(ln) <= 12:
            # Short all-caps company / division code (e.g. HIM).
            if not result.get("company_name"):
                result["company_name"] = ln
            break
        if re.search(r"[A-Za-z]", ln):
            addr_parts.append(ln)
    if addr_parts and not result.get("stockist_address"):
        result["stockist_address"] = ", ".join(addr_parts)
    company = re.search(
        r"((?:[A-Z][A-Z&. ]{2,})\s+COMPANY(?:\s+[A-Z][A-Z]+)?)",
        peek,
    )
    if company and not result.get("company_name"):
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
    else:
        # Tally: 1-Sep-26 to 30-Sep-26
        mon_period = re.search(
            r"(\d{1,2})[- ]([A-Za-z]{3})[- ](\d{2,4})\s+to\s+"
            r"(\d{1,2})[- ]([A-Za-z]{3})[- ](\d{2,4})",
            peek,
            re.I,
        )
        if mon_period:
            months = {
                "jan": 1,
                "feb": 2,
                "mar": 3,
                "apr": 4,
                "may": 5,
                "jun": 6,
                "jul": 7,
                "aug": 8,
                "sep": 9,
                "oct": 10,
                "nov": 11,
                "dec": 12,
            }

            def _iso_mon(day: str, mon: str, year: str) -> Optional[str]:
                mi = months.get(mon[:3].lower())
                if not mi:
                    return None
                yr = year if len(year) == 4 else ("20" + year)
                return f"{yr}-{mi:02d}-{int(day):02d}"

            pf = _iso_mon(
                mon_period.group(1), mon_period.group(2), mon_period.group(3)
            )
            pt = _iso_mon(
                mon_period.group(4), mon_period.group(5), mon_period.group(6)
            )
            if pf and pt:
                result["period_from"] = pf
                result["period_to"] = pt
    footer = _footer_totals_from_page_results(pages)
    totals = result.setdefault("totals", {})
    if footer:
        for key, val in footer.items():
            if val is not None:
                totals[key] = val
        totals.setdefault("extra", {})["total_row_source"] = "printed_footer"
        logger.info(
            "STOCK_PDF_GROUP_QTY_VALUE footer_totals keys=%s",
            sorted(footer.keys()),
        )
    extra = totals.setdefault("extra", {})
    extra["extraction_method"] = "pdf_group_qty_value"
    extra["statement_count"] = 1
    if is_opening_inwards_outwards_closing_layout(peek):
        extra["layout_family"] = _OPENING_INWARDS_OUTWARDS_CLOSING_FAMILY
        extra["layout"] = _OPENING_INWARDS_OUTWARDS_CLOSING_FAMILY
    logger.info(
        "STOCK_PDF_GROUP_QTY_VALUE file=%s items=%s period=%s..%s layout=%s",
        filename,
        len(deduped),
        result.get("period_from"),
        result.get("period_to"),
        extra.get("layout_family") or "-",
    )
    return result


def _resolved_movement_columns_enabled() -> bool:
    """Op/Pur/Sl/Cl word columns without a sales-return header (default ON)."""
    return os.getenv(
        "STOCK_PDF_RESOLVED_MOVEMENT_COLUMNS", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _count_moved_items(items: Optional[List[Dict[str, Any]]]) -> int:
    """Rows with printed opening or closing — not later-page summary tables."""
    n = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("opening_qty") not in (None, "") or item.get(
            "closing_qty"
        ) not in (None, ""):
            n += 1
    return n


_TRAILING_PACK_RE = re.compile(
    r"(?i)^(?P<name>.+?)\s+(?P<pack>"
    r"\d+(?:\.\d+)?\s*(?:['’`]\s*)?S\.?"
    r"|\d+(?:\.\d+)?\s*(?:ML|MG|GM|GRM|GR|KG|G|M|TAB|TABS|CAP|CAPS|TABLET)s?\.?"
    r"|\d+\s*[xX]\s*\d+"
    r"|UNITS?"
    r")$"
)
# Glued / parenthetical packs when Packing cell is empty:
# "…SPRAY100 ML", "…OR6S", "…CHO(10S" / "…(200ML)".
_GLUED_OR_PAREN_PACK_UNIT = (
    r"\d+(?:\.\d+)?\s*(?:['’`]\s*)?S|"
    r"\d+(?:\.\d+)?\s*(?:ML|MG|GM|GRM|GR|KG|G|M)"
)
_GLUED_OR_PAREN_PACK_RE = re.compile(
    rf"(?i)^(?:"
    rf"(?P<head_paren>.+?)\(\s*(?P<pack_paren>{_GLUED_OR_PAREN_PACK_UNIT})\s*\)?"
    rf"|(?P<head_glue>.+?[A-Za-z])(?P<pack_glue>{_GLUED_OR_PAREN_PACK_UNIT})"
    rf")\s*$"
)
# "LIV.52 SYP,100ML" / "… GHEE 200ML" / "… TAB 30S" when Packing already holds
# the same size (often truncated: 100M / 30'S).
_DUP_PACK_IN_NAME_RE = re.compile(
    r"(?i)^(?P<head>.+?)(?:[,\s]+)(?P<num>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>['’`]?S|ML|MG|GM|GRM|GR|KG|G|M)$"
)
# "BONNISAN LIQ(200ML)" when Packing already holds 200M.
_PAREN_PACK_IN_NAME_RE = re.compile(
    r"(?i)^(?P<head>.+?)\(\s*(?P<num>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>['’`]?S|ML|MG|GM|GRM|KG|G|M)\s*\)$"
)
# "GEL60ML" / "SYP-100ML" / "PASTE-50G" glued onto the last word.
_GLUED_PACK_SUFFIX_RE = re.compile(
    r"(?i)^(?P<head>.*?)(?P<num>\d+(?:\.\d+)?)(?P<unit>['’`]?S|ML|MG|GM|GRM|GR|KG|G|M)$"
)


def _normalize_pack_unit(unit: Any) -> str:
    return re.sub(r"['’`´]", "", str(unit or "").strip()).upper()


def _pack_num_unit(pack: Any) -> Optional[Tuple[str, str]]:
    raw = re.sub(r"\s+", "", str(pack or "").strip())
    raw = re.sub(r"['’`´]", "", raw)
    match = re.fullmatch(
        r"(?i)(\d+(?:\.\d+)?)(ML|MG|GM|GRM|KG|G|M|S)", raw
    )
    if not match:
        return None
    return match.group(1), match.group(2).upper()


def _pack_units_equivalent(a: str, b: str) -> bool:
    ua, ub = _normalize_pack_unit(a), _normalize_pack_unit(b)
    if not ua or not ub:
        return False
    if ua == ub:
        return True
    # Truncated Pack column: 100M for 100ML / 100GM, 200M for 200ML.
    return {ua, ub} in (
        {"M", "ML"},
        {"M", "GM"},
        {"M", "G"},
        {"G", "GM"},
        {"G", "GR"},
        {"G", "GRM"},
        {"GR", "GM"},
        {"GR", "GRM"},
        {"GM", "GRM"},
    )


def _finish_peeled_product_name(name: str) -> str:
    """Collapse spaces and drop a dangling hyphen left by SYP-100ML peels."""
    cleaned = re.sub(r"\s+", " ", str(name or "").strip())
    cleaned = re.sub(r"[-–—]+\s*$", "", cleaned).strip()
    return cleaned


def _peel_duplicate_pack_from_name(item: Dict[str, Any]) -> bool:
    """Drop a volume already present in packing from the trailing product_name."""
    pack = str(item.get("packing") or "").strip()
    parsed_pack = _pack_num_unit(pack)
    if not parsed_pack:
        return False
    pack_num, pack_unit = parsed_pack
    name = str(item.get("product_name") or "").replace("`", "'").replace("´", "'")
    name = name.strip()
    if not name:
        return False

    def _apply_head(head: str, reason: str) -> bool:
        peeled = _finish_peeled_product_name(head)
        if peeled and re.search(r"[A-Za-z]", peeled):
            item["product_name"] = peeled
            logger.info(
                "STOCK_PDF_RIGHT_ALIGN_QTY %s name=%s pack=%s",
                reason,
                item["product_name"],
                pack,
            )
            return True
        return False

    # Parenthetical volume: "BONNISAN LIQ(200ML)".
    match = _PAREN_PACK_IN_NAME_RE.match(name)
    if match and match.group("num") == pack_num and _pack_units_equivalent(
        match.group("unit"), pack_unit
    ):
        if _apply_head(match.group("head"), "peel_paren_pack"):
            return True

    # Spaced / comma volume: "LIV.52 SYP,100ML" or "… TAB 30S".
    match = _DUP_PACK_IN_NAME_RE.match(name)
    if match and match.group("num") == pack_num and _pack_units_equivalent(
        match.group("unit"), pack_unit
    ):
        if _apply_head(match.group("head"), "peel_dup_pack"):
            return True

    # Glued suffix on last token: "… GEL60ML" / "SYP-100ML" / "PASTE-50G".
    parts = name.split()
    if not parts:
        return False
    last = parts[-1]
    glued = _GLUED_PACK_SUFFIX_RE.match(last)
    if (
        glued
        and glued.group("num") == pack_num
        and _pack_units_equivalent(glued.group("unit"), pack_unit)
    ):
        stem = str(glued.group("head") or "").strip()
        if stem and re.search(r"[A-Za-z]", stem):
            parts[-1] = stem
        else:
            parts = parts[:-1]
        if _apply_head(" ".join(parts), "peel_glued_pack"):
            return True
    return False


def _peel_trailing_pack_into_packing(
    item: Dict[str, Any], columns: List[Dict[str, Any]]
) -> None:
    """Move a pack size glued onto the product name when Packing is empty."""
    from services.stock_native_tables import _right_align_qty_enabled

    if not _right_align_qty_enabled():
        return
    if not any(str(c.get("canonical") or "") == "pack" for c in columns or []):
        return
    if item.get("packing"):
        _peel_duplicate_pack_from_name(item)
        return
    name = str(item.get("product_name") or "").replace("`", "'").replace("´", "'")
    match = _TRAILING_PACK_RE.match(name.strip())
    pack = None
    head = None
    if match:
        head = match.group("name")
        pack = match.group("pack")
    else:
        glued = _GLUED_OR_PAREN_PACK_RE.match(name.strip())
        if glued:
            head = glued.group("head_paren") or glued.group("head_glue")
            pack = glued.group("pack_paren") or glued.group("pack_glue")
    if not head or not pack:
        return
    peeled = _finish_peeled_product_name(head)
    if not peeled or not re.search(r"[A-Za-z]", peeled):
        return
    item["product_name"] = peeled
    item["packing"] = re.sub(r"\s+", " ", str(pack)).strip().replace("`", "'")
    logger.info(
        "STOCK_PDF_RIGHT_ALIGN_QTY peel_pack name=%s pack=%s",
        item["product_name"],
        item["packing"],
    )
    _peel_duplicate_pack_from_name(item)


_LABELED_VALUE_FOOTERS = (
    (re.compile(r"(?i)^open(?:ing)?\s+stock\s+value$"), "opening_value"),
    (re.compile(r"(?i)^opening\s+value$"), "opening_value"),
    (re.compile(r"(?i)^purchase\s+value$"), "receipts_value"),
    (re.compile(r"(?i)^sale\s+value$"), "sales_value"),
    (re.compile(r"(?i)^sales\s+value\s+on\s+prate$"), "sales_value"),
    (re.compile(r"(?i)^closing\s+stock\s+value$"), "closing_value"),
    (re.compile(r"(?i)^closing\s+value$"), "closing_value"),
)
# LstSL / Open / Recd sheets print "Last Month Sales / Open. Value / …" footers.
# Same labels as vision `_LSTSL_FOOTER_LABEL_NAME`; never product rows.
# Printed lines often carry two labels (Last Month Sales + Open. Value) or
# Closing Value plus Order/Pend chrome — peel every label+amount pair.
_LSTSL_LABELED_VALUE_FOOTERS = (
    (re.compile(r"(?i)^last\s*month\s*sales?$"), "last_month_sales_value"),
    (re.compile(r"(?i)^open\.?\s*value$"), "opening_value"),
    (re.compile(r"(?i)^opening\s*(?:stock\s*)?value$"), "opening_value"),
    (re.compile(r"(?i)^receipt\s*value$"), "receipts_value"),
    (re.compile(r"(?i)^sales\s+value\s+on\s+prate$"), "sales_value"),
    (re.compile(r"(?i)^sales?\s*(?:stock\s*)?value$"), "sales_value"),
    (re.compile(r"(?i)^closing\s*(?:stock\s*)?value$"), "closing_value"),
)
_SKIP_VALUE_FOOTER_RE = re.compile(
    r"(?i)^(?:\((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\)\s+)?"
    r"(?:open(?:ing)?\s+stock\s+value|opening\s+value|purchase\s+value|"
    r"sale\s+value|sales\s+value\s+on\s+prate|closing\s+(?:stock\s+)?value|"
    r"sale\s+return\s+value|purchase\s+return\s+value|st\s+value)$"
)
# Prefix match (vision-style): "Closing Value * - Near Expiry" is still a footer.
_SKIP_LSTSL_VALUE_FOOTER_RE = re.compile(
    r"(?i)^(?:last\s*month\s*sales?|open\.?\s*value|"
    r"opening\s*(?:stock\s*)?value|receipt\s*value|"
    r"sales\s+value\s+on\s+prate|"
    r"sales?\s*(?:stock\s*)?value|closing\s*(?:stock\s*)?value|"
    r"order\s*value)\b"
)
_LSTSL_FOOTER_PAIR_RE = re.compile(
    r"(?ix)"
    r"(last\s*month\s*sales?|open\.?\s*value|opening\s*(?:stock\s*)?value|"
    r"receipt\s*value|sales\s+value\s+on\s+prate|"
    r"sales?\s*(?:stock\s*)?value|closing\s*(?:stock\s*)?value|"
    r"order\s*value|pend\.?\s*value)"
    r"\s*"
    r"([+-]?[0-9][0-9,]*(?:\.\d+)?)"
)
_LSTSL_FOOTER_IGNORE_KEYS = frozenset({"order_value", "pend_value"})


def _pdf_words_lstsl_value_footers_enabled() -> bool:
    """STOCK_PDF_WORDS_LSTSL_VALUE_FOOTERS — peel LstSL money footers.

    Default ON. Kill switch STOCK_PDF_WORDS_LSTSL_VALUE_FOOTERS=false.
    """
    return os.getenv(
        "STOCK_PDF_WORDS_LSTSL_VALUE_FOOTERS", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _is_labeled_value_footer_name(name: Any) -> bool:
    """True for printed Sale/Closing/Open value footer labels (not SKUs)."""
    label = re.sub(r"\s+", " ", str(name or "")).strip()
    if not label:
        return False
    if _SKIP_VALUE_FOOTER_RE.match(label):
        return True
    return bool(
        _pdf_words_lstsl_value_footers_enabled()
        and _SKIP_LSTSL_VALUE_FOOTER_RE.match(label)
    )


def _labeled_value_footer_patterns() -> Tuple[
    Tuple[re.Pattern[str], str], ...
]:
    patterns = list(_LABELED_VALUE_FOOTERS)
    if _pdf_words_lstsl_value_footers_enabled():
        patterns.extend(_LSTSL_LABELED_VALUE_FOOTERS)
    return tuple(patterns)


def _footer_label_to_key(label: str) -> Optional[str]:
    """Map a printed footer label to totals key; Order/Pend are ignore-only."""
    cleaned = re.sub(r"\s+", " ", str(label or "")).strip()
    if not cleaned:
        return None
    if re.fullmatch(r"(?i)order\s*value", cleaned):
        return "order_value"
    if re.fullmatch(r"(?i)pend\.?\s*value", cleaned):
        return "pend_value"
    for pattern, key in _labeled_value_footer_patterns():
        if pattern.match(cleaned):
            return key
    return None


def _row_value_footer_parts(
    tokens: List[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[float]]:
    """Label and the single printed amount on a value-footer row."""
    from services.stock_header_resolver import parse_number

    parts: List[str] = []
    number: Optional[float] = None
    for tok in tokens or []:
        if not isinstance(tok, dict):
            continue
        text = str(tok.get("text") or "").strip()
        if not text:
            continue
        parsed = parse_number(text)
        if parsed is not None and re.fullmatch(
            r"-?\d+(?:\.\d+)?", text.replace(",", "")
        ):
            if number is not None:
                return None, None
            number = float(parsed)
            continue
        parts.append(text)
    if not parts:
        return None, None
    label = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return label, number


def _extract_labeled_value_footer_pairs(
    tokens: List[Dict[str, Any]],
) -> Tuple[bool, Dict[str, float]]:
    """Peel one or more label+amount pairs from an LstSL money footer row.

    Returns (is_footer_row, totals_fragment). Dual-label lines and Closing Value
    rows that also print Order/Pend chrome are dropped when only footer chrome
    remains after the pairs.
    """
    from services.stock_header_resolver import parse_number

    if not _pdf_words_lstsl_value_footers_enabled():
        return False, {}
    parts: List[str] = []
    for tok in tokens or []:
        if not isinstance(tok, dict):
            continue
        text = str(tok.get("text") or "").strip()
        if text:
            parts.append(text)
    if not parts:
        return False, {}
    blob = re.sub(r"\s+", " ", " ".join(parts)).strip()
    matches = list(_LSTSL_FOOTER_PAIR_RE.finditer(blob))
    if not matches:
        return False, {}
    found: Dict[str, float] = {}
    saw_footer = False
    for match in matches:
        key = _footer_label_to_key(match.group(1))
        amount = parse_number(match.group(2))
        if key is None or amount is None:
            continue
        saw_footer = True
        if key in _LSTSL_FOOTER_IGNORE_KEYS:
            continue
        found[key] = float(amount)
    if not saw_footer:
        return False, {}
    residual = _LSTSL_FOOTER_PAIR_RE.sub(" ", blob)
    residual = re.sub(r"[\[\]()|:]", " ", residual)
    residual = re.sub(
        r"(?i)\b(?:order|pend|value|sales|month|last|open|opening|"
        r"receipt|closing)\b",
        " ",
        residual,
    )
    residual = re.sub(r"[0-9,.\+\-]+", " ", residual)
    residual = re.sub(r"\s+", " ", residual).strip()
    if residual:
        return False, {}
    return True, found


def _split_labeled_value_footers(
    page_results: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    """Drop printed Sale/Closing/Open value rows and keep their amounts."""
    totals: Dict[str, float] = {}
    cleaned: List[Dict[str, Any]] = []
    patterns = _labeled_value_footer_patterns()
    for page in page_results or []:
        kept: List[List[Dict[str, Any]]] = []
        for row in page.get("data_rows") or []:
            is_multi, multi_totals = _extract_labeled_value_footer_pairs(row)
            if is_multi:
                for key, number in multi_totals.items():
                    totals[key] = float(number)
                    logger.info(
                        "STOCK_PDF_WORDS_VALUE_FOOTER field=%s value=%s",
                        key,
                        number,
                    )
                continue
            label, number = _row_value_footer_parts(row)
            if label and number is not None and _is_labeled_value_footer_name(label):
                stored = False
                for pattern, key in patterns:
                    if pattern.match(label):
                        totals[key] = float(number)
                        logger.info(
                            "STOCK_PDF_WORDS_VALUE_FOOTER field=%s value=%s",
                            key,
                            number,
                        )
                        stored = True
                        break
                if not stored:
                    logger.info(
                        "STOCK_PDF_WORDS_VALUE_FOOTER skip label=%s", label
                    )
                continue
            kept.append(row)
        page_copy = dict(page)
        page_copy["data_rows"] = kept
        cleaned.append(page_copy)
    return cleaned, totals


def _company_from_division_banner(name: Any) -> Optional[str]:
    """Manufacturer line such as 'HIMALAYA ZEAL DIVISION (078)'."""
    raw = re.sub(r"\s+", " ", str(name or "")).strip()
    if not re.search(r"\bDIVISION\b", raw, re.I):
        return None
    cleaned = re.sub(r"\(\s*\d+\s*\)", "", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    if not cleaned or re.search(r"\d", cleaned) or len(cleaned) > 60:
        return None
    return cleaned


def _items_from_resolved_word_columns(
    page_results: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Use header-resolved word columns when the specialised word builders miss.

    Requires opening, purchase, sales, and closing together so a partial header
    does not replace a later parser. Sales-return is optional (many pipe-ruled
    Op.Qnt/Pur.Qnt/Sl.Qnt/Cl.Qnt sheets omit it).
    """
    from services.stock_native_tables import resolver_ready

    needed_with_return = {
        "opening_qty",
        "purchase_qty",
        "sales_qty",
        "sales_return_qty",
        "closing_qty",
        "closing_value",
    }
    needed_movement = {
        "opening_qty",
        "purchase_qty",
        "sales_qty",
        "closing_qty",
        "closing_value",
    }
    needed_qty_only = {
        "opening_qty",
        "purchase_qty",
        "sales_qty",
        "closing_qty",
    }
    cleaned_pages, labeled_totals = _split_labeled_value_footers(page_results)
    ready_pages = []
    qty_only = False
    for page in cleaned_pages:
        ready = resolver_ready(page.get("header_cells") or [])
        if not ready.get("resolved"):
            continue
        canons = {
            str(c.get("canonical") or "") for c in (ready.get("columns") or [])
        }
        has_value = bool(canons & {"closing_value", "sales_value", "opening_value"})
        if needed_with_return <= canons or (
            _resolved_movement_columns_enabled() and needed_movement <= canons
        ):
            ready_pages.append(page)
            if needed_with_return <= canons:
                logger.info(
                    "STOCK_PDF_WORDS_RESOLVED_COLUMNS match=with_sales_return"
                )
            else:
                logger.info(
                    "STOCK_PDF_WORDS_RESOLVED_COLUMNS match=op_pur_sl_cl"
                )
        elif (
            _resolved_movement_columns_enabled()
            and needed_qty_only <= canons
            and not has_value
        ):
            ready_pages.append(page)
            qty_only = True
            logger.info("STOCK_PDF_WORDS_RESOLVED_COLUMNS match=qty_only")
    if not ready_pages:
        return None
    items = build_items_from_pdf_page_results(ready_pages)
    kept: List[Dict[str, Any]] = []
    division_company: Optional[str] = None
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or "").strip()
        if not name or _is_total_row_name(name) or _is_sheet_total_name(name):
            continue
        division = _company_from_division_banner(name)
        if division:
            division_company = division
            logger.info(
                "STOCK_PDF_WORDS_DIVISION_COMPANY company=%s", division
            )
            continue
        if _is_labeled_value_footer_name(name):
            logger.info(
                "STOCK_PDF_WORDS_VALUE_FOOTER product_skip name=%s", name[:60]
            )
            continue
        if re.match(
            r"(?i)^[\*\-#\s]*(?:near\s+expiry|scheme\s+on\s+product)\b",
            name,
        ):
            logger.info("STOCK_PDF_WORDS_LEGEND_SKIP name=%s", name[:60])
            continue
        from services.stock_native_tables import is_software_price_ad_name

        if name.startswith("[") or is_software_price_ad_name(name) or re.search(
            r"software by|end of report|stock qt|total amount|total quantity|"
            r"company\s*total|value\s*->|qnt\s*->|"
            r"prepared\s+by|computer\s+generated|does\s+not\s+require\s+stamp|"
            r"\bDSTK\b|days\s+stock\s+on|\bCIN\s*No\b|\bPAN\s*No\b|\bGSTIN\b|"
            r"UAM/UDYAM|UDYAM-|\bARKAY\s+MEDICOS\b|"
            r"^manufacturer\s*(?:name\s*)?:|"
            r"^report\s*date\b|^page\s+\d+\s+of\s+\d+\b",
            name,
            re.I,
        ):
            continue
        # Bare company banner (HIMALAYA / ZEAL) — not "HIMALAYA- 60306 AACTARIL…".
        if re.match(r"^(?:himalaya|zeal)\s*-?\s*$", name, re.I):
            continue
        if re.fullmatch(r"[\d][\d,.\s]*", name):
            logger.info("STOCK_PDF_WORDS_AMOUNT_NAME_SKIP name=%s", name[:40])
            continue
        if _is_section_banner_item(item):
            logger.info("STOCK_PDF_WORDS_SECTION_SKIP name=%s", name[:60])
            continue
        # Company / division banners have a name but no movement numbers.
        # Keep printed zero-stock SKUs that still have a packing column.
        # Printed 0 is a real qty — do not treat all-zero product rows as banners.
        qty_vals = [
            item.get("opening_qty"),
            item.get("receipts_qty"),
            item.get("sales_qty"),
            item.get("closing_qty"),
        ]
        qty_blank = all(v in (None, "") for v in qty_vals)
        qty_all_zero = all(v in (None, "", 0, 0.0) for v in qty_vals)
        pack_raw = str(item.get("packing") or "").strip()
        # Misaligned first-column "0" under a brand banner is not a pack size.
        if pack_raw in {"0", "0.0", "0.00"} and _BRAND_SECTION_PREFIX_RE.match(name):
            item["packing"] = None
            pack_raw = ""
        # Brand/division line alone (no qtys). Do not drop a real SKU that still
        # starts with HIMALAYA after a bad pending merge — strip the banner.
        if _BRAND_SECTION_PREFIX_RE.match(name):
            if qty_blank or (
                qty_all_zero
                and not _SECTION_PACK_WORD.search(name)
                and not pack_raw
            ):
                logger.info("STOCK_PDF_WORDS_BRAND_BANNER_SKIP name=%s", name[:60])
                continue
            if _brand_section_pending_skip_enabled():
                # Only peel "HIMALAYA ZENITH " / "HIMALYA DRUG " (brand + line).
                stripped = re.sub(
                    r"(?i)^hima(?:laya|lya)\s+\S+\s+",
                    "",
                    name,
                    count=1,
                ).strip()
                if (
                    stripped
                    and stripped.lower() != name.lower()
                    and re.search(r"[A-Za-z]", stripped)
                    and (
                        _SECTION_PACK_WORD.search(stripped)
                        or len(stripped.split()) >= 2
                    )
                ):
                    logger.info(
                        "STOCK_PDF_WORDS_BRAND_PREFIX_STRIP from=%s to=%s",
                        name[:60],
                        stripped[:60],
                    )
                    item["product_name"] = stripped
                    name = stripped
        if qty_blank:
            extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
            has_pack = bool(str(item.get("packing") or "").strip())
            if (
                not has_pack
                and all(
                    extra.get(k) in (None, "", 0, 0.0)
                    for k in (
                        "opening_value",
                        "receipts_value",
                        "sales_value",
                        "closing_value",
                    )
                )
                and item.get("closing_value") in (None, "", 0, 0.0)
            ):
                continue
        kept.append(item)
    if len(kept) < 3:
        return None
    logger.info("STOCK_PDF_WORDS_RESOLVED_COLUMNS rows=%s qty_only=%s", len(kept), qty_only)
    parsed: Dict[str, Any] = {
        "line_items": kept,
        "prefer_letterhead_stockist": True,
    }
    if qty_only:
        parsed["qty_only"] = True
    if division_company:
        parsed["company_name"] = division_company
    footer = _footer_totals_from_page_results(ready_pages)
    if labeled_totals:
        footer = dict(footer or {})
        footer.update(labeled_totals)
    if footer:
        parsed["footer_totals"] = footer
    return parsed


def _pdf_words_letterhead_meta_enabled() -> bool:
    """STOCK_PDF_WORDS_LETTERHEAD_META — banner company + printed address.

    Default ON. Kill switch STOCK_PDF_WORDS_LETTERHEAD_META=false.
    """
    return os.getenv("STOCK_PDF_WORDS_LETTERHEAD_META", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _op_qty_pur_sale_bal_meta_enabled() -> bool:
    """STOCK_PDF_WORDS_OP_QTY_PUR_SALE_BAL_META — default ON (kill switch).

    Tags Op.Qty|Pur.Qty|Sale.Qty|Bal qty grids and fills Seller/Manufacturer
    chrome so reconciliation can soft-skip known printed Bal gaps.
    """
    return os.getenv(
        "STOCK_PDF_WORDS_OP_QTY_PUR_SALE_BAL_META", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _stockist_looks_like_qty_headers(name: Any) -> bool:
    """True when Op/Pur/Sale/Bal column chrome was parked as stockist_name."""
    s = re.sub(r"\s+", " ", str(name or "").strip())
    if not s:
        return False
    hits = 0
    for pat in (
        r"\bItem\s+Name\b",
        r"\bOp\.?\s*Qty\b",
        r"\bPur\.?\s*Qty\b",
        r"\bSale\.?\s*Qty\b",
        r"\bBal\.?\s*Qty\b",
        r"\bTotal\s+Qty\b",
        r"\bPack(?:ing)?\b",
    ):
        if re.search(pat, s, re.I):
            hits += 1
    return hits >= 2


def _letterhead_digits_are_fy_only(line: str) -> bool:
    """True when the only multi-digit runs are a FY/year paren like (2024-2025)."""
    s = str(line or "")
    without_fy = _LETTERHEAD_FY_RE.sub("", s)
    return not bool(re.search(r"\d{2,}", without_fy))


def _opening_received_issued_header_summary_enabled() -> bool:
    """STOCK_PDF_OPENING_RECEIVED_ISSUED_SUMMARY — labeled header money.

    Opening/Received/Issued/Closing sheets print Opening Value / Purchase /
    Sales / Closing Value / Sales Value On prate above the grid. Default ON.
    Kill switch: STOCK_PDF_OPENING_RECEIVED_ISSUED_SUMMARY=false.
    """
    return os.getenv(
        "STOCK_PDF_OPENING_RECEIVED_ISSUED_SUMMARY", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


_OPENING_RECEIVED_ISSUED_HEADER_RE = re.compile(
    r"(?is)"
    r"(?:\bitem\s*name\b.+\bpack\b.+\bopening\b.+\breceived\b.+\bissued\b"
    r".+\bclosing\b)"
    r"|(?:\bopening\s+value\b.+\bclosing\s+value\b)"
    r"|(?:\bsales\s+value\s+on\s+prate\b)"
)

_OPENING_RECEIVED_ISSUED_PAIR_RE = re.compile(
    r"(?ix)"
    r"(?P<label>"
    r"opening\s+value|"
    r"closing\s+value|"
    r"sales\s+value\s+on\s+prate|"
    r"purchase\s+value|"
    r"purchase(?!\s+return)|"
    r"sales?(?!\s+value|\s+return)"
    r")"
    r"\s*[:=\-]?\s*"
    r"(?P<amount>[+-]?\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|[+-]?\d+\.\d{2})"
)


def _pdf_text_from_bytes(file_bytes: Optional[bytes], max_pages: int = 3) -> str:
    if not file_bytes:
        return ""
    # Refuse non-PDF bytes — fitz can mis-read XLSX/ZIP streams as numeric junk
    # and falsely trigger LMS / Values footer detectors.
    if not bytes(file_bytes[:5]).startswith(b"%PDF"):
        return ""
    try:
        import fitz

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        parts: List[str] = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            parts.append(page.get_text("text") or "")
        doc.close()
        return "\n".join(parts)
    except Exception:
        return ""


# Indian / western money tokens printed on stock statement footers.
# Indian grouping is last-3 then pairs of 2: 2,50,813 / 1,23,571 / 12,34,567.
_INDIAN_MONEY = r"\d{1,3}(?:,\d{2})*,\d{3}"
_FOOTER_MONEY_TOKEN = re.compile(
    r"(?<![\d.])("
    + _INDIAN_MONEY
    + r"|\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"  # Western: 250,813.00
    + r"|\d+\.\d{2}"  # Plain decimal: 250325.29
    + r"|\d{4,}"  # Bare whole rupees: 250813
    + r")(?![\d.])"
)

_VALUES_FOOTER_BLOCK = re.compile(
    r"(?is)\bValues?\b\s*(?::\s*[^\n]*)?\s*"
    r"((?:[^\n]*\n){0,3}?"
    r"[^\n]*"
    + _INDIAN_MONEY
    + r"\s+"
    + _INDIAN_MONEY
    + r"[^\n]*)",
)

# "Total Values is based on PRate" / "Total Values" with western decimals
# under Opening|Inward|Sales|[Other]|Closing (qty-only grids).
_TOTAL_VALUES_PRATE_BLOCK = re.compile(
    r"(?is)\bTotal\s+Values?\b(?:\s+is\s+based\s+on\s+\w+)?"
    r"[^\n]*\n?"
    r"([^\n]*(?:\d+\.\d{2}|\d{1,3}(?:,\d{2})*,\d{3})[^\n]*)"
)

# Lone money amount printed below the Total Values / footnote band.
_STANDALONE_FOOTER_MONEY = re.compile(
    r"(?m)^\s*("
    + _INDIAN_MONEY
    + r"|\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"
    + r"|\d+\.\d{2}"
    + r")\s*$"
)


def _parse_footer_money_token(raw: str) -> Optional[float]:
    text = str(raw or "").strip().replace("\u00a0", " ")
    if not text:
        return None
    from services.stock_header_resolver import parse_number

    number = parse_number(text.replace(",", ""))
    if number is None:
        return None
    # Footer aggregates are never tiny placeholder digits.
    if abs(float(number)) < 1.0:
        return None
    return float(number)


def _extract_standalone_footer_amounts(
    blob: str, *, after_pos: int = 0
) -> List[Dict[str, Any]]:
    """Preserve unlabeled money printed below a Total Values / footer band.

    Never guesses meaning — stores amount + raw location only.
    """
    found: List[Dict[str, Any]] = []
    if not blob or after_pos < 0:
        return found
    tail = blob[after_pos:]
    # Skip the Total Values money line itself; keep later lone amounts.
    lines = tail.splitlines()
    saw_total_values = False
    for idx, ln in enumerate(lines):
        if re.search(r"(?i)\btotal\s+values?\b", ln):
            saw_total_values = True
            continue
        if not saw_total_values and after_pos == 0:
            continue
        m = _STANDALONE_FOOTER_MONEY.match(ln)
        if not m:
            continue
        # Skip footnote / section headers that happen to be numeric-only.
        number = _parse_footer_money_token(m.group(1))
        if number is None:
            continue
        # Skip tiny placeholders; keep real rupee footers.
        if abs(number) < 100.0:
            continue
        raw_label = None
        if idx > 0:
            prev = lines[idx - 1].strip()
            if prev and re.search(r"[A-Za-z]", prev) and not re.search(
                r"(?i)\btotal\s+values?\b", prev
            ):
                raw_label = prev[:120]
        found.append(
            {
                "amount": float(number),
                "raw_label": raw_label,
                "source_location": "below_total_values_row",
                "raw_text": m.group(1),
            }
        )
    return found


def extract_universal_stock_footer_financials(text: str) -> Dict[str, Any]:
    """Universal stock-statement footer money band (layout-family, not stockist).

    Recognises:
    - ``Values: …`` / ``Values`` followed by 4 money amounts
      (Opening / Inward / Outward-Sales / Closing) — Zandra Opg/Inw/Out/Clg
    - ``Total Values is based on PRate`` with 4–5 money amounts under
      Opening / Inward / Sales / [Other] / Closing
    - Labeled Opening/Purchase/Sales/Closing value footers
    - Three unlabeled decimals above NEAR EXPIRY DETAIL (LMS/Sales/Closing)

    Returns only printed amounts. Never invents zeros.
    """
    out: Dict[str, Any] = {
        "opening_value": None,
        "inward_value": None,
        "receipts_value": None,
        "sales_value": None,
        "outward_value": None,
        "other_value": None,
        "closing_value": None,
        "lms_value": None,
        "source": None,
        "detected_columns": [],
        "raw_amounts": [],
        "standalone_amounts": [],
    }
    if not (text or "").strip():
        return out
    blob = str(text)

    def _amounts_from_chunk(chunk: str, *, keep_zero: bool = False) -> List[float]:
        amounts: List[float] = []
        for tok in _FOOTER_MONEY_TOKEN.findall(chunk):
            number = _parse_footer_money_token(tok)
            if number is not None:
                amounts.append(number)
                continue
            if keep_zero:
                try:
                    raw = float(str(tok).replace(",", ""))
                except (TypeError, ValueError):
                    continue
                if abs(raw) < 0.0005:
                    amounts.append(0.0)
        return amounts

    # 0) Total Values is based on PRate — Opening/Inward/Sales/[Other]/Closing.
    for match in _TOTAL_VALUES_PRATE_BLOCK.finditer(blob):
        chunk = match.group(0)
        # Prefer amounts on the Total Values line itself so a lone amount on
        # the next line (e.g. 157618.62) is not pulled into the money band.
        label_line = chunk.splitlines()[0] if chunk else chunk
        amounts = _amounts_from_chunk(label_line, keep_zero=True)
        if len(amounts) < 4:
            amounts = _amounts_from_chunk(chunk, keep_zero=True)
        if len(amounts) < 4:
            continue
        # Prefer last 5 (with Other) else last 4 — only from the Values line.
        band = amounts[-5:] if len(amounts) >= 5 else amounts[-4:]
        if len(band) == 5:
            out["opening_value"] = band[0]
            out["inward_value"] = band[1]
            out["receipts_value"] = band[1]
            out["sales_value"] = band[2]
            out["outward_value"] = band[2]
            out["other_value"] = band[3]
            out["closing_value"] = band[4]
            out["detected_columns"] = [
                "opening_value",
                "inward_value",
                "sales_value",
                "other_value",
                "closing_value",
            ]
        else:
            out["opening_value"] = band[0]
            out["inward_value"] = band[1]
            out["receipts_value"] = band[1]
            out["sales_value"] = band[2]
            out["outward_value"] = band[2]
            out["closing_value"] = band[3]
            out["detected_columns"] = [
                "opening_value",
                "inward_value",
                "sales_value",
                "closing_value",
            ]
        out["raw_amounts"] = list(band)
        out["source"] = "total_values_prate_money_band"
        out["standalone_amounts"] = _extract_standalone_footer_amounts(
            blob, after_pos=match.start() + len(label_line)
        )
        logger.info(
            "STOCK_FOOTER_FINANCIALS source=total_values_prate opening=%s "
            "inward=%s sales=%s other=%s closing=%s standalone=%s",
            out.get("opening_value"),
            out.get("inward_value"),
            out.get("sales_value"),
            out.get("other_value"),
            out.get("closing_value"),
            len(out.get("standalone_amounts") or []),
        )
        return out

    # 1) Values: … 2,50,813 1,23,571 1,54,994 2,32,294
    for match in _VALUES_FOOTER_BLOCK.finditer(blob):
        chunk = match.group(0)
        amounts = _amounts_from_chunk(chunk)
        # Prefer the last 4 distinct-ish money tokens in a Values block.
        if len(amounts) >= 4:
            four = amounts[-4:]
            out["opening_value"] = four[0]
            out["inward_value"] = four[1]
            out["receipts_value"] = four[1]
            out["outward_value"] = four[2]
            out["sales_value"] = four[2]
            out["closing_value"] = four[3]
            out["raw_amounts"] = four
            out["source"] = "values_four_money_band"
            out["detected_columns"] = [
                "opening_value",
                "inward_value",
                "outward_value",
                "closing_value",
            ]
            out["standalone_amounts"] = _extract_standalone_footer_amounts(
                blob, after_pos=match.end()
            )
            logger.info(
                "STOCK_FOOTER_FINANCIALS source=values_four opening=%s inward=%s "
                "outward=%s closing=%s",
                four[0],
                four[1],
                four[2],
                four[3],
            )
            return out

    # 2) Labeled Opening/Purchase/Sales/Closing value lines.
    try:
        from services.sales_statement_extractor import _parse_labeled_stock_value_footer

        labeled = _parse_labeled_stock_value_footer(blob) or {}
    except Exception:
        labeled = {}
    if labeled:
        if labeled.get("opening_value") is not None:
            out["opening_value"] = float(labeled["opening_value"])
        if labeled.get("purchase_value") is not None:
            out["inward_value"] = float(labeled["purchase_value"])
            out["receipts_value"] = float(labeled["purchase_value"])
        if labeled.get("sales_value") is not None:
            out["sales_value"] = float(labeled["sales_value"])
            out["outward_value"] = float(labeled["sales_value"])
        if labeled.get("closing_value") is not None:
            out["closing_value"] = float(labeled["closing_value"])
        cols = [k for k, v in out.items() if v is not None and k.endswith("_value")]
        if cols:
            out["source"] = "labeled_stock_value_footer"
            out["detected_columns"] = cols
            out["raw_amounts"] = [
                out.get("opening_value"),
                out.get("inward_value"),
                out.get("sales_value"),
                out.get("closing_value"),
            ]
            return out

    # 3) LMS / Sales / Closing money above NEAR EXPIRY DETAIL.
    money = _money_footer_from_text_before_near_expiry(blob)
    if any(money.get(k) is not None for k in money):
        if money.get("lms_value") is not None:
            out["lms_value"] = money["lms_value"]
        if money.get("sales_value") is not None:
            out["sales_value"] = money["sales_value"]
            out["outward_value"] = money["sales_value"]
        if money.get("closing_value") is not None:
            out["closing_value"] = money["closing_value"]
        out["source"] = "lms_nearexp_money_footer"
        out["detected_columns"] = [
            k
            for k in ("lms_value", "sales_value", "closing_value")
            if out.get(k) is not None
        ]
        out["raw_amounts"] = [
            out.get("lms_value"),
            out.get("sales_value"),
            out.get("closing_value"),
        ]
        return out

    return out


def _image_footer_ocr_text(file_bytes: Optional[bytes]) -> str:
    """OCR the bottom band of a stock-statement image (jpg/png/webp).

    Used when Vision already mapped qty rows but Total Values / standalone
    money only appear in the footer chrome. Never invents amounts.
    """
    if not file_bytes:
        return ""
    head = file_bytes[:16]
    is_image = (
        head.startswith(b"\xff\xd8\xff")  # JPEG
        or head.startswith(b"\x89PNG\r\n\x1a\n")  # PNG
        or head[:4] == b"RIFF"  # WEBP container
        or head[:4] in {b"GIF8", b"II*\x00", b"MM\x00*"}
    )
    if not is_image:
        return ""
    try:
        from services.sales_statement_extractor import _ocr_image_footer_text

        # Slightly taller band so Total Values + standalone amount both fit.
        text = _ocr_image_footer_text(file_bytes, top_ratio=0.62) or ""
        if text.strip():
            logger.info(
                "STOCK_FOOTER_FINANCIALS source=image_footer_ocr chars=%s",
                len(text),
            )
        return text
    except Exception:
        logger.info("STOCK_FOOTER_FINANCIALS image_footer_ocr_failed")
        return ""


def _footer_financials_from_vision_total_rows(
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """Build footer money from peeled vision TOTAL / Total Values rows.

    Remaps money parked in qty slots. Never invents — only copies mapped cells.
    """
    out: Dict[str, Any] = {
        "opening_value": None,
        "inward_value": None,
        "receipts_value": None,
        "sales_value": None,
        "outward_value": None,
        "other_value": None,
        "closing_value": None,
        "lms_value": None,
        "source": None,
        "detected_columns": [],
        "raw_amounts": [],
        "standalone_amounts": [],
    }
    if not isinstance(result, dict):
        return out
    extra = ((result.get("totals") or {}).get("extra") or {})
    rows = extra.get("vision_table_total_rows") or []
    if not isinstance(rows, list) or not rows:
        return out
    try:
        from services.stock_vision_table import _remap_total_values_money_parked_in_qty
    except Exception:
        return out

    named = [
        r
        for r in rows
        if isinstance(r, dict)
        and re.search(r"TOTAL|VALUES?", str(r.get("product_name") or ""), re.I)
    ]
    grand = [
        r
        for r in named
        if re.search(r"(?i)grand\s+total", str(r.get("product_name") or ""))
    ]
    printed = grand[-1] if grand else (named[-1] if named else None)
    if not isinstance(printed, dict):
        return out
    _remap_total_values_money_parked_in_qty(printed)

    def _num(key: str) -> Optional[float]:
        raw = printed.get(key)
        if raw is None and isinstance(printed.get("extra"), dict):
            raw = printed["extra"].get(key)
        try:
            return float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None

    opening = _num("opening_value")
    inward = _num("receipts_value")
    if inward is None and isinstance(printed.get("extra"), dict):
        try:
            pv = printed["extra"].get("purchase_value")
            inward = float(pv) if pv not in (None, "") else None
        except (TypeError, ValueError):
            inward = None
    sales = _num("sales_value")
    closing = _num("closing_value")
    other = None
    if isinstance(printed.get("extra"), dict):
        try:
            ov = printed["extra"].get("other_value")
            other = float(ov) if ov not in (None, "") else None
        except (TypeError, ValueError):
            other = None

    if sales is None and closing is None and opening is None:
        return out

    out["opening_value"] = opening
    out["inward_value"] = inward
    out["receipts_value"] = inward
    out["sales_value"] = sales
    out["outward_value"] = sales
    out["other_value"] = other
    out["closing_value"] = closing
    out["source"] = "vision_total_values_row"
    cols = []
    for key, val in (
        ("opening_value", opening),
        ("inward_value", inward),
        ("sales_value", sales),
        ("other_value", other),
        ("closing_value", closing),
    ):
        if val is not None:
            cols.append(key)
    out["detected_columns"] = cols
    out["raw_amounts"] = [opening, inward, sales, other, closing]
    # Standalone amounts already parked by a prior OCR pass.
    prior = extra.get("standalone_printed_amounts")
    if isinstance(prior, list) and prior:
        out["standalone_amounts"] = list(prior)
    logger.info(
        "STOCK_FOOTER_FINANCIALS source=vision_total_values_row opening=%s "
        "inward=%s sales=%s closing=%s",
        opening,
        inward,
        sales,
        closing,
    )
    return out


def apply_universal_stock_footer_financials(
    result: Dict[str, Any],
    *,
    file_bytes: Optional[bytes] = None,
    text: Optional[str] = None,
) -> Dict[str, Any]:
    """Fill missing aggregate money from printed footers; never invent zeros.

    Product-level sales_value/closing_value stay null on qty-only sheets.
    Aggregate totals are preserved under totals.* and totals.extra.*.
    """
    if not isinstance(result, dict):
        return result
    if result.get("multi_statement") and isinstance(result.get("statements"), list):
        result["statements"] = [
            apply_universal_stock_footer_financials(
                stmt, file_bytes=file_bytes, text=text
            )
            if isinstance(stmt, dict)
            else stmt
            for stmt in result["statements"]
        ]
        return result

    extra0 = ((result.get("totals") or {}).get("extra") or {})
    # Spreadsheet OP/PI/CLQty Total Value footers are already label-mapped
    # (OPVal/PIVal/SI Value/CLValue). Do not re-parse parked text into
    # unrelated aliases like outward_value or swap opening↔closing.
    if (
        str(extra0.get("extraction_method") or "") == "stock_sales_op_pi_clqty"
        or str(extra0.get("total_value_row_source") or "") == "xls_op_pi_clqty_total_value"
        or str(extra0.get("footer_financial_source") or "") == "xls_op_pi_clqty_total_value"
    ):
        return result

    blob = str(text or "").strip()
    if not blob:
        blob = _pdf_text_from_bytes(file_bytes, max_pages=8)
    if not blob:
        # Also accept OCR/page text already parked on the result.
        for key in ("ocr_text", "page_text", "footer_text", "content_text"):
            val = extra0.get(key)
            if isinstance(val, str) and val.strip():
                blob = val
                break
    if not blob.strip() and file_bytes:
        # Image uploads: OCR the bottom band for Total Values / standalone money.
        blob = _image_footer_ocr_text(file_bytes)

    footer = extract_universal_stock_footer_financials(blob) if blob.strip() else {}
    if not footer.get("source"):
        # Fall back to already-mapped vision TOTAL / Total Values rows.
        footer = _footer_financials_from_vision_total_rows(result)
    if not footer.get("source"):
        # Totals may already hold printed money — still mirror UI sums.
        _mirror_line_value_sums_from_totals(result)
        return result

    totals = result.setdefault("totals", {})
    if not isinstance(totals, dict):
        totals = {}
        result["totals"] = totals
    extra = totals.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        totals["extra"] = extra

    def _missing(val: Any) -> bool:
        return val in (None, "")

    # Aggregate financials — fill only when currently missing (don't wipe good money).
    # Also replace bogus qty-sum sales_value (whole number ≈ sum(sales_qty)).
    try:
        sales_qty_sum = sum(
            float(i.get("sales_qty") or 0)
            for i in (result.get("line_items") or [])
            if isinstance(i, dict)
        )
    except (TypeError, ValueError):
        sales_qty_sum = 0.0
    try:
        cur_sales = (
            float(totals["sales_value"])
            if not _missing(totals.get("sales_value"))
            else None
        )
    except (TypeError, ValueError):
        cur_sales = None
    sales_is_qty_sum = (
        cur_sales is not None
        and sales_qty_sum > 0
        and abs(cur_sales - sales_qty_sum) < 0.51
    )

    if footer.get("sales_value") is not None and (
        _missing(totals.get("sales_value")) or sales_is_qty_sum
    ):
        totals["sales_value"] = float(footer["sales_value"])
        extra["sales_value_source"] = str(
            footer.get("source") or "universal_stock_footer_financials"
        )
        extra["has_printed_sales_value"] = True
    if footer.get("closing_value") is not None and _missing(totals.get("closing_value")):
        totals["closing_value"] = float(footer["closing_value"])
        extra["closing_value_source"] = str(
            footer.get("source") or "universal_stock_footer_financials"
        )
    if footer.get("opening_value") is not None:
        if _missing(totals.get("opening_value")):
            totals["opening_value"] = float(footer["opening_value"])
        if _missing(extra.get("opening_value")):
            extra["opening_value"] = float(footer["opening_value"])
    if footer.get("inward_value") is not None:
        if _missing(totals.get("receipts_value")):
            totals["receipts_value"] = float(footer["inward_value"])
        extra["inward_value"] = float(footer["inward_value"])
        if _missing(extra.get("receipts_value")):
            extra["receipts_value"] = float(footer["inward_value"])
        if _missing(extra.get("purchase_value")):
            extra["purchase_value"] = float(footer["inward_value"])
    if footer.get("outward_value") is not None:
        extra["outward_value"] = float(footer["outward_value"])
    if footer.get("other_value") is not None:
        extra["other_value"] = float(footer["other_value"])
    if footer.get("lms_value") is not None:
        extra["lms_value"] = float(footer["lms_value"])
        totals["lms_value"] = float(footer["lms_value"])

    extra["footer_financials"] = {
        k: footer.get(k)
        for k in (
            "opening_value",
            "inward_value",
            "outward_value",
            "other_value",
            "sales_value",
            "closing_value",
            "lms_value",
            "source",
            "detected_columns",
            "raw_amounts",
            "standalone_amounts",
        )
    }
    if footer.get("standalone_amounts"):
        extra["standalone_printed_amounts"] = list(footer["standalone_amounts"])
    extra["footer_financial_source"] = footer.get("source")
    extra["footer_detected_columns"] = list(footer.get("detected_columns") or [])
    # Completeness vs detected source columns (aggregate only).
    missing_cols: List[str] = []
    for col in footer.get("detected_columns") or []:
        if col == "opening_value" and _missing(totals.get("opening_value")) and _missing(
            extra.get("opening_value")
        ):
            missing_cols.append(col)
        elif col in {"inward_value", "receipts_value"} and _missing(
            extra.get("inward_value")
        ):
            missing_cols.append(col)
        elif col in {"outward_value", "sales_value"} and _missing(totals.get("sales_value")):
            missing_cols.append(col)
        elif col == "other_value" and _missing(extra.get("other_value")):
            missing_cols.append(col)
        elif col == "closing_value" and _missing(totals.get("closing_value")):
            missing_cols.append(col)
        elif col == "lms_value" and _missing(extra.get("lms_value")):
            missing_cols.append(col)
    extra["footer_financial_missing_columns"] = missing_cols
    extra["footer_financial_complete"] = not missing_cols
    # Never clobber a more specific statement total_row_source (e.g.
    # free_return_total_value / xls_grand_total) with a generic footer tag.
    if footer.get("source") and not extra.get("total_row_source"):
        extra["total_row_source"] = str(footer["source"])

    # Qty-only product grids: keep line money null (never coerce unknown → 0).
    _qty_only_sources = {
        "values_four_money_band",
        "total_values_prate_money_band",
    }
    if extra.get("qty_only") or footer.get("source") in _qty_only_sources:
        extra["qty_only"] = True
        extra["no_sales_value"] = True
        extra["no_closing_value"] = True
        for it in result.get("line_items") or []:
            if not isinstance(it, dict):
                continue
            for key in ("opening_value", "receipts_value", "sales_value", "closing_value"):
                val = it.get(key)
                if val in (0, 0.0):
                    it[key] = None
                elif val not in (None, ""):
                    try:
                        number = float(val)
                    except (TypeError, ValueError):
                        it[key] = None
                        continue
                    sq = it.get("sales_qty")
                    cq = it.get("closing_qty")
                    if key == "sales_value" and sq is not None:
                        try:
                            if abs(number - float(sq)) < 0.001:
                                it[key] = None
                        except (TypeError, ValueError):
                            pass
                    if key == "closing_value" and cq is not None:
                        try:
                            if abs(number - float(cq)) < 0.001:
                                it[key] = None
                        except (TypeError, ValueError):
                            pass

    # UI reads line_*_value_sum — mirror printed footer money when lines are qty-only.
    _mirror_line_value_sums_from_totals(result)

    logger.info(
        "STOCK_FOOTER_FINANCIALS_APPLIED source=%s opening=%s inward=%s sales=%s "
        "closing=%s complete=%s",
        footer.get("source"),
        totals.get("opening_value") or extra.get("opening_value"),
        extra.get("inward_value"),
        totals.get("sales_value"),
        totals.get("closing_value"),
        extra.get("footer_financial_complete"),
    )
    return result


def _line_has_nonzero_money(items: List[Dict[str, Any]], key: str) -> bool:
    """True when any product row carries a non-zero printed money cell.

    Vision qty-only grids often stamp sales_value/closing_value as 0.0 for
    missing columns — that must not block mirroring printed footer totals
    into line_*_value_sum for the UI.
    """
    for item in items or []:
        if not isinstance(item, dict):
            continue
        raw = item.get(key)
        if raw in (None, ""):
            continue
        try:
            if abs(float(raw)) >= 0.005:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _mirror_line_value_sums_from_totals(result: Dict[str, Any]) -> None:
    """When product rows have no money, mirror totals.* into line_*_value_sum for UI.

    Never invents amounts — only copies already-printed aggregate totals.
    Overwrites stale 0.0 line sums left by qty-only Vision rows.
    """
    if not isinstance(result, dict):
        return
    totals = result.get("totals")
    if not isinstance(totals, dict):
        return
    extra = totals.setdefault("extra", {})
    if not isinstance(extra, dict):
        return
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    has_sv = _line_has_nonzero_money(items, "sales_value")
    has_cv = _line_has_nonzero_money(items, "closing_value")
    if not has_sv and not _stvr_missing(totals.get("sales_value")):
        try:
            sv = float(totals["sales_value"])
            extra["line_sales_value_sum"] = sv
            extra["line_sales_value_sum_source"] = str(
                extra.get("sales_value_source")
                or extra.get("footer_financial_source")
                or "totals_sales_value"
            )
            logger.info(
                "STOCK_LINE_VALUE_SUM_MIRROR field=sales_value value=%s source=%s",
                sv,
                extra["line_sales_value_sum_source"],
            )
        except (TypeError, ValueError):
            pass
    if not has_cv and not _stvr_missing(totals.get("closing_value")):
        try:
            cv = float(totals["closing_value"])
            extra["line_closing_value_sum"] = cv
            extra["line_closing_value_sum_source"] = str(
                extra.get("closing_value_source")
                or extra.get("footer_financial_source")
                or "totals_closing_value"
            )
            logger.info(
                "STOCK_LINE_VALUE_SUM_MIRROR field=closing_value value=%s source=%s",
                cv,
                extra["line_closing_value_sum_source"],
            )
        except (TypeError, ValueError):
            pass


# ---------------------------------------------------------------------------
# Sales Total Value Recovery Fallback (layout-family, not stockist-named)
# ---------------------------------------------------------------------------

_SALES_TOTAL_LABEL_RE = re.compile(
    r"(?ix)^\s*(?:[-–—_=.*]{0,4}\s*)?"
    r"(?:(?:firm|company|division|page|sub)\s+)?"
    r"(?:"
    r"total\s+values?(?:\s+is\s+based\s+on\s+\w+)?"
    r"|grand\s+totals?(?:\s+value|\s+amt|\s+amount)?"
    r"|net\s+sales?(?:\s+value)?"
    r"|total\s+sales?(?:\s+value)?"
    r"|sales?\s+totals?(?:\s+value)?"
    r"|g\.?\s*totals?"
    r")"
    r"\b"
)

_SALES_TOTAL_QTY_LABEL_RE = re.compile(
    r"(?ix)^\s*(?:total\s+(?:quantity|qty|qnt)|quantity\s+total)\b"
)


def _sales_total_value_recovery_enabled() -> bool:
    """STOCK_SALES_TOTAL_VALUE_RECOVERY — recover missing/rejected sales totals.

    Default ON. Kill switch: false.
    """
    return os.getenv("STOCK_SALES_TOTAL_VALUE_RECOVERY", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _stvr_as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stvr_money_like(value: Any) -> bool:
    """True for printed money (decimals or large whole rupees), not small qty."""
    num = _stvr_as_float(value)
    if num is None or abs(num) < 1.0:
        return False
    if abs(num - round(num)) > 0.001:
        return True
    return abs(num) >= 100.0


def _stvr_has_decimal_money(amounts: List[float]) -> bool:
    """True when at least one amount has a non-zero paisa fraction (rupees)."""
    for amount in amounts or []:
        try:
            if abs(float(amount) - round(float(amount))) > 0.001:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _stvr_label_claims_money(label: str) -> bool:
    """TOTAL VALUE / GRAND TOTAL AMT / NET SALES — not bare qty GRAND TOTAL."""
    key = str(label or "")
    if not key.strip():
        return False
    return bool(
        re.search(
            r"(?i)\b(?:total\s+values?(?:\s+is\s+based\s+on\s+\w+)?|"
            r"value\s+total|grand\s+totals?\s*(?:value|amt|amount)"
            r"|net\s+sales?|total\s+sales?\s*(?:value|amt|amount)?"
            r"|sales?\s+totals?\s*(?:value|amt|amount)?|(?:rs\.?|inr)\b)",
            key,
        )
        or re.search(r"(?i)\b(?:value|amt|amount|prate)\b", key)
    )


def _stvr_headers_have_value_column(header_cells: List[Any]) -> bool:
    for cell in header_cells or []:
        text = str(cell or "").replace("\n", " ").strip()
        if re.search(r"(?i)\b(?:value|amt|amount|rs\.?)\b", text):
            # Closing Balance / Opening Balance are qty headers, not money.
            if re.search(r"(?i)\bbalance\b", text) and not re.search(
                r"(?i)\b(?:value|amt|amount)\b", text
            ):
                continue
            if re.search(
                r"(?i)\b(?:qty|quantity|qnt|balance)\b", text
            ) and not re.search(r"(?i)\b(?:value|amt|amount)\b", text):
                continue
            return True
    return False


def _stvr_candidate_is_qty_footer(
    label: str,
    amounts: List[float],
    *,
    has_value_header: bool = False,
) -> bool:
    """Reject TOTAL QUANTITY / bare GRAND TOTAL qty bands as sales/closing value."""
    if _SALES_TOTAL_QTY_LABEL_RE.match(str(label or "")):
        return True
    if re.search(r"(?i)\b(?:total|grand)\s+(?:quantity|qty|qnt)\b", str(label or "")):
        return True
    # Closing Balance / Issue Qty style labels are never money.
    if re.search(
        r"(?i)\b(?:closing\s+balance|opening\s+balance|issue\s+qty|"
        r"total\s+qty|quantity\s+total)\b",
        str(label or ""),
    ):
        return True
    if _stvr_label_claims_money(label):
        return False
    if has_value_header and _stvr_has_decimal_money(amounts):
        return False
    if has_value_header and any(_stvr_money_like(a) for a in amounts or []):
        # Explicit Value/Amt column + money-shaped cell under TOTAL VALUE family.
        # Bare GRAND TOTAL on a qty+value grid with decimals is handled above;
        # whole-only under Value headers can still be rupees (8516 / 20900).
        if _stvr_label_claims_money(label) or re.search(
            r"(?i)\b(?:total\s+value|grand\s+totals?\s*(?:value|amt)?)\b",
            str(label or ""),
        ):
            return False
    # Bare GRAND TOTAL / TOTAL SALES with whole-number-only cells = qty footer
    # (e.g. Total Qty 50200 + Issue 10453 misread as sales/closing value).
    if not _stvr_has_decimal_money(amounts):
        if not has_value_header:
            return True
        if not _stvr_label_claims_money(label) and re.search(
            r"(?i)^\s*(?:grand\s+)?totals?\b", str(label or "")
        ):
            return True
    return False


def _stvr_missing(value: Any) -> bool:
    return value in (None, "")


def _stvr_parse_numbers(cells: List[Any]) -> List[float]:
    out: List[float] = []
    for cell in cells:
        text = str(cell or "").strip().replace("\u00a0", " ").replace(",", "")
        if not text or re.fullmatch(r"[-–—]+", text):
            continue
        num = _parse_footer_money_token(text)
        if num is None:
            # Allow small zeros / whole qtys that sit beside money on TOTAL VALUE.
            try:
                raw = float(text)
            except (TypeError, ValueError):
                continue
            if abs(raw) < 0.0005:
                out.append(0.0)
            continue
        out.append(float(num))
    return out


def _stvr_label_priority(label: str) -> int:
    key = re.sub(r"[^a-z0-9]+", "", str(label or "").lower())
    if "grandtotal" in key and any(tok in key for tok in ("value", "amt", "amount")):
        return 4
    if "grandtotal" in key:
        # Bare GRAND TOTAL is often a qty band — rank below explicit VALUE rows.
        return 2
    if "netsales" in key or "salestotal" in key or "totalsales" in key:
        return 3
    if "totalvalue" in key:
        return 3
    if key.startswith("total") or "total" in key:
        return 1
    return 0


def _stvr_pick_sales_closing_from_amounts(
    amounts: List[float],
    *,
    sale_idx: Optional[int] = None,
    close_idx: Optional[int] = None,
    require_money: bool = True,
) -> Tuple[Optional[float], Optional[float]]:
    """Map TOTAL VALUE numeric cells to sales/closing using header indices when known."""
    if not amounts:
        return None, None
    sales: Optional[float] = None
    closing: Optional[float] = None

    def _accept(cand: float) -> bool:
        if require_money:
            return _stvr_money_like(cand)
        return abs(cand) >= 1.0 or _stvr_money_like(cand)

    if sale_idx is not None and 0 <= sale_idx < len(amounts):
        cand = amounts[sale_idx]
        if _accept(cand):
            sales = float(cand)
    if close_idx is not None and 0 <= close_idx < len(amounts):
        cand = amounts[close_idx]
        if _accept(cand):
            closing = float(cand)
    if sales is not None or closing is not None:
        return sales, closing
    # No header geometry: prefer last two money-like amounts (sales, closing).
    money = [a for a in amounts if _stvr_money_like(a)]
    if len(money) >= 2:
        return float(money[-2]), float(money[-1])
    if len(money) == 1:
        return float(money[0]), None
    # Never fall back to raw whole qty cells as rupees.
    if require_money:
        return None, None
    if len(amounts) >= 2:
        return float(amounts[-2]), float(amounts[-1])
    if len(amounts) == 1:
        return float(amounts[0]), None
    return None, None


def _stvr_header_sale_close_indices(header_cells: List[Any]) -> Tuple[Optional[int], Optional[int]]:
    """Numeric-column indices for SALE / CLOSING *value* when present, else qty."""
    sale_idx: Optional[int] = None
    close_idx: Optional[int] = None
    sale_qty_idx: Optional[int] = None
    close_qty_idx: Optional[int] = None
    num_i = 0
    for cell in header_cells:
        text = str(cell or "").replace("\n", " ").strip()
        if not text:
            continue
        # Skip pure product/name headers — they are not numeric columns.
        compact = re.sub(r"[^a-z0-9]+", "", text.lower())
        if compact in {
            "product",
            "productdescription",
            "productname",
            "item",
            "itemdescription",
            "particulars",
            "particular",
            "name",
        }:
            continue
        looks_numeric_col = bool(
            re.search(
                r"(?i)qty|quantity|stock|value|amt|amount|sale|sales|closing|"
                r"open|purchase|receive|receipt|issue|rate|free|return|balance",
                text,
            )
        )
        if not looks_numeric_col:
            continue
        is_value_col = bool(re.search(r"(?i)\b(?:value|amt|amount)\b", text))
        is_qty_col = bool(
            re.search(r"(?i)\b(?:qty|quantity|qnt|balance)\b", text)
        ) and not is_value_col
        if re.search(
            r"^(?:sale|sales)(?:\s*(?:qty|quantity|value|amt))?$|"
            r"\bsale\s*quantity\b|\bsales?\s*qty\b|"
            r"\bsales?\s*(?:value|amt|amount)\b|"
            r"\bissue\s*(?:qty|value|amt|amount)?\b",
            text,
            re.I,
        ):
            if is_value_col and sale_idx is None:
                sale_idx = num_i
            elif is_qty_col and sale_qty_idx is None:
                sale_qty_idx = num_i
            elif sale_qty_idx is None and sale_idx is None:
                sale_qty_idx = num_i
        if re.search(
            r"^(?:closing|close|cl|bal)(?:\s*(?:qty|quantity|stock|value|amt|balance))?$|"
            r"\bclosing\s*stock\b|\bcl\.?\s*stk\b|\bclosing\s*value\b|"
            r"\bclosing\s*balance\b|\bclosing\s*(?:amt|amount)\b",
            text,
            re.I,
        ):
            if is_value_col and close_idx is None:
                close_idx = num_i
            elif is_qty_col and close_qty_idx is None:
                close_qty_idx = num_i
            elif close_qty_idx is None and close_idx is None:
                close_qty_idx = num_i
        num_i += 1
    # Prefer Value/Amt columns when the sheet prints them; qty indices alone
    # must not drive sales_value / closing_value recovery.
    if sale_idx is None:
        sale_idx = None  # do not use sale_qty_idx for money recovery
    if close_idx is None:
        close_idx = None
    return sale_idx, close_idx


def extract_sales_total_value_from_text(text: str) -> Dict[str, Any]:
    """Find TOTAL VALUE / GRAND TOTAL / NET SALES / unlabeled trailing money.

    Prefers the last GRAND TOTAL; else last TOTAL VALUE / NET SALES. Dedupes
    identical repeated summary rows. Never invents amounts.
    """
    out: Dict[str, Any] = {
        "sales_value": None,
        "closing_value": None,
        "source": None,
        "label": None,
        "page_or_sheet": None,
        "raw_amounts": [],
        "method": None,
        "column_shift_detected": False,
    }
    if not (text or "").strip():
        return out
    lines = [ln.strip() for ln in str(text).replace("\r", "\n").split("\n") if ln.strip()]
    candidates: List[Tuple[int, int, Dict[str, Any]]] = []
    sale_idx: Optional[int] = None
    close_idx: Optional[int] = None
    # Track a recent header for column geometry.
    for ln in lines:
        if re.search(
            r"(?i)\b(?:product|item)\b.*\b(?:sale|sales|closing|open)",
            ln,
        ) or re.search(r"(?i)\bSALE\s*QUANTITY\b|\bCLOSING\s*STOCK\b", ln):
            # Split header tokens loosely.
            parts = re.split(r"[\t|]+|\s{2,}", ln)
            if len(parts) < 3:
                parts = ln.split()
            sale_idx, close_idx = _stvr_header_sale_close_indices(parts)

    seen_pairs: set = set()
    for order, ln in enumerate(lines):
        if _SALES_TOTAL_QTY_LABEL_RE.match(ln):
            continue
        label_m = _SALES_TOTAL_LABEL_RE.match(ln)
        unlabeled = False
        if not label_m:
            # Unlabeled trailing summary: mostly numbers, no product letters.
            nums = _FOOTER_MONEY_TOKEN.findall(ln)
            if len(nums) < 2:
                continue
            if re.search(r"[A-Za-z]{3,}", ln) and not re.search(
                r"(?i)\b(?:total|sales?|value|qty|quantity|closing|opening)\b",
                ln,
            ):
                continue
            if not re.match(r"^[\d,.\s\-–—]+$", re.sub(r"(?i)\b(?:rs\.?|inr)\b", "", ln)):
                # Allow a leading serial-less numeric strip after last product.
                if re.search(r"[A-Za-z]{3,}", ln):
                    continue
            unlabeled = True
            label = "unlabeled_summary"
            priority = 0
        else:
            label = label_m.group(0).strip()
            priority = _stvr_label_priority(label)
        # Numbers on the same line (label stripped).
        rest = _SALES_TOTAL_LABEL_RE.sub("", ln, count=1) if not unlabeled else ln
        amounts = _stvr_parse_numbers(re.split(r"[\t|]+|\s+", rest))
        if len(amounts) < 1:
            continue
        sales, closing = _stvr_pick_sales_closing_from_amounts(
            amounts, sale_idx=sale_idx, close_idx=close_idx
        )
        if sales is None and closing is None:
            continue
        pair = (
            round(sales, 2) if sales is not None else None,
            round(closing, 2) if closing is not None else None,
        )
        if pair in seen_pairs:
            # Identical ERP reprint — keep one (last wins via overwrite below).
            continue
        seen_pairs.add(pair)
        candidates.append(
            (
                priority,
                order,
                {
                    "sales_value": sales,
                    "closing_value": closing,
                    "label": label,
                    "raw_amounts": amounts,
                    "method": (
                        "unlabeled_trailing_summary"
                        if unlabeled
                        else "labeled_total_value_row"
                    ),
                },
            )
        )

    if not candidates:
        return out
    # Prefer highest priority, then last occurrence (multi-page last page wins).
    candidates.sort(key=lambda c: (c[0], c[1]))
    best = candidates[-1][2]
    out.update(best)
    out["source"] = "sales_total_value_recovery"
    return out


def extract_sales_total_value_from_excel_rows(
    rows: List[List[Any]],
    *,
    sheet_name: str = "",
) -> Dict[str, Any]:
    """Scan worksheet rows for TOTAL VALUE / GRAND TOTAL / unlabeled summaries."""
    out: Dict[str, Any] = {
        "sales_value": None,
        "closing_value": None,
        "source": None,
        "label": None,
        "page_or_sheet": sheet_name or None,
        "raw_amounts": [],
        "method": None,
        "column_shift_detected": False,
        "cell_ref": None,
    }
    if not rows:
        return out
    sale_idx: Optional[int] = None
    close_idx: Optional[int] = None
    # Absolute column indices (not numeric-only) for Excel cell mapping.
    sale_col: Optional[int] = None
    close_col: Optional[int] = None
    candidates: List[Tuple[int, int, Dict[str, Any]]] = []
    seen_pairs: set = set()

    for r_i, row in enumerate(rows):
        if not isinstance(row, list) or not row:
            continue
        cells = list(row)
        first = str(cells[0] if cells else "").strip()
        joined = " ".join(str(c).strip() for c in cells if c not in (None, ""))
        # Header geometry.
        if re.search(r"(?i)\b(?:product|item)\s*(?:description|name)?\b", joined) and re.search(
            r"(?i)\b(?:sale|sales|closing|open)", joined
        ):
            sale_col = close_col = None
            for c_i, cell in enumerate(cells):
                text = str(cell or "").replace("\n", " ").strip()
                if not text:
                    continue
                if sale_col is None and re.search(
                    r"\bsale\s*quantity\b|\bsales?\s*qty\b|"
                    r"^(?:sale|sales)$|\bissue\s*(?:qty|value)?\b",
                    text,
                    re.I,
                ):
                    sale_col = c_i
                if close_col is None and re.search(
                    r"\bclosing\s*stock\b|\bclosing\s*(?:qty|value)?\b|"
                    r"^(?:closing|close|bal)$",
                    text,
                    re.I,
                ):
                    close_col = c_i
            # Numeric-only indices among value-ish columns after product.
            sale_idx, close_idx = _stvr_header_sale_close_indices(
                [str(c or "") for c in cells]
            )

        label = first or joined
        is_labeled = bool(_SALES_TOTAL_LABEL_RE.match(label) or _SALES_TOTAL_LABEL_RE.match(joined))
        is_qty_total = bool(
            _SALES_TOTAL_QTY_LABEL_RE.match(label) or _SALES_TOTAL_QTY_LABEL_RE.match(joined)
        )
        if is_qty_total and not re.search(r"(?i)value", label):
            continue

        unlabeled = False
        if not is_labeled:
            # Nameless numeric strip after products (Kaushal-style).
            nums = []
            alpha = 0
            for c in cells:
                t = str(c or "").strip()
                if not t:
                    continue
                if re.search(r"[A-Za-z]{3,}", t):
                    alpha += 1
                n = _stvr_as_float(str(t).replace(",", ""))
                if n is not None:
                    nums.append(float(n))
            if alpha > 0 or len(nums) < 2:
                continue
            if not any(_stvr_money_like(n) for n in nums):
                continue
            unlabeled = True
            label = "unlabeled_summary"
            priority = 0
        else:
            priority = _stvr_label_priority(label)

        sales: Optional[float] = None
        closing: Optional[float] = None
        cell_ref = None
        if sale_col is not None and sale_col < len(cells):
            sales = _stvr_as_float(str(cells[sale_col]).replace(",", ""))
            cell_ref = f"{sheet_name or 'sheet'}!R{r_i + 1}C{sale_col + 1}"
        if close_col is not None and close_col < len(cells):
            closing = _stvr_as_float(str(cells[close_col]).replace(",", ""))
        if sales is None and closing is None:
            amounts = _stvr_parse_numbers([str(c) for c in cells[1:]])
            if not amounts:
                amounts = _stvr_parse_numbers([str(c) for c in cells])
            sales, closing = _stvr_pick_sales_closing_from_amounts(
                amounts, sale_idx=sale_idx, close_idx=close_idx
            )
        if sales is None and closing is None:
            continue
        # Skip TOTAL QUANTITY when values are small whole qtys only.
        if (
            is_labeled
            and not unlabeled
            and not re.search(r"(?i)value|grand|net\s+sales", label)
            and sales is not None
            and not _stvr_money_like(sales)
        ):
            continue
        pair = (
            round(sales, 2) if sales is not None else None,
            round(closing, 2) if closing is not None else None,
        )
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        candidates.append(
            (
                priority,
                r_i,
                {
                    "sales_value": sales,
                    "closing_value": closing,
                    "label": label,
                    "raw_amounts": [sales, closing],
                    "method": (
                        "excel_unlabeled_summary"
                        if unlabeled
                        else "excel_total_value_row"
                    ),
                    "page_or_sheet": sheet_name or None,
                    "cell_ref": cell_ref,
                },
            )
        )

    if not candidates:
        return out
    candidates.sort(key=lambda c: (c[0], c[1]))
    best = candidates[-1][2]
    out.update(best)
    out["source"] = "sales_total_value_recovery"
    return out


def _stvr_excel_sheets_from_bytes(
    file_bytes: Optional[bytes],
) -> List[Tuple[str, List[List[Any]]]]:
    if not file_bytes:
        return []
    sheets: List[Tuple[str, List[List[Any]]]] = []
    head = file_bytes[:8]
    # OOXML xlsx
    if head.startswith(b"PK"):
        try:
            from openpyxl import load_workbook

            wb = load_workbook(filename=__import__("io").BytesIO(file_bytes), data_only=True, read_only=True)
            for name in wb.sheetnames:
                ws = wb[name]
                rows: List[List[Any]] = []
                for row in ws.iter_rows(values_only=True):
                    rows.append(list(row))
                sheets.append((name, rows))
            wb.close()
            return sheets
        except Exception:
            logger.info("STOCK_SALES_TOTAL_VALUE_RECOVERY excel_xlsx_read_failed")
    # Legacy xls
    try:
        import xlrd

        wb = xlrd.open_workbook(file_contents=file_bytes)
        for sh in wb.sheets():
            rows = []
            for r in range(sh.nrows):
                rows.append([sh.cell_value(r, c) for c in range(sh.ncols)])
            sheets.append((sh.name, rows))
        return sheets
    except Exception:
        logger.info("STOCK_SALES_TOTAL_VALUE_RECOVERY excel_xls_read_failed")
    return sheets


def _stvr_needs_recovery(result: Dict[str, Any]) -> Dict[str, Any]:
    """Decide whether sales/closing totals need recovery + column-shift hints."""
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    qty_sum = 0.0
    for it in items:
        q = _stvr_as_float(it.get("sales_qty"))
        if q is not None:
            qty_sum += q
    sales = _stvr_as_float(totals.get("sales_value"))
    closing = _stvr_as_float(totals.get("closing_value"))
    rejected = _stvr_as_float(extra.get("rejected_sales_value"))
    rejected_cv = _stvr_as_float(extra.get("rejected_closing_value"))
    sales_missing = _stvr_missing(totals.get("sales_value"))
    closing_missing = _stvr_missing(totals.get("closing_value"))
    sales_is_qty_sum = (
        sales is not None and qty_sum > 0 and abs(sales - qty_sum) < 0.51
    )
    # Column shift: money parked in totals.sales_qty while sales_value empty.
    totals_sq = _stvr_as_float(totals.get("sales_qty"))
    if totals_sq is None:
        totals_sq = _stvr_as_float(extra.get("sales_qty"))
    column_shift = bool(
        sales_missing
        and totals_sq is not None
        and _stvr_money_like(totals_sq)
        and (qty_sum <= 0 or abs(totals_sq - qty_sum) > 0.51)
    )
    # Line-level: many rows have money in sales_qty / qty in sales_value.
    line_shift = 0
    for it in items:
        sq = _stvr_as_float(it.get("sales_qty"))
        sv = _stvr_as_float(it.get("sales_value"))
        if sq is not None and _stvr_money_like(sq) and (
            sv is None or (sv is not None and abs(sv - round(sv)) < 0.001 and abs(sv) < 500)
        ):
            line_shift += 1
    if line_shift >= max(1, len(items) // 3) and len(items) >= 1:
        column_shift = True
    # Column-shift repair can run without treating a healthy money total as missing.
    sales_ok = (
        sales is not None
        and _stvr_money_like(sales)
        and not sales_is_qty_sum
    )
    need_sales = bool(
        (not sales_ok)
        and (
            sales_missing
            or sales_is_qty_sum
            or (rejected is not None and (sales_missing or sales_is_qty_sum))
            or column_shift
        )
    )
    need_closing = bool(
        closing_missing or (rejected_cv is not None and closing_missing)
    )
    return {
        "need_sales": need_sales,
        "need_closing": need_closing,
        "column_shift": column_shift,
        "qty_sum": qty_sum,
        "rejected_sales": rejected,
        "rejected_closing": rejected_cv,
        "sales": sales,
        "closing": closing,
    }


def apply_sales_total_value_recovery_fallback(
    result: Dict[str, Any],
    *,
    file_bytes: Optional[bytes] = None,
    text: Optional[str] = None,
) -> Dict[str, Any]:
    """Recover missing/rejected/mis-mapped totals.sales_value from source.

    Runs after primary extraction + sanitize. Never invents amounts. Never
    fills product-level money from document totals. Default ON.
    """
    if not _sales_total_value_recovery_enabled():
        return result
    if not isinstance(result, dict):
        return result
    if result.get("multi_statement") and isinstance(result.get("statements"), list):
        result["statements"] = [
            apply_sales_total_value_recovery_fallback(
                stmt, file_bytes=file_bytes, text=text
            )
            if isinstance(stmt, dict)
            else stmt
            for stmt in result["statements"]
        ]
        return result

    need = _stvr_needs_recovery(result)
    totals = result.setdefault("totals", {})
    if not isinstance(totals, dict):
        totals = {}
        result["totals"] = totals
    extra = totals.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        totals["extra"] = extra

    source_format = str(result.get("source_format") or extra.get("source_format") or "")
    log_base = {
        "status": "skipped",
        "source_format": source_format,
        "page_or_sheet": None,
        "cell_ref": None,
        "original_sales_value": totals.get("sales_value"),
        "original_closing_value": totals.get("closing_value"),
        "recovered_sales_value": None,
        "recovered_closing_value": None,
        "recovery_method": None,
        "column_shift_detected": bool(need.get("column_shift")),
        "validation_result": None,
        "reason": None,
    }

    if not need.get("need_sales") and not need.get("need_closing"):
        log_base["status"] = "not_needed"
        log_base["validation_result"] = "ok_primary"
        extra["sales_total_value_recovery"] = log_base
        # Still mirror UI sums when footer already filled totals but lines are qty-only.
        _mirror_line_value_sums_from_totals(result)
        logger.info(
            "STOCK_SALES_TOTAL_VALUE_RECOVERY status=not_needed format=%s sales=%s",
            source_format,
            totals.get("sales_value"),
        )
        return result

    # 1) Column-shift repair on lines first (header/geometry-aware guard).
    if need.get("column_shift"):
        try:
            apply_universal_sales_qty_value_guard(result)
            log_base["column_shift_detected"] = True
        except Exception:
            pass
        # Totals-level: money parked in sales_qty slot.
        totals_sq = _stvr_as_float(totals.get("sales_qty"))
        if totals_sq is None:
            totals_sq = _stvr_as_float(extra.get("sales_qty"))
        if (
            _stvr_missing(totals.get("sales_value"))
            and totals_sq is not None
            and _stvr_money_like(totals_sq)
            and abs(totals_sq - float(need.get("qty_sum") or 0)) > 0.51
        ):
            totals["sales_value"] = float(totals_sq)
            if "sales_qty" in totals:
                totals["sales_qty"] = need.get("qty_sum")
            extra["sales_value_source"] = "sales_total_value_recovery_column_shift"
            log_base["recovered_sales_value"] = totals["sales_value"]
            log_base["recovery_method"] = "totals_column_shift"
            log_base["column_shift_detected"] = True
            need["need_sales"] = False

    # 2) Restore sanitize-rejected money when it is money-shaped (not qty sum).
    rejected = need.get("rejected_sales")
    if (
        need.get("need_sales")
        and rejected is not None
        and _stvr_money_like(rejected)
        and abs(float(rejected) - float(need.get("qty_sum") or 0)) > 0.51
    ):
        totals["sales_value"] = float(rejected)
        extra["sales_value_source"] = "sales_total_value_recovery_rejected_restore"
        extra["total_row_source"] = (
            extra.get("total_row_source") or "sales_total_value_recovery"
        )
        log_base["recovered_sales_value"] = totals["sales_value"]
        log_base["recovery_method"] = "restore_rejected_sales_value"
        log_base["validation_result"] = "restored_rejected_money"
        need["need_sales"] = False

    rejected_cv = need.get("rejected_closing")
    if (
        need.get("need_closing")
        and rejected_cv is not None
        and _stvr_money_like(rejected_cv)
    ):
        totals["closing_value"] = float(rejected_cv)
        extra["closing_value_source"] = "sales_total_value_recovery_rejected_restore"
        log_base["recovered_closing_value"] = totals["closing_value"]
        need["need_closing"] = False

    # 3) Peel already-mapped TOTAL products parked on extras.
    if need.get("need_sales") or need.get("need_closing"):
        peeled = extra.get("vision_table_total_rows") or []
        if isinstance(peeled, list) and peeled:
            try:
                from services.stock_vision_table import _promote_printed_total_row_money

                promoted = _promote_printed_total_row_money(totals, peeled)
            except Exception:
                promoted = []
            if promoted:
                log_base["recovery_method"] = "peeled_total_row"
                log_base["recovered_sales_value"] = totals.get("sales_value")
                log_base["recovered_closing_value"] = totals.get("closing_value")
                if not _stvr_missing(totals.get("sales_value")):
                    need["need_sales"] = False
                if not _stvr_missing(totals.get("closing_value")):
                    need["need_closing"] = False

    recovered: Dict[str, Any] = {}

    # 4) Text / OCR / PDF chrome.
    if need.get("need_sales") or need.get("need_closing"):
        blob = str(text or "").strip()
        if not blob:
            blob = _pdf_text_from_bytes(file_bytes, max_pages=20)
        if not blob:
            for key in ("ocr_text", "page_text", "footer_text", "content_text"):
                val = extra.get(key)
                if isinstance(val, str) and val.strip():
                    blob = val
                    break
        if blob.strip():
            recovered = extract_sales_total_value_from_text(blob)
            if recovered.get("sales_value") is not None or recovered.get("closing_value") is not None:
                log_base["page_or_sheet"] = recovered.get("page_or_sheet")
                log_base["recovery_method"] = recovered.get("method")

    # 5) Excel worksheets (every sheet; last-page / last matching wins).
    if (need.get("need_sales") or need.get("need_closing")) and not recovered.get(
        "sales_value"
    ):
        for sheet_name, rows in _stvr_excel_sheets_from_bytes(file_bytes):
            hit = extract_sales_total_value_from_excel_rows(
                rows, sheet_name=sheet_name
            )
            if hit.get("sales_value") is None and hit.get("closing_value") is None:
                continue
            # Prefer GRAND TOTAL over earlier TOTAL VALUE across sheets.
            if (
                not recovered.get("sales_value")
                or _stvr_label_priority(str(hit.get("label") or ""))
                >= _stvr_label_priority(str(recovered.get("label") or ""))
            ):
                recovered = hit
        if recovered.get("method"):
            log_base["recovery_method"] = recovered.get("method")
            log_base["page_or_sheet"] = recovered.get("page_or_sheet")
            log_base["cell_ref"] = recovered.get("cell_ref")

    # Apply recovered amounts when still needed.
    applied = False
    if (
        need.get("need_sales")
        and recovered.get("sales_value") is not None
        and _stvr_money_like(recovered["sales_value"])
        and abs(float(recovered["sales_value"]) - float(need.get("qty_sum") or 0)) > 0.51
    ):
        totals["sales_value"] = float(recovered["sales_value"])
        extra["sales_value_source"] = "sales_total_value_recovery"
        extra["total_row_source"] = "sales_total_value_recovery"
        log_base["recovered_sales_value"] = totals["sales_value"]
        applied = True
        need["need_sales"] = False
    if need.get("need_closing") and recovered.get("closing_value") is not None:
        if _stvr_money_like(recovered["closing_value"]) or abs(
            float(recovered["closing_value"])
        ) >= 1.0:
            totals["closing_value"] = float(recovered["closing_value"])
            extra["closing_value_source"] = "sales_total_value_recovery"
            extra["total_row_source"] = (
                extra.get("total_row_source") or "sales_total_value_recovery"
            )
            log_base["recovered_closing_value"] = totals["closing_value"]
            applied = True
            need["need_closing"] = False

    # Stamp recovery source only when we actually filled a missing total —
    # never clobber free_return_total_value / xls_grand_total / etc.
    _weak_sources = {
        None,
        "",
        "line_item_sum",
        "line_sum_fallback",
        "sales_total_value_recovery",
    }
    if applied and extra.get("total_row_source") in _weak_sources:
        extra["total_row_source"] = "sales_total_value_recovery"

    # No product-level money (ignore Vision's stamped 0.0): mirror totals for UI.
    _mirror_line_value_sums_from_totals(result)
    if _stvr_missing(totals.get("sales_value")) and not _line_has_nonzero_money(
        [i for i in (result.get("line_items") or []) if isinstance(i, dict)],
        "sales_value",
    ):
        extra.setdefault("line_sales_value_sum", None)
        extra.setdefault(
            "line_sales_value_sum_source", "unavailable_no_line_values"
        )
    if _stvr_missing(totals.get("closing_value")) and not _line_has_nonzero_money(
        [i for i in (result.get("line_items") or []) if isinstance(i, dict)],
        "closing_value",
    ):
        extra.setdefault("line_closing_value_sum", None)
        extra.setdefault(
            "line_closing_value_sum_source", "unavailable_no_line_values"
        )

    if applied or log_base.get("recovered_sales_value") is not None:
        log_base["status"] = "recovered"
        log_base["validation_result"] = "source_verified"
        if recovered.get("label"):
            log_base["label"] = recovered.get("label")
        if recovered.get("column_shift_detected"):
            log_base["column_shift_detected"] = True
    elif need.get("need_sales"):
        log_base["status"] = "absent"
        log_base["reason"] = "sales_value_not_found_in_source"
        log_base["validation_result"] = "genuinely_absent"
        # Preserve null — do not fabricate.
        if _stvr_missing(totals.get("sales_value")):
            totals["sales_value"] = None
    else:
        log_base["status"] = "ok"
        log_base["validation_result"] = "ok_after_shift_or_restore"

    extra["sales_total_value_recovery"] = log_base
    logger.info(
        "STOCK_SALES_TOTAL_VALUE_RECOVERY status=%s format=%s method=%s "
        "original=%s recovered=%s shift=%s validation=%s sheet=%s cell=%s reason=%s",
        log_base.get("status"),
        source_format,
        log_base.get("recovery_method"),
        log_base.get("original_sales_value"),
        log_base.get("recovered_sales_value"),
        log_base.get("column_shift_detected"),
        log_base.get("validation_result"),
        log_base.get("page_or_sheet"),
        log_base.get("cell_ref"),
        log_base.get("reason"),
    )
    return result


def _looks_like_lms_nearexp_stock_pdf(text: str) -> bool:
    """Loose fingerprint: LMS movement grid and/or NEAR EXPIRY DETAIL money band."""
    if not text:
        return False
    has_lms = bool(re.search(r"(?i)\blms\b", text))
    has_opening = bool(re.search(r"(?i)\bopening\b", text))
    has_sales = bool(re.search(r"(?i)\bsales?\b", text))
    has_closing = bool(re.search(r"(?i)\bclosing\b", text))
    has_near = bool(re.search(r"(?i)near\s+expir", text))
    if has_lms and has_opening and has_sales and has_closing:
        return True
    if has_lms and has_near:
        return True
    money = _money_footer_from_text_before_near_expiry(text)
    if has_near and money.get("sales_value") is not None and money.get("closing_value") is not None:
        return True
    return False


def apply_lms_nearexp_pdf_fallback(
    result: Dict[str, Any],
    *,
    file_bytes: Optional[bytes] = None,
    text: Optional[str] = None,
) -> Dict[str, Any]:
    """Last-chance repair for OPENING/PURCHASE/LMS + NEAR EXPIRY DETAIL PDFs.

    Runs after any parser (including Gemini). Re-reads printed money footers and
    batch detail rows from PDF text so sales_value is never the qty sum (e.g.
    1569), closing_value is restored, and all near-expiry batches land on JSON.
    """
    if not isinstance(result, dict):
        return result
    if result.get("multi_statement") and isinstance(result.get("statements"), list):
        result["statements"] = [
            apply_lms_nearexp_pdf_fallback(stmt, file_bytes=file_bytes, text=text)
            if isinstance(stmt, dict)
            else stmt
            for stmt in result["statements"]
        ]
        return result

    blob = str(text or "").strip()
    if not blob:
        blob = _pdf_text_from_bytes(file_bytes)
    if not blob or not _looks_like_lms_nearexp_stock_pdf(blob):
        return result

    totals = result.setdefault("totals", {})
    if not isinstance(totals, dict):
        totals = {}
        result["totals"] = totals
    extra = totals.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        totals["extra"] = extra

    money = _money_footer_from_text_before_near_expiry(blob)
    details_raw = parse_near_expiry_detail_section(blob)
    details = [_near_expiry_detail_record(row) for row in details_raw]

    # Line money must stay null — PDF has qty columns only.
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    sales_qty_sum = 0.0
    for it in items:
        for key in (
            "opening_value",
            "receipts_value",
            "sales_value",
            "closing_value",
        ):
            val = it.get(key)
            try:
                number = float(val) if val not in (None, "") else None
            except (TypeError, ValueError):
                number = None
            if number is None:
                it[key] = None
                continue
            sq = it.get("sales_qty")
            cq = it.get("closing_qty")
            drop = abs(number) < 0.001
            try:
                if key == "sales_value" and sq is not None and abs(number - float(sq)) < 0.001:
                    drop = True
                if key == "closing_value" and cq is not None and abs(number - float(cq)) < 0.001:
                    drop = True
            except (TypeError, ValueError):
                pass
            if abs(number - round(number)) < 0.001 and abs(number) < 5000:
                drop = True
            it[key] = None if drop else number
        try:
            sales_qty_sum += float(it.get("sales_qty") or 0)
        except (TypeError, ValueError):
            pass

    # Replace bogus totals.sales_value (== qty sum like 1569) with money footer.
    try:
        cur_sales = float(totals["sales_value"]) if totals.get("sales_value") not in (None, "") else None
    except (TypeError, ValueError):
        cur_sales = None
    money_sales = money.get("sales_value")
    money_lms = money.get("lms_value")
    money_closing = money.get("closing_value")
    if money_sales is None and money_lms is not None:
        money_sales = money_lms
    bogus_sales = (
        cur_sales is None
        or (
            sales_qty_sum > 0
            and abs(cur_sales - sales_qty_sum) < 0.51
        )
        or (
            money_sales is not None
            and abs(cur_sales - float(money_sales)) > 0.05
            and abs(cur_sales - round(cur_sales)) < 0.001
            and abs(cur_sales) < 20000
        )
    )
    if money_sales is not None and bogus_sales:
        totals["sales_value"] = float(money_sales)
        logger.info(
            "STOCK_LMS_NEAREXP_FALLBACK sales_value=%s was=%s qty_sum=%s",
            money_sales,
            cur_sales,
            sales_qty_sum,
        )
    if money_closing is not None:
        totals["closing_value"] = float(money_closing)
        extra["closing_value"] = float(money_closing)
        logger.info(
            "STOCK_LMS_NEAREXP_FALLBACK closing_value=%s",
            money_closing,
        )
    if money_lms is not None:
        extra["lms_value"] = float(money_lms)
        extra["last_month_sales_value"] = float(money_lms)
        # Also mirror under totals for consumers that only read top-level money.
        totals["lms_value"] = float(money_lms)

    if details:
        extra["near_expiry_details"] = details
        result["near_expiry_details"] = list(details)
        # Duplicate under totals for schemas that only forward totals.*.
        totals["near_expiry_details"] = list(details)
        annotate_near_expiry_discrepancies(items, details)
        logger.info(
            "STOCK_LMS_NEAREXP_FALLBACK near_expiry_details=%s",
            len(details),
        )

    extra["qty_only"] = True
    extra["no_sales_value"] = True
    extra["no_closing_value"] = True
    extra["lms_nearexp_fallback"] = True
    extra["total_row_source"] = "lms_nearexp_money_footer"
    if not str(extra.get("extraction_method") or "").startswith("pdf_words_lms"):
        # Keep original method, but mark repair.
        extra["lms_nearexp_repaired_from"] = extra.get("extraction_method")
    return result


def finalize_lms_qty_only_statement(result: Dict[str, Any]) -> Dict[str, Any]:
    """Qty-only OPENING/PURCHASE/LMS grids: no per-line money; keep footer money.

    Clears invented sales_value(=sales_qty) / closing_value=0 on lines, restores
    LMS/Sales/Closing money totals from extra when sanitize wiped them, and
    stamps no_*_value so later coercers keep nulls.
    """
    if not isinstance(result, dict):
        return result
    totals = result.setdefault("totals", {})
    if not isinstance(totals, dict):
        return result
    extra = totals.setdefault("extra", {})
    if not isinstance(extra, dict):
        return result
    method = str(extra.get("extraction_method") or "")
    if method not in {"pdf_words_lms_qty", "pdf_stacked_lms_nearexp"} and not (
        extra.get("qty_only") and extra.get("lms_value") is not None
    ):
        # Still apply when near-expiry LMS layout was tagged.
        if not (extra.get("qty_only") and extra.get("near_expiry_details")):
            if not extra.get("lms_nearexp_fallback"):
                return result
    extra["qty_only"] = True
    extra["no_sales_value"] = True
    extra["no_closing_value"] = True
    extra["no_opening_value"] = True
    extra["no_receipts_value"] = True
    for it in result.get("line_items") or []:
        if not isinstance(it, dict):
            continue
        # Never invent per-line money on this layout.
        for key in (
            "opening_value",
            "receipts_value",
            "sales_value",
            "closing_value",
        ):
            val = it.get(key)
            if val in (None, ""):
                it[key] = None
                continue
            try:
                number = float(val)
            except (TypeError, ValueError):
                it[key] = None
                continue
            # Drop zeros and qty-copies (sales_value == sales_qty).
            if abs(number) < 0.001:
                it[key] = None
                continue
            if key == "sales_value":
                sq = it.get("sales_qty")
                try:
                    if sq is not None and abs(number - float(sq)) < 0.001:
                        it[key] = None
                        continue
                except (TypeError, ValueError):
                    pass
            if key == "closing_value":
                cq = it.get("closing_qty")
                try:
                    if cq is not None and abs(number - float(cq)) < 0.001:
                        it[key] = None
                        continue
                except (TypeError, ValueError):
                    pass
            # Any remaining small whole number on a qty-only sheet is not money.
            if abs(number - round(number)) < 0.001 and abs(number) < 5000:
                it[key] = None
    # Restore footer money (LMS / Sales / Closing values).
    lms_v = extra.get("lms_value")
    if lms_v is None:
        lms_v = extra.get("last_month_sales_value")
    if lms_v is None:
        lms_v = totals.get("lms_value")
    sales_v = totals.get("sales_value")
    closing_v = totals.get("closing_value")
    # If sanitize replaced money with qty sum (~sales_qty total), restore.
    try:
        sales_qty_sum = float(extra.get("sales_qty") or 0)
    except (TypeError, ValueError):
        sales_qty_sum = 0.0
    if sales_qty_sum <= 0:
        try:
            sales_qty_sum = sum(
                float(i.get("sales_qty") or 0)
                for i in (result.get("line_items") or [])
                if isinstance(i, dict)
            )
        except (TypeError, ValueError):
            sales_qty_sum = 0.0
    try:
        sales_f = float(sales_v) if sales_v not in (None, "") else None
    except (TypeError, ValueError):
        sales_f = None
    if sales_f is not None and sales_qty_sum > 0 and abs(sales_f - sales_qty_sum) < 0.51:
        sales_f = None
    if sales_f is None and lms_v is not None:
        try:
            sales_f = float(lms_v)
        except (TypeError, ValueError):
            sales_f = None
    if sales_f is not None:
        totals["sales_value"] = float(sales_f)
    if closing_v in (None, "", 0, 0.0) and extra.get("closing_value") is not None:
        closing_v = extra.get("closing_value")
    try:
        closing_f = float(closing_v) if closing_v not in (None, "") else None
    except (TypeError, ValueError):
        closing_f = None
    if closing_f is not None and abs(closing_f) >= 1.0:
        totals["closing_value"] = float(closing_f)
        extra["closing_value"] = float(closing_f)
    if lms_v is not None:
        try:
            extra["lms_value"] = float(lms_v)
            extra["last_month_sales_value"] = float(lms_v)
            totals["lms_value"] = float(lms_v)
        except (TypeError, ValueError):
            pass
    extra["total_row_source"] = "lms_nearexp_money_footer"
    # Normalize near-expiry detail records for consumers.
    details = extra.get("near_expiry_details") or result.get("near_expiry_details")
    if isinstance(details, list) and details:
        normalized = [
            _near_expiry_detail_record(row) if isinstance(row, dict) else row
            for row in details
        ]
        extra["near_expiry_details"] = normalized
        totals["near_expiry_details"] = list(normalized)
        result["near_expiry_details"] = list(normalized)
        annotate_near_expiry_discrepancies(result.get("line_items") or [], normalized)
    return result


def _universal_sales_qty_value_guard_enabled() -> bool:
    """STOCK_UNIVERSAL_SALES_QTY_VALUE_GUARD — all-path sales qty↔value repair.

    Default ON. Runs after every PDF/Excel/image extract. Re-slots printed
    sales_qty / sales_value when they are clearly swapped or when qty was
    copied into value. Never invents money from rate×qty. Kill switch:
    STOCK_UNIVERSAL_SALES_QTY_VALUE_GUARD=false.
    """
    return os.getenv(
        "STOCK_UNIVERSAL_SALES_QTY_VALUE_GUARD", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _guard_as_float(raw: Any) -> Optional[float]:
    if raw in (None, ""):
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _guard_money_like(val: float) -> bool:
    """Rupee-shaped: has paise (preferred), or very large whole rupees."""
    if abs(val) < 1.0:
        return False
    # Paise / decimal rupees — strongest money signal (avoids qty 530 → money).
    if abs(val - round(val)) > 0.001:
        return True
    # Whole-rupee VALUE cells only when far above typical pack/sales qtys.
    return abs(val) >= 1000.0


def _guard_qty_like(val: float) -> bool:
    """Whole non-money quantity. Zero is blank, not a printed qty for unswap."""
    return abs(val - round(val)) <= 0.001 and 0 < abs(val) < 500.0


def _guard_whole_qty(val: float) -> bool:
    """Whole printed qty including large Issue counts (e.g. 2551)."""
    return abs(val - round(val)) <= 0.001 and abs(val) > 0.001


def _row_qty_balances(
    opening: float, receipt: float, sales: float, closing: float
) -> bool:
    return abs((opening + receipt - sales) - closing) <= 0.51


def _item_receipt_qty(item: Dict[str, Any]) -> float:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    for key in ("receipts_qty", "purchase_qty"):
        val = _guard_as_float(item.get(key))
        if val is not None:
            return float(val)
    if isinstance(extra, dict):
        for key in ("receipts_qty", "purchase_qty"):
            val = _guard_as_float(extra.get(key))
            if val is not None:
                return float(val)
    return 0.0


def _repair_issue_qty_parked_in_sales_value(item: Dict[str, Any]) -> bool:
    """Move Issue Qty out of sales_value when sales_qty is blank.

    Qty-only Op.Bal / Issue / Closing sheets often park the Issue digits in
    sales_value (e.g. LIV.52 Issue 2551 → sales_value=2551, sales_qty=null).
    Never invent digits — only re-slot a printed whole number that balances.
    """
    if not isinstance(item, dict):
        return False
    sq = _guard_as_float(item.get("sales_qty"))
    sv = _guard_as_float(item.get("sales_value"))
    if sv is None or abs(sv) < 0.001:
        return False
    if sq is not None and abs(sq) > 0.001:
        return False
    if not _guard_whole_qty(sv):
        return False
    # Decimal rupees stay in sales_value.
    if abs(sv - round(sv)) > 0.001:
        return False
    opening = _guard_as_float(item.get("opening_qty")) or 0.0
    closing = _guard_as_float(item.get("closing_qty"))
    if closing is None:
        return False
    receipt = _item_receipt_qty(item)
    candidate = float(round(sv))
    if not _row_qty_balances(opening, receipt, candidate, float(closing)):
        return False
    # Prefer Issue-as-qty over treating a large whole as money when identity
    # holds; money columns on these sheets are usually decimal or absent.
    item["sales_qty"] = candidate
    item["sales_value"] = None
    extra = item.setdefault("extra", {})
    if not isinstance(extra, dict):
        item["extra"] = {}
        extra = item["extra"]
    fs = extra.setdefault("field_source", {})
    if isinstance(fs, dict):
        fs["sales_qty"] = "printed"
        fs["sales_value"] = "missing"
    flags = list(extra.get("flags") or [])
    if "ISSUE_QTY_PARKED_IN_SALES_VALUE" not in flags:
        flags.append("ISSUE_QTY_PARKED_IN_SALES_VALUE")
    extra["flags"] = flags
    logger.info(
        "STOCK_ISSUE_QTY_PARKED_IN_SALES_VALUE product=%s sales_qty=%s",
        str(item.get("product_name") or "")[:60],
        candidate,
    )
    return True


def issue_closing_rate_prefers_swap(item: Dict[str, Any]) -> bool:
    """True when printed money rates disagree with current Issue/Closing slots.

    Stock identity holds either way after a pure Issue↔Closing swap, so qty
    formulas alone cannot detect the error. Sale/closing values (or opening
    unit rate vs closing value) break the tie using printed money only — never
    invents amounts.
    """
    if not isinstance(item, dict):
        return False
    sales = _guard_as_float(item.get("sales_qty"))
    closing = _guard_as_float(item.get("closing_qty"))
    if sales is None or closing is None:
        return False
    if sales <= 0 or closing <= 0 or abs(sales - closing) <= 0.05:
        return False
    opening = _guard_as_float(item.get("opening_qty")) or 0.0
    receipt = _item_receipt_qty(item)
    if not _row_qty_balances(opening, receipt, sales, closing):
        return False
    # Swapped assignment also balances — identity-ambiguous.
    if not _row_qty_balances(opening, receipt, closing, sales):
        return False

    sales_value = _guard_as_float(item.get("sales_value"))
    closing_value = _guard_as_float(item.get("closing_value"))
    opening_value = _guard_as_float(item.get("opening_value"))
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if opening_value is None and isinstance(extra, dict):
        opening_value = _guard_as_float(extra.get("opening_value"))

    if (
        sales_value is not None
        and closing_value is not None
        and sales_value > 0
        and closing_value > 0
    ):
        keep = abs((sales_value / sales) - (closing_value / closing))
        swapped = abs((sales_value / closing) - (closing_value / sales))
        rate_a = sales_value / closing
        rate_b = closing_value / sales
        if min(rate_a, rate_b) > 0 and max(rate_a, rate_b) / min(rate_a, rate_b) <= 1.35:
            if keep >= 80 and swapped * 4 < keep and swapped <= 80:
                return True
    elif (
        opening is not None
        and opening > 0.05
        and opening_value is not None
        and opening_value > 0.05
        and closing_value is not None
        and closing_value > 0.05
    ):
        rate = opening_value / opening
        if rate > 0.01:
            keep_err = abs(closing_value - closing * rate)
            swap_err = abs(closing_value - sales * rate)
            # Closing value matches the other qty far better → columns swapped.
            if swap_err * 4 < keep_err and swap_err <= max(1.0, 0.08 * closing_value):
                return True
    return False


def _repair_issue_closing_swap_by_rate(item: Dict[str, Any]) -> bool:
    """Swap Issue↔Closing when stock identity holds both ways but rates disagree.

    Opening+receipt−issue=closing is true after a pure swap, so formula checks
    alone accept the wrong column assignment. Sale/closing values (or opening
    unit rate vs closing value) break the tie using printed money only.
    """
    if not issue_closing_rate_prefers_swap(item):
        return False

    sales = _guard_as_float(item.get("sales_qty"))
    closing = _guard_as_float(item.get("closing_qty"))
    sales_value = _guard_as_float(item.get("sales_value"))
    closing_value = _guard_as_float(item.get("closing_value"))
    if sales is None or closing is None:
        return False

    item["sales_qty"] = float(closing)
    item["closing_qty"] = float(sales)
    if sales_value is not None and closing_value is not None:
        item["sales_value"] = float(closing_value)
        item["closing_value"] = float(sales_value)
    extra = item.setdefault("extra", {})
    if not isinstance(extra, dict):
        item["extra"] = {}
        extra = item["extra"]
    fs = extra.setdefault("field_source", {})
    if isinstance(fs, dict):
        fs["sales_qty"] = "issue_closing_rate_swap"
        fs["closing_qty"] = "issue_closing_rate_swap"
    flags = list(extra.get("flags") or [])
    if "ISSUE_CLOSING_RATE_SWAP" not in flags:
        flags.append("ISSUE_CLOSING_RATE_SWAP")
    extra["flags"] = flags
    logger.info(
        "STOCK_ISSUE_CLOSING_RATE_SWAP product=%s sales=%s closing=%s",
        str(item.get("product_name") or "")[:60],
        item["sales_qty"],
        item["closing_qty"],
    )
    return True


def _repair_sales_qty_value_pair(item: Dict[str, Any]) -> bool:
    """Fix one line: unswap money↔qty, or clear qty copied into sales_value."""
    if not isinstance(item, dict):
        return False
    if _repair_issue_qty_parked_in_sales_value(item):
        return True
    if _repair_issue_closing_swap_by_rate(item):
        return True
    sq = _guard_as_float(item.get("sales_qty"))
    sv = _guard_as_float(item.get("sales_value"))
    extra = item.setdefault("extra", {})
    if not isinstance(extra, dict):
        item["extra"] = {}
        extra = item["extra"]
    fs = extra.setdefault("field_source", {})
    if not isinstance(fs, dict):
        extra["field_source"] = {}
        fs = extra["field_source"]
    changed = False
    sv_blank = sv is None or abs(sv) < 0.001

    # Case A: money parked in sales_qty, non-zero whole qty parked in sales_value.
    if (
        sq is not None
        and sv is not None
        and not sv_blank
        and _guard_money_like(sq)
        and _guard_qty_like(sv)
        and not _guard_money_like(sv)
    ):
        item["sales_qty"] = float(sv)
        item["sales_value"] = float(sq)
        fs["sales_qty"] = "printed"
        fs["sales_value"] = "printed"
        changed = True
        logger.info(
            "STOCK_UNIVERSAL_SALES_QTY_VALUE_GUARD action=unswap product=%s "
            "sales_qty=%s sales_value=%s",
            str(item.get("product_name") or "")[:60],
            item["sales_qty"],
            item["sales_value"],
        )
    else:
        # Case B: sales_value is an exact copy of sales_qty (small whole) → clear.
        if (
            sq is not None
            and sv is not None
            and abs(sq - sv) < 0.001
            and abs(sq) > 0.001
            and _guard_qty_like(sv)
            and not _guard_money_like(sv)
        ):
            item["sales_value"] = None
            fs["sales_value"] = "missing"
            changed = True
            logger.info(
                "STOCK_UNIVERSAL_SALES_QTY_VALUE_GUARD action=clear_qty_copy "
                "product=%s qty=%s",
                str(item.get("product_name") or "")[:60],
                sq,
            )
        # Case C: money in sales_qty while sales_value blank/zero → move money.
        # Do not invent sales_qty (leave missing). Never treat 0 as the qty.
        # Skip when Free/scheme columns exist — decimal Sale qtys (30.26) on
        # Item|Pack|Free|Return grids are quantities, not parked rupees.
        # CRITICAL: never move a whole number that already balances as Issue Qty
        # (Op+Rec−Issue=Closing). Large Issue counts (≥1000) look "money-like"
        # but are quantities on qty-only Op.Bal / Issue / Closing sheets.
        elif sq is not None and _guard_money_like(sq) and sv_blank:
            has_free_cols = any(
                (item.get(k) not in (None, "") or extra.get(k) not in (None, ""))
                for k in (
                    "sale_free_qty",
                    "purchase_free_qty",
                    "sales_free_qty",
                    "free_out_qty",
                    "free_in_qty",
                )
            )
            opening = _guard_as_float(item.get("opening_qty")) or 0.0
            closing = _guard_as_float(item.get("closing_qty"))
            receipt = _item_receipt_qty(item)
            balances_as_issue = (
                closing is not None
                and _guard_whole_qty(sq)
                and _row_qty_balances(opening, receipt, float(sq), float(closing))
            )
            if has_free_cols and abs(sq - round(sq)) > 0.001 and abs(sq) < 500.0:
                logger.info(
                    "STOCK_UNIVERSAL_SALES_QTY_VALUE_GUARD action=skip_decimal_qty "
                    "product=%s sales_qty=%s",
                    str(item.get("product_name") or "")[:60],
                    sq,
                )
            elif balances_as_issue:
                logger.info(
                    "STOCK_UNIVERSAL_SALES_QTY_VALUE_GUARD "
                    "action=skip_money_from_qty_identity product=%s sales_qty=%s",
                    str(item.get("product_name") or "")[:60],
                    sq,
                )
            else:
                item["sales_value"] = float(sq)
                item["sales_qty"] = None
                fs["sales_value"] = "printed"
                fs["sales_qty"] = "missing"
                changed = True
                logger.info(
                    "STOCK_UNIVERSAL_SALES_QTY_VALUE_GUARD action=money_from_qty "
                    "product=%s sales_value=%s",
                    str(item.get("product_name") or "")[:60],
                    item["sales_value"],
                )

    if changed:
        flags = list(extra.get("flags") or [])
        if "UNIVERSAL_SALES_QTY_VALUE_GUARD" not in flags:
            flags.append("UNIVERSAL_SALES_QTY_VALUE_GUARD")
        extra["flags"] = flags
    return changed


def _repair_totals_sales_value_qty_sum(result: Dict[str, Any]) -> bool:
    """When totals.sales_value equals line sales_qty sum, prefer printed money.

    Prefers (in order): sum of line sales_value that look like money, then
    extra.printed_* / footer money keys. Never invents from rate×qty.
    """
    totals = result.get("totals")
    if not isinstance(totals, dict):
        return False
    extra = totals.setdefault("extra", {})
    if not isinstance(extra, dict):
        totals["extra"] = {}
        extra = totals["extra"]
    # Qty-only / LMS layouts keep footer money via finalize_lms — do not remap.
    if extra.get("qty_only") or extra.get("no_sales_value"):
        return False
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    if not items:
        return False
    qty_sum = 0.0
    money_sum = 0.0
    money_rows = 0
    for it in items:
        sq = _guard_as_float(it.get("sales_qty"))
        sv = _guard_as_float(it.get("sales_value"))
        if sq is not None:
            qty_sum += sq
        if sv is not None and _guard_money_like(sv):
            money_sum += sv
            money_rows += 1
    totals_sv = _guard_as_float(totals.get("sales_value"))
    if totals_sv is None or qty_sum <= 0:
        return False
    # Already money-shaped and not equal to qty sum — leave alone.
    if _guard_money_like(totals_sv) and abs(totals_sv - qty_sum) > 0.51:
        return False
    if abs(totals_sv - qty_sum) > 0.51:
        return False
    # totals.sales_value collapsed to qty sum. Prefer printed line money sum.
    candidate: Optional[float] = None
    if money_rows > 0 and money_sum > 0 and abs(money_sum - qty_sum) > 0.51:
        candidate = round(money_sum, 2)
    if candidate is None:
        for key in (
            "printed_amount_total_sales_value",
            "printed_sales_value",
            "footer_sales_value",
            "sales_issue_value",
        ):
            raw = extra.get(key)
            num = _guard_as_float(raw)
            if num is not None and _guard_money_like(num) and abs(num - qty_sum) > 0.51:
                candidate = float(num)
                break
    if candidate is None:
        return False
    totals["sales_value"] = float(candidate)
    extra["sales_value_source"] = "universal_sales_qty_value_guard"
    logger.info(
        "STOCK_UNIVERSAL_SALES_QTY_VALUE_GUARD action=totals_from_money "
        "was_qty_sum=%s sales_value=%s",
        qty_sum,
        candidate,
    )
    return True


def result_looks_like_opbal_qty_only(result: Dict[str, Any]) -> bool:
    """True when line items are Op.Bal/Issue/Closing quantities with no money column.

    Used to stamp qty_only on generic Gemini extracts so Issue Qty is never
    migrated into sales_value, and so parked-value / rate-swap repairs run.
    """
    if not isinstance(result, dict):
        return False
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    layout = str(extra.get("layout") or "")
    method = str(extra.get("extraction_method") or "")
    if layout in {"opbal_issue_closing", "opening_receive_issue_closing"}:
        return True
    if "opbal" in method or method.startswith("swilerp"):
        return True
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    if len(items) < 4:
        return False
    money_rows = 0
    qty_rows = 0
    balanced = 0
    for item in items:
        sq = _guard_as_float(item.get("sales_qty"))
        sv = _guard_as_float(item.get("sales_value"))
        cv = _guard_as_float(item.get("closing_value"))
        oq = _guard_as_float(item.get("opening_qty")) or 0.0
        rq = _item_receipt_qty(item)
        cq = _guard_as_float(item.get("closing_qty"))
        if (sv is not None and abs(sv) > 0.01 and not (
            sq is not None and abs(sv - sq) < 0.001
        )) or (cv is not None and abs(cv) > 0.01):
            # Treat copied qty-as-value as non-money for this probe.
            if sv is not None and abs(sv) > 0.01 and not (
                sq is not None and abs(sv - sq) < 0.001
            ):
                if _guard_money_like(sv) and not (
                    cq is not None
                    and _guard_whole_qty(sv)
                    and _row_qty_balances(oq, rq, float(sv), float(cq))
                ):
                    money_rows += 1
            elif cv is not None and abs(cv) > 0.01 and _guard_money_like(cv):
                money_rows += 1
        if any(
            abs(v) > 0.01
            for v in (oq, rq, sq or 0.0, cq or 0.0)
            if v is not None
        ):
            qty_rows += 1
        if (
            sq is not None
            and cq is not None
            and _row_qty_balances(oq, rq, float(sq), float(cq))
        ):
            balanced += 1
        elif (
            sq is None
            and sv is not None
            and cq is not None
            and _guard_whole_qty(sv)
            and _row_qty_balances(oq, rq, float(sv), float(cq))
        ):
            # Issue parked in sales_value — still qty-only layout.
            balanced += 1
            qty_rows += 1
    if qty_rows < 3:
        return False
    if money_rows > max(1, int(0.2 * len(items))):
        return False
    return balanced >= max(3, int(0.5 * len(items)))


def stamp_opbal_qty_only_if_detected(result: Dict[str, Any]) -> bool:
    """Stamp qty_only/layout when Op.Bal Issue Closing geometry is detected."""
    if not isinstance(result, dict):
        return False
    if not result_looks_like_opbal_qty_only(result):
        return False
    totals = result.setdefault("totals", {})
    if not isinstance(totals, dict):
        return False
    extra = totals.setdefault("extra", {})
    if not isinstance(extra, dict):
        return False
    changed = False
    if not extra.get("qty_only"):
        extra["qty_only"] = True
        changed = True
    if not extra.get("no_sales_value"):
        extra["no_sales_value"] = True
        changed = True
    if not extra.get("layout"):
        extra["layout"] = "opbal_issue_closing"
        changed = True
    return changed


def _repair_missing_issue_from_printed_closing(item: Dict[str, Any]) -> bool:
    """Fill Issue Qty when it was dropped but Opening/Closing are printed.

    On Op.Bal / Issue / Closing sheets, Vision sometimes zeroes Issue while
    Closing remains correct (e.g. Opening=1110, Issue blank, Closing=688 →
    Issue=422). Only fills when the derived Issue is a positive whole number
    that restores stock identity — never fabricates Closing or Opening.
    """
    if not isinstance(item, dict):
        return False
    sq = _guard_as_float(item.get("sales_qty"))
    if sq is not None and abs(sq) > 0.001:
        return False
    # Do not steal Issue that is already parked in sales_value — that path
    # has its own repair.
    sv = _guard_as_float(item.get("sales_value"))
    opening = _guard_as_float(item.get("opening_qty"))
    closing = _guard_as_float(item.get("closing_qty"))
    if opening is None or closing is None:
        return False
    receipt = _item_receipt_qty(item)
    if sv is not None and abs(sv) > 0.001 and _guard_whole_qty(sv):
        if _row_qty_balances(float(opening), receipt, float(sv), float(closing)):
            return False
    candidate = float(opening) + receipt - float(closing)
    if candidate <= 0.51 or not _guard_whole_qty(candidate):
        return False
    if not _row_qty_balances(float(opening), receipt, candidate, float(closing)):
        return False
    # Prefer when printed Total also matches Opening+Receipt.
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    total = None
    if isinstance(extra, dict):
        total = _guard_as_float(extra.get("total_stock_qty"))
        if total is None:
            total = _guard_as_float(extra.get("total_stock"))
    if total is not None and abs(total - (float(opening) + receipt)) > 0.51:
        return False
    item["sales_qty"] = float(round(candidate))
    extra = item.setdefault("extra", {})
    if not isinstance(extra, dict):
        item["extra"] = {}
        extra = item["extra"]
    fs = extra.setdefault("field_source", {})
    if isinstance(fs, dict):
        fs["sales_qty"] = "derived_from_closing_identity"
    flags = list(extra.get("flags") or [])
    if "MISSING_ISSUE_FROM_CLOSING_IDENTITY" not in flags:
        flags.append("MISSING_ISSUE_FROM_CLOSING_IDENTITY")
    extra["flags"] = flags
    logger.info(
        "STOCK_MISSING_ISSUE_FROM_CLOSING product=%s sales_qty=%s",
        str(item.get("product_name") or "")[:60],
        item["sales_qty"],
    )
    return True


def repair_opbal_qty_only_fields(result: Dict[str, Any]) -> Dict[str, Any]:
    """Post-extract Op.Bal repairs that must survive structured selection.

    1) Stamp qty_only when the sheet has no real money column.
    2) Move Issue Qty out of sales_value.
    3) Swap Issue↔Closing when rates disagree.
    4) Clear residual qty copies from sales_value.
    5) Derive missing Issue from Opening/Closing identity when Issue was dropped.
    Never invents digits or fabricates money.
    """
    if not isinstance(result, dict):
        return result
    stamp_opbal_qty_only_if_detected(result)
    result = apply_universal_sales_qty_value_guard(result)
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    qty_only = bool(extra.get("qty_only") or extra.get("no_sales_value"))
    if qty_only or result_looks_like_opbal_qty_only(result):
        for item in result.get("line_items") or []:
            if isinstance(item, dict):
                _repair_missing_issue_from_printed_closing(item)
    return result


def apply_universal_sales_qty_value_guard(result: Dict[str, Any]) -> Dict[str, Any]:
    """Stockist-agnostic sales_qty / sales_value repair for PDF + Excel + image.

    Layout-family fallback (not stockist-named): when Sale Qty lands in
    sales_value or money lands in sales_qty, re-slot the printed numbers.
    Does not invent sales_value from rate × qty. Default ON.
    """
    if not _universal_sales_qty_value_guard_enabled():
        return result
    if not isinstance(result, dict):
        return result
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    # Auto-detect qty-only Op.Bal sheets (generic Gemini often omits the flag).
    if not bool(extra.get("qty_only") or extra.get("no_sales_value")):
        stamp_opbal_qty_only_if_detected(result)
        totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
        extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    # Qty-only sheets have no per-line Sale Value column — only clear copies.
    qty_only = bool(extra.get("qty_only") or extra.get("no_sales_value"))
    fixed = 0
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        if qty_only:
            # Qty-only: re-slot Issue parked in sales_value; strip qty copies;
            # never invent money from rate×qty.
            row_fixed = False
            if _repair_issue_qty_parked_in_sales_value(item):
                row_fixed = True
            if _repair_issue_closing_swap_by_rate(item):
                row_fixed = True
            sq = _guard_as_float(item.get("sales_qty"))
            sv = _guard_as_float(item.get("sales_value"))
            if (
                sq is not None
                and sv is not None
                and abs(sq - sv) < 0.001
                and abs(sq) > 0.001
                and _guard_qty_like(sv)
            ):
                item["sales_value"] = None
                item_extra = item.setdefault("extra", {})
                if isinstance(item_extra, dict):
                    fs = item_extra.setdefault("field_source", {})
                    if isinstance(fs, dict):
                        fs["sales_value"] = "missing"
                    flags = list(item_extra.get("flags") or [])
                    if "UNIVERSAL_SALES_QTY_VALUE_GUARD" not in flags:
                        flags.append("UNIVERSAL_SALES_QTY_VALUE_GUARD")
                    item_extra["flags"] = flags
                row_fixed = True
            if row_fixed:
                fixed += 1
            continue
        if _repair_sales_qty_value_pair(item):
            fixed += 1
    totals_fixed = _repair_totals_sales_value_qty_sum(result)
    if fixed or totals_fixed:
        logger.info(
            "STOCK_UNIVERSAL_SALES_QTY_VALUE_GUARD lines_fixed=%s totals_fixed=%s",
            fixed,
            int(bool(totals_fixed)),
        )
    return result


def _apply_opening_received_issued_header_summary(
    result: Dict[str, Any], peek: str
) -> bool:
    """Peel Opening Value / Purchase / Sales Value On prate / Closing Value.

    Layout-family fix for Opening|Received|Issued|Closing grids (and the same
    columns in Excel). Never invents amounts; only copies printed labels.
    """
    if not _opening_received_issued_header_summary_enabled():
        return False
    if not isinstance(result, dict):
        return False
    blob = str(peek or "")
    # OPENING/PURCHASE/LMS/NEAREXP StockHDZ pages are qty + footer money only —
    # do not treat them as Opening Value / Received labeled money summaries.
    if _is_stacked_lms_nearexp_layout(blob):
        return False
    if not blob.strip() or not _OPENING_RECEIVED_ISSUED_HEADER_RE.search(blob):
        return False
    from services.stock_header_resolver import parse_number

    found: Dict[str, float] = {}
    for match in _OPENING_RECEIVED_ISSUED_PAIR_RE.finditer(blob):
        label = re.sub(r"\s+", " ", match.group("label")).strip().lower()
        amount = parse_number(match.group("amount"))
        if amount is None:
            continue
        if label == "opening value":
            found["opening_value"] = float(amount)
        elif label == "closing value":
            found["closing_value"] = float(amount)
        elif label == "sales value on prate":
            found["sales_value"] = float(amount)
        elif label in {"purchase", "purchase value"}:
            found["receipts_value"] = float(amount)
            found["purchase_value"] = float(amount)
        elif label in {"sale", "sales"}:
            # Cost/issue band — keep only when Sales Value On prate is absent.
            found.setdefault("sales_issue_value", float(amount))
    if not found:
        return False
    totals = result.setdefault("totals", {})
    if not isinstance(totals, dict):
        totals = {}
        result["totals"] = totals
    extra = totals.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        totals["extra"] = extra
    changed = False
    if found.get("opening_value") is not None:
        totals["opening_value"] = found["opening_value"]
        extra["opening_value"] = found["opening_value"]
        changed = True
    if found.get("closing_value") is not None:
        totals["closing_value"] = found["closing_value"]
        changed = True
    if found.get("sales_value") is not None:
        totals["sales_value"] = found["sales_value"]
        changed = True
    elif found.get("sales_issue_value") is not None and totals.get(
        "sales_value"
    ) in (None, ""):
        totals["sales_value"] = found["sales_issue_value"]
        changed = True
    for key in ("receipts_value", "purchase_value", "sales_issue_value"):
        if found.get(key) is not None:
            extra[key] = found[key]
            if key == "receipts_value":
                totals["receipts_value"] = found[key]
            changed = True
    if changed:
        extra["layout"] = extra.get("layout") or "opening_received_issued_closing"
        extra["psr_column_layout"] = (
            extra.get("psr_column_layout") or "opening_received_issued_closing"
        )
        if not extra.get("total_row_source"):
            extra["total_row_source"] = "opening_received_issued_header"
        logger.info(
            "STOCK_PDF_OPENING_RECEIVED_ISSUED_SUMMARY "
            "opening_value=%s receipts_value=%s sales_value=%s closing_value=%s",
            found.get("opening_value"),
            found.get("receipts_value"),
            totals.get("sales_value"),
            found.get("closing_value"),
        )
    return changed


def _drop_consecutive_duplicate_zero_rows(
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Drop consecutive identical all-zero SKU rows (printed pack/name twins)."""
    if not items:
        return items
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not out:
            out.append(item)
            continue
        prev = out[-1]
        name_a = _norm_name(item.get("product_name"))
        name_b = _norm_name(prev.get("product_name"))
        pack_a = _norm_name(item.get("packing"))
        pack_b = _norm_name(prev.get("packing"))
        qty_keys = (
            "opening_qty",
            "receipts_qty",
            "sales_qty",
            "closing_qty",
        )

        def _zeroish(row: Dict[str, Any]) -> bool:
            vals = [row.get(k) for k in qty_keys]
            if any(v not in (None, "", 0, 0.0) for v in vals):
                return False
            return True

        same = name_a and name_a == name_b and pack_a == pack_b
        if same and _zeroish(item) and _zeroish(prev):
            logger.info(
                "STOCK_PDF_OPENING_RECEIVED_ISSUED_DEDUP name=%s pack=%s",
                item.get("product_name"),
                item.get("packing"),
            )
            continue
        out.append(item)
    return out


def _apply_op_qty_pur_sale_bal_meta(result: Dict[str, Any], peek: str) -> bool:
    """Fingerprint Op.Qty|Pur.Qty|Sale.Qty|Bal and fill title/company/stockist."""
    if not _op_qty_pur_sale_bal_meta_enabled() or not isinstance(result, dict):
        return False
    blob = str(peek or "")
    if not blob.strip():
        return False
    try:
        from services.stock_direct_vision import _has_op_qty_pur_sale_bal_headers
    except Exception:
        return False
    if not _has_op_qty_pur_sale_bal_headers(blob):
        return False

    changed = False
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        result.setdefault("totals", {})["extra"] = extra
    if extra.get("layout") != "op_qty_pur_sale_bal":
        extra["layout"] = "op_qty_pur_sale_bal"
        changed = True
    extra["psr_column_layout"] = "op_qty_pur_sale_bal"
    extra["schema"] = "op_qty_pur_sale_bal"
    extra["qty_only"] = True

    if re.search(r"Mfac\s+SAS\s+Summary", blob, re.I):
        result["report_title"] = "Mfac SAS Summary Report"
        changed = True
    elif re.search(r"Stock\s+and\s+Sale\s+Report", blob, re.I) and not str(
        result.get("report_title") or ""
    ).strip():
        result["report_title"] = "Stock and Sale Report"
        changed = True

    # Drop column-header chrome parked as stockist_name.
    if _stockist_looks_like_qty_headers(result.get("stockist_name")):
        result["stockist_name"] = None
        changed = True

    seller = re.search(
        r"Seller\s*:\s*([A-Za-z][A-Za-z0-9 .&'/-]*?)"
        r"(?=\s+OLD\s+NO|\s+NEW\s+NO|\s+\d{2,}|\s*,|\n|$)",
        blob,
        re.I,
    )
    if seller and seller.group(1).strip():
        stockist = re.sub(r"\s+", " ", seller.group(1)).strip(" -")
        if stockist:
            result["stockist_name"] = stockist
            changed = True
    elif re.search(
        r"Mfac\s+SAS\s+Summary", str(result.get("stockist_name") or ""), re.I
    ):
        result["stockist_name"] = None
        changed = True
    elif not str(result.get("stockist_name") or "").strip():
        # Letterhead above Stock and Sale Report (address may sit between).
        for line in blob.splitlines():
            stripped = re.sub(r"\s+", " ", line.strip())
            if not stripped or _LETTERHEAD_TABLE_RE.match(stripped):
                continue
            if _LETTERHEAD_TITLE_RE.search(stripped):
                continue
            if _LETTERHEAD_ADDRESS_RE.search(stripped):
                continue
            if re.search(r"\d{2,}", stripped) and not _letterhead_digits_are_fy_only(
                stripped
            ):
                continue
            if not re.search(
                r"(?i)\b(?:distribut|agenc(?:y|ies)|medico|stores|pharma)\b",
                stripped,
            ):
                continue
            stockist = _LETTERHEAD_FY_RE.sub("", stripped).strip(" -")
            stockist = re.sub(r"\s+", " ", stockist).strip()
            if stockist and not _stockist_looks_like_qty_headers(stockist):
                result["stockist_name"] = stockist
                changed = True
                break

    mfr = re.search(
        r"Manufacturer\s*(?:Name\s*)?:\s*"
        r"([A-Za-z][A-Za-z0-9 .&'/-]*?)"
        r"(?=\s+\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\s{2,}|\n|$)",
        blob,
        re.I,
    )
    if mfr and mfr.group(1).strip():
        company = re.sub(r"\s+", " ", mfr.group(1)).strip(" -")
        stockist_now = str(result.get("stockist_name") or "").strip()
        if company and (
            not stockist_now or company.upper() != stockist_now.upper()
        ):
            result["company_name"] = company
            changed = True
    elif re.search(
        r"Mfac\s+SAS\s+Summary", str(result.get("company_name") or ""), re.I
    ):
        result["company_name"] = None
        changed = True
    elif not str(result.get("company_name") or "").strip():
        m_zeal = re.search(
            r"\b(HIMALAYA\s*\([^)]{2,40}\))(?:\s*\[[^\]]+\])?",
            blob,
            re.I,
        )
        if m_zeal:
            company = re.sub(r"\s+", " ", m_zeal.group(1)).strip()
            stockist_now = str(result.get("stockist_name") or "").strip()
            if company and (
                not stockist_now or company.upper() != stockist_now.upper()
            ):
                result["company_name"] = company
                changed = True

    # From Date 01-10-26 to 31-10-26 (qty-grid letterhead).
    if not str(result.get("period_from") or "").strip():
        from_date = re.search(
            r"From\s+Date\s+(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\s+"
            r"to\s+(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})",
            blob,
            re.I,
        )
        if from_date:
            def _iso_from_date(day: str, month: str, year: str) -> str:
                yr = year if len(year) == 4 else ("20" + year)
                return f"{yr}-{int(month):02d}-{int(day):02d}"

            result["period_from"] = _iso_from_date(
                from_date.group(1), from_date.group(2), from_date.group(3)
            )
            result["period_to"] = _iso_from_date(
                from_date.group(4), from_date.group(5), from_date.group(6)
            )
            changed = True

    # Printed "Report Date :02-11-26 17:14:12" — metadata only, never a line item.
    if not str(extra.get("report_date") or "").strip():
        report_date = re.search(
            r"Report\s*Date\s*:?\s*"
            r"(\d{1,2}[-./]\d{1,2}[-./]\d{2,4}"
            r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)",
            blob,
            re.I,
        )
        if report_date:
            extra["report_date"] = re.sub(
                r"\s+", " ", report_date.group(1)
            ).strip()
            changed = True

    # Manufacturer totals band under headers: Op / Pur / Sale / Bal money.
    # e.g. "Manufacturer : HIMALAYA - ZANDRA 3,89,850.04 2,46,223.81 …"
    band = re.search(
        r"Manufacturer\s*(?:Name\s*)?:[^\n]*?"
        r"(\d{1,3}(?:,\d{2,3})+\.\d{2})\s+"
        r"(\d{1,3}(?:,\d{2,3})+\.\d{2})\s+"
        r"(\d{1,3}(?:,\d{2,3})+\.\d{2})\s+"
        r"(\d{1,3}(?:,\d{2,3})+\.\d{2})",
        blob,
        re.I,
    )
    if band:
        def _inr(raw: str) -> float:
            return float(str(raw).replace(",", ""))

        opening_v = _inr(band.group(1))
        purchase_v = _inr(band.group(2))
        sales_v = _inr(band.group(3))
        closing_v = _inr(band.group(4))
        totals = result.setdefault("totals", {})
        if not isinstance(totals, dict):
            totals = {}
            result["totals"] = totals
        if totals.get("sales_value") in (None, "", 0, 0.0):
            totals["sales_value"] = sales_v
            changed = True
        if totals.get("closing_value") in (None, "", 0, 0.0):
            totals["closing_value"] = closing_v
            changed = True
        if extra.get("opening_value") in (None, "", 0, 0.0):
            extra["opening_value"] = opening_v
            changed = True
        if extra.get("purchase_value") in (None, "", 0, 0.0):
            extra["purchase_value"] = purchase_v
            changed = True
        extra["manufacturer_band_totals"] = True
        extra["total_row_source"] = "op_qty_pur_sale_bal_manufacturer_band"
        logger.info(
            "STOCK_PDF_WORDS_OP_QTY_PUR_SALE_BAL_BAND_TOTALS "
            "opening_value=%s purchase_value=%s sales_value=%s closing_value=%s",
            opening_v,
            purchase_v,
            sales_v,
            closing_v,
        )

    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        item_extra = item.setdefault("extra", {})
        if not isinstance(item_extra, dict):
            item_extra = {}
            item["extra"] = item_extra
        item_extra["layout"] = "op_qty_pur_sale_bal"
        item_extra["psr_column_layout"] = "op_qty_pur_sale_bal"
        item_extra["schema"] = "op_qty_pur_sale_bal"

    if changed:
        logger.info(
            "STOCK_PDF_WORDS_OP_QTY_PUR_SALE_BAL_META "
            "title=%r company=%r stockist=%r items=%s",
            result.get("report_title"),
            result.get("company_name"),
            result.get("stockist_name"),
            len(result.get("line_items") or []),
        )
    return changed


_LETTERHEAD_TITLE_RE = re.compile(
    r"(?i)stock\s+and\s+sales|stock\s+and\s+sale\s+report|stock\s*&\s*sales|"
    r"sale\s+and\s+stock|stock\s+statement|stock\s+report|page\s*no\b|for\s+the\s+period|"
    r"report\s*date\b"
)
_LETTERHEAD_TABLE_RE = re.compile(
    r"(?i)^(?:sl\.?\s*no|sno|product(?:\s+name)?|item(?:\s+name)?|"
    r"particulars?|opening|op\.?\s*stock|op\.?\s*qty|pack(?:ing)?)\b"
)
_LETTERHEAD_FY_RE = re.compile(
    r"\(\s*\d{4}\s*[-–/]\s*\d{2,4}\s*\)"
)
_LETTERHEAD_ADDRESS_RE = re.compile(
    r"(?i)\bgstin\b|\bgst\s*:|\bpin(?:code)?\b|\bdist(?:rict)?\b|"
    r"\broad\b|\boad\b|\bstreet\b|\bnagar\b|\bplot\b|\bat/?po\b|"
    r"\bshop(?:p)?ing\b|\bcomplex\b|\bmarket\b|\bbaza?a?r\b|"
    r"\b\d{6}\b|\bphone\b|\bmobile\b|\be-?mail\b|\bstate\b|"
    r"\bkarnataka\b|\barnataka\b|\bmaharashtra\b|\btamil\s*nadu\b|"
    r"\bandhra\b|\btelangana\b|\bgujarat\b|\bdelhi\b|\bfssai\b|"
    r"\bbihar\b|\bbhagalpur\b"
)
_LETTERHEAD_DISTRIBUTOR_RE = re.compile(
    r"(?i)\b(?:agenc(?:y|ies)|distribut|medico|stores|"
    r"private\s+limited|pvt\.?\s*ltd)\b"
)


def _looks_like_division_banner(line: str) -> bool:
    s = str(line or "").strip()
    if not s or len(s) > 60:
        return False
    if _LETTERHEAD_TITLE_RE.search(s) or _LETTERHEAD_TABLE_RE.search(s):
        return False
    if _LETTERHEAD_ADDRESS_RE.search(s) or _LETTERHEAD_DISTRIBUTOR_RE.search(s):
        return False
    if re.search(r"\d{2,}", s):
        return False
    words = re.findall(r"[A-Za-z]{2,}", s)
    return 1 <= len(words) <= 6


def _apply_pdf_words_letterhead_meta(result: Dict[str, Any], peek: str) -> bool:
    """Fill company from the printed division banner; address from GSTIN/city lines.

    Does not invent Puttur/GSTIN when those tokens are not on the page.
    """
    if not _pdf_words_letterhead_meta_enabled() or not isinstance(result, dict):
        return False
    lines = [ln.strip() for ln in str(peek or "").splitlines() if ln.strip()]
    if not lines:
        return False
    table_i = next(
        (i for i, ln in enumerate(lines) if _LETTERHEAD_TABLE_RE.match(ln)),
        min(12, len(lines)),
    )
    header_lines = lines[:table_i]
    changed = False
    stockist = str(result.get("stockist_name") or "").strip()
    company = str(result.get("company_name") or "").strip()
    if company and stockist and company.upper() == stockist.upper():
        result["company_name"] = None
        company = ""
        changed = True
    if not company:
        # Manufacturer Name : HIMALAYA / Division Name:-HIMALAYA(ZEAL)
        for ln in header_lines:
            m_div = re.search(
                r"Division\s*Name\s*[:-]+\s*(.+)$", ln, re.I
            )
            m_mfr = re.search(
                r"Manufacturer\s*Name\s*:\s*(.+?)(?:\s+Division\b|$)", ln, re.I
            )
            picked = None
            if m_div and m_div.group(1).strip():
                picked = re.sub(r"\s+", " ", m_div.group(1)).strip(" -")
            elif m_mfr and m_mfr.group(1).strip():
                picked = re.sub(r"\s+", " ", m_mfr.group(1)).strip(" -")
            if picked and (
                not stockist or picked.upper() != stockist.upper()
            ):
                result["company_name"] = picked
                logger.info(
                    "STOCK_PDF_WORDS_LETTERHEAD_META manufacturer_company=%r",
                    result["company_name"],
                )
                changed = True
                company = picked
                break
    if not company:
        # Stock Report from … (HIMALAYA(ZANDRA)) — paren division, not stockist.
        for ln in header_lines:
            m_co = re.search(r"Stock\s+Report.*?\((.+)\)\s*$", ln, re.I)
            if not m_co:
                continue
            company_from_report = re.sub(r"\s+", " ", m_co.group(1)).strip(" -")
            if company_from_report and (
                not stockist or company_from_report.upper() != stockist.upper()
            ):
                result["company_name"] = company_from_report
                logger.info(
                    "STOCK_PDF_WORDS_LETTERHEAD_META stock_report_company=%r",
                    result["company_name"],
                )
                changed = True
                company = company_from_report
                break
    if not company:
        # "For HIMALAYA" / "For HIMALAYA ZEAL" under STOCK REPORT.
        for ln in header_lines:
            m_for = re.match(
                r"(?i)^For\s+(HIMALAYA(?:\s+[A-Za-z0-9()/\-]+)*)\s*$",
                ln.strip(),
            )
            if not m_for:
                continue
            picked = re.sub(r"\s+", " ", m_for.group(1)).strip(" -")
            if picked and (not stockist or picked.upper() != stockist.upper()):
                result["company_name"] = picked
                logger.info(
                    "STOCK_PDF_WORDS_LETTERHEAD_META for_company=%r",
                    result["company_name"],
                )
                changed = True
                company = picked
                break
    if not company:
        for ln in header_lines:
            if stockist and ln.upper() == stockist.upper():
                continue
            if _LETTERHEAD_TITLE_RE.search(ln):
                continue
            if _LETTERHEAD_ADDRESS_RE.search(ln):
                continue
            if _looks_like_division_banner(ln):
                result["company_name"] = re.sub(r"\s+", " ", ln)
                logger.info(
                    "STOCK_PDF_WORDS_LETTERHEAD_META company=%r",
                    result["company_name"],
                )
                changed = True
                break
    address = str(result.get("stockist_address") or "").strip()
    if address and _LETTERHEAD_TITLE_RE.search(address):
        result["stockist_address"] = None
        address = ""
        changed = True
    if not address:
        company_now = str(result.get("company_name") or "").strip()
        parts: List[str] = []
        for ln in header_lines:
            if stockist and ln.upper() == stockist.upper():
                continue
            if company_now and ln.upper() == company_now.upper():
                continue
            if re.match(r"(?i)^For\s+HIMALAYA\b", ln):
                continue
            if _LETTERHEAD_TITLE_RE.search(ln):
                continue
            if _LETTERHEAD_ADDRESS_RE.search(ln):
                parts.append(ln)
            elif (
                re.search(r"[A-Za-z]{3,}", ln)
                and not re.search(
                    r"(?i)\b(?:stock\s+report|particular|packing|op\.?\s*qnt)\b",
                    ln,
                )
                and not re.match(
                    r"(?i)^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\s+To\b",
                    ln,
                )
                and (not stockist or ln.upper() != stockist.upper())
            ):
                # Multi-line street/city under the firm name (ADITYA PLACE…).
                parts.append(ln)
        if parts:
            result["stockist_address"] = " ".join(parts)
            logger.info(
                "STOCK_PDF_WORDS_LETTERHEAD_META address=%r",
                result["stockist_address"],
            )
            changed = True
    return changed


def parse_pdf_free_column_statement(
    file_bytes: bytes, filename: str
) -> Optional[Dict[str, Any]]:
    """Text PDF whose words print Purchase Free and Sales Free as their own columns."""
    import io

    from services.sales_statement_extractor import empty_result
    from services.stock_native_tables import (
        from_fitz_pdf_bytes,
        from_pdf_words,
        pdf_built_has_clone_pages,
        pdf_built_has_mashed_headers,
        pdf_built_has_nul_digits,
        pdf_built_table_empty,
    )

    built: Optional[Dict[str, Any]] = None
    peek = ""
    pdf = None
    parsed: Optional[Dict[str, Any]] = None
    page_results: List[Dict[str, Any]] = []
    try:
        try:
            import pdfplumber

            pdf = pdfplumber.open(io.BytesIO(file_bytes))
            built = from_pdf_words(pdf)
            nul = pdf_built_has_nul_digits(built)
            mashed = pdf_built_has_mashed_headers(built)
            clone = pdf_built_has_clone_pages(built)
            empty = pdf_built_table_empty(built)
            if nul or mashed or clone or empty:
                logger.info(
                    "STOCK_PDF_PSEUDO_DIGITS plumber_nul=%s mashed=%s clone=%s "
                    "empty=%s fallback=fitz",
                    int(nul),
                    int(mashed),
                    int(clone),
                    int(empty),
                )
                built = from_fitz_pdf_bytes(file_bytes)
            peek = ""
            if pdf.pages:
                from services.stock_header_resolver import decode_pdf_pseudo_digits

                peek = decode_pdf_pseudo_digits(
                    "\n".join(page.extract_text() or "" for page in pdf.pages)
                )
        except Exception as plumber_exc:
            logger.info(
                "STOCK_PDF_WORDS_TABLE file=%s status=plumber_skip error=%s",
                filename,
                type(plumber_exc).__name__,
            )
            built = None
        if (
            built is None
            or pdf_built_has_nul_digits(built)
            or pdf_built_table_empty(built)
        ):
            if built is not None:
                logger.info(
                    "STOCK_PDF_PSEUDO_DIGITS plumber_nul=%s empty=%s fallback=fitz",
                    int(pdf_built_has_nul_digits(built)),
                    int(pdf_built_table_empty(built)),
                )
            built = from_fitz_pdf_bytes(file_bytes)
        if "\x00" in (peek or ""):
            try:
                import fitz

                _doc = fitz.open(stream=file_bytes, filetype="pdf")
                from services.stock_header_resolver import decode_pdf_pseudo_digits

                peek = decode_pdf_pseudo_digits(_doc[0].get_text("text") or peek)
                _doc.close()
            except Exception:
                pass
        if not peek:
            try:
                import fitz

                _doc = fitz.open(stream=file_bytes, filetype="pdf")
                peek = _doc[0].get_text("text") or ""
                _doc.close()
            except Exception:
                peek = ""
        page_results = built.get("page_results") or []
        # OPENING/PURCHASE/LMS/SALES grids must win over free-column and over
        # near-expiry BATCH/MRP detail tables that share the same page.
        parsed = items_from_lms_qty_rows(
            built.get("header_cells") or [],
            built.get("data_rows") or [],
        )
        if parsed and parsed.get("line_items"):
            logger.info(
                "STOCK_PDF_WORDS_LMS_QTY prefer=lms_movement items=%s",
                len(parsed["line_items"]),
            )
        # Stacked plain-text recovers rows word banding drops (e.g. HERBOLAX
        # TAB with pack "100" next to an all-dash twin). Prefer when fuller.
        if peek and _is_stacked_lms_nearexp_layout(peek):
            stacked_lms = items_from_stacked_lms_nearexp_text(peek)
            stacked_n = len((stacked_lms or {}).get("line_items") or [])
            word_n = len((parsed or {}).get("line_items") or [])
            if stacked_lms and stacked_n > word_n:
                parsed = stacked_lms
                logger.info(
                    "STOCK_PDF_STACKED_LMS_NEAREXP prefer=fuller_than_words "
                    "stacked=%s words=%s",
                    stacked_n,
                    word_n,
                )
        if not parsed or not parsed.get("line_items"):
            parsed = items_from_free_column_rows(
                built.get("header_cells") or [],
                built.get("data_rows") or [],
            )
        # Prefer x-aligned qty/value rows over the wrapped-name Receipt/Issue
        # y-band reader. That reader treats packing digits as opening_qty.
        if (
            (not parsed or not parsed.get("line_items"))
            and _qty_value_grid_skips_receipt_issue()
            and _page_rows_have_aligned_qty_value(page_results)
        ):
            parsed = _items_from_resolved_word_columns(page_results)
            if parsed and parsed.get("line_items"):
                logger.info(
                    "STOCK_PDF_WORDS_RESOLVED_COLUMNS prefer=aligned_qty_value"
                )
        if (not parsed or not parsed.get("line_items")) and pdf is not None:
            parsed = items_from_receipt_issue_pages(pdf, page_results)
        if (not parsed or not parsed.get("line_items")) and pdf is not None:
            parsed = items_from_open_rcvd_sold_pages(
                pdf, built.get("page_results") or []
            )
        if not parsed or not parsed.get("line_items"):
            parsed = _items_from_resolved_word_columns(
                built.get("page_results") or []
            )
        # Durable fallback for STOCK STATEMENT LMS/NEAREXP pages when word
        # banding still misses (stacked name/pack/qty lines in plain text).
        if (not parsed or not parsed.get("line_items")) and peek:
            stacked = items_from_stacked_lms_nearexp_text(peek)
            if stacked and stacked.get("line_items"):
                parsed = stacked
                logger.info(
                    "STOCK_PDF_STACKED_LMS_NEAREXP prefer=text_fallback items=%s",
                    len(parsed["line_items"]),
                )
        if (
            parsed
            and parsed.get("letterhead_is_stockist_only")
            and pdf is not None
            and pdf.pages
        ):
            from services.stock_header_resolver import decode_pdf_pseudo_digits

            peek = decode_pdf_pseudo_digits(
                pdf.pages[0].extract_text(x_tolerance=1, y_tolerance=3) or peek
            )
    except Exception as exc:
        logger.info(
            "STOCK_PDF_WORDS_TABLE file=%s status=skipped error=%s detail=%s",
            filename,
            type(exc).__name__,
            str(exc)[:240],
        )
        return None
    finally:
        if pdf is not None:
            try:
                pdf.close()
            except Exception:
                pass
    if not parsed or not parsed.get("line_items"):
        return None
    result = empty_result(filename, "pdf")
    result["line_items"] = _drop_consecutive_duplicate_zero_rows(
        list(parsed["line_items"] or [])
    )
    if re.search(r"Sale\s+And\s+Stock\s+Statement", peek, re.I):
        result["report_title"] = "Sale And Stock Statement"
    elif re.search(r"STOCK\s*&\s*SALES\s+ANALYSIS", peek, re.I):
        result["report_title"] = "STOCK & SALES ANALYSIS"
    elif re.search(r"STOCK\s*&\s*SALES", peek, re.I):
        result["report_title"] = "STOCK & SALES"
    elif re.search(r"Stock\s+And\s+Sales\s+Statement", peek, re.I):
        result["report_title"] = "Stock And Sales Statement"
    elif re.search(r"Stock\s+and\s+Sale\s+Report", peek, re.I):
        result["report_title"] = "Stock and Sale Report"
    elif re.search(r"Stock\s+Statement", peek, re.I):
        result["report_title"] = "Stock Statement Report"
    elif re.search(r"Statements\s+of", peek, re.I):
        result["report_title"] = "Statements of"
    if not result.get("report_title"):
        for line in peek.splitlines():
            if re.search(r"stock\s+and\s+sales", line, re.I) and re.search(
                r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", line
            ):
                result["report_title"] = re.sub(r"\s+", " ", line).strip()
                break
    first = ""
    for line in peek.splitlines():
        if line.strip():
            first = line.strip()
            break
    stated = re.search(r"Statements\s+of\s+(.+)", peek, re.I)
    company = re.search(r"Company\s*:\s*(.+)", peek, re.I)
    if stated:
        result["stockist_name"] = stated.group(1).strip()
    elif (
        first
        and re.search(r"[A-Za-z]", first)
        and (
            not re.search(r"\d{2,}", first)
            or _letterhead_digits_are_fy_only(first)
        )
        and not re.search(r"^page\s*\d+\s*of\s*\d+", first, re.I)
        and not _stockist_looks_like_qty_headers(first)
        and not re.search(
            r"PRODUCT\s+DESCRIPTION|ITEM\s+DESCRIPTION|^ITEM$|^PRODUCT$",
            first,
            re.I,
        )
    ):
        stockist_first = _LETTERHEAD_FY_RE.sub("", first).strip(" -")
        stockist_first = re.sub(r"\s+", " ", stockist_first).strip() or first
        result["stockist_name"] = stockist_first
        if not parsed.get("letterhead_is_stockist_only"):
            # FY letterhead is the stockist, not the manufacturer/company.
            if not _letterhead_digits_are_fy_only(first):
                result["company_name"] = first
    if company:
        result["company_name"] = company.group(1).strip()
    title_co = re.search(
        r"([A-Z][A-Z0-9 .&'-]{2,}?)\s+STOCK\s*[&+]\s*SALES\s+STATEM",
        peek,
        re.I,
    )
    if title_co:
        result["company_name"] = re.sub(r"\s+", " ", title_co.group(1)).strip()
    # LstSL Stock & Sales Report: "... for : THE HIMALAYA DRUG CO. (DIV.: ZEAL)"
    for_company = re.search(
        r"\bfor\s*:\s*(.+?)(?:\s{2,}Page|\s+Page\s*:|$)",
        peek,
        re.I,
    )
    if for_company:
        result["company_name"] = re.sub(
            r"\s+", " ", for_company.group(1)
        ).strip(" -")
    if parsed.get("prefer_letterhead_stockist") and not result.get("stockist_name"):
        for line in peek.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if re.search(r"\d{2,}", stripped) and not _letterhead_digits_are_fy_only(
                stripped
            ):
                continue
            if re.search(r"^page\s*\d+\s*of\s*\d+", stripped, re.I):
                continue
            if _stockist_looks_like_qty_headers(stripped):
                continue
            if _LETTERHEAD_TABLE_RE.match(stripped):
                continue
            if re.search(
                r"stock\s+statement|stock\s+and\s+sale|company\s*:|item type|"
                r"product name|page\s*:|product\s+description|item\s+description|"
                r"report\s*date",
                stripped,
                re.I,
            ):
                continue
            if re.search(r"[A-Za-z]{3,}", stripped):
                stockist_line = _LETTERHEAD_FY_RE.sub("", stripped).strip(" -")
                result["stockist_name"] = (
                    re.sub(r"\s+", " ", stockist_line).strip() or stripped
                )
                break
    if _stockist_looks_like_qty_headers(result.get("stockist_name")):
        result["stockist_name"] = None
    period = re.search(
        r"(?:From|Duration)(?:\s+Date)?\s*:?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\s+"
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
    if not result.get("period_from") and re.search(r"(?i)\bSTOCK\s+REPORT\b", peek):
        # "01/09/26 To :30/09/26" under STOCK REPORT (no From cue).
        bare_to = re.search(
            r"(?m)^\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\s+"
            r"(?:To|to)\s*:?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\s*$",
            peek,
        )
        if bare_to:
            def _iso_bare(day: str, month: str, year: str) -> str:
                yr = year if len(year) == 4 else ("20" + year)
                return f"{yr}-{int(month):02d}-{int(day):02d}"

            result["period_from"] = _iso_bare(
                bare_to.group(1), bare_to.group(2), bare_to.group(3)
            )
            result["period_to"] = _iso_bare(
                bare_to.group(4), bare_to.group(5), bare_to.group(6)
            )
            logger.info(
                "STOCK_PDF_WORDS_STOCK_REPORT_PERIOD period_from=%s period_to=%s",
                result["period_from"],
                result["period_to"],
            )
    if not result.get("period_from"):
        # FROM 01/Oct/2026 TO 31/Oct/2026 (slash or dash month names).
        month_period = re.search(
            r"From\s*:?\s*(\d{1,2}[-/ ][A-Za-z]{3,9}[-/ ]\d{2,4})\s+"
            r"To\s*:?\s*(\d{1,2}[-/ ][A-Za-z]{3,9}[-/ ]\d{2,4})",
            peek,
            re.I,
        )
        if month_period:
            from services.sales_statement_extractor import _normalize_date

            result["period_from"] = _normalize_date(month_period.group(1))
            result["period_to"] = _normalize_date(month_period.group(2))
    if not result.get("period_from"):
        for_period = re.search(
            r"(?:For\s+the\s+)?Period\s+(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\s+"
            r"To\s+(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})",
            peek,
            re.I,
        )
        if for_period:
            def _iso_period(day: str, month: str, year: str) -> str:
                yr = year if len(year) == 4 else ("20" + year)
                return f"{yr}-{int(month):02d}-{int(day):02d}"

            result["period_from"] = _iso_period(
                for_period.group(1), for_period.group(2), for_period.group(3)
            )
            result["period_to"] = _iso_period(
                for_period.group(4), for_period.group(5), for_period.group(6)
            )
    if not result.get("period_from"):
        compact = re.search(
            r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})\s*(?:[-–]|to)\s*"
            r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})",
            peek,
            re.I,
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
    if not result.get("period_from"):
        month_banner = re.search(
            r"for\s+the\s+month\s+([A-Za-z]+)[\s\-/]+(\d{4})",
            peek,
            re.I,
        )
        if month_banner:
            try:
                from services.sales_statement_extractor import _month_period

                pf, pt = _month_period(
                    month_banner.group(1).strip(), int(month_banner.group(2))
                )
                if pf and pt:
                    result["period_from"] = pf
                    result["period_to"] = pt
            except Exception:
                pass
    if not result.get("period_from"):
        # Months SS Report: "for The period of 2026-10-01 to 2026-10-31"
        iso_period = re.search(
            r"(?:for\s+the\s+)?period(?:\s+of)?\s*:?\s*"
            r"(\d{4}-\d{2}-\d{2})\s*(?:to|upto|-|–|—)\s*"
            r"(\d{4}-\d{2}-\d{2})",
            peek,
            re.I,
        )
        if iso_period:
            result["period_from"] = iso_period.group(1)
            result["period_to"] = iso_period.group(2)
            logger.info(
                "STOCK_PDF_WORDS_ISO_PERIOD period_from=%s period_to=%s",
                result["period_from"],
                result["period_to"],
            )
    if parsed.get("company_name"):
        result["company_name"] = parsed["company_name"]
        logger.info(
            "STOCK_PDF_WORDS_DIVISION_COMPANY applied company=%s",
            result["company_name"],
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
    _apply_pdf_words_letterhead_meta(result, peek)
    _apply_opening_received_issued_header_summary(result, peek)
    # STOCK STATEMENT : HIMALAYA ZEAL — company is the titled division.
    stmt_co = re.search(
        r"STOCK\s+STATEMENT\s*:\s*([^\n]+)",
        peek,
        re.I,
    )
    if stmt_co and stmt_co.group(1).strip():
        result["company_name"] = re.sub(
            r"\s+", " ", stmt_co.group(1)
        ).strip(" -")
    # Printed Manufacturer/Division beats a letterhead street line.
    mfr_line = re.search(
        r"Manufacturer\s*Name\s*:\s*(.+?)(?:\s+Division\b|$)",
        peek,
        re.I,
    )
    div_line = re.search(
        r"Division\s*Name\s*[:-]+\s*([^\n]+)",
        peek,
        re.I,
    )
    if div_line and div_line.group(1).strip():
        result["company_name"] = re.sub(
            r"\s+", " ", div_line.group(1)
        ).strip(" -")
        logger.info(
            "STOCK_PDF_WORDS_MANUFACTURER_DIVISION company=%s",
            result["company_name"],
        )
    elif mfr_line and mfr_line.group(1).strip():
        result["company_name"] = re.sub(
            r"\s+", " ", mfr_line.group(1)
        ).strip(" -")
        logger.info(
            "STOCK_PDF_WORDS_MANUFACTURER_NAME company=%s",
            result["company_name"],
        )
    _apply_op_qty_pur_sale_bal_meta(result, peek)
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["extraction_method"] = (
        "pdf_words_lms_qty"
        if parsed.get("qty_only") and any(
            isinstance(it, dict)
            and isinstance(it.get("extra"), dict)
            and it["extra"].get("lms_qty") is not None
            for it in (result.get("line_items") or [])
        )
        else "pdf_words_free_columns"
    )
    extra["statement_count"] = 1
    if parsed.get("qty_only"):
        extra["qty_only"] = True
        # Qty-only movement grids have no per-line money columns — keep null,
        # never leave invented 0 / sales_qty copies on line items.
        for it in result.get("line_items") or []:
            if not isinstance(it, dict):
                continue
            if it.get("sales_value") in (0, 0.0) and it.get("sales_qty") not in (
                None,
                "",
            ):
                # Defensive: some upstream paths copy qty into value.
                if abs(float(it.get("sales_value") or 0) - float(it.get("sales_qty") or -1)) < 0.001:
                    it["sales_value"] = None
            if it.get("closing_value") in (0, 0.0) and "closing_value" not in (
                parsed.get("value_summary") or {}
            ):
                it["closing_value"] = None
    if parsed.get("closing_value") is not None:
        result["totals"]["closing_value"] = parsed["closing_value"]
    if parsed.get("sales_value") is not None:
        result["totals"]["sales_value"] = parsed["sales_value"]
    if parsed.get("lms_value") is not None:
        extra["lms_value"] = parsed["lms_value"]
        extra["last_month_sales_value"] = parsed["lms_value"]
    # Three money lines above NEAR EXPIRY DETAIL = LMS / Sales / Closing values.
    # Always prefer these printed amounts for LMS qty-only sheets.
    money_band = _money_footer_from_text_before_near_expiry(peek)
    if money_band.get("lms_value") is not None:
        extra["lms_value"] = money_band["lms_value"]
        extra["last_month_sales_value"] = money_band["lms_value"]
    if money_band.get("sales_value") is not None:
        result["totals"]["sales_value"] = money_band["sales_value"]
    if money_band.get("closing_value") is not None:
        result["totals"]["closing_value"] = money_band["closing_value"]
        extra["closing_value"] = money_band["closing_value"]
    near_details = parse_near_expiry_detail_section(peek)
    if near_details:
        extra["near_expiry_details"] = [
            _near_expiry_detail_record(row) for row in near_details
        ]
        result["near_expiry_details"] = list(extra["near_expiry_details"])
        annotate_near_expiry_discrepancies(
            result.get("line_items") or [], extra["near_expiry_details"]
        )
        logger.info(
            "STOCK_NEAR_EXPIRY_DETAIL rows=%s",
            len(near_details),
        )
    for key, value in (parsed.get("value_summary") or {}).items():
        if key in {"sales_value", "closing_value"} or value is None:
            continue
        extra[key] = value
    footer = parsed.get("footer_totals")
    if not footer:
        footer = _footer_totals_from_page_results(page_results)
    if not footer and any(money_band.get(k) is not None for k in money_band):
        footer = {
            "lms_value": money_band.get("lms_value"),
            "sales_value": money_band.get("sales_value"),
            "closing_value": money_band.get("closing_value"),
            "last_month_sales_value": money_band.get("lms_value"),
        }
    # Manufacturer Op/Pur/Sale/Bal money band already filled totals — do not
    # let a qty-only footer wipe sales_value / closing_value or the source tag.
    keep_mfr_band = bool(extra.get("manufacturer_band_totals"))
    keep_header_summary = bool(
        extra.get("total_row_source") == "opening_received_issued_header"
        or result["totals"].get("sales_value") not in (None, "")
        or result["totals"].get("closing_value") not in (None, "")
    )
    if footer:
        if not keep_mfr_band and not keep_header_summary:
            extra["total_row_source"] = "printed_footer"
        elif not keep_mfr_band and keep_header_summary:
            extra["total_row_source"] = (
                extra.get("total_row_source") or "printed_footer"
            )
            if footer.get("opening_value") is not None or footer.get(
                "closing_value"
            ) is not None:
                extra["total_row_source"] = "opening_received_issued_grouptotal"
        for key in (
            "opening_qty",
            "receipts_qty",
            "sales_qty",
            "closing_qty",
            "opening_value",
            "receipts_value",
            "last_month_sales_value",
            "sales_issue_value",
            "purchase_value",
        ):
            if footer.get(key) is not None:
                if keep_mfr_band and key in {"opening_value", "receipts_value"}:
                    continue
                if keep_header_summary and key in {
                    "opening_value",
                    "receipts_value",
                    "purchase_value",
                }:
                    # Header labels already filled these; keep printed values.
                    if result["totals"].get(key) not in (None, "") or extra.get(
                        key
                    ) not in (None, ""):
                        continue
                if key in {
                    "opening_qty",
                    "receipts_qty",
                    "sales_qty",
                    "closing_qty",
                    "opening_value",
                    "receipts_value",
                }:
                    result["totals"][key] = footer[key]
                extra[key] = footer[key]
        if (
            footer.get("sales_value") is not None
            and not keep_mfr_band
            and result["totals"].get("sales_value") in (None, "")
        ):
            result["totals"]["sales_value"] = footer["sales_value"]
        if (
            footer.get("closing_value") is not None
            and not keep_mfr_band
            and result["totals"].get("closing_value") in (None, "")
        ):
            result["totals"]["closing_value"] = footer["closing_value"]
    # Ensure period from peek survives the content-only finalize late pass.
    try:
        from services.sales_statement_extractor import (
            _finalize_statement_period_fields,
        )

        result = _finalize_statement_period_fields(result, content_text=peek)
    except Exception:
        pass
    result = finalize_lms_qty_only_statement(result)
    logger.info(
        "STOCK_PDF_WORDS_TABLE file=%s items=%s closing_value=%s",
        filename,
        len(result["line_items"]),
        result["totals"].get("closing_value"),
    )
    return result


def parse_pdf_split_column_statement(
    file_bytes: bytes, filename: str
) -> Optional[Dict[str, Any]]:
    """Text PDFs that print product/opening on left pages and sale/closing on right.

    Wide STOCK & SALES sheets often emit every left half first, then every right
    half. Uses PyMuPDF words + STOCK_PDF_SPLIT_COLUMN_PAGES stitch (0 Gemini).
    Returns None unless at least one page pair was stitched.
    """
    if os.getenv("STOCK_PDF_SPLIT_COLUMN_PAGES", "true").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None
    from services.sales_statement_extractor import empty_result
    from services.stock_native_tables import from_fitz_pdf_bytes

    try:
        built = from_fitz_pdf_bytes(file_bytes)
    except Exception as exc:
        logger.info(
            "STOCK_PDF_SPLIT_COLUMN_PAGES file=%s status=skipped error=%s",
            filename,
            type(exc).__name__,
        )
        return None
    page_results = built.get("page_results") or []
    stitched = [
        p
        for p in page_results
        if isinstance(p, dict) and p.get("split_column_pages")
    ]
    if len(stitched) < 1:
        return None
    parsed = _items_from_resolved_word_columns(stitched)
    if not parsed or not parsed.get("line_items"):
        items = build_items_from_pdf_page_results(stitched)
        kept = [
            it
            for it in items
            if isinstance(it, dict)
            and str(it.get("product_name") or "").strip()
            and not _is_total_row_name(it.get("product_name"))
            and not _is_sheet_total_name(it.get("product_name"))
        ]
        if len(kept) < 3:
            logger.info(
                "STOCK_PDF_SPLIT_COLUMN_PAGES file=%s status=too_few items=%s",
                filename,
                len(kept),
            )
            return None
        parsed = {"line_items": kept, "prefer_letterhead_stockist": True}
    peek = ""
    try:
        import fitz
        from services.stock_header_resolver import decode_pdf_pseudo_digits

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            peek = decode_pdf_pseudo_digits(doc[0].get_text("text") or "")
        finally:
            doc.close()
    except Exception:
        peek = ""
    result = empty_result(filename, "pdf")
    result["line_items"] = list(parsed.get("line_items") or [])
    if re.search(r"Stock\s+And\s+Sales\s+Statement", peek, re.I):
        result["report_title"] = "Stock And Sales Statement"
    elif re.search(r"STOCK\s+AND\s+SALES", peek, re.I):
        result["report_title"] = "STOCK AND SALES STATEMENT"
    first = next((ln.strip() for ln in peek.splitlines() if ln.strip()), "")
    if first and re.search(r"[A-Za-z]", first) and not re.search(
        r"PRODUCT\s+NAME|^PCode$|STOCK\s+AND\s+SALES", first, re.I
    ):
        result["stockist_name"] = first
    period = re.search(
        r"From\s+Date\s*:?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\s+"
        r"Upto\s+Date\s*:?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})",
        peek,
        re.I,
    )
    if period:
        def _iso(day: str, month: str, year: str) -> str:
            yr = year if len(year) == 4 else ("20" + year)
            return f"{yr}-{int(month):02d}-{int(day):02d}"

        result["period_from"] = _iso(period.group(1), period.group(2), period.group(3))
        result["period_to"] = _iso(period.group(4), period.group(5), period.group(6))
    mfgr = re.search(r"MFGR\s*:\s*(.+)", peek, re.I)
    if mfgr:
        result["company_name"] = mfgr.group(1).strip()
    _apply_pdf_words_letterhead_meta(result, peek)
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["extraction_method"] = "pdf_split_column_pages"
    extra["statement_count"] = 1
    # Statement-level transfers (not printed per product row). Logged so
    # identity gaps are not mistaken for column mis-assignment.
    for label, key in (
        ("TRFIN", "transfer_in_value"),
        ("TRFOUT", "transfer_out_value"),
        ("EXSTK", "excess_stock_value"),
    ):
        m = re.search(rf"\b{label}\s*:\s*([0-9,.]+)", peek, re.I)
        if not m:
            continue
        from services.stock_header_resolver import parse_number

        amount = parse_number(m.group(1))
        if amount is not None:
            extra[key] = amount
            logger.info(
                "STOCK_PDF_SPLIT_COLUMN_PAGES statement_%s=%s",
                key,
                amount,
            )
            extra["identity_note"] = (
                "statement_has_trfin_trfout_not_printed_per_row"
            )
    footer = parsed.get("footer_totals") or _footer_totals_from_page_results(stitched)
    if footer:
        extra["total_row_source"] = "printed_footer"
        for key in (
            "opening_qty",
            "receipts_qty",
            "sales_qty",
            "closing_qty",
            "opening_value",
            "receipts_value",
            "sales_value",
            "closing_value",
        ):
            if footer.get(key) is not None:
                result["totals"][key] = footer[key]
                extra[key] = footer[key]
    logger.info(
        "STOCK_PDF_SPLIT_COLUMN_PAGES file=%s pairs=%s items=%s",
        filename,
        len(stitched),
        len(result["line_items"]),
    )
    return result
