"""Final decision model for document intelligence."""

from __future__ import annotations

from typing import Any, Dict, Optional

VERIFIED = "VERIFIED"
RECOVERED_AND_VERIFIED = "RECOVERED_AND_VERIFIED"
UNRESOLVED = "UNRESOLVED"
PROVIDER_ERROR = "PROVIDER_ERROR"
EXTRACTION_ERROR = "EXTRACTION_ERROR"


def decide_final_status(
    *,
    validation: Optional[Dict[str, Any]],
    verification: Optional[Dict[str, Any]],
    recovery: Optional[Dict[str, Any]],
    extraction_error: Optional[str] = None,
    provider_error: Optional[str] = None,
    ocr_disagreement: bool = False,
) -> Dict[str, Any]:
    """Rule-based final status. PASS-from-parser alone is never enough."""
    if provider_error:
        return {
            "status": PROVIDER_ERROR,
            "reason": provider_error,
            "verified": False,
        }
    if extraction_error:
        return {
            "status": EXTRACTION_ERROR,
            "reason": extraction_error,
            "verified": False,
        }

    validation = validation or {}
    # Unread source evidence blocks both VERIFIED and RECOVERED_AND_VERIFIED
    # before schema or stock-identity results are considered.
    if validation.get("source_evidence") is False:
        return {
            "status": UNRESOLVED,
            "reason": "insufficient_source_evidence",
            "verified": False,
        }
    business = validation.get("business_validation")
    business = business if isinstance(business, dict) else {}
    business_status = str(business.get("status") or "")
    if business_status == "inconsistent":
        return {
            "status": UNRESOLVED,
            "reason": business.get("reason") or "stock_identity_failure",
            "verified": False,
        }
    if business_status == "unknown":
        return {
            "status": UNRESOLVED,
            "reason": "identity_profile_unknown",
            "verified": False,
        }
    verification = verification or {}
    recovery = recovery or {}
    reasons = {str(reason) for reason in (validation.get("reasons") or [])}
    issues = {str(issue) for issue in (verification.get("issues") or [])}
    if (
        "gemini_vision_without_source_evidence" in reasons
        or "gemini_vision_without_source_evidence" in issues
    ):
        return {
            "status": UNRESOLVED,
            "reason": "gemini_vision_without_source_evidence",
            "verified": False,
        }

    accepted = bool(validation.get("accepted"))
    verify_ok = bool(verification.get("passed"))
    # recovery_runner writes phase2.status="recovered"; older callers used
    # applied/accepted booleans. Accept either shape.
    phase2_status = str(recovery.get("status") or "").strip().lower()
    recovered = bool(
        recovery.get("applied")
        or recovery.get("accepted")
        or phase2_status == "recovered"
    )
    recovery_accepted = bool(
        recovery.get("accepted")
        or phase2_status == "recovered"
    )

    if recovered and recovery_accepted and verify_ok and accepted:
        if ocr_disagreement and not verification.get("allow_ocr_disagreement"):
            return {
                "status": UNRESOLVED,
                "reason": "ocr_disagreement_after_recovery",
                "verified": False,
            }
        return {
            "status": RECOVERED_AND_VERIFIED,
            "reason": "recovery_revalidated",
            "verified": True,
        }

    if accepted and verify_ok and not ocr_disagreement:
        return {
            "status": VERIFIED,
            "reason": "schema_evidence_business_ok",
            "verified": True,
        }

    if accepted and verify_ok and ocr_disagreement:
        return {
            "status": UNRESOLVED,
            "reason": "ocr_disagreement",
            "verified": False,
        }

    if accepted and not verify_ok:
        return {
            "status": UNRESOLVED,
            "reason": verification.get("reason") or "independent_verification_failed",
            "verified": False,
        }

    return {
        "status": UNRESOLVED,
        "reason": validation.get("primary_failure")
        or validation.get("reason")
        or "validation_not_accepted",
        "verified": False,
    }
