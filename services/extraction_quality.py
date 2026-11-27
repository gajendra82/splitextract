"""Quality gate for secondary-sales extraction.

Existing parsers stay primary. This module only decides whether a result is
trustworthy. It does not rewrite quantities or call a model.

Named format parsers remain protected unless the extract shows clear
OCR/header corruption (header-as-product rows, empty items, etc.).
Weak OCR/heuristic methods always receive full quality scoring.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Generic / weak readers may be incomplete. Named format parsers stay primary.
_GENERIC_METHODS = {
    "",
    "gemini_vision",
    "gemini_extraction_fallback",
    "txt_stock_fallback",
    "legacy_doc_text",
    "legacy_doc_text_unverified",
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
    "legacy_doc_text_unverified",
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


def _identity_applies(
    items: List[Dict[str, Any]],
    extra: Dict[str, Any],
    *,
    veto_active: bool = False,
) -> bool:
    """The simple opening/receipt/sales formula only applies when those columns are used.

    When Phase-1 veto is active, every stock_identity_kind is eligible. For kinds
    that historically trusted printed closing, rows without purchase/sales are
    treated as MISSING (not failure) — see veto_decision.
    """
    if extra.get("stock_identity_fail_count") is None and not veto_active:
        return False
    kind = str(extra.get("stock_identity_kind") or "")
    # Layout-specific identities still score when fail_count is present.
    _identity_kinds = {
        "",
        "opening_receipts_sales_closing",
        "open_pur_ret_sales_close",
    }
    if not veto_active and kind not in _identity_kinds:
        return False
    # Validator already counted failures for this formula — always score them when
    # the sheet actually uses opening/receipts/sales. Pure closing-only sheets
    # (all openings/sales/receipts zero) stay exempt.
    try:
        fail_count = int(extra.get("stock_identity_fail_count") or 0)
    except (TypeError, ValueError):
        fail_count = 0
    if veto_active:
        # Phase 1: every stock_identity_kind is eligible; printed-closing kinds
        # without purchase/sales are handled in veto_decision (MISSING, not fail).
        return True
    if fail_count > 0:
        has_movement = any(
            (_number(item.get("opening_qty")) or 0.0) > 0
            or (_number(item.get("sales_qty")) or 0.0) > 0
            or (_number(item.get("receipts_qty")) or 0.0) > 0
            for item in items
        )
        if has_movement:
            return True
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


def _score_financial_extraction(result: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """Independent financial score — aggregates only; null ≠ zero."""
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    detected = list(extra.get("footer_detected_columns") or [])
    missing = list(extra.get("footer_financial_missing_columns") or [])
    footer = extra.get("footer_financials") if isinstance(extra.get("footer_financials"), dict) else {}
    qty_only = bool(extra.get("qty_only"))

    # Detected source columns from footer, else infer from present aggregate keys.
    if not detected and footer.get("detected_columns"):
        detected = list(footer["detected_columns"])
    if not detected:
        for key in (
            "opening_value",
            "inward_value",
            "receipts_value",
            "sales_value",
            "outward_value",
            "closing_value",
            "lms_value",
        ):
            if totals.get(key) not in (None, "") or extra.get(key) not in (None, ""):
                detected.append(key)

    present = 0
    for col in detected:
        if col == "opening_value" and (
            totals.get("opening_value") not in (None, "")
            or extra.get("opening_value") not in (None, "")
        ):
            present += 1
        elif col in {"inward_value", "receipts_value", "purchase_value"} and (
            extra.get("inward_value") not in (None, "")
            or extra.get("receipts_value") not in (None, "")
            or extra.get("purchase_value") not in (None, "")
            or totals.get("receipts_value") not in (None, "")
        ):
            present += 1
        elif col in {"outward_value", "sales_value"} and (
            totals.get("sales_value") not in (None, "")
            or extra.get("outward_value") not in (None, "")
            or extra.get("sales_value") not in (None, "")
        ):
            present += 1
        elif col == "closing_value" and (
            totals.get("closing_value") not in (None, "")
            or extra.get("closing_value") not in (None, "")
        ):
            present += 1
        elif col == "lms_value" and extra.get("lms_value") not in (None, ""):
            present += 1

    # Prefer explicit footer completeness when the extractor stamped it.
    if extra.get("footer_financial_complete") is True and not missing:
        return {
            "score": 100,
            "detected_columns": detected
            or list(extra.get("footer_detected_columns") or []),
            "present_columns": present or len(detected),
            "missing_columns": [],
            "complete": True,
            "qty_only": qty_only,
            "source": extra.get("footer_financial_source") or footer.get("source"),
        }

    if not detected:
        # Qty-only sheets without a Values footer: financial N/A, not a fail.
        score = 100 if qty_only else 50
        return {
            "score": score,
            "detected_columns": [],
            "present_columns": present,
            "missing_columns": missing,
            "complete": qty_only,
            "qty_only": qty_only,
        }

    score = int(round(100.0 * present / max(len(detected), 1)))
    if missing:
        score = min(score, max(0, 100 - 20 * len(missing)))
    if extra.get("footer_financial_complete") is False:
        score = min(score, 70)
    complete = bool(
        (not missing and present >= len(detected))
        or extra.get("footer_financial_complete") is True
    )
    return {
        "score": max(0, min(100, score)),
        "detected_columns": detected,
        "present_columns": present,
        "missing_columns": missing,
        "complete": complete,
        "qty_only": qty_only,
        "source": extra.get("footer_financial_source") or footer.get("source"),
    }


def _score_quantity_extraction(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Independent quantity completeness across opening/receipts/sales/closing.

    Rows whose field_source marks every qty as missing (printed blank product
    lines) are completeness-neutral — they are not treated as failed numerics.
    """
    if not items:
        return {"score": 0, "numeric_pct": 0.0, "movement_pct": 0.0}
    numeric_rows = 0
    movement_rows = 0
    scored_rows = 0
    blank_rows = 0
    for item in items:
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}
        qty_keys = ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
        if fs and all(fs.get(k) == "missing" for k in qty_keys):
            blank_rows += 1
            continue
        scored_rows += 1
        qty_fields = tuple(item.get(k) for k in qty_keys)
        if any(_number(value) is not None for value in qty_fields):
            numeric_rows += 1
        if any((_number(value) or 0.0) != 0.0 for value in qty_fields):
            movement_rows += 1
    denom = max(scored_rows, 1)
    numeric_pct = 100.0 * numeric_rows / denom
    movement_pct = 100.0 * movement_rows / denom
    if scored_rows == 0 and blank_rows > 0:
        score = 100
    else:
        score = int(round(0.7 * numeric_pct + 0.3 * min(100.0, movement_pct * 1.2)))
    return {
        "score": max(0, min(100, score)),
        "numeric_pct": round(numeric_pct, 2),
        "movement_pct": round(movement_pct, 2),
        "blank_printed_rows": blank_rows,
        "scored_rows": scored_rows,
    }


