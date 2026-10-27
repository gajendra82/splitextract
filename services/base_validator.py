"""Wrap existing sales quality and stock-identity signals.

Does not rewrite evaluate_extraction_quality. A named parser is not accepted
only because that function protected it. This result is advisory in Phase 1:
it does not set extraction_failed and does not change HTTP status.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from services.extraction_quality import _duplicate_percent, evaluate_extraction_quality
from services.schemas import normalize_sales_statement

_DUPLICATE_MIN_ROWS = 6
_DUPLICATE_PERCENT_LIMIT = 40.0


def validate_sales_result(
    result: Optional[Dict[str, Any]],
    source_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return accepted/unresolved without mutating ``result``."""
    if not isinstance(result, dict):
        return _decision(
            accepted=False,
            reasons=["result_not_object"],
            checks=_blank_checks(schema=False, quality=False, rows=False),
            quality={},
        )

    projected = normalize_sales_statement(result)
    schema_errors = list((projected.get("metadata") or {}).get("schema_errors") or [])
    items = _items(result)
    extra = _extra(result)
    quality = evaluate_extraction_quality(result, source_metadata)
    quality_reasons = [str(reason) for reason in (quality.get("reasons") or [])]

    reasons: List[str] = []
    schema_ok = not schema_errors and (
        isinstance(result.get("line_items"), list)
        or (
            result.get("multi_statement")
            and isinstance(result.get("statements"), list)
        )
    )
    if not schema_ok:
        reasons.append("schema_invalid")
        reasons.extend(schema_errors)

    rows_ok = bool(items)
    if not rows_ok:
        reasons.append("empty_line_items")

    identity_ok, identity_reasons = _identity_ok(result, items)
    reasons.extend(identity_reasons)

    duplicates_ok = True
    if len(items) >= _DUPLICATE_MIN_ROWS and _duplicate_percent(items) > _DUPLICATE_PERCENT_LIMIT:
        duplicates_ok = False
        reasons.append("duplicate_rows")

    quality_ok = True
    if extra_flag_failed(result) or not rows_ok or quality.get("should_fallback"):
        # Protected named parsers return should_fallback False. Weak and empty
        # extracts still fail this check via the existing quality function.
        if extra_flag_failed(result) or not rows_ok or _serious_quality(quality_reasons):
            quality_ok = False
            if extra_flag_failed(result):
                reasons.append("extraction_failed")
            for reason in quality_reasons:
                if reason not in reasons:
                    reasons.append(reason)

    cell_ok, cell_reasons, cell_report = _cell_validation_ok(result, source_metadata)
    reasons.extend(cell_reasons)

    checks = {
        "schema": schema_ok,
        "quality": quality_ok,
        "stock_identity": identity_ok,
        "rows": rows_ok,
        "duplicates": duplicates_ok,
        "cell_validation": cell_ok,
        "column_mapping": "column_confusion" not in cell_reasons
        and "ambiguous_mapping" not in cell_reasons,
        "ocr_verified": "unverified_ocr_fallback" not in cell_reasons,
    }
    accepted = all(checks.values())
    decision = _decision(
        accepted=accepted, reasons=_unique(reasons), checks=checks, quality=quality
    )
    if cell_report is not None:
        decision["cell_validation"] = cell_report
    return decision


def extra_flag_failed(result: Dict[str, Any]) -> bool:
    if _extra(result).get("extraction_failed"):
        return True
    for statement in _statements(result):
        if _extra(statement).get("extraction_failed"):
            return True
    return False


def _identity_ok(result: Dict[str, Any], items: List[Dict[str, Any]]) -> tuple:
    """Reject when existing stock-identity output shows a serious failure.

    Named-parser protection inside evaluate_extraction_quality is ignored here.
    """
    reasons: List[str] = []
    limit = _identity_limit()
    for statement in _statements(result):
        extra = _extra(statement)
        statement_items = [
            item
            for item in (statement.get("line_items") or [])
            if isinstance(item, dict)
        ] or items
        validation = extra.get("stock_validation")
        fail_count = extra.get("stock_identity_fail_count")
        if isinstance(validation, dict) and validation.get("is_valid") is False:
            reasons.append("stock_identity_failure")
            continue
        fail_pct = _fail_percent(fail_count, statement_items)
        if fail_pct is not None and fail_pct > limit:
            reasons.append("stock_identity_failure")
    if "stock_identity_failure" in reasons:
        return False, _unique(reasons)
    return True, []


def _fail_percent(fail_count: Any, items: List[Dict[str, Any]]) -> Optional[float]:
    if fail_count is None or not items:
        return None
    try:
        count = float(fail_count)
    except (TypeError, ValueError):
        return None
    return 100.0 * count / len(items)


def _serious_quality(reasons: List[str]) -> bool:
    if not reasons:
        return False
    ignored = {"low_ocr_quality_score"}
    return any(reason not in ignored for reason in reasons)


