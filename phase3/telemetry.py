"""Read-only Phase 3 telemetry.

Projects fields that already exist on a finished extraction result. It does
not call a parser, a model, or OCR, and it does not write back into the result.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _method(result: Dict[str, Any]) -> str:
    totals = _dict(result.get("totals"))
    extra = _dict(totals.get("extra"))
    return str(extra.get("extraction_method") or "")


def _layout(result: Dict[str, Any]) -> str:
    totals = _dict(result.get("totals"))
    extra = _dict(totals.get("extra"))
    return str(extra.get("layout") or extra.get("statement_layout") or "")


def _profile_id(result: Dict[str, Any]) -> str:
    """Read the existing profile selector. Selection does not mutate line items."""
    snapshot = copy.deepcopy(result)
    try:
        from services.stock_identity_profiles import select_profile

        return str(select_profile(snapshot).profile_id or "")
    except Exception:
        return ""


def _evidence_methods(phase1: Dict[str, Any]) -> List[str]:
    evidence = _dict(phase1.get("evidence"))
    methods = []
    for page in evidence.get("pages") or []:
        if isinstance(page, dict) and page.get("extraction_method"):
            methods.append(str(page.get("extraction_method")))
    return methods


def ocr_path(
    *,
    file_kind: str = "",
    pdf_kind: str = "",
    requires_ocr: bool = False,
    extraction_method: str = "",
    evidence_methods: Optional[List[str]] = None,
) -> str:
    """Name the path the current system already took. Does not select an engine."""
    method = (extraction_method or "").lower()
    evidence = [item.lower() for item in (evidence_methods or [])]
    if "paddle" in method or any("paddle" in item for item in evidence):
        return "paddleocr"
    if "tesseract" in method or any("tesseract" in item for item in evidence):
        return "tesseract_legacy"
    if file_kind in {"xls", "xlsx"}:
        return "spreadsheet"
    if file_kind in {"doc", "docx", "txt", "html"}:
        return "native_text"
    if file_kind == "image" or requires_ocr or pdf_kind == "scanned":
        return "ocr_required"
    if any(item == "embedded_sparse" for item in evidence):
        return "pymupdf_fallback"
    if pdf_kind == "digital" or "pymupdf" in evidence or file_kind == "pdf":
        return "digital_text"
    return "other"


def project_telemetry(
    result: Optional[Dict[str, Any]],
    *,
    document_id: str,
    file_kind: str = "",
    pdf_kind: str = "",
    requires_ocr: Optional[bool] = None,
    processing_time_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Return a new metadata dict. ``result`` is not modified."""
    result = result if isinstance(result, dict) else {}
    phase1 = _dict(result.get("phase1"))
    route = _dict(phase1.get("route"))
    validation = _dict(phase1.get("validation"))
    phase2 = _dict(result.get("phase2"))
    if requires_ocr is None:
        requires_ocr = bool(route.get("requires_ocr"))
    method = _method(result) or str(validation.get("parser") or "")
    evidence_methods = _evidence_methods(phase1)
    kind = file_kind or str(route.get("file_kind") or "")
    kind_pdf = pdf_kind or str(route.get("pdf_kind") or "")
    return {
        "document_id": document_id,
        "document_type": str(route.get("document_type") or ""),
        "file_kind": kind,
        "pdf_kind": kind_pdf or None,
        "extraction_method": method,
        "layout": _layout(result),
        "profile": _profile_id(result),
        "ocr_required": bool(requires_ocr),
        "ocr_path": ocr_path(
            file_kind=kind,
            pdf_kind=kind_pdf,
            requires_ocr=bool(requires_ocr),
            extraction_method=method,
            evidence_methods=evidence_methods,
        ),
        "ocr_quality": validation.get("quality_score"),
        "evidence_methods": evidence_methods,
        "phase1_accepted": validation.get("accepted"),
        "phase1_status": validation.get("status"),
        "failure_type": phase2.get("failure_type") or validation.get("failure_type"),
        "recovery_strategy": phase2.get("strategy"),
        "attempt_number": phase2.get("attempts"),
        "evidence_type": phase2.get("evidence_type") or "not_recorded",
        "mapping_status": phase2.get("mapping_status") or "not_recorded",
        "projection_status": phase2.get("projection_status") or "not_recorded",
        "candidate_status": phase2.get("status"),
        "final_validation_accepted": _final_accepted(result),
        "final_status": phase2.get("status") or validation.get("status"),
        "processing_time_ms": processing_time_ms,
    }


def _final_accepted(result: Dict[str, Any]) -> Any:
    phase1 = _dict(result.get("phase1"))
    validation = _dict(phase1.get("validation"))
    return validation.get("accepted")
