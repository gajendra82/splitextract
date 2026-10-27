"""Independent verification. Extractor is not the sole authority."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def independently_verify(
    result: Dict[str, Any],
    *,
    validation: Optional[Dict[str, Any]] = None,
    ocr_disagreement: bool = False,
    unverified_ocr_fallback: bool = False,
) -> Dict[str, Any]:
    """Compare extracted values against business evidence rules.

    Arithmetic alone is not proof. Schema-valid JSON and a balanced stock
    identity equation are not sufficient to mark values source-correct.
    """
    validation = validation or {}
    issues: List[str] = []

    if not validation.get("accepted"):
        issues.append("validation_not_accepted")

    identity = result.get("stock_identity") or {}
    if isinstance(identity, dict):
        if identity.get("failed") or identity.get("status") in {
            "FAIL",
            "FAILED",
            "STOCK_IDENTITY_FAILURE",
        }:
            issues.append("stock_identity_failure")
        failures = identity.get("failures") or identity.get("failed_rows") or []
        if failures:
            issues.append("stock_identity_row_failures")

    # Null must not be silently treated as zero in verification notes.
    invented = _detect_null_zero_promotion(result)
    if invented:
        issues.append("null_zero_promotion_suspected")

    if ocr_disagreement:
        issues.append("ocr_engine_disagreement")
    if unverified_ocr_fallback:
        issues.append("unverified_ocr_fallback")

    checks = validation.get("checks") if isinstance(validation.get("checks"), dict) else {}
    reasons = {str(reason) for reason in (validation.get("reasons") or [])}
    if (
        "stock_identity_failure" in reasons
        or checks.get("stock_identity") is False
    ):
        issues.append("stock_identity_failure")
    if checks.get("column_mapping") is False:
        issues.append("column_confusion")
    if checks.get("cell_validation") is False:
        issues.append("cell_validation_failure")
    if _source_discrepancy(result) or "source_discrepancy" in reasons:
        issues.append("source_discrepancy")
    if checks.get("ocr_verified") is False:
        issues.append("unverified_ocr_fallback")

    cell_report = validation.get("cell_validation")
    if isinstance(cell_report, dict) and cell_report.get("has_blocking_issues"):
        issues.append("cell_validation_failure")

    # Dynamic fields must not overwrite canonicals (presence check only).
    if _dynamic_overwrites_canonical(result):
        issues.append("dynamic_overwrote_canonical")

    issues = _unique(issues)
    non_validation = [i for i in issues if i != "validation_not_accepted"]
    passed = bool(validation.get("accepted")) and not non_validation

    return {
        "passed": passed,
        "issues": issues,
        "reason": issues[0] if issues else "ok",
        "checks": {
            "validation_accepted": bool(validation.get("accepted")),
            "stock_identity_ok": "stock_identity_failure" not in issues
            and "stock_identity_row_failures" not in issues,
            "null_integrity": "null_zero_promotion_suspected" not in issues,
            "ocr_agreement": not ocr_disagreement,
            "ocr_verified": "unverified_ocr_fallback" not in issues,
            "column_mapping_ok": "column_confusion" not in issues,
            "cell_validation_ok": "cell_validation_failure" not in issues,
            "source_match": "source_discrepancy" not in issues,
            "canonical_guard": "dynamic_overwrote_canonical" not in issues,
            # Stock equation balance is diagnostic only — never sole proof.
            "stock_equation_is_not_proof": True,
        },
    }


def _unique(values: List[str]) -> List[str]:
    seen: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def _source_discrepancy(result: Dict[str, Any]) -> bool:
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    grounding = extra.get("source_grounding")
    if not isinstance(grounding, dict):
        return False
    try:
        return int(grounding.get("discrepancy_count") or 0) > 0
    except (TypeError, ValueError):
        return False


def _detect_null_zero_promotion(result: Dict[str, Any]) -> bool:
    """Heuristic: flag only explicit sanitizer markers if present."""
    meta = result.get("integrity") or result.get("extra") or {}
    if isinstance(meta, dict) and meta.get("null_promoted_to_zero"):
        return True
    return False


def _dynamic_overwrites_canonical(result: Dict[str, Any]) -> bool:
    extras = result.get("dynamic_fields") or result.get("extra_fields") or {}
    if not isinstance(extras, dict):
        return False
    canonical = {
        "opening_qty",
        "purchase_qty",
        "sales_qty",
        "closing_qty",
        "receipt_qty",
        "issues_qty",
        "free_qty",
    }
    return bool(canonical.intersection(extras.keys()))