def _statements(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    statements = result.get("statements")
    if result.get("multi_statement") and isinstance(statements, list) and statements:
        return [item for item in statements if isinstance(item, dict)]
    return [result]


def _items(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for statement in _statements(result):
        for item in statement.get("line_items") or []:
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _extra(result: Dict[str, Any]) -> Dict[str, Any]:
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    return extra


def _identity_limit() -> float:
    raw = os.getenv("GEMINI_EXTRACTION_MAX_IDENTITY_FAILURE_PERCENT", "20")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 20.0


def _cell_validation_ok(
    result: Dict[str, Any],
    source_metadata: Optional[Dict[str, Any]],
) -> tuple:
    """Evidence-driven cell/column gates. Prefer unresolved over silent wrong."""
    meta = source_metadata if isinstance(source_metadata, dict) else {}
    ocr_meta = {}
    if isinstance(result.get("ocr_evidence"), dict):
        ocr_meta = result["ocr_evidence"]
    elif isinstance(meta.get("ocr_evidence"), dict):
        ocr_meta = meta["ocr_evidence"]
    di = result.get("document_intelligence") if isinstance(result.get("document_intelligence"), dict) else {}
    ocr_bundle = di.get("ocr") if isinstance(di.get("ocr"), dict) else {}
    unverified = bool(
        ocr_meta.get("unverified_fallback")
        or ocr_bundle.get("unverified_fallback")
        or meta.get("unverified_ocr_fallback")
    )
    disagreement = bool(
        ocr_meta.get("disagreement")
        or ocr_bundle.get("disagreement")
        or meta.get("ocr_disagreement")
    )
    # Skip expensive geometry when no OCR / mapping signals are present and
    # the extract already came from native digital/Excel paths.
    route = {}
    phase1 = result.get("phase1") if isinstance(result.get("phase1"), dict) else {}
    if isinstance(phase1.get("route"), dict):
        route = phase1["route"]
    requires_ocr = bool(route.get("requires_ocr") or meta.get("requires_ocr"))
    if not requires_ocr and not ocr_meta and not unverified and not disagreement:
        # Still reject explicit blank→zero promotion markers.
        from services.cell_validator import validate_critical_cells

        report = validate_critical_cells(result)
        blocking = [
            issue
            for issue in report.issues
            if issue.category
            in {
                "INVALID_NORMALIZATION",
                "PLAUSIBILITY_ONLY_ASSIGNMENT",
                "NUMERIC_CHARACTER_CONFUSION",
                "NUMERIC_CONCATENATION",
                "SOURCE_DISCREPANCY",
            }
        ]
        if not blocking:
            return True, [], report.to_dict()
        reasons = [_reason_from_category(issue.category) for issue in blocking]
        return False, _unique(reasons + ["cell_validation_failure"]), report.to_dict()

    from services.cell_validator import validate_critical_cells

    report = validate_critical_cells(
        result,
        ocr_evidence=ocr_meta or None,
        column_headers=meta.get("column_headers"),
        column_cells=meta.get("column_cells"),
        ocr_disagreement=disagreement,
        unverified_ocr_fallback=unverified,
    )
    if not report.has_blocking_issues:
        return True, [], report.to_dict()
    reasons = [_reason_from_category(issue.category) for issue in report.issues]
    reasons.append("cell_validation_failure")
    return False, _unique(reasons), report.to_dict()


def _reason_from_category(category: str) -> str:
    mapping = {
        "COLUMN_CONFUSION": "column_confusion",
        "AMBIGUOUS_MAPPING": "ambiguous_mapping",
        "NUMERIC_CHARACTER_CONFUSION": "numeric_character_confusion",
        "NUMERIC_CONCATENATION": "numeric_character_confusion",
        "UNVERIFIED_OCR_FALLBACK": "unverified_ocr_fallback",
        "OCR_DISAGREEMENT": "ocr_engine_disagreement",
        "PLAUSIBILITY_ONLY_ASSIGNMENT": "plausibility_only_assignment",
        "INVALID_NORMALIZATION": "invalid_normalization",
        "EMPTY_OCR_PAGE": "empty_ocr_page",
        "TRUNCATED_CELL": "truncated_cell",
        "STOCK_IDENTITY_MISMATCH": "stock_identity_failure",
        "SOURCE_DISCREPANCY": "source_discrepancy",
    }
    return mapping.get(category, "cell_validation_failure")


def _blank_checks(**overrides: bool) -> Dict[str, bool]:
    checks = {
        "schema": True,
        "quality": True,
        "stock_identity": True,
        "rows": True,
        "duplicates": True,
        "cell_validation": True,
        "column_mapping": True,
        "ocr_verified": True,
    }
    checks.update(overrides)
    return checks


def _unique(reasons: List[str]) -> List[str]:
    seen = []
    for reason in reasons:
        if reason and reason not in seen:
            seen.append(reason)
    return seen


def _decision(
    *,
    accepted: bool,
    reasons: List[str],
    checks: Dict[str, bool],
    quality: Dict[str, Any],
) -> Dict[str, Any]:
    payload = {
        "accepted": accepted,
        "status": "pass" if accepted else "unresolved",
        "reasons": reasons,
        "checks": checks,
        "quality_score": quality.get("score"),
        "parser": quality.get("parser"),
        "failure_type": None,
    }
    if not accepted:
        from services.failure_taxonomy import classify_failure

        failure = classify_failure(
            payload,
            extraction_method=str(quality.get("parser") or ""),
        )
        payload["failure_type"] = failure.value if failure else None
    return payload
