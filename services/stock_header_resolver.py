"""Shared header-to-canonical-field resolver (Phase 2a).

Pure module: no I/O, not wired into existing readers yet.
Alias tables are copied/merged from existing parsers; those tables are untouched.
"""

from __future__ import annotations

import logging
import os
import re
from statistics import median
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

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
    "other_outward_qty",
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
        "particular",
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
        "strength",  # Group Wise Sales Strength column
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
        "purchase cost",
        "stock cost",
    ],
    # Last-month-sale (LMS) is its own column — never opening/receipts.
    "lms": [
        "lms",
        "lm sale",
        "lm sales",
        "last month sale",
        "last month sales",
        "last month sale qty",
        "lstsl",
        "lst sl",
        "lst sale",
        "last sl",
        # STOCK & SALES STATEMENT: AVG / LST MN / FOR MN under SALES (history).
        "lst mn",
        "lstmn",
        "for mn",
        "formn",
        "avg",
        "avg.",
        # Printed "Prev Sale" / "Previous Sale" is last-month sale, not this period.
        "prev sale",
        "prev sales",
        "prev. sale",
        "previous sale",
        "previous sales",
    ],
    # Descriptive / commercial columns that can sit before Opening in stock
    # statements. They must never claim movement qty semantics.
    "non_stock_qty": [
        "nrv",
        "net realisable value",
        "net realizable value",
        "age",
        "pro age",
        "product age",
        "days age",
        "stock age",
        "margin",
        "margin percent",
        "margin %",
        "tax",
        "gst",
        "hsn",
        "dump",
        "dump qty",
    ],
    "opening": [
        "op",
        "opn",
        "opg",
        "open",  # LstSL / Open / Recd / Sales / Close sheets
        "opening",
        "opstk",
        "op stk",
        "ostk",
        "o stk",  # O.Stk / O Stk (Profitmaker Stock & Sales)
        "ob",
        "opbal",
        "op bal",
        # OpenBalQty — must beat closing "bal qty" infix inside openbalqty.
        "openbalqty",
        "open bal qty",
        "opening bal qty",
        "openbal qty",
        "opening stock",
        # Vision/OCR often truncates the final letter(s) of "Opening stock".
        "opening stoc",
        # MR Report: OPENING STK QTY (two-line header).
        "opening stk qty",
        "opening stk",
        "opening stock qty",
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
        # OCR cut off the end of RECEIPT (RECEI / RECEIP).
        "recei",
        "receip",
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
        "receive quant",  # wrapped RECEIVE QUANT(ITY)
        "purchase qty",
        "purchased",
        "purchased qty",
        "rcvd",
        "rcvd qty",
        "purchbillqty",
        "purch bill qty",
        "purchase bill qty",
    ],
    "sales": [
        "sale",
        "sales",
        "sl",
        "issue",
        "iss",
        "issu",  # OCR truncate of ISSUE
        "issur",
        "isue",
        "issues",
        "out",
        "outward",
        "sold",
        "issue quantity",
        "issue quant",  # wrapped ISSUE QUANT(ITY)
        "issue/sales",
        "sales qty",
        "sale qty",
        "sales bill qty",
        "sale bill qty",
        "salesbillqty",
    ],
    "sales_return": [
        "saleret",
        "saleref",  # common OCR of SaleRet
        "sale ret",
        "sale ref",
        "s ret",
        "s return",
        "sreturn",
        "sret",
        "sr",
        "sales return",
        "sale return",
        "sale rtn",
        "free retn",
        "free/retn",
        "retn",
        "goods ret",
        "goods return",
        "goods ret qty",
        "goods return qty",
        "cn",
        "return",
        "retrn",
        "sale ret.",
    ],
    "purchase_return": [
        "pret",
        "p return",
        "preturn",
        "p r",
        "p r qty",
        "p r quantity",
        "pr qty",
        "pr quantity",
        "pur ret",
        "purc ret",
        "purc. ret",
        "purret",
        "pu ret",
        "pu.ret",
        "puret",
        "purchase return",
        "purchase ret",
        "purchase rtn",
        "prch rtn",
        "prch ret",
        "pur rtn",
        "purchase rtn qty",
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
        "expired",
        "expired qty",
        # STOCK & SALES STATEMENT near-expiry band under STOCK.
        "nr exp",
        "nr. exp",
        "nr.exp",
        "nrexp",
        "near exp",
        "near expiry",
        # Breakage / expiry / purchase-return combined movement column.
        "b/e/pr",
        "bepr",
        "b e pr",
    ],
    "closing": [
        "cls",
        "clos",
        "close",  # LstSL Close column (exact; "clos" alone also hits via compact)
        "closin",  # OCR truncate of CLOSING
        "closi",
        "clacing",  # OCR of Closing (c/o swap)
        "clsoing",
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
        "closing stoc",  # wrapped CLOSING STOC(K)
        "closing qty",
        "closing balance",
        "closing bal",
        # CloseBalQty — distinct from OpenBalQty / ClosingBalAmt.
        "closebalqty",
        "close bal qty",
        "closing bal qty",
        "closebal qty",
        "cl bal",
        "cl balance",
        "cl qty",
        "cl qnt",
        "cl qnty",
        "cl quantity",
        "clq",
        "qoh",  # Qty on hand (Profitmaker / Daxinsoft)
        "qo h",
        "qty on hand",
        "quantity on hand",
        "stock",  # Medica STOCK when paired with STK VAL; disambiguated below
        "bal.",
        "clstock",
        "cl.stock",
        # STOCK & SALES STATEMENT: EXCESS under STOCK = closing on hand.
        "excess",
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
        # MR Report / Stock and Sales: PUR FREE QTY | SALE FREE QTY.
        "pur free",
        "pur free qty",
        "purchase free",
        "purchase free qty",
        "sale free",
        "sale free qty",
        "sales free",
        "sales free qty",
    ],
    "sample": [
        "sample",
        "smpl",
    ],
    # Signed adjustment between sales and closing (outward when negative).
    "others": [
        "others",
        "others qty",
        "other qty",
        "repl other",
        "repl/other",
        "repl / other",
        "replace",
        "replace others",
        "replace+",
    ],
    "total": [
        "total",
        "tot",
        "total qty",
        "total stock",
        "total stk",
        "tot stock",
        "tot stk",
        "total in",
        "total in qty",
        "tot in",
        "tot in qty",
        "in qty",
        "total receipt",
        "total receipts",
        "total receive",
        "total received",
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
        "reorder",
        "re order",
        "re-order",
        "re-order qty",
        "reorder qty",
        "re order qty",
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
        "amount-i",
        "amount i",
    ],
}

