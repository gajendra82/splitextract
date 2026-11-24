"""Indian pharma number / date / null normalization helpers."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

_NULL_TOKENS = {
    "",
    "-",
    "—",
    "--",
    "–",
    "NA",
    "N/A",
    "NULL",
    "NONE",
    ".",
    "NIL",
}

# Indian lakh grouping: 1,25,000 or 12,34,567
_INDIAN_COMMA_RE = re.compile(r"^\d{1,3}(,\d{2})+(,\d{3})?$|^\d{1,2}(,\d{2})+,\d{3}$")
_WESTERN_COMMA_RE = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")
_PAREN_NEG_RE = re.compile(r"^\((.+)\)$")
_CR_DR_RE = re.compile(r"(?i)\b(cr|dr|credit|debit)\b")
_CURRENCY_RE = re.compile(r"(?i)(?:₹|rs\.?|inr)\s*")


def is_null_cell(raw: Any) -> bool:
    if raw is None:
        return True
    text = str(raw).replace("\xa0", " ").strip()
    return text.upper() in _NULL_TOKENS or text == ""


def parse_indian_number(raw: Any) -> Optional[float | int]:
    """Parse Indian / Western numeric cells.

    Empty / dash / N/A → None (not 0).
    Explicit ``0`` / ``0.00`` → 0.
    ``1,25,000`` → 125000; ``(500)`` / ``500 CR`` → -500.
    """
    if raw is None:
        return None
    text = str(raw).replace("\xa0", " ").strip()
    if is_null_cell(text):
        return None

    negative = False
    paren = _PAREN_NEG_RE.match(text)
    if paren:
        text = paren.group(1).strip()
        negative = True

    if _CR_DR_RE.search(text):
        # Accounting: CR often credits (negative stock movement); treat as negative.
        if re.search(r"(?i)\b(cr|credit)\b", text):
            negative = True
        text = _CR_DR_RE.sub("", text).strip()

    text = _CURRENCY_RE.sub("", text)
    text = re.sub(r"\s+", "", text)

    # Keep digits, commas, dots, minus.
    cleaned = re.sub(r"[^\d,.\-]", "", text)
    if not cleaned or cleaned in {".", "-", ",", "-."}:
        return None

    if cleaned.startswith("-"):
        negative = True
        cleaned = cleaned[1:]

    # Strip grouping commas (Indian or Western).
    if "," in cleaned:
        # Drop all commas; Indian and Western both become plain digits.
        cleaned = cleaned.replace(",", "")

    try:
        value = float(cleaned)
    except ValueError:
        return None

    if negative:
        value = -abs(value)
    if value.is_integer():
        return int(value)
    return value


def parse_indian_date(raw: Any, *, today: Optional[date] = None) -> Optional[str]:
    """Normalize Indian DD-MM-YYYY / DD/MM/YYYY to ISO ``YYYY-MM-DD``."""
    if raw is None:
        return None
    text = str(raw).replace("\xa0", " ").strip()
    if is_null_cell(text):
        return None

    # Artifact like 01/09/2023/2026 → keep DD/MM + sensible 4-digit year.
    parts = re.split(r"[/\-.]", text)
    parts = [p for p in parts if p]
    if len(parts) >= 3 and all(re.fullmatch(r"\d+", p) for p in parts[:3]):
        day_s, month_s = parts[0], parts[1]
        years = [p for p in parts[2:] if len(p) == 4]
        year_s = years[-1] if years else parts[2]
        if len(year_s) == 2:
            year_s = f"20{year_s}"
        try:
            day_i, month_i, year_i = int(day_s), int(month_s), int(year_s)
        except ValueError:
            return None
        # If month > 12, swap (US-style leaked as MM-DD).
        if month_i > 12 and day_i <= 12:
            day_i, month_i = month_i, day_i
        # Prefer year closest to today when multiple 4-digit years appear.
        if len(years) > 1:
            ref = today or date.today()
            year_i = min((int(y) for y in years), key=lambda y: abs(y - ref.year))
        try:
            return date(year_i, month_i, day_i).isoformat()
        except ValueError:
            return None

    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def ground_value(value: Any, row_text: str) -> bool:
    """Numeric-normalized containment check for row-scoped grounding.

    Digits are compared without commas/spaces/dots. A match must not be a
    longer digit run (so 1250 does not match inside 12500).
    """
    if value is None:
        return True
    v = str(value).replace(",", "").replace(".", "").replace(" ", "")
    t = str(row_text or "").replace(",", "").replace(".", "").replace(" ", "")
    if not v:
        return True
    if not v.isdigit():
        return v.casefold() in t.casefold()
    return re.search(rf"(?<!\d){re.escape(v)}(?!\d)", t) is not None


def totals_within_tolerance(
    extracted_total: Any,
    ocr_total: Any,
    *,
    rel_tol: float = 0.005,
) -> bool:
    """True when relative difference is under ``rel_tol`` (default 0.5%)."""
    a = parse_indian_number(extracted_total)
    b = parse_indian_number(ocr_total)
    if a is None or b is None:
        return False
    if b == 0:
        return a == 0
    return abs(float(a) - float(b)) / abs(float(b)) < rel_tol
