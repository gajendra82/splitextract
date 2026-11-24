"""Layered validation view over existing authoritative validators."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def build_validation_stack(
    result: Dict[str, Any],
    *,
    phase1_validation: Optional[Dict[str, Any]] = None,
    ocr_disagreement: bool = False,
    unverified_ocr_fallback: bool = False,
) -> Dict[str, Any]:
    """Organize validation signals without weakening base_validator."""
    phase1_validation = phase1_validation or {}
    layers: List[Dict[str, Any]] = []

    accepted = bool(phase1_validation.get("accepted"))
    failures = list(phase1_validation.get("failures") or [])
    primary = phase1_validation.get("primary_failure") or phase1_validation.get(
        "failure_type"
    )
    reasons = {str(r) for r in (phase1_validation.get("reasons") or [])}
    checks = (
        phase1_validation.get("checks")
        if isinstance(phase1_validation.get("checks"), dict)
        else {}
    )

    layers.append(
        {
            "name": "schema",
            "passed": accepted or primary not in {"SCHEMA_FAILURE", "schema"},
            "detail": "normalize/schema via phase1",
        }
    )
    layers.append(
        {
            "name": "evidence",
            "passed": "EVIDENCE" not in str(primary or "").upper(),
            "detail": "evidence validation",
        }
    )
    layers.append(
        {
            "name": "structural",
            "passed": "STRUCTURE" not in str(primary or "").upper(),
            "detail": "structural checks",
        }
    )
    layers.append(
        {
            "name": "column_mapping",
            "passed": "MAPPING" not in str(primary or "").upper()
            and "COLUMN" not in str(primary or "").upper(),
            "detail": "layout-scoped mapping",
        }
    )
    layers.append(
        {
            "name": "numeric",
            "passed": "NUMERIC" not in str(primary or "").upper(),
            "detail": "numeric integrity",
        }
    )
    identity = result.get("stock_identity") or {}
    phase_reasons = {str(reason) for reason in (phase1_validation.get("reasons") or [])}
    phase_checks = phase1_validation.get("checks") if isinstance(phase1_validation.get("checks"), dict) else {}
    identity_ok = not (
        (
            isinstance(identity, dict)
            and (identity.get("failed") or identity.get("failures"))
        )
        or "stock_identity_failure" in phase_reasons
        or phase_checks.get("stock_identity") is False
        or str(primary or "") == "STOCK_IDENTITY_FAILURE"
    )
    layers.append(
        {
            "name": "business_identity",
            "passed": identity_ok,
            "detail": "stock identity profiles remain authoritative",
        }
    )
    layers.append(
        {
            "name": "duplicate_row",
            "passed": "DUPLICATE" not in str(primary or "").upper(),
            "detail": "duplicate-row validation",
        }
    )
    layers.append(
        {
            "name": "cross_field",
            "passed": identity_ok,
            "detail": "cross-field consistency via identity/business rules",
        }
    )
    ocr_ok = not (
        ocr_disagreement
        or unverified_ocr_fallback
        or checks.get("ocr_verified") is False
        or "unverified_ocr_fallback" in reasons
        or "empty_ocr_page" in reasons
    )
    layers.append(
        {
            "name": "ocr_evidence_consistency",
            "passed": ocr_ok,
            "detail": "cross-engine OCR consistency / verified evidence",
        }
    )

    return {
        "accepted": accepted,
        "primary_failure": primary,
        "failures": failures,
        "layers": layers,
        "authoritative": "base_validator.validate_sales_result",
    }
