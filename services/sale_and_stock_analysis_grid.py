"""Phone-screenshot Excel grid: Item | Pack | Opening | Purchase | Sale* | Closing.

Layout family ``sale_and_stock_analysis_grid``. Fires only when the printed
headers (or the OCR peek) show SALE AND STOCK ANALYSIS with Purchase Free /
Sale Return / Others. Other stockist routes are not modified.

Does not invent a quantity. Sale* is copied from that cell. Footer rupee
totals stay out of line qty fields. A later comparison-backup between readers
is intentionally not wired here.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_LAYOUT = "sale_and_stock_analysis_grid"

# Full names used only to restore a clipped leading character on this layout.
# Pack must match, and the visible text must be a 1–3 character suffix.
_NAME_PACKS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("ACTARIL SOAP", ("75GM",)),
    ("ABANA TAB", ("60S",)),
    ("CONFIDO TAB", ("60S",)),
    ("LIV-52 SYP 200ML", ("200ML",)),
    ("LIV-52 SYP 100ML", ("100ML",)),
    ("LIV-52 TAB", ("100S",)),
    ("HERBOLAX TAB", ("100S",)),
)

_DASH = {"", "-", "—", "–", "null", "none", "n/a", "na"}


def sale_and_stock_analysis_grid_enabled() -> bool:
    """In-code toggle. Default ON. No new environment key."""
    return True


def build_sale_and_stock_analysis_grid_prompt() -> str:
    """Column-anchor prompt used only after the layout detector matches."""
    return (
        "This image is a phone screenshot of an Excel sheet titled "
        "SALE AND STOCK ANALYSIS (WPS or similar). "
        "layout family sale_and_stock_analysis_grid. "
        "Transcribe the grid. Do not calculate, balance, or invent digits.\n"
        "Return JSON with one table. header_rows left to right, one cell per "
        "printed column, even when the header wraps:\n"
        "Item Name, Pack, Opening, Purchase, Purchase Free, Purchase Return, "
        "Sale* (Approved), Sale Free, Sale Return, Others, Closing.\n"
        "Sale* (Approved) is ONE column. If the header wraps as 'Sale*' over "
        "'Approved' / 'Appro', still emit a single header 'Sale*' — do not "
        "add an Appro column (that shifts Sale Free and Closing left).\n"
        "rows: every product, cells exactly column_count, dashes and blanks "
        "are null, never 0. A printed 0 stays 0.\n"
        "Mapping: Opening→opening qty, Purchase→purchase qty (not Sale), "
        "Purchase Free→free inward, Purchase Return→purchase return, "
        "Sale*→sales qty (paid sale only, not Sale Free, not Closing, not "
        "Purchase), Sale Free→sale free, Sale Return→sale return, "
        "Others→others, Closing→closing qty.\n"
        "Copy Opening even when it is a small integer. Never force Opening "
        "to 0. Never put the Sale* number into Purchase or receipts.\n"
        "Product names may be clipped on the left edge of the screenshot. "
        "Copy only the visible letters. Do not guess the missing prefix.\n"
        "Rows named Total, Grand Total, Total (HM4), or a left-clipped "
        "'tal (HM4)' / 'and (HM4)' are footer MONEY (large decimals such as "
        "793445.19). Set is_total_row=true. Never emit them as products and "
        "never put those decimals into quantity cells.\n"
        "Division banners such as 'HM4' are not products. "
        "Qty cells are small integers. A large decimal is money and belongs "
        "only on the total row.\n"
        "Do not invent sales from Closing minus Opening. If Sale* is blank, "
        "sales is null."
    )


def _excluded_chrome(blob: str) -> bool:
    if re.search(r"\b(?:B\s*VAL|S\s*VAL|BVAL|SVAL)\b", blob, re.I):
        return True
    if re.search(r"\bPharmAssist\b", blob, re.I):
        return True
    if re.search(r"LstSL|ClosStock|SaleRet", blob, re.I):
        return True
    if re.search(r"\bOp\.?\s*Stock\b|\bCl\.?\s*Stock\b", blob, re.I):
        return True
    if re.search(
        r"OPENING\s+VALUE|ISSUE\s+VALUE|RECEIPT\s+VALUE|RECEIVE\s+VALUE",
        blob,
        re.I,
    ):
        return True
    if re.search(r"\bVAL(?:UE)?\b", blob, re.I) and re.search(
        r"\bQTY\b", blob, re.I
    ):
        return True
    # OPENING STOCK / CLOSING STOCK qty grids are a different family.
    if re.search(r"\bOPENING\s+STOCK\b|\bCLOSING\s+STOCK\b", blob, re.I):
        return True
    return False


def text_looks_like_sale_and_stock_analysis_grid(text: str) -> bool:
    """True for this Excel phone grid only.

    Requires Opening + Purchase + Sale + Closing, and either the title
    SALE AND STOCK ANALYSIS or the Purchase Free + Sale Return + Others
    header triple. Qty+value, PharmAssist, LstSL, and Op.Stock grids do
    not match.
    """
    if not sale_and_stock_analysis_grid_enabled():
        return False
    blob = text or ""
    if not blob.strip() or _excluded_chrome(blob):
        return False
    has_open = bool(re.search(r"\bOpening\b", blob, re.I))
    has_pur = bool(re.search(r"\bPurchase\b", blob, re.I))
    has_close = bool(re.search(r"\bClos(?:e|ing)\b", blob, re.I))
    has_sale = bool(re.search(r"\bSale\*?\b", blob, re.I))
    if not (has_open and has_pur and has_close and has_sale):
        return False
    title = bool(
        re.search(r"SALE\s+(?:AND|&)\s+STOCK\s+ANALYSIS", blob, re.I)
    )
    purchase_free = bool(re.search(r"Purchase\s+Fre", blob, re.I))
    sale_return = bool(re.search(r"Sale\s+Return", blob, re.I))
    others = bool(re.search(r"\bOthers\b", blob, re.I))
    if title and (purchase_free or sale_return or others):
        return True
    return bool(purchase_free and sale_return and others)


def headers_look_like_sale_and_stock_analysis_grid(
    headers: Sequence[Any],
) -> bool:
    parts = [str(h or "").strip() for h in (headers or []) if str(h or "").strip()]
    if len(parts) < 6:
        return False
    return text_looks_like_sale_and_stock_analysis_grid(" ".join(parts))


def _result_headers(result: Dict[str, Any]) -> List[str]:
    extra = ((result.get("totals") or {}).get("extra") or {})
    column_map = extra.get("column_map") or []
    headers: List[str] = []
    if isinstance(column_map, list):
        for col in column_map:
            if isinstance(col, dict) and str(col.get("header_text") or "").strip():
                headers.append(str(col.get("header_text")))
    if headers:
        return headers
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        raw = (item.get("extra") or {}).get("vision_column_headers")
        if isinstance(raw, (list, tuple)) and raw:
            return [str(h or "") for h in raw]
    return []


def result_matches_sale_and_stock_analysis_grid(result: Dict[str, Any]) -> bool:
    if not isinstance(result, dict) or not sale_and_stock_analysis_grid_enabled():
        return False
    extra = ((result.get("totals") or {}).get("extra") or {})
    if str(extra.get("layout") or "") == _LAYOUT:
        return True
    headers = _result_headers(result)
    if headers_look_like_sale_and_stock_analysis_grid(headers):
        return True
    title = str(result.get("report_title") or "")
    blob = " ".join([title, " ".join(headers)])
    return text_looks_like_sale_and_stock_analysis_grid(blob)


def _norm_name(value: Any) -> str:
    text = str(value or "").upper().replace("’", "'")
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _norm_pack(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def restore_clipped_leading_name(name: Any, pack: Any) -> Optional[str]:
    """Restore a missing 1–3 character prefix when pack matches one catalog row.

    Returns None when the visible name should be kept.
    """
    observed = _norm_name(name)
    pack_key = _norm_pack(pack)
    if len(observed) < 4 or not pack_key:
        return None
    for full, packs in _NAME_PACKS:
        if pack_key not in packs:
            continue
        if _norm_name(full) == observed:
            return None

    def _hits(needle: str) -> List[str]:
        found: List[str] = []
        if len(needle) < 4:
            return found
        for full, packs in _NAME_PACKS:
            if pack_key not in packs:
                continue
            catalog = _norm_name(full)
            gap = len(catalog) - len(needle)
            if catalog.endswith(needle) and 1 <= gap <= 3:
                found.append(full)
        return found

    direct = _hits(observed)
    if len(direct) == 1:
        return direct[0]
    if direct:
        return None
    if observed and observed[0].isalpha():
        dropped = _hits(observed[1:].strip())
        if len(dropped) == 1:
            return dropped[0]
    return None


def _header_role(text: Any) -> Optional[str]:
    raw = str(text or "").lower().replace("*", " ")
    norm = re.sub(r"[^a-z0-9]+", " ", raw).strip()
    if not norm:
        return None
    if re.search(r"\bval", norm):
        return None
    if "purchase" in norm and re.search(r"\bfre", norm):
        return "free_in"
    if "purchase" in norm and re.search(r"\bre", norm):
        return "purchase_return"
    if re.search(r"\bsale", norm) and "free" in norm:
        return "free_out"
    if re.search(r"\bsale", norm) and "ret" in norm:
        return "sales_return"
    if norm in {"appro", "approved", "approve"} or norm.startswith("appro "):
        return "ignore"
    if re.search(r"\bsale", norm):
        return "sales"
    if "opening" in norm or norm in {"open", "op"}:
        return "opening"
    if "purchase" in norm or norm in {"pur", "purch"}:
        return "purchase"
    if "closing" in norm or norm.startswith("clos"):
        return "closing"
    if norm.startswith("other"):
        return "others"
    if norm in {"pack", "packing", "pk"} or norm.startswith("pack"):
        return "pack"
    if "item" in norm or "product" in norm or norm in {"name", "m on"}:
        return "name"
    return None


def _role_index(headers: Sequence[Any]) -> Dict[str, int]:
    roles: Dict[str, int] = {}
    for idx, header in enumerate(headers):
        role = _header_role(header)
        if not role or role == "ignore" or role in roles:
            continue
        roles[role] = idx
    needed = {"opening", "purchase", "sales", "closing"}
    if not needed.issubset(roles):
        return {}
    return roles


def _cell_list(item: Dict[str, Any], width: int) -> List[Any]:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    raw = extra.get("vision_raw_cells")
    if isinstance(raw, (list, tuple)) and raw:
        cells = list(raw)
    else:
        physical = extra.get("physical_cells")
        cells = [None] * width
        if isinstance(physical, dict):
            for key, value in physical.items():
                try:
                    idx = int(key)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < width:
                    cells[idx] = value
                elif idx >= width:
                    cells.extend([None] * (idx + 1 - len(cells)))
                    cells[idx] = value
    if len(cells) < width:
        cells.extend([None] * (width - len(cells)))
    return cells


def _parse_cell(value: Any) -> Tuple[str, Optional[float]]:
    """Return ('empty'|'qty'|'money', number)."""
    if value is None or isinstance(value, bool):
        return "empty", None
    if isinstance(value, (int, float)):
        number = float(value)
        if abs(number - int(number)) > 0.001 and abs(number) >= 1000:
            return "money", number
        return "qty", number
    text = str(value).strip()
    if text.lower() in _DASH:
        return "empty", None
    cleaned = text.replace(",", "").replace(" ", "")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        return "empty", None
    number = float(cleaned)
    if abs(number - int(number)) > 0.001 and abs(number) >= 1000:
        return "money", number
    return "qty", number


def _is_footer_name(name: Any) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    if re.match(
        r"(?i)^\s*(?:grand\s+|sub\s+|net\s+)?tot(?:al)?\b",
        text,
    ):
        return True
    if re.match(r"(?i)^\s*(?:tal|rand\s*total|and\s*total)\b", text):
        return True
    if re.search(r"(?i)\(\s*HM\d+\s*\)\s*$", text) and re.search(
        r"(?i)tal|total|grand", text
    ):
        return True
    if re.fullmatch(r"(?i)and(?:\s*\([^)]*\))?", text):
        return True
    if re.match(r"(?i)^vision\s*:?\s*HM\d+\s*$", text):
        return True
    if re.fullmatch(r"(?i)HM\d+", text):
        return True
    return False


def _set_qty(item: Dict[str, Any], field: str, number: Optional[float]) -> None:
    extra = item.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        item["extra"] = extra
    fs = extra.setdefault("field_source", {})
    if not isinstance(fs, dict):
        fs = {}
        extra["field_source"] = fs
    if field in {"opening_qty", "sales_qty", "closing_qty", "purchase_qty", "receipts_qty"}:
        item[field] = number
    else:
        extra[field] = number
        if field in {
            "purchase_return_qty",
            "sales_return_qty",
        }:
            item[field] = number
    fs[field] = "printed" if number is not None else "missing"


def _reslot_item(item: Dict[str, Any], roles: Dict[str, int], headers: Sequence[Any]) -> bool:
    cells = _cell_list(item, len(headers))
    if not any(cells):
        return False
    money_hits = 0
    parsed: Dict[str, Tuple[str, Optional[float]]] = {}
    for role, idx in roles.items():
        if idx >= len(cells):
            parsed[role] = ("empty", None)
            continue
        kind, number = _parse_cell(cells[idx])
        parsed[role] = (kind, number)
        if kind == "money":
            money_hits += 1
    name = item.get("product_name")
    if roles.get("name") is not None:
        raw_name = cells[roles["name"]]
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
    if _is_footer_name(name) or money_hits >= 3:
        item["_grid_drop"] = True
        if money_hits:
            item["_grid_money"] = {
                role: number
                for role, (kind, number) in parsed.items()
                if kind == "money" and number is not None
            }
        return True
    if roles.get("name") is not None and isinstance(name, str) and name.strip():
        item["product_name"] = name.strip()
    if roles.get("pack") is not None:
        pack_cell = cells[roles["pack"]]
        if isinstance(pack_cell, str) and pack_cell.strip():
            item["packing"] = pack_cell.strip()

    def _qty(role: str) -> Optional[float]:
        kind, number = parsed.get(role, ("empty", None))
        if kind == "qty":
            return number
        return None

    opening = _qty("opening")
    purchase = _qty("purchase")
    sales = _qty("sales")
    closing = _qty("closing")
    _set_qty(item, "opening_qty", opening)
    _set_qty(item, "purchase_qty", purchase)
    _set_qty(item, "receipts_qty", purchase)
    _set_qty(item, "sales_qty", sales)
    _set_qty(item, "closing_qty", closing)
    extra = item.setdefault("extra", {})
    if _qty("free_in") is not None or "free_in" in roles:
        extra["free_in_qty"] = _qty("free_in")
        extra["purchase_free_qty"] = _qty("free_in")
    if _qty("free_out") is not None or "free_out" in roles:
        extra["free_out_qty"] = _qty("free_out")
        extra["sale_free_qty"] = _qty("free_out")
    if "purchase_return" in roles:
        _set_qty(item, "purchase_return_qty", _qty("purchase_return"))
    if "sales_return" in roles:
        _set_qty(item, "sales_return_qty", _qty("sales_return"))
    if "others" in roles:
        extra["other_outward_qty"] = _qty("others")
        extra["others_qty"] = _qty("others")
    flags = list(extra.get("flags") or [])
    if "SALE_AND_STOCK_ANALYSIS_GRID_RESLOT" not in flags:
        flags.append("SALE_AND_STOCK_ANALYSIS_GRID_RESLOT")
    extra["flags"] = flags
    extra["layout"] = _LAYOUT
    return True


def _restore_names(items: Sequence[Dict[str, Any]]) -> int:
    restored = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        fixed = restore_clipped_leading_name(
            item.get("product_name"), item.get("packing")
        )
        if not fixed:
            continue
        item["product_name"] = fixed
        extra = item.setdefault("extra", {})
        if isinstance(extra, dict):
            extra["name_source"] = "clipped_leading_char"
        restored += 1
    return restored


def opening_sale_receipt_failure(result: Dict[str, Any]) -> bool:
    """#26989 signature: openings and sales wiped, Sale parked in receipts."""
    items = [
        i
        for i in (result.get("line_items") or [])
        if isinstance(i, dict) and not _is_footer_name(i.get("product_name"))
    ]
    if len(items) < 5:
        return False
    open_zero = 0
    sale_zero = 0
    receipt_hit = 0
    for item in items:
        try:
            opening = float(item.get("opening_qty") or 0)
        except (TypeError, ValueError):
            opening = 0.0
        try:
            sales = float(item.get("sales_qty") or 0)
        except (TypeError, ValueError):
            sales = 0.0
        try:
            receipts = float(item.get("receipts_qty") or item.get("purchase_qty") or 0)
        except (TypeError, ValueError):
            receipts = 0.0
        if abs(opening) < 0.01:
            open_zero += 1
        if abs(sales) < 0.01:
            sale_zero += 1
        if receipts > 0.01:
            receipt_hit += 1
    return (
        open_zero / len(items) >= 0.8
        and sale_zero / len(items) >= 0.8
        and receipt_hit >= 3
    )


