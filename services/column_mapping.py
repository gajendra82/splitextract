"""Profile-attached column labels for recovery.

A mapping says which printed header a recognized layout already treats as a
canonical field. It does not guess what an unknown header means, and it does
not calculate or validate a quantity.

Lookup is by extraction method, layout, or identity profile id. There is no
universal header dictionary. "Op" maps only on a layout whose parser already
says that printed word is opening quantity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from services.focused_row_request import CANONICAL_FIELDS

SOURCE_PARSER = "parser"
SOURCE_PROFILE = "profile"
SOURCE_LAYOUT = "explicit_layout_definition"


@dataclass(frozen=True)
class ColumnMapping:
    printed_header: str
    canonical_field: str
    source: str
    layout_id: str
    confidence: float = 1.0
    column_index: Optional[int] = None
    aliases: tuple = ()


def header_key(text: str) -> str:
    """Case, whitespace, and punctuation only. Not a prefix or fuzzy match."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


def _mapping(
    header: str,
    field: str,
    layout_id: str,
    *,
    source: str = SOURCE_LAYOUT,
    aliases: tuple = (),
    column_index: Optional[int] = None,
) -> ColumnMapping:
    return ColumnMapping(
        printed_header=header,
        canonical_field=field,
        source=source,
        layout_id=layout_id,
        aliases=tuple(aliases),
        column_index=column_index,
    )


# Pharma Hub Jun/Jul prompt lists Opening, Purchase, Sales, Closing, and
# Stock-Out. It does not assign column indexes, and it does not say that
# Op, Pur, Sale, or Bal are those columns. Those short labels stay unmapped.
_PHARMA_HUB = (
    _mapping("Opening", "opening_qty", "pharma_hub_jun_jul"),
    _mapping("Purchase", "receipts_qty", "pharma_hub_jun_jul"),
    _mapping("Sales", "sales_qty", "pharma_hub_jun_jul"),
    _mapping("Closing", "closing_qty", "pharma_hub_jun_jul"),
    _mapping("Stock-Out", "stock_out_qty", "pharma_hub_jun_jul"),
    _mapping("Stock-Value", "closing_value", "pharma_hub_jun_jul"),
    _mapping("Sales-Value", "sales_value", "pharma_hub_jun_jul"),
)

# MediVision "Stock and Sales" prompt assigns these printed words directly.
# WSale, Jul Sa, and Jun Sa are deliberately absent.
_MEDIVISION_OP_WSALE = (
    _mapping("Op", "opening_qty", "medivision_op_wsale"),
    _mapping("Purc", "receipts_qty", "medivision_op_wsale"),
    _mapping("Sale", "sales_qty", "medivision_op_wsale"),
    _mapping("Cl qty", "closing_qty", "medivision_op_wsale"),
    _mapping("Sa val", "sales_value", "medivision_op_wsale"),
    _mapping("Cl val", "closing_value", "medivision_op_wsale"),
)

# _MONTHLY_SS_ROLES. Compact role names match the printed header after the
# same whitespace/punctuation normalization. Sale Value is not Sale Qty.
_MONTHLY_SS = (
    _mapping("Opening", "opening_qty", "monthly_ss_opening_pur_sale_closing", source=SOURCE_PARSER),
    _mapping("Pur Qty", "receipts_qty", "monthly_ss_opening_pur_sale_closing", source=SOURCE_PARSER),
    _mapping("Sale Qty", "sales_qty", "monthly_ss_opening_pur_sale_closing", source=SOURCE_PARSER),
    _mapping("Closing", "closing_qty", "monthly_ss_opening_pur_sale_closing", source=SOURCE_PARSER),
    _mapping("Sale Value", "sales_value", "monthly_ss_opening_pur_sale_closing", source=SOURCE_PARSER),
    _mapping("Pur Value", "receipts_value", "monthly_ss_opening_pur_sale_closing", source=SOURCE_PARSER),
    _mapping("Closing Amt", "closing_value", "monthly_ss_opening_pur_sale_closing", source=SOURCE_PARSER),
)

# Product-wise header detector: Opening, Receipt, Issues, Closing are the
# quantity columns. "Sales" is the sales-amount column, not sales_qty.
_PRODUCT_WISE = (
    _mapping("Opening", "opening_qty", "product_wise_stock_statement", source=SOURCE_PARSER),
    _mapping("Receipt", "receipts_qty", "product_wise_stock_statement", source=SOURCE_PARSER),
    _mapping(
        "Issues",
        "sales_qty",
        "product_wise_stock_statement",
        source=SOURCE_PARSER,
        aliases=("Issue",),
    ),
    _mapping("Closing", "closing_qty", "product_wise_stock_statement", source=SOURCE_PARSER),
)

