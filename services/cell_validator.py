"""Cell-level validation for critical stock fields.

Detects OCR/normalization failures and marks them unresolved.
Does not invent values or repair via stock arithmetic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from services.column_geometry import (
    CRITICAL_QTY_FIELDS,
    ColumnMappingReport,
    column_report_for_result,
    reject_plausibility_only_assignment,
)
from services.hybrid_schema import HybridSchemaError, parse_recovery_number

_MISSING_TEXT = {"", "-", "—", "--", "–", "NA", "N/A", "NULL", "NONE", "."}
_NUMERIC_CONFUSION_RE = re.compile(
    r"(?i)\b(?:[oOlI|]{1,}\d|\d+[oOlI|]+|\d+[.,]\d+[.,]\d+)\b"
)
# Long digit runs often indicate glued adjacent qty columns (e.g. 106|100 → 106100).
_GLUED_QTY_RE = re.compile(r"^\d{6,}$")
# Liquidation / age text landed in a qty column → do not accept as quantity.
_DAYS_IN_QTY_RE = re.compile(r"(?i)\b\d+\s*days?\b")


@dataclass
class CellIssue:
    field: str
    category: str
    message: str
    product_name: Optional[str] = None
    raw_value: Any = None
    evidence_ref: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field": self.field,
            "category": self.category,
            "message": self.message,
            "product_name": self.product_name,
            "raw_value": self.raw_value,
            "evidence_ref": self.evidence_ref,
        }


@dataclass
class CellValidationReport:
    issues: List[CellIssue] = field(default_factory=list)
    column_report: Optional[ColumnMappingReport] = None
    accepted_critical_fields: int = 0
    unresolved_critical_fields: int = 0

    @property
    def has_blocking_issues(self) -> bool:
        blocking = {
            "COLUMN_CONFUSION",
            "NUMERIC_CHARACTER_CONFUSION",
            "NUMERIC_CONCATENATION",
            "MISSING_DIGITS",
            "INVALID_NORMALIZATION",
            "PLAUSIBILITY_ONLY_ASSIGNMENT",
            "HEADER_VALUE_MISMATCH",
            "ROW_SHIFT",
            "TRUNCATED_CELL",
            "EMPTY_OCR_PAGE",
            "AMBIGUOUS_MAPPING",
            "OCR_DISAGREEMENT",
            "UNVERIFIED_OCR_FALLBACK",
            "STOCK_IDENTITY_MISMATCH",
            "SOURCE_DISCREPANCY",
        }
        return any(issue.category in blocking for issue in self.issues)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issues": [issue.to_dict() for issue in self.issues],
            "column_report": self.column_report.to_dict()
            if self.column_report
            else None,
            "accepted_critical_fields": self.accepted_critical_fields,
            "unresolved_critical_fields": self.unresolved_critical_fields,
            "has_blocking_issues": self.has_blocking_issues,
        }


def validate_critical_cells(
    result: Dict[str, Any],
    *,
    ocr_evidence: Optional[Dict[str, Any]] = None,
    column_headers: Optional[Sequence[Any]] = None,
    column_cells: Optional[Sequence[Any]] = None,
    ocr_disagreement: bool = False,
    unverified_ocr_fallback: bool = False,
) -> CellValidationReport:
    """Validate critical quantities against no-guessing + geometry rules."""
    report = CellValidationReport()
    report.column_report = column_report_for_result(
        result, headers=column_headers, cells=column_cells
    )
    if report.column_report.has_column_confusion:
        report.issues.append(
            CellIssue(
                field="column_mapping",
                category="COLUMN_CONFUSION",
                message="ambiguous or shifted column mapping",
            )
        )
    if report.column_report.ambiguous_fields:
        report.issues.append(
            CellIssue(
                field="column_mapping",
                category="AMBIGUOUS_MAPPING",
                message=",".join(report.column_report.ambiguous_fields),
            )
        )

    if unverified_ocr_fallback:
        report.issues.append(
            CellIssue(
                field="ocr_engine",
                category="UNVERIFIED_OCR_FALLBACK",
                message="primary OCR unavailable; fallback must not be marked verified",
            )
        )
    if ocr_disagreement:
        report.issues.append(
            CellIssue(
                field="ocr_engine",
                category="OCR_DISAGREEMENT",
                message="engines disagree; source evidence required",
            )
        )

    if ocr_evidence:
        report.issues.extend(_ocr_page_issues(ocr_evidence))

    items = _line_items(result)
    for item in items:
        product = str(item.get("product_name") or "") or None
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if extra.get("source_discrepancy") is True:
            report.issues.append(
                CellIssue(
                    field="source",
                    category="SOURCE_DISCREPANCY",
                    message=str(
                        extra.get("source_validation_error")
                        or extra.get("validation_error")
                        or "extracted value disagrees with the source document"
                    ),
                    product_name=product,
                    evidence_ref=extra.get("source_evidence"),
                )
            )
        identity_mismatch = extra.get("stock_identity_ok") is False
        for field_name in CRITICAL_QTY_FIELDS:
            if field_name not in item and field_name not in (item.get("extra") or {}):
                continue
            raw = item.get(field_name)
            if raw is None and isinstance(item.get("extra"), dict):
                raw = item["extra"].get(field_name)
            source = None
            if isinstance(item.get("extra"), dict):
                source = (item["extra"].get("field_sources") or {}).get(field_name)

            if source and reject_plausibility_only_assignment(
                canonical_field=field_name, source=str(source)
            ):
                report.issues.append(
                    CellIssue(
                        field=field_name,
                        category="PLAUSIBILITY_ONLY_ASSIGNMENT",
                        message="critical quantity assigned without source geometry",
                        product_name=product,
                        raw_value=raw,
                    )
                )
                report.unresolved_critical_fields += 1
                continue

            # Identity failure is diagnostic only — never used to rewrite values,
            # but critical qtys on a failing row must not be silently accepted.
            if identity_mismatch and raw is not None and not _is_missing_token(raw):
                report.issues.append(
                    CellIssue(
                        field=field_name,
                        category="STOCK_IDENTITY_MISMATCH",
                        message="row stock identity failed; critical qty unresolved",
                        product_name=product,
                        raw_value=raw,
                    )
                )
                report.unresolved_critical_fields += 1
                continue

            issue = _numeric_cell_issue(field_name, raw, product_name=product)
            if issue:
                report.issues.append(issue)
                report.unresolved_critical_fields += 1
            else:
                # Explicit zero counts as accepted; blank/null does not invent zero.
                if raw is None or _is_missing_token(raw):
                    report.unresolved_critical_fields += 1
                else:
                    report.accepted_critical_fields += 1

        # Detect blank→zero promotion markers.
        if extra.get("null_promoted_to_zero") or extra.get("blank_coerced_to_zero"):
            report.issues.append(
                CellIssue(
                    field="quantity",
                    category="INVALID_NORMALIZATION",
                    message="blank/missing coerced to zero",
                    product_name=product,
                )
            )

    return report


def _numeric_cell_issue(
    field_name: str, raw: Any, *, product_name: Optional[str]
) -> Optional[CellIssue]:
    if raw is None or _is_missing_token(raw):
        return None
    if isinstance(raw, bool):
        return CellIssue(
            field=field_name,
            category="INVALID_NORMALIZATION",
            message="boolean is not a quantity",
            product_name=product_name,
            raw_value=raw,
        )
    text = str(raw).strip()
    if _DAYS_IN_QTY_RE.search(text):
        return CellIssue(
            field=field_name,
            category="HEADER_VALUE_MISMATCH",
            message="qty column contains liquidation/days text",
            product_name=product_name,
            raw_value=raw,
        )
    if _NUMERIC_CONFUSION_RE.search(text):
        return CellIssue(
            field=field_name,
            category="NUMERIC_CHARACTER_CONFUSION",
            message="suspicious OCR digit/letter confusion",
            product_name=product_name,
            raw_value=raw,
        )
    if isinstance(raw, str) and _GLUED_QTY_RE.match(text.replace(",", "")):
        # Concatenation of two typical qty columns (e.g. 33|37 → 3337).
        return CellIssue(
            field=field_name,
            category="NUMERIC_CONCATENATION",
            message="possible glued adjacent quantities",
            product_name=product_name,
            raw_value=raw,
        )
    try:
        parse_recovery_number(raw, field_name)
    except HybridSchemaError:
        return CellIssue(
            field=field_name,
            category="INVALID_NORMALIZATION",
            message="cannot parse quantity without coercion",
            product_name=product_name,
            raw_value=raw,
        )
    return None


def _ocr_page_issues(ocr_evidence: Dict[str, Any]) -> List[CellIssue]:
    issues: List[CellIssue] = []
    pages = ocr_evidence.get("pages") if isinstance(ocr_evidence, dict) else None
    if not isinstance(pages, list):
        return issues
    for page in pages:
        if not isinstance(page, dict):
            continue
        status = str(page.get("status") or "")
        text = str(page.get("text") or "")
        # summary() pops text but keeps text_length — honor that so gates
        # do not false-positive EMPTY_OCR_PAGE on API summaries.
        text_length = page.get("text_length")
        try:
            effective_len = len(text.strip()) if text.strip() else int(text_length or 0)
        except (TypeError, ValueError):
            effective_len = len(text.strip())
        if status == "error" or page.get("error"):
            issues.append(
                CellIssue(
                    field="ocr_page",
                    category="TRUNCATED_CELL",
                    message=str(page.get("error") or "ocr_page_error"),
                    evidence_ref=f"page:{page.get('page')}",
                )
            )
        if status == "success" and effective_len < 8:
            issues.append(
                CellIssue(
                    field="ocr_page",
                    category="EMPTY_OCR_PAGE",
                    message="ocr page nearly empty",
                    evidence_ref=f"page:{page.get('page')}",
                )
            )
        tables = page.get("tables")
        if isinstance(tables, dict) and tables.get("available") is False:
            # Not always an error for non-table pages; only flag when text is also thin.
            if effective_len < 40:
                issues.append(
                    CellIssue(
                        field="ocr_table",
                        category="EMPTY_OCR_PAGE",
                        message="no table evidence on sparse OCR page",
                        evidence_ref=f"page:{page.get('page')}",
                    )
                )
        elif isinstance(tables, dict) and tables.get("available") is True:
            # Summary form with tables available counts as non-empty evidence.
            pass
    return issues


def _is_missing_token(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().upper() in _MISSING_TEXT:
        return True
    return False


def _line_items(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    statements = result.get("statements")
    if result.get("multi_statement") and isinstance(statements, list) and statements:
        sources = [s for s in statements if isinstance(s, dict)]
    else:
        sources = [result]
    for statement in sources:
        for item in statement.get("line_items") or []:
            if isinstance(item, dict):
                rows.append(item)
    return rows