def grid_needs_prompt_retry(
    result: Dict[str, Any],
    *,
    used_dedicated_prompt: bool,
    calls: int,
    budget: int,
) -> bool:
    if used_dedicated_prompt or calls >= budget:
        return False
    if not result_matches_sale_and_stock_analysis_grid(result):
        return False
    return opening_sale_receipt_failure(result)


def prefer_grid_read(current: Dict[str, Any], retry: Dict[str, Any]) -> bool:
    """Keep the dedicated reread only when it clears the failure signature."""
    if not isinstance(retry, dict) or not retry.get("line_items"):
        return False
    if opening_sale_receipt_failure(retry):
        return False
    return opening_sale_receipt_failure(current)


def _sum_sales(items: Sequence[Dict[str, Any]]) -> float:
    total = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            total += float(item.get("sales_qty") or 0)
        except (TypeError, ValueError):
            continue
    return total


def apply_sale_and_stock_analysis_grid(result: Dict[str, Any]) -> Dict[str, Any]:
    """Re-slot printed Sale* / Opening / Closing cells for this layout only.

    Returns the same object unchanged when the detector does not match.
    """
    if not result_matches_sale_and_stock_analysis_grid(result):
        return result
    headers = _result_headers(result)
    roles = _role_index(headers)
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    kept: List[Dict[str, Any]] = []
    footer_money: Dict[str, float] = {}
    reslotted = 0
    for item in items:
        if roles:
            _reslot_item(item, roles, headers)
        if item.pop("_grid_drop", False) or _is_footer_name(item.get("product_name")):
            money = item.pop("_grid_money", None) or {}
            if isinstance(money, dict):
                for role, number in money.items():
                    footer_money.setdefault(role, float(number))
            continue
        item.pop("_grid_money", None)
        kept.append(item)
        if "SALE_AND_STOCK_ANALYSIS_GRID_RESLOT" in list(
            (item.get("extra") or {}).get("flags") or []
        ):
            reslotted += 1
    restored = _restore_names(kept)
    result["line_items"] = kept
    totals = result.setdefault("totals", {})
    if not isinstance(totals, dict):
        totals = {}
        result["totals"] = totals
    extra = totals.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        totals["extra"] = extra
    sales_sum = _sum_sales(kept)
    totals["sales_qty"] = sales_sum
    extra["sales_qty"] = sales_sum
    extra["line_sales_qty_sum"] = sales_sum
    extra["layout"] = _LAYOUT
    # Same identity family as ITEM|PACK|FREE|SALE so printed closing is kept
    # and totals.sales_qty stays the sum of Sale* cells.
    extra["stock_identity_kind"] = "item_pack_free_return"
    extra["sale_and_stock_analysis_grid"] = True
    if footer_money.get("sales") is not None and totals.get("sales_value") is None:
        totals["sales_value"] = footer_money["sales"]
    if footer_money.get("closing") is not None and totals.get("closing_value") is None:
        totals["closing_value"] = footer_money["closing"]
    if footer_money.get("opening") is not None and extra.get("opening_value") is None:
        extra["opening_value"] = footer_money["opening"]
    if footer_money.get("purchase") is not None and extra.get("purchase_value") is None:
        extra["purchase_value"] = footer_money["purchase"]
    logger.info(
        "SALE_AND_STOCK_ANALYSIS_GRID layout=%s stage=apply rows=%s "
        "reslotted=%s names_restored=%s sales_qty=%s",
        _LAYOUT,
        len(kept),
        reslotted,
        restored,
        sales_sum,
    )
    return result


