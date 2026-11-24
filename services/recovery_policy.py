"""Choose whether a classified failure may start extraction recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

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
        FailureType.OCR_REQUIRED, "ocr_path", 1, True
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
    # OCR already attempted Tess→Gemini transcription upstream. If still
    # unverified, allow one targeted pass — never silent attempts=0 skip.
    FailureType.UNVERIFIED_OCR_FALLBACK: RecoveryPolicy(
        FailureType.UNVERIFIED_OCR_FALLBACK, "targeted_fields", 1, True
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


def recovery_policy_for_result(
    failure_type: FailureType,
    result: Optional[Dict[str, Any]] = None,
) -> RecoveryPolicy:
    """Default policy, upgraded when DI OCR evidence makes full Gemini skip a dead end.

    When Paddle/Python already produced OCR text or table HTML, the early
    Gemini vision fallback is skipped on purpose. GENERIC_PARSER_FAILURE then
    must not stay on ``existing_gemini_fallback`` (call_gemini=False). Route it
    to targeted field recovery instead. Other failure types keep their table.
    """
    policy = recovery_policy(failure_type)
    if failure_type != FailureType.GENERIC_PARSER_FAILURE:
        return policy
    if not _result_has_di_ocr_evidence(result):
        return policy
    return RecoveryPolicy(
        FailureType.GENERIC_PARSER_FAILURE,
        "targeted_fields",
        2,
        True,
    )


def _result_has_di_ocr_evidence(result: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(result, dict):
        return False
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    if extra.get("di_ocr_override") is True:
        return True
    if str(extra.get("ocr_table_html") or "").strip():
        return True
    if str(extra.get("ocr_text") or "").strip():
        return True
    evidence = result.get("ocr_evidence")
    if isinstance(evidence, dict):
        pages = evidence.get("pages")
        if isinstance(pages, list) and any(
            isinstance(page, dict)
            and (
                str(page.get("text") or "").strip()
                or (isinstance(page.get("tables"), dict) and page["tables"].get("available"))
            )
            for page in pages
        ):
            return True
    return False