# Op.Bal / Receipt / Issue / Closing. Total, Dump, Expiry, and Near are not
# quantity canonical fields. "Op" is not "Op.Bal".
_OPBAL = (
    _mapping(
        "Op.Bal",
        "opening_qty",
        "opening_receipt_issue_closing",
        source=SOURCE_PARSER,
        aliases=("OpBal",),
    ),
    _mapping("Receipt", "receipts_qty", "opening_receipt_issue_closing", source=SOURCE_PARSER),
    _mapping("Issue", "sales_qty", "opening_receipt_issue_closing", source=SOURCE_PARSER),
    _mapping("Closing", "closing_qty", "opening_receipt_issue_closing", source=SOURCE_PARSER),
)

# Product Stock Report aliases. Bare "value" and "amount" are omitted: the
# parser accepts them, but they are not specific enough to promote here.
# Pack / Op / Pur / Bal vision prompt numbers the printed columns.
# SS and Sp Qty are scheme columns in that prompt, not stock_out_qty.
_PACK_OP_PUR_BAL = (
    _mapping("Op", "opening_qty", "pack_op_pur_bal_stock_sale", column_index=3),
    _mapping("Pur", "receipts_qty", "pack_op_pur_bal_stock_sale", column_index=4),
    _mapping("Sale", "sales_qty", "pack_op_pur_bal_stock_sale", column_index=7),
    _mapping("Sale Val", "sales_value", "pack_op_pur_bal_stock_sale", column_index=10),
    _mapping("Bal.", "closing_qty", "pack_op_pur_bal_stock_sale", column_index=11),
    _mapping("Bal Val", "closing_value", "pack_op_pur_bal_stock_sale", column_index=12),
)


_PRODUCT_STOCK_REPORT = (
    _mapping(
        "Opening",
        "opening_qty",
        "product_stock_report",
        source=SOURCE_PARSER,
        aliases=("Opening Qty", "Op Qty"),
    ),
    _mapping(
        "Purchase",
        "receipts_qty",
        "product_stock_report",
        source=SOURCE_PARSER,
        aliases=("Purchase Qty", "Pur Qty"),
    ),
    _mapping(
        "Sale",
        "sales_qty",
        "product_stock_report",
        source=SOURCE_PARSER,
        aliases=("Sales", "Sale Qty", "Sales Qty"),
    ),
    _mapping(
        "Closing",
        "closing_qty",
        "product_stock_report",
        source=SOURCE_PARSER,
        aliases=("Closing Qty", "Cls Qty", "Closing Stock"),
    ),
    _mapping(
        "Cls Amt",
        "closing_value",
        "product_stock_report",
        source=SOURCE_PARSER,
        aliases=("Closing Amt", "Closing Value", "Cls Amount", "Stk Val", "Stock Val", "Stock Value"),
    ),
    _mapping(
        "Sale Amt",
        "sales_value",
        "product_stock_report",
        source=SOURCE_PARSER,
        aliases=("Sales Amt", "Sale Value", "Sales Value", "Net Amt"),
    ),
)


# Op / Pur / ORec / Sale / OIss / Cl. ORec is not receipts_qty. OIss is not sales_qty.
_OP_PUR_OREC = (
    _mapping("Op.Qty.", "opening_qty", "op_pur_orec_sale_oiss", source=SOURCE_PARSER),
    _mapping("Pur.Qty.", "receipts_qty", "op_pur_orec_sale_oiss", source=SOURCE_PARSER),
    _mapping("Sale.Qty.", "sales_qty", "op_pur_orec_sale_oiss", source=SOURCE_PARSER),
    _mapping("Cl.Qty.", "closing_qty", "op_pur_orec_sale_oiss", source=SOURCE_PARSER),
)


_DEFINITIONS = {
    "op_pur_orec_sale_oiss": _OP_PUR_OREC,
    "pharma_hub_jun_jul": _PHARMA_HUB,
    "pharma_hub_jun_jul_photo": _PHARMA_HUB,
    "medivision_op_wsale_photo": _MEDIVISION_OP_WSALE,
    "monthly_ss": _MONTHLY_SS,
    "monthly_ss_report": _MONTHLY_SS,
    "monthly_ss_opening_pur_sale_closing": _MONTHLY_SS,
    "product_wise": _PRODUCT_WISE,
    "product_wise_image": _PRODUCT_WISE,
    "product_wise_stock_statement": _PRODUCT_WISE,
    "product_wise_stock_statement_image": _PRODUCT_WISE,
    "opbal": _OPBAL,
    "product_stock_report": _PRODUCT_STOCK_REPORT,
    "product_stock_report_html": _PRODUCT_STOCK_REPORT,
    "pack_op_pur_bal_stock_sale": _PACK_OP_PUR_BAL,
    "pack_op_pur_bal_stock_sale_vision": _PACK_OP_PUR_BAL,
}