_GROUP_TO_QTY = {
    "opening": "opening_qty",
    "purchase": "purchase_qty",
    "sales": "sales_qty",
    "sales_return": "sales_return_qty",
    "purchase_return": "purchase_return_qty",
    "expiry_damage": "expiry_damage_qty",
    "others": "other_outward_qty",
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
        if group == "non_stock_qty" and not _ignore_non_qty_headers_enabled():
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
        m_compact = re.sub(r"\s+", "", m)
        if len(m) <= 3:
            # Token match, or camelCase / glued BillAmt-style suffix.
            # Only amt/rs for endswith — bare "val" would false-hit *val words.
            if m in tokens:
                return True
            # "ClsAmt" / "Stk Rs" suffix only. "others" ends in "rs" and is not money.
            if m_compact in {"amt", "rs"} and compact.endswith(m_compact):
                if compact == m_compact:
                    return True
                prev = compact[-len(m_compact) - 1]
                if not prev.isalpha():
                    return True
        else:
            if m in norm or m_compact in compact:
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
    "closing bal amt": "closing_value",
    "closingbalamt": "closing_value",
    "close bal amt": "closing_value",
    "closebalamt": "closing_value",
    # OCR of ClosingBalAmt → "ClosingBal tQty" / "ClosingBal aqty".
    "closingbal tqty": "closing_value",
    "closing baltqty": "closing_value",
    "closingbal aqty": "closing_value",
    # MR Report: CLOSING VALUE (PUR RATE) — money, not a rate column.
    "closing value (pur rate)": "closing_value",
    "closing value pur rate": "closing_value",
    "closing value (purchase rate)": "closing_value",
    "stk val": "closing_value",
    "stkval": "closing_value",
    "stk value": "closing_value",
    "stk. value": "closing_value",
    "stock value": "closing_value",
    "stock-value": "closing_value",
    "sval": "sales_value",
    "sale val": "sales_value",
    "sa val": "sales_value",
    "sales value": "sales_value",
    "sale value": "sales_value",
    "sales bill amt": "sales_value",
    "sale bill amt": "sales_value",
    "salesbillamt": "sales_value",
    "op val": "opening_value",
    "opval": "opening_value",
    "open value": "opening_value",
    "open. value": "opening_value",
    "opening value": "opening_value",
    "pur val": "purchase_value",
    "purchase value": "purchase_value",
    "purch bill amt": "purchase_value",
    "purchbillamt": "purchase_value",
    "purchase bill amt": "purchase_value",
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

    compact_early = re.sub(r"\s+", "", norm)
    # ITEMCODE is a catalogue key, never product_name (alias "item") or value.
    if compact_early in {"itemcode", "itemcd"} or norm == "item code":
        return "non_stock_qty", 0.95, "item_code"

    direct = _direct_value_field(norm)
    if direct:
        return f"direct:{direct}", 0.95, f"direct_value:{direct}"

    if _is_rate(norm) and not _has_value_marker(norm):
        return "rate", 0.95, "rate_alias"

    # "Cl Panaji" / "Cl Mapusa" are branch closing splits, not statement closing.
    # Keep real closing aliases (Cl Qty / Cl.Qnt / Cl Stock / Cl Val).
    cl_tokens = _tokens(norm)
    if (
        len(cl_tokens) == 2
        and cl_tokens[0] == "cl"
        and cl_tokens[1]
        not in {
            "qty",
            "qnt",
            "qnty",
            "quantity",
            "qoh",
            "stock",
            "stk",
            "bal",
            "balance",
            "val",
            "value",
            "amt",
            "q",
        }
    ):
        return "non_stock_qty", 0.9, "branch_split_header"

    # Vision sometimes glues Sales+Close into one header; keep sales_qty and
    # recover Close from the following Order column in the Vision table path.
    if norm in {"sales close", "sale close", "sales/close", "sale/close"}:
        return "sales", 0.75, "sales_close_merged"

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
    compact_norm = re.sub(r"\s+", "", norm)
    for group, alias, conf in hits:
        if group == "sales_return" and alias == "return":
            if any(t.startswith("pur") for t in tokens) or "purchase" in norm:
                continue
            # P.RETURN is purchase return; bare "return" must not claim it.
            if "p" in tokens:
                continue
        if group == "purchase" and (
            "return" in tokens
            or "ret" in tokens
            or "rtn" in tokens
            or compact_norm.endswith("rtn")
        ):
            # Let purchase_return win.
            continue
        if group == "sales" and (
            "ret" in tokens
            or "return" in tokens
            or "rtn" in tokens
            or compact_norm.endswith("rtn")
            or "salertn" in compact_norm
        ):
            continue
        # "Total Sold" / "Total Rcvd" is a movement subtotal, not stock total.
        # "Total Receipt" is opening + purchase (available stock), not a second purchase.
        if group == "total" and any(
            k in compact_norm for k in ("sold", "sale", "rcvd", "receipt", "recv")
        ):
            if norm not in {
                "total receipt",
                "total receipts",
                "total receive",
                "total received",
            }:
                continue
        # Short "op" must not beat a multi-token total alias like "op rec".
        # Keep Op.Value / Op.Qnt / Op.Value Expiry (qty-value or glued date label).
        if group == "opening" and alias in {"op", "opn"} and len(tokens) > 1:
            rest = set(tokens) - {"op", "opn"}
            _op_keep = {
                "value",
                "val",
                "amt",
                "amount",
                "qty",
                "qnt",
                "qnty",
                "quantity",
                "stk",
                "stock",
                "bal",
                "expiry",
                "exp",
                "date",
            }
            if not rest or not rest <= _op_keep:
                continue
        # "Op.Value Expiry" / "Pur.Value Expiry" — date label glued onto movement,
        # not an Exp/Dmg quantity column.
        if group == "expiry_damage" and alias in {"expiry", "exp"} and len(tokens) > 1:
            if any(
                t
                in {
                    "op",
                    "opening",
                    "opn",
                    "pur",
                    "purc",
                    "purch",
                    "purchase",
                    "sl",
                    "sale",
                    "sales",
                    "cl",
                    "cls",
                    "closing",
                }
                for t in tokens
            ):
                continue
        # OpenBalQty must not match closing via infix "bal qty" inside openbalqty.
        if group == "closing" and alias in {
            "bal qty",
            "bal",
            "balance",
            "balance qty",
            "bal.",
        }:
            if (
                compact_norm.startswith("open")
                or "openbal" in compact_norm
                or "opening" in compact_norm
            ):
                continue
        # "Receipt Free" / "Purchase Free" is free-in, not purchase.
        if group == "purchase" and "free" in tokens:
            continue
        # "Sales Free" is free-out, not sales qty.
        if group == "sales" and "free" in tokens:
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


def _adjacent_qty_value_pair_enabled() -> bool:
    """Op.+Opening Bal / Receipt+Receipt/Pur / Closing+Closing Bala → qty|value.

    Default ON. The right column of a same-group qty pair is that group's
    value, not a DUPLICATE_CANONICAL ignore. Qty-only Receipt/Pur (no qty
    column to the left) stays purchase_qty.
    """
    return os.getenv(
        "STOCK_HEADER_ADJACENT_QTY_VALUE_PAIR", "true"
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _promote_adjacent_same_group_qty_to_value(
    prelim: List[Dict[str, Any]],
) -> bool:
    """When two consecutive columns share a movement group and both are qty.

    Sales & Stock: Receipt | Receipt/Pur, Closing | Closing Bala, Op. | Opening Bal.
    Promotes the right column to the group's value field. Never invents digits.
    """
    if not _adjacent_qty_value_pair_enabled() or len(prelim) < 2:
        return False
    changed = False
    for i in range(1, len(prelim)):
        left = prelim[i - 1]
        right = prelim[i]
        if not isinstance(left, dict) or not isinstance(right, dict):
            continue
        group = left.get("group")
        if not group or group != right.get("group") or group not in _GROUP_TO_VALUE:
            continue
        qty_field = _GROUP_TO_QTY.get(group)
        val_field = _GROUP_TO_VALUE.get(group)
        if not qty_field or not val_field:
            continue
        if left.get("canonical") != qty_field:
            continue
        if right.get("canonical") != qty_field:
            continue
        if left.get("is_value") or right.get("is_value"):
            continue
        raw_left = str(left.get("header_text") or "")
        raw_right = str(right.get("header_text") or "")
        norm_left = normalize_header(raw_left)
        norm_right = normalize_header(raw_right)
        # A header that already says Qty stays a quantity. Issue/Sales Qty
        # next to Issue Qty is a second qty, not the value column.
        if re.search(r"(?i)\b(?:qty|quantity|qnty)\b", norm_right):
            continue
        # Bare repeated Sales/Purchase (Group Wise) stay qty. Only pair
        # headers (Receipt/Pur, Opening Bal, Closing Bala, Issue/Sales).
        # Exception: identical bare OPENING|OPENING or PURCHASE|PURCHASE (MR
        # Report two-line headers flattened to parent band only) → qty|value.
        bare_same_band = (
            group in {"opening", "purchase", "closing"}
            and norm_left == norm_right
            and norm_left
            in {
                "opening",
                "open",
                "op",
                "opn",
                "purchase",
                "purchases",
                "pur",
                "purc",
                "purch",
                "closing",
                "close",
                "clos",
                "cls",
            }
        )
        if (
            not bare_same_band
            and "/" not in raw_right
            and not re.search(r"(?i)\bbal", norm_right)
            and not _has_value_marker(norm_right)
        ):
            continue
        right["canonical"] = val_field
        right["is_value"] = True
        right["confidence"] = max(float(right.get("confidence") or 0.0), 0.9)
        right["reason"] = "adjacent_qty_value_pair"
        logger.info(
            "STOCK_HEADER_ADJACENT_QTY_VALUE_PAIR col=%s %r -> %s left=%s",
            right.get("col_index"),
            right.get("header_text"),
            val_field,
            left.get("header_text"),
        )
        changed = True
    return changed


def _qf_qty_sequence_enabled() -> bool:
    """STOCK_HEADER_QF_SEQUENCE — map repeated (Q+F) headers Opn→Rec→Issue→Bal."""
    return os.getenv("STOCK_HEADER_QF_SEQUENCE", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _column_bands_enabled() -> bool:
    """STOCK_HEADER_COLUMN_BANDS — assign by header midpoint bands (zigzag-safe).

    Default ON so deploy needs no env. Set false to restore nearest-centre assign.
    """
    return os.getenv("STOCK_HEADER_COLUMN_BANDS", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _ignore_non_qty_headers_enabled() -> bool:
    """STOCK_HEADER_IGNORE_NON_QTY — keep NRV/Age/etc. out of qty fields."""
    return os.getenv("STOCK_HEADER_IGNORE_NON_QTY", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _absorb_ignore_geometry_enabled() -> bool:
    """STOCK_HEADER_ABSORB_IGNORE_GEOMETRY — keep ignore columns in x-bands.

    When OFF (default), ignore columns (NRV/Age/unrecognized Rate OCR, etc.)
    are dropped from geometry centres and their tokens spill into neighbouring
    qty bands — often opening_qty. When ON, those columns absorb tokens so
    rate/NRV never become opening/closing qty.
    """
    return os.getenv("STOCK_HEADER_ABSORB_IGNORE_GEOMETRY", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _medica_in_ot_as_free_in_enabled() -> bool:
    """STOCK_MEDICA_IN_OT_AS_FREE_IN — Medica printed IN/OT is free_in_qty. Default OFF."""
    return os.getenv("STOCK_MEDICA_IN_OT_AS_FREE_IN", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _multi_stock_enabled() -> bool:
    """STOCK_HEADER_MULTI_STOCK — leftmost bare STOCK=opening, rightmost=closing.

    Vision often flattens OPENING STOCK / CLOSING STOCK into repeated bare
    STOCK headers; without this, medica_stock maps the first to closing and
    the real closing is dropped as DUPLICATE_CANONICAL. Default OFF.
    """
    return os.getenv("STOCK_HEADER_MULTI_STOCK", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _group_wise_sales_sequence_enabled() -> bool:
    """STOCK_HEADER_GROUP_WISE_SEQUENCE — Op/Purchase/Free/Ret/Sales/Cl.Stock map.

    Group Wise Sales grids flatten multi-line headers into repeated bare
    Purchase/Sales labels; keeping the first Sales as sales_qty drops the
    real Sales Qty column. Default OFF.
    """
    return os.getenv("STOCK_HEADER_GROUP_WISE_SEQUENCE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _lstsl_close_after_sales_enabled() -> bool:
    """STOCK_HEADER_LSTSL_CLOSE_AFTER_SALES — default ON.

    Vision often misreads printed Close as Open. / Open (footer Open. Value
    bleed) right after Sales on LstSL|Open|Recd|Sales|Close sheets.
    """
    return os.getenv(
        "STOCK_HEADER_LSTSL_CLOSE_AFTER_SALES", "true"
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _looks_like_lstsl_open_recd_headers(prelim: List[Dict[str, Any]]) -> bool:
    blob = " ".join(
        normalize_header(c.get("header_text")) for c in prelim if isinstance(c, dict)
    )
    if not re.search(r"\blstsl\b|\blst\s*sl\b|\blms\b", blob):
        return False
    canons = {c.get("canonical") for c in prelim if isinstance(c, dict)}
    return {"opening_qty", "purchase_qty", "sales_qty"}.issubset(canons)


def _apply_lstsl_close_after_sales(
    prelim: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
) -> bool:
    """Map the column immediately after Sales → closing_qty on LstSL sheets.

    Only when Close is missing. Accepts duplicate Open / Open. / ignore / a
    wrongly valued Sales label — never invents a column index.
    """
    if not _lstsl_close_after_sales_enabled():
        return False
    if not _looks_like_lstsl_open_recd_headers(prelim):
        return False
    if any(c.get("canonical") == "closing_qty" for c in prelim):
        return False
    sales_i = next(
        (
            i
            for i, c in enumerate(prelim)
            if c.get("canonical") == "sales_qty" and not c.get("is_value")
        ),
        None,
    )
    if sales_i is None or sales_i + 1 >= len(prelim):
        return False
    nxt = prelim[sales_i + 1]
    nxt_norm = normalize_header(nxt.get("header_text"))
    nxt_canon = nxt.get("canonical")
    # Typical misreads of Close: second Open / Open. / bare Sales (Order) /
    # Value / already-ignored duplicate.
    open_like = bool(re.match(r"^open(?:ing)?\.?$", nxt_norm or ""))
    allow = (
        nxt_canon in {"ignore", "opening_qty", "sales_qty", "sales_value", None}
        or open_like
        or nxt_norm in {"value", "order", "ord"}
    )
    if not allow:
        return False
    nxt["canonical"] = "closing_qty"
    nxt["group"] = "closing"
    nxt["is_value"] = False
    nxt["confidence"] = max(float(nxt.get("confidence") or 0.0), 0.9)
    nxt["reason"] = "lstsl_close_after_sales"
    logger.info(
        "STOCK_HEADER_LSTSL_CLOSE_AFTER_SALES col=%s text=%r",
        nxt.get("col_index"),
        nxt.get("header_text"),
    )
    errors.append(
        {
            "code": "LSTSL_CLOSE_AFTER_SALES",
            "message": (
                f"Mapped col_index={nxt.get('col_index')} "
                f"({nxt.get('header_text')!r}) to closing_qty after Sales"
            ),
            "col_index": nxt.get("col_index"),
        }
    )
    return True


# Between Op.Stock and Cl.Stock on Group Wise Sales (qty columns only).
_GROUP_WISE_MID_CANONICALS: Tuple[str, ...] = (
    "purchase_qty",
    "free_in_qty",
    "purchase_return_qty",
    "ignore",  # Purchase Ret Free
    "sales_return_qty",
    "ignore",  # Sales Ret Free
    "sales_qty",
    "free_out_qty",
)


def _header_norm_tokens(text: Any) -> str:
    return normalize_header(text)


def _looks_like_group_wise_sales_headers(prelim: Sequence[Dict[str, Any]]) -> bool:
    """True for Op.Stock + Strength/Purchase/Free + repeated Sales + Cl.Stock."""
    norms = [_header_norm_tokens(c.get("header_text")) for c in prelim]
    if not norms:
        return False
    has_strength = any(n == "strength" for n in norms)
    has_op = any(
        n in {"op", "op.", "op stock", "op.stock", "opening", "opening stock", "opn"}
        or n.startswith("op ")
        or n.startswith("op.")
        for n in norms
    )
    has_cl = any(
        "cl.stock" in n
        or n in {"cl stock", "closing", "closing stock", "clstock", "cls"}
        or n.startswith("cl.stock")
        for n in norms
    )
    sales_like = sum(
        1
        for n in norms
        if n == "sales"
        or n.startswith("sales ")
        or n.startswith("sale ")
        or n in {"sale", "sales qty", "sales free", "sales ret", "sales ret free"}
    )
    purchase_like = sum(
        1
        for n in norms
        if "purchase" in n or n in {"free", "pur", "purc"}
    )
    if not (has_op and has_cl and sales_like >= 3):
        return False
    return bool(has_strength or purchase_like >= 2)


def _apply_group_wise_sales_sequence(
    prelim: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
) -> bool:
    """Map Group Wise Op/Purchase/Free/Ret/Sales/Cl.Stock column order.

    Does not invent values — only renames columns so duplicate bare Sales /
    Purchase headers do not collapse onto the first hit.
    """
    if not _group_wise_sales_sequence_enabled():
        return False
    if not _looks_like_group_wise_sales_headers(prelim):
        return False

    op_i = next(
        (
            i
            for i, c in enumerate(prelim)
            if c.get("canonical") == "opening_qty"
            or (
                c.get("group") == "opening"
                and not c.get("is_value")
            )
        ),
        None,
    )
    cl_i = next(
        (
            i
            for i, c in enumerate(reversed(prelim))
            if c.get("canonical") == "closing_qty"
            or (
                c.get("group") == "closing"
                and not c.get("is_value")
            )
        ),
        None,
    )
    if cl_i is not None:
        cl_i = len(prelim) - 1 - cl_i
    if op_i is None or cl_i is None or cl_i <= op_i + 1:
        return False

    mid = list(range(op_i + 1, cl_i))
    if len(mid) < 6:
        return False
    # Use the rightmost len(_GROUP_WISE_MID_CANONICALS) mid columns when Vision
    # emits an extra Free/Purchase fragment before the classic block.
    roles = list(_GROUP_WISE_MID_CANONICALS)
    if len(mid) > len(roles):
        mid = mid[-len(roles) :]
    elif len(mid) < len(roles):
        roles = roles[: len(mid)]
        # Prefer keeping sales_qty + free_out at the end of a short mid span.
        if len(roles) >= 2:
            roles[-2] = "sales_qty"
            roles[-1] = "free_out_qty"

    for idx, canon in zip(mid, roles):
        col = prelim[idx]
        col["canonical"] = canon
        col["is_value"] = False
        col["confidence"] = 0.92
        col["reason"] = "group_wise_sales_sequence"
        if canon == "purchase_qty":
            col["group"] = "purchase"
        elif canon == "sales_qty":
            col["group"] = "sales"
        elif canon == "sales_return_qty":
            col["group"] = "sales_return"
        elif canon == "purchase_return_qty":
            col["group"] = "purchase_return"
        elif canon in {"free_in_qty", "free_out_qty"}:
            col["group"] = "free"
        else:
            col["group"] = None

    prelim[op_i]["canonical"] = "opening_qty"
    prelim[op_i]["group"] = "opening"
    prelim[op_i]["is_value"] = False
    prelim[op_i]["confidence"] = max(float(prelim[op_i].get("confidence") or 0.0), 0.92)
    prelim[op_i]["reason"] = "group_wise_sales_sequence"
    prelim[cl_i]["canonical"] = "closing_qty"
    prelim[cl_i]["group"] = "closing"
    prelim[cl_i]["is_value"] = False
    prelim[cl_i]["confidence"] = max(float(prelim[cl_i].get("confidence") or 0.0), 0.92)
    prelim[cl_i]["reason"] = "group_wise_sales_sequence"

    logger.info(
        "STOCK_HEADER_GROUP_WISE_SEQUENCE op_col=%s cl_col=%s mid=%s",
        prelim[op_i].get("col_index"),
        prelim[cl_i].get("col_index"),
        [(prelim[i].get("col_index"), roles[j]) for j, i in enumerate(mid)],
    )
    errors.append(
        {
            "code": "GROUP_WISE_SALES_SEQUENCE",
            "message": (
                f"Assigned Group Wise Sales mid columns op={prelim[op_i].get('col_index')} "
                f"cl={prelim[cl_i].get('col_index')}"
            ),
            "op_col": prelim[op_i].get("col_index"),
            "cl_col": prelim[cl_i].get("col_index"),
        }
    )
    return True


def _apply_multi_stock_sequence(
    prelim: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
) -> bool:
    """Map repeated bare STOCK headers: first→opening_qty, last→closing_qty.

    Also treats adjacent SALES + QTY as a section label + sales_qty (Vision
    splits multi-line 'SALES QTY' into two columns; values sit under QTY).
    """
    if not _multi_stock_enabled():
        return False

    stock_idxs: List[int] = []
    for i, col in enumerate(prelim):
        norm = normalize_header(col.get("header_text"))
        if norm != "stock":
            continue
        canon = str(col.get("canonical") or "ignore")
        reason = str(col.get("reason") or "")
        # Only remappable bare STOCK (medica_stock / closing / still unknown).
        if canon not in {"closing_qty", "opening_qty", "ignore"} and reason != "medica_stock":
            continue
        stock_idxs.append(i)

    applied = False
    if len(stock_idxs) >= 2:
        first_i = stock_idxs[0]
        last_i = stock_idxs[-1]
        prelim[first_i]["canonical"] = "opening_qty"
        prelim[first_i]["group"] = "opening"
        prelim[first_i]["is_value"] = False
        prelim[first_i]["confidence"] = max(
            float(prelim[first_i].get("confidence") or 0.0), 0.9
        )
        prelim[first_i]["reason"] = "multi_stock_opening"
        prelim[last_i]["canonical"] = "closing_qty"
        prelim[last_i]["group"] = "closing"
        prelim[last_i]["is_value"] = False
        prelim[last_i]["confidence"] = max(
            float(prelim[last_i].get("confidence") or 0.0), 0.9
        )
        prelim[last_i]["reason"] = "multi_stock_closing"
        for mid_i in stock_idxs[1:-1]:
            prelim[mid_i]["canonical"] = "ignore"
            prelim[mid_i]["group"] = None
            prelim[mid_i]["confidence"] = 0.0
            prelim[mid_i]["reason"] = "multi_stock_middle"
        logger.info(
            "STOCK_HEADER_MULTI_STOCK opening_col=%s closing_col=%s middle=%s",
            prelim[first_i]["col_index"],
            prelim[last_i]["col_index"],
            ",".join(str(prelim[i]["col_index"]) for i in stock_idxs[1:-1]) or "-",
        )
        errors.append(
            {
                "code": "MULTI_STOCK_SEQUENCE",
                "message": (
                    f"Bare STOCK columns: opening at col_index="
                    f"{prelim[first_i]['col_index']}, closing at "
                    f"col_index={prelim[last_i]['col_index']}"
                ),
                "opening_col": prelim[first_i]["col_index"],
                "closing_col": prelim[last_i]["col_index"],
            }
        )
        applied = True

    # SALES | QTY. → section label + sales_qty (values under QTY).
    _qty_norms = frozenset({"qty", "quantity", "qnty", "qty."})
    for i, col in enumerate(prelim):
        if i + 1 >= len(prelim):
            break
        left_norm = normalize_header(col.get("header_text"))
        right_norm = normalize_header(prelim[i + 1].get("header_text"))
        if left_norm not in {"sales", "sale"}:
            continue
        if right_norm not in _qty_norms:
            continue
        if str(col.get("canonical") or "") not in {"sales_qty", "sales_value", "ignore"}:
            continue
        right = prelim[i + 1]
        right_canon = str(right.get("canonical") or "ignore")
        if right_canon not in {"ignore", "sales_qty", "value_marker"}:
            continue
        col["canonical"] = "ignore"
        col["group"] = "sales"
        col["confidence"] = 0.0
        col["reason"] = "sales_section_label"
        right["canonical"] = "sales_qty"
        right["group"] = "sales"
        right["is_value"] = False
        right["confidence"] = max(float(right.get("confidence") or 0.0), 0.9)
        right["reason"] = "sales_qty_after_sales_label"
        logger.info(
            "STOCK_HEADER_MULTI_STOCK sales_qty_pair label_col=%s qty_col=%s",
            col.get("col_index"),
            right.get("col_index"),
        )
        applied = True

    return applied


def _glued_stock_header_prefers_qty(norm: str, group: str) -> bool:
    """Vision often glues 'Opening stock' + 'Value' into one qty-column header.

    Exact value phrases (opening value / op val) stay values. Headers that still
    carry a stock/qty noun with a glued Value token map to qty.
    """
    if not _ignore_non_qty_headers_enabled():
        return False
    if group not in {"opening", "closing"}:
        return False
    if _direct_value_field(norm):
        return False
    tokens = set(_tokens(norm))
    compact = re.sub(r"\s+", "", norm)
    if tokens & {"stock", "sto", "stoc", "stk", "qty", "quantity", "qnty"}:
        return True
    return any(tok in compact for tok in ("stock", "sto", "stoc", "stk"))


def _build_column_bands(
    centers: Sequence[Tuple[int, float, str]],
    pitch: float,
) -> List[Dict[str, Any]]:
    """Build [x0, x1] bands from sorted (col_index, x_center, canonical).

    Boundaries are midpoints between neighbouring centres. A small dead zone
    around each midpoint mirrors the old equidistant AMBIGUOUS_CELL behaviour.
    """
    ordered = sorted(centers, key=lambda t: t[1])
    n = len(ordered)
    if n == 0:
        return []
    half_dead = max(0.0, 0.05 * float(pitch or 50.0))
    bands: List[Dict[str, Any]] = []
    for i, (idx, xc, canon) in enumerate(ordered):
        if i == 0:
            left_mid = float("-inf")
        else:
            left_mid = (ordered[i - 1][1] + xc) / 2.0
        if i == n - 1:
            right_mid = float("inf")
        else:
            right_mid = (xc + ordered[i + 1][1]) / 2.0
        x0 = left_mid if left_mid == float("-inf") else left_mid + half_dead
        x1 = right_mid if right_mid == float("inf") else right_mid - half_dead
        if x1 < x0:
            # Degenerate pitch — fall back to a thin band around the centre.
            x0, x1 = xc - half_dead, xc + half_dead
        bands.append(
            {
                "col_index": int(idx),
                "canonical": canon,
                "x_center": float(xc),
                "x0": float(x0),
                "x1": float(x1),
                "left_mid": float(left_mid),
                "right_mid": float(right_mid),
            }
        )
    return bands


_QTY_VALUE_CANONS = frozenset(
    {
        "opening_qty",
        "opening_value",
        "purchase_qty",
        "purchase_value",
        "sales_qty",
        "sales_value",
        "closing_qty",
        "closing_value",
        "sales_return_qty",
        "purchase_return_qty",
        "expiry_damage_qty",
        "total_qty",
        "free_in_qty",
        "free_out_qty",
        "order_qty",
        "rate",
    }
)

_UNIT_PACK_RE = re.compile(
    r"^\(?[A-Z]?\d+(?:\.\d+)?\s*"
    r"(?:\*\s*\d+(?:\.\d+)?)?\s*"
    r"(?:TAB|TABS|ML|MG|CAP|SYP|INJ|DS|S|'S|GR|GM|GRM|G|PH|UNT)\)?$",
    re.I,
)


def _header_peel_pack_in_qty_enabled() -> bool:
    """Peel pack tokens that landed in a qty/value band (60'S / 30ML / HIM).

    Default ON. Same generic rule as Vision unlabeled-pack peel: a pack word
    under OPENING must not collide with the printed opening qty.
    """
    return os.getenv(
        "STOCK_HEADER_PEEL_PACK_IN_QTY", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _unit_pack_token(text: Any) -> bool:
    t = str(text or "").strip().rstrip(".,-_")
    if not t:
        return False
    if _UNIT_PACK_RE.fullmatch(t):
        return True
    return bool(
        re.fullmatch(
            r"\d+\s*\*\s*\d+(?:\.\d+)?\s*(?:ML|MG|GM|G|GRM|GR|TAB|TABS|CAP|UNT)?",
            t,
            re.I,
        )
    )


def _peel_pack_from_qty_texts(texts: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Split pack-like tokens out of a qty/value bucket."""
    packs: List[str] = []
    rest: List[str] = []
    for t in texts:
        if _unit_pack_token(t):
            packs.append(t)
        else:
            rest.append(t)
    if not packs and len(texts) >= 2:
        letter = [t for t in texts if re.fullmatch(r"[A-Za-z]{2,6}", str(t or "").strip())]
        numeric = [
            t
            for t in texts
            if re.fullmatch(r"-?\d+(?:[.,]\d+)?", str(t or "").strip().replace(",", ""))
        ]
        if letter and numeric and len(letter) + len(numeric) == len(texts):
            return letter, numeric
        # Truncated pack fragments (10M from 10ML, 100M from 100ML) are not
        # unit tokens and not letter-only. Keep printed numbers; peel the rest.
        numbers: List[str] = []
        others: List[str] = []
        for t in texts:
            if parse_number(t) is not None:
                numbers.append(t)
            else:
                others.append(t)
        if numbers and others:
            return others, numbers
    return packs, rest


def _stash_peeled_pack(fields: Dict[str, Optional[str]], packs: Sequence[str]) -> None:
    token = " ".join(p for p in packs if p).strip()
    if not token:
        return
    existing = str(fields.get("pack") or "").strip()
    fields["pack"] = f"{existing} {token}".strip() if existing else token
    logger.info("STOCK_HEADER_PEEL_PACK_IN_QTY pack=%r", token)


def _finalize_qty_bucket(
    texts: List[str],
    canon: str,
    fields: Dict[str, Optional[str]],
    errors: List[Dict[str, Any]],
    col_index: int,
) -> None:
    """Write one column bucket, peeling unlabeled pack tokens out of qty cells."""
    if not texts:
        fields.setdefault(canon, None)
        return
    if (
        _header_peel_pack_in_qty_enabled()
        and canon in _QTY_VALUE_CANONS
    ):
        packs, rest = _peel_pack_from_qty_texts(texts)
        if packs:
            _stash_peeled_pack(fields, packs)
            texts = rest
        if not texts:
            fields[canon] = None
            return
        if len(texts) == 1:
            fields[canon] = texts[0]
            return
    if len(texts) == 1:
        fields[canon] = texts[0]
        return
    if canon == "product_name":
        fields[canon] = " ".join(t for t in texts if t).strip() or None
        return
    fields[canon] = None
    errors.append(
        {
            "code": "CELL_COLLISION",
            "message": f"Two tokens in column {col_index} ({canon})",
            "col_index": col_index,
        }
    )


def _disambiguate_gap_token(
    raw: str,
    left_b: Dict[str, Any],
    right_b: Dict[str, Any],
    tx: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Pick a side when a token sits in the dead zone between two header bands.

    Pack-size digits belong with the product name. Right-aligned whole qtys
    sit past the closing/opening label, in the gap before RATE.
    """
    left_c = str(left_b.get("canonical") or "")
    right_c = str(right_b.get("canonical") or "")
    number = parse_number(raw)
    has_letter = bool(re.search(r"[A-Za-z]", raw or ""))
    if left_c == "product_name" and right_c in {"pack", "opening_qty"}:
        if re.fullmatch(r"(?i)pcs|nos|no", raw or ""):
            return right_b if right_c == "pack" else left_b
        return left_b
    qty_canons = {
        "opening_qty",
        "purchase_qty",
        "sales_qty",
        "closing_qty",
        "total_qty",
        "sales_return_qty",
        "purchase_return_qty",
        "other_outward_qty",
        "free_in_qty",
        "free_out_qty",
    }
    if left_c in qty_canons and right_c in qty_canons and tx is not None:
        ld = abs(float(tx) - float(left_b.get("x_center") or 0))
        rd = abs(float(tx) - float(right_b.get("x_center") or 0))
        return left_b if ld <= rd else right_b
    if left_c in qty_canons and right_c == "rate":
        if number is None or has_letter:
            return None
        if abs(number - round(number)) < 0.001:
            return left_b
        return right_b
    if left_c == "rate" and right_c in {"lms", "ignore"}:
        if number is None:
            return None
        if abs(number - round(number)) > 0.001:
            return left_b
        return right_b
    if left_c == "lms" and right_c in {"ignore", "expiry_damage_qty"}:
        return left_b if number is not None else None
    return None


def _assign_cells_by_column_bands(
    row_tokens: List[Dict[str, Any]],
    centers: Sequence[Tuple[int, float, str]],
    pitch: float,
    fields: Dict[str, Optional[str]],
) -> Dict[str, Any]:
    """Assign tokens by header x-bands (row tokens already Y-clustered by caller)."""
    errors: List[Dict[str, Any]] = []
    bands = _build_column_bands(centers, pitch)
    if not bands:
        return {"fields": fields, "errors": errors}

    bucket: Dict[int, List[str]] = {int(b["col_index"]): [] for b in bands}
    ambiguous_canons: set = set()
    assigned = 0
    unassigned = 0
    ambiguous = 0

    for token in row_tokens:
        if not isinstance(token, dict) or token.get("x") is None:
            continue
        tx = float(token["x"])
        raw = str(token.get("text") or "")
        hits = [b for b in bands if b["x0"] <= tx <= b["x1"]]
        if len(hits) == 1:
            bucket[int(hits[0]["col_index"])].append(raw)
            assigned += 1
            continue
        if len(hits) > 1:
            # Overlapping bands (should be rare) — treat as ambiguous.
            errors.append(
                {
                    "code": "AMBIGUOUS_CELL",
                    "message": raw,
                    "x": tx,
                    "cols": [int(b["col_index"]) for b in hits[:2]],
                }
            )
            for b in hits[:2]:
                canon = b["canonical"]
                if canon == "ignore":
                    continue
                ambiguous_canons.add(canon)
                fields[canon] = None
            ambiguous += 1
            continue
        # Outside every open band → midpoint dead zone or beyond table.
        # If between two centres, mark both neighbours ambiguous (zigzag boundary).
        ordered = sorted(bands, key=lambda b: b["x_center"])
        left_b = None
        right_b = None
        for b in ordered:
            if b["x_center"] <= tx:
                left_b = b
            elif right_b is None:
                right_b = b
                break
        if left_b is not None and right_b is not None:
            chosen = _disambiguate_gap_token(raw, left_b, right_b, tx)
            if chosen is not None:
                bucket[int(chosen["col_index"])].append(raw)
                assigned += 1
                continue
            errors.append(
                {
                    "code": "AMBIGUOUS_CELL",
                    "message": raw,
                    "x": tx,
                    "cols": [int(left_b["col_index"]), int(right_b["col_index"])],
                }
            )
            for b in (left_b, right_b):
                canon = b["canonical"]
                if canon == "ignore":
                    continue
                ambiguous_canons.add(canon)
                fields[canon] = None
            ambiguous += 1
        else:
            errors.append({"code": "UNASSIGNED_TOKEN", "message": raw, "x": tx})
            unassigned += 1

    for b in bands:
        idx = int(b["col_index"])
        canon = b["canonical"]
        # Absorber columns keep tokens out of qty bands; never write to fields.
        if canon == "ignore":
            continue
        if canon in ambiguous_canons:
            fields[canon] = None
            continue
        _finalize_qty_bucket(bucket.get(idx) or [], canon, fields, errors, idx)

    logger.info(
        "HEADER_COLUMN_BANDS mode=bands assigned=%s ambiguous=%s unassigned=%s "
        "cols=%s",
        assigned,
        ambiguous,
        unassigned,
        len(bands),
    )
    return {"fields": fields, "errors": errors}


_QTY_VALUE_SEQUENCE_GROUPS: Tuple[Tuple[str, str, str], ...] = (
    ("opening", "opening_qty", "opening_value"),
    ("purchase", "purchase_qty", "purchase_value"),
    ("sales", "sales_qty", "sales_value"),
    ("closing", "closing_qty", "closing_value"),
)

_QF_QTY_SEQUENCE_GROUPS: Tuple[Tuple[str, str], ...] = (
    ("opening", "opening_qty"),
    ("purchase", "purchase_qty"),
    ("sales", "sales_qty"),
    ("closing", "closing_qty"),
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

    header_blob = " ".join(
        normalize_header(c.get("header_text")) for c in prelim if isinstance(c, dict)
    )
    has_named_movement = any(
        isinstance(c, dict)
        and (
            c.get("group") in {"opening", "purchase", "sales", "closing"}
            or str(c.get("canonical") or "")
            in {
                "opening_qty",
                "purchase_qty",
                "sales_qty",
                "closing_qty",
            }
        )
        for c in prelim
    )
    has_opening_purchase_text = bool(
        re.search(
            r"\b(opening|op\s*bal|opbal|receipt|receipts|purchase|purchases|issue)\b",
            header_blob,
            re.I,
        )
    )

    # Exactly two bare QTY/VALUE pairs with no Opening/Receipt parent bands →
    # SALE + CLOSING (STOCK & SALES ANALYSIS crops often drop <SALE>/<CLOSING>).
    if len(pair_starts) == 2 and not has_named_movement and not has_opening_purchase_text:
        assigned = 0
        for gi, pi in enumerate(pair_starts):
            gname, q_field, v_field = _QTY_VALUE_SEQUENCE_GROUPS[gi + 2]  # sales, closing
            prelim[pi]["canonical"] = q_field
            prelim[pi]["group"] = gname
            prelim[pi]["is_value"] = False
            prelim[pi]["confidence"] = 0.9
            prelim[pi]["reason"] = "qty_value_sequence_sale_closing"
            prelim[pi + 1]["canonical"] = v_field
            prelim[pi + 1]["group"] = gname
            prelim[pi + 1]["is_value"] = True
            prelim[pi + 1]["confidence"] = 0.9
            prelim[pi + 1]["reason"] = "qty_value_sequence_sale_closing"
            assigned += 1
        if assigned:
            errors.append(
                {
                    "code": "QTY_VALUE_SEQUENCE_SALE_CLOSING",
                    "message": (
                        "Assigned 2 bare QTY/VALUE pairs as SALE then CLOSING "
                        "(no opening/receipt headers)"
                    ),
                    "pairs": assigned,
                }
            )
            logger.info(
                "HEADER_RESOLVER qty_value_sequence_sale_closing pairs=%s",
                assigned,
            )
            return True
        return False

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


def _is_qf_subheader(norm: str) -> bool:
    """True for Saleable Stock Report qty sub-headers like (Q+F) / Q+F."""
    compact = re.sub(r"[^a-z0-9]+", "", (norm or "").lower())
    return compact in {"qf", "qplusf", "qtyf", "qandf"}


def _apply_qf_qty_sequence(
    prelim: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
) -> bool:
    """When parent Opn/Rec/Issue/Bal labels are lost and only (Q+F) remains.

    Maps consecutive (Q+F) columns left-to-right onto
    opening_qty → purchase_qty → sales_qty → closing_qty. Does not invent
    values — only renames columns. Default ON (STOCK_HEADER_QF_SEQUENCE).
    """
    if not _qf_qty_sequence_enabled():
        return False
    if not any(
        str(c.get("canonical") or "") in {"product_name", "pack"} for c in prelim
    ):
        return False

    idxs: List[int] = []
    for i, col in enumerate(prelim):
        norm = normalize_header(col.get("header_text"))
        canon = str(col.get("canonical") or "ignore")
        if not _is_qf_subheader(norm):
            continue
        # Remap only unlabeled (Q+F) cells — never override named Opn/Rec/etc.
        if canon not in {"ignore", "value_marker"}:
            continue
        idxs.append(i)

    if len(idxs) < 3:
        return False

    run: List[int] = []
    best: List[int] = []
    for i in idxs:
        if not run or i == run[-1] + 1:
            run.append(i)
        else:
            if len(run) > len(best):
                best = list(run)
            run = [i]
    if len(run) > len(best):
        best = list(run)
    if len(best) < 3:
        return False
    best = best[:4]

    for gi, pi in enumerate(best):
        gname, q_field = _QF_QTY_SEQUENCE_GROUPS[gi]
        prelim[pi]["canonical"] = q_field
        prelim[pi]["group"] = gname
        prelim[pi]["is_value"] = False
        prelim[pi]["confidence"] = 0.9
        prelim[pi]["reason"] = "qf_qty_sequence"

    logger.info(
        "stock_header_qf_sequence assigned=%d cols=%s",
        len(best),
        ",".join(str(prelim[i]["col_index"]) for i in best),
    )
    errors.append(
        {
            "code": "QF_QTY_SEQUENCE",
            "message": (
                f"Assigned {len(best)} (Q+F) columns left-to-right "
                "(opening/purchase/sales[/closing])"
            ),
            "cols": len(best),
        }
    )
    return True


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
            if _medica_in_ot_as_free_in_enabled():
                logger.info(
                    "MEDICA_IN_OT_FREE_IN header resolved col_index=%s text=%r -> free_in_qty",
                    idx,
                    text,
                )
                prelim.append(
                    {
                        "col_index": idx,
                        "header_text": text,
                        "subheader_text": sub_s or None,
                        "group": None,
                        "canonical": "free_in_qty",
                        "confidence": 0.9,
                        "is_value": False,
                        "reason": "medica_in_ot",
                    }
                )
                continue
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
        if compact_hdr in {
            "mexp",
            "mexpiry",
            "shexp",
            "nearexpiry",
            "nearexp",
            "expirydate",
            "expdate",
            "expdt",
            "expirystock",
        } or norm in {
            "m exp",
            "sh exp",
            "near expiry",
            "expiry",
            "expiry date",
            "exp date",
            "exp dt",
            "expiry stock",
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
        elif group == "non_stock_qty":
            canonical = "ignore"
            is_value = False
            logger.info(
                "STOCK_HEADER_IGNORE_NON_QTY ignored header col_index=%s text=%r",
                idx,
                text,
            )
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
            # "Opening sto Value" is a qty column with glued Value OCR, not opening_value.
            if valueish and group and _glued_stock_header_prefers_qty(norm, group):
                valueish = False
                reason = (f"{reason}|glued_stock_qty" if reason else "glued_stock_qty")
                logger.info(
                    "STOCK_HEADER_IGNORE_NON_QTY glued stock header prefers qty "
                    "col_index=%s text=%r",
                    idx,
                    text,
                )
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

        # Glued "IN/OT STOCK" is one header cell: it cannot be split into two
        # columns here, so STOCK stays closing_qty and the glue is logged.
        if (
            compact_hdr in {"inotstock", "inoutstock"}
            and canonical == "closing_qty"
            and _medica_in_ot_as_free_in_enabled()
        ):
            reason = f"{reason}|medica_in_ot_glued" if reason else "medica_in_ot_glued"
            logger.warning(
                "MEDICA_IN_OT_FREE_IN glued header col_index=%s text=%r kept as "
                "closing_qty (IN/OT part cannot be split from one header cell)",
                idx,
                text,
            )

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

    # Repeated bare STOCK → first opening, last closing (before free/dedupe).
    _apply_multi_stock_sequence(prelim, errors)

    # Free / scheme direction from neighbours (or PUR/SALE in the header text).
    for i, col in enumerate(prelim):
        if col["canonical"] != "free" and col.get("group") != "free":
            continue
        hdr_free = normalize_header(col.get("header_text"))
        left_group = prelim[i - 1].get("group") if i > 0 else None
        right_group = prelim[i + 1].get("group") if i + 1 < len(prelim) else None
        left_canon = prelim[i - 1]["canonical"] if i > 0 else None
        if re.search(r"\b(?:pur|purchase|rec|receipt)\b", hdr_free):
            col["canonical"] = "free_in_qty"
            col["confidence"] = 0.75
            col["reason"] = "free_header_purchase"
        elif re.search(r"\b(?:sale|sales)\b", hdr_free):
            col["canonical"] = "free_out_qty"
            col["confidence"] = 0.75
            col["reason"] = "free_header_sales"
        elif left_group == "purchase" or left_canon in {
            "purchase_qty",
            "purchase_value",
            "free_in_qty",
        }:
            col["canonical"] = "free_in_qty"
            col["confidence"] = 0.6
            col["reason"] = "neighbour"
        elif left_group == "purchase_return" or left_canon in {
            "purchase_return_qty",
        }:
            # FREE after P.RETURN is return-free. Do not steal free_out from
            # the FREE column that sits after SALE (right neighbour is SALE).
            col["canonical"] = "ignore"
            col["confidence"] = 0.5
            col["reason"] = "return_free"
            logger.info(
                "STOCK_HEADER_RETURN_FREE ignored col_index=%s text=%r",
                col.get("col_index"),
                col.get("header_text"),
            )
        elif left_group == "sales" or left_canon in {
            "sales_qty",
            "sales_value",
            "free_out_qty",
        }:
            col["canonical"] = "free_out_qty"
            col["confidence"] = 0.6
            col["reason"] = "neighbour"
        elif left_group == "sales_return" or left_canon in {
            "sales_return_qty",
        }:
            col["canonical"] = "ignore"
            col["confidence"] = 0.5
            col["reason"] = "return_free"
            logger.info(
                "STOCK_HEADER_RETURN_FREE ignored col_index=%s text=%r",
                col.get("col_index"),
                col.get("header_text"),
            )
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
    # Saleable Stock Report: parent Opn/Rec/Issue/Bal lost; only (Q+F) remains.
    _apply_qf_qty_sequence(prelim, errors)
    # Group Wise Sales: Op/Purchase/Free/Ret/Sales Qty/Cl.Stock (before dedup).
    _apply_group_wise_sales_sequence(prelim, errors)
    # LstSL|Open|Recd|Sales|Close: Close misread as Open. after Sales.
    _apply_lstsl_close_after_sales(prelim, errors)

    # Receipt|Receipt/Pur and Closing|Closing Bala: right column is VALUE.
    _promote_adjacent_same_group_qty_to_value(prelim)

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
    # Optionally keep ignore columns as absorbers so Rate/NRV tokens do not
    # spill into neighbouring opening/closing bands. Date/expiry ignore columns
    # always absorb — otherwise Op.Value|Expiry|Pur.Qnt bands collapse and
    # amounts land in the wrong movement field.
    absorb_ignore = _absorb_ignore_geometry_enabled()
    centers: List[Tuple[int, float, str]] = []
    date_ignore_absorbers = 0
    geo_cols = [c for c in columns if c.get("x_center") is not None]
    non_ignore_xs = [
        float(c["x_center"])
        for c in geo_cols
        if str(c.get("canonical") or "ignore") != "ignore"
    ]
    left_anchor = min(non_ignore_xs) if non_ignore_xs else None
    for c in geo_cols:
        canon = str(c.get("canonical") or "ignore")
        reason = str(c.get("reason") or "")
        is_date_ignore = canon == "ignore" and reason == "date_or_near_expiry"
        # OTHERS / ITEMCODE sit right of the product column. Dropping them
        # spills their tokens into closing/order (CELL_COLLISION → None).
        is_right_ignore = (
            canon == "ignore"
            and left_anchor is not None
            and float(c["x_center"]) > left_anchor
        )
        if (
            canon == "ignore"
            and not absorb_ignore
            and not is_date_ignore
            and not is_right_ignore
        ):
            continue
        if is_date_ignore or is_right_ignore:
            date_ignore_absorbers += 1
        centers.append((int(c["col_index"]), float(c["x_center"]), canon))
    centers.sort(key=lambda t: t[1])
    if absorb_ignore or date_ignore_absorbers:
        logger.info(
            "STOCK_HEADER_ABSORB_IGNORE_GEOMETRY centers=%s ignore_absorbers=%s "
            "date_ignore=%s flag=%s",
            len(centers),
            sum(1 for _i, _x, c in centers if c == "ignore"),
            date_ignore_absorbers,
            absorb_ignore,
        )
    if len(centers) < 2:
        pitch = 50.0
    else:
        gaps = [centers[i + 1][1] - centers[i][1] for i in range(len(centers) - 1)]
        pitch = float(median(gaps)) if gaps else 50.0
        if pitch <= 0:
            pitch = 50.0

    # Midpoint bands (default ON). Kill switch STOCK_HEADER_COLUMN_BANDS=false
    # restores nearest-centre assign for rollback without code revert.
    if _column_bands_enabled():
        return _assign_cells_by_column_bands(
            row_tokens, centers, pitch, fields
        )

    # Legacy nearest-centre assign (flag off).
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
                for _d, _i, amb_canon in dists[:2]:
                    if amb_canon == "ignore":
                        continue
                    ambiguous_canons.add(amb_canon)
                    fields[amb_canon] = None
                continue
        bucket[best_idx].append(raw)

    for idx, _x, canon in centers:
        if canon == "ignore":
            continue
        if canon in ambiguous_canons:
            fields[canon] = None
            continue
        _finalize_qty_bucket(bucket.get(idx) or [], canon, fields, errors, idx)

    return {"fields": fields, "errors": errors}


# Consecutive Greek τ..ύ (U+03C4..U+03CD) are a common PDF ToUnicode miss
# for digits 0-9. Mapping is codepoint - 0x03C4; no-op when absent.
_PSEUDO_DIGIT_START = 0x03C4
_PSEUDO_DIGIT_END = 0x03CD


def decode_pdf_pseudo_digits(text: Any) -> str:
    """Map PDF pseudo-digit Greek letters τ..ύ onto ASCII 0-9."""
    raw = "" if text is None else str(text)
    if not raw:
        return raw
    out: List[str] = []
    changed = False
    for ch in raw:
        cp = ord(ch)
        if _PSEUDO_DIGIT_START <= cp <= _PSEUDO_DIGIT_END:
            out.append(chr(ord("0") + (cp - _PSEUDO_DIGIT_START)))
            changed = True
        else:
            out.append(ch)
    return "".join(out) if changed else raw


def decode_pdf_word_tokens(words: Any) -> Any:
    """Decode pseudo-digits on pdfplumber/fitz word tokens. Other shapes pass through."""
    if not words:
        return words
    decoded: List[Any] = []
    for word in words:
        if isinstance(word, dict):
            item = dict(word)
            if "text" in item:
                item["text"] = decode_pdf_pseudo_digits(item.get("text"))
            decoded.append(item)
        elif isinstance(word, (list, tuple)) and len(word) > 4:
            seq = list(word)
            seq[4] = decode_pdf_pseudo_digits(seq[4])
            decoded.append(tuple(seq) if isinstance(word, tuple) else seq)
        else:
            decoded.append(word)
    return decoded


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
    text = decode_pdf_pseudo_digits(str(raw).strip())
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
    # Pipe-ruled PDFs glue gutters / expiry onto amounts ("33.00|", "1884.14|05/2029|").
    if "|" in text:
        text = text.split("|", 1)[0].strip()
    # Vision/OCR often truncates money as "9,877." / "3,123." — trailing dot,
    # no fraction digits. Keep the printed integer part; never invent decimals.
    if re.fullmatch(r"-?\d+\.", text):
        text = text[:-1]
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
