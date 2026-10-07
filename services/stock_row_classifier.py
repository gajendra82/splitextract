"""Log-only stock row identity classifier (Phase 0).

Pure functions only: classify rows against stock identity and propose
candidates. Never mutates line items or rewrites printed values.
"""

from __future__ import annotations

import copy
import os
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple


class RowStatus(str, Enum):
    VALID = "VALID"
    MINOR_DISCREPANCY = "MINOR_DISCREPANCY"
    CLOSING_DERIVED = "CLOSING_DERIVED"
    COLUMN_ASSIGNMENT_SUSPECTED = "COLUMN_ASSIGNMENT_SUSPECTED"
    OCR_VALUE_SUSPECTED = "OCR_VALUE_SUSPECTED"
    MISSING_VALUE = "MISSING_VALUE"
    STRUCTURAL_ERROR = "STRUCTURAL_ERROR"


@dataclass
class RepairProposal:
    """Suppressed arithmetic repair (STOCK_NO_REWRITE) before a line_item exists."""

    source: str
    fields: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""


# Canonical qty fields used by the identity formula, in typical print order.
PRINTED_QTY_ORDER: Tuple[str, ...] = (
    "opening",
    "purchase",
    "free_in",
    "sales",
    "free_out",
    "sales_return",
    "purchase_return",
    "expiry_damage",
    "closing",
)

# Alias table: any of these keys on the line item or item["extra"] maps to a
# canonical field. First hit wins per canonical (top-level preferred by caller
# order in read_row_fields).
FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "opening": (
        "opening_qty",
        "opening",
        "op_qty",
        "opn_qty",
        "opstk",
    ),
    "purchase": (
        "receipts_qty",
        "purchase_qty",
        "receipts",
        "purchase",
        "pur_qty",
        "purch_qty",
    ),
    "free_in": (
        "free_in_qty",
        "free_receipt_qty",
        "scheme_in_qty",
    ),
    "sales": (
        "sales_qty",
        "sales",
        "sale_qty",
        "issue_qty",
        "issue",
    ),
    "free_out": (
        "free_out_qty",
        "sales_scheme_qty",
        "sales_scheme",
        "scheme_qty",
        "free",
        "sample_qty",
        "free_qty",
    ),
    "sales_return": (
        "sales_return_qty",
        "sale_return_qty",
        "sale_return",
        "saleret",
        "sret",
        "sales_return",
    ),
    "purchase_return": (
        "purchase_return_qty",
        "pret_qty",
        "pret",
        "pur_return",
    ),
    "expiry_damage": (
        "expiry_damage_qty",
        "expiry_damage",
        "exp_damage",
        "exp_dmg",
        "expdmg",
        "breakage_qty",
        "shortage_qty",
    ),
    "closing": (
        "closing_qty",
        "closing",
        "close_qty",
        "closstock",
        "bal_qty",
        "balance_qty",
    ),
    "total": (
        "total_qty",
        "total_stock",
        "total",
    ),
}

_DIGIT_CONFUSIONS = {
    ("1", "7"),
    ("7", "1"),
    ("0", "8"),
    ("8", "0"),
    ("5", "6"),
    ("6", "5"),
    ("3", "8"),
    ("8", "3"),
    ("2", "7"),
    ("7", "2"),
}

_TOTAL_ROW_NAME = re.compile(
    r"(?i)^\s*(?:grand\s*)?(?:sub\s*)?totals?\b|^\s*total\s*(?:qty|value|amount)?\s*$"
)


def stock_row_classifier_shadow_enabled() -> bool:
    """Phase 0 shadow classifier. Default ON (log-only, no routing change)."""
    value = os.getenv("STOCK_ROW_CLASSIFIER_SHADOW")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "on"}