def _score_identity_extraction(
    items: List[Dict[str, Any]],
    extra: Dict[str, Any],
    *,
    veto_active: bool,
    veto_info: Dict[str, Any],
    identity_gate: float,
) -> Dict[str, Any]:
    """Independent stock-identity score."""
    fail_count = extra.get("stock_identity_fail_count")
    applies = _identity_applies(items, extra, veto_active=veto_active)
    # Short OP/PI document-gap sheets record discrepancies separately; the
    # OP+PI−Sale formula is not an extract identity gate.
    if (
        str(extra.get("extraction_method") or "") == "stock_sales_op_pi_clqty"
        or extra.get("stock_document_gap")
    ):
        return {
            "score": 100,
            "applies": False,
            "fail_count": int(fail_count or 0),
            "fail_pct": 0.0,
            "document_gap": True,
            "discrepancy_count": int(
                extra.get("stock_reconciliation_discrepancy_count")
                or extra.get("stock_identity_discrepancy_count")
                or 0
            ),
        }
    if fail_count is None or not items or not applies:
        return {
            "score": 100 if not applies else 80,
            "applies": applies,
            "fail_count": fail_count,
            "fail_pct": None,
        }
    fail_pct = 100.0 * float(fail_count) / max(len(items), 1)
    if veto_active and veto_info.get("veto"):
        score = 0
    elif fail_pct <= max(0.0, identity_gate):
        score = int(round(100.0 - fail_pct))
    else:
        score = max(0, int(round(70.0 - fail_pct)))
    return {
        "score": max(0, min(100, score)),
        "applies": True,
        "fail_count": fail_count,
        "fail_pct": round(fail_pct, 2),
    }


