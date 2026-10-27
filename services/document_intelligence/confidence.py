"""Multi-dimension confidence. No single magic score overrides evidence."""

from __future__ import annotations

from typing import Any, Dict, Optional


def build_confidence(
    *,
    layout_confidence: float = 0.0,
    mapping_confidence: float = 0.0,
    ocr_confidence: Optional[float] = None,
    evidence_confidence: float = 0.0,
    schema_ok: bool = False,
    business_ok: bool = False,
    cross_engine_agreement: Optional[bool] = None,
    known_parser: bool = False,
) -> Dict[str, Any]:
    schema_confidence = 1.0 if schema_ok else 0.0
    business_validation = 1.0 if business_ok else 0.0
    agreement = cross_engine_agreement
    if agreement is True:
        agreement_score = 1.0
    elif agreement is False:
        agreement_score = 0.0
    else:
        agreement_score = None

    # Rule-based overall: business cannot compensate for missing evidence.
    if not schema_ok or not business_ok:
        overall = "low"
    elif evidence_confidence < 0.4 or (ocr_confidence is not None and ocr_confidence < 0.4):
        overall = "low"
    elif agreement is False:
        overall = "low"
    elif known_parser and evidence_confidence >= 0.6 and mapping_confidence >= 0.5:
        overall = "high"
    elif evidence_confidence >= 0.55 and layout_confidence >= 0.5:
        overall = "medium"
    else:
        overall = "low"

    return {
        "schema_confidence": schema_confidence,
        "evidence_confidence": float(evidence_confidence),
        "ocr_confidence": ocr_confidence,
        "layout_confidence": float(layout_confidence),
        "mapping_confidence": float(mapping_confidence),
        "business_validation": business_validation,
        "cross_engine_agreement": agreement_score,
        "overall": overall,
        "known_parser": known_parser,
    }