def _extra(result: Dict[str, Any]) -> Dict[str, Any]:
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    return extra


def _stored_parser_mappings(result: Dict[str, Any], profile_id: str) -> Tuple[ColumnMapping, ...]:
    """Headers a parser already stored on this statement. Not a header dictionary."""
    raw = _extra(result).get("column_mapping")
    if not isinstance(raw, dict):
        return ()
    found = []
    for key, value in raw.items():
        header = None
        field = None
        confidence = 1.0
        column_index = None
        if isinstance(value, dict) and key in CANONICAL_FIELDS:
            header = value.get("header")
            field = key
            if value.get("confidence") is not None:
                try:
                    confidence = float(value.get("confidence"))
                except (TypeError, ValueError):
                    confidence = 0.0
            index = value.get("column_index")
            if isinstance(index, int):
                column_index = index
        elif isinstance(value, str) and isinstance(key, str):
            header = key
            field = value
        if not isinstance(header, str) or not header.strip():
            continue
        if field not in CANONICAL_FIELDS or confidence <= 0:
            continue
        found.append(
            ColumnMapping(
                printed_header=header.strip(),
                canonical_field=field,
                source=SOURCE_PARSER,
                layout_id=profile_id,
                confidence=confidence,
                column_index=column_index,
            )
        )
    return tuple(found)


def mappings_for_profile(result: Dict[str, Any], profile: Any) -> Tuple[ColumnMapping, ...]:
    """Mappings for the profile select_profile already chose. Unknown stays empty."""
    if not getattr(profile, "supports_recovery", False):
        return ()
    profile_id = str(getattr(profile, "profile_id", "") or "")
    if profile_id in {"unknown", "not_scored"}:
        return ()
    if not isinstance(result, dict):
        return ()
    extra = _extra(result)
    keys = (
        str(extra.get("extraction_method") or ""),
        str(extra.get("layout") or ""),
        profile_id,
    )
    found = []
    seen = set()
    owned = set()
    for key in keys:
        for item in _DEFINITIONS.get(key, ()):
            ident = (item.layout_id, header_key(item.printed_header), item.canonical_field)
            names = (item.printed_header,) + tuple(item.aliases or ())
            for name in names:
                owned.add((header_key(name), item.canonical_field))
            if ident in seen:
                continue
            seen.add(ident)
            found.append(item)
    # A parser-stored header is trusted only when this layout already owns
    # that same printed header and canonical field. A global Excel alias is not.
    for item in _stored_parser_mappings(result, profile_id):
        if (header_key(item.printed_header), item.canonical_field) not in owned:
            continue
        ident = (item.layout_id, header_key(item.printed_header), item.canonical_field)
        if ident in seen:
            continue
        seen.add(ident)
        found.append(item)
    return tuple(found)


def _matching(header: str, mappings: Iterable[ColumnMapping]) -> Tuple[ColumnMapping, ...]:
    wanted = header_key(header)
    if not wanted:
        return ()
    hits = []
    for item in mappings:
        names = (item.printed_header,) + tuple(item.aliases or ())
        if any(header_key(name) == wanted for name in names):
            hits.append(item)
    return tuple(hits)


def header_map_for_evidence(
    mappings: Sequence[ColumnMapping],
    headers: Sequence[Any],
) -> Dict[str, str]:
    """Printed headers on this evidence that match the layout. Duplicates drop out.

    Two different printed headers for one canonical field are not chosen between.
    A header that matches two canonical fields is not chosen either.
    """
    hits = []
    for header in headers or []:
        text = str(header or "").strip()
        if not text:
            continue
        matched = _matching(text, mappings)
        fields = {item.canonical_field for item in matched}
        if len(fields) != 1:
            continue
        hits.append((text, next(iter(fields))))
    by_field: Dict[str, list] = {}
    for header, field in hits:
        by_field.setdefault(field, []).append(header)
    resolved: Dict[str, str] = {}
    for field, found_headers in by_field.items():
        keys = {header_key(header) for header in found_headers}
        if len(keys) != 1:
            continue
        resolved[found_headers[0]] = field
    return resolved
