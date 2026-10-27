"""Choose whether a classified failure may start extraction recovery."""

from __future__ import annotations

from dataclasses import dataclass

from services.failure_taxonomy import FailureType


@dataclass(frozen=True)
class RecoveryPolicy:
    failure_type: FailureType
    strategy: str
    max_attempts: int
    call_gemini: bool
    provider_retry: bool = False


# Provider retries stay inside call_gemini_with_quota.
# OCR, low-text, and the generic fallback already ran before Phase 2.
_POLICIES = {
    FailureType.PROVIDER_ERROR: RecoveryPolicy(
        FailureType.PROVIDER_ERROR, "provider_retry", 0, False, provider_retry=True
    ),
    FailureType.EMPTY_OUTPUT: RecoveryPolicy(
        FailureType.EMPTY_OUTPUT, "structured_reread", 2, True
    ),
    FailureType.SCHEMA_INVALID: RecoveryPolicy(
        FailureType.SCHEMA_INVALID, "structured_output_retry", 2, True
    ),
    FailureType.MISSING_FIELDS: RecoveryPolicy(
        FailureType.MISSING_FIELDS, "targeted_fields", 2, True
    ),
    FailureType.STOCK_IDENTITY_FAILURE: RecoveryPolicy(
        FailureType.STOCK_IDENTITY_FAILURE, "targeted_stock_extraction", 2, True
    ),
    FailureType.DUPLICATE_ROWS: RecoveryPolicy(
        FailureType.DUPLICATE_ROWS, "structured_reread", 1, True
    ),
    FailureType.LOW_TEXT_EVIDENCE: RecoveryPolicy(
        FailureType.LOW_TEXT_EVIDENCE, "image_evidence", 1, False
    ),
    FailureType.OCR_REQUIRED: RecoveryPolicy(
        FailureType.OCR_REQUIRED, "ocr_path", 1, False
    ),
    FailureType.COLUMN_CONFUSION: RecoveryPolicy(
        FailureType.COLUMN_CONFUSION, "targeted_stock_extraction", 2, True
    ),
    FailureType.AMBIGUOUS_MAPPING: RecoveryPolicy(
        FailureType.AMBIGUOUS_MAPPING, "targeted_stock_extraction", 1, True
    ),
    FailureType.NUMERIC_CHARACTER_CONFUSION: RecoveryPolicy(
        FailureType.NUMERIC_CHARACTER_CONFUSION, "targeted_fields", 2, True
    ),
    FailureType.CELL_VALIDATION_FAILURE: RecoveryPolicy(
        FailureType.CELL_VALIDATION_FAILURE, "targeted_fields", 2, True
    ),
    FailureType.UNVERIFIED_OCR_FALLBACK: RecoveryPolicy(
        FailureType.UNVERIFIED_OCR_FALLBACK, "ocr_path", 0, False
    ),
    FailureType.GENERIC_PARSER_FAILURE: RecoveryPolicy(
        FailureType.GENERIC_PARSER_FAILURE, "existing_gemini_fallback", 1, False
    ),
    FailureType.LLM_EXTRACTION_FAILURE: RecoveryPolicy(
        FailureType.LLM_EXTRACTION_FAILURE, "targeted_recovery", 2, True
    ),
}


def recovery_policy(failure_type: FailureType) -> RecoveryPolicy:
    return _POLICIES[failure_type]
