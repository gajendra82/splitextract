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
    if _source_evidence_missing(result) or "insufficient_source_evidence" in reasons:
        issues.append("insufficient_source_evidence")
    if gemini_vision_lacks_source_evidence(result) or (
        "gemini_vision_without_source_evidence" in reasons
    ):
        issues.append("gemini_vision_without_source_evidence")
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
            "source_match": "source_discrepancy" not in issues
            and "insufficient_source_evidence" not in issues
            and "gemini_vision_without_source_evidence" not in issues,
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


def gemini_vision_lacks_source_evidence(result: Dict[str, Any]) -> bool:
    """Gemini wrote quantities, and neither OCR text nor source columns exist.

    A checked source table or usable OCR text can still support those
    quantities. Vision numbers alone cannot.
    """
    if not isinstance(result, dict) or not _uses_gemini_vision(result):
        return False
    if _has_usable_ocr_text(result) or _has_source_columns(result):
        return False
    return _has_quantity(result)


def _uses_gemini_vision(result: Dict[str, Any]) -> bool:
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    method = str(extra.get("extraction_method") or "").casefold()
    # VL HTML table parse is structured OCR evidence, not full-page Gemini.
    if method == "vl_html_table_parse":
        return False
    parts = [
        extra.get("extraction_method"),
        extra.get("reader_engine"),
        extra.get("repaired_from"),
        extra.get("layout"),
    ]
    blob = " ".join(str(part or "") for part in parts).casefold()
    # Named parsers such as swil_receipt_pur_value_vision are not Gemini.
    return "gemini" in blob


def _has_usable_ocr_text(result: Dict[str, Any]) -> bool:
    from services.source_evidence import has_valid_source_evidence

    # Gemini-as-OCR / SmartRouter table HTML counts as usable source text.
    if has_valid_source_evidence(extraction_result=result):
        return True
    direct = result.get("_grounding_ocr_text")
    if isinstance(direct, str) and direct.strip():
        return True
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    grounding = extra.get("source_grounding")
    if isinstance(grounding, dict):
        text = grounding.get("ocr_text")
        if isinstance(text, str) and text.strip():
            return True
    for evidence in _ocr_evidence_dicts(result):
        pages = evidence.get("pages")
        if not isinstance(pages, list):
            continue
        for page in pages:
            if not isinstance(page, dict):
                continue
            text = page.get("text")
            if isinstance(text, str) and text.strip():
                return True
            try:
                if int(page.get("text_length") or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _ocr_evidence_dicts(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    evidence = result.get("ocr_evidence")
    if isinstance(evidence, dict):
        found.append(evidence)
    di = result.get("document_intelligence")
    if isinstance(di, dict):
        ocr = di.get("ocr")
        if isinstance(ocr, dict) and isinstance(ocr.get("evidence"), dict):
            found.append(ocr["evidence"])
    return found


def _has_source_columns(result: Dict[str, Any]) -> bool:
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    grounding = extra.get("source_grounding")
    if not isinstance(grounding, dict):
        return False
    if str(grounding.get("status") or "") != "checked":
        return False
    try:
        compared = int(grounding.get("compared_rows") or 0)
    except (TypeError, ValueError):
        compared = 0
    return compared > 0


def _has_quantity(result: Dict[str, Any]) -> bool:
    items = result.get("line_items")
    if not isinstance(items, list):
        return False
    fields = ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
    for item in items:
        if not isinstance(item, dict):
            continue
        if any(item.get(field) is not None for field in fields):
            return True
    return False


def _source_evidence_missing(result: Dict[str, Any]) -> bool:
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    grounding = extra.get("source_grounding")
    if not isinstance(grounding, dict):
        return False
    if grounding.get("status") == "insufficient_source_evidence":
        return True
    return bool(grounding.get("blocks_verification"))


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
