"""Shared source-evidence gates for validation / grounding / verification."""

from __future__ import annotations

from typing import Any, Dict, Optional


def has_valid_source_evidence(
    ocr_di_result: Optional[Dict[str, Any]] = None,
    extraction_result: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when grounding/validation has usable source text or table HTML."""
    ocr_di = ocr_di_result if isinstance(ocr_di_result, dict) else {}
    result = extraction_result if isinstance(extraction_result, dict) else {}
    extra = _extra(result)

    table_html = str(
        ocr_di.get("table_html")
        or extra.get("ocr_table_html")
        or ""
    ).strip()
    table_available = bool(
        ocr_di.get("table_available")
        or extra.get("table_available")
        or ("<table" in table_html.lower())
    )
    method = str(
        extra.get("extraction_method")
        or result.get("extraction_method")
        or ""
    )

    if table_available and table_html:
        return True
    if method == "vl_html_table_parse" and table_html:
        return True

    text = str(ocr_di.get("text") or "").strip()
    if text:
        return True
    if isinstance(result.get("_grounding_ocr_text"), str) and result["_grounding_ocr_text"].strip():
        return True

    return False


def _extra(result: Dict[str, Any]) -> Dict[str, Any]:
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    return extra
