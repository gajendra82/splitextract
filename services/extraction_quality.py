"""Quality gate for secondary-sales extraction.

Existing parsers stay primary. This module only decides whether a result is
trustworthy. It does not rewrite quantities or call a model.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

# Generic readers may be incomplete. Named format parsers are left as-is.
_GENERIC_METHODS = {
    "",
    "gemini_vision",
    "gemini_extraction_fallback",
    "txt_stock_fallback",
    "legacy_doc_text",
    "pdf_split_page_images",
}

_NOISE_NAME = re.compile(
    r"(?i)\b(?:0pen(?:ing)?|5a1es?|c1os(?:ing)?|cl0sing)\b"
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {"-", "—", "NA", "N/A", "null", "None"}:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _statements(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    statements = result.get("statements")
    if isinstance(statements, list) and statements:
        return [item for item in statements if isinstance(item, dict)]
    return [result]


def _items(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for statement in _statements(result):
        for item in statement.get("line_items") or []:
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _method(result: Dict[str, Any]) -> str:
    for statement in _statements(result):
        extra = ((statement.get("totals") or {}).get("extra") or {})
        method = str(extra.get("extraction_method") or "").strip()
        if method:
            return method
    return ""


def _name_kind(name: str) -> str:
    text = re.sub(r"\s+", " ", name or "").strip()
    letters = sum(1 for char in text if char.isalpha())
    if letters < 3 or len(text) <= 1:
        return "missing"
    if _NOISE_NAME.search(text):
        return "noise"
    symbols = sum(1 for char in text if not char.isalnum() and char not in {" ", ".", "-", "/", "'", "(", ")"})
    if symbols > letters:
        return "noise"
    return "ok"


def _duplicate_percent(items: List[Dict[str, Any]]) -> float:
    """Share of rows that repeat the same product and quantities."""
    keys = []
    for item in items:
        keys.append(
            (
                re.sub(r"\s+", " ", str(item.get("product_name") or "")).strip().lower(),
                item.get("opening_qty"),
                item.get("receipts_qty"),
                item.get("sales_qty"),
                item.get("closing_qty"),
            )
        )
    if len(keys) < 2:
        return 0.0
    seen: Dict[tuple, int] = {}
    for key in keys:
        seen[key] = seen.get(key, 0) + 1
    extras = sum(count - 1 for count in seen.values() if count > 1)
    return 100.0 * extras / len(keys)


def _period_is_inverted(result: Dict[str, Any]) -> bool:
    """True only when both period bounds are real dates and the range runs backwards."""
    start = str(result.get("period_from") or "")[:10]
    end = str(result.get("period_to") or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
        return False
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
        return False
    return start > end


def _identity_applies(items: List[Dict[str, Any]], extra: Dict[str, Any]) -> bool:
    """The simple opening/receipt/sales formula only applies when those columns are used."""
    if extra.get("stock_identity_fail_count") is None:
        return False
    kind = str(extra.get("stock_identity_kind") or "")
    if kind not in {"", "opening_receipts_sales_closing"}:
        return False
    usable = 0
    for item in items:
        opening = _number(item.get("opening_qty")) or 0.0
        sales = _number(item.get("sales_qty")) or 0.0
        receipts = _number(item.get("receipts_qty")) or 0.0
        if opening > 0 and (sales > 0 or receipts > 0):
            usable += 1
    return usable >= 5 and usable >= max(1, int(0.2 * len(items)))


def evaluate_extraction_quality(
    result: Optional[Dict[str, Any]],
    source_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Score an existing extract. Does not change its line items."""
    meta = source_metadata or {}
    if not isinstance(result, dict):
        return {
            "quality": "poor",
            "score": 0,
            "should_fallback": True,
            "reasons": ["empty_line_items"],
        }

    items = _items(result)
    method = _method(result)
    min_rows = _env_int("GEMINI_EXTRACTION_MIN_VALID_PRODUCT_ROWS", 1)
    noise_limit = _env_float("GEMINI_EXTRACTION_MAX_OCR_NOISE_PERCENT", 30)
    identity_limit = _env_float("GEMINI_EXTRACTION_MAX_IDENTITY_FAILURE_PERCENT", 20)
    numeric_floor = _env_float("GEMINI_EXTRACTION_MIN_VALID_NUMERIC_PERCENT", 70)

    kinds = [_name_kind(str(item.get("product_name") or "")) for item in items]
    valid_names = sum(1 for kind in kinds if kind == "ok")
    noise_names = sum(1 for kind in kinds if kind == "noise")
    numeric_rows = 0
    for item in items:
        qty_fields = (
            item.get("opening_qty"),
            item.get("receipts_qty"),
            item.get("sales_qty"),
            item.get("closing_qty"),
        )
        if any(_number(value) is not None for value in qty_fields):
            numeric_rows += 1

    name_pct = (100.0 * valid_names / len(items)) if items else 0.0
    noise_pct = (100.0 * noise_names / len(items)) if items else 0.0
    numeric_pct = (100.0 * numeric_rows / len(items)) if items else 0.0

    reasons: List[str] = []
    protected = method not in _GENERIC_METHODS and valid_names >= min_rows
    if protected:
        return {
            "quality": "good",
            "score": 100,
            "should_fallback": False,
            "reasons": [],
            "parser": method,
        }

    if not items:
        reasons.append("empty_line_items")
    else:
        if name_pct < 50:
            reasons.append("missing_product_names")
        if noise_pct > noise_limit:
            reasons.append("high_ocr_noise")
        if numeric_pct < numeric_floor:
            reasons.append("missing_numeric_values")
        pages = int(meta.get("page_count") or 0)
        if pages >= 2 and len(items) < 3:
            reasons.append("too_few_product_rows")
        duplicate_pct = _duplicate_percent(items)
        if len(items) >= 6 and duplicate_pct > 40:
            reasons.append("duplicate_rows")
        if _period_is_inverted(result):
            reasons.append("invalid_period")

        extra = ((result.get("totals") or {}).get("extra") or {})
        fail_count = extra.get("stock_identity_fail_count")
        if _identity_applies(items, extra) and fail_count is not None:
            fail_pct = 100.0 * float(fail_count) / len(items)
            if fail_pct > identity_limit:
                reasons.append("stock_identity_failure")

    score = 100
    if "empty_line_items" in reasons:
        score = 0
    else:
        score -= int(max(0.0, 50 - name_pct))
        score -= int(max(0.0, noise_pct - noise_limit))
        score -= int(max(0.0, numeric_floor - numeric_pct))
        if "stock_identity_failure" in reasons:
            score -= 25
        if "too_few_product_rows" in reasons or "duplicate_rows" in reasons:
            score -= 20
        if "invalid_period" in reasons:
            score -= 10
        score = max(0, min(100, score))

    return {
        "quality": "poor" if reasons else "good",
        "score": score,
        "should_fallback": bool(reasons),
        "reasons": reasons,
        "parser": method or "generic",
    }
