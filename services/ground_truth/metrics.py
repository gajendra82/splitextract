"""Aggregate OCR ground-truth metrics."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def compute_iou(
    box_a: Sequence[float], box_b: Sequence[float]
) -> Optional[float]:
    """Axis-aligned IoU for [x1,y1,x2,y2]."""
    if not box_a or not box_b or len(box_a) != 4 or len(box_b) != 4:
        return None
    ax1, ay1, ax2, ay2 = (float(v) for v in box_a)
    bx1, by1, bx2, by2 = (float(v) for v in box_b)
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def _ratio(num: int, den: int) -> Optional[float]:
    if den <= 0:
        return None
    return num / float(den)


def _f1(precision: Optional[float], recall: Optional[float]) -> Optional[float]:
    if precision is None or recall is None:
        return None
    if precision + recall <= 0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def aggregate_metrics(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(matches)
    correct = sum(1 for m in matches if m.get("correct"))
    silent_wrong = sum(1 for m in matches if m.get("silent_wrong"))
    critical = sum(1 for m in matches if m.get("critical") and not m.get("correct"))

    def subset(types: Iterable[str]) -> List[Dict[str, Any]]:
        wanted = set(types)
        return [m for m in matches if m.get("field_type") in wanted]

    quantity = subset({"quantity", "numeric"})
    # quantity specifically
    quantity_only = [m for m in matches if m.get("field_type") == "quantity"]
    price = subset({"price"})
    date = subset({"date"})
    batch = subset({"batch"})
    header = subset({"header"})
    table = subset({"table_cell", "quantity", "price"})
    text = subset({"text", "header"})
    numeric_all = [
        m
        for m in matches
        if m.get("field_type")
        in {
            "numeric",
            "quantity",
            "price",
            "percentage",
            "hsn_gst",
            "date",
            "batch",
            "table_cell",
        }
    ]

    def acc(items: List[Dict[str, Any]]) -> Optional[float]:
        return _ratio(sum(1 for m in items if m.get("correct")), len(items))

    # Precision/recall for verified GT fields:
    # predicted present & correct / predicted present
    # predicted present & correct / all GT fields
    predicted = [m for m in matches if m.get("predicted_raw") is not None]
    tp = sum(1 for m in predicted if m.get("correct"))
    precision = _ratio(tp, len(predicted))
    recall = _ratio(tp, total)
    f1 = _f1(precision, recall)

    def prf(items: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
        pred = [m for m in items if m.get("predicted_raw") is not None]
        true_pos = sum(1 for m in pred if m.get("correct"))
        p = _ratio(true_pos, len(pred))
        r = _ratio(true_pos, len(items))
        return {"precision": p, "recall": r, "f1": _f1(p, r), "n": len(items)}

    # Accepted-field silent-wrong: incorrect values that were not flagged.
    # Denominator is accepted/predicted fields (not all GT fields).
    accepted_fields = [
        m
        for m in matches
        if m.get("predicted_raw") is not None and not m.get("flagged_unresolved")
    ]
    accepted_silent_wrong = sum(1 for m in accepted_fields if m.get("silent_wrong"))
    unresolved = sum(1 for m in matches if m.get("flagged_unresolved"))
    missing = sum(1 for m in matches if m.get("error_type") == "MISSING")

    return {
        "verified_fields": total,
        "correct_fields": correct,
        "field_accuracy": _ratio(correct, total),
        "numeric_accuracy": acc(numeric_all),
        "quantity_accuracy": acc(quantity_only) if quantity_only else acc(quantity),
        "price_accuracy": acc(price),
        "date_accuracy": acc(date),
        "batch_accuracy": acc(batch),
        "header_accuracy": acc(header),
        "table_column_accuracy": acc(table),
        "text_accuracy": acc(text),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        # Overall silent-wrong / all verified GT fields (legacy denominator).
        "silent_wrong_rate": _ratio(silent_wrong, total),
        "silent_wrong_count": silent_wrong,
        "silent_wrong_denominator_verified_fields": total,
        # Accepted-field silent-wrong / accepted predicted fields.
        "accepted_field_silent_wrong_rate": _ratio(
            accepted_silent_wrong, len(accepted_fields)
        ),
        "accepted_field_silent_wrong_count": accepted_silent_wrong,
        "accepted_field_denominator": len(accepted_fields),
        "explicit_unresolved_rate": _ratio(unresolved, total),
        "missing_value_rate": _ratio(missing, total),
        "critical_error_rate": _ratio(critical, total),
        "by_type_prf": {
            "numeric": prf(numeric_all),
            "text": prf(text),
            "table": prf(table),
        },
        "error_type_counts": _count(matches, "error_type"),
    }


def _count(items: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for item in items:
        value = item.get(key)
        if not value:
            continue
        out[str(value)] = out.get(str(value), 0) + 1
    return out


def document_status(matches: List[Dict[str, Any]]) -> str:
    if not matches:
        return "INSUFFICIENT_GROUND_TRUTH"
    high = [
        m
        for m in matches
        if (m.get("priority") or "high") == "high"
        and m.get("field_type")
        in {
            "numeric",
            "quantity",
            "price",
            "date",
            "batch",
            "percentage",
            "hsn_gst",
            "table_cell",
            "header",
        }
    ]
    pool = high or matches
    if all(m.get("correct") for m in pool):
        return "FULLY_CORRECT"
    if any(m.get("correct") for m in pool) and any(not m.get("correct") for m in pool):
        return "PARTIALLY_CORRECT"
    if any(not m.get("correct") for m in pool):
        return "INCORRECT"
    return "INSUFFICIENT_GROUND_TRUTH"
