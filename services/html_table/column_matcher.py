"""Fuzzy header → schema field matcher for pharma stock/sales HTML tables."""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Pattern

logger = logging.getLogger(__name__)

SCHEMA_FIELDS = (
    "product_name",
    "opening_qty",
    "opening_value",
    "receive_qty",
    "receive_value",
    "sales_qty",
    "sales_value",
    "closing_qty",
    "closing_value",
    "last_month_sale",
    "expiry_stock",
)


def _compile(patterns: List[str]) -> List[Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


# Case-insensitive regexes covering common Indian pharma/distribution headers.
COLUMN_PATTERNS: Dict[str, List[Pattern[str]]] = {
    "product_name": _compile(
        [
            r"\bproduct\s*desc(?:ription)?\b",
            r"\bproduct\s*name\b",
            r"\bitem\s*desc(?:ription)?\b",
            r"\bdescription\b",
            r"\bdrug\s*name\b",
            r"\bitem\s*name\b",
            r"\bmedicine\s*name\b",
            r"\bcommodity\b",
            r"\bparticulars\b",
            r"\bproduct\b",
        ]
    ),
    "opening_qty": _compile(
        [
            r"\bopening\s*stock\b",
            r"\bopening\s*qty\b",
            r"\bopening\s*quantity\b",
            r"\bopen(?:ing)?\s*stock\b",
            r"\bop\.?\s*stock\b",
            r"\bop\.?\s*stk\b",
            r"\bopening\s*balance(?:\s*qty)?\b",
            r"\bopen\s*bal(?:ance)?(?:\s*qty)?\b",
            r"\bop\.?\s*bal(?:ance)?(?:\s*qty\.?)?\b",
            r"\bopn\.?\s*(?:qty|stock|stk)?\b",
            # Bare "Open" on Stock & Sales grids (never LstSL / last-month).
            r"^(?!.*\b(?:lst|last|lm|value|val|amt|amount)\b).*\bopen(?:ing)?\b$",
            r"^(?!.*\b(?:value|val|amt|amount)\b).*\bopening\b",
        ]
    ),
    "opening_value": _compile(
        [
            r"\bopening\s*value\b",
            r"\bopen(?:ing)?\s*val(?:ue)?\b",
            r"\bopening\s*amount\b",
            r"\bopening\s*amt\b",
            r"\bop\.?\s*val(?:ue)?\b",
        ]
    ),
    "receive_qty": _compile(
        [
            r"\breceive\s*qty\b",
            r"\breceive\s*quantity\b",
            r"\breceipt\s*qty\b",
            r"\breceipts?\s*qty\b",
            r"\breceipt\s*quantity\b",
            # Common OCR/abbrev form on Himalaya/Swil-style grids.
            r"\brcpt\.?\s*qty\b",
            r"^rcpt\.?$",
            r"\brecd\.?\s*qty\b",
            r"\binward\s*qty\b",
            r"\binward\b",
            r"\bpurchase\s*qty\b",
            r"\bincoming\s*qty\b",
            r"\bgrn\s*qty\b",
            # Bare "Recd." / "Recd" on Stock & Sales grids.
            r"^recd\.?$",
            r"^rec(?:eive)?d?\.?$",
            # Bare "Purchase" on stock statements (not Free / Total / Value).
            r"^(?!.*\b(?:free|scheme|bonus|total|value|val|amt|amount)\b).*\bpurchase\b$",
            r"^(?!.*\b(?:value|val|amt|amount)\b).*\breceive(?:\s*qty|\s*quantity)?\b",
            r"^(?!.*\b(?:value|val|amt|amount)\b).*\breceipts?(?:\s*qty|\s*quantity)?\b",
            r"^(?!.*\b(?:value|val|amt|amount)\b).*\brcpt\.?(?:\s*qty|\s*quantity)?\b",
        ]
    ),
    "receive_value": _compile(
        [
            r"\breceive\s*value\b",
            r"\breceipt\s*value\b",
            r"\breceipts?\s*value\b",
            r"\brecd\.?\s*val(?:ue)?\b",
            r"\binward\s*val(?:ue)?\b",
            r"\bpurchase\s*value\b",
        ]
    ),
    "sales_qty": _compile(
        [
            r"\bissue\s*qty\b",
            r"\bissue\s*quantity\b",
            r"\bsales?\s*qty\b",
            r"\bsales?\s*quantity\b",
            r"\bdispatch\s*qty\b",
            r"\bdespatch\s*qty\b",
            r"\boutward\s*qty\b",
            r"\boutward\b",
            r"\boutgoing\s*qty\b",
            r"\bdelivery\s*qty\b",
            r"\bgoods\s*issue\b",
            r"^(?!.*(?:replied|return|last|prev|lm|month)).*\bissue\b",
            r"^(?!.*(?:last|prev|lm|month|value|val)).*\bsales?\b$",
        ]
    ),
    "sales_value": _compile(
        [
            r"\bissue\s*value\b",
            r"\bsales?\s*value\b",
            r"\bdispatch\s*value\b",
            r"\bdespatch\s*value\b",
            r"\boutward\s*value\b",
            r"\boutgoing\s*value\b",
        ]
    ),
    "closing_qty": _compile(
        [
            r"\bclosing\s*stock\b",
            r"\bclosing\s*qty\b",
            r"\bclosing\s*quantity\b",
            r"\bclose\s*stock\b",
            r"\bend\s*stock\b",
            r"\bbalance\s*stock\b",
            r"\bclosing\s*balance\b",
            r"\bclosing\s*bal\b",
            r"\bcl\.?\s*stk\b",
            r"\bcl\.?\s*stock\b",
            r"\bcl\.?\s*bal(?:ance)?(?:\s*qty\.?)?\b",
            r"^close$",
            r"\bclosing\b",
        ]
    ),
    "closing_value": _compile(
        [
            r"\bclosing\s*value\b",
            r"\bclose\s*val(?:ue)?\b",
            r"\bend\s*value\b",
            r"\bbalance\s*value\b",
            r"\bclosing\s*amount\b",
            r"\bcl\.?\s*val(?:ue)?\b",
        ]
    ),
    "last_month_sale": _compile(
        [
            r"\blast\s*month\s*sales?\b",
            r"\bprev(?:ious)?\s*month\b",
            r"\blm\s*sales?\b",
            r"\bprevious\s*month\s*sales?\b",
            # Stock & Sales monospace headers: LstSL / Lst.SL / LST SL
            r"^lst\.?\s*sl\.?$",
            r"\blst\.?\s*sl\b",
            r"^lm\.?\s*sl\.?$",
            r"\blast\s*sl\b",
        ]
    ),
    "expiry_stock": _compile(
        [
            r"\bexpiry\s*stock\b",
            r"\bexp\.?\s*stock\b",
            r"\bexpired\b",
            r"\bexpiry\s*qty\b",
            r"\bexp\.?\s*qty\b",
            r"\bnear\s*expiry\b",
            r"\bexpiry\b",
        ]
    ),
}

# Prefer specific fields (value / last-month / expiry) before broad qty tokens.
_FIELD_TRY_ORDER = (
    "opening_value",
    "receive_value",
    "sales_value",
    "closing_value",
    "last_month_sale",
    "expiry_stock",
    "opening_qty",
    "receive_qty",
    "sales_qty",
    "closing_qty",
    "product_name",
)


def match_headers(headers: List[str]) -> Dict[int, str]:
    """Map header column indexes to schema fields via fuzzy regex.

    Unmatched headers are skipped. If two headers match the same field, the
    first wins and a warning is logged for the duplicate.
    """
    col_map: Dict[int, str] = {}
    claimed: Dict[str, int] = {}

    for index, raw in enumerate(headers or []):
        text = re.sub(r"\s+", " ", str(raw or "").replace("\xa0", " ")).strip()
        if not text:
            continue
        matched_field = None
        for field in _FIELD_TRY_ORDER:
            patterns = COLUMN_PATTERNS.get(field) or []
            for pattern in patterns:
                if pattern.search(text):
                    matched_field = field
                    break
            if matched_field:
                break
        if not matched_field:
            continue
        if matched_field in claimed:
            logger.warning(
                "column_matcher: header %r (col %s) also matches %s; "
                "keeping first match at col %s",
                text,
                index,
                matched_field,
                claimed[matched_field],
            )
            continue
        col_map[index] = matched_field
        claimed[matched_field] = index

    return col_map
