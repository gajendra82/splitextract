"""Classify existing repository reference data by provenance."""

from __future__ import annotations

from typing import Any, Dict, Optional

SOURCE_VERIFIED = "SOURCE_VERIFIED"
MODEL_DERIVED = "MODEL_DERIVED"
PARSER_DERIVED = "PARSER_DERIVED"
UNKNOWN = "UNKNOWN"


def classify_reference_source(payload: Optional[Dict[str, Any]]) -> str:
    """Return provenance class for an existing QA / Phase artifact.

    Rules (conservative — when unsure, return UNKNOWN):
    - Embedded PDF native text / human visual inspection → SOURCE_VERIFIED
    - Tesseract / Paddle / Gemini / other OCR-as-truth → MODEL_DERIVED
    - Production parser / Phase 3 extract → PARSER_DERIVED
    """
    if not isinstance(payload, dict):
        return UNKNOWN

    # Explicit flags win.
    if payload.get("source_verified") is True:
        return SOURCE_VERIFIED
    if payload.get("source_verified") is False:
        return UNKNOWN
    if str(payload.get("provenance") or "").upper() in {
        SOURCE_VERIFIED,
        MODEL_DERIVED,
        PARSER_DERIVED,
        UNKNOWN,
    }:
        return str(payload.get("provenance")).upper()

    source = str(payload.get("source") or "").strip().lower()
    notes = " ".join(str(n) for n in (payload.get("notes") or [])).lower()
    method = str(
        payload.get("verification_method")
        or payload.get("extraction_method")
        or ""
    ).lower()

    if source in {"embedded", "native", "native_text", "pdf_embedded"}:
        return SOURCE_VERIFIED
    if "source_visual" in method or "human" in method or "manual" in method:
        return SOURCE_VERIFIED

    if source in {"ocr", "tesseract", "paddle", "gemini", "vision"}:
        return MODEL_DERIVED
    if "tesseract" in notes or "ocr used" in notes or "gemini" in notes:
        return MODEL_DERIVED
    if "paddle" in notes or "surya" in notes or "chandra" in notes:
        return MODEL_DERIVED

    if source in {"parser", "extracted", "phase3", "api"}:
        return PARSER_DERIVED
    if "parser" in method or "extract_sales" in method:
        return PARSER_DERIVED

    # qa.ground_truth build with mixed pages: only embedded is verified.
    page_sources = payload.get("page_sources") or payload.get("pages")
    if isinstance(page_sources, list) and page_sources:
        kinds = {
            str(p.get("source") or "").lower()
            for p in page_sources
            if isinstance(p, dict)
        }
        if kinds and kinds <= {"embedded", "native", "native_text"}:
            return SOURCE_VERIFIED
        if "ocr" in kinds:
            return MODEL_DERIVED

    return UNKNOWN


def inventory_reference_policy() -> Dict[str, str]:
    """Static policy description for the Phase 4 GT report."""
    return {
        "qa_results/ground_truth (embedded PDF pages)": SOURCE_VERIFIED,
        "qa_results/ground_truth (OCR-fallback pages)": MODEL_DERIVED,
        "qa_results_images/ground_truth": MODEL_DERIVED,
        "phase3_eval/records extraction": PARSER_DERIVED,
        "Phase 3 PASS/FAIL / stock identity": PARSER_DERIVED,
        "Tesseract / Paddle / Gemini outputs": MODEL_DERIVED,
        "Human / agent visual inspection of original file": SOURCE_VERIFIED,
        "Native spreadsheet cell values read from source file": SOURCE_VERIFIED,
    }
