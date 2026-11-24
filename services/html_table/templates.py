"""Known high-volume vendor HTML table templates."""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

DetectFn = Callable[[List[str]], bool]

# Premier / Himalaya Marg-style PRODUCT DESCRIPTION … EXPIRY STOCK layout.
_PREMIER_HEADERS = (
    "product description",
    "opening stock",
    "opening value",
    "receive quantity",
    "receive value",
    "issue quantity",
    "issue value",
    "closing stock",
    "closing value",
    "last month sale",
    "expiry stock",
)

_PREMIER_COL_MAP: Dict[int, str] = {
    0: "product_name",
    1: "opening_qty",
    2: "opening_value",
    3: "receive_qty",
    4: "receive_value",
    5: "sales_qty",
    6: "sales_value",
    7: "closing_qty",
    8: "closing_value",
    9: "last_month_sale",
    10: "expiry_stock",
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("\xa0", " ")).strip().casefold()


def _detect_himalaya_premier(headers: List[str]) -> bool:
    norms = [_norm(h) for h in (headers or [])]
    if len(norms) < 8:
        return False
    # Exact Premier header band (allow trailing extras).
    expected = list(_PREMIER_HEADERS)
    if len(norms) >= len(expected) and norms[: len(expected)] == expected:
        return True
    # Tolerant: key columns present in order.
    joined = " | ".join(norms)
    return (
        "product description" in joined
        and "opening stock" in joined
        and "receive quantity" in joined
        and "issue quantity" in joined
        and "closing stock" in joined
    )


TEMPLATES: Dict[str, Dict[str, object]] = {
    "himalaya_premier": {
        "detect": _detect_himalaya_premier,
        "col_map": dict(_PREMIER_COL_MAP),
    },
}


def route_template(
    headers: List[str],
) -> Tuple[Optional[str], Optional[Dict[int, str]]]:
    """Return ``(template_name, col_map)`` or ``(None, None)`` when no template fits."""
    for name, spec in TEMPLATES.items():
        detect = spec.get("detect")
        col_map = spec.get("col_map")
        if not callable(detect) or not isinstance(col_map, dict):
            continue
        try:
            if detect(headers or []):
                return name, {int(k): str(v) for k, v in col_map.items()}
        except Exception:
            continue
    return None, None
