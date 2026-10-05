"""Shared header-to-canonical-field resolver (Phase 2a).

Pure module: no I/O, not wired into existing readers yet.
Alias tables are copied/merged from existing parsers; those tables are untouched.
"""

from __future__ import annotations

import os
import re
from statistics import median
from typing import Any, Dict, List, Optional, Sequence, Tuple

CANONICAL_FIELDS = (
    "product_name",
    "pack",
    "batch",
    "rate",
    "lms",
    "opening_qty",
    "opening_value",
    "purchase_qty",
    "purchase_value",
    "free_in_qty",
    "sales_qty",
    "sales_value",
    "free_out_qty",
    "sales_return_qty",
    "purchase_return_qty",
    "expiry_damage_qty",
    "total_qty",
    "closing_qty",
    "closing_value",
    "order_qty",
    "ignore",
)

# Short aliases must match a whole token, never a substring.
_SHORT_TOKEN_ALIASES = frozenset(
    {"op", "in", "out", "sr", "cb", "rec", "sl", "pi", "dn", "cn", "tot"}
)

# Group aliases (normalised phrases). Mapping is to a group, then qty/value
# and free direction are resolved with context.
ALIASES: Dict[str, List[str]] = {
    "product_name": [
        "product name",
        "product",
        "item",
        "item name",
        "item description",
        "product description",
        "product desc",
        "particulars",
        "medicine name",
        "drug name",
        "mat name",
        "material name",
        "material",
        "sku description",
        "sku desc",
        "description",
        "desc",
        "name",
    ],
    "pack": [
        "pack",
        "packing",
        "packg",
        "pack size",
        "packing size",
        "size",
        "unit",
    ],
    "batch": [
        "batch",
        "batch no",
        "batch number",
        "batchno",
        "b no",
        "lot",
        "lot no",
    ],
    "rate": [
        "rate",
        "ptr",
        "pts",
        "mrp",
        "unit rate",
        "sales rate",
        "price",
    ],
    # Last-month-sale (LMS) is its own column — never opening/receipts.
    "lms": [
        "lms",
        "lm sale",
        "lm sales",
        "last month sale",
        "last month sales",
        "last month sale qty",
    ],
    "opening": [
        "op",
        "opn",
        "opg",
        "opening",
        "opstk",
        "op stk",
        "ob",
        "opbal",
        "op bal",
        "opening stock",
        "opening qty",
        "opening quantity",
        "op stock",
        "op.",
        "op.bal",
        "op.bal.",
        "opening bal",
        "opening balance",
        "qty opening",
        "stock opening",
    ],
    "purchase": [
        "pur",
        "purc",
        "purch",
        "purchase",
        "purchases",
        "recd",
        "rec",
        "rcpt",
        "recpt",
        "receipt",
        "receipts",
        "receive",
        "received",
        "in",
        "inward",
        "pi",
        "primary",
        "primary purchase",
        "receipt pur",
        "receipt/pur",
        "receive quantity",
        "purchase qty",
        "purchased",
        "purchased qty",
    ],
    "sales": [
        "sale",
        "sales",
        "sl",
        "issue",
        "iss",
        "issues",
        "out",
        "outward",
        "sold",
        "issue quantity",
        "issue/sales",
        "sales qty",
        "sale qty",
    ],
    "sales_return": [
        "saleret",
        "saleref",  # common OCR of SaleRet
        "sale ret",
        "sale ref",
        "s ret",
        "sret",
        "sr",
        "sales return",
        "sale return",
        "goods ret",
        "goods return",
        "goods ret qty",
        "goods return qty",
        "cn",
        "return",
        "retrn",
    ],
    "purchase_return": [
        "pret",
        "pur ret",
        "purc ret",
        "purc. ret",
        "purret",
        "pu ret",
        "pu.ret",
        "puret",
        "purchase return",
        "purchase ret",
        "purc ret qty",
        "dn",
    ],
    "expiry_damage": [
        "exp",
        "expiry",
        "dmg",
        "damage",
        "exp dmg",
        "exp/dmg",
        "brk",
        "breakage",
        "shortage",
        "expdmg",
        "exp damage",
        "expiry damage",
    ],
    "closing": [
        "cls",
        "clos",
        "closing",
        "clos stock",
        "closstock",
        "bal",
        "balance",
        "balance qty",
        "bal qty",
        "clstk",
        "cl stk",
        "cistk",  # OCR of Cl.Stk → CI.Stk
        "ci stk",
        "cb",
        "closing stock",
        "closing qty",
        "cl qty",
        "clq",
        "stock",  # Medica STOCK when paired with STK VAL; disambiguated below
        "bal.",
        "clstock",
        "cl.stock",
    ],
    "free": [
        "free",
        "scheme",
        "sch",
        "bonus",
        "sp",
        "ss",
        "p sch",
        "s sch",
        "psch",
        "ssch",
        "receipt free",
        "rec free",
        "free replace",
        "free repl",
        "free receipt",
    ],
    "sample": [
        "sample",
        "smpl",
    ],
    "total": [
        "total",
        "tot",
        "total qty",
        "total in",
        "total in qty",
        "tot in",
        "tot in qty",
        "in qty",
        "op+rec",
        "op + rec",
        "op rec",
    ],
    "order": [
        "order",
        "ord qty",
        "order qty",
        "orderqty",
        "ordqty",
    ],
    "value_marker": [
        "value",
        "val",
        "amt",
        "amount",
        "rs",
        "rupees",
        "₹",
        "stk val",
        "stkval",
        "stock value",
        "stock-value",
        "cls amt",
        "clsamt",
        "closing value",
        "closing val",
        "cl val",
        "clval",
        "bval",
        "bal val",
        "sale val",
        "sales value",
        "sale value",
        "sval",
        "sa val",
        "pur val",
        "purchase value",
        "opening value",
        "op val",
        "opval",
        "receive value",
        "issue value",
    ],
}

