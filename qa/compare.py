"""Deterministic row comparison. An LLM is not used."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from qa.ground_truth import CANONICAL_FIELDS

QTY_FIELDS = (
    "opening_qty",
    "opening_value",
    "receipts_qty",
    "receipts_value",
    "sales_qty",
    "sales_value",
    "closing_qty",
    "closing_value",
)

_FIELD_KIND = {
    "opening_qty": "wrong_opening_qty",
    "opening_value": "wrong_opening_value",
    "receipts_qty": "wrong_receipt_qty",
    "receipts_value": "wrong_receipt_value",
    "sales_qty": "wrong_sales_qty",
    "sales_value": "wrong_sales_value",
    "closing_qty": "wrong_closing_qty",
    "closing_value": "wrong_closing_value",
    "product_name": "wrong_product_name",
    "product_code": "wrong_product_code",
}

_JUNK_PRODUCT = re.compile(
    r"(page\s*\d+|/\d{1,2}/\d{4}|group\s+wise|product\s+name|op\.?\s*stock|"
    r"grand\s+total|^total\b|continued|as on\b|\d{1,2}/\d{1,2}/\d{2,4})",
    re.I,
)


def norm_name(value: Any) -> str:
    text = str(value or "").upper()
    text = text.replace("|", " ")
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def name_ratio(left: str, right: str) -> float:
    a = norm_name(left)
    b = norm_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    direct = SequenceMatcher(None, a, b).ratio()
    sorted_ratio = SequenceMatcher(
        None, " ".join(sorted(a.split())), " ".join(sorted(b.split()))
    ).ratio()
    contain = 0.0
    if a in b or b in a:
        contain = min(len(a), len(b)) / max(len(a), len(b))
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    prefix = 0.0
    if len(shorter.split()) >= 2 and longer.startswith(shorter):
        rest = longer[len(shorter) :].strip()
        rest_words = rest.split()
        rest_is_pack = all(
            re.search(r"\d", word)
            or word in {"ML", "MG", "GM", "GR", "TAB", "TABS", "CAP", "CAPS", "S"}
            for word in rest_words
        )
        if not rest or rest_is_pack:
            prefix = 0.94
    return max(direct, sorted_ratio, contain, prefix)


def as_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan"}:
        return None
    text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def numbers_equal(left: Optional[float], right: Optional[float]) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    tol = max(0.05, 0.001 * max(abs(left), abs(right)))
    return abs(left - right) <= tol


def _api_items(result: Dict[str, Any]) -> List[dict]:
    if not isinstance(result, dict):
        return []
    statements = result.get("statements")
    if isinstance(statements, list) and statements:
        items: List[dict] = []
        for statement in statements:
            if isinstance(statement, dict):
                items.extend(statement.get("line_items") or [])
        return [i for i in items if isinstance(i, dict)]
    return [i for i in (result.get("line_items") or []) if isinstance(i, dict)]


def api_method(result: Dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ""
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    method = str(extra.get("extraction_method") or "")
    if method:
        return method
    statements = result.get("statements")
    if isinstance(statements, list):
        for statement in statements:
            if not isinstance(statement, dict):
                continue
            totals = statement.get("totals") if isinstance(statement.get("totals"), dict) else {}
            extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
            method = str(extra.get("extraction_method") or "")
            if method:
                return method
    return ""


def _api_value(item: dict, field: str) -> Optional[float]:
    if field in {"product_name", "product_code"}:
        return None
    if field in item:
        return as_number(item.get(field))
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if field in extra:
        return as_number(extra.get(field))
    return None


def _display_num(value: Optional[float]) -> Any:
    if value is None:
        return None
    if abs(value - round(value)) < 1e-6:
        return int(round(value))
    return round(value, 2)


def _qty_signature(item: dict, fields: Sequence[str]) -> Tuple:
    return tuple(_api_value(item, f) if "product_name" not in f else None for f in fields)


def _gt_value(item: dict, field: str) -> Optional[float]:
    return as_number(item.get(field))


def _numeric_agreement(gt: dict, api: dict, fields: Sequence[str]) -> int:
    score = 0
    for field in fields:
        if field in {"product_name", "product_code"}:
            continue
        left = _gt_value(gt, field)
        right = _api_value(api, field)
        if left is None or right is None:
            continue
        if numbers_equal(left, right):
            score += 3
        else:
            score -= 2
    return score


def _pair_score(gt: dict, api: dict, qty_fields: Sequence[str]) -> float:
    ratio = name_ratio(gt.get("product_name"), api.get("product_name"))
    code_l = norm_name(gt.get("product_code"))
    code_r = norm_name(api.get("product_code"))
    code_bonus = 0.15 if code_l and code_r and code_l == code_r else 0.0
    numeric = _numeric_agreement(gt, api, qty_fields)
    return ratio + code_bonus + numeric / 20.0


def _is_junk_product(name: str) -> bool:
    text = name or ""
    if _JUNK_PRODUCT.search(text):
        return True
    letters = re.sub(r"[^A-Za-z]", "", text)
    return len(letters) < 2


def _adjustment(item: dict, key: str) -> float:
    value = as_number(item.get(key))
    return 0.0 if value is None else value


def expected_closing(item: dict, formula: str) -> Optional[float]:
    if formula == "none":
        return None
    opening = as_number(item.get("opening_qty"))
    receipts = as_number(item.get("receipts_qty"))
    sales = as_number(item.get("sales_qty"))
    if opening is None or receipts is None or sales is None:
        return None
    if formula == "simple":
        return opening + receipts - sales
    return (
        opening
        + receipts
        + _adjustment(item, "purchase_free")
        + _adjustment(item, "other_receipt_qty")
        + _adjustment(item, "sales_return")
        + _adjustment(item, "sales_return_free")
        - _adjustment(item, "purchase_return")
        - sales
        - _adjustment(item, "sales_free")
        - _adjustment(item, "other_issue_qty")
        - _adjustment(item, "exp_damage")
    )


def _formula_holds(item: dict, formula: str) -> Optional[bool]:
    expected = expected_closing(item, formula)
    closing = as_number(item.get("closing_qty"))
    if expected is None or closing is None:
        return None
    return abs(expected - closing) <= 0.51


def _duplicates(items: Sequence[dict], label: str) -> List[dict]:
    seen: Dict[str, int] = {}
    dups = []
    for item in items:
        key = norm_name(item.get("product_name"))
        pack = norm_name(item.get("packing"))
        token = f"{key}|{pack}"
        if not key:
            continue
        seen[token] = seen.get(token, 0) + 1
    reported = set()
    for item in items:
        key = norm_name(item.get("product_name"))
        pack = norm_name(item.get("packing"))
        token = f"{key}|{pack}"
        if seen.get(token, 0) > 1 and token not in reported:
            reported.add(token)
            dups.append(
                {
                    "side": label,
                    "product": item.get("product_name"),
                    "count": seen[token],
                }
            )
    return dups


def compare_statements(ground: Dict[str, Any], extracted: Dict[str, Any]) -> dict:
    gt_items = [i for i in (ground.get("line_items") or []) if isinstance(i, dict)]
    api_items = _api_items(extracted)
    present = set(ground.get("present_fields") or [])
    absent = set(ground.get("absent_fields") or [])
    qty_fields = [f for f in QTY_FIELDS if f in present]
    confidence = ground.get("confidence") or "low"

    pairs: List[Tuple[float, int, int]] = []
    for gi, gt in enumerate(gt_items):
        for ai, api in enumerate(api_items):
            ratio = name_ratio(gt.get("product_name"), api.get("product_name"))
            numeric = _numeric_agreement(gt, api, qty_fields)
            # A similar name, or a weaker name whose quantities clearly correspond.
            if ratio >= 0.90 or (ratio >= 0.74 and numeric >= 3):
                pairs.append((_pair_score(gt, api, qty_fields), gi, ai))
    pairs.sort(reverse=True)
    used_g = set()
    used_a = set()
    matched: List[Tuple[dict, dict, float]] = []
    for score, gi, ai in pairs:
        if gi in used_g or ai in used_a:
            continue
        used_g.add(gi)
        used_a.add(ai)
        matched.append((gt_items[gi], api_items[ai], name_ratio(
            gt_items[gi].get("product_name"), api_items[ai].get("product_name")
        )))

    mismatches: List[dict] = []
    ocr_review: List[dict] = []
    unavailable_counts: Dict[str, int] = {}

    for gt, api, ratio in matched:
        product = gt.get("product_name") or api.get("product_name")
        if ratio < 0.90:
            ocr_review.append(
                {
                    "product": product,
                    "api_product": api.get("product_name"),
                    "similarity": round(ratio, 3),
                    "note": "Product names differ but the row quantities correspond.",
                }
            )
        for field in qty_fields:
            expected = _gt_value(gt, field)
            actual = _api_value(api, field)
            if numbers_equal(expected, actual):
                continue
            if expected is None and confidence != "high":
                ocr_review.append(
                    {
                        "product": product,
                        "field": field,
                        "expected": None,
                        "actual": _display_num(actual),
                        "note": "Ground truth did not read this cell confidently.",
                    }
                )
                continue
            mismatches.append(
                {
                    "product": product,
                    "field": field,
                    "expected": _display_num(expected),
                    "actual": _display_num(actual),
                    "kind": _FIELD_KIND.get(field, "field_mismatch"),
                }
            )
        for field in absent:
            if field == "product_name":
                continue
            actual = _api_value(api, field)
            if actual is None:
                continue
            # A printed column is absent, so 0 and any other number are invented.
            unavailable_counts[field] = unavailable_counts.get(field, 0) + 1

    for field, count in unavailable_counts.items():
        mismatches.append(
            {
                "product": f"{count} rows",
                "field": field,
                "expected": None,
                "actual": 0 if count else None,
                "kind": "unavailable_field_not_null",
                "rows": count,
                "note": "This column is not in the PDF. Unavailable must stay null, not 0.",
            }
        )

    missing = []
    for index, gt in enumerate(gt_items):
        if index in used_g:
            continue
        missing.append(
            {
                "product": gt.get("product_name"),
                "opening_qty": _display_num(_gt_value(gt, "opening_qty")),
                "receipts_qty": _display_num(_gt_value(gt, "receipts_qty")),
                "sales_qty": _display_num(_gt_value(gt, "sales_qty")),
                "closing_qty": _display_num(_gt_value(gt, "closing_qty")),
                "kind": "missing_product",
            }
        )
    extra = []
    for index, api in enumerate(api_items):
        if index in used_a:
            continue
        name = str(api.get("product_name") or "")
        kind = "header_or_footer_as_product" if _is_junk_product(name) else "extra_product"
        if re.search(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", name) and len(norm_name(name).split()) <= 3:
            kind = "footer_or_date_as_product"
        extra.append(
            {
                "product": name,
                "opening_qty": _display_num(_api_value(api, "opening_qty")),
                "sales_qty": _display_num(_api_value(api, "sales_qty")),
                "closing_qty": _display_num(_api_value(api, "closing_qty")),
                "kind": kind,
            }
        )

    formula = ground.get("formula") or "none"
    formula_ok = [
        flag
        for flag in (_formula_holds(item, formula) for item in gt_items)
        if flag is not None
    ]
    formula_applies = bool(formula_ok) and (sum(1 for f in formula_ok if f) / len(formula_ok) >= 0.7)
    accounting = []
    if formula_applies:
        for gt, api, _ratio in matched:
            gt_flag = _formula_holds(gt, formula)
            if gt_flag is not True:
                continue
            api_view = {field: _api_value(api, field) for field in QTY_FIELDS}
            for key in (
                "sales_return",
                "sales_free",
                "purchase_return",
                "purchase_free",
                "exp_damage",
                "other_receipt_qty",
                "other_issue_qty",
                "sales_return_free",
            ):
                api_view[key] = _api_value(api, key)
                if api_view[key] is None:
                    api_view[key] = _gt_value(gt, key)
            if _formula_holds(api_view, formula) is False:
                accounting.append(
                    {
                        "product": gt.get("product_name"),
                        "formula": formula,
                        "expected_closing": _display_num(expected_closing(api_view, formula)),
                        "actual_closing": _display_num(_api_value(api, "closing_qty")),
                        "kind": "accounting_inconsistency",
                    }
                )

    duplicates = _duplicates(gt_items, "ground_truth") + _duplicates(api_items, "extracted")

    if confidence == "low":
        if missing or extra:
            ocr_review.append(
                {
                    "note": (
                        "The independent read was not confident, so unmatched "
                        f"rows were not scored ({len(gt_items)} read, {len(api_items)} extracted)."
                    )
                }
            )
        missing = []
        extra = []
        mismatches = [
            m for m in mismatches if m.get("kind") != "unavailable_field_not_null"
        ]
        accounting = []
    elif ground.get("partial"):
        for row in extra:
            ocr_review.append(
                {
                    "product": row.get("product"),
                    "note": "This extracted row was not confirmed by a readable OCR line.",
                }
            )
        extra = []

    hard_mismatches = [
        m for m in mismatches if m.get("kind") != "unavailable_field_not_null"
    ]
    status = "PASS"
    if confidence == "low":
        status = "REVIEW"
    elif missing or extra or hard_mismatches or accounting:
        status = "FAIL"
    elif unavailable_counts:
        status = "FAIL"
    elif ocr_review:
        status = "REVIEW"

    return {
        "status": status,
        "expected_rows": len(gt_items),
        "extracted_rows": len(api_items),
        "matched_rows": len(matched),
        "missing_rows": missing,
        "extra_rows": extra,
        "duplicates": duplicates,
        "mismatches": mismatches,
        "ocr_review": ocr_review,
        "accounting": accounting,
        "unavailable_fields": unavailable_counts,
        "formula_checked": formula if formula_applies else "none",
    }