def maybe_retry_sale_and_stock_analysis_grid(
    result: Dict[str, Any],
    image_bytes: bytes,
    *,
    request_id: str,
    calls: int,
    budget: int,
    used_dedicated_prompt: bool,
) -> Tuple[Dict[str, Any], int]:
    """One stricter Vision read when this grid was mis-slotted. No-op otherwise."""
    if not grid_needs_prompt_retry(
        result,
        used_dedicated_prompt=used_dedicated_prompt,
        calls=calls,
        budget=budget,
    ):
        return result, 0
    if not image_bytes:
        return result, 0
    logger.info(
        "SALE_AND_STOCK_ANALYSIS_GRID layout=%s stage=retry request_id=%s",
        _LAYOUT,
        request_id,
    )
    from services.stock_vision_table import (
        extract_stock_table_vision,
        map_vision_table,
    )

    raw = extract_stock_table_vision(
        [image_bytes],
        {
            "label": "sale_and_stock_analysis_grid",
            "prompt": build_sale_and_stock_analysis_grid_prompt(),
            "request_id": request_id,
        },
    )
    if not isinstance(raw, dict) or raw.get("error"):
        return result, 1
    mapped = map_vision_table(raw, request_id=request_id)
    trial_items = list(mapped.get("line_items") or [])
    if not trial_items:
        return result, 1
    trial = dict(result)
    trial["line_items"] = trial_items
    totals = dict(result.get("totals") or {})
    extra = dict((result.get("totals") or {}).get("extra") or {})
    if mapped.get("column_map"):
        extra["column_map"] = mapped["column_map"]
    totals["extra"] = extra
    trial["totals"] = totals
    trial = apply_sale_and_stock_analysis_grid(trial)
    if not prefer_grid_read(result, trial):
        logger.info(
            "SALE_AND_STOCK_ANALYSIS_GRID layout=%s stage=retry_kept_original",
            _LAYOUT,
        )
        return result, 1
    extra = trial.setdefault("totals", {}).setdefault("extra", {})
    extra["sale_and_stock_analysis_grid_retry"] = True
    extra["gemini_calls"] = calls + 1
    logger.info(
        "SALE_AND_STOCK_ANALYSIS_GRID layout=%s stage=retry_accepted rows=%s "
        "sales_qty=%s",
        _LAYOUT,
        len(trial.get("line_items") or []),
        extra.get("sales_qty"),
    )
    return trial, 1
