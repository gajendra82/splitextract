"""Map Phase 1 validation signals onto one recovery failure type."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional


class FailureType(str, Enum):
    EMPTY_OUTPUT = "EMPTY_OUTPUT"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    STOCK_IDENTITY_FAILURE = "STOCK_IDENTITY_FAILURE"
    DUPLICATE_ROWS = "DUPLICATE_ROWS"
    MISSING_FIELDS = "MISSING_FIELDS"
    LOW_TEXT_EVIDENCE = "LOW_TEXT_EVIDENCE"
    OCR_REQUIRED = "OCR_REQUIRED"
    COLUMN_CONFUSION = "COLUMN_CONFUSION"
    NUMERIC_CHARACTER_CONFUSION = "NUMERIC_CHARACTER_CONFUSION"
    AMBIGUOUS_MAPPING = "AMBIGUOUS_MAPPING"
    CELL_VALIDATION_FAILURE = "CELL_VALIDATION_FAILURE"
    UNVERIFIED_OCR_FALLBACK = "UNVERIFIED_OCR_FALLBACK"
    GENERIC_PARSER_FAILURE = "GENERIC_PARSER_FAILURE"
    LLM_EXTRACTION_FAILURE = "LLM_EXTRACTION_FAILURE"
    PROVIDER_ERROR = "PROVIDER_ERROR"


_GEMINI_METHODS = {
    "gemini_vision",
    "gemini_extraction_fallback",
    "pdf_text_gemini",
}
_MISSING_FIELD_REASONS = {"missing_product_names", "missing_numeric_values"}


def classify_failure(
    validation: Optional[Dict[str, Any]],
    *,
    route: Optional[Dict[str, Any]] = None,
    extraction_method: Optional[str] = None,
    provider_error: Optional[str] = None,
) -> Optional[FailureType]:
    """Return one primary failure, or None when validation already accepted."""
    if provider_error:
        return FailureType.PROVIDER_ERROR
    if not isinstance(validation, dict) or validation.get("accepted"):
        return None

    reasons = {str(reason) for reason in (validation.get("reasons") or []) if reason}
    checks = validation.get("checks") if isinstance(validation.get("checks"), dict) else {}
    method = str(
        extraction_method
        or validation.get("parser")
        or ""
    ).strip()
    route = route if isinstance(route, dict) else {}

    if "schema_invalid" in reasons or any("schema" in reason and "error" in reason for reason in reasons):
        return FailureType.SCHEMA_INVALID
    if "empty_line_items" in reasons or checks.get("rows") is False:
        return FailureType.EMPTY_OUTPUT
    if "unverified_ocr_fallback" in reasons or checks.get("ocr_verified") is False:
        return FailureType.UNVERIFIED_OCR_FALLBACK
    if "column_confusion" in reasons or checks.get("column_mapping") is False:
        return FailureType.COLUMN_CONFUSION
    if "ambiguous_mapping" in reasons:
        return FailureType.AMBIGUOUS_MAPPING
    if "numeric_character_confusion" in reasons:
        return FailureType.NUMERIC_CHARACTER_CONFUSION
    if "cell_validation_failure" in reasons or checks.get("cell_validation") is False:
        return FailureType.CELL_VALIDATION_FAILURE
    if "stock_identity_failure" in reasons or checks.get("stock_identity") is False:
        return FailureType.STOCK_IDENTITY_FAILURE
    if "duplicate_rows" in reasons or checks.get("duplicates") is False:
        return FailureType.DUPLICATE_ROWS
    if reasons & _MISSING_FIELD_REASONS:
        return FailureType.MISSING_FIELDS
    if method in _GEMINI_METHODS:
        return FailureType.LLM_EXTRACTION_FAILURE
    if route.get("requires_ocr") is True:
        return FailureType.OCR_REQUIRED
    if "low_ocr_quality_score" in reasons or route.get("pdf_kind") == "scanned":
        return FailureType.LOW_TEXT_EVIDENCE
    if "extraction_failed" in reasons and "gemini" in method:
        return FailureType.LLM_EXTRACTION_FAILURE
    return FailureType.GENERIC_PARSER_FAILURE
