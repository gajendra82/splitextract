"""Quality gate for secondary-sales extraction.

Existing parsers stay primary. This module only decides whether a result is
trustworthy. It does not rewrite quantities or call a model.

Named format parsers remain protected unless the extract shows clear
OCR/header corruption (header-as-product rows, empty items, etc.).
Weak OCR/heuristic methods always receive full quality scoring.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

# Generic / weak readers may be incomplete. Named format parsers stay primary.
_GENERIC_METHODS = {
    "",
    "gemini_vision",
    "gemini_extraction_fallback",
    "txt_stock_fallback",
    "legacy_doc_text",
    "pdf_split_page_images",
    "pdf_split_text",
    "pdf_text_heuristic",
    "pdf_text_gemini",
    "tesseract_heuristic",
}

# OCR/heuristic methods that must never skip the quality gate.
_WEAK_OCR_METHODS = {
    "tesseract_heuristic",
    "pdf_text_heuristic",
    "pdf_split_text",
    "pdf_split_page_images",
    "txt_stock_fallback",
    "legacy_doc_text",
    "gemini_vision",
    "gemini_extraction_fallback",
    "",
}

_NOISE_NAME = re.compile(
    r"(?i)\b(?:0pen(?:ing)?|5a1es?|c1os(?:ing)?|cl0sing)\b"
)

# Metadata / banner lines that OCR often promotes to product_name.
_HEADER_AS_PRODUCT = re.compile(
    r"(?i)^\s*(?:"
    r"STOCK\s*REPORT|STOCK\s*&\s*SALES|SALES\s*&\s*STOCK|COMPANY\s+WISE|"
    r"COMPANY\s*NAME\s*:|AT/?PO\s*-|GST\s*:|GSTIN\s*:|"
    r"ITEM\s+DESCRIPTION|PRODUCT\s*NAME|SNO\s+ITEM|"
    r"Page\s*No\.?|CONT\s+ON\s+NEXT|END\s+OF\s+REPORT|"
    r"TOTAL\s*VAL|FREE\s+VALUE|RETURNED\s+VALUE|"
    r"Phone\s*[+:]|E-?Mail\s*:"
    r")",
)

_STOCK_HEADER_HINTS = (
    "OPENING",
    "OPEN",
    "OPN",
    "OPSTK",
    "RECEIPT",
    "PURCHASE",
    "PUR",
    "SALES",
    "SALE",
    "ISSUE",
    "CLOSING",
    "CLOSE",
    "CLSTK",
    "STOCK",
    "QTY",
    "QUANTITY",
    "VALUE",
    "AMOUNT",
    "PRODUCT",
    "ITEM",
    "PACKING",
    "PACK",
    "RATE",
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


def quality_threshold() -> float:
    """Configurable OCR/extraction quality floor (0–100)."""
    return _env_float("SALES_STATEMENT_OCR_QUALITY_THRESHOLD", 75)


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
    if _NOISE_NAME.search(text) or _HEADER_AS_PRODUCT.search(text):
        return "noise"
    symbols = sum(
        1
        for char in text
        if not char.isalnum() and char not in {" ", ".", "-", "/", "'", "(", ")"}
    )
    if symbols > letters:
        return "noise"
    return "ok"


def _header_as_product_count(items: List[Dict[str, Any]]) -> int:
    count = 0
    for item in items:
        name = str(item.get("product_name") or "")
        if _HEADER_AS_PRODUCT.search(name):
            count += 1
            continue
        # Address/GST lines OCR promotes to products (Nilakantha).
        if re.search(r"GST\s*:|GSTIN|AT/?PO\s*-", name, re.I):
            count += 1
    return count


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
    # Validator already counted failures for this formula — always score them.
    # Misaligned extracts often zero every opening_qty, which would otherwise
    # hide stock_identity_failure and report quality=good.
    try:
        if int(extra.get("stock_identity_fail_count") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    usable = 0
    for item in items:
        opening = _number(item.get("opening_qty")) or 0.0
        sales = _number(item.get("sales_qty")) or 0.0
        receipts = _number(item.get("receipts_qty")) or 0.0
        if opening > 0 and (sales > 0 or receipts > 0):
            usable += 1
    return usable >= 5 and usable >= max(1, int(0.2 * len(items)))


def _normalize_token(token: str) -> str:
    """OCR-tolerant token: O→0, l/I→1, etc. stripped to A-Z0-9."""
    text = (token or "").upper()
    table = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B", "|": "I"})
    text = text.translate(table)
    return re.sub(r"[^A-Z]", "", text)


def assess_source_text_quality(text: str) -> Dict[str, Any]:
    """Score raw OCR/source text before trusting a parser (0–100).

    Used for image-only / scanned inputs. Does not rewrite extraction.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return {
            "score": 0,
            "quality": "poor",
            "should_fallback": True,
            "reasons": ["empty_ocr_text"],
            "text_length": 0,
            "header_hits": 0,
            "numeric_density": 0.0,
            "noise_ratio": 0.0,
        }

    alnum = sum(1 for ch in cleaned if ch.isalnum())
    symbols = sum(
        1 for ch in cleaned if not ch.isalnum() and not ch.isspace()
    )
    noise_ratio = symbols / max(len(cleaned), 1)
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    meaningful = [
        ln
        for ln in lines
        if sum(1 for ch in ln if ch.isalpha()) >= 3
    ]

    upper = cleaned.upper()
    header_hits = 0
    for hint in _STOCK_HEADER_HINTS:
        if hint in upper or _normalize_token(hint) in _normalize_token(upper[:800]):
            # Count each distinct hint once.
            if re.search(rf"\b{re.escape(hint)}\b", upper) or hint in {
                "OPN",
                "OPSTK",
                "CLSTK",
            }:
                header_hits += 1
            elif len(hint) >= 4 and hint in upper:
                header_hits += 1

    # Tolerant OCR header forms
    if re.search(r"OPE?X?N?I?N?G|OPSTK|OP\.?\s*BAL", upper):
        header_hits = max(header_hits, header_hits + 1)
    if re.search(r"RECE|PURCH|RCPT", upper):
        header_hits = max(header_hits, 1)
    if re.search(r"CLOS|CL\.?\s*STK|CLSTK", upper):
        header_hits = max(header_hits, 1)

    digits = sum(1 for ch in cleaned if ch.isdigit())
    numeric_density = digits / max(alnum, 1)

    reasons: List[str] = []
    if len(cleaned) < 40 or alnum < 25:
        reasons.append("empty_ocr_text")
    if noise_ratio > 0.45:
        reasons.append("high_ocr_noise")
    if header_hits < 2 and len(cleaned) > 80:
        reasons.append("missing_stock_headers")
    if len(meaningful) < 3 and len(cleaned) > 40:
        reasons.append("too_few_product_rows")
    if numeric_density < 0.05 and len(cleaned) > 120:
        reasons.append("missing_numeric_values")

    score = 100
    score -= min(50, max(0, 40 - len(cleaned) // 10))
    score -= int(min(40, noise_ratio * 80))
    if header_hits < 2:
        score -= 25
    if len(meaningful) < 5:
        score -= 15
    if numeric_density < 0.08:
        score -= 15
    score = max(0, min(100, score))
    threshold = quality_threshold()
    if score < threshold and "empty_ocr_text" not in reasons:
        # Low score alone is enough for source-text gate.
        reasons.append("low_ocr_quality_score")

    return {
        "score": score,
        "quality": "poor" if reasons else "good",
        "should_fallback": bool(reasons) or score < threshold,
        "reasons": reasons,
        "text_length": len(cleaned),
        "header_hits": header_hits,
        "numeric_density": round(numeric_density, 4),
        "noise_ratio": round(noise_ratio, 4),
    }


def assess_extraction_quality(
    result: Optional[Dict[str, Any]],
    source_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Alias for evaluate_extraction_quality (centralized quality gate API)."""
    return evaluate_extraction_quality(result, source_metadata)


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
    threshold = quality_threshold()

    kinds = [_name_kind(str(item.get("product_name") or "")) for item in items]
    valid_names = sum(1 for kind in kinds if kind == "ok")
    noise_names = sum(1 for kind in kinds if kind == "noise")
    header_rows = _header_as_product_count(items)
    numeric_rows = 0
    nonzero_movement = 0
    for item in items:
        qty_fields = (
            item.get("opening_qty"),
            item.get("receipts_qty"),
            item.get("sales_qty"),
            item.get("closing_qty"),
        )
        if any(_number(value) is not None for value in qty_fields):
            numeric_rows += 1
        if any((_number(value) or 0.0) != 0.0 for value in qty_fields):
            nonzero_movement += 1
        if (_number(item.get("sales_value")) or 0.0) != 0.0:
            nonzero_movement += 1
        if (_number(item.get("closing_value")) or 0.0) != 0.0:
            nonzero_movement += 1

    name_pct = (100.0 * valid_names / len(items)) if items else 0.0
    noise_pct = (100.0 * noise_names / len(items)) if items else 0.0
    numeric_pct = (100.0 * numeric_rows / len(items)) if items else 0.0

    reasons: List[str] = []
    weak_method = method in _GENERIC_METHODS or method in _WEAK_OCR_METHODS
    # Clear corruption always overrides named-parser protection.
    hard_fail = False
    if not items:
        reasons.append("empty_line_items")
        hard_fail = True
    if items and header_rows >= max(1, int(0.08 * len(items))):
        reasons.append("header_as_product_rows")
        hard_fail = True
    if (
        weak_method
        and items
        and len(items) >= 5
        and nonzero_movement == 0
        and int(meta.get("page_count") or 0) >= 1
        and meta.get("source_has_visible_data")
    ):
        # Only when caller marked the document as visibly non-empty.
        reasons.append("all_zero_suspicious")
        hard_fail = True

    protected = (
        method not in _GENERIC_METHODS
        and method not in _WEAK_OCR_METHODS
        and valid_names >= min_rows
        and not hard_fail
    )
    extra = ((result.get("totals") or {}).get("extra") or {})
    fail_count = extra.get("stock_identity_fail_count")
    layout = str(extra.get("layout") or "")
    # Opening/Receive/Issue/Closing photos must not pass as good while rows fail.
    identity_gate = identity_limit
    if (
        method == "main_stock_sales_statement"
        or layout == "opening_receive_issue_closing"
    ):
        identity_gate = 0.0
    if (
        protected
        and items
        and _identity_applies(items, extra)
        and fail_count is not None
    ):
        fail_pct = 100.0 * float(fail_count) / len(items)
        if fail_pct > identity_gate:
            protected = False
            reasons.append("stock_identity_failure")
            hard_fail = True
    if protected:
        return {
            "quality": "good",
            "score": 100,
            "should_fallback": False,
            "reasons": [],
            "parser": method,
        }

    if items and not hard_fail:
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

        if _identity_applies(items, extra) and fail_count is not None:
            fail_pct = 100.0 * float(fail_count) / len(items)
            if fail_pct > identity_gate:
                if "stock_identity_failure" not in reasons:
                    reasons.append("stock_identity_failure")

    # Optional source-text assessment from metadata.
    source_text = meta.get("ocr_text") or meta.get("source_text")
    if isinstance(source_text, str) and source_text.strip() and (
        weak_method or not items
    ):
        source_q = assess_source_text_quality(source_text)
        if source_q.get("should_fallback"):
            for reason in source_q.get("reasons") or []:
                if reason not in reasons:
                    reasons.append(reason)

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
        if "header_as_product_rows" in reasons:
            score -= 35
        if "all_zero_suspicious" in reasons:
            score -= 30
        score = max(0, min(100, score))

    if (
        score < threshold
        and (weak_method or hard_fail)
        and "low_ocr_quality_score" not in reasons
    ):
        reasons.append("low_ocr_quality_score")

    should_fallback = bool(reasons)
    # Empty extract always falls back when Vision can see the file.
    if not items:
        should_fallback = True

    return {
        "quality": "poor" if should_fallback else "good",
        "score": score,
        "should_fallback": should_fallback,
        "reasons": reasons,
        "parser": method or "generic",
    }