def _score_row_completeness(
    items: List[Dict[str, Any]],
    *,
    name_pct: float,
    noise_pct: float,
    header_rows: int,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Independent product-row / identity-of-rows score."""
    if not items:
        return {"score": 0, "name_pct": 0.0, "noise_pct": 0.0, "header_rows": 0}
    extra = extra or {}
    score = 100
    score -= int(max(0.0, 50 - name_pct))
    score -= int(max(0.0, noise_pct - 30))
    if header_rows:
        score -= min(40, header_rows * 8)
    dup = _duplicate_percent(items)
    if len(items) >= 6 and dup > 40:
        score -= 20

    source_rows = extra.get("source_product_row_count")
    extracted_rows = extra.get("extracted_product_row_count")
    if extracted_rows is None:
        extracted_rows = len(items)
    missing_source_rows = 0
    try:
        if source_rows is not None and int(source_rows) > int(extracted_rows):
            missing_source_rows = int(source_rows) - int(extracted_rows)
            score -= min(40, missing_source_rows * 4)
    except (TypeError, ValueError):
        missing_source_rows = 0

    # Shadow classifier: blank printed rows are MISSING_VALUE (acceptable).
    # Only assignment/OCR/structural suspects should hurt completeness.
    shadow = extra.get("shadow_row_classification")
    valid_pct = None
    suspect_pct = None
    column_assignment_count = 0
    if isinstance(shadow, dict):
        counts = shadow.get("row_status_counts") or {}
        if isinstance(counts, dict) and items:
            acceptable = (
                int(counts.get("VALID") or 0)
                + int(counts.get("MISSING_VALUE") or 0)
                + int(counts.get("MINOR_DISCREPANCY") or 0)
                + int(counts.get("CLOSING_DERIVED") or 0)
            )
            column_assignment_count = int(
                counts.get("COLUMN_ASSIGNMENT_SUSPECTED") or 0
            )
            suspects = (
                column_assignment_count
                + int(counts.get("OCR_VALUE_SUSPECTED") or 0)
                + int(counts.get("STRUCTURAL_ERROR") or 0)
            )
            valid_pct = 100.0 * acceptable / len(items)
            suspect_pct = 100.0 * suspects / len(items)
            # Confirmed column-assignment errors must never leave a perfect
            # completeness score — even a single flagged row is a discrepancy.
            if column_assignment_count > 0:
                score -= min(40, 15 + column_assignment_count * 5)
            elif suspect_pct > 10:
                score -= int(min(40, round(suspect_pct)))
            elif valid_pct < 85:
                score -= int(min(25, round(85 - valid_pct)))

    return {
        "score": max(0, min(100, score)),
        "name_pct": round(name_pct, 2),
        "noise_pct": round(noise_pct, 2),
        "header_rows": header_rows,
        "duplicate_pct": round(dup, 2),
        "row_count": len(items),
        "source_row_count": source_rows,
        "missing_source_rows": missing_source_rows,
        "valid_row_pct": round(valid_pct, 2) if valid_pct is not None else None,
        "suspect_row_pct": round(suspect_pct, 2) if suspect_pct is not None else None,
        "column_assignment_count": column_assignment_count,
    }


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
            "dimension_scores": {
                "financial": {"score": 0},
                "quantity": {"score": 0},
                "identity": {"score": 0},
                "row_completeness": {"score": 0},
            },
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

    extra = ((result.get("totals") or {}).get("extra") or {})
    fail_count = extra.get("stock_identity_fail_count")
    layout = str(extra.get("layout") or "")

    # Phase 1: row-classifier veto before protected-parser score 100.
    veto_active = False
    veto_info: Dict[str, Any] = {}
    try:
        from services.stock_row_classifier import (
            infer_input_type,
            veto_active_for,
            veto_decision,
        )

        input_type = infer_input_type(
            str(meta.get("ext") or ""),
            meta,
        )
        veto_active = veto_active_for(input_type)
        if veto_active:
            veto_info = veto_decision(result, input_type)
            if veto_info.get("veto"):
                hard_fail = True
                if "IDENTITY_VETO" not in reasons:
                    reasons.append("IDENTITY_VETO")
                if "stock_identity_failure" not in reasons:
                    reasons.append("stock_identity_failure")
    except Exception:
        veto_active = False

    protected = (
        method not in _GENERIC_METHODS
        and method not in _WEAK_OCR_METHODS
        and valid_names >= min_rows
        and not hard_fail
    )
    # Opening/Receive/Issue/Closing photos must not pass as good while rows fail.
    identity_gate = identity_limit
    if (
        method == "main_stock_sales_statement"
        or layout == "opening_receive_issue_closing"
    ):
        identity_gate = 0.0
    # Vetoed input types use classifier thresholds, not the 20% tolerance.
    if veto_active:
        identity_gate = -1.0  # any fail_count > 0 fails when identity applies
    # Native Op.Qty/Op.Val/…/Br.Trf words parses: many printed rows never balance
    # (Br.Trf. vs Cls.Qty). Keep column-accurate extract; do not Gemini-overwrite.
    _keep_on_identity = method in {
        "stock_sales_op_qty_val_words",
        "stock_sales_consolidated_opval",
        # Stock and Sales Detail: a few printed rows are off-by-1/2 on the
        # sheet itself; keep header-aligned Cr/Db columns, do not Gemini-overwrite.
        "stock_sales_detail_opval",
        "docx_stock_sales_detail",
        # Short OP/PI/Sale/CLQty XLSX: printed CLQty often ≠ OP+PI−Sale
        # (hidden transfers). Keep openpyxl rows; discrepancies are reported
        # via recon counters, not as extract corruption.
        "stock_sales_op_pi_clqty",
    } and os.getenv(
        "STOCK_OP_QTY_VAL_KEEP_ON_IDENTITY_FAIL", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}
    # Purchase Free / Sales Free / Exp text PDFs: printed closing often does
    # not subtract Exp/Dmg. Keep the word-aligned rows; do not Gemini-overwrite.
    # Split-column OPSTK|PURC|SALE|STOCK pages: TRFIN/TRFOUT exist only as
    # statement totals, so many printed rows fail Opening+Purc−Sale=Stock
    # while header assignment is correct — keep native stitch, no Gemini rewrite.
    _keep_pdf_words = method in {
        "pdf_words_free_columns",
        "pdf_split_column_pages",
    } and os.getenv(
        "STOCK_PDF_WORDS_FREE_COLUMNS_KEEP", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if (
        protected
        and items
        and _identity_applies(items, extra, veto_active=veto_active)
        and fail_count is not None
    ):
        fail_pct = 100.0 * float(fail_count) / len(items)
        if veto_active:
            if veto_info.get("veto") or fail_pct > 0:
                protected = False
                if "stock_identity_failure" not in reasons:
                    reasons.append("stock_identity_failure")
                hard_fail = True
        elif fail_pct > identity_gate:
            if _keep_on_identity or _keep_pdf_words:
                logger.info(
                    "STOCK_OP_QTY_VAL_KEEP method=%s fail_pct=%.1f "
                    "reason=keep_native_skip_gemini_identity_fallback",
                    method,
                    fail_pct,
                )
            else:
                protected = False
                reasons.append("stock_identity_failure")
                hard_fail = True

    # Independent dimension scores (always computed).
    financial_dim = _score_financial_extraction(result, extra if isinstance(extra, dict) else {})
    quantity_dim = _score_quantity_extraction(items)
    identity_dim = _score_identity_extraction(
        items,
        extra if isinstance(extra, dict) else {},
        veto_active=veto_active,
        veto_info=veto_info,
        identity_gate=identity_gate,
    )
    row_dim = _score_row_completeness(
        items,
        name_pct=name_pct,
        noise_pct=noise_pct,
        header_rows=header_rows,
        extra=extra if isinstance(extra, dict) else {},
    )
    dimension_scores = {
        "financial": financial_dim,
        "quantity": quantity_dim,
        "identity": identity_dim,
        "row_completeness": row_dim,
    }

    # Footer detected but incomplete → surface as reason (does not alone veto natives).
    # Never claim missing_footer_financials when the extractor stamped complete=true.
    footer_complete_flag = (
        extra.get("footer_financial_complete")
        if isinstance(extra, dict)
        else None
    )
    if (
        financial_dim.get("detected_columns")
        and not financial_dim.get("complete")
        and footer_complete_flag is not True
    ):
        if "missing_footer_financials" not in reasons:
            reasons.append("missing_footer_financials")
    elif footer_complete_flag is True and "missing_footer_financials" in reasons:
        reasons = [r for r in reasons if r != "missing_footer_financials"]

    try:
        _fail_n = int(fail_count) if fail_count is not None else 0
    except (TypeError, ValueError):
        _fail_n = 0
    _short_op_pi_doc_gap = (
        method == "stock_sales_op_pi_clqty"
        or bool(extra.get("stock_document_gap"))
        if isinstance(extra, dict)
        else method == "stock_sales_op_pi_clqty"
    )
    if (
        _fail_n > 0
        and "stock_identity_failure" not in reasons
        and not _short_op_pi_doc_gap
    ):
        reasons.append("stock_identity_failure")
    if int(row_dim.get("missing_source_rows") or 0) > 0:
        if "missing_source_rows" not in reasons:
            reasons.append("missing_source_rows")
    valid_row_pct = row_dim.get("valid_row_pct")
    suspect_row_pct = row_dim.get("suspect_row_pct")
    column_assignment_count = int(row_dim.get("column_assignment_count") or 0)
    if column_assignment_count > 0:
        if "column_assignment_suspected" not in reasons:
            reasons.append("column_assignment_suspected")
    if suspect_row_pct is not None and float(suspect_row_pct) > 15:
        if "low_valid_row_rate" not in reasons:
            reasons.append("low_valid_row_rate")
    elif valid_row_pct is not None and float(valid_row_pct) < 70:
        if "low_valid_row_rate" not in reasons:
            reasons.append("low_valid_row_rate")

    # Perfect score is forbidden when identity / completeness / financial dims fail.
    dim_floor = min(
        int(financial_dim.get("score") or 100),
        int(quantity_dim.get("score") or 100),
        int(identity_dim.get("score") or 100),
        int(row_dim.get("score") or 100),
    )
    imperfect = (
        _fail_n > 0
        or int(row_dim.get("missing_source_rows") or 0) > 0
        or column_assignment_count > 0
        or (suspect_row_pct is not None and float(suspect_row_pct) > 5)
        or (
            financial_dim.get("detected_columns")
            and not financial_dim.get("complete")
        )
        or dim_floor < 100
    )

    if protected and not (veto_active and veto_info.get("veto")):
        out = {
            "quality": "good",
            "score": 100,
            "should_fallback": False,
            "reasons": [],
            "parser": method,
            "dimension_scores": dimension_scores,
        }
        if imperfect:
            # Named parsers stay primary (no Gemini fallback) but cannot claim
            # a perfect score when validation / completeness is incomplete.
            out["score"] = max(0, min(99, dim_floor))
            out["reasons"] = list(reasons)
            out["quality"] = "good" if out["score"] >= quality_threshold() else "poor"
            # Only force fallback on hard corruption — not mere identity misses
            # on otherwise column-accurate native extracts.
            out["should_fallback"] = bool(hard_fail)
        if financial_dim.get("detected_columns") and not financial_dim.get("complete"):
            if "missing_footer_financials" not in out["reasons"]:
                out["reasons"] = list(out.get("reasons") or []) + [
                    "missing_footer_financials"
                ]
            if not financial_dim.get("source"):
                out["should_fallback"] = True
                out["quality"] = "poor"
                out["score"] = min(int(out.get("score") or 100), int(financial_dim.get("score") or 0))
        return out

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

        if _identity_applies(items, extra, veto_active=veto_active) and fail_count is not None:
            fail_pct = 100.0 * float(fail_count) / len(items)
            if (veto_active and (veto_info.get("veto") or fail_pct > 0)) or (
                not veto_active and fail_pct > identity_gate
            ):
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
        if "IDENTITY_VETO" in reasons:
            score -= 30
        if "too_few_product_rows" in reasons or "duplicate_rows" in reasons:
            score -= 20
        if "invalid_period" in reasons:
            score -= 10
        if "header_as_product_rows" in reasons:
            score -= 35
        if "all_zero_suspicious" in reasons:
            score -= 30
        if "missing_footer_financials" in reasons:
            score -= max(0, 25 - int((financial_dim.get("score") or 0) // 4))
        score = max(0, min(100, score))

    # Phase 4a: failed soffice convert → never KEEP_PARSER at score 100.
    if method == "legacy_doc_text_unverified":
        score = min(score, 50)
        if "legacy_doc_text_unverified" not in reasons:
            reasons.append("legacy_doc_text_unverified")

    if (
        score < threshold
        and (weak_method or hard_fail or method == "legacy_doc_text_unverified")
        and "low_ocr_quality_score" not in reasons
    ):
        reasons.append("low_ocr_quality_score")

    should_fallback = bool(reasons)
    # Empty extract always falls back when Vision can see the file.
    if not items:
        should_fallback = True
    # Missing footer money alone should not force Gemini when rows/qty are solid
    # and a Values band was never printed (qty_only without detected columns).
    if (
        reasons == ["missing_footer_financials"]
        and financial_dim.get("qty_only")
        and not financial_dim.get("detected_columns")
    ):
        should_fallback = False
        reasons = []

    return {
        "quality": "poor" if should_fallback else "good",
        "score": score,
        "should_fallback": should_fallback,
        "reasons": reasons,
        "parser": method or "generic",
        "dimension_scores": dimension_scores,
    }