def stock_identity_veto_enabled() -> bool:
    """Phase 1 identity veto. Default OFF — no production behaviour change."""
    value = os.getenv("STOCK_IDENTITY_VETO", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def stock_identity_veto_types() -> set:
    """Input types the veto applies to. Default image + scanned_pdf."""
    raw = os.getenv("STOCK_IDENTITY_VETO_TYPES", "image,scanned_pdf")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def veto_active_for(input_type: Optional[str]) -> bool:
    """True when the Phase-1 flag is on and this input type is in scope."""
    if not stock_identity_veto_enabled():
        return False
    kind = str(input_type or "").strip().lower()
    if not kind:
        return False
    return kind in stock_identity_veto_types()


# ---------------------------------------------------------------------------
# Phase 3a: STOCK_NO_REWRITE — repairs become candidates
# ---------------------------------------------------------------------------

_NO_REWRITE_TLS = threading.local()


def stock_no_rewrite_enabled() -> bool:
    return os.getenv("STOCK_NO_REWRITE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def apply_repairs() -> bool:
    """False when STOCK_NO_REWRITE is on (repairs become candidates only)."""
    return not stock_no_rewrite_enabled()


def _nr_bump(source: str) -> None:
    bag = getattr(_NO_REWRITE_TLS, "by_source", None)
    if bag is None:
        bag = {}
        _NO_REWRITE_TLS.by_source = bag
    key = str(source or "unknown")
    bag[key] = int(bag.get(key) or 0) + 1


def take_no_rewrite_counts() -> Dict[str, int]:
    bag = getattr(_NO_REWRITE_TLS, "by_source", None) or {}
    _NO_REWRITE_TLS.by_source = {}
    return {str(k): int(v) for k, v in bag.items()}


def reset_no_rewrite_counts() -> None:
    _NO_REWRITE_TLS.by_source = {}


def record_candidate(
    line_item: Dict[str, Any],
    proposed: Dict[str, Any],
    source: str,
    reason: str,
    *,
    column_absent_fields: Sequence[str] = (),
) -> None:
    """Append a repair proposal without mutating printed qty/value fields.

    Exception: fields listed in ``column_absent_fields`` (column missing from
    the document header, not merely an empty cell) may be filled when the rest
    of the row is VALID after filling; those get field_source=derived.
    """
    if not isinstance(line_item, dict):
        return
    extra = line_item.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        line_item["extra"] = extra

    proposed = {str(k): v for k, v in (proposed or {}).items()}
    absent = [str(f) for f in (column_absent_fields or ()) if f]

    # Optional fill for truly absent columns when the row becomes VALID.
    if absent:
        trial = copy.deepcopy(line_item)
        for field in absent:
            if field in proposed:
                if field.startswith("extra."):
                    key = field.split(".", 1)[1]
                    trial.setdefault("extra", {})[key] = proposed[field]
                else:
                    trial[field] = proposed[field]
        status_trial, _ = classify_row(read_row_fields(trial))
        if status_trial == RowStatus.VALID:
            fs = extra.setdefault("field_source", {})
            if not isinstance(fs, dict):
                fs = {}
                extra["field_source"] = fs
            for field in absent:
                if field not in proposed:
                    continue
                if field.startswith("extra."):
                    key = field.split(".", 1)[1]
                    line_item.setdefault("extra", {})[key] = proposed[field]
                    fs[key] = "derived"
                else:
                    line_item[field] = proposed[field]
                    fs[field] = "derived"

    # would_balance: would identity hold if proposed fields were applied?
    would_balance = False
    try:
        trial_b = copy.deepcopy(line_item)
        for field, value in proposed.items():
            if field.startswith("extra."):
                trial_b.setdefault("extra", {})[field.split(".", 1)[1]] = value
            else:
                trial_b[field] = value
        status_b, _ = classify_row(read_row_fields(trial_b))
        would_balance = status_b in {
            RowStatus.VALID,
            RowStatus.MINOR_DISCREPANCY,
        }
    except Exception:
        would_balance = False

    cands = extra.setdefault("candidates", [])
    if not isinstance(cands, list):
        cands = []
        extra["candidates"] = cands
    cands.append(
        {
            "source": str(source),
            "fields": proposed,
            "reason": str(reason),
            "would_balance": bool(would_balance),
        }
    )

    # row_status from UNCHANGED printed fields (after optional derived fills).
    try:
        status, _ = classify_row(read_row_fields(line_item))
        extra["row_status"] = status.value
    except Exception:
        extra["row_status"] = RowStatus.COLUMN_ASSIGNMENT_SUSPECTED.value

    _nr_bump(source)


# Kinds that historically trusted printed closing and forced fail_count=0.
_PRINTED_CLOSING_KINDS = frozenset(
    {
        "medica_opstk",
        "medica_opstk_columns",
        "medivision_op_purc_nm60d",
        "summary_rtl",
        "batchwise",
        "stock_valuation",
        "zl_opening_primary_closing",
        "zl_secondary_xlsx",
        "zl_opening_bal",
    }
)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
_SHEET_EXTS = {".xls", ".xlsx", ".xlsm"}
_TEXT_EXTS = {".txt", ".htm", ".html"}
_WORD_EXTS = {".doc", ".docx"}


def infer_input_type(
    ext: Optional[str] = None,
    source_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Classify the upload for STOCK_IDENTITY_VETO_TYPES filtering."""
    meta = source_metadata or {}
    hinted = str(meta.get("input_type") or "").strip().lower()
    if hinted:
        return hinted
    extension = (ext or str(meta.get("ext") or "")).lower()
    if not extension.startswith(".") and extension:
        extension = f".{extension}"
    if extension in _IMAGE_EXTS:
        return "image"
    if extension == ".pdf":
        if meta.get("image_only_pdf") or int(meta.get("embedded_chars") or 0) < 40:
            return "scanned_pdf"
        return "text_pdf"
    if extension in _SHEET_EXTS:
        return "spreadsheet"
    if extension in _TEXT_EXTS:
        return "text"
    if extension in _WORD_EXTS:
        return "word"
    return "other"


def gemini_call_budget(result: Optional[Dict[str, Any]] = None) -> int:
    """Default 2; honor totals.extra.gemini_budget when set (Phase 2b-2)."""
    if isinstance(result, dict):
        extra = ((result.get("totals") or {}).get("extra") or {})
        try:
            custom = int(extra.get("gemini_budget") or 0)
            if custom > 0:
                return custom
        except (TypeError, ValueError):
            pass
    return 2


def get_gemini_calls(result: Optional[Dict[str, Any]]) -> int:
    if not isinstance(result, dict):
        return 0
    extra = ((result.get("totals") or {}).get("extra") or {})
    try:
        return int(extra.get("gemini_calls") or 0)
    except (TypeError, ValueError):
        return 0


def bump_gemini_calls(result: Dict[str, Any], n: int = 1) -> int:
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    if not isinstance(extra, dict):
        return 0
    total = get_gemini_calls(result) + int(n)
    extra["gemini_calls"] = total
    return total


def veto_decision(result: Dict[str, Any], input_type: str) -> Dict[str, Any]:
    """Recompute row classification and decide whether to veto acceptance.

    Never reads totals.extra.shadow_row_classification.
    """
    empty = {
        "veto": False,
        "reason": "veto_inactive",
        "flagged_rows": [],
        "counts": {},
        "valid_ratio": 1.0,
    }
    if not veto_active_for(input_type):
        return empty

    classified = classify_result(result)
    counts = dict(classified.get("row_status_counts") or {})
    valid_ratio = float(classified.get("valid_ratio") or 0.0)
    flagged = [
        int(row.get("row_index"))
        for row in (classified.get("rows") or [])
        if str(row.get("status") or "")
        not in {
            RowStatus.VALID.value,
            RowStatus.MINOR_DISCREPANCY.value,
            RowStatus.CLOSING_DERIVED.value,
        }
    ]

    # Printed-closing kinds: MISSING_VALUE alone must not drive the veto when
    # purchase/sales columns are absent from the sheet.
    extra = ((result.get("totals") or {}).get("extra") or {}) if isinstance(result, dict) else {}
    kind = str(extra.get("stock_identity_kind") or extra.get("extraction_method") or "")
    if kind in _PRINTED_CLOSING_KINDS or any(
        token in kind for token in ("medica", "medivision", "zl_opening", "batchwise", "valuation")
    ):
        has_purchase_or_sales = False
        for item in _iter_line_items(result):
            fields = read_row_fields(item)
            if fields.get("purchase") is not None or fields.get("sales") is not None:
                has_purchase_or_sales = True
                break
        if not has_purchase_or_sales:
            # Recompute ratio ignoring MISSING_VALUE rows for veto purposes.
            usable = 0
            valid_like = 0
            flagged = []
            for row in classified.get("rows") or []:
                status = str(row.get("status") or "")
                if status == RowStatus.MISSING_VALUE.value:
                    continue
                usable += 1
                if status in {
                    RowStatus.VALID.value,
                    RowStatus.MINOR_DISCREPANCY.value,
                    RowStatus.CLOSING_DERIVED.value,
                }:
                    valid_like += 1
                else:
                    flagged.append(int(row.get("row_index")))
            if usable < 1:
                return {
                    "veto": False,
                    "reason": "printed_closing_missing_movement_columns",
                    "flagged_rows": [],
                    "counts": counts,
                    "valid_ratio": valid_ratio,
                }
            valid_ratio = float(valid_like) / float(usable)
            if valid_ratio >= 0.90 and not any(
                str(r.get("status")) == RowStatus.STRUCTURAL_ERROR.value
                for r in (classified.get("rows") or [])
            ):
                return {
                    "veto": False,
                    "reason": "printed_closing_ok",
                    "flagged_rows": flagged,
                    "counts": counts,
                    "valid_ratio": round(valid_ratio, 4),
                }

    would, reason = would_fallback(
        {
            **classified,
            "valid_ratio": round(valid_ratio, 4),
            "classified_rows": classified.get("classified_rows"),
        }
    )
    return {
        "veto": bool(would),
        "reason": reason if would else "ok",
        "flagged_rows": flagged,
        "counts": counts,
        "valid_ratio": round(valid_ratio, 4),
    }


def _coerce_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\u00a0", " ").replace(",", "")
    if not text or text.upper() in {
        "-",
        "—",
        "--",
        "NA",
        "N/A",
        "NULL",
        "NONE",
        "",
    }:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _lookup_alias(item: Dict[str, Any], aliases: Sequence[str]) -> Optional[float]:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    for name in aliases:
        if name in item and item.get(name) not in (None, ""):
            return _coerce_number(item.get(name))
        if isinstance(extra, dict) and name in extra and extra.get(name) not in (None, ""):
            return _coerce_number(extra.get(name))
    return None


def read_row_fields(line_item: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Map a line item to canonical floats (or None). Does not mutate.

    When ``extra.field_source`` marks a qty as ``missing``, the value is None
    even if the top-level key holds a legacy 0.0 boundary placeholder.
    """
    if not isinstance(line_item, dict):
        return {key: None for key in (*PRINTED_QTY_ORDER, "total")}
    extra = line_item.get("extra") if isinstance(line_item.get("extra"), dict) else {}
    fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}
    fields: Dict[str, Optional[float]] = {}
    for canonical, aliases in FIELD_ALIASES.items():
        if any(fs.get(name) == "missing" for name in aliases):
            fields[canonical] = None
            continue
        fields[canonical] = _lookup_alias(line_item, aliases)
    return fields


def expected_closing(fields: Dict[str, Optional[float]]) -> Optional[float]:
    """closing = opening + purchase + free_in + sales_return
    − sales − free_out − purchase_return − expiry_damage
    (only terms that are not None).

    When printed ``total`` is present it is used as the inward base.
    If total ≈ opening+purchase+sales_return (Total In), do not add
    sales_return again. If total ≈ opening+purchase only, add sales_return.
    """
    opening = fields.get("opening")
    purchase = fields.get("purchase")
    sales = fields.get("sales")
    total = fields.get("total")
    sales_return = fields.get("sales_return")
    free_in = fields.get("free_in")

    if total is not None:
        derived = float(total)
        open_pur = 0.0
        if opening is not None:
            open_pur += float(opening)
        if purchase is not None:
            open_pur += float(purchase)
        if free_in is not None:
            open_pur += float(free_in)
        sret = float(sales_return) if sales_return is not None else 0.0
        tol = _valid_tolerance(float(total), open_pur or float(total))
        in_total = abs(float(total) - (open_pur + sret)) <= tol and (
            abs(float(total) - open_pur) > tol or abs(sret) > tol
        )
        if abs(float(total) - (open_pur + sret)) < abs(float(total) - open_pur) - 1e-9:
            in_total = True
        if sales_return is not None and not in_total:
            derived += float(sales_return)
    else:
        if opening is None:
            return None
        if purchase is None and sales is None:
            return None
        derived = float(opening)
        if purchase is not None:
            derived += float(purchase)
        if free_in is not None:
            derived += float(free_in)
        if sales_return is not None:
            derived += float(sales_return)

    for sub_key in ("sales", "free_out", "purchase_return", "expiry_damage"):
        value = fields.get(sub_key)
        if value is not None:
            derived -= float(value)
    return round(derived, 4)


def total_identity_ok(fields: Dict[str, Optional[float]]) -> Optional[bool]:
    """When total is printed: total ≈ opening + purchase (+ free_in).

    Returns None when total (or opening) is absent so the check does not apply.
    """
    total = fields.get("total")
    opening = fields.get("opening")
    if total is None or opening is None:
        return None
    expected = float(opening)
    purchase = fields.get("purchase")
    if purchase is not None:
        expected += float(purchase)
    free_in = fields.get("free_in")
    if free_in is not None:
        expected += float(free_in)
    tol = _valid_tolerance(float(opening), float(total))
    return _diff_ok(expected, float(total), tol)


def _valid_tolerance(opening: float, closing: float) -> float:
    return max(1.0, 0.005 * max(abs(opening), abs(closing)))


def _minor_tolerance(opening: float, closing: float) -> float:
    return max(2.0, 0.02 * max(abs(opening), abs(closing)))


def _diff_ok(expected: float, closing: float, tolerance: float) -> bool:
    return abs(expected - closing) <= tolerance + 1e-9


def _adjacent_swap_candidates(
    fields: Dict[str, Optional[float]],
    closing: float,
    valid_tol: float,
) -> List[Dict[str, Any]]:
    present = [key for key in PRINTED_QTY_ORDER if key != "closing" and fields.get(key) is not None]
    candidates: List[Dict[str, Any]] = []
    for index in range(len(present) - 1):
        left, right = present[index], present[index + 1]
        trial = dict(fields)
        trial[left], trial[right] = trial[right], trial[left]
        expected = expected_closing(trial)
        if expected is None:
            continue
        if _diff_ok(expected, closing, valid_tol):
            candidates.append(
                {
                    "source": "adjacent_swap",
                    "fields": {left: trial[left], right: trial[right]},
                    "reason": f"swap_{left}_{right}",
                }
            )
    return candidates


def _format_qty(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text


def _parse_qty_text(text: str) -> Optional[float]:
    try:
        return float(text)
    except ValueError:
        return None


def _digit_edit_variants(value: float) -> List[float]:
    raw = _format_qty(value)
    if not raw or raw.startswith("-"):
        # Keep sign handling simple: only positive/zero edits for Phase 0.
        body = raw[1:] if raw.startswith("-") else raw
        sign = -1.0 if raw.startswith("-") else 1.0
    else:
        body = raw
        sign = 1.0
    digits = list(body)
    variants: List[float] = []

    def _push(candidate: str) -> None:
        if not candidate or candidate == "." or candidate == "-":
            return
        parsed = _parse_qty_text(candidate)
        if parsed is None:
            return
        variants.append(sign * parsed)

    # Drop one digit.
    for index, char in enumerate(digits):
        if not char.isdigit():
            continue
        dropped = "".join(digits[:index] + digits[index + 1 :])
        _push(dropped)

    # Insert one digit 0-9 at each position.
    for index in range(len(digits) + 1):
        for digit in "0123456789":
            inserted = "".join(digits[:index] + [digit] + digits[index:])
            _push(inserted)

    # Confusion pairs at each digit.
    for index, char in enumerate(digits):
        for left, right in _DIGIT_CONFUSIONS:
            if char != left:
                continue
            confused = list(digits)
            confused[index] = right
            _push("".join(confused))

    # Deduplicate while preserving order, exclude original.
    seen = {round(value, 6)}
    unique: List[float] = []
    for variant in variants:
        key = round(variant, 6)
        if key in seen:
            continue
        seen.add(key)
        unique.append(variant)
    return unique


def _ocr_value_candidates(
    fields: Dict[str, Optional[float]],
    closing: float,
    valid_tol: float,
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    editable = [key for key in PRINTED_QTY_ORDER if fields.get(key) is not None]
    for key in editable:
        original = fields[key]
        if original is None:
            continue
        for variant in _digit_edit_variants(original):
            trial = dict(fields)
            trial[key] = variant
            if key == "closing":
                # Expected from unchanged non-closing fields vs candidate closing.
                base_expected = expected_closing(fields)
                if base_expected is None:
                    continue
                if _diff_ok(base_expected, float(variant), valid_tol):
                    candidates.append(
                        {
                            "source": "ocr_digit_edit",
                            "fields": {key: variant},
                            "reason": f"digit_edit_{key}",
                        }
                    )
                    break
                continue
            expected = expected_closing(trial)
            if expected is None:
                continue
            if _diff_ok(expected, float(closing), valid_tol):
                candidates.append(
                    {
                        "source": "ocr_digit_edit",
                        "fields": {key: variant},
                        "reason": f"digit_edit_{key}",
                    }
                )
                break
    return candidates[:8]


def classify_row(
    fields: Dict[str, Optional[float]],
    *,
    structural_error: bool = False,
) -> Tuple[RowStatus, Dict[str, Any]]:
    """Classify one row. Candidates are returned, never applied.

    When closing is absent but opening, purchase and sales exist, returns
    CLOSING_DERIVED with ``info["derived_closing"]`` set — but only when the
    printed total identity (if present) holds. Callers may then write the
    derived closing onto the line item with field_source=derived.
    """
    info: Dict[str, Any] = {
        "diff": None,
        "tolerance": None,
        "candidates": [],
        "unexplained": False,
    }
    if structural_error:
        info["reason"] = "structural_error"
        return RowStatus.STRUCTURAL_ERROR, info

    opening = fields.get("opening")
    closing = fields.get("closing")
    purchase = fields.get("purchase")
    sales = fields.get("sales")
    total = fields.get("total")

    tot_ok = total_identity_ok(fields)
    if tot_ok is not None:
        info["total_identity_ok"] = bool(tot_ok)
        info["checks"] = list(info.get("checks") or []) + ["total=opening+purchase"]

    # Closing absent: derive when opening + purchase + sales are all present.
    if closing is None:
        if opening is None or purchase is None or sales is None:
            info["reason"] = "missing_required_field"
            return RowStatus.MISSING_VALUE, info
        if tot_ok is False:
            # Printed total does not match opening+purchase — do not trust derive.
            info["reason"] = "total_identity_failed"
            info["unexplained"] = True
            # Prefer OCR/column suspects on the total/open/purchase triple.
            trial = dict(fields)
            trial["closing"] = expected_closing(fields) or 0.0
            valid_tol = _valid_tolerance(float(opening), float(trial["closing"]))
            swap_candidates = _adjacent_swap_candidates(
                trial, float(trial["closing"]), valid_tol
            )
            if swap_candidates:
                info["candidates"] = swap_candidates
                return RowStatus.COLUMN_ASSIGNMENT_SUSPECTED, info
            ocr_candidates = _ocr_value_candidates(
                trial, float(trial["closing"]), valid_tol
            )
            if ocr_candidates:
                info["candidates"] = ocr_candidates
                return RowStatus.OCR_VALUE_SUSPECTED, info
            return RowStatus.COLUMN_ASSIGNMENT_SUSPECTED, info

        derived = expected_closing(fields)
        if derived is None:
            info["reason"] = "missing_required_field"
            return RowStatus.MISSING_VALUE, info
        info["derived_closing"] = derived
        info["reason"] = "closing_derived"
        info["checks"] = list(info.get("checks") or []) + ["closing_derived"]
        return RowStatus.CLOSING_DERIVED, info

    # SALE + CLOSING only sheets (no Opening / Receipt columns printed).
    # Opening+Receipt−Sales=Closing does not apply; keep printed sales/closing.
    if (
        opening is None
        and purchase is None
        and sales is not None
        and closing is not None
    ):
        info["reason"] = "sale_closing_only"
        info["checks"] = list(info.get("checks") or []) + ["sale_closing_only"]
        return RowStatus.VALID, info

    if opening is None or (purchase is None and sales is None):
        info["reason"] = "missing_required_field"
        return RowStatus.MISSING_VALUE, info

    expected = expected_closing(fields)
    if expected is None:
        info["reason"] = "missing_required_field"
        return RowStatus.MISSING_VALUE, info

    # When total is printed and fails, flag even if closing identity happens to hold.
    if tot_ok is False:
        info["reason"] = "total_identity_failed"
        info["expected_closing"] = expected
        info["diff"] = round(abs(expected - float(closing)), 4)
        return RowStatus.COLUMN_ASSIGNMENT_SUSPECTED, info

    diff = abs(expected - float(closing))
    valid_tol = _valid_tolerance(float(opening), float(closing))
    minor_tol = _minor_tolerance(float(opening), float(closing))
    info["diff"] = round(diff, 4)
    info["tolerance"] = valid_tol
    info["expected_closing"] = expected

    if _diff_ok(expected, float(closing), valid_tol):
        return RowStatus.VALID, info
    if _diff_ok(expected, float(closing), minor_tol):
        info["tolerance"] = minor_tol
        return RowStatus.MINOR_DISCREPANCY, info

    swap_candidates = _adjacent_swap_candidates(fields, float(closing), valid_tol)
    if swap_candidates:
        info["candidates"] = swap_candidates
        return RowStatus.COLUMN_ASSIGNMENT_SUSPECTED, info

    ocr_candidates = _ocr_value_candidates(fields, float(closing), valid_tol)
    if ocr_candidates:
        info["candidates"] = ocr_candidates
        return RowStatus.OCR_VALUE_SUSPECTED, info

    info["unexplained"] = True
    info["candidates"] = []
    return RowStatus.COLUMN_ASSIGNMENT_SUSPECTED, info


_IDENTITY_LOGGED: set = set()
_IDENTITY_LOG_LOCK = threading.Lock()


def log_identity_selected_once(
    request_id: str,
    printed_fields: Sequence[str],
    checks: Sequence[str],
) -> None:
    """Log IDENTITY_SELECTED once per request_id."""
    key = str(request_id or "-")
    with _IDENTITY_LOG_LOCK:
        if key in _IDENTITY_LOGGED:
            return
        _IDENTITY_LOGGED.add(key)
    import logging

    logging.getLogger(__name__).info(
        "IDENTITY_SELECTED request_id=%s fields=%s checks=%s",
        key,
        list(printed_fields),
        list(checks),
    )


def apply_closing_derived(
    line_item: Dict[str, Any],
    *,
    printed_fields: Optional[Sequence[str]] = None,
    request_id: str = "-",
) -> RowStatus:
    """Classify and, when CLOSING_DERIVED, write closing_qty with field_source=derived.

    Does not rewrite any printed qty. Only fills closing when the column was
    absent (not in printed_fields / field_source missing).
    """
    if not isinstance(line_item, dict):
        return RowStatus.STRUCTURAL_ERROR

    printed = [str(f) for f in (printed_fields or [])]
    if printed:
        log_identity_selected_once(
            request_id,
            printed,
            checks=["total=opening+purchase"] if "total_qty" in printed else [],
        )

    fields = read_row_fields(line_item)
    # Honour field_source: missing closing stays None even if top-level is 0.0.
    extra = line_item.get("extra") if isinstance(line_item.get("extra"), dict) else {}
    fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}
    if fs.get("closing_qty") == "missing" or (
        printed
        and "closing_qty" not in printed
        and "closing_qty" not in fs
    ):
        fields["closing"] = None

    # Prefer total from extra.total_stock when field_source says printed.
    if fields.get("total") is None and extra.get("total_stock") is not None:
        try:
            fields["total"] = float(extra["total_stock"])
        except (TypeError, ValueError):
            pass

    # Empty cells in a printed movement column count as 0 for derive/identity,
    # not "column absent". Column-absent fields stay None (not in printed).
    for canon_key, field_key in (
        ("purchase_qty", "purchase"),
        ("opening_qty", "opening"),
        ("sales_qty", "sales"),
        ("total_qty", "total"),
        ("free_in_qty", "free_in"),
        ("free_out_qty", "free_out"),
        ("sales_return_qty", "sales_return"),
        ("expiry_damage_qty", "expiry_damage"),
    ):
        if (
            printed
            and canon_key in printed
            and fields.get(field_key) is None
            and canon_key != "closing_qty"
        ):
            fields[field_key] = 0.0

    status, info = classify_row(fields)
    extra = line_item.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        line_item["extra"] = extra
    fs = extra.setdefault("field_source", {})
    if not isinstance(fs, dict):
        fs = {}
        extra["field_source"] = fs

    if status == RowStatus.CLOSING_DERIVED:
        derived = info.get("derived_closing")
        # When a Closing/Balance column is printed on the document, never invent
        # closing_qty from arithmetic — keep null and stash calculated only.
        if "closing_qty" in printed:
            if derived is not None:
                extra["calculated_closing_qty"] = float(derived)
            if fs.get("closing_qty") != "printed":
                fs["closing_qty"] = "missing"
                line_item["closing_qty"] = None
            extra["row_status"] = RowStatus.MISSING_VALUE.value
            return RowStatus.MISSING_VALUE
        if derived is not None and float(derived) >= -0.51:
            line_item["closing_qty"] = float(derived)
            fs["closing_qty"] = "derived"
            extra["row_status"] = RowStatus.CLOSING_DERIVED.value
        else:
            # Negative derived closing is not stock — keep missing.
            fs["closing_qty"] = "missing"
            line_item["closing_qty"] = None
            extra["row_status"] = RowStatus.COLUMN_ASSIGNMENT_SUSPECTED.value
    else:
        extra["row_status"] = status.value
        if status not in {
            RowStatus.VALID,
            RowStatus.MINOR_DISCREPANCY,
            RowStatus.CLOSING_DERIVED,
        }:
            # Do not present a placeholder closing as valid when flagged.
            if (
                printed
                and "closing_qty" not in printed
                and fs.get("closing_qty") != "printed"
            ):
                fs["closing_qty"] = "missing"
                line_item["closing_qty"] = None
    return status


def _is_total_row(item: Dict[str, Any]) -> bool:
    name = str(item.get("product_name") or "").strip()
    if not name:
        return False
    return bool(_TOTAL_ROW_NAME.search(name))


def _iter_line_items(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not isinstance(result, dict):
        return rows
    if result.get("multi_statement") and isinstance(result.get("statements"), list):
        for statement in result["statements"]:
            if not isinstance(statement, dict):
                continue
            for item in statement.get("line_items") or []:
                if isinstance(item, dict):
                    rows.append(item)
        return rows
    for item in result.get("line_items") or []:
        if isinstance(item, dict):
            rows.append(item)
    return rows


def classify_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Classify all product rows. Does not mutate ``result`` or its items."""
    counts = {status.value: 0 for status in RowStatus}
    row_details: List[Dict[str, Any]] = []
    items = _iter_line_items(result)
    for index, item in enumerate(items):
        if _is_total_row(item):
            continue
        fields = read_row_fields(item)
        status, info = classify_row(fields)
        counts[status.value] = counts.get(status.value, 0) + 1
        row_details.append(
            {
                "row_index": index,
                "status": status.value,
                "diff": info.get("diff"),
                "candidate_count": len(info.get("candidates") or []),
            }
        )

    classified = sum(counts.values())
    valid_like = (
        counts[RowStatus.VALID.value]
        + counts[RowStatus.MINOR_DISCREPANCY.value]
        + counts.get(RowStatus.CLOSING_DERIVED.value, 0)
    )
    valid_ratio = (float(valid_like) / float(classified)) if classified else 0.0
    return {
        "row_status_counts": counts,
        "valid_ratio": round(valid_ratio, 4),
        "classified_rows": classified,
        "rows": row_details,
    }


def would_fallback(summary: Dict[str, Any]) -> Tuple[bool, str]:
    """True when shadow classifier would want Gemini / re-read."""
    counts = summary.get("row_status_counts") or {}
    classified = int(summary.get("classified_rows") or sum(int(v) for v in counts.values()))
    if classified < 1:
        return True, "no_product_rows"
    if int(counts.get(RowStatus.STRUCTURAL_ERROR.value, 0) or 0) > 0:
        return True, "structural_error"
    valid_ratio = float(summary.get("valid_ratio") or 0.0)
    if valid_ratio < 0.90:
        return True, "valid_ratio_below_0.90"
    return False, "ok"


def shadow_summary_for_storage(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Counts + valid_ratio only (no per-row payload)."""
    return {
        "row_status_counts": dict(summary.get("row_status_counts") or {}),
        "valid_ratio": summary.get("valid_ratio"),
        "classified_rows": summary.get("classified_rows"),
    }


def classify_result_safe(result: Dict[str, Any]) -> Dict[str, Any]:
    """Classify a deep copy so callers cannot mutate through shared refs."""
    return classify_result(copy.deepcopy(result))


_SOFFICE_LOGGED = False


def log_soffice_availability_once() -> bool:
    """Log SOFFICE_AVAILABLE once at process startup (10s convert probe)."""
    global _SOFFICE_LOGGED
    import logging
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    logger = logging.getLogger(__name__)
    if _SOFFICE_LOGGED:
        return shutil.which("soffice") is not None
    _SOFFICE_LOGGED = True

    binary = shutil.which("soffice")
    if not binary:
        logger.info("SOFFICE_AVAILABLE=false reason=not_on_path")
        return False

    try:
        with tempfile.TemporaryDirectory(prefix="stock_soffice_") as tmp:
            tmp_path = Path(tmp)
            # Minimal OOXML zip so soffice has something to convert.
            import zipfile

            docx_path = tmp_path / "probe.docx"
            with zipfile.ZipFile(docx_path, "w") as zf:
                zf.writestr(
                    "[Content_Types].xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/word/document.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                    "</Types>",
                )
                zf.writestr(
                    "word/document.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    "<w:body><w:p><w:r><w:t>probe</w:t></w:r></w:p></w:body></w:document>",
                )
                zf.writestr(
                    "_rels/.rels",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                    'Target="word/document.xml"/>'
                    "</Relationships>",
                )
            proc = subprocess.run(
                [
                    binary,
                    "--headless",
                    "--norestore",
                    "--convert-to",
                    "odt",
                    "--outdir",
                    str(tmp_path),
                    str(docx_path),
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
            ok = proc.returncode == 0 and any(tmp_path.glob("*.odt"))
            logger.info(
                "SOFFICE_AVAILABLE=%s reason=%s",
                "true" if ok else "false",
                "convert_ok" if ok else f"convert_rc={proc.returncode}",
            )
            return ok
    except Exception as exc:
        logger.info(
            "SOFFICE_AVAILABLE=false reason=%s",
            type(exc).__name__,
        )
        return False