_GROUP_TO_QTY = {
    "opening": "opening_qty",
    "purchase": "purchase_qty",
    "sales": "sales_qty",
    "sales_return": "sales_return_qty",
    "purchase_return": "purchase_return_qty",
    "expiry_damage": "expiry_damage_qty",
    "closing": "closing_qty",
    "total": "total_qty",
    "order": "order_qty",
    "free": "free",  # resolved later
    "sample": "free_out_qty",
}

_GROUP_TO_VALUE = {
    "opening": "opening_value",
    "purchase": "purchase_value",
    "sales": "sales_value",
    "closing": "closing_value",
}

_VALUE_MARKERS = frozenset(ALIASES["value_marker"])
_RATE_ALIASES = frozenset(ALIASES["rate"])


def normalize_header(text: Any) -> str:
    """Lowercase; replace . _ / - + with space; collapse whitespace; strip."""
    s = str(text or "").lower()
    s = s.replace("₹", " ₹ ")
    # Grid/OCR chrome often prefixes headers (|, —, ~, §).
    s = re.sub(r"^[\s\|§~•·\-–—_/=]+", "", s)
    s = re.sub(r"[\s\|§~•·\-–—_/=]+$", "", s)
    s = re.sub(r"[._/\-+–—]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokens(norm: str) -> List[str]:
    return [t for t in re.split(r"\s+", norm) if t]


def _alias_hits(norm: str) -> List[Tuple[str, str, float]]:
    """Return (group, matched_alias, confidence) sorted by alias length desc."""
    hits: List[Tuple[str, str, float]] = []
    tokens = set(_tokens(norm))
    compact = re.sub(r"\s+", "", norm)
    for group, aliases in ALIASES.items():
        if group == "value_marker":
            continue
        for alias in aliases:
            a_norm = normalize_header(alias)
            if not a_norm:
                continue
            conf = 0.95
            if len(a_norm) <= 3 or a_norm in _SHORT_TOKEN_ALIASES:
                # Whole-token only for short aliases.
                if a_norm not in tokens and a_norm != norm:
                    continue
                conf = 0.85
            else:
                a_compact = re.sub(r"\s+", "", a_norm)
                if a_norm == norm or a_compact == compact:
                    conf = 1.0
                elif a_norm in tokens:
                    conf = 0.9
                elif re.search(
                    rf"(?:^|\s){re.escape(a_norm)}(?:\s|$)", norm
                ):
                    conf = 0.9
                elif a_compact and a_compact in compact and len(a_compact) >= 4:
                    conf = 0.75
                else:
                    continue
            hits.append((group, a_norm, conf))
    hits.sort(key=lambda h: (-len(h[1]), -h[2]))
    return hits


def _has_value_marker(norm: str) -> bool:
    tokens = _tokens(norm)
    compact = re.sub(r"\s+", "", norm)
    for marker in _VALUE_MARKERS:
        m = normalize_header(marker)
        if len(m) <= 3:
            if m in tokens:
                return True
        else:
            if m in norm or re.sub(r"\s+", "", m) in compact:
                return True
    return False


def _is_rate(norm: str) -> bool:
    tokens = _tokens(norm)
    for alias in _RATE_ALIASES:
        a = normalize_header(alias)
        if len(a) <= 3:
            if a in tokens or a == norm:
                return True
        elif a in norm or a == norm:
            return True
    return False


_DIRECT_VALUE_FIELDS = {
    "bval": "closing_value",
    "bal val": "closing_value",
    "balval": "closing_value",
    "cls amt": "closing_value",
    "clsamt": "closing_value",
    "closing value": "closing_value",
    "closing val": "closing_value",
    "cl val": "closing_value",
    "clval": "closing_value",
    "ci val": "closing_value",  # OCR of Cl.Val → Ci.Val
    "cival": "closing_value",
    "stk val": "closing_value",
    "stkval": "closing_value",
    "stock value": "closing_value",
    "stock-value": "closing_value",
    "sval": "sales_value",
    "sale val": "sales_value",
    "sa val": "sales_value",
    "sales value": "sales_value",
    "sale value": "sales_value",
    "sales value": "sales_value",
    "op val": "opening_value",
    "opval": "opening_value",
    "opening value": "opening_value",
    "pur val": "purchase_value",
    "purchase value": "purchase_value",
    "receive value": "purchase_value",
    "issue value": "sales_value",
}


def _direct_value_field(norm: str) -> Optional[str]:
    if norm in _DIRECT_VALUE_FIELDS:
        return _DIRECT_VALUE_FIELDS[norm]
    compact = re.sub(r"\s+", "", norm)
    for key, field in _DIRECT_VALUE_FIELDS.items():
        if re.sub(r"\s+", "", key) == compact:
            return field
    return None


def _pick_group(norm: str) -> Tuple[Optional[str], float, str]:
    """Choose the best semantic group for a normalised header."""
    if not norm:
        return None, 0.0, "empty"

    direct = _direct_value_field(norm)
    if direct:
        return f"direct:{direct}", 0.95, f"direct_value:{direct}"

    if _is_rate(norm) and not _has_value_marker(norm):
        return "rate", 0.95, "rate_alias"

    # Prefer sales_return over sales when both could match ("sale ret").
    # Prefer purchase_return over purchase when "pur" + "ret".
    hits = _alias_hits(norm)
    if not hits:
        # Bare value markers handled by caller.
        if _has_value_marker(norm):
            return "value_marker", 0.8, "value_marker"
        return None, 0.0, "unknown"

    # Special: "return" alone is sales_return only when no pur token.
    tokens = _tokens(norm)
    filtered: List[Tuple[str, str, float]] = []
    for group, alias, conf in hits:
        if group == "sales_return" and alias == "return":
            if any(t.startswith("pur") for t in tokens) or "purchase" in norm:
                continue
        if group == "purchase" and ("return" in tokens or "ret" in tokens):
            # Let purchase_return win.
            continue
        if group == "sales" and (
            "ret" in tokens
            or "return" in tokens
            or "saleret" in re.sub(r"\s+", "", norm)
        ):
            continue
        # Short "op" must not beat a multi-token total alias like "op rec".
        if group == "opening" and alias in {"op", "opn"} and len(tokens) > 1:
            continue
        # "Receipt Free" is free-in, not purchase.
        if group == "purchase" and "free" in tokens:
            continue
        filtered.append((group, alias, conf))
    if not filtered:
        # Prefer unknown over restoring intentionally filtered purchase/sales
        # hits (e.g. "Purc. Ret" must not fall back to purchase_qty).
        non_movement = [
            h
            for h in hits
            if h[0]
            not in {
                "purchase",
                "sales",
                "opening",
                "closing",
                "total",
            }
        ]
        filtered = non_movement or hits

    # Priority when multiple groups: product/pack/batch/rate first, then
    # returns before sales/purchase, then movement groups. Prefer longer
    # aliases within the same priority band so "op rec" (total) beats "op".
    priority = {
        "product_name": 100,
        "pack": 95,
        "batch": 94,
        "rate": 93,
        "lms": 92,
        "order": 90,
        "sales_return": 85,
        "purchase_return": 84,
        "expiry_damage": 80,
        "sample": 75,
        "free": 70,
        "opening": 65,
        "purchase": 60,
        "sales": 55,
        "closing": 50,
        "total": 45,
        "value_marker": 10,
    }
    filtered.sort(key=lambda h: (-len(h[1]), -priority.get(h[0], 0), -h[2]))
    group, alias, conf = filtered[0]
    return group, conf, f"alias:{alias}"


def _cells_sorted(header_cells: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cells = [dict(c) for c in header_cells if isinstance(c, dict)]
    cells.sort(key=lambda c: int(c.get("col_index") or 0))
    return cells


def _qty_value_sequence_enabled() -> bool:
    return os.getenv("STOCK_HEADER_QTY_VALUE_SEQUENCE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


_QTY_VALUE_SEQUENCE_GROUPS: Tuple[Tuple[str, str, str], ...] = (
    ("opening", "opening_qty", "opening_value"),
    ("purchase", "purchase_qty", "purchase_value"),
    ("sales", "sales_qty", "sales_value"),
    ("closing", "closing_qty", "closing_value"),
)


def _is_bare_qty_header(norm: str) -> bool:
    compact = re.sub(r"\s+", "", (norm or "").rstrip("."))
    return compact in {"qty", "quantity", "qnty", "qty"}


def _is_bare_value_header(norm: str, col: Dict[str, Any]) -> bool:
    if col.get("canonical") == "value_marker":
        return True
    if col.get("reason") == "orphan_value":
        return True
    compact = re.sub(r"\s+", "", norm or "")
    return compact in {"value", "val", "amt", "amount", "rs"}


def _apply_bare_qty_value_sequence(
    prelim: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
) -> bool:
    """Map unlabeled QTY/VALUE pairs left-to-right onto opening→…→closing.

    Used when parent band headers (OPENING/RECEIPT/ISSUE) were dropped and every
    pair looks like bare QTY + VALUE. Does not invent values — only renames
    columns. Disabled unless STOCK_HEADER_QTY_VALUE_SEQUENCE is on.
    """
    if not _qty_value_sequence_enabled():
        return False

    pair_starts: List[int] = []
    i = 0
    while i < len(prelim) - 1:
        left = prelim[i]
        right = prelim[i + 1]
        left_norm = normalize_header(left.get("header_text"))
        right_norm = normalize_header(right.get("header_text"))
        left_canon = str(left.get("canonical") or "ignore")
        # Skip columns already tied to a named stock group.
        if left.get("group") in _GROUP_TO_QTY and left_canon in {
            "opening_qty",
            "purchase_qty",
            "sales_qty",
            "closing_qty",
            "opening_value",
            "purchase_value",
            "sales_value",
            "closing_value",
        }:
            i += 1
            continue
        if _is_bare_qty_header(left_norm) and _is_bare_value_header(right_norm, right):
            pair_starts.append(i)
            i += 2
            continue
        i += 1

    if len(pair_starts) < 3:
        return False

    labeled_closing = any(
        c.get("group") == "closing" or c.get("canonical") == "closing_qty"
        for c in prelim
    )
    # When CLOSING is already labeled separately, only claim the leading pairs.
    max_pairs = 3 if labeled_closing else 4
    assigned = 0
    for gi, pi in enumerate(pair_starts[:max_pairs]):
        if gi >= len(_QTY_VALUE_SEQUENCE_GROUPS):
            break
        gname, q_field, v_field = _QTY_VALUE_SEQUENCE_GROUPS[gi]
        if labeled_closing and gname == "closing":
            break
        prelim[pi]["canonical"] = q_field
        prelim[pi]["group"] = gname
        prelim[pi]["is_value"] = False
        prelim[pi]["confidence"] = 0.85
        prelim[pi]["reason"] = "qty_value_sequence"
        prelim[pi + 1]["canonical"] = v_field
        prelim[pi + 1]["group"] = gname
        prelim[pi + 1]["is_value"] = True
        prelim[pi + 1]["confidence"] = 0.85
        prelim[pi + 1]["reason"] = "qty_value_sequence"
        assigned += 1

    # CLOSING + empty/VALUE neighbour → closing_value when still unassigned.
    for idx, col in enumerate(prelim):
        if col.get("canonical") != "closing_qty":
            continue
        if idx + 1 >= len(prelim):
            break
        nxt = prelim[idx + 1]
        nxt_norm = normalize_header(nxt.get("header_text"))
        if nxt.get("canonical") in {"ignore", None, "value_marker"} and (
            not nxt_norm
            or _is_bare_value_header(nxt_norm, nxt)
            or nxt.get("reason") == "orphan_value"
        ):
            nxt["canonical"] = "closing_value"
            nxt["group"] = "closing"
            nxt["is_value"] = True
            nxt["confidence"] = 0.85
            nxt["reason"] = "qty_value_sequence_closing"

    if assigned:
        errors.append(
            {
                "code": "QTY_VALUE_SEQUENCE",
                "message": (
                    f"Assigned {assigned} bare QTY/VALUE pairs left-to-right "
                    "(opening/purchase/sales[/closing])"
                ),
                "pairs": assigned,
            }
        )
        return True
    return False


def resolve_columns(
    header_cells: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Map header cells onto CANONICAL_FIELDS.

    Each cell: {text, x_center|None, col_index, subheader_text|None}.
    """
    errors: List[Dict[str, Any]] = []
    cells = _cells_sorted(header_cells)

    # NON_MONOTONIC x_center check.
    last_x: Optional[float] = None
    last_idx: Optional[int] = None
    for cell in cells:
        x = cell.get("x_center")
        idx = int(cell.get("col_index") or 0)
        if x is not None and last_x is not None and last_idx is not None:
            if float(x) < float(last_x) and idx > last_idx:
                errors.append(
                    {
                        "code": "NON_MONOTONIC",
                        "message": f"x_center decreases at col_index={idx}",
                        "col_index": idx,
                    }
                )
                break
        if x is not None:
            last_x = float(x)
            last_idx = idx

    # First pass: group + qty/value from text/subheader.
    prelim: List[Dict[str, Any]] = []
    for cell in cells:
        text = str(cell.get("text") or "")
        sub = cell.get("subheader_text")
        sub_s = str(sub) if sub not in (None, "") else ""
        norm = normalize_header(text)
        sub_norm = normalize_header(sub_s)
        combined = normalize_header(f"{text} {sub_s}".strip())
        idx = int(cell.get("col_index") or 0)

        # Serial / row-number headers (Sr., S.No, #) — never sales_return "sr".
        # Bare mid-table "SR" is a qty column (Sale / Sale-Ret), not a row index.
        compact_hdr = re.sub(r"\s+", "", norm)
        raw_stripped = text.strip()
        is_serial_header = bool(
            re.match(
                r"^(?:#|s\.?\s*no\.?|sl\.?\s*no\.?|sr\.?\s*no\.?|s\.?\s*r\.?\s*no\.?|no\.?)$",
                raw_stripped,
                re.I,
            )
            or re.match(r"^s\.?r\.$", raw_stripped, re.I)  # "Sr." / "S.R."
        )
        if is_serial_header:
            prelim.append(
                {
                    "col_index": idx,
                    "header_text": text,
                    "subheader_text": sub_s or None,
                    "group": None,
                    "canonical": "ignore",
                    "confidence": 0.0,
                    "is_value": False,
                    "reason": "serial_number_header",
                }
            )
            continue

        # Medica IN/OT (or IN/OUT) is a dedicated transfer column — not purchase "in".
        if compact_hdr in {"inot", "inout"} or norm in {"in ot", "in out"}:
            prelim.append(
                {
                    "col_index": idx,
                    "header_text": text,
                    "subheader_text": sub_s or None,
                    "group": None,
                    "canonical": "ignore",
                    "confidence": 0.0,
                    "is_value": False,
                    "reason": "medica_in_ot",
                }
            )
            continue

        # M.EXP / Expiry / Expiry Date columns are dates, not expiry_damage qty.
        # Qty damage columns use Exp/Dmg, Exp Dmg, Breakage, Shortage, etc.
        if compact_hdr in {"mexp", "mexpiry", "shexp", "nearexpiry", "expirydate", "expdate", "expdt"} or norm in {
            "m exp",
            "sh exp",
            "near expiry",
            "expiry",
            "expiry date",
            "exp date",
            "exp dt",
        }:
            prelim.append(
                {
                    "col_index": idx,
                    "header_text": text,
                    "subheader_text": sub_s or None,
                    "group": None,
                    "canonical": "ignore",
                    "confidence": 0.0,
                    "is_value": False,
                    "reason": "date_or_near_expiry",
                }
            )
            continue

        group, conf, reason = _pick_group(combined if sub_norm else norm)
        if group is None and sub_norm:
            group, conf, reason = _pick_group(norm)

        is_value = False
        if group and group.startswith("direct:"):
            canonical = group.split(":", 1)[1]
            is_value = True
            group = {
                "opening_value": "opening",
                "purchase_value": "purchase",
                "sales_value": "sales",
                "closing_value": "closing",
            }.get(canonical)
        elif group == "rate":
            canonical = "rate"
            is_value = False
        elif group in {"product_name", "pack", "batch", "lms"}:
            canonical = group
        elif group == "value_marker":
            canonical = "value_marker"  # inherit later
            is_value = True
            conf = min(conf, 0.8)
        elif group == "sample":
            canonical = "free_out_qty"
        elif group == "order":
            canonical = "order_qty"
        elif group == "free":
            canonical = "free"  # neighbour pass
        elif group == "total":
            canonical = "total_qty"
        elif group in _GROUP_TO_QTY:
            # Qty vs value from markers on header or subheader.
            valueish = _has_value_marker(norm) or _has_value_marker(sub_norm)
            # Explicit qty subheader wins over bare group.
            if sub_norm in {"qty", "quantity", "qnty"}:
                valueish = False
            if sub_norm in {"value", "val", "amt", "amount"}:
                valueish = True
            if valueish and group in _GROUP_TO_VALUE:
                canonical = _GROUP_TO_VALUE[group]
                is_value = True
            else:
                canonical = _GROUP_TO_QTY[group]
                is_value = False
        else:
            canonical = "ignore"
            conf = 0.0
            reason = "unknown"

        # Medica IN/OT handled above.

        # Bare "stock" (Medica STOCK) → closing_qty.
        if (canonical == "ignore" or group == "closing") and norm == "stock":
            canonical = "closing_qty"
            group = "closing"
            conf = max(float(conf), 0.85)
            reason = "medica_stock"
        prelim.append(
            {
                "col_index": idx,
                "header_text": text,
                "subheader_text": sub_s or None,
                "group": group,
                "canonical": canonical,
                "confidence": float(conf),
                "is_value": is_value,
                "reason": reason,
            }
        )

    # Inherit bare Value columns from the qty column immediately to the left.
    for i, col in enumerate(prelim):
        if col["canonical"] != "value_marker":
            continue
        if i == 0:
            col["canonical"] = "ignore"
            col["reason"] = "orphan_value"
            col["confidence"] = 0.0
            continue
        left = prelim[i - 1]
        left_group = left.get("group")
        if left_group in _GROUP_TO_VALUE:
            col["canonical"] = _GROUP_TO_VALUE[left_group]
            col["group"] = left_group
            col["is_value"] = True
            col["confidence"] = 0.8
            col["reason"] = "inherit_left_qty"
        elif left["canonical"] in _GROUP_TO_VALUE.values():
            # Left already a value — still try group.
            for g, vfield in _GROUP_TO_VALUE.items():
                if left["canonical"] == _GROUP_TO_QTY.get(g) or left["canonical"] == vfield:
                    col["canonical"] = vfield
                    col["group"] = g
                    col["is_value"] = True
                    col["confidence"] = 0.8
                    col["reason"] = "inherit_left_qty"
                    break
            else:
                col["canonical"] = "ignore"
                col["reason"] = "orphan_value"
                col["confidence"] = 0.0
        else:
            # Amt right of Cls → closing_value even if left is closing_qty.
            if left_group == "closing" or left["canonical"] == "closing_qty":
                col["canonical"] = "closing_value"
                col["group"] = "closing"
                col["is_value"] = True
                col["confidence"] = 0.8
                col["reason"] = "inherit_left_qty"
            else:
                col["canonical"] = "ignore"
                col["reason"] = "orphan_value"
                col["confidence"] = 0.0

    # Free / scheme direction from neighbours.
    for i, col in enumerate(prelim):
        if col["canonical"] != "free" and col.get("group") != "free":
            continue
        left_group = prelim[i - 1].get("group") if i > 0 else None
        right_group = prelim[i + 1].get("group") if i + 1 < len(prelim) else None
        left_canon = prelim[i - 1]["canonical"] if i > 0 else None
        if left_group == "purchase" or left_canon in {
            "purchase_qty",
            "purchase_value",
            "free_in_qty",
        }:
            col["canonical"] = "free_in_qty"
            col["confidence"] = 0.6
            col["reason"] = "neighbour"
        elif left_group == "sales" or left_canon in {
            "sales_qty",
            "sales_value",
            "free_out_qty",
        }:
            col["canonical"] = "free_out_qty"
            col["confidence"] = 0.6
            col["reason"] = "neighbour"
        elif right_group == "purchase":
            col["canonical"] = "free_in_qty"
            col["confidence"] = 0.6
            col["reason"] = "neighbour"
        elif right_group == "sales":
            col["canonical"] = "free_out_qty"
            col["confidence"] = 0.6
            col["reason"] = "neighbour"
        else:
            col["canonical"] = "ignore"
            col["confidence"] = 0.0
            col["reason"] = "AMBIGUOUS_FREE"
            errors.append(
                {
                    "code": "AMBIGUOUS_FREE",
                    "message": f"Free/scheme column at col_index={col['col_index']} has no purchase/sales neighbour",
                    "col_index": col["col_index"],
                }
            )

    # Total kept when it sits between opening/purchase and sales/closing.
    groups_in_order = [c.get("group") for c in prelim]
    for i, col in enumerate(prelim):
        if col["canonical"] != "total_qty" and col.get("group") != "total":
            continue
        left_groups = {g for g in groups_in_order[:i] if g}
        right_groups = {g for g in groups_in_order[i + 1 :] if g}
        has_left = bool(left_groups & {"opening", "purchase"})
        has_right = bool(right_groups & {"sales", "closing"})
        if has_left and has_right:
            col["canonical"] = "total_qty"
            col["reason"] = col.get("reason") or "total_between"
        else:
            col["canonical"] = "ignore"
            col["confidence"] = 0.0
            col["reason"] = "total_not_between"

    # Bare "SR" between purchase and closing with no SALE column → sales_qty.
    # Stock grids often label the sale column SR when SALE is absent.
    has_sales_qty = any(c.get("canonical") == "sales_qty" for c in prelim)
    if not has_sales_qty:
        for i, col in enumerate(prelim):
            if col.get("canonical") != "sales_return_qty":
                continue
            hdr_norm = normalize_header(str(col.get("header_text") or ""))
            hdr_compact = re.sub(r"\s+", "", hdr_norm)
            if hdr_compact not in {"sr", "sret"} and hdr_norm not in {"s r", "s.r"}:
                continue
            left_groups = {c.get("group") for c in prelim[:i] if c.get("group")}
            right_groups = {c.get("group") for c in prelim[i + 1 :] if c.get("group")}
            if not (left_groups & {"opening", "purchase"} and right_groups & {"closing"}):
                continue
            col["canonical"] = "sales_qty"
            col["group"] = "sales"
            col["is_value"] = False
            col["confidence"] = max(float(col.get("confidence") or 0.0), 0.85)
            col["reason"] = "sr_as_sales_no_sale_col"

    # Bare QTY/VALUE pairs (parent OPENING/RECEIPT/ISSUE headers lost): assign
    # left-to-right opening → purchase → sales → closing. Geometry/order only.
    _apply_bare_qty_value_sequence(prelim, errors)

    # Deduplicate canonical fields: keep higher confidence.
    best: Dict[str, int] = {}
    for i, col in enumerate(prelim):
        canon = col["canonical"]
        if canon in {"ignore", None}:
            continue
        if canon not in best:
            best[canon] = i
            continue
        j = best[canon]
        if col["confidence"] > prelim[j]["confidence"]:
            prelim[j]["canonical"] = "ignore"
            prelim[j]["reason"] = "DUPLICATE_CANONICAL"
            errors.append(
                {
                    "code": "DUPLICATE_CANONICAL",
                    "message": f"Duplicate {canon} at col_index={prelim[j]['col_index']}; keeping col_index={col['col_index']}",
                    "col_index": prelim[j]["col_index"],
                    "canonical": canon,
                }
            )
            best[canon] = i
        else:
            col["canonical"] = "ignore"
            col["reason"] = "DUPLICATE_CANONICAL"
            errors.append(
                {
                    "code": "DUPLICATE_CANONICAL",
                    "message": f"Duplicate {canon} at col_index={col['col_index']}; keeping col_index={prelim[j]['col_index']}",
                    "col_index": col["col_index"],
                    "canonical": canon,
                }
            )

    columns = [
        {
            "col_index": c["col_index"],
            "header_text": c["header_text"],
            "canonical": c["canonical"] if c["canonical"] in CANONICAL_FIELDS else "ignore",
            "confidence": float(c["confidence"]),
            "is_value": bool(c["is_value"]),
            "reason": c["reason"],
        }
        for c in prelim
    ]

    resolved = {c["canonical"] for c in columns if c["canonical"] != "ignore"}
    # Usable when at least two of the four core qty columns are present.
    # closing_qty is optional (phone crops often omit it).
    _CORE_QTY = {"opening_qty", "purchase_qty", "sales_qty", "closing_qty"}
    if len(resolved & _CORE_QTY) < 2:
        errors.append(
            {
                "code": "MISSING_CORE_COLUMNS",
                "message": (
                    "Need at least two of opening_qty/purchase_qty/sales_qty/closing_qty"
                ),
                "resolved": sorted(resolved),
            }
        )

    return {"columns": columns, "errors": errors}


def assign_cells(
    row_tokens: List[Dict[str, Any]],
    columns: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Assign row tokens to resolved columns.

    Cell-index mode: tokens with col_index (or columns without geometry).
    Geometry mode: tokens with x; columns must carry x_center.
    """
    errors: List[Dict[str, Any]] = []
    fields: Dict[str, Optional[str]] = {
        c["canonical"]: None
        for c in columns
        if c.get("canonical") and c["canonical"] != "ignore"
    }

    # Attach x_center onto columns if present in the column dict.
    col_by_index = {int(c["col_index"]): c for c in columns}
    has_geometry = any(
        t.get("x") is not None for t in row_tokens if isinstance(t, dict)
    ) and any(c.get("x_center") is not None for c in columns)

    if not has_geometry:
        # Direct by col_index.
        for token in row_tokens:
            if not isinstance(token, dict):
                continue
            idx = token.get("col_index")
            if idx is None:
                continue
            col = col_by_index.get(int(idx))
            if not col or col.get("canonical") == "ignore":
                continue
            canon = col["canonical"]
            raw = token.get("text")
            raw_s = None if raw is None else str(raw)
            if fields.get(canon) is not None and raw_s not in (None, ""):
                fields[canon] = None
                errors.append(
                    {
                        "code": "CELL_COLLISION",
                        "message": f"Two tokens for {canon}",
                        "col_index": int(idx),
                    }
                )
            else:
                fields[canon] = raw_s
        return {"fields": fields, "errors": errors}

    # Geometry assignment.
    centers: List[Tuple[int, float, str]] = []
    for c in columns:
        if c.get("x_center") is None or c.get("canonical") == "ignore":
            continue
        centers.append((int(c["col_index"]), float(c["x_center"]), c["canonical"]))
    centers.sort(key=lambda t: t[1])
    if len(centers) < 2:
        pitch = 50.0
    else:
        gaps = [centers[i + 1][1] - centers[i][1] for i in range(len(centers) - 1)]
        pitch = float(median(gaps)) if gaps else 50.0
        if pitch <= 0:
            pitch = 50.0

    # Map col_index -> list of assigned token texts (detect collisions).
    bucket: Dict[int, List[str]] = {idx: [] for idx, _x, _c in centers}
    ambiguous_canons: set = set()

    for token in row_tokens:
        if not isinstance(token, dict) or token.get("x") is None:
            continue
        tx = float(token["x"])
        raw = str(token.get("text") or "")
        # Distances to each column centre.
        dists = [(abs(tx - x), idx, canon) for idx, x, canon in centers]
        dists.sort(key=lambda d: d[0])
        if not dists:
            errors.append({"code": "UNASSIGNED_TOKEN", "message": raw, "x": tx})
            continue
        best_dist, best_idx, best_canon = dists[0]
        if best_dist > 0.65 * pitch:
            errors.append(
                {
                    "code": "UNASSIGNED_TOKEN",
                    "message": raw,
                    "x": tx,
                    "nearest_col": best_idx,
                }
            )
            continue
        if len(dists) >= 2:
            second = dists[1][0]
            if abs(best_dist - second) < 0.10 * pitch:
                errors.append(
                    {
                        "code": "AMBIGUOUS_CELL",
                        "message": raw,
                        "x": tx,
                        "cols": [dists[0][1], dists[1][1]],
                    }
                )
                ambiguous_canons.add(dists[0][2])
                ambiguous_canons.add(dists[1][2])
                fields[dists[0][2]] = None
                fields[dists[1][2]] = None
                continue
        bucket[best_idx].append(raw)

    for idx, _x, canon in centers:
        if canon in ambiguous_canons:
            fields[canon] = None
            continue
        texts = bucket.get(idx) or []
        if len(texts) == 0:
            fields.setdefault(canon, None)
        elif len(texts) == 1:
            fields[canon] = texts[0]
        else:
            fields[canon] = None
            errors.append(
                {
                    "code": "CELL_COLLISION",
                    "message": f"Two tokens in column {idx} ({canon})",
                    "col_index": idx,
                }
            )

    return {"fields": fields, "errors": errors}


def parse_number(raw: Any, *, allow_internal_spaces: bool = False) -> Optional[float]:
    """Parse a printed quantity/amount into float|None.

    Handles Indian grouping, accounting negatives, dashes/nil as None,
    and 0 / 0.00 as 0.0. Internal spaces only when allow_internal_spaces.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return None
    lower = text.lower()
    if lower in {"-", "--", "—", "nil", "n/a", "na", "none", "null", "."}:
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
    elif text.endswith("-") and not text.startswith("-"):
        negative = True
        text = text[:-1].strip()

    if allow_internal_spaces:
        text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)

    # Strip currency markers.
    text = text.replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    text = text.replace(",", "")
    if not text:
        return None
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if negative:
        value = -abs(value)
    return value
