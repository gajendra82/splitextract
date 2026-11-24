"""Baseline versus Phase 2 transition labels.

Comparison reads finished results. It does not extract, recover, or invent
expected values.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

CANONICAL_FIELDS = (
    "opening_qty",
    "opening_value",
    "receipts_qty",
    "receipts_value",
    "sales_qty",
    "sales_value",
    "closing_qty",
    "closing_value",
)

_UNTRUSTED_PROFILES = {"unknown", "not_scored"}


def iter_line_items(result: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    rows: List[Dict[str, Any]] = []
    for item in result.get("line_items") or []:
        if isinstance(item, dict):
            rows.append(item)
    for statement in result.get("statements") or []:
        if not isinstance(statement, dict):
            continue
        for item in statement.get("line_items") or []:
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _phase1(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    phase1 = result.get("phase1")
    return phase1 if isinstance(phase1, dict) else {}


def _validation(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    validation = _phase1(result).get("validation")
    return validation if isinstance(validation, dict) else {}


def _phase2(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    phase2 = result.get("phase2")
    return phase2 if isinstance(phase2, dict) else {}


def baseline_accepted(result: Optional[Dict[str, Any]]) -> bool:
    return _validation(result).get("accepted") is True


def canonical_signature(result: Optional[Dict[str, Any]]) -> Tuple[Tuple[Any, ...], ...]:
    rows = []
    for item in iter_line_items(result):
        rows.append(tuple(item.get(field) for field in ("product_name",) + CANONICAL_FIELDS))
    return tuple(rows)


def dynamic_signature(result: Optional[Dict[str, Any]]) -> Tuple[Tuple[Any, ...], ...]:
    rows = []
    for item in iter_line_items(result):
        extras = tuple(
            sorted(
                (key, item.get(key))
                for key in item
                if key not in CANONICAL_FIELDS and key != "product_name"
            )
        )
        rows.append(extras)
    return tuple(rows)


def null_became_zero(baseline: Optional[Dict[str, Any]], phase2: Optional[Dict[str, Any]]) -> bool:
    left = iter_line_items(baseline)
    right = iter_line_items(phase2)
    if len(left) != len(right):
        return False
    for before, after in zip(left, right):
        for field in CANONICAL_FIELDS:
            if before.get(field) is None and after.get(field) == 0:
                return True
            if before.get(field) is None and after.get(field) == 0.0:
                return True
    return False


def recovery_state(result: Optional[Dict[str, Any]]) -> str:
    phase2 = _phase2(result)
    if not phase2:
        return "RECOVERY_NOT_ATTEMPTED"
    status = str(phase2.get("status") or "")
    failure = str(phase2.get("failure_type") or "")
    attempts = int(phase2.get("attempts") or 0)
    if status == "not_needed":
        return "RECOVERY_NOT_ATTEMPTED"
    if failure == "PROVIDER_ERROR":
        return "RECOVERY_PROVIDER_ERROR"
    if status == "recovered":
        return "RECOVERY_ACCEPTED"
    if attempts > 0:
        return "RECOVERY_REJECTED"
    return "RECOVERY_UNRESOLVED"


def verification_state(label: Optional[str]) -> str:
    if label == "correct":
        return "RECOVERY_VERIFIED_CORRECT"
    if label == "wrong":
        return "RECOVERY_VERIFIED_WRONG"
    return "RECOVERY_UNVERIFIED"


def safety_flags(
    baseline: Optional[Dict[str, Any]],
    phase2: Optional[Dict[str, Any]],
    *,
    profile_id: str = "",
) -> List[str]:
    flags: List[str] = []
    if canonical_signature(baseline) != canonical_signature(phase2):
        flags.append("canonical_values_changed")
    if dynamic_signature(baseline) != dynamic_signature(phase2):
        flags.append("dynamic_field_changed")
    if null_became_zero(baseline, phase2):
        flags.append("null_became_zero")
    phase2_meta = _phase2(phase2)
    if profile_id in _UNTRUSTED_PROFILES and phase2_meta.get("status") == "recovered":
        flags.append("unknown_layout_accepted")
    return flags


def classify_transition(
    baseline: Optional[Dict[str, Any]],
    phase2: Optional[Dict[str, Any]],
    *,
    verification: str = "UNVERIFIED",
    profile_id: str = "",
    baseline_error: bool = False,
) -> Dict[str, Any]:
    """Return one transition plus the safety flags that explain it.

    ``verification`` is ``correct``, ``wrong``, or ``UNVERIFIED``. Unverified
    acceptance is not treated as correct or wrong.
    """
    flags = safety_flags(baseline, phase2, profile_id=profile_id)
    state = recovery_state(phase2)
    verified = verification_state(None if verification == "UNVERIFIED" else verification)

    if baseline_error:
        transition = "BASELINE_ERROR"
    elif baseline_accepted(baseline):
        changed = "canonical_values_changed" in flags or "dynamic_field_changed" in flags
        if "null_became_zero" in flags or (changed and verification == "wrong"):
            transition = "PASS → WRONG"
        elif changed and state == "RECOVERY_ACCEPTED":
            transition = "PASS → RECOVERED"
        elif changed:
            transition = "PASS → CHANGED"
        else:
            transition = "PASS → PASS"
    else:
        if state == "RECOVERY_PROVIDER_ERROR":
            transition = "FAIL → PROVIDER_ERROR"
        elif state == "RECOVERY_ACCEPTED" and verification == "wrong":
            transition = "FAIL → WRONG"
        elif state == "RECOVERY_ACCEPTED":
            transition = "FAIL → RECOVERED"
        else:
            transition = "FAIL → UNRESOLVED"

    return {
        "transition": transition,
        "recovery_state": state,
        "verification": verified,
        "safety_flags": flags,
        "baseline_accepted": baseline_accepted(baseline),
        "values_unchanged": "canonical_values_changed" not in flags
        and "dynamic_field_changed" not in flags,
    }
