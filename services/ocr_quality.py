"""OCR quality evaluation for routing between text and vision models."""

import re
from typing import Dict, Optional

from services.config import EXTRACTION_CONFIG


_INVOICE_KEYWORDS = [
    "invoice", "inv no", "inv.", "bill", "tax invoice", "gstin", "gst",
    "hsn", "cgst", "sgst", "igst", "total", "amount", "qty", "quantity",
    "batch", "exp", "mrp", "rate", "net amount", "grand total",
]

_GSTIN_PATTERN = re.compile(
    r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b", re.IGNORECASE
)
_INVOICE_NO_PATTERN = re.compile(
    r"\b(?:invoice|inv\.?|bill)\s*(?:no\.?|number|#)?\s*[:#-]?\s*([A-Z0-9/\-]{3,25})\b",
    re.IGNORECASE,
)
_DATE_PATTERN = re.compile(
    r"\b(?:\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{4}[-/]\d{1,2}[-/]\d{1,2})\b"
)
_AMOUNT_PATTERN = re.compile(
    r"\b(?:total|grand\s*total|net\s*amount|invoice\s*total)\b[^\n]{0,40}?\b\d[\d,]*\.?\d*\b",
    re.IGNORECASE,
)


def _readable_char_ratio(text: str) -> float:
    if not text:
        return 0.0
    readable = sum(
        1 for ch in text
        if ch.isalnum() or ch.isspace() or ch in ".,/-()%|@#&*+:"
    )
    return readable / max(len(text), 1)


def _invoice_keyword_score(text: str) -> float:
    if not text:
        return 0.0
    lower = text.lower()
    hits = sum(1 for kw in _INVOICE_KEYWORDS if kw in lower)
    return min(100.0, (hits / max(len(_INVOICE_KEYWORDS), 1)) * 100.0 * 3.5)


def analyze_ocr_quality(
    text: str,
    confidence: float = 0.0,
    thresholds: Optional[object] = None,
) -> Dict:
    """
    Evaluate OCR text quality and return routing metadata.

    Returns:
        {
            "confidence": float,
            "quality": "GOOD" | "POOR",
            "reason": str,
            "score": int,
            "text_length": int,
            "readable_ratio": float,
            "invoice_keyword_score": float,
            "gstin_detected": bool,
            "invoice_number_detected": bool,
            "date_detected": bool,
            "amount_detected": bool,
        }
    """
    cfg = thresholds or EXTRACTION_CONFIG
    cleaned = (text or "").strip()
    text_length = len(cleaned)
    readable_ratio = _readable_char_ratio(cleaned)
    keyword_score = _invoice_keyword_score(cleaned)

    gstin_detected = bool(_GSTIN_PATTERN.search(cleaned))
    invoice_number_detected = bool(_INVOICE_NO_PATTERN.search(cleaned))
    date_detected = bool(_DATE_PATTERN.search(cleaned))
    amount_detected = bool(_AMOUNT_PATTERN.search(cleaned))

    score = 0.0
    score += min(confidence, 100) * 0.35
    score += min(text_length / max(cfg.ocr_min_text_length, 1), 1.0) * 25
    score += readable_ratio * 20
    score += keyword_score * 0.15
    if gstin_detected:
        score += 8
    if invoice_number_detected:
        score += 6
    if date_detected:
        score += 3
    if amount_detected:
        score += 3

    reasons = []
    if confidence < cfg.ocr_confidence_threshold:
        reasons.append(f"confidence {confidence:.1f} < {cfg.ocr_confidence_threshold}")
    if text_length < cfg.ocr_min_text_length:
        reasons.append(f"text length {text_length} < {cfg.ocr_min_text_length}")
    if readable_ratio < cfg.ocr_min_readable_ratio:
        reasons.append(
            f"readable ratio {readable_ratio:.2f} < {cfg.ocr_min_readable_ratio}"
        )

    quality = "GOOD" if not reasons else "POOR"

    return {
        "confidence": round(float(confidence), 1),
        "quality": quality,
        "reason": "; ".join(reasons),
        "score": int(round(score)),
        "text_length": text_length,
        "readable_ratio": round(readable_ratio, 4),
        "invoice_keyword_score": round(keyword_score, 1),
        "gstin_detected": gstin_detected,
        "invoice_number_detected": invoice_number_detected,
        "date_detected": date_detected,
        "amount_detected": amount_detected,
    }
