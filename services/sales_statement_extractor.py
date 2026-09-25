"""Sales / stock statement extraction into a unified JSON schema.

Separate from invoice OCR — do not use for tax invoices.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".txt",
    ".htm",
    ".html",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
TEXT_EXTENSIONS = {".txt"}
WORD_EXTENSIONS = {".doc", ".docx"}


_MONTH_MAP = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def empty_result(source_file: str = "", source_format: str = "") -> Dict[str, Any]:
    return {
        "source_file": source_file,
        "source_format": source_format,
        "stockist_name": None,
        "stockist_address": None,
        "company_name": None,
        "period_from": None,
        "period_to": None,
        "report_title": None,
        "line_items": [],
        "totals": {
            "sales_value": None,
            "closing_value": None,
            "extra": {},
        },
    }


def empty_line_item() -> Dict[str, Any]:
    return {
        "product_code": None,
        "product_name": None,
        "packing": None,
        "opening_qty": 0.0,
        "receipts_qty": 0.0,
        "sales_qty": 0.0,
        "sales_value": 0.0,
        "closing_qty": 0.0,
        "closing_value": 0.0,
        "extra": {},
    }


_EMPTY_NUMERIC = {
    "-",
    "—",
    "--",
    "NA",
    "N/A",
    "NULL",
    "NONE",
    "#N/A",
    "#VALUE!",
    "#REF!",
    "#DIV/0!",
    "#NAME?",
}


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\u00a0", " ")
    if not text or text.upper() in _EMPTY_NUMERIC:
        return default
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    text = re.sub(r"^(?:₹|\$|(?:Rs\.?|INR)\s*)", "", text, flags=re.I).strip()
    text = re.sub(r"\s+", "", text)
    if text.startswith("="):
        return default
    if re.fullmatch(r"-?\d+,\d{1,2}", text):
        text = text.replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+,\d+", text):
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")
    text = text.replace("L", "").replace("l", "")
    if not text:
        return default
    try:
        number = float(text)
    except ValueError:
        m = re.search(r"-?\d+(?:\.\d+)?", text)
        number = float(m.group(0)) if m else default
    if negative and number:
        return -abs(number)
    return number


def _to_nullable_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _to_float(value, 0.0)


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
_YMD_RE = re.compile(r"^(\d{4})[./](\d{1,2})[./](\d{1,2})(?:\b|[ T])")
_EXCEL_HEADER_SCAN_ROWS = 20
_DATE_TOKEN_RE = (
    r"(?:"
    r"\d{4}[./-]\d{1,2}[./-]\d{1,2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?"
    r"|"
    r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
    r"|"
    r"\d{1,2}[- ][A-Za-z]{3,9}[- ]\d{2,4}"
    r")"
)


def _is_excel_date_number_format(number_format: Optional[str]) -> bool:
    """True when an Excel number format is a date/datetime, not qty/money."""
    if not number_format:
        return False
    fmt = str(number_format).strip()
    if not fmt or fmt.lower() in {"general", "standard", "@", "0"}:
        return False
    lowered = fmt.lower().replace("\\", "")
    if any(tok in lowered for tok in ("yy", "yyyy", "dddd", "mmm")):
        return True
    return bool(re.search(r"(mm|m|dd|d)[-/.\s](mm|m|dd|d)", lowered))


def _normalize_date(raw: Optional[Any]) -> Optional[str]:
    """Normalize a statement date to YYYY-MM-DD using DD/MM/YYYY business rules.

    Does not treat bare numbers as Excel serials. Use `_normalize_excel_date`
    when a cell number format is available.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")
    if isinstance(raw, date):
        return raw.strftime("%Y-%m-%d")

    text = str(raw).strip()
    if not text or text in {"-", "—", "None", "NaT", "nat"}:
        return None

    # YYYY-MM-DD / ISO datetime — must run before DD/MM/YYYY search.
    # Otherwise "2026-08-01" is misread as 26-08-01 → 2001-08-26.
    m = _ISO_DATETIME_RE.match(text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime(
                "%Y-%m-%d"
            )
        except ValueError:
            return None
    m = _YMD_RE.match(text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime(
                "%Y-%m-%d"
            )
        except ValueError:
            pass

    # 01/06/2026, 01-06-2026, 01.06.2026, 01/06/26 (Indian DD/MM/YYYY)
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # 01-Jun-26 / 01-Jun-2026
    m = re.search(r"(\d{1,2})[- ]([A-Za-z]{3,9})[- ](\d{2,4})", text)
    if m:
        d = int(m.group(1))
        mo = _MONTH_MAP.get(m.group(2).lower()[:3]) or _MONTH_MAP.get(m.group(2).lower())
        y = int(m.group(3))
        if y < 100:
            y += 2000
        if mo:
            try:
                return datetime(y, mo, d).strftime("%Y-%m-%d")
            except ValueError:
                pass

    # Month Of Jul 2026 → first/last day handled by caller period helpers
    return None


def _normalize_excel_date(
    value: Any, number_format: Optional[str] = None
) -> Optional[str]:
    """Normalize an Excel header/metadata cell to YYYY-MM-DD or None.

    Numeric values become dates only when the cell number format is a date
    format (or xlrd marked the cell as a date). Quantities and sales amounts
    are never treated as serial dates.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None

    try:
        import pandas as pd

        if value is getattr(pd, "NaT", None) or (
            hasattr(pd, "isna") and isinstance(value, pd.Timestamp) and pd.isna(value)
        ):
            return None
        if isinstance(value, pd.Timestamp):
            return value.to_pydatetime().strftime("%Y-%m-%d")
    except Exception:
        pass

    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")

    if isinstance(value, (int, float)):
        if not _is_excel_date_number_format(number_format):
            return None
        serial = float(value)
        if not (20000.0 <= serial <= 80000.0):
            return None
        try:
            return (datetime(1899, 12, 30) + timedelta(days=serial)).strftime("%Y-%m-%d")
        except Exception:
            return None

    return _normalize_date(value)


def _excel_header_cell_text(value: Any, number_format: Optional[str] = None) -> str:
    """Stringify a header cell. Date-only cells become YYYY-MM-DD.

    Title cells such as 'Sales ... From 01/08/2026 To 29/08/2026' keep their
    full text so From/To can still be parsed. Do not collapse them to the
    first embedded date.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    try:
        import pandas as pd

        if isinstance(value, pd.Timestamp) and not pd.isna(value):
            return value.to_pydatetime().strftime("%Y-%m-%d")
    except Exception:
        pass
    if isinstance(value, (int, float)):
        return _normalize_excel_date(value, number_format) or ""
    text = str(value).strip()
    if not text or text in {"-", "—"}:
        return ""
    if re.fullmatch(_DATE_TOKEN_RE, text):
        return _normalize_date(text) or text
    return text


def _apply_excel_header_period(
    result: Dict[str, Any],
    rows: List[List[Any]],
    formats: Optional[List[List[Optional[str]]]] = None,
) -> Dict[str, Any]:
    """Read period_from / period_to from the Excel header/metadata region only."""
    if not isinstance(result, dict):
        return result
    formats = formats or []
    for ri, row in enumerate(rows[:_EXCEL_HEADER_SCAN_ROWS]):
        if not isinstance(row, list):
            continue
        fmts = formats[ri] if ri < len(formats) and isinstance(formats[ri], list) else []
        texts: List[str] = []
        normalized_cells: List[Optional[str]] = []
        for ci, cell in enumerate(row):
            fmt = fmts[ci] if ci < len(fmts) else None
            normalized_cells.append(_normalize_excel_date(cell, fmt))
            text = _excel_header_cell_text(cell, fmt)
            if text:
                texts.append(text)
        joined = " ".join(texts)
        if not joined:
            continue

        m = re.search(
            r"(?:From(?:\s*date)?|Period(?:\s*from)?)\s*:?\s*"
            rf"({_DATE_TOKEN_RE})\s+(?:to|[-–])\s*:?\s*({_DATE_TOKEN_RE})",
            joined,
            re.I,
        )
        if m:
            pf, pt = _normalize_date(m.group(1)), _normalize_date(m.group(2))
            if pf and not result.get("period_from"):
                result["period_from"] = pf
            if pt and not result.get("period_to"):
                result["period_to"] = pt
            continue

        m = re.search(
            rf"(?:From(?:\s*date)?)\s*:?\s*({_DATE_TOKEN_RE})\b",
            joined,
            re.I,
        )
        if m and not result.get("period_from"):
            result["period_from"] = _normalize_date(m.group(1))

        m = re.search(
            rf"(?:(?<!From )To(?:\s*date)?)\s*:?\s*({_DATE_TOKEN_RE})\b",
            joined,
            re.I,
        )
        if m and not result.get("period_to"):
            result["period_to"] = _normalize_date(m.group(1))

        if (
            (not result.get("period_from") or not result.get("period_to"))
            and re.search(r"STOCK|SALES|STATEMENT|PERIOD|FROM|DATE", joined, re.I)
        ):
            m = re.search(
                rf"({_DATE_TOKEN_RE})\s*(?:to|[-–])\s*({_DATE_TOKEN_RE})",
                joined,
                re.I,
            )
            if m:
                pf, pt = _normalize_date(m.group(1)), _normalize_date(m.group(2))
                if pf and pt:
                    result["period_from"] = result.get("period_from") or pf
                    result["period_to"] = result.get("period_to") or pt

        if not result.get("period_from") or not result.get("period_to"):
            m = re.search(
                rf"(?:Statement\s*Date|Report\s*Date|Period)\s*:?\s*({_DATE_TOKEN_RE})",
                joined,
                re.I,
            )
            if m:
                parsed = _normalize_date(m.group(1))
                if parsed:
                    result["period_from"] = result.get("period_from") or parsed
                    result["period_to"] = result.get("period_to") or parsed

        for ci, cell in enumerate(row):
            label = str(cell).strip() if cell is not None else ""
            nxt = normalized_cells[ci + 1] if ci + 1 < len(normalized_cells) else None
            if not nxt:
                continue
            if re.match(r"^(from(?:\s*date)?|period(?:\s*from)?)\s*:?\s*$", label, re.I):
                result["period_from"] = result.get("period_from") or nxt
            elif re.match(r"^(to(?:\s*date)?|period\s*to)\s*:?\s*$", label, re.I):
                result["period_to"] = result.get("period_to") or nxt
            elif re.match(
                r"^(statement\s*date|report\s*date|date)\s*:?\s*$", label, re.I
            ):
                result["period_from"] = result.get("period_from") or nxt
                result["period_to"] = result.get("period_to") or nxt
    return result


def _usable_period_date(value: Any) -> Optional[Any]:
    """Return the original period value if it is usable, else None.

    Usable means non-empty and either a real YYYY-MM-DD calendar date or a
    value that existing `_normalize_date` already treats as valid.
    Does not invent dates or change a usable original value.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _ISO_DATE_RE.match(text):
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return value
        except ValueError:
            return None
    if _normalize_date(text) is None:
        return None
    return value


def _apply_period_from_fallback(result: Dict[str, Any]) -> Dict[str, Any]:
    """If period_from is missing/invalid, copy a usable period_to.

    period_from = period_from if valid else period_to
    Never overwrites a valid period_from. Never changes period_to.
    """
    if not isinstance(result, dict):
        return result
    if _usable_period_date(result.get("period_from")) is not None:
        return result
    period_to = result.get("period_to")
    if _usable_period_date(period_to) is not None:
        result["period_from"] = period_to
    return result


def _month_period(month_name: str, year: int) -> Tuple[Optional[str], Optional[str]]:
    mo = _MONTH_MAP.get(month_name.lower()[:3]) or _MONTH_MAP.get(month_name.lower())
    if not mo:
        return None, None
    start = datetime(year, mo, 1)
    if mo == 12:
        end = datetime(year, 12, 31)
    else:
        end = datetime(year, mo + 1, 1)
        from datetime import timedelta

        end = end - timedelta(days=1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _clean_name(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _ocr_image_footer_text(image_bytes: bytes, top_ratio: float = 0.78) -> str:
    """OCR only the bottom band of a page image to capture TOTAL rows."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""

    import os

    cmd = os.getenv("TESSERACT_CMD", "").strip()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    elif os.name == "nt":
        win_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(win_cmd):
            pytesseract.pytesseract.tesseract_cmd = win_cmd

    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        crop = img.crop((0, int(h * top_ratio), w, h))
        return pytesseract.image_to_string(crop, config="--psm 6") or ""
    except Exception as exc:
        logger.warning("Footer OCR failed: %s", exc)
        return ""


def _merge_footer_total_into_text(page_text: str, image_bytes: Optional[bytes]) -> str:
    """Ensure TOTAL footer line is present in page text (full-page OCR often truncates it)."""
    text = page_text or ""
    if re.search(r"^\s*TOTAL\b.*\d", text, re.I | re.M):
        # Already has a TOTAL with numbers
        return text
    if not image_bytes:
        return text
    footer = _ocr_image_footer_text(image_bytes)
    if not footer.strip():
        return text
    total_lines = [
        ln.strip()
        for ln in footer.splitlines()
        if re.match(r"^\s*TOTAL\b", ln.strip(), re.I)
    ]
    if not total_lines:
        # keep whole footer band — may still help parsers
        return text + "\n" + footer
    return text + "\n" + "\n".join(total_lines)


def _parse_ps_pharma_statement(text: str, filename: str) -> Optional[Dict[str, Any]]:
    """Parse P.S.PHARMACEUTICALS OPENING/RECEIPT/ISSUE/CLOSING stock & sales analysis."""
    if not text:
        return None
    # QTY/VALUE pairs (with or without RATE). A printed dash is an empty cell,
    # so the 4-number tail parser must not claim these tables.
    if re.search(r"\bRECEIPT\b", text, re.I) and re.search(
        r"QTY\.?\s+VALUE", text, re.I
    ) and (
        re.search(r"\bRATE\b", text, re.I) or re.search(r"\bDUMP\b", text, re.I)
    ):
        return None
    # Only P.S. format (OPENING/RECEIPT/ISSUE/CLOSING) — do not steal Mahajan pages
    is_ps = bool(
        re.search(r"P\.?\s*S\.?\s*PHARMACEUTICAL", text, re.I)
        and (
            re.search(r"OPENING\s+RECEIPT\s+ISSUE\s+CLOSING", text, re.I)
            or re.search(r"STOCK\s*&\s*SALES\s+ANALYSIS", text, re.I)
            or re.search(r"^\s*TOTAL\b", text, re.I | re.M)
        )
    ) or bool(re.search(r"OPENING\s+RECEIPT\s+ISSUE\s+CLOSING", text, re.I))
    if not is_ps:
        return None

    result = empty_result(filename, "pdf")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        result["stockist_name"] = _clean_name(lines[0])
    for ln in lines[:12]:
        if re.search(r"STOCK\s*&\s*SALES|Sales\s*&\s*Stock", ln, re.I):
            result["report_title"] = _clean_name(ln)
            m = re.search(
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*[-–]+\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
                ln,
            )
            if m:
                result["period_from"] = _normalize_date(m.group(1))
                result["period_to"] = _normalize_date(m.group(2))
        if re.search(r"AUROBINDO|VERITAZ|HEALTHCARE", ln, re.I) and not re.search(
            r"PHARMACEUTICALS|ANALYSIS", ln, re.I
        ):
            # company often appears as division line or inside title parentheses
            m_co = re.search(r"\(([^)]+)\)", ln)
            if m_co:
                result["company_name"] = _clean_name(m_co.group(1))
            elif not result.get("company_name"):
                result["company_name"] = _clean_name(ln)
        if re.search(r"RAGHUNATH|JAMMU|Phone|GSTIN", ln, re.I) and not result.get(
            "stockist_address"
        ):
            if not re.search(r"Phone|GSTIN|E-Mail|VAT", ln, re.I):
                result["stockist_address"] = _clean_name(ln)
        if (
            not result.get("report_title")
            and result.get("stockist_name")
            and _clean_name(ln) != result.get("stockist_name")
            and re.search(r"\d|ROAD|FLOOR", ln, re.I)
            and not re.search(r"STOCK\s*&\s*SALES|OPENING\s+RECEIPT", ln, re.I)
        ):
            prev = result.get("stockist_address") or ""
            result["stockist_address"] = _clean_name(f"{prev} {ln}")

    # Row: <product [packing]> opening receipt issue closing.
    # A printed "-" is an empty qty cell, not a missing column.
    qty_tok = r"(?:-?\d+(?:\.\d+)?|-)"
    row_re = re.compile(
        rf"^(.+?)\s+({qty_tok})\s+({qty_tok})\s+({qty_tok})\s+({qty_tok})\s*$"
    )
    nums_re = re.compile(
        rf"^({qty_tok})\s+({qty_tok})\s+({qty_tok})\s+({qty_tok})\s*$"
    )
    pack_unit = re.compile(r"^(?:ML|MG|GM|G|TAB|TABS|CAP|CAPS|SYP|S)$", re.I)
    pack_token = re.compile(
        r"^(?:\d+\*\d+|\d+(?:\.\d+)?[`']?(?:ML|MG|GM|G|TAB|TABS|CAP|CAPS|SYP|S)|"
        r"\d+ML|\d+MG|0\.\d+ML)$",
        re.I,
    )

    def _ps_qty(token: str) -> float:
        if str(token).strip() == "-":
            return 0.0
        return _to_float(token)

    def _ps_name_pack(left: str) -> Tuple[str, Optional[str]]:
        tokens = _clean_name(left).split()
        packing = None
        name_tokens = tokens
        if (
            len(tokens) >= 2
            and pack_unit.match(tokens[-1])
            and re.match(r"^\d", tokens[-2])
        ):
            packing = f"{tokens[-2]} {tokens[-1]}"
            name_tokens = tokens[:-2]
        elif tokens and pack_token.match(tokens[-1].replace("`", "").replace("'", "")):
            packing = tokens[-1]
            name_tokens = tokens[:-1]
        return _clean_name(" ".join(name_tokens)), packing

    def _ps_item(left: str, opening: str, receipt: str, issue: str, closing: str) -> Optional[Dict[str, Any]]:
        product_name, packing = _ps_name_pack(left)
        if len(product_name) < 3:
            return None
        if re.fullmatch(r"AUROBINDO.*|VERITAZ.*|THE HIMALAYA.*", product_name, re.I):
            return None
        item = empty_line_item()
        item["product_name"] = product_name
        item["packing"] = packing
        item["opening_qty"] = _ps_qty(opening)
        item["receipts_qty"] = _ps_qty(receipt)
        item["sales_qty"] = _ps_qty(issue)  # ISSUE
        item["closing_qty"] = _ps_qty(closing)
        item["sales_value"] = 0.0
        item["closing_value"] = 0.0
        return item

    items: List[Dict[str, Any]] = []
    pending_name = ""
    for ln in lines:
        if re.match(r"^\s*TOTAL\b", ln, re.I):
            pending_name = ""
            continue
        if re.search(
            r"^(ITEM|OPENING|STOCK|AUROBINDO|Phone|GSTIN|Page|P\.?S\.?)",
            ln,
            re.I,
        ):
            pending_name = ""
            continue
        if re.search(r"THE HIMALAYA DRUG", ln, re.I):
            if not result.get("company_name"):
                result["company_name"] = "THE HIMALAYA DRUG CO"
            pending_name = ""
            continue
        m = row_re.match(ln)
        if m:
            row = _ps_item(m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
            if row:
                items.append(row)
            pending_name = ""
            continue
        n = nums_re.match(ln)
        if n and pending_name:
            row = _ps_item(pending_name, n.group(1), n.group(2), n.group(3), n.group(4))
            if row:
                items.append(row)
            pending_name = ""
            continue
        if re.search(r"[A-Za-z]{3}", ln) and not re.search(
            r"ROAD|FLOOR|PHONE|GSTIN|ANALYSIS", ln, re.I
        ):
            pending_name = ln
        else:
            pending_name = ""

    if not items and not re.search(r"^\s*TOTAL\b", text, re.I | re.M):
        return None

    result["line_items"] = items
    result["totals"]["extra"]["extraction_method"] = "ps_pharma_parser"
    result = _apply_total_row_to_result(result, text)

    # If footer TOTAL still missing, fall back to line sums for qty totals
    totals = result["totals"]
    if totals.get("opening_qty") is None and items:
        totals["opening_qty"] = sum(_to_float(i["opening_qty"]) for i in items)
        totals["receipts_qty"] = sum(_to_float(i["receipts_qty"]) for i in items)
        totals["sales_qty"] = sum(_to_float(i["sales_qty"]) for i in items)
        totals["closing_qty"] = sum(_to_float(i["closing_qty"]) for i in items)
        totals["extra"]["total_row_source"] = "line_sum_fallback"
        totals["extra"]["total_row_labels"] = [
            "OPENING",
            "RECEIPT",
            "ISSUE",
            "CLOSING",
        ]
    return result


def _is_opbal_issue_closing_format(text: str) -> bool:
    """Mahajan-style: PRODUCT/PACKING OpBal Receipt Total Issue Closing."""
    if not text:
        return False
    header = "\n".join(ln for ln in text.splitlines()[:50] if ln.strip())
    has_op = bool(re.search(r"Op\.?\s*Bal", header, re.I))
    has_receipt = bool(re.search(r"\bReceipt\b", header, re.I))
    has_issue = bool(re.search(r"\bIssue\b", header, re.I))
    has_closing = bool(re.search(r"\bClosing\b", header, re.I))
    return has_op and has_receipt and has_issue and has_closing


def _opbal_layout(text: str) -> str:
    """Return qty column layout for OpBal statements.

    - issue_expiry_closing: OpBal Receipt Total Issue Expiry Closing Near
      (MAHAJAN & MAHAJAN)
    - issue_closing_dump: OpBal Receipt Total Issue Closing Dump Near
      (MAHAJAN ASSOCIATES)
    """
    header = "\n".join(ln for ln in (text or "").splitlines()[:50] if ln.strip())
    # Expiry/Breakage column sits between Issue and Closing
    if re.search(
        r"Issue\s+Expiry\s+Closing|Issue\s+\S*\s*Expiry\s+\S*\s*Closing|"
        r"Expiry\s+Closing\s+Near|Breakage\s+Balance",
        header,
        re.I,
    ):
        return "issue_expiry_closing"
    if re.search(r"\bExpiry\b", header, re.I) and re.search(
        r"Issue.{0,20}Expiry.{0,20}Closing", header, re.I | re.S
    ):
        return "issue_expiry_closing"
    return "issue_closing_dump"


def _ocr_qty_token(tok: str) -> Optional[float]:
    """Parse a trailing qty token; tolerate OCR letter glue (S54, a2) and o/° as 0."""
    if tok is None:
        return None
    t = str(tok).strip().replace(",", "")
    if not t:
        return None
    if t in {"o", "O", "°", "〇", "()", "—", "-", "."}:
        return 0.0
    if re.fullmatch(r"-?\d+(?:\.\d+)?", t):
        return _to_float(t)
    # Single letter glued onto digits (common Tesseract noise on this format)
    m = re.fullmatch(r"[A-Za-z](-?\d+(?:\.\d+)?)", t)
    if m:
        return _to_float(m.group(1))
    # Trailing junk letter: 66o, 30_
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)[A-Za-z._]*", t)
    if m and not re.search(r"(?:TAB|ML|MG|CAP|SYP|INJ)\b", t, re.I):
        return _to_float(m.group(1))
    return None


def _looks_like_packing_token(tok: str) -> bool:
    t = str(tok or "").strip().rstrip(".,-_")
    if not t:
        return False
    if re.fullmatch(
        r"\d+(?:\.\d+)?(?:TAB|TABS|ML|MG|CAP|SYP|INJ|DS|S)", t, re.I
    ):
        return True
    if re.fullmatch(r"\d+\*\d+", t):
        return True
    # OCR packing like 10TAB_ / 5M. / 1OTAB (O for 0)
    if re.fullmatch(r"\d+(?:\.\d+)?(?:TAB|ML|MG|CAP|S)[A-Za-z._]*", t, re.I):
        return True
    if re.fullmatch(r"\d+[Oo]TAB[A-Za-z._]*", t, re.I):
        return True
    return False


def _repair_opbal_qty_tuple(use: List[float]) -> List[float]:
    """Rebuild Total as OpBal+Receipt when Total looks OCR-truncated."""
    if len(use) < 5:
        return use
    fixed = list(use)
    op, rec, total = fixed[0], fixed[1], fixed[2]
    expected = op + rec
    if abs(expected - total) <= max(1.0, 0.02 * max(abs(expected), 1.0)):
        return fixed
    if expected <= 0 or total < 0:
        return fixed
    exp_i, tot_i = int(round(expected)), int(round(total))
    if tot_i >= exp_i or tot_i <= 0:
        return fixed
    exp_s, tot_s = str(exp_i), str(tot_i)
    ratio = expected / max(total, 0.1)
    # 42→2 (suffix) or 60→6 (prefix / dropped trailing 0)
    if exp_s.endswith(tot_s) or (
        exp_s.startswith(tot_s) and (9 <= ratio <= 11 or 90 <= ratio <= 110)
    ):
        fixed[2] = float(expected)
    return fixed


def _parse_opbal_receipt_issue_row(
    ln: str, layout: str = "issue_closing_dump"
) -> Optional[Dict[str, Any]]:
    """Parse one OpBal/Receipt/Total/Issue/... product row."""
    if not ln or re.match(r"^\s*TOTAL\b", ln, re.I):
        return None
    if re.search(
        r"PRODUCT\s*NAME|PACKING|Op\.?\s*Bal|Page\s*No|Continued|Sales\s*&\s*Stock|"
        r"GRAND\s*TOTAL",
        ln,
        re.I,
    ):
        return None

    tokens = ln.split()
    if len(tokens) < 4:
        return None

    # Allow 8 tokens so a leading Shelf code can be dropped via last-7 slice
    qtys: List[float] = []
    i = len(tokens) - 1
    while i >= 0 and len(qtys) < 8:
        val = _ocr_qty_token(tokens[i])
        if val is None:
            break
        qtys.append(val)
        i -= 1
    qtys.reverse()
    if len(qtys) < 5:
        return None

    left = tokens[: i + 1]
    packing = None
    if left and _looks_like_packing_token(left[-1]):
        packing = left[-1].rstrip(".,-_")
        left = left[:-1]
    elif (
        len(left) >= 2
        and re.fullmatch(r"\d+(?:\.\d+)?", left[-2] or "")
        and re.fullmatch(r"(?:TAB|TABS|ML|MG|CAP|SYP|INJ|DS)", left[-1] or "", re.I)
    ):
        packing = f"{left[-2]}{left[-1]}"
        left = left[:-2]
    # Drop trailing bare shelf code left in the name (e.g. "... TAB 11")
    if left and re.fullmatch(r"\d{1,2}", left[-1] or ""):
        left = left[:-1]

    product_name = _clean_name(" ".join(left))
    if len(product_name) < 3:
        return None
    if re.fullmatch(r"AUROBINDO.*|VERITAZ.*|Qy\.?", product_name, re.I):
        return None

    # Prefer full 7-col layout, then 6, then 5. Repair truncated Total before accepting.
    use = None
    for n in (7, 6, 5):
        if n > len(qtys):
            continue
        candidate = _repair_opbal_qty_tuple(list(qtys[-n:] if len(qtys) > n else qtys))
        bal_diff = abs((candidate[0] + candidate[1]) - candidate[2])
        if bal_diff <= max(5.0, 0.25 * max(abs(candidate[2]), abs(candidate[0] + candidate[1]), 1.0)):
            use = candidate
            break
    if use is None:
        return None

    item = empty_line_item()
    item["product_name"] = product_name
    item["packing"] = packing
    item["opening_qty"] = use[0]
    item["receipts_qty"] = use[1]
    item["sales_qty"] = use[3]  # ISSUE (not Total)
    item["sales_value"] = 0.0
    item["closing_value"] = 0.0
    item["extra"]["total_stock_qty"] = use[2]

    if layout == "issue_expiry_closing" and len(use) >= 6:
        # OpBal Receipt Total Issue Expiry Closing [Near]
        item["extra"]["expiry_breakage_qty"] = use[4]
        item["closing_qty"] = use[5]
        if len(use) >= 7:
            item["extra"]["near_expiry_qty"] = use[6]
    else:
        # OpBal Receipt Total Issue Closing [Dump] [Near]
        item["closing_qty"] = use[4]
        if len(use) >= 6:
            item["extra"]["dump_qty"] = use[5]
        if len(use) >= 7:
            item["extra"]["near_expiry_qty"] = use[6]
    return item


def _parse_opbal_receipt_issue_statement(
    text: str, filename: str, source_format: str = "pdf"
) -> Optional[Dict[str, Any]]:
    """Parse Mahajan-style OpBal/Receipt/Total/Issue/Closing statements."""
    if not _is_opbal_issue_closing_format(text):
        return None

    layout = _opbal_layout(text)
    result = empty_result(filename, source_format)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        result["stockist_name"] = _clean_name(lines[0])
    for ln in lines[:20]:
        if re.search(r"Sales\s*&\s*Stock|Stock\s*&\s*Sales", ln, re.I):
            result["report_title"] = _clean_name(ln)
            m = re.search(
                r"(?:From\s+)?(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*(?:Upto|to|[-–])\s*"
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
                ln,
                re.I,
            )
            if m:
                result["period_from"] = _normalize_date(m.group(1))
                result["period_to"] = _normalize_date(m.group(2))
        if re.search(r"AUROBINDO|VERITAZ|PHARMA\s+LIMITED|AUROBINO", ln, re.I) and not re.search(
            r"MAHAJAN|ASSOCIATES|Sales\s*&\s*Stock", ln, re.I
        ):
            result["company_name"] = _clean_name(ln)
        if re.search(r"GARH|JAMMU|MOHALLA|ROAD|NAGAR|R\.?\s*N\.?\s*PURA", ln, re.I) and not result.get(
            "stockist_address"
        ):
            result["stockist_address"] = _clean_name(ln)

    items: List[Dict[str, Any]] = []
    for ln in lines:
        row = _parse_opbal_receipt_issue_row(ln, layout=layout)
        if row:
            items.append(row)

    if not items:
        return None

    result["line_items"] = items
    result["totals"]["extra"]["extraction_method"] = "opbal_issue_closing_parser"
    result["totals"]["extra"]["opbal_layout"] = layout
    result["totals"]["sales_value"] = None
    result["totals"]["closing_value"] = None
    result = _apply_total_row_to_result(result, text)
    return result


def _parse_statement_total_row(text: str) -> Optional[Dict[str, Any]]:
    """Parse bottom TOTAL qty row (e.g. P.S.PHARMACEUTICALS OPENING/RECEIPT/ISSUE/CLOSING)."""
    if not text:
        return None

    header_blob = "\n".join(
        ln for ln in text.splitlines()[:50] if ln.strip()
    )
    # Prefer GRAND TOTAL, else last TOTAL line (footer)
    total_line = None
    grand_line = None
    for ln in text.splitlines():
        s = ln.strip()
        if re.match(r"^\s*GRAND\s*TOTAL\b", s, re.I):
            grand_line = s
        elif re.match(r"^\s*TOTAL\b", s, re.I):
            total_line = s
    total_line = grand_line or total_line

    if not total_line:
        return None

    # Strip currency/OCR junk before digit scan ($29770 → 29770; prefer clean GRAND)
    cleaned = total_line.replace("$", " ").replace(",", "")
    raw_nums = re.findall(r"-?\d+(?:\.\d+)?", cleaned)
    # Drop OCR-merged monsters (>8 integer digits)
    nums = [
        _to_float(n)
        for n in raw_nums
        if len(n.split(".")[0].lstrip("-")) <= 8
    ]
    if len(nums) < 4:
        return None

    parsed: Dict[str, Any] = {
        "total_row_raw": total_line,
        "total_row_numbers": nums,
    }

    # P.S.PHARMACEUTICALS: ITEM DESCRIPTION OPENING RECEIPT ISSUE CLOSING
    if re.search(r"OPENING\s+RECEIPT\s+ISSUE\s+CLOSING", header_blob, re.I) or (
        re.search(r"\bOPENING\b", header_blob, re.I)
        and re.search(r"\bISSUE\b", header_blob, re.I)
        and len(nums) == 4
    ):
        parsed.update(
            {
                "opening_qty": nums[0],
                "receipts_qty": nums[1],
                "sales_qty": nums[2],  # ISSUE
                "closing_qty": nums[3],
                "total_row_format": "opening_receipt_issue_closing",
                "total_row_labels": ["OPENING", "RECEIPT", "ISSUE", "CLOSING"],
            }
        )
        return parsed

    # Mahajan & Mahajan: OpBal Receipt Total Issue Expiry Closing Near
    if _opbal_layout(text) == "issue_expiry_closing" and len(nums) >= 6:
        parsed.update(
            {
                "opening_qty": nums[0],
                "receipts_qty": nums[1],
                "total_stock_qty": nums[2],
                "sales_qty": nums[3],  # ISSUE
                "expiry_breakage_qty": nums[4],
                "closing_qty": nums[5],  # Closing Balance (NOT the Expiry column)
                "total_row_format": "opbal_receipt_total_issue_expiry_closing",
                "total_row_labels": [
                    "OpBal",
                    "Receipt",
                    "Total",
                    "Issue",
                    "Expiry",
                    "Closing",
                ],
            }
        )
        if len(nums) >= 7:
            parsed["near_expiry_qty"] = nums[6]
            parsed["total_row_labels"].append("NearExpiry")
        return parsed

    # Mahajan Associates: OpBal Receipt Total Issue Closing [Dump] [Near Expiry]
    if re.search(r"Op\.?\s*Bal|Receipt|Issue|Closing", header_blob, re.I) and len(nums) >= 5:
        parsed.update(
            {
                "opening_qty": nums[0],
                "receipts_qty": nums[1],
                "total_stock_qty": nums[2],
                "sales_qty": nums[3],  # ISSUE
                "closing_qty": nums[4],
                "total_row_format": "opbal_receipt_total_issue_closing",
                "total_row_labels": [
                    "OpBal",
                    "Receipt",
                    "Total",
                    "Issue",
                    "Closing",
                ],
            }
        )
        if len(nums) >= 6:
            parsed["dump_qty"] = nums[5]
            parsed["total_row_labels"].append("Dump")
        if len(nums) >= 7:
            parsed["near_expiry_qty"] = nums[6]
            parsed["total_row_labels"].append("NearExpiry")
        return parsed

    # Generic 4-number TOTAL → treat as opening/receipt/issue/closing
    if len(nums) == 4:
        parsed.update(
            {
                "opening_qty": nums[0],
                "receipts_qty": nums[1],
                "sales_qty": nums[2],
                "closing_qty": nums[3],
                "total_row_format": "qty4_generic",
                "total_row_labels": ["OPENING", "RECEIPT", "ISSUE", "CLOSING"],
            }
        )
        return parsed

    return parsed


def _apply_total_row_to_result(result: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Attach parsed footer TOTAL qty columns onto statement totals."""
    if not isinstance(result, dict) or result.get("multi_statement"):
        return result
    parsed = _parse_statement_total_row(text)
    if not parsed:
        return result

    totals = result.setdefault(
        "totals", {"sales_value": None, "closing_value": None, "extra": {}}
    )
    if not isinstance(totals.get("extra"), dict):
        totals["extra"] = {}

    # Authoritative qty totals from footer row (do not treat as money)
    for key in (
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "closing_qty",
        "total_stock_qty",
        "dump_qty",
        "near_expiry_qty",
        "expiry_breakage_qty",
    ):
        if key in parsed:
            totals[key] = parsed[key]

    totals["extra"]["total_row_raw"] = parsed.get("total_row_raw")
    totals["extra"]["total_row_numbers"] = parsed.get("total_row_numbers")
    totals["extra"]["total_row_format"] = parsed.get("total_row_format")
    totals["extra"]["total_row_labels"] = parsed.get("total_row_labels")
    totals["extra"]["total_row_source"] = "footer_total"

    # Qty-only formats must not keep footer numbers as sales_value money
    if parsed.get("total_row_format") in {
        "opening_receipt_issue_closing",
        "opbal_receipt_total_issue_closing",
        "opbal_receipt_total_issue_expiry_closing",
        "qty4_generic",
    }:
        if totals.get("sales_value") in parsed.get("total_row_numbers", []):
            totals["sales_value"] = None
        if totals.get("closing_value") in parsed.get("total_row_numbers", []):
            # closing_value money vs closing_qty — clear only if it matches a qty total
            totals["closing_value"] = None

    return result


def _is_implausible_money(
    value: Any, *, max_digits: int = 8, allow_negative: bool = False
) -> bool:
    """True for OCR-garbage currency (e.g. 75374123024 from merged TOTAL columns)."""
    if value is None:
        return False
    try:
        num = float(value)
    except (TypeError, ValueError):
        return True
    if num < 0 and not allow_negative:
        return True
    num = abs(num)
    # Secondary-sales stockist totals almost never need > 8 integer digits
    int_digits = len(str(int(abs(num))))
    if int_digits > max_digits:
        return True
    # Hard cap (~10 crore) for a single stockist month statement value
    if abs(num) >= 100_000_000:
        return True
    return False


def _looks_like_concatenated_totals(sales_value: Any, closing_value: Any) -> bool:
    """Detect OCR merge of adjacent TOTAL numbers (75374|123024 -> 75374123024)."""
    try:
        sales = float(sales_value)
        closing = float(closing_value) if closing_value is not None else None
    except (TypeError, ValueError):
        return False
    if sales <= 0:
        return False
    sales_int = str(int(sales))
    if len(sales_int) < 9:
        return False
    if closing is not None and closing > 0:
        close_int = str(int(closing))
        # e.g. ...12302438126 where closing was partially merged — less common
        if sales_int.endswith(close_int) and len(sales_int) > len(close_int) + 3:
            return True
    # Split into plausible adjacent qty totals (4-6 digits + 4-6 digits)
    for split_at in range(4, min(7, len(sales_int) - 3)):
        left, right = sales_int[:split_at], sales_int[split_at:]
        if 3 <= len(right) <= 6 and left.isdigit() and right.isdigit():
            if int(left) > 0 and int(right) > 0:
                return True
    return False


def _sanitize_statement_financials(result: Dict[str, Any]) -> Dict[str, Any]:
    """Fix bogus sales/closing money totals from OCR TOTAL merges (qty-only formats)."""
    if not isinstance(result, dict):
        return result
    # Multi-statement wrapper
    if result.get("multi_statement") and isinstance(result.get("statements"), list):
        result["statements"] = [
            _sanitize_statement_financials(stmt) for stmt in result["statements"]
        ]
        return result

    # Product Stock Report has no sale-amount column; fill qty/value here so
    # every parse path (including format-specific vision) is covered.
    if _is_product_stock_report_result(result):
        result = _psr_fill_missing_sales(result)

    items = result.get("line_items") or []
    if not isinstance(items, list):
        items = []

    sum_sales_value = sum(_to_float(i.get("sales_value")) for i in items if isinstance(i, dict))
    sum_closing_value = sum(_to_float(i.get("closing_value")) for i in items if isinstance(i, dict))
    sum_sales_qty = sum(_to_float(i.get("sales_qty")) for i in items if isinstance(i, dict))
    sum_closing_qty = sum(_to_float(i.get("closing_qty")) for i in items if isinstance(i, dict))

    totals = result.setdefault("totals", {"sales_value": None, "closing_value": None, "extra": {}})
    if not isinstance(totals.get("extra"), dict):
        totals["extra"] = {}

    totals["extra"]["line_sales_value_sum"] = sum_sales_value
    totals["extra"]["line_closing_value_sum"] = sum_closing_value

    # Prefer footer TOTAL-row qty when present (P.S.PHARMACEUTICALS etc.)
    has_footer_qty = totals.get("extra", {}).get("total_row_source") == "footer_total"
    if has_footer_qty:
        if totals.get("sales_qty") is not None:
            totals["extra"]["sales_qty"] = totals.get("sales_qty")
        else:
            totals["extra"]["sales_qty"] = sum_sales_qty
        if totals.get("closing_qty") is not None:
            totals["extra"]["closing_qty"] = totals.get("closing_qty")
        else:
            totals["extra"]["closing_qty"] = sum_closing_qty
    else:
        totals["extra"]["sales_qty"] = sum_sales_qty
        totals["extra"]["closing_qty"] = sum_closing_qty

    sales_total = totals.get("sales_value")
    closing_total = totals.get("closing_value")
    allow_negative_money = (
        str(totals.get("extra", {}).get("extraction_method") or "")
        in {"swil_landscape_qty_value", "purc_sale_cl_layout"}
    )

    qty_only = (
        len(items) > 0
        and sum_sales_qty > 0
        and sum_sales_value <= 0
        and sum(1 for i in items if isinstance(i, dict) and _to_float(i.get("sales_value")) > 0)
        <= max(1, len(items) // 10)
    )

    bogus_sales = (
        _is_implausible_money(sales_total, allow_negative=allow_negative_money)
        or _looks_like_concatenated_totals(sales_total, closing_total)
        or (
            sales_total is not None
            and sum_sales_value > 0
            and float(sales_total) > max(sum_sales_value * 50, sum_sales_value + 100000)
        )
        or (
            qty_only
            and sales_total is not None
            and float(sales_total) > 0
            and str(totals.get("extra", {}).get("total_row_source") or "")
            != "daxinsoft_footer"
        )
    )

    if bogus_sales:
        totals["extra"]["rejected_sales_value"] = sales_total
        # Prefer real line-item money sum; else null for qty-only statements
        totals["sales_value"] = sum_sales_value if sum_sales_value > 0 else None

    if _is_implausible_money(
        totals.get("closing_value"), allow_negative=allow_negative_money
    ):
        totals["extra"]["rejected_closing_value"] = totals.get("closing_value")
        totals["closing_value"] = sum_closing_value if sum_closing_value > 0 else None
    elif (
        qty_only
        and totals.get("closing_value") is not None
        and sum_closing_value <= 0
        and float(totals.get("closing_value") or 0) > sum_closing_qty * 1000
        and float(totals.get("closing_value") or 0) > 100000
    ):
        # Closing "value" is likely a qty total mislabeled from OCR TOTAL
        totals["extra"]["rejected_closing_value"] = totals.get("closing_value")
        totals["extra"]["closing_qty_from_total_row"] = totals.get("closing_value")
        totals["closing_value"] = None

    # Sanitize absurd per-line sales_value (rare, but same OCR merge class)
    for item in items:
        if not isinstance(item, dict):
            continue
        if _is_implausible_money(
            item.get("sales_value"), allow_negative=allow_negative_money
        ):
            item.setdefault("extra", {})
            if isinstance(item["extra"], dict):
                item["extra"]["rejected_sales_value"] = item.get("sales_value")
            item["sales_value"] = 0.0
        if _is_implausible_money(
            item.get("closing_value"), allow_negative=allow_negative_money
        ):
            item.setdefault("extra", {})
            if isinstance(item["extra"], dict):
                item["extra"]["rejected_closing_value"] = item.get("closing_value")
            item["closing_value"] = 0.0

    return _apply_period_from_fallback(result)


def _sniff_extension(file_bytes: bytes, filename: str) -> str:
    """Resolve extension from filename, with magic-byte fallback for images/text/Word."""
    name = Path(filename or "upload").name
    ext = Path(name).suffix.lower()
    if ext in SUPPORTED_EXTENSIONS:
        return ext

    head = file_bytes[:16] if file_bytes else b""
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"BM"):
        return ".bmp"
    if head.startswith(b"RIFF") and file_bytes[8:12] == b"WEBP":
        return ".webp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tif"
    # OOXML Word (.docx) is a ZIP containing word/document.xml
    if head.startswith(b"PK"):
        try:
            import zipfile

            with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
                names = set(zf.namelist())
                if "word/document.xml" in names:
                    return ".docx"
                if "xl/workbook.xml" in names:
                    return ".xlsx"
        except Exception:
            pass
    # Legacy OLE: Word .doc vs Excel .xls
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        try:
            import olefile

            with olefile.OleFileIO(io.BytesIO(file_bytes)) as ole:
                streams = {"/".join(p).lower() for p in ole.listdir()}
                if any("worddocument" in s for s in streams):
                    return ".doc"
                if any(s in streams for s in ("workbook", "book")):
                    return ".xls"
        except Exception:
            # Prefer .doc when filename hints Word; else leave as-is
            if "doc" in (ext or "") or "word" in name.lower():
                return ".doc"
    # UTF-8 / ASCII text sales statements
    if file_bytes and not any(b == 0 for b in file_bytes[:200]):
        try:
            sample = file_bytes[:2000].decode("utf-8")
            if re.search(r"STOCK|SALES|PRODUCT|OPENING|CLOSING", sample, re.I):
                return ".txt"
        except UnicodeDecodeError:
            try:
                sample = file_bytes[:2000].decode("latin-1")
                if re.search(r"STOCK|SALES|PRODUCT|OPENING|CLOSING", sample, re.I):
                    return ".txt"
            except Exception:
                pass
    return ext


def _apply_stock_identity_validation(result: Dict[str, Any]) -> Dict[str, Any]:
    """Validate stock identity using columns present in the detected format.

    Does not replace parsers. Does not invent qty to force the formula.
    """
    if not isinstance(result, dict):
        return result
    if result.get("multi_statement") and isinstance(result.get("statements"), list):
        result["statements"] = [
            _apply_stock_identity_validation(stmt) for stmt in result["statements"]
        ]
        return result

    kind = _stock_identity_kind(result)
    items = result.get("line_items") or []
    totals = result.setdefault(
        "totals", {"sales_value": None, "closing_value": None, "extra": {}}
    )
    if not isinstance(totals.get("extra"), dict):
        totals["extra"] = {}
    totals["extra"]["stock_identity_kind"] = kind
    if kind == STOCK_IDENTITY_SALERET:
        totals["extra"]["stock_identity_formula"] = (
            "total=opening+purchase; closing=total-sale+sale_return-exp_damage"
        )
    elif kind == STOCK_IDENTITY_SAMPLE:
        totals["extra"]["stock_identity_formula"] = (
            "total=opening+purchase; closing=total-sale-sample-exp_clos"
        )
    elif kind == STOCK_IDENTITY_OPBAL:
        totals["extra"]["stock_identity_formula"] = (
            "closing=opening+receipt-issue"
        )
    else:
        totals["extra"]["stock_identity_formula"] = (
            "closing=opening+receipts-sales"
        )

    fail = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        extra = _psr_ensure_extra(item)
        expected_total = _stock_expected_total(item)
        expected_close = _stock_expected_closing(item, kind)
        extra["expected_total"] = expected_total
        extra["expected_closing"] = expected_close
        ok = _stock_row_identity_ok(item, kind)
        extra["stock_identity_ok"] = ok
        if not ok:
            fail += 1
    totals["extra"]["stock_identity_fail_count"] = fail
    opening_sum = sum(_to_float(i.get("opening_qty")) for i in items if isinstance(i, dict))
    receipt_sum = sum(_to_float(i.get("receipts_qty")) for i in items if isinstance(i, dict))
    sales_sum = sum(_to_float(i.get("sales_qty")) for i in items if isinstance(i, dict))
    closing_sum = sum(_to_float(i.get("closing_qty")) for i in items if isinstance(i, dict))
    expected_total = opening_sum + receipt_sum
    calculated_closing = expected_total - sales_sum
    totals["extra"]["stock_validation"] = {
        "opening_plus_purchase": expected_total,
        "expected_total": expected_total,
        "calculated_closing": calculated_closing,
        "extracted_closing": closing_sum,
        "is_valid": fail == 0 and abs(calculated_closing - closing_sum) < 0.05,
    }
    return result


def extract_sales_statement(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """Dispatch by extension and return unified sales-statement JSON."""
    name = Path(filename or "upload").name
    ext = _sniff_extension(file_bytes, name)
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported format '{ext or '(none)'}'. "
            f"Supported: {sorted(SUPPORTED_EXTENSIONS)}"
        )

    if ext in TEXT_EXTENSIONS:
        result = _parse_txt(file_bytes, name)
    elif ext in {".htm", ".html"}:
        result = _parse_htm(file_bytes, name)
    elif ext in WORD_EXTENSIONS:
        result = _parse_word(file_bytes, name, ext)
    elif ext == ".pdf":
        result = _parse_pdf(file_bytes, name)
    elif ext in {".xls", ".xlsx", ".xlsm"}:
        result = _parse_xls(file_bytes, name, ext)
    elif ext in IMAGE_EXTENSIONS:
        result = _parse_image(file_bytes, name, ext)
    else:
        raise ValueError(f"Unsupported format '{ext}'")

    result = _sanitize_statement_financials(result)
    return _apply_stock_identity_validation(result)


# ---------------------------------------------------------------------------
# TXT (Kaveri-style fixed / spaced rows)
# ---------------------------------------------------------------------------

_KAVERI_ROW = re.compile(
    r"^(\d{3,5})\s+(.+?)\s+(\S*\*\S*|\S*(?:ML|MG|TAB|CAP|SYP|VIAL|DROPS|'S|S)\S*)\s+"
    r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+"
    r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)"
    r"(?:\s+(\S+))?(?:\s+(\S+))?\s*$",
    re.IGNORECASE,
)


_SALES_STOCK_HEADER_TOKEN = re.compile(
    r"Op\.?\s*Bal\.?|Opening|Receipt|Total|Issue|Closing|Rate|Value|Amt|Amount",
    re.I,
)


def _sales_stock_label_kind(label: str) -> str:
    token = re.sub(r"[^a-z]", "", (label or "").lower())
    if token.startswith("op") or token.startswith("opening"):
        return "opening"
    if token.startswith("receipt"):
        return "receipt"
    if token.startswith("total"):
        return "total"
    if token.startswith("issue") or token.startswith("iss"):
        return "issue"
    if token.startswith("clos"):
        return "closing"
    if token.startswith("rate"):
        return "rate"
    if "value" in token or token.startswith("amt") or token.startswith("amount"):
        return "value"
    return "other"


def _sales_stock_column_plan(
    header_line: str, sub_line: str
) -> Optional[Dict[str, Any]]:
    """Column windows and roles from the printed header, not a fixed qty count.

    Data columns on this statement are evenly spaced from the first header
    token. A Qty/Balance sub-header stays a quantity. Value, Amt, or Rate
    is stored only when that column is printed.
    """
    packing = re.search(r"\bPACKING\b", header_line, re.I)
    if not packing:
        return None
    hits = [
        m
        for m in _SALES_STOCK_HEADER_TOKEN.finditer(header_line)
        if m.start() > packing.start()
    ]
    if len(hits) < 2:
        return None
    stride = hits[1].start() - hits[0].start()
    if stride < 4:
        return None
    origin = hits[0].start()
    columns: List[Dict[str, Any]] = []
    for index, hit in enumerate(hits):
        start = origin + stride * index
        end = origin + stride * (index + 1)
        kind = _sales_stock_label_kind(hit.group(0))
        sub = ""
        if sub_line and start < len(sub_line):
            sub = sub_line[start:end]
        valueish = kind in {"value", "rate"} or bool(
            re.search(r"value|amt|amount|rate", sub, re.I)
        )
        parent = kind
        if kind == "value" and columns:
            parent = str(columns[-1].get("kind") or "value")
        role = None
        if parent == "opening":
            role = "opening_value" if valueish else "opening_qty"
        elif parent == "receipt":
            role = "receipts_value" if valueish else "receipts_qty"
        elif parent == "total":
            role = "total_value" if valueish else "total_stock"
        elif parent == "issue":
            role = "sales_value" if valueish else "sales_qty"
        elif parent == "closing":
            role = "closing_value" if valueish else "closing_qty"
        elif kind == "rate" or parent == "rate":
            role = "unit_rate"
        if not role:
            continue
        columns.append(
            {
                "kind": parent,
                "role": role,
                "start": start,
                "end": end,
                "label": hit.group(0),
            }
        )
    if not columns:
        return None
    return {"packing_at": packing.start(), "columns": columns, "stride": stride}


def _slice_fixed_field(line: str, start: int, end: Optional[int]) -> str:
    if start >= len(line):
        return ""
    chunk = line[start:] if end is None else line[start:end]
    return chunk.strip()


def _parse_fixed_sales_stock_statement(
    text: str, filename: str
) -> Optional[Dict[str, Any]]:
    """Srinandan-style Sales & Stock Statement.

    Reads whichever quantity and value columns the header actually prints.
    """
    if not text or not re.search(r"Sales\s*&\s*Stock\s*Statement", text, re.I):
        return None
    if not re.search(
        r"PRODUCT\s*NAME\s+PACKING\s+.*Op\.?\s*Bal",
        text,
        re.I,
    ):
        return None

    raw_lines = text.splitlines()
    header_idx = next(
        (
            i
            for i, ln in enumerate(raw_lines)
            if re.search(r"PRODUCT\s*NAME", ln, re.I)
            and re.search(r"\bPACKING\b", ln, re.I)
        ),
        None,
    )
    if header_idx is None:
        return None
    header_line = raw_lines[header_idx]
    sub_line = ""
    if header_idx + 1 < len(raw_lines) and re.search(
        r"Qty|Value|Amt|Amount|Balance|Rate", raw_lines[header_idx + 1], re.I
    ):
        sub_line = raw_lines[header_idx + 1]
    plan = _sales_stock_column_plan(header_line, sub_line)
    if not plan:
        return None
    columns = plan["columns"]
    packing_at = int(plan["packing_at"])
    has_value = any(
        col["role"] in {
            "opening_value",
            "receipts_value",
            "total_value",
            "sales_value",
            "closing_value",
            "unit_rate",
        }
        for col in columns
    )

    result = empty_result(filename, "txt")
    items: List[Dict[str, Any]] = []
    footer_vals: Optional[Dict[str, float]] = None

    for ln in raw_lines:
        if not ln.strip():
            continue
        letters = re.sub(r"[^A-Za-z]", "", ln)
        if len(letters) < 4:
            continue

        if re.search(r"Sales\s*&\s*Stock\s*Statement", ln, re.I):
            title = re.search(
                r"Sales\s*&\s*Stock\s*Statement\s*\([^)]*\)",
                ln,
                re.I,
            )
            result["report_title"] = _clean_name(
                title.group(0) if title else "Sales & Stock Statement"
            )
            m_period = re.search(
                r"From\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+Upto\s+"
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
                ln,
                re.I,
            )
            if m_period:
                result["period_from"] = _normalize_date(m_period.group(1))
                result["period_to"] = _normalize_date(m_period.group(2))
            continue

        if re.search(r"\bCOMPANY\b", ln, re.I) and not re.search(
            r"Sales\s*&\s*Stock|PRODUCT\s*NAME|Page\s*No", ln, re.I
        ):
            if not result.get("company_name"):
                result["company_name"] = _clean_name(ln)
            continue

        if not result.get("stockist_name") and not re.search(
            r"Page\s*No|PRODUCT\s*NAME|Continued", ln, re.I
        ):
            result["stockist_name"] = _clean_name(ln)
            continue

        if (
            result.get("stockist_name")
            and not result.get("stockist_address")
            and not re.search(
                r"Page\s*No|PRODUCT\s*NAME|Sales\s*&\s*Stock|COMPANY|Continued",
                ln,
                re.I,
            )
        ):
            result["stockist_address"] = _clean_name(ln)
            continue

        if re.search(
            r"PRODUCT\s*NAME|^\s*-{5,}|^\s*\*{3,}|Continued|Page\s*No|^\s*Qty\.",
            ln,
            re.I,
        ):
            continue

        name = _slice_fixed_field(ln, 0, packing_at)
        packing = _slice_fixed_field(ln, packing_at, int(columns[0]["start"]))
        present: Dict[str, float] = {}
        numeric = 0
        rejected = False
        for col in columns:
            cell = _slice_fixed_field(ln, int(col["start"]), int(col["end"]))
            if not cell:
                continue
            if not re.fullmatch(r"-?\d+(?:\.\d+)?", cell):
                rejected = True
                break
            present[col["role"]] = _to_float(cell)
            numeric += 1
        if rejected or numeric < 2:
            continue

        if re.match(r"^(GRAND\s+)?TOTAL\b", name, re.I):
            footer_vals = present
            continue
        if len(name) < 2:
            continue

        item = empty_line_item()
        item["product_name"] = _clean_name(name)
        item["packing"] = packing or None
        item["opening_qty"] = present.get("opening_qty", 0.0)
        item["receipts_qty"] = present.get("receipts_qty", 0.0)
        item["sales_qty"] = present.get("sales_qty", 0.0)
        item["closing_qty"] = present.get("closing_qty", 0.0)
        item["sales_value"] = present.get("sales_value")
        item["closing_value"] = present.get("closing_value")
        if "total_stock" in present:
            item["extra"]["total_stock"] = present["total_stock"]
        for extra_key in (
            "opening_value",
            "receipts_value",
            "total_value",
            "unit_rate",
        ):
            if extra_key in present:
                item["extra"][extra_key] = present[extra_key]
        if "receipts_value" in present:
            item["extra"]["purchase_value"] = present["receipts_value"]
        if "sales_value" in present:
            item["extra"]["issue_value"] = present["sales_value"]
        item["extra"]["layout"] = "sales_stock_issue_qty"
        items.append(item)

    if not items:
        return None

    result["line_items"] = items
    result["totals"]["sales_value"] = None
    result["totals"]["closing_value"] = None
    result["totals"]["extra"]["extraction_method"] = "opbal_sales_stock_txt"
    result["totals"]["extra"]["qty_only"] = not has_value
    result["totals"]["extra"]["column_roles"] = [col["role"] for col in columns]
    if footer_vals:
        if "opening_qty" in footer_vals:
            result["totals"]["opening_qty"] = footer_vals["opening_qty"]
        if "receipts_qty" in footer_vals:
            result["totals"]["receipts_qty"] = footer_vals["receipts_qty"]
        if "sales_qty" in footer_vals:
            result["totals"]["sales_qty"] = footer_vals["sales_qty"]
        if "closing_qty" in footer_vals:
            result["totals"]["closing_qty"] = footer_vals["closing_qty"]
        if "sales_value" in footer_vals:
            result["totals"]["sales_value"] = footer_vals["sales_value"]
        if "closing_value" in footer_vals:
            result["totals"]["closing_value"] = footer_vals["closing_value"]
        if "total_stock" in footer_vals:
            result["totals"]["extra"]["total_stock_qty"] = footer_vals["total_stock"]
        result["totals"]["extra"]["total_row_source"] = "footer_total"
        result["totals"]["extra"]["total_row_labels"] = [col["label"] for col in columns]
    return result


def _strip_printer_controls(text: str) -> str:
    """Drop ESC/control bytes that dot-matrix stock reports embed in TXT files."""
    out = []
    i = 0
    raw = text or ""
    while i < len(raw):
        ch = raw[i]
        if ch == "\x1b":
            i += 1
            while i < len(raw) and raw[i] in "@0123456789":
                i += 1
            if i < len(raw) and raw[i].isalpha():
                i += 1
            continue
        if ord(ch) < 32 and ch not in "\t\n\r":
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _trailing_metrics(line: str, count: int):
    """Split a row into the left-hand name and the last `count` qty/value cells.

    Standalone dashes are empty cells and still occupy a column.
    """
    tokens = line.replace("|", " ").split()
    metrics = []
    index = len(tokens)
    while index > 0 and len(metrics) < count:
        tok = tokens[index - 1]
        plain = tok.replace(",", "") if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?", tok) else tok
        if plain in {"-", "--", "---"} or re.fullmatch(r"-?\d+(?:\.\d+)?", plain):
            metrics.append(plain)
            index -= 1
            continue
        break
    if len(metrics) < count:
        return None
    metrics.reverse()
    return tokens[:index], metrics


def _metric_number(raw: str, *, money: bool):
    if raw in {"-", "--", "---"}:
        return None if money else 0.0
    return _to_float(raw)


def _text_stock_families():
    """Header fingerprints for TXT stock statements the Kaveri parser does not cover."""
    return (
        (
            "vineet_sales_stock",
            r"DESCRIPTION\s*/\s*PACKING",
            r"OPENING",
            (
                "opening_qty",
                "receipts_qty",
                "sales_qty",
                "credit_qty",
                "debit_qty",
                "closing_qty",
            ),
            False,
        ),
        (
            "siva_paired_stock",
            r"PRODUCT\s*NAME",
            r"Issue\s*/\s*Sales|Shortage",
            (
                "opening_qty",
                "opening_value",
                "receipts_qty",
                "receipts_value",
                "total_stock",
                "sales_qty",
                "sales_value",
                "shortage_qty",
                "shortage_value",
                "expiry_qty",
                "expiry_value",
                "closing_qty",
                "closing_value",
            ),
            True,
        ),
        (
            "paired_opening_purchases",
            r"Product\s*Name",
            r"Opening\s+Stock",
            (
                "opening_qty",
                "opening_value",
                "receipts_qty",
                "receipts_value",
                "sales_qty",
                "sales_value",
                "closing_qty",
                "closing_value",
            ),
            False,
        ),
        (
            "panchaganga_unit",
            r"Product\s*Name",
            r"Op\.?\s*St|St-In",
            (
                "opening_qty",
                "receipts_qty",
                "sales_qty",
                "closing_qty",
                "closing_value",
            ),
            False,
        ),
        (
            "code_opening_closing_value",
            r"Product\s*Name",
            r"\bOpening\b",
            (
                "opening_qty",
                "receipts_qty",
                "total_stock",
                "sales_qty",
                "closing_qty",
                "closing_value",
            ),
            True,
        ),
        (
            "medicine_with_return",
            r"MEDICINE\s*NAME",
            r"\bPRTN\b",
            (
                "opening_qty",
                "receipts_qty",
                "total_stock",
                "return_qty",
                "loose_sale_qty",
                "adjust_qty",
                "sales_qty",
                "sales_value",
                "closing_qty",
                "closing_value",
            ),
            False,
        ),
        (
            "medicine_sal_val",
            r"MEDICINE\s*NAME",
            r"SAL\.?\s*VAL",
            (
                "opening_qty",
                "receipts_qty",
                "total_stock",
                "loose_sale_qty",
                "adjust_qty",
                "sales_qty",
                "sales_value",
                "closing_qty",
                "closing_value",
            ),
            False,
        ),
        (
            "ps_global_qoh",
            r"Product\s*Name",
            r"O\.?\s*Stk",
            (
                "opening_qty",
                "receipts_qty",
                "total_stock",
                "sales_qty",
                "closing_qty",
                "closing_value",
                "age_days",
            ),
            False,
        ),
        (
            "pipe_opn_recd",
            r"\bPRODUCT\b",
            r"\bOPN\.|\bRECD\b",
            (
                "opening_qty",
                "receipts_qty",
                "return_qty",
                "total_stock",
                "sales_qty",
                "closing_qty",
                "dump_qty",
            ),
            False,
        ),
        (
            "prakash_paired",
            r"Product\s*Name",
            r"<[-]+OB[-]+>",
            (
                "opening_qty",
                "opening_value",
                "receipts_qty",
                "receipts_value",
                "sales_qty",
                "sales_value",
                "closing_qty",
                "closing_value",
            ),
            False,
        ),
        (
            "shipra_issued",
            r"Product\s*name",
            r"Opng\.?\s*Bal",
            (
                "opening_qty",
                "receipts_qty",
                "receipt_other_qty",
                "total_stock",
                "sales_qty",
                "sales_value",
                "issue_other_qty",
                "closing_qty",
                "closing_value",
                "expirable_qty",
                "dump_qty",
            ),
            False,
        ),
    )


def _match_text_stock_family(text: str):
    for name, left_pat, right_pat, roles, has_code in _text_stock_families():
        if re.search(left_pat, text, re.I) and re.search(right_pat, text, re.I):
            if not re.search(
                r"STOCK|SALES\s*&\s*STOCK|S\.S\.REPORT|STOCK\s+STATEMENT",
                text,
                re.I,
            ):
                continue
            return name, roles, has_code
    return None


def _apply_stock_metrics(item: Dict[str, Any], roles, metrics) -> None:
    money_roles = {
        "opening_value",
        "receipts_value",
        "sales_value",
        "closing_value",
        "shortage_value",
    }
    line_roles = {
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "closing_qty",
        "sales_value",
        "closing_value",
    }
    for role, raw in zip(roles, metrics):
        money = role in money_roles or role.endswith("_value")
        number = _metric_number(raw, money=money)
        if role in line_roles:
            if role in {"sales_value", "closing_value"}:
                item[role] = number
            elif number is not None:
                item[role] = number
            continue
        if number is None:
            continue
        if role == "total_stock":
            item["extra"]["total_stock"] = number
        elif role == "receipts_value":
            item["extra"]["purchase_value"] = number
            item["extra"]["receipts_value"] = number
        elif role == "opening_value":
            item["extra"]["opening_value"] = number
        else:
            item["extra"][role] = number
    if item.get("sales_value") not in (None, "") and "issue_value" not in item["extra"]:
        item["extra"]["issue_value"] = item["sales_value"]


def _parse_text_stock_fallback(text: str, filename: str) -> Optional[Dict[str, Any]]:
    """Parse other fixed stock-statement TXT layouts. Unused when a specific parser already matched."""
    cleaned = _strip_printer_controls(text)
    matched = _match_text_stock_family(cleaned)
    if not matched:
        return None
    family, roles, has_code = matched
    result = empty_result(filename, "txt")
    items: List[Dict[str, Any]] = []
    for ln in cleaned.splitlines():
        raw = ln.strip()
        if not raw or set(raw) <= {"-", "=", "_", " "}:
            continue
        if re.search(r"\bFrom\s*:", raw, re.I) and not result.get("stockist_name"):
            left_name = re.split(r"\bFrom\s*:", raw, flags=re.I)[0].strip()
            if len(re.sub(r"[^A-Za-z]", "", left_name)) >= 4:
                result["stockist_name"] = _clean_name(left_name)
        if re.search(r"COMPANY\s*(?:NAME)?\s*:", raw, re.I):
            company = re.split(r"COMPANY\s*(?:NAME)?\s*:", raw, flags=re.I)[-1]
            company = re.split(r"\d{1,2}[./-]\d{1,2}", company)[0]
            company = re.split(r"\bPage\b", company, flags=re.I)[0]
            result["company_name"] = _clean_name(company).rstrip(" :")
        elif not result.get("company_name") and re.search(
            r"HIMALAYA", raw, re.I
        ) and not re.search(r"PRODUCT|DESCRIPTION|MEDICINE|Page\s*No", raw, re.I):
            if len(raw) < 80:
                result["company_name"] = _clean_name(raw.strip("* "))
        m_period = re.search(
            r"(?:FROM|From|w\.e\.f\.?)\s*:?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*(?:TO|Upto|and|to|-|–)\s*"
            r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
            raw,
            re.I,
        )
        if m_period and not result.get("period_from"):
            result["period_from"] = _normalize_date(m_period.group(1))
            result["period_to"] = _normalize_date(m_period.group(2))
        else:
            m_from = re.search(
                r"\bFrom\s*:?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", raw, re.I
            )
            m_to = re.search(
                r"\bTo\s*:?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", raw, re.I
            )
            if m_from and not result.get("period_from"):
                result["period_from"] = _normalize_date(m_from.group(1))
            if m_to and not result.get("period_to"):
                result["period_to"] = _normalize_date(m_to.group(1))
        if re.search(r"SALES\s*&\s*STOCK|STOCK\s+AND\s+SALES|STOCK\s+STATEMENT|S\.S\.REPORT", raw, re.I):
            if not result.get("report_title"):
                result["report_title"] = _clean_name(raw)[:80]
        if '"' in raw or re.search(r"\bSlrt\b", raw, re.I):
            continue
        split = _trailing_metrics(raw, len(roles))
        if not split:
            if not result.get("stockist_name") and not re.search(
                r"STOCK|SALES|FROM\s*:|COMPANY|Page|PRODUCT|DESCRIPTION|MEDICINE|GST|BETWEEN",
                raw,
                re.I,
            ):
                if (
                    len(re.sub(r"[^A-Za-z]", "", raw)) >= 4
                    and not re.search(r"TOTAL|Value\s+Rs|Page\b", raw, re.I)
                ):
                    result["stockist_name"] = _clean_name(raw)
            continue
        left, metrics = split
        label = _clean_name(" ".join(left))
        if not label or re.search(
            r"PRODUCT|DESCRIPTION|PACKING|OPENING|MEDICINE|QTY\.|VALUE|STOCK\b",
            label,
            re.I,
        ):
            continue
        if re.match(r"^(GRAND\s+)?TOTAL\b", label, re.I):
            parsed = {}
            for role, cell in zip(roles, metrics):
                money = role.endswith("_value")
                number = _metric_number(cell, money=money)
                if number is None:
                    continue
                parsed[role] = number
            if re.search(r"AMOUNT", label, re.I):
                if "sales_qty" in parsed and "sales_value" not in parsed:
                    result["totals"]["sales_value"] = parsed.get("sales_qty")
                result["totals"]["closing_value"] = parsed.get("closing_value", parsed.get("closing_qty"))
                result["totals"]["extra"]["amount_row"] = parsed
            else:
                for key in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
                    if key in parsed:
                        result["totals"][key] = parsed[key]
                result["totals"]["extra"]["total_row_source"] = "footer_total"
            continue
        item = empty_line_item()
        tokens = left
        if has_code and tokens and re.fullmatch(r"[A-Z0-9./-]{2,12}", tokens[0], re.I):
            item["product_code"] = tokens[0]
            tokens = tokens[1:]
        packing = None
        if family != "vineet_sales_stock" and len(tokens) >= 2:
            if (
                len(tokens) >= 3
                and re.fullmatch(r"\d+(?:\.\d+)?", tokens[-2])
                and re.fullmatch(r"[A-Za-z]{1,6}", tokens[-1])
            ):
                packing = f"{tokens[-2]} {tokens[-1]}"
                tokens = tokens[:-2]
            else:
                tail = tokens[-1]
                if re.search(r"\d|'|`|ML|GM|TAB|CAP|SYP", tail, re.I):
                    packing = tail.rstrip(",")
                    tokens = tokens[:-1]
        name = _clean_name(" ".join(tokens))
        if len(name) < 2:
            continue
        item["product_name"] = name
        item["packing"] = packing
        item["sales_value"] = None
        item["closing_value"] = None
        item["extra"]["layout"] = "sales_stock_issue_qty"
        item["extra"]["txt_family"] = family
        _apply_stock_metrics(item, roles, metrics)
        items.append(item)
    if not items:
        return None
    result["line_items"] = items
    result["totals"]["extra"]["extraction_method"] = "txt_stock_fallback"
    result["totals"]["extra"]["txt_family"] = family
    result["totals"]["extra"]["column_roles"] = list(roles)
    if result["totals"].get("sales_value") is None and not any(
        role.endswith("_value") for role in roles
    ):
        result["totals"]["extra"]["qty_only"] = True
    return result


def _parse_txt(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    text = file_bytes.decode("utf-8", errors="ignore")
    if "\x00" in text[:200]:
        text = file_bytes.decode("latin-1", errors="ignore")

    fixed = _parse_fixed_sales_stock_statement(text, filename)
    if fixed and fixed.get("line_items"):
        return fixed

    if _is_product_stock_report_text(text):
        psr = _parse_product_stock_report(text, filename, "txt")
        if psr and psr.get("line_items"):
            return psr

    result = empty_result(filename, "txt")
    lines = [ln.rstrip() for ln in text.splitlines()]

    # Header: stockist on first non-empty line
    non_empty = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("=")]
    if non_empty:
        result["stockist_name"] = _clean_name(non_empty[0])
    if len(non_empty) > 1 and not re.search(
        r"MONTHLY|STOCK|SALES|COMPANY|FROM", non_empty[1], re.I
    ):
        result["stockist_address"] = _clean_name(non_empty[1])

    for ln in lines:
        if re.search(r"MONTHLY\s+STOCK|STOCK\s*&\s*SALES|STOCK AND SALES", ln, re.I):
            result["report_title"] = _clean_name(ln)
        m_co = re.search(r"COMPANY\s*NAME\s*:?\s*(.+)$", ln, re.I)
        if m_co and m_co.group(1).strip():
            result["company_name"] = _clean_name(m_co.group(1))
        # COMPANY NAME on previous line, value on next — handled below
        m_from = re.search(
            r"(?:FROM|FORM)\s*:?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*(?:TO|:)?\s*"
            r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})?",
            ln,
            re.I,
        )
        if m_from:
            result["period_from"] = _normalize_date(m_from.group(1))
            if m_from.group(2):
                result["period_to"] = _normalize_date(m_from.group(2))

    # Company name may be on its own line after "COMPANY NAME :"
    for i, ln in enumerate(lines):
        if re.search(r"COMPANY\s*NAME\s*:?\s*$", ln.strip(), re.I) and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt and not nxt.startswith("="):
                result["company_name"] = _clean_name(nxt)
                break
        if re.search(r"COMPANY\s*NAME\s*:", ln, re.I):
            after = re.split(r"COMPANY\s*NAME\s*:", ln, flags=re.I)[-1].strip()
            if after:
                result["company_name"] = _clean_name(after)

    items: List[Dict[str, Any]] = []
    for ln in lines:
        raw = ln.strip()
        if not raw or raw.startswith("=") or raw.startswith("PRD") or raw.startswith("CODE"):
            continue
        if re.search(r"^\s*TOTAL\s*:", raw, re.I):
            nums = re.findall(r"-?\d+(?:\.\d+)?", raw)
            if nums:
                # Product-grid total has 3 amounts (receipts/sales/closing).
                # Trailing voucher TOTAL with a single 0.00 must not overwrite.
                floats = [_to_float(n) for n in nums]
                if len(floats) >= 3:
                    result["totals"]["extra"]["receipts_value"] = floats[-3]
                    result["totals"]["sales_value"] = floats[-2]
                    result["totals"]["closing_value"] = floats[-1]
                elif len(floats) >= 2 and result["totals"]["sales_value"] is None:
                    result["totals"]["sales_value"] = floats[-2]
                    result["totals"]["closing_value"] = floats[-1]
            continue
        if re.match(r"^(VOTNO|Signature|FORM|FROM|TIME|DATE|PAGE|COMPANY)", raw, re.I):
            continue

        item = _parse_kaveri_line(raw)
        if item:
            items.append(item)

    if not items:
        fallback = _parse_text_stock_fallback(text, filename)
        if fallback and fallback.get("line_items"):
            return fallback

    result["line_items"] = items
    return result


def _parse_kaveri_line(line: str) -> Optional[Dict[str, Any]]:
    m = _KAVERI_ROW.match(line)
    if m:
        item = empty_line_item()
        item["product_code"] = m.group(1)
        item["product_name"] = _clean_name(m.group(2))
        item["packing"] = m.group(3)
        item["opening_qty"] = _to_float(m.group(4))
        item["receipts_qty"] = _to_float(m.group(5))
        item["extra"]["total_stock"] = _to_float(m.group(6))
        item["sales_qty"] = _to_float(m.group(7))
        item["sales_value"] = _to_float(m.group(8))
        item["closing_qty"] = _to_float(m.group(9))
        item["closing_value"] = _to_float(m.group(10))
        if m.group(11):
            item["extra"]["exp_batch"] = m.group(11)
        if m.group(12):
            item["extra"]["exp_date"] = m.group(12)
        return item

    # Fallback: code + right-aligned numeric fields
    m2 = re.match(r"^(\d{3,5})\s+(.+)$", line)
    if not m2:
        return None
    code = m2.group(1)
    rest = m2.group(2).strip()
    # Pull last 7–8 numbers (opening..closing_value [+ batch/date ignored if non-numeric])
    tokens = rest.split()
    if len(tokens) < 8:
        return None
    # Find packing token (contains * preferred)
    pack_idx = None
    for i, tok in enumerate(tokens):
        if "*" in tok:
            pack_idx = i
            break
    if pack_idx is None:
        # packing often just before opening qty sequence
        # take last 7 numeric tokens as metrics
        nums: List[str] = []
        cut = len(tokens)
        for i in range(len(tokens) - 1, -1, -1):
            if re.fullmatch(r"-?\d+(?:\.\d+)?", tokens[i]):
                nums.append(tokens[i])
                cut = i
                if len(nums) >= 7:
                    break
            elif nums:
                break
        nums = list(reversed(nums))
        if len(nums) < 7:
            return None
        name_pack = " ".join(tokens[:cut]).strip()
        packing = None
        product_name = name_pack
    else:
        product_name = " ".join(tokens[:pack_idx]).strip()
        packing = tokens[pack_idx]
        after = tokens[pack_idx + 1 :]
        nums = [t for t in after if re.fullmatch(r"-?\d+(?:\.\d+)?", t)]
        if len(nums) < 7:
            return None

    item = empty_line_item()
    item["product_code"] = code
    item["product_name"] = _clean_name(product_name)
    item["packing"] = packing
    item["opening_qty"] = _to_float(nums[0])
    item["receipts_qty"] = _to_float(nums[1])
    item["extra"]["total_stock"] = _to_float(nums[2])
    item["sales_qty"] = _to_float(nums[3])
    item["sales_value"] = _to_float(nums[4])
    item["closing_qty"] = _to_float(nums[5])
    item["closing_value"] = _to_float(nums[6])
    return item


# ---------------------------------------------------------------------------
# HTM (Livem-style)
# ---------------------------------------------------------------------------

def _map_livem_numbers(nums: List[float]) -> Dict[str, Any]:
    """Map Livem numeric cells to unified fields.

    Livem columns (when present):
      LMS (opening) | TotalStock Qty | Stock Amount | Sales Qty | Sales Amt | Close Qty | Close Amt

    The "Purchase Qty" cell is total available stock (opening + receipts), not receipts alone.
    """
    item = empty_line_item()
    if len(nums) >= 7:
        opening = nums[0]
        total_stock = nums[1]
        item["opening_qty"] = opening
        item["receipts_qty"] = max(0.0, total_stock - opening)
        item["extra"]["total_stock"] = total_stock
        item["extra"]["stock_value"] = nums[2]
        item["sales_qty"] = nums[3]
        item["sales_value"] = nums[4]
        item["closing_qty"] = nums[5]
        item["closing_value"] = nums[6]
    elif len(nums) == 6:
        opening = nums[0]
        total_stock = nums[1]
        item["opening_qty"] = opening
        item["receipts_qty"] = max(0.0, total_stock - opening)
        item["extra"]["total_stock"] = total_stock
        item["extra"]["stock_value"] = nums[2]
        item["sales_qty"] = nums[3]
        item["sales_value"] = nums[4]
        item["closing_qty"] = nums[5]
    elif len(nums) == 5:
        # LMS, TotalStock, Amount, Close Qty, Close Amt (no sales movement)
        opening = nums[0]
        total_stock = nums[1]
        item["opening_qty"] = opening
        item["receipts_qty"] = max(0.0, total_stock - opening)
        item["extra"]["total_stock"] = total_stock
        item["extra"]["stock_value"] = nums[2]
        item["closing_qty"] = nums[3]
        item["closing_value"] = nums[4]
    elif len(nums) == 4:
        # LMS, Opening/Close value, Close Qty, Close Amt — no purchase/sales
        item["opening_qty"] = nums[0]
        item["extra"]["opening_value"] = nums[1]
        item["closing_qty"] = nums[2]
        item["closing_value"] = nums[3]
        if abs(nums[0] - nums[2]) < 1e-9 and abs(nums[1] - nums[3]) < 1e-9:
            # mirrored open/close — treat amount as both opening and closing value
            pass
    elif len(nums) == 3:
        item["opening_qty"] = nums[0]
        item["closing_qty"] = nums[1]
        item["closing_value"] = nums[2]
    elif len(nums) == 2:
        item["closing_qty"] = nums[0]
        item["closing_value"] = nums[1]
        item["opening_qty"] = nums[0]
    elif len(nums) == 1:
        item["closing_qty"] = nums[0]
        item["opening_qty"] = nums[0]
    else:
        item["extra"]["raw_numbers"] = nums
    return item


def _parse_htm(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    from bs4 import BeautifulSoup

    result = empty_result(filename, "htm")
    html = file_bytes.decode("latin-1", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")

    rows: List[List[str]] = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if cells:
            rows.append(cells)

    page_text = soup.get_text("\n", strip=True)
    if _is_product_stock_report_text(page_text):
        psr = _parse_product_stock_report_from_rows(
            rows, filename, "htm", page_text
        )
        if psr and psr.get("line_items"):
            return psr

    for cells in rows:
        joined = " ".join(cells)
        if len(cells) == 1 and not re.search(r"_+|Company|Product|TOTAL|SUB", cells[0], re.I):
            if not result["stockist_name"]:
                result["stockist_name"] = _clean_name(cells[0])
        if "Company" in joined:
            for i, c in enumerate(cells):
                if re.search(r"Company\s*:", c, re.I) and i + 1 < len(cells):
                    result["company_name"] = _clean_name(cells[i + 1])
                elif re.search(r"Company\s*:\s*(.+)", c, re.I):
                    result["company_name"] = _clean_name(
                        re.search(r"Company\s*:\s*(.+)", c, re.I).group(1)
                    )
            m_title = re.search(r"Stock and Sales Report.*", joined, re.I)
            if m_title:
                result["report_title"] = _clean_name(m_title.group(0))
            m_mo = re.search(r"Month\s+Of\s+([A-Za-z]+)\s+(\d{4})", joined, re.I)
            if m_mo:
                pf, pt = _month_period(m_mo.group(1), int(m_mo.group(2)))
                result["period_from"], result["period_to"] = pf, pt

    items: List[Dict[str, Any]] = []
    for cells in rows:
        if len(cells) < 2:
            continue
        name = cells[0]
        if re.search(
            r"^(Product|Opening|Purchase|Sale|Closing|Company|SUB|Total|_+|LIVEM)",
            name,
            re.I,
        ):
            if re.search(r"^SUB\s*TOTAL|^Total\s*:", name, re.I) or (
                len(cells) >= 2 and re.search(r"^Total\s*:", name, re.I)
            ):
                nums = [_to_float(c) for c in cells if re.search(r"\d", c)]
                # opening_amt, purchase_return?, sales, closing
                if len(nums) >= 4:
                    result["totals"]["extra"]["opening_value"] = nums[-4]
                    result["totals"]["extra"]["purchase_return_value"] = nums[-3]
                    result["totals"]["sales_value"] = nums[-2]
                    result["totals"]["closing_value"] = nums[-1]
                elif len(nums) >= 2:
                    result["totals"]["sales_value"] = nums[-2]
                    result["totals"]["closing_value"] = nums[-1]
            continue
        if re.fullmatch(r"_+", name) or name.startswith("_"):
            continue

        packing = cells[1] if len(cells) > 1 else None
        nums_start = 2
        if packing and re.fullmatch(r"-?\d+(?:\.\d+)?", packing.replace(",", "")):
            packing = None
            nums_start = 1

        num_cells = cells[nums_start:]
        nums = [_to_float(c) for c in num_cells if re.search(r"\d", c)]

        # Source rows that only list product + pack have no stock figures in the HTM.
        # Skip them so the response is not filled with all-zero placeholders.
        if not nums:
            continue

        item = _map_livem_numbers(nums)
        item["product_name"] = _clean_name(name)
        item["packing"] = packing
        items.append(item)

    result["line_items"] = items
    return result


# ---------------------------------------------------------------------------
# Word (.doc / .docx)
# ---------------------------------------------------------------------------

def _parse_word(file_bytes: bytes, filename: str, ext: str) -> Dict[str, Any]:
    """Parse Word sales statements (.docx via python-docx; .doc via convert/fallback)."""
    # Misnamed or sniffed OOXML
    if ext == ".docx" or file_bytes[:2] == b"PK":
        return _parse_docx(file_bytes, filename)

    # Legacy .doc → try convert to docx, else extract text
    converted = _convert_doc_to_docx_bytes(file_bytes)
    if converted:
        result = _parse_docx(converted, filename)
        result["source_format"] = "doc"
        return result

    text = _extract_legacy_doc_text(file_bytes)
    if text.strip():
        # Reuse TXT parser heuristics on extracted Word text
        result = _parse_txt(text.encode("utf-8", errors="ignore"), filename)
        result["source_format"] = "doc"
        result["totals"]["extra"]["extraction_method"] = "legacy_doc_text"
        return result

    raise ValueError(
        "Could not parse legacy .doc file. Please re-save as .docx and upload again."
    )


def _convert_doc_to_docx_bytes(file_bytes: bytes) -> Optional[bytes]:
    """Convert legacy .doc to .docx via Microsoft Word COM when available (Windows)."""
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError:
        return None

    import os
    import tempfile

    in_path = out_path = None
    word = None
    try:
        pythoncom.CoInitialize()
        fd_in, in_path = tempfile.mkstemp(suffix=".doc")
        os.close(fd_in)
        fd_out, out_path = tempfile.mkstemp(suffix=".docx")
        os.close(fd_out)
        with open(in_path, "wb") as fh:
            fh.write(file_bytes)

        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(in_path, ReadOnly=True)
        # 16 = wdFormatXMLDocument (.docx)
        doc.SaveAs(out_path, FileFormat=16)
        doc.Close(False)
        with open(out_path, "rb") as fh:
            return fh.read()
    except Exception as exc:
        logger.warning("Word COM .doc conversion failed: %s", exc)
        return None
    finally:
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
        for path in (in_path, out_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def _extract_legacy_doc_text(file_bytes: bytes) -> str:
    """Best-effort text extraction from legacy OLE .doc without Word installed."""
    chunks: List[str] = []
    try:
        import olefile

        with olefile.OleFileIO(io.BytesIO(file_bytes)) as ole:
            for stream_name in ("WordDocument", "1Table", "0Table"):
                if not ole.exists(stream_name):
                    continue
                raw = ole.openstream(stream_name).read()
                # Prefer UTF-16LE runs (common in Word binary)
                try:
                    decoded = raw.decode("utf-16-le", errors="ignore")
                    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", decoded)
                    if len(cleaned.strip()) > 40:
                        chunks.append(cleaned)
                except Exception:
                    pass
                ascii_parts = re.findall(rb"[\x20-\x7e]{4,}", raw)
                if ascii_parts:
                    chunks.append(b" ".join(ascii_parts).decode("ascii", errors="ignore"))
    except Exception as exc:
        logger.warning("OLE .doc text extract failed: %s", exc)

    if not chunks:
        # Last resort: scan whole file for readable ASCII
        ascii_parts = re.findall(rb"[\x20-\x7e]{4,}", file_bytes)
        if ascii_parts:
            chunks.append(b"\n".join(ascii_parts).decode("ascii", errors="ignore"))

    return "\n".join(chunks)


def _norm_docx_header(header: Any) -> str:
    """Normalize Word table headers: 'Cr.Sch.Qty' / 'Op. Val' -> 'crschqty' / 'opval'."""
    return re.sub(r"[^a-z0-9]", "", str(header or "").strip().lower())


def _unglue_stock_sales_label(text: str) -> str:
    """Insert spaces into glued Stock-and-Sales Detail Report labels."""
    t = _clean_name(text)
    if not t:
        return t
    replacements = (
        (r"(?i)stockandsalesdetail", "Stock and Sales Detail"),
        (r"(?i)stockandsales", "Stock and Sales"),
        (r"(?i)\baurobindo(?=pharma)", "AUROBINDO "),
        (r"(?i)pharma(?=ltd|limited)", "PHARMA "),
        (r"(?i)\brajesh(?=medicos)", "RAJESH "),
        (r"(?i)\bmedicos\s*-\s*", "MEDICOS - "),
    )
    for pat, repl in replacements:
        t = re.sub(pat, repl, t)
    return _clean_name(t)


# Canonical Stock-and-Sales Detail Report columns (20-col Aurobindo / Medicos format)
_STOCK_SALES_DETAIL_HEADER_ALIASES: Dict[str, Tuple[str, ...]] = {
    "sl_no": ("slno", "sno", "srno"),
    "product_name": ("item", "product", "productname", "particulars"),
    "opening_qty": ("opqty", "openingqty", "opbal", "opbalqty"),
    "opening_value": ("opval", "openingval", "openingvalue", "opvalue"),
    "receipts_qty": ("pqty", "purchaseqty", "purqty", "receiptsqty"),
    "purchase_scheme": ("psch", "purchasescheme", "pursch", "purchasesch"),
    "purchase_value": ("pval", "purchaseval", "purchasevalue", "purval"),
    "sales_qty": ("sqty", "saleqty", "salesqty"),
    "sales_scheme": ("ssch", "salescheme", "salesscheme", "salesch"),
    "sales_value": ("sval", "saleval", "salesval", "salesvalue"),
    "break_sales_qty": ("brsqty", "brsaleqty", "breaksqty", "breakagesqty"),
    "break_sales_value": ("brsval", "brsaleval", "breaksval", "breakagesval"),
    "credit_qty": ("crqty", "creditqty", "crnoteqty"),
    "credit_scheme_qty": ("crschqty", "creditschemeqty", "crsch"),
    "credit_value": ("crval", "creditval", "creditvalue"),
    "debit_qty": ("dbqty", "debitqty", "drqty"),
    "debit_scheme_qty": ("dbschqty", "debitschemeqty", "dbsch", "drschqty"),
    "debit_value": ("dbval", "debitval", "debitvalue", "drval"),
    "closing_qty": ("clqty", "closingqty", "clqnt", "balqty", "closingbal"),
    "closing_value": ("clval", "closingval", "closingvalue", "clvalue", "balval"),
}


def _map_stock_sales_detail_headers(headers: List[Any]) -> Dict[str, int]:
    """Map normalized header cells onto canonical field names."""
    mapping: Dict[str, int] = {}
    for idx, header in enumerate(headers):
        key = _norm_docx_header(header)
        if not key:
            continue
        for field, aliases in _STOCK_SALES_DETAIL_HEADER_ALIASES.items():
            if field in mapping:
                continue
            if key == field.replace("_", "") or key in aliases:
                mapping[field] = idx
                break
    return mapping


def _cell_at(cells: List[str], idx: Optional[int]) -> str:
    if idx is None or idx < 0 or idx >= len(cells):
        return ""
    return str(cells[idx] if cells[idx] is not None else "").strip()


def _parse_stock_sales_detail_total_row(
    cells: List[str], colmap: Dict[str, int], result: Dict[str, Any]
) -> None:
    """Capture Total-row values so closing/sales match the printed report."""
    def _val(field: str, fallback_idx: Optional[int] = None) -> Optional[float]:
        raw = _cell_at(cells, colmap.get(field, fallback_idx))
        return _to_nullable_float(raw) if raw not in (None, "") else None

    sales = _val("sales_value", 9)
    closing = _val("closing_value", 19)
    if sales is not None:
        result["totals"]["sales_value"] = sales
    if closing is not None:
        result["totals"]["closing_value"] = closing

    extra = result["totals"].setdefault("extra", {})
    extra["raw_total_row"] = cells
    for field, fallback in (
        ("opening_value", 3),
        ("purchase_value", 6),
        ("break_sales_value", 11),
        ("credit_value", 14),
        ("debit_value", 17),
        ("opening_qty", 2),
        ("receipts_qty", 4),
        ("purchase_scheme", 5),
        ("sales_qty", 7),
        ("sales_scheme", 8),
        ("break_sales_qty", 10),
        ("credit_qty", 12),
        ("credit_scheme_qty", 13),
        ("debit_qty", 15),
        ("debit_scheme_qty", 16),
        ("closing_qty", 18),
    ):
        value = _val(field, fallback)
        if value is not None:
            extra[field] = value


def _parse_stock_sales_detail_item_row(
    cells: List[str], colmap: Dict[str, int]
) -> Optional[Dict[str, Any]]:
    """Build one line item with every Stock-and-Sales Detail column preserved."""
    # Prefer header map; fall back to fixed 20-col positions used by this report.
    positional = {
        "sl_no": 0,
        "product_name": 1,
        "opening_qty": 2,
        "opening_value": 3,
        "receipts_qty": 4,
        "purchase_scheme": 5,
        "purchase_value": 6,
        "sales_qty": 7,
        "sales_scheme": 8,
        "sales_value": 9,
        "break_sales_qty": 10,
        "break_sales_value": 11,
        "credit_qty": 12,
        "credit_scheme_qty": 13,
        "credit_value": 14,
        "debit_qty": 15,
        "debit_scheme_qty": 16,
        "debit_value": 17,
        "closing_qty": 18,
        "closing_value": 19,
    }

    def raw(field: str) -> str:
        return _cell_at(cells, colmap.get(field, positional.get(field)))

    product_name = _clean_name(raw("product_name").replace("\n", " ").replace("\r", " "))
    sl_no = raw("sl_no")
    if not product_name:
        return None
    # Skip repeated header rows inside later tables
    if _norm_docx_header(product_name) in {"item", "product"}:
        return None
    if sl_no and not re.match(r"^\d+$", sl_no) and _norm_docx_header(sl_no) in {
        "slno", "total", "sno"
    }:
        return None

    item = empty_line_item()
    item["product_name"] = product_name
    item["opening_qty"] = _to_float(raw("opening_qty"))
    item["receipts_qty"] = _to_float(raw("receipts_qty"))
    item["sales_qty"] = _to_float(raw("sales_qty"))
    item["sales_value"] = _to_float(raw("sales_value"))
    item["closing_qty"] = _to_float(raw("closing_qty"))
    item["closing_value"] = _to_float(raw("closing_value"))

    # Keep every remaining numeric column (including zeros) for reconciliation.
    extra_fields = (
        "opening_value",
        "purchase_scheme",
        "purchase_value",
        "sales_scheme",
        "break_sales_qty",
        "break_sales_value",
        "credit_qty",
        "credit_scheme_qty",
        "credit_value",
        "debit_qty",
        "debit_scheme_qty",
        "debit_value",
    )
    extra: Dict[str, Any] = {}
    if sl_no:
        extra["sl_no"] = sl_no
    for field in extra_fields:
        cell = raw(field)
        # Always store when the column exists in the sheet (header or positional)
        if field in colmap or len(cells) > positional[field]:
            extra[field] = _to_float(cell) if cell != "" else 0.0
    item["extra"] = extra
    return item


def _parse_docx(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    from docx import Document

    result = empty_result(filename, "docx")
    doc = Document(io.BytesIO(file_bytes))

    for para in doc.paragraphs:
        t = _clean_name(para.text)
        if not t:
            continue
        if re.search(r"Stock\s*and\s*Sales|StockandSales", t, re.I):
            result["report_title"] = _unglue_stock_sales_label(t)
        m_seller = re.search(r"Seller\s*:\s*(.+?)(?:\s+From|\s*$)", t, re.I)
        if m_seller:
            result["stockist_name"] = _unglue_stock_sales_label(m_seller.group(1))
        m_from = re.search(
            r"From\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*to\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
            t,
            re.I,
        )
        if m_from:
            result["period_from"] = _normalize_date(m_from.group(1))
            result["period_to"] = _normalize_date(m_from.group(2))
        if "ReportDate" in t.replace(" ", "") or re.search(r"Report\s*Date", t, re.I):
            company = re.split(r"Report\s*Date", t, flags=re.I)[0]
            company = re.sub(r"\s+", " ", company).strip(" -\t")
            if company:
                result["company_name"] = _unglue_stock_sales_label(company)

    items: List[Dict[str, Any]] = []
    is_stock_sales_detail = False

    for table in doc.tables:
        if not table.rows:
            continue
        headers = [c.text.strip() for c in table.rows[0].cells]
        colmap = _map_stock_sales_detail_headers(headers)
        header_norm = {_norm_docx_header(h) for h in headers}
        table_is_detail = (
            "item" in header_norm
            and ("clval" in header_norm or "closingvalue" in header_norm)
            and ("opqty" in header_norm or "openingqty" in header_norm)
        )
        if table_is_detail:
            is_stock_sales_detail = True

        for row in table.rows[1:]:
            cells = [c.text.strip() for c in row.cells]
            if not cells or not any(cells):
                continue

            first_norm = _norm_docx_header(cells[0])
            if first_norm in {"slno", "sno", "srno"}:
                continue
            if first_norm == "total" or str(cells[0]).strip().lower() == "total":
                if table_is_detail or len(cells) >= 20:
                    _parse_stock_sales_detail_total_row(cells, colmap, result)
                else:
                    # Legacy narrow table fallback
                    if len(cells) > 9:
                        result["totals"]["sales_value"] = _to_nullable_float(cells[9])
                    if len(cells) > 19:
                        result["totals"]["closing_value"] = _to_nullable_float(cells[19])
                    result["totals"]["extra"]["raw"] = cells
                continue

            if table_is_detail or (
                "item" in header_norm and ("clval" in colmap or len(cells) >= 20)
            ):
                item = _parse_stock_sales_detail_item_row(cells, colmap)
                if item:
                    items.append(item)
                continue

            # Non Stock-and-Sales-Detail Word tables: keep previous best-effort path
            if not re.match(r"^\d+$", cells[0].strip()) and len(cells) < 3:
                continue
            item = empty_line_item()
            if len(cells) < 10:
                continue
            item["product_name"] = _clean_name(cells[1].replace("\n", " "))
            item["opening_qty"] = _to_float(cells[2])
            item["receipts_qty"] = _to_float(cells[4] if len(cells) > 4 else 0)
            item["sales_qty"] = _to_float(cells[7] if len(cells) > 7 else 0)
            item["sales_value"] = _to_float(cells[9] if len(cells) > 9 else 0)
            item["closing_qty"] = _to_float(cells[18] if len(cells) > 18 else 0)
            item["closing_value"] = _to_float(cells[19] if len(cells) > 19 else 0)
            item["extra"] = {
                "sl_no": cells[0],
                "opening_value": _to_float(cells[3] if len(cells) > 3 else 0),
                "purchase_scheme": _to_float(cells[5] if len(cells) > 5 else 0),
                "purchase_value": _to_float(cells[6] if len(cells) > 6 else 0),
                "sales_scheme": _to_float(cells[8] if len(cells) > 8 else 0),
            }
            if item["product_name"]:
                items.append(item)

    result["line_items"] = items
    if is_stock_sales_detail:
        result["totals"].setdefault("extra", {})["extraction_method"] = (
            "docx_stock_sales_detail"
        )
        # Prefer printed Total-row closing; if absent, fall back to line sum.
        if result["totals"].get("closing_value") is None and items:
            result["totals"]["closing_value"] = round(
                sum(_to_float(i.get("closing_value")) for i in items), 2
            )
        if result["totals"].get("sales_value") is None and items:
            result["totals"]["sales_value"] = round(
                sum(_to_float(i.get("sales_value")) for i in items), 2
            )
        line_closing = round(sum(_to_float(i.get("closing_value")) for i in items), 2)
        printed_closing = result["totals"].get("closing_value")
        result["totals"]["extra"]["line_closing_value_sum"] = line_closing
        if printed_closing is not None:
            result["totals"]["extra"]["closing_value_matches_lines"] = (
                abs(float(printed_closing) - line_closing) < 0.05
            )
    return result


# ---------------------------------------------------------------------------
# XLS / XLSX (Victory-style sparse grid)
# ---------------------------------------------------------------------------

_XLS_HEADER_ALIASES = {
    "item": "item",
    "itemname": "item",
    "product": "item",
    "productname": "item",
    "description": "item",
    "desc": "item",
    "productdescription": "item",
    "productdesc": "item",
    "productdesc.": "item",
    "skudescription": "item",
    "skudesc": "item",
    "medicinename": "item",
    "drugname": "item",
    "matname": "item",
    "materialname": "item",
    "material": "item",
    "pack": "pack",
    "packing": "pack",
    "packsize": "pack",
    "packingsize": "pack",
    "size": "pack",
    "op": "op",
    "op.": "op",
    "opstock": "op",
    "op.stock": "op",
    "opening": "op",
    "openingstock": "op",
    "openingqty": "op",
    "obqty": "op",
    "ob(qty)": "op",
    "ob": "op",
    "openingquantity": "op",
    "openingbalqty": "op",
    "openingbalanceqty": "op",
    "pur": "pur",
    "purch": "pur",
    "purchase": "pur",
    "purchases": "pur",
    "purchaseqty": "pur",
    "receipt": "pur",
    "receipts": "pur",
    "receiptqty": "pur",
    "receiptsqty": "pur",
    "receive": "pur",
    "receiveqty": "pur",
    "inward": "pur",
    "inwardqty": "pur",
    "primarypurchase": "pur",
    "primary": "pur",
    "primaryqty": "pur",
    "primary(qty)": "pur",
    "sale": "sale",
    "sales": "sale",
    "saleqty": "sale",
    "salesqty": "sale",
    "soldqty": "sale",
    "secondaryqtytotal": "sale",
    "secondaryqty": "sale",
    "secondaryquantity": "sale",
    "secondarysales": "sale",
    "issue": "sale",
    "issueqty": "sale",
    "sold": "sale",
    "bal": "bal",
    "bal.": "bal",
    "clstock": "bal",
    "cl.stock": "bal",
    "closing": "bal",
    "closingstock": "bal",
    "closingqty": "bal",
    "closingquantity": "bal",
    "cbqty": "bal",
    "cb(qty)": "bal",
    "cb": "bal",
    "closingbalqty": "bal",
    "closingbalanceqty": "bal",
    "date": "date",
    "statementdate": "date",
    "stockdate": "date",
    "reportdate": "date",
    "ason": "date",
    "transactiondate": "date",
    "period": "date",
    "bval": "bval",
    "closingvalue": "bval",
    "closingval": "bval",
    "closingamount": "bval",
    "closingstockvalue": "bval",
    "closingvaluation": "bval",
    "clvalue": "bval",
    "clval": "bval",
    "sval": "sval",
    "secondaryvalue": "sval",
    "issuevalue": "sval",
    "issueval": "sval",
    "salesvalue": "sval",
    "salevalue": "sval",
    "saleamount": "sval",
    "salesamount": "sval",
    "secondarysalesvalue": "sval",
    "valueofsales": "sval",
    "openingvalue": "opval",
    "openingval": "opval",
    "opvalue": "opval",
    "opval": "opval",
    "receivevalue": "purval",
    "receiveval": "purval",
    "purchasevalue": "purval",
    "inwardvalue": "purval",
    "qtyopening": "op",
    "quantityopening": "op",
    "stockopening": "op",
    "qtysales": "sale",
    "quantitysales": "sale",
    "qtysale": "sale",
    "qtyclosing": "bal",
    "quantityclosing": "bal",
    "stockclosing": "bal",
    "valueopening": "opval",
    "valuesales": "sval",
    "valuesale": "sval",
    "valueclosing": "bval",
    "scheme": "scheme",
    "free": "free",
    "freeqty": "free",
    "sample": "sample",
    "sampleqty": "sample",
    "expiry": "expiry",
    "expiryclos": "expiry",
    "rate": "rate",
    "secondaryrate": "rate",
    "productcode": "product_code",
    "productid": "product_code",
    "itemcode": "product_code",
    "matcode": "product_code",
    "materialcode": "product_code",
    "sku": "product_code",
    "skucode": "product_code",
    "customername": "customer_name",
    "customer": "customer_name",
    "partyname": "customer_name",
    "distributorname": "customer_name",
    "stockistname": "customer_name",
    "custcode": "cust_code",
    "customercode": "cust_code",
    "customerid": "cust_code",
    "partycode": "cust_code",
    "distributorcode": "cust_code",
    "stockistcode": "cust_code",
    "stockist": "cust_code",
    "year": "year",
    "month": "month",
    "mo": "month",
    "mon": "month",
    "div": "div",
    "division": "div",
    "businessdivision": "div",
    "ptr": "rate",
    "ptrwithgst": "rate",
    "salesrate": "rate",
    "unitrate": "rate",
    "price": "rate",
    "others": "others",
    "#others": "others",
}

_POD_HOSPITAL_SALES_HEADERS = {
    "invoice number",
    "invoice date",
    "card code",
    "card name",
    "item code",
    "item description",
    "quantity",
    "total",
}
_POD_STOCK_HEADERS = {
    "item_code",
    "item_name",
    "manufacturer",
    "opening",
    "purchases",
    "sales",
    "closing",
}


def _normalized_sheet_headers(row: Tuple[Any, ...]) -> set:
    return {
        re.sub(r"\s+", " ", str(value or "").strip().lower())
        for value in row
        if value is not None and str(value).strip()
    }


def _parse_pod_hospital_sales_xlsx(
    workbook: Any, filename: str
) -> Optional[Dict[str, Any]]:
    """Parse the two-sheet POD hospital-sales workbook without affecting generic XLSX."""
    def pod_date(value: Any) -> str:
        """Preserve Excel date cells; avoid treating YYYY-MM-DD as DD-MM-YY."""
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d")
        text = str(value or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:\s+00:00:00)?", text):
            return text[:10]
        return _normalize_date(text) or text

    hospital_sheet = next(
        (sheet for sheet in workbook.worksheets
         if _POD_HOSPITAL_SALES_HEADERS.issubset(
             _normalized_sheet_headers(next(sheet.iter_rows(
                 min_row=1, max_row=1, values_only=True), ()))
         )),
        None,
    )
    stock_sheet = next(
        (sheet for sheet in workbook.worksheets
         if _POD_STOCK_HEADERS.issubset(
             _normalized_sheet_headers(next(sheet.iter_rows(
                 min_row=1, max_row=1, values_only=True), ()))
         )),
        None,
    )
    if hospital_sheet is None or stock_sheet is None:
        return None

    result = empty_result(filename, "xlsx")
    result["report_title"] = "POD Hospital Wise Sales"
    result["company_name"] = "ZYDUS"
    result["totals"]["extra"]["extraction_method"] = "pod_hospital_wise_sales_xlsx"
    result["totals"]["extra"]["hospital_sales_worksheet"] = hospital_sheet.title
    result["totals"]["extra"]["stock_statement_worksheet"] = stock_sheet.title

    hospital_headers = [
        re.sub(r"\s+", " ", str(value or "").strip().lower())
        for value in next(hospital_sheet.iter_rows(
            min_row=1, max_row=1, values_only=True
        ))
    ]
    hospital_indices = {
        header: index for index, header in enumerate(hospital_headers) if header
    }

    def hospital_cell(row: Tuple[Any, ...], header: str) -> Any:
        index = hospital_indices.get(header)
        return row[index] if index is not None and index < len(row) else None

    items: List[Dict[str, Any]] = []
    hospital_summary: Dict[str, Dict[str, Any]] = {}
    invoice_dates: List[datetime] = []
    invoice_numbers: set = set()
    manufacturers: set = set()

    for row in hospital_sheet.iter_rows(min_row=2, values_only=True):
        invoice_no = str(hospital_cell(row, "invoice number") or "").strip()
        hospital_name = _clean_name(str(hospital_cell(row, "card name") or ""))
        product_name = _clean_name(str(hospital_cell(row, "item description") or ""))
        if not invoice_no or not hospital_name or not product_name:
            continue

        invoice_date = hospital_cell(row, "invoice date")
        if isinstance(invoice_date, datetime):
            invoice_dates.append(invoice_date)
            invoice_date_value = invoice_date.strftime("%Y-%m-%d")
        else:
            invoice_date_value = pod_date(invoice_date)

        quantity = _to_float(hospital_cell(row, "quantity"))
        total = _to_float(hospital_cell(row, "total"))
        manufacturer = _clean_name(
            str(hospital_cell(row, "manufacturer") or "")
        )
        if manufacturer:
            manufacturers.add(manufacturer)

        item = empty_line_item()
        item["product_code"] = str(hospital_cell(row, "item code") or "").strip() or None
        item["product_name"] = product_name
        item["sales_qty"] = quantity
        item["sales_value"] = total
        item["extra"] = {
            "invoice_number": invoice_no,
            "invoice_date": invoice_date_value,
            "hospital_code": str(hospital_cell(row, "card code") or "").strip(),
            "hospital_name": hospital_name,
            "hsn": str(hospital_cell(row, "hsn") or "").strip(),
            "manufacturer": manufacturer,
            "batch": str(hospital_cell(row, "batch") or "").strip(),
            "expiry": pod_date(hospital_cell(row, "expiry")),
            "tax_rate": _to_float(hospital_cell(row, "tax rate")),
            "mrp": _to_float(hospital_cell(row, "mrp")),
        }
        items.append(item)
        invoice_numbers.add(invoice_no)

        summary = hospital_summary.setdefault(
            hospital_name,
            {
                "hospital_code": item["extra"]["hospital_code"],
                "hospital_name": hospital_name,
                "line_count": 0,
                "invoice_numbers": set(),
                "sales_qty": 0.0,
                "sales_value": 0.0,
            },
        )
        summary["line_count"] += 1
        summary["invoice_numbers"].add(invoice_no)
        summary["sales_qty"] += quantity
        summary["sales_value"] += total

    if not items:
        return None

    stock_headers = [
        str(value or "").strip().lower()
        for value in next(stock_sheet.iter_rows(
            min_row=1, max_row=1, values_only=True
        ))
    ]
    stock_indices = {
        header: index for index, header in enumerate(stock_headers) if header
    }

    def stock_cell(row: Tuple[Any, ...], header: str) -> Any:
        index = stock_indices.get(header)
        return row[index] if index is not None and index < len(row) else None

    stock_statement: List[Dict[str, Any]] = []
    for row in stock_sheet.iter_rows(min_row=2, values_only=True):
        item_name = _clean_name(str(stock_cell(row, "item_name") or ""))
        item_code = str(stock_cell(row, "item_code") or "").strip()
        if not item_name and not item_code:
            continue
        stock_statement.append({
            "item_code": item_code,
            "item_name": item_name,
            "manufacturer": _clean_name(str(stock_cell(row, "manufacturer") or "")),
            "packing_factor": _to_float(stock_cell(row, "packing factor")),
            "uom": str(stock_cell(row, "uom") or "").strip(),
            "opening_qty": _to_float(stock_cell(row, "opening")),
            "purchases_qty": _to_float(stock_cell(row, "purchases")),
            "sales_qty": _to_float(stock_cell(row, "sales")),
            "closing_qty": _to_float(stock_cell(row, "closing")),
        })

    hospital_sales_summary = []
    for summary in hospital_summary.values():
        hospital_sales_summary.append({
            "hospital_code": summary["hospital_code"],
            "hospital_name": summary["hospital_name"],
            "line_count": summary["line_count"],
            "invoice_count": len(summary["invoice_numbers"]),
            "sales_qty": round(summary["sales_qty"], 2),
            "sales_value": round(summary["sales_value"], 2),
        })

    result["line_items"] = items
    result["totals"]["sales_value"] = round(
        sum(_to_float(item["sales_value"]) for item in items), 2
    )
    result["totals"]["extra"].update({
        "hospital_count": len(hospital_summary),
        "invoice_count": len(invoice_numbers),
        "hospital_sales_line_count": len(items),
        "hospital_sales_summary": hospital_sales_summary,
        "stock_statement": stock_statement,
        "stock_statement_line_count": len(stock_statement),
        "stock_opening_qty": round(
            sum(_to_float(item["opening_qty"]) for item in stock_statement), 2
        ),
        "stock_purchases_qty": round(
            sum(_to_float(item["purchases_qty"]) for item in stock_statement), 2
        ),
        "stock_sales_qty": round(
            sum(_to_float(item["sales_qty"]) for item in stock_statement), 2
        ),
        "stock_closing_qty": round(
            sum(_to_float(item["closing_qty"]) for item in stock_statement), 2
        ),
    })
    result["stockist_name"] = "POD Hospital Wise Sales"
    if len(manufacturers) == 1:
        result["company_name"] = next(iter(manufacturers))
    if invoice_dates:
        result["period_from"] = min(invoice_dates).strftime("%Y-%m-%d")
        result["period_to"] = max(invoice_dates).strftime("%Y-%m-%d")
    return result


def _xls_norm_header(label: Any) -> str:
    text = str(label or "").replace("\u00a0", " ").replace("\r", " ").replace("\n", " ")
    compact = re.sub(r"[\s_]+", "", text.strip().lower())
    if compact in _XLS_HEADER_ALIASES:
        return _XLS_HEADER_ALIASES[compact]
    compact = compact.replace("quantity", "qty")
    compact = compact.replace("material", "mat")
    compact = compact.replace("description", "desc")
    if compact in _XLS_HEADER_ALIASES:
        return _XLS_HEADER_ALIASES[compact]
    alnum = re.sub(r"[^a-z0-9#]+", "", compact)
    return _XLS_HEADER_ALIASES.get(alnum, compact)


_XLS_HEADER_SCORE = {
    "item": 4,
    "product_code": 3,
    "op": 2,
    "pur": 2,
    "sale": 2,
    "bal": 2,
    "customer_name": 2,
    "cust_code": 2,
    "rate": 1,
    "sval": 1,
    "bval": 1,
    "opval": 1,
    "purval": 1,
    "pack": 1,
    "div": 1,
}

# Generic group labels used only to join a parent header with the row beneath it.
_XLS_GENERIC_HEADER_PARENTS = {
    "stock",
    "stocks",
    "quantity",
    "qty",
    "value",
    "values",
    "amount",
    "amounts",
}

# Weaker names stay only when a more specific column for the same field is absent.
_XLS_WEAK_LABELS = {
    "sale": {"issue", "issueqty"},
    "pur": {"receive", "receiveqty", "inward", "inwardqty"},
}


def _xls_score_colmap(colmap: Dict[str, int]) -> int:
    return sum(_XLS_HEADER_SCORE.get(key, 0) for key in colmap)


def _xls_label_compact(label: Any) -> str:
    text = str(label or "").replace("\u00a0", " ").replace("\r", " ").replace("\n", " ")
    compact = re.sub(r"[\s_]+", "", text.strip().lower())
    compact = compact.replace("quantity", "qty").replace("material", "mat")
    return re.sub(r"[^a-z0-9#]+", "", compact)


def _xls_parent_token(label: Any) -> str:
    text = str(label or "").replace("\n", " ").replace("\r", " ")
    return re.sub(r"[^a-z]", "", text.strip().lower())


def _xls_combine_header_pair(upper: List[Any], lower: List[Any]) -> List[Any]:
    """Join a group header such as Quantity with Opening / Sales / Closing."""
    width = max(len(upper), len(lower))
    combined: List[Any] = []
    for i in range(width):
        up = upper[i] if i < len(upper) else None
        low = lower[i] if i < len(lower) else None
        up_s = str(up).strip() if up not in (None, "") else ""
        low_s = str(low).strip() if low not in (None, "") else ""
        if low_s and up_s and _xls_parent_token(up_s) in _XLS_GENERIC_HEADER_PARENTS:
            combined.append(f"{up_s} {low_s}")
        elif low_s:
            combined.append(low)
        else:
            combined.append(up)
    return combined


def _xls_best_header(
    rows: List[List[Any]], scan_rows: int = 40
) -> Tuple[Optional[int], Dict[str, int], int]:
    """Score the first scan_rows for a stock table header. Ignore titles."""
    best_idx: Optional[int] = None
    best_map: Dict[str, int] = {}
    best_score = 0
    limit = rows[:scan_rows]
    for ri, row in enumerate(limit):
        if not isinstance(row, list):
            continue
        candidates = [row]
        if ri > 0 and isinstance(limit[ri - 1], list):
            candidates.append(_xls_combine_header_pair(limit[ri - 1], row))
        for candidate in candidates:
            colmap = _xls_header_colmap(candidate)
            if not _xls_is_stock_header(colmap):
                continue
            score = _xls_score_colmap(colmap)
            if score > best_score:
                best_idx, best_map, best_score = ri, colmap, score
    return best_idx, best_map, best_score


def _xls_header_colmap(row: List[Any]) -> Dict[str, int]:
    colmap: Dict[str, int] = {}
    weak: Dict[str, bool] = {}
    for ci, cell in enumerate(row):
        if cell is None or not str(cell).strip() or str(cell).strip() in {"-", "—"}:
            continue
        field = _xls_norm_header(cell)
        if not field:
            continue
        is_weak = _xls_label_compact(cell) in _XLS_WEAK_LABELS.get(field, ())
        if field not in colmap:
            colmap[field] = ci
            weak[field] = is_weak
        elif weak.get(field) and not is_weak:
            colmap[field] = ci
            weak[field] = False
    return colmap


def _xls_is_stock_header(colmap: Dict[str, int]) -> bool:
    has_name = "item" in colmap
    has_qty = bool({"op", "sale", "bal", "pur"} & set(colmap))
    has_val = bool({"sval", "bval", "opval"} & set(colmap))
    has_mat = has_name and "product_code" in colmap
    return (has_name and (has_qty or has_val)) or has_mat


def _xls_cell_code(value: Any) -> Optional[str]:
    """Preserve text codes (including leading zeroes). Do not use names as codes."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if not text or text in {"-", "—"}:
        return None
    return text


def _xls_cell_has_qty(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    text = str(value).strip().replace("\u00a0", " ").replace(",", "").replace("₹", "")
    if text.upper() in _EMPTY_NUMERIC:
        return False
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    text = re.sub(r"\s+", "", text)
    return bool(re.fullmatch(r"-?\d+(?:\.\d+)?", text))


def _xls_col_letter(idx: int) -> str:
    n = idx + 1
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _xls_expand_merged(sh: Any, rows: List[List[Any]]) -> None:
    """Copy merged-cell values into empty covered cells. Does not run macros."""
    merged = getattr(sh, "merged_cells", None)
    if merged is None:
        return
    ranges = getattr(merged, "ranges", merged)
    for rng in ranges:
        bounds = getattr(rng, "bounds", None)
        if not bounds:
            continue
        min_col, min_row, max_col, max_row = bounds
        if min_row - 1 >= len(rows) or min_col - 1 >= len(rows[min_row - 1]):
            continue
        value = rows[min_row - 1][min_col - 1]
        if value in (None, ""):
            continue
        for r in range(min_row - 1, min(max_row, len(rows))):
            while len(rows[r]) < max_col:
                rows[r].append(None)
            for c in range(min_col - 1, max_col):
                if rows[r][c] in (None, ""):
                    rows[r][c] = value


def _xls_expand_xlrd_merged(sh: Any, rows: List[List[Any]]) -> None:
    """Copy xlrd merged ranges. Bounds are half-open: (rlo, rhi, clo, chi)."""
    for rlo, rhi, clo, chi in getattr(sh, "merged_cells", []) or []:
        if rlo >= len(rows) or clo >= len(rows[rlo]):
            continue
        value = rows[rlo][clo]
        if value in (None, ""):
            continue
        for r in range(rlo, min(rhi, len(rows))):
            while len(rows[r]) < chi:
                rows[r].append(None)
            for c in range(clo, chi):
                if rows[r][c] in (None, ""):
                    rows[r][c] = value


def _xls_iter_sheets(
    file_bytes: bytes, ext: str
) -> List[Tuple[str, List[List[Any]], List[List[Optional[str]]], bool]]:
    sheets: List[Tuple[str, List[List[Any]], List[List[Optional[str]]], bool]] = []
    if ext == ".xls" or (
        file_bytes[:8].startswith(b"\xd0\xcf\x11\xe0") and ext not in {".xlsx", ".xlsm"}
    ):
        import xlrd

        wb = xlrd.open_workbook(file_contents=file_bytes)
        for idx in range(wb.nsheets):
            sh = wb.sheet_by_index(idx)
            rows: List[List[Any]] = []
            formats: List[List[Optional[str]]] = []
            for r in range(sh.nrows):
                vals: List[Any] = []
                fmts: List[Optional[str]] = []
                for c in range(sh.ncols):
                    cell = sh.cell(r, c)
                    vals.append(cell.value)
                    fmts.append(
                        "YYYY-MM-DD" if cell.ctype == xlrd.XL_CELL_DATE else None
                    )
                rows.append(vals)
                formats.append(fmts)
            _xls_expand_xlrd_merged(sh, rows)
            hidden = bool(getattr(sh, "visibility", 0))
            sheets.append((sh.name or f"Sheet{idx + 1}", rows, formats, hidden))
        return sheets

    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=False)
    formulas = load_workbook(io.BytesIO(file_bytes), data_only=False, read_only=False)
    try:
        for sh, sh_formula in zip(wb.worksheets, formulas.worksheets):
            rows = []
            formats = []
            for row, formula_row in zip(sh.iter_rows(), sh_formula.iter_rows()):
                vals = []
                for cell, formula_cell in zip(row, formula_row):
                    value = cell.value
                    formula = formula_cell.value
                    if value is None and isinstance(formula, str) and formula.startswith("="):
                        value = formula
                    vals.append(value)
                rows.append(vals)
                formats.append([cell.number_format for cell in row])
            _xls_expand_merged(sh, rows)
            hidden = str(getattr(sh, "sheet_state", "visible") or "visible") != "visible"
            sheets.append((sh.title or "Sheet1", rows, formats, hidden))
    finally:
        wb.close()
        formulas.close()
    return sheets


def _xls_finalize_result(result: Dict[str, Any]) -> Dict[str, Any]:
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    if extra.get("stockist_code") and not result.get("stockist_code"):
        result["stockist_code"] = extra["stockist_code"]
    if extra.get("division") and not result.get("division"):
        result["division"] = extra["division"]
    # Do not invent statement_date. Copy only a real extracted period date.
    if not result.get("statement_date"):
        result["statement_date"] = (
            _usable_period_date(result.get("period_from"))
            or _usable_period_date(result.get("period_to"))
        )
    extra.setdefault(
        "extraction_metadata",
        {
            "source_type": result.get("source_format"),
            "date_found": bool(
                result.get("statement_date")
                or result.get("period_from")
                or result.get("period_to")
            ),
            "stockist_found": bool(result.get("stockist_name")),
        },
    )
    return result


def _parse_xls(file_bytes: bytes, filename: str, ext: str) -> Dict[str, Any]:
    if ext != ".xls":
        try:
            from openpyxl import load_workbook

            pod_wb = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
            try:
                pod_result = _parse_pod_hospital_sales_xlsx(pod_wb, filename)
            finally:
                pod_wb.close()
            if pod_result is not None:
                return pod_result
        except Exception as exc:
            if file_bytes[:8].startswith(b"\xd0\xcf\x11\xe0"):
                logger.info("OOXML reader failed on OLE workbook; using .xls parser: %s", exc)
                ext = ".xls"
            else:
                raise

    parts: List[Dict[str, Any]] = []
    last_error: Optional[str] = None
    for sheet_name, rows, formats, hidden in _xls_iter_sheets(file_bytes, ext):
        try:
            part = empty_result(filename, ext.lstrip("."))
            part = _xls_fill_from_rows(part, rows, formats, sheet_name)
            part.setdefault("totals", {}).setdefault("extra", {})["sheet_hidden"] = hidden
        except Exception as exc:
            last_error = f"{sheet_name}: {exc}"
            logger.warning("Excel sheet %s skipped: %s", sheet_name, exc)
            continue
        count = len(part.get("line_items") or [])
        extra = (part.get("totals") or {}).get("extra") or {}
        logger.info(
            "Excel sheet %s parser=semantic_xls items=%s header_row=%s fields=%s hidden=%s",
            sheet_name,
            count,
            extra.get("header_row"),
            extra.get("extraction_metadata", {}).get("detected_fields"),
            hidden,
        )
        if part.get("line_items"):
            parts.append(part)
    visible_parts = [
        part
        for part in parts
        if not ((part.get("totals") or {}).get("extra") or {}).get("sheet_hidden")
    ]
    if visible_parts:
        parts = visible_parts

    def _product_names(part: Dict[str, Any]) -> set:
        return {
            item.get("product_name")
            for item in (part.get("line_items") or [])
            if isinstance(item, dict) and item.get("product_name")
        }

    parts = sorted(parts, key=lambda part: len(part.get("line_items") or []), reverse=True)
    detailed: List[Dict[str, Any]] = []
    for part in parts:
        names = _product_names(part)
        if any(names and names <= _product_names(other) for other in detailed):
            continue
        detailed.append(part)
    parts = detailed
    if not parts:
        result = empty_result(filename, ext.lstrip("."))
        if last_error:
            result["totals"]["extra"]["extraction_error"] = last_error
        return _xls_finalize_result(result)
    result = parts[0]
    seen = set()
    merged: List[Dict[str, Any]] = []
    for part in parts:
        if not result.get("stockist_name") and part.get("stockist_name"):
            result["stockist_name"] = part["stockist_name"]
        if not result.get("period_from") and part.get("period_from"):
            result["period_from"] = part["period_from"]
            result["period_to"] = part.get("period_to")
        pextra = (part.get("totals") or {}).get("extra") or {}
        rextra = result.setdefault("totals", {}).setdefault("extra", {})
        for key in ("stockist_code", "division", "year", "month"):
            if pextra.get(key) and not rextra.get(key):
                rextra[key] = pextra[key]
        for item in part.get("line_items") or []:
            key = (
                item.get("product_code"),
                item.get("product_name"),
                item.get("opening_qty"),
                item.get("sales_qty"),
                item.get("closing_qty"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    result["line_items"] = merged
    if len(parts) > 1:
        result["totals"]["extra"]["sheets_extracted"] = [
            ((p.get("totals") or {}).get("extra") or {}).get("sheet")
            for p in parts
        ]
    return _xls_finalize_result(result)


def _xls_preamble_texts(row: List[Any]) -> List[str]:
    """Non-empty cells, collapsing merge-expanded repeats (same value across columns)."""
    texts = [str(c).strip() for c in row if c is not None and str(c).strip()]
    if len(texts) > 1 and len(set(texts)) == 1:
        return [texts[0]]
    return texts


def _xls_fill_from_rows(
    result: Dict[str, Any],
    rows: List[List[Any]],
    formats: List[List[Optional[str]]],
    sheet_name: str = "Sheet1",
) -> Dict[str, Any]:

    # Metadata scan
    for row in rows[:20]:
        texts = _xls_preamble_texts(row)
        joined = " ".join(texts)
        if not texts:
            continue
        if (
            not result["stockist_name"]
            and not re.search(r"@|E-?Mail|Phone\s*:", texts[0], re.I)
            and re.search(
                r"MEDICAL|STORES|AGENC|PHARMA|MEDICO|DISTRIBUT",
                texts[0],
                re.I,
            )
            and not re.search(r"STOCK\s*(?:&|AND)\s*SALES", texts[0], re.I)
        ):
            result["stockist_name"] = _clean_name(texts[0])
        if (
            re.search(r"NAGAR|ROAD|HOUSE|COMPLEX|STREET|TEMPLE|NEAR|APT", joined, re.I)
            and len(joined) > 20
            and not re.search(r"E-?Mail|Phone\s*:", joined, re.I)
        ):
            if not result["stockist_address"]:
                result["stockist_address"] = _clean_name(joined)
        if re.search(r"Stock and Sale|Stock & Sale|Sales Report", joined, re.I):
            result["report_title"] = _clean_name(joined)
            if not result.get("company_name"):
                brand = re.search(
                    r"[-–]?\s*([A-Za-z][A-Za-z0-9 .&']{1,40}?)\s+STOCK\s*(?:&|AND)\s*SALES",
                    joined,
                    re.I,
                )
                if brand:
                    result["company_name"] = _clean_name(brand.group(1).lstrip("-").strip())
            m = re.search(
                r"From\s*date\s*(\d{1,2}[- ][A-Za-z]{3,9}[- ]\d{2,4})\s*to\s*"
                r"(\d{1,2}[- ][A-Za-z]{3,9}[- ]\d{2,4})",
                joined,
                re.I,
            )
            if m:
                result["period_from"] = _normalize_date(m.group(1))
                result["period_to"] = _normalize_date(m.group(2))
        if re.search(r"Manufacturer", joined, re.I):
            # e.g. Manufacturer 000255 AUROBINDO
            parts = [t for t in texts if not re.fullmatch(r"\d+", t)]
            for i, t in enumerate(texts):
                if re.search(r"Manufacturer", t, re.I) and i + 1 < len(texts):
                    # skip code-like next token
                    cand = texts[i + 1]
                    if re.fullmatch(r"\d+", str(cand)) and i + 2 < len(texts):
                        result["company_name"] = _clean_name(str(texts[i + 2]))
                    else:
                        result["company_name"] = _clean_name(str(cand))

    _apply_excel_header_period(result, rows, formats)

    # Score title/blank rows away; do not assume row 1 is the header.
    header_idx, colmap, header_score = _xls_best_header(rows)
    # Himalaya ZL dump: Opening_bal_qty / Primary_qty / Closing_bal_qty / Secondaryrate.
    # No sales or amount column. Flag only this header so other layouts stay unchanged.
    zl_bal_layout = False
    if header_idx is not None and header_idx < len(rows):
        _zl_compacts = {
            re.sub(r"[\s_]+", "", str(cell or "").strip().lower())
            for cell in rows[header_idx]
            if cell is not None and str(cell).strip()
        }
        zl_bal_layout = (
            "openingbalqty" in _zl_compacts
            and "closingbalqty" in _zl_compacts
            and "primaryqty" in _zl_compacts
            and "secondaryrate" in _zl_compacts
            and "sale" not in colmap
        )
    # Stock & sales sheet: each qty column has a printed value beside it.
    # Quantity already comes from opening/closing. Do not remap issue/receive qty.
    paired_value_layout = (
        header_idx is not None
        and {"openingvalue", "closingvalue", "issuevalue", "receivevalue", "op", "bal"}
        <= set(colmap)
        and "sale" not in colmap
        and "bval" not in colmap
    )
    if header_idx is None:
        extra = result.setdefault("totals", {}).setdefault("extra", {})
        extra["sheet"] = sheet_name
        extra["header_row"] = None
        extra["header_detection_confidence"] = 0.0
        extra["extraction_metadata"] = {
            "source_type": result.get("source_format"),
            "sheet": sheet_name,
            "header_row": None,
            "header_detection_confidence": 0.0,
            "date_found": bool(result.get("period_from") or result.get("period_to")),
            "stockist_found": bool(result.get("stockist_name")),
            "detected_fields": [],
        }
        return result

    def _col(*names: str) -> Optional[int]:
        for n in names:
            k = _xls_norm_header(n)
            if k in colmap:
                return colmap[k]
        return None

    if header_idx and not result.get("stockist_name"):
        for preamble in rows[:header_idx]:
            if not isinstance(preamble, list):
                continue
            texts = _xls_preamble_texts(preamble)
            if len(texts) != 1:
                continue
            name = texts[0]
            if re.search(
                r"@|\d|STOCK|SALES|STATEMENT|PRODUCT|ITEM|OPENING|CLOSING|"
                r"REPORT|PHONE|E-?MAIL|D\.?L|PAGE|FROM|DATE|ADDRESS",
                name,
                re.I,
            ):
                continue
            if 4 <= len(name) <= 60:
                result["stockist_name"] = _clean_name(name)
                break

    items: List[Dict[str, Any]] = []
    start = (header_idx + 1) if header_idx is not None else 0
    for row in rows[start:]:
        texts = [(ci, c) for ci, c in enumerate(row) if c is not None and str(c).strip() != ""]
        if not texts:
            continue
        joined = " ".join(str(c) for _, c in texts)
        if re.search(
            r"For Manufacturer|Opening Val|Closing Val|(?<!\()\bSales\s*:|Printed Using|Report Date|Page \d",
            joined,
            re.I,
        ):
            if re.search(r"Opening Val", joined, re.I):
                for i, (_ci, c) in enumerate(texts):
                    if re.search(r"Opening Val", str(c), re.I) and i + 1 < len(texts):
                        result["totals"]["extra"]["opening_value"] = _to_float(texts[i + 1][1])
                    if re.search(r"Purchase", str(c), re.I) and i + 1 < len(texts):
                        result["totals"]["extra"]["purchase_value"] = _to_float(texts[i + 1][1])
            if re.search(r"Closing Val", joined, re.I):
                for i, (_ci, c) in enumerate(texts):
                    if re.search(r"Closing Val", str(c), re.I) and i + 1 < len(texts):
                        result["totals"]["closing_value"] = _to_float(texts[i + 1][1])
            if re.search(r"\bSales\s*:", joined, re.I):
                for i, (_ci, c) in enumerate(texts):
                    label = str(c).strip()
                    # Prefer primary "Sales :" over "Sales (May):" / "Sales (Apr):"
                    if re.fullmatch(r"Sales\s*:", label, re.I) and i + 1 < len(texts):
                        result["totals"]["sales_value"] = _to_float(texts[i + 1][1])
                        break
            continue
        if re.search(
            r"Manufacturer|Item\b|Pack\b|Item Name|Op\.?\s*Stock|Cl\s*Stock|"
            r"Mat Name|Mat Code|Product Name|Secondary Qty",
            joined,
            re.I,
        ) and header_idx is not None:
            continue
        if re.search(
            r"^(VALUE OF|Company\s*:|Desc\s*:|From\s*:|To\s*:|Report Generated|Prepared By|Page\s+\d)",
            joined,
            re.I,
        ):
            if re.search(r"^VALUE OF SALE\b", joined, re.I) and not re.search(
                r"LAST|FREE", joined, re.I
            ):
                nums = [_to_float(c) for _, c in texts if _xls_cell_has_qty(c)]
                if nums:
                    result["totals"]["sales_value"] = nums[0]
                    result["totals"]["extra"]["value_of_sale"] = nums[0]
            elif re.search(r"^VALUE OF CLOSING STOCK\b", joined, re.I):
                nums = [_to_float(c) for _, c in texts if _xls_cell_has_qty(c)]
                if nums:
                    result["totals"]["closing_value"] = nums[0]
                    result["totals"]["extra"]["value_of_closing_stock"] = nums[0]
            continue

        # Product row: Item / Item Name / Mat Name (never Customer Name or Mat Code)
        name_idx = _col("item", "item name", "mat name", "product name", "description")
        code_idx = _col("product_code", "mat code", "item code")
        product_name = None
        if name_idx is not None and name_idx < len(row) and row[name_idx] not in (None, ""):
            product_name = str(row[name_idx]).strip()
        elif code_idx is None:
            # Legacy fallback only when no Mat Name / Item column exists
            for ci, c in texts:
                s = str(c).strip()
                if len(s) > 3 and not re.fullmatch(r"-?\d+(?:\.\d+)?", s):
                    if not re.search(r"Manufacturer|AUROBINDO|Pack|Item", s, re.I):
                        product_name = s
                        break
        if not product_name:
            continue
        if _xls_norm_header(product_name) in {"item", "product_code", "op", "sale", "bal", "pur"}:
            continue
        if _xls_is_stock_header(_xls_header_colmap(row)):
            continue
        if re.search(
            r"^(Item|Item Name|Mat Name|Mat Code|Pack|Manufacturer|Sales\s*:|"
            r"Opening|Closing|Product Name|Product|Description|Customer Name|"
            r"Year|Month|Div)$",
            product_name,
            re.I,
        ):
            continue
        if re.search(r"^(Sales|Opening Val|Closing Val|Purchase|Credit|Branch|Adj)\b", product_name, re.I):
            continue
        if re.search(
            r"^(Total Of|TOTAL\b|Grand Total|Sub\s*Total|Net\s*Total|SUMMARY\b|"
            r"Opening Stock|Closing Stock|Report Generated|Prepared By)\b",
            product_name,
            re.I,
        ):
            if re.match(r"^(?:grand\s+|net\s+)?total\b", product_name.strip(), re.I):
                sv = _col("sval", "secondary value")
                bv = _col("bval")
                if sv is not None and sv < len(row) and _xls_cell_has_qty(row[sv]):
                    result["totals"]["sales_value"] = _to_float(row[sv])
                if bv is not None and bv < len(row) and _xls_cell_has_qty(row[bv]):
                    result["totals"]["closing_value"] = _to_float(row[bv])
            continue
        if header_idx is not None:
            qty_cells = [
                row[idx]
                for idx in (
                    _col("op.", "op", "op. stock", "ob (qty)"),
                    _col("pur", "purch", "primary (qty)"),
                    _col("sale", "secondary qty total"),
                    _col("bal.", "bal", "cl stock", "cb (qty)"),
                )
                if idx is not None and idx < len(row)
            ]
            if qty_cells and not any(_xls_cell_has_qty(v) for v in qty_cells):
                continue

        item = empty_line_item()
        item["product_name"] = _clean_name(product_name)
        if code_idx is not None and code_idx < len(row):
            item["product_code"] = _xls_cell_code(row[code_idx])

        cust_i = _col("customer_name", "customer name")
        code_stockist_i = _col("cust_code", "cust code")
        if cust_i is not None and cust_i < len(row) and row[cust_i] not in (None, ""):
            stockist = _clean_name(str(row[cust_i]))
            if stockist and not result.get("stockist_name"):
                result["stockist_name"] = stockist
        if code_stockist_i is not None and code_stockist_i < len(row):
            stockist_code = _xls_cell_code(row[code_stockist_i])
            if stockist_code and not result["totals"]["extra"].get("stockist_code"):
                result["totals"]["extra"]["stockist_code"] = stockist_code

        year_i = _col("year")
        month_i = _col("month", "mo")
        div_i = _col("div", "division")
        if year_i is not None and year_i < len(row) and row[year_i] not in (None, ""):
            result["totals"]["extra"].setdefault("year", _xls_cell_code(row[year_i]))
        if month_i is not None and month_i < len(row) and row[month_i] not in (None, ""):
            result["totals"]["extra"].setdefault("month", _xls_cell_code(row[month_i]))
        if div_i is not None and div_i < len(row) and row[div_i] not in (None, ""):
            result["totals"]["extra"].setdefault("division", _clean_name(str(row[div_i])))
        # Year + Month are period metadata only. Do not invent calendar dates.

        pack_i = _col("pack")
        if pack_i is not None and pack_i < len(row) and row[pack_i]:
            item["packing"] = str(row[pack_i]).strip()

        op_i = _col("op.", "op", "ob (qty)")
        pur_i = _col("pur", "primary (qty)")
        sale_i = _col("sale", "secondary qty total")
        bal_i = _col("bal.", "bal", "cb (qty)")
        bval_i = _col("bval")
        sval_i = _col("sval", "secondary value")
        rate_i = _col("rate", "secondary rate")

        def _assign_number(key: str, idx: Optional[int], into_extra: bool = False) -> None:
            if idx is None or idx >= len(row):
                return
            value = row[idx]
            if isinstance(value, str) and value.strip().startswith("="):
                item["extra"]["unresolved_formula"] = True
                item["extra"]["extraction_warning"] = "formula value was not cached"
                if not into_extra:
                    item[key] = None
                return
            number = _to_float(value)
            if into_extra:
                item["extra"][key] = number
            else:
                item[key] = number

        _assign_number("opening_qty", op_i)
        _assign_number("receipts_qty", pur_i)
        _assign_number("sales_qty", sale_i)
        _assign_number("closing_qty", bal_i)
        _assign_number("closing_value", bval_i)
        _assign_number("sales_value", sval_i)
        _assign_number("opening_value", _col("opval"), into_extra=True)
        _assign_number("receipts_value", _col("purval"), into_extra=True)
        for extra_field, extra_names in (
            ("scheme_qty", ("scheme",)),
            ("free_qty", ("free",)),
            ("sample_qty", ("sample",)),
        ):
            _assign_number(extra_field, _col(*extra_names), into_extra=True)
        if rate_i is not None and rate_i < len(row) and _xls_cell_has_qty(row[rate_i]):
            item["extra"]["unit_rate"] = _to_float(row[rate_i])
            rate = item["extra"]["unit_rate"]
            if rate and not item.get("sales_value") and item.get("sales_qty") is not None:
                item["sales_value"] = round(item["sales_qty"] * rate, 2)
            if rate and not item.get("closing_value") and item.get("closing_qty") is not None:
                item["closing_value"] = round(item["closing_qty"] * rate, 2)
        if zl_bal_layout:
            item["extra"]["layout"] = "zl_opening_primary_closing"
            mrp_i = _col("mrp")
            if mrp_i is not None and mrp_i < len(row) and _xls_cell_has_qty(row[mrp_i]):
                item["extra"]["mrp"] = _to_float(row[mrp_i])
        if paired_value_layout:
            item["extra"]["layout"] = "paired_stock_value"

            def _paired_amount(field: str) -> Optional[float]:
                idx = colmap.get(field)
                if idx is None or idx >= len(row) or not _xls_cell_has_qty(row[idx]):
                    return None
                return _to_float(row[idx])

            opening_value = _paired_amount("openingvalue")
            receive_value = _paired_amount("receivevalue")
            issue_value = _paired_amount("issuevalue")
            closing_value = _paired_amount("closingvalue")
            if opening_value is not None:
                item["extra"]["opening_value"] = opening_value
            if receive_value is not None:
                item["extra"]["purchase_value"] = receive_value
            if issue_value is not None:
                item["extra"]["issue_value"] = issue_value
            if closing_value is not None:
                item["closing_value"] = closing_value
        date_i = _col("date")
        if (
            date_i is not None
            and date_i < len(row)
            and not result.get("period_from")
            and not result.get("period_to")
        ):
            fmt = None
            # formats align with original row index when available
            parsed = _normalize_excel_date(row[date_i], fmt)
            if parsed:
                result["period_from"] = parsed
                result["period_to"] = parsed
        others_i = _col("others", "#others")
        if others_i is not None and others_i < len(row) and _xls_cell_has_qty(row[others_i]):
            item["extra"]["others_qty"] = _to_float(row[others_i])

        # May/Apr history columns if present
        for hist in ("may", "apr"):
            hi = _col(hist)
            if hi is not None and hi < len(row) and row[hi] not in (None, ""):
                item["extra"][f"hist_{hist}"] = _to_float(row[hi])

        items.append(item)

    result["line_items"] = items
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["sheet"] = sheet_name
    extra["header_row"] = header_idx
    extra["header_detection_confidence"] = (
        round(min(1.0, header_score / 12.0), 2) if header_idx is not None else 0.0
    )
    if "item" in colmap:
        extra["product_column"] = _xls_col_letter(colmap["item"])
        if header_idx is not None and colmap["item"] < len(rows[header_idx]):
            extra["product_column_header"] = str(
                rows[header_idx][colmap["item"]] or ""
            ).strip()
    role_fields = {
        "product": "item",
        "product_code": "product_code",
        "opening_qty": "op",
        "receipts_qty": "pur",
        "sales_qty": "sale",
        "closing_qty": "bal",
        "sales_value": "sval",
        "closing_value": "bval",
        "opening_value": "opval",
        "receipts_value": "purval",
        "rate": "rate",
    }
    column_mapping = {}
    for role, field in role_fields.items():
        idx = colmap.get(field)
        header = None
        if (
            idx is not None
            and header_idx is not None
            and idx < len(rows[header_idx])
        ):
            header = re.sub(r"\s+", " ", str(rows[header_idx][idx] or "")).strip() or None
        column_mapping[role] = {
            "column": _xls_col_letter(idx) if idx is not None else None,
            "header": header,
            "confidence": 0.9 if idx is not None else 0.0,
        }
    if "sale" not in colmap and items:
        extra["extraction_warning"] = "sales quantity column was not detected"
    extra["column_mapping"] = column_mapping
    extra["extraction_metadata"] = {
        "source_type": result.get("source_format"),
        "sheet": sheet_name,
        "header_row": header_idx,
        "header_detection_confidence": extra["header_detection_confidence"],
        "product_column": extra.get("product_column"),
        "product_column_header": extra.get("product_column_header"),
        "column_mapping": column_mapping,
        "date_found": bool(result.get("period_from") or result.get("period_to")),
        "stockist_found": bool(result.get("stockist_name")),
        "detected_fields": sorted(colmap.keys()),
        "extraction_warning": extra.get("extraction_warning"),
    }
    return result


# ---------------------------------------------------------------------------
# PDF (split multi-stockist statements, then extract each)
# ---------------------------------------------------------------------------

_STOCKIST_NAME_HINT = re.compile(
    r"PHARMACEUTICAL|PHARMAC(?:Y|IES)|AGENC|MEDICOSE|MEDICOS|"
    r"MEDICAL\s+(?:STORES|AGENC)|ASSOCIATES|DISTRIBUT|ENTERPRISES|"
    r"TRADERS?|AGENCIES",
    re.I,
)
_STATEMENT_TITLE_HINT = re.compile(
    r"STOCK\s*&\s*SALES|STOCK\s+AND\s+SALES|SALES\s*&\s*STOCK|"
    r"Sales\s*&\s*Stock\s+Statement|STOCK\s+REPORT|Stock\s+and\s+Sale",
    re.I,
)
_CONTINUATION_HINT = re.compile(
    r"^\s*(?:--\s*)?Continued\s+Page|^\s*Page\s*No\.?\s*[2-9]\b",
    re.I,
)
_MANUFACTURER_HINT = re.compile(
    r"\b(?:AUROBINDO|VERITAZ|HEALTHCARE\s+LTD|PHARMA\s+LTD|PHARMA\s+LIMITED|"
    r"LABORATOR)\b",
    re.I,
)


def _normalize_stockist_key(name: str) -> str:
    text = re.sub(r"\s+", " ", (name or "").upper()).strip()
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Drop trailing year/noise like "26-27"
    text = re.sub(r"\b\d{2}-\d{2}\b", "", text).strip()
    return text


def _looks_like_stockist_header(line: str) -> bool:
    s = (line or "").strip()
    if len(s) < 4 or len(s) > 90:
        return False
    if re.search(r"^\d", s):
        return False
    if re.search(
        r"PRODUCT|PACKING|ITEM\s+DESCRIPT|OpBal|OPENING|CLOSING|GSTIN|Phone\s*:|"
        r"E-Mail|Page\s*No|Continued|STOCK\s*&\s*SALES|Sales\s*&\s*Stock",
        s,
        re.I,
    ):
        return False
    if _MANUFACTURER_HINT.search(s) and not _STOCKIST_NAME_HINT.search(s):
        return False
    if _STOCKIST_NAME_HINT.search(s):
        return True
    # ALL-CAPS agency-like short header. Reject OCR noise such as "CN SS OO GO".
    letters = re.sub(r"[^A-Za-z]", "", s)
    words = re.findall(r"[A-Za-z]{4,}", s)
    if (
        letters
        and letters.isupper()
        and len(s.split()) <= 8
        and len(letters) >= 6
        and len(letters) / max(len(s), 1) >= 0.45
        and words
    ):
        return True
    return False


def _clean_stockist_label(name: str) -> str:
    text = _clean_name(name)
    text = re.sub(r"\s+\d{2}-\d{2}\s*$", "", text).strip()
    return text


def _detect_stockist_from_page_text(text: str) -> Optional[str]:
    """Return stockist name if this page starts a (new) statement."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None

    # Continuations of previous stockist's statement
    head = "\n".join(lines[:6])
    if _CONTINUATION_HINT.search(lines[0]) and not _looks_like_stockist_header(lines[0]):
        return None
    if re.search(r"Continued\s+Page", head, re.I) and not _looks_like_stockist_header(
        lines[0]
    ):
        # Still allow if first line is clearly a new stockist
        if not _looks_like_stockist_header(lines[0]):
            return None

    # Prefer first stockist-like line near top
    for ln in lines[:8]:
        if _looks_like_stockist_header(ln):
            # Require statement context somewhere on page when possible
            if _STATEMENT_TITLE_HINT.search(text) or _STOCKIST_NAME_HINT.search(ln):
                return _clean_stockist_label(ln)
            return _clean_stockist_label(ln)
    return None


def _ocr_pdf_page_text(page, zoom: float = 2.0) -> Tuple[str, bytes]:
    """Render PDF page to PNG and OCR; return (text, png_bytes)."""
    import fitz

    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img_bytes = pix.tobytes("png")
    try:
        text = _ocr_image_to_text(img_bytes)
    except Exception as exc:
        logger.warning("PDF page OCR failed: %s", exc)
        text = ""
    text = _merge_footer_total_into_text(text or "", img_bytes)
    return text or "", img_bytes


def _group_pdf_pages_by_stockist(
    page_infos: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Group page infos into stockist statement segments.

    page_infos items: {page_index, text, image_bytes, stockist}
    """
    groups: List[Dict[str, Any]] = []
    for info in page_infos:
        stockist = info.get("stockist")
        if stockist:
            key = _normalize_stockist_key(stockist)
            # New group unless same stockist continues
            if groups and _normalize_stockist_key(groups[-1]["stockist_name"]) == key:
                groups[-1]["pages"].append(info)
            else:
                groups.append(
                    {
                        "stockist_name": stockist,
                        "stockist_key": key,
                        "pages": [info],
                    }
                )
        else:
            if groups:
                groups[-1]["pages"].append(info)
            else:
                # Orphan page before first header — keep as unknown segment
                groups.append(
                    {
                        "stockist_name": f"Statement_{info['page_index'] + 1}",
                        "stockist_key": f"STATEMENT_{info['page_index'] + 1}",
                        "pages": [info],
                    }
                )
    return groups


_SSA_FIELDS = (
    "product_name",
    "opening_qty",
    "receipts_qty",
    "purchase_return",
    "purchase_others",
    "total_stock",
    "sales_qty",
    "sale_return",
    "sales_others",
    "closing_qty",
    "unit_rate",
)
_SSA_FIELD_TOKENS = (
    {"item", "description"},
    {"opening"},
    {"purchases", "purchase"},
    {"return"},
    {"others", "other"},
    {"total"},
    {"sales", "sale"},
    {"return"},
    {"others", "other"},
    {"closing"},
    {"rate"},
)
_SSA_HEADER_VOCAB = {
    "item",
    "description",
    "opening",
    "stock",
    "purchases",
    "purchase",
    "return",
    "others",
    "other",
    "total",
    "sales",
    "sale",
    "closing",
    "rate",
    "qty",
}


def _ssa_token(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _ssa_word_parts(word: Any) -> Optional[Tuple[float, float, float, float, str]]:
    if isinstance(word, dict):
        text = str(word.get("text") or "").strip()
        if not text:
            return None
        return (
            float(word["x0"]),
            float(word["y0"]),
            float(word["x1"]),
            float(word["y1"]),
            text,
        )
    if not isinstance(word, (list, tuple)) or len(word) < 5:
        return None
    text = str(word[4] or "").strip()
    if not text:
        return None
    return (float(word[0]), float(word[1]), float(word[2]), float(word[3]), text)


def _ssa_cluster_rows(words: List[Any]) -> List[Dict[str, Any]]:
    boxes: List[Tuple[float, float, float, float, str]] = []
    for word in words or []:
        parts = _ssa_word_parts(word)
        if parts:
            boxes.append(parts)
    if not boxes:
        return []
    heights = sorted(b[3] - b[1] for b in boxes)
    tol = max(2.0, heights[len(heights) // 2] * 0.6)
    boxes.sort(key=lambda b: ((b[1] + b[3]) / 2.0, b[0]))
    rows: List[Dict[str, Any]] = []
    for box in boxes:
        cy = (box[1] + box[3]) / 2.0
        if rows and abs(cy - rows[-1]["cy"]) <= tol:
            rows[-1]["words"].append(box)
            n = len(rows[-1]["words"])
            rows[-1]["cy"] = ((rows[-1]["cy"] * (n - 1)) + cy) / n
        else:
            rows.append({"cy": cy, "words": [box]})
    for row in rows:
        row["words"].sort(key=lambda b: b[0])
    return rows


def _ssa_row_tokens(row: Dict[str, Any]) -> List[str]:
    return [_ssa_token(box[4]) for box in row["words"] if _ssa_token(box[4])]


def _ssa_is_header_row(row: Dict[str, Any]) -> bool:
    tokens = _ssa_row_tokens(row)
    if len(tokens) < 3:
        return False
    hits = sum(1 for tok in tokens if tok in _SSA_HEADER_VOCAB)
    return hits >= 3 and hits / len(tokens) >= 0.6


def _ssa_is_column_index_row(row: Dict[str, Any]) -> bool:
    tokens = [box[4] for box in row["words"]]
    nums = []
    for tok in tokens:
        if re.fullmatch(r"\d{1,2}", tok):
            nums.append(int(tok))
        elif _ssa_token(tok) not in {"", "no", "col", "column"}:
            return False
    return len(nums) >= 8 and nums == list(range(nums[0], nums[0] + len(nums)))


def _ssa_header_anchors(rows: List[Dict[str, Any]]) -> Optional[List[Tuple[str, float]]]:
    header_rows = [row for row in rows if _ssa_is_header_row(row)]
    if not header_rows:
        return None
    words: List[Tuple[float, float, float, float, str]] = []
    for row in header_rows:
        words.extend(row["words"])
    words.sort(key=lambda b: b[0])
    anchors: List[Tuple[str, float]] = []
    cursor = 0
    for field, accepted in zip(_SSA_FIELDS, _SSA_FIELD_TOKENS):
        found = None
        while cursor < len(words):
            tok = _ssa_token(words[cursor][4])
            cursor += 1
            if tok in {"stock", "qty", "analysis", "and"}:
                continue
            if tok in accepted:
                found = words[cursor - 1]
                break
        if found is None:
            return None
        x = found[0] if field == "product_name" else (found[0] + found[2]) / 2.0
        anchors.append((field, x))
    return anchors


def _ssa_assign_row(
    row: Dict[str, Any], anchors: List[Tuple[str, float]]
) -> Dict[str, Any]:
    """Put the name/number split on the opening column, not the item label."""
    numeric = [(field, x) for field, x in anchors if field != "product_name"]
    gap = numeric[1][1] - numeric[0][1]
    bounds = [numeric[0][1] - gap / 2.0]
    for idx in range(len(numeric) - 1):
        bounds.append((numeric[idx][1] + numeric[idx + 1][1]) / 2.0)
    fields = ["product_name"] + [field for field, _x in numeric]
    cells: Dict[str, List[str]] = {field: [] for field in fields}
    for box in row["words"]:
        cx = (box[0] + box[2]) / 2.0
        col = 0
        while col < len(bounds) and cx >= bounds[col]:
            col += 1
        cells[fields[col]].append(box[4])
    return cells


def _ssa_skip_product(name: str) -> bool:
    text = _clean_name(name)
    if len(re.sub(r"[^A-Za-z]", "", text)) < 3:
        return True
    if re.search(
        r"stock\s*&\s*sales|stock\s+and\s+sales|himalaya\s+wellness|"
        r"item\s*description|opening\s*stock|closing\s*stock|total\s*stock|"
        r"total\s*quantity|value\s+in\s+rs|continued|page\s*\d|grand\s*total|"
        r"^total\b|column\s*no|formula",
        text,
        re.I,
    ):
        return True
    if "=" in text:
        return True
    return False


def _ssa_numbers_are_column_index(values: List[Optional[float]]) -> bool:
    present = [v for v in values if v is not None]
    if len(present) < 8:
        return False
    return all(abs(v - float(i + 1)) < 0.01 for i, v in enumerate(present))


def _parse_stock_sales_analysis_words(
    pages: List[Dict[str, Any]], filename: str
) -> Optional[Dict[str, Any]]:
    """Map Busy/Himalaya STOCK & SALES ANALYSIS cells by word coordinates.

    Keeps TOTAL STOCK distinct from CLOSING STOCK and does not borrow a blank
    cell from the next product row. Other statement layouts return None.
    """
    items: List[Dict[str, Any]] = []
    blob_parts: List[str] = []
    matched = False
    for page in pages:
        words = page.get("words") or []
        if not words:
            continue
        blob_parts.append(" ".join(str(w[4]) for w in words if len(w) > 4))
        rows = _ssa_cluster_rows(words)
        anchors = _ssa_header_anchors(rows)
        if not anchors:
            continue
        matched = True
        header_bottom = max(
            (row["cy"] for row in rows if _ssa_is_header_row(row)), default=0.0
        )
        pending = ""
        pending_y = None
        line_h = 12.0
        if rows:
            line_h = max(8.0, min(20.0, abs(rows[1]["cy"] - rows[0]["cy"]) if len(rows) > 1 else 12.0))
        for row in rows:
            if row["cy"] <= header_bottom + 1.0:
                continue
            if _ssa_is_header_row(row) or _ssa_is_column_index_row(row):
                pending = ""
                continue
            cells = _ssa_assign_row(row, anchors)
            name = _clean_name(" ".join(cells.get("product_name") or []))
            numeric: Dict[str, Optional[float]] = {}
            has_number = False
            for field, _x in anchors:
                if field == "product_name":
                    continue
                raw_bits = cells.get(field) or []
                if not raw_bits:
                    numeric[field] = None
                    continue
                parsed = _ocr_qty_token(raw_bits[0])
                if parsed is None and re.search(r"\d", raw_bits[0]):
                    parsed = _to_float(raw_bits[0])
                numeric[field] = parsed
                if parsed is not None:
                    has_number = True
            if _ssa_numbers_are_column_index(list(numeric.values())):
                pending = ""
                continue
            if pending and pending_y is not None and row["cy"] - pending_y > line_h * 2.5:
                pending = ""
            if not has_number:
                if name and not _ssa_skip_product(name):
                    pending = _clean_name(f"{pending} {name}")
                    pending_y = row["cy"]
                else:
                    pending = ""
                continue
            if pending:
                name = _clean_name(f"{pending} {name}")
                pending = ""
                pending_y = None
            if _ssa_skip_product(name):
                continue
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = _to_float(numeric.get("opening_qty"))
            item["receipts_qty"] = _to_float(numeric.get("receipts_qty"))
            item["sales_qty"] = _to_float(numeric.get("sales_qty"))
            item["closing_qty"] = _to_float(numeric.get("closing_qty"))
            extra = {}
            for src, dest in (
                ("total_stock", "total_stock"),
                ("purchase_return", "purchase_return"),
                ("purchase_others", "purchase_others"),
                ("sale_return", "sale_return"),
                ("sales_others", "sales_others"),
                ("unit_rate", "unit_rate"),
            ):
                if numeric.get(src) is not None:
                    extra[dest] = numeric[src]
                else:
                    extra[dest] = None
            item["extra"] = extra
            items.append(item)
    if not matched or not items:
        return None
    blob = " ".join(blob_parts)
    if not re.search(r"STOCK\s*&\s*SALES\s*ANALYSIS|STOCK\s+AND\s+SALES\s+ANALYSIS", blob, re.I):
        return None
    result = empty_result(filename, "pdf")
    result["report_title"] = "STOCK & SALES ANALYSIS"
    company = re.search(r"HIMALAYA\s+WELLNESS", blob, re.I)
    if company:
        result["company_name"] = "HIMALAYA WELLNESS"
    result["line_items"] = items
    result["totals"]["extra"]["extraction_method"] = "stock_sales_analysis_geometry"
    return result


def _statement_nonzero_rows(result: Optional[Dict[str, Any]]) -> int:
    if not isinstance(result, dict):
        return 0
    count = 0
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        if any(
            _to_float(item.get(key)) > 0
            for key in (
                "opening_qty",
                "receipts_qty",
                "sales_qty",
                "sales_value",
                "closing_qty",
                "closing_value",
            )
        ):
            count += 1
    return count


def _statement_numbers_are_blank(result: Optional[Dict[str, Any]]) -> bool:
    """True when product rows exist but qty/value columns are almost all zero.

    Scanned stock-and-sale grids often OCR into names with no usable digits.
    Those rows must not block the page-image read.
    """
    if not isinstance(result, dict):
        return False
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    if len(items) < 3:
        return False
    nonzero = 0
    for item in items:
        if any(
            _to_float(item.get(key)) > 0
            for key in (
                "opening_qty",
                "receipts_qty",
                "sales_qty",
                "sales_value",
                "closing_qty",
                "closing_value",
            )
        ):
            nonzero += 1
    return nonzero <= max(1, len(items) // 10)


def _pdf_pages_are_image_only(pages: List[Dict[str, Any]]) -> bool:
    """True when every page is a scan (no embedded PDF words)."""
    if not pages:
        return False
    return all(not (page.get("words") or []) for page in pages)


def _drop_trailing_statement_total_item(
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Drop a footer TOTAL row that vision attached to the last product."""
    if len(items) < 5:
        return items
    last = items[-1]
    name = str(last.get("product_name") or "")
    if re.search(r"^(TOTAL|GRAND\s*TOTAL|SUB\s*TOTAL)\b", name, re.I):
        return items[:-1]
    others = items[:-1]
    last_rcp = _to_float(last.get("receipts_qty"))
    others_rcp = sum(_to_float(i.get("receipts_qty")) for i in others)
    last_op = _to_float(last.get("opening_qty"))
    others_op = sum(_to_float(i.get("opening_qty")) for i in others)
    if last_rcp >= 200 and others_rcp > 0 and last_rcp >= others_rcp * 0.8:
        return others
    if last_op >= 200 and others_op > 0 and last_op >= others_op * 0.8:
        return others
    return items


def _looks_like_zandra_stock_sale_text(text: str) -> bool:
    blob = text or ""
    if re.search(r"Stock\s+and\s+Sale\s+Statement|Op\s*Stk|Cl\s*Stk", blob, re.I):
        return True
    if re.search(r"Sale\s+Statement", blob, re.I) and re.search(
        r"Item\s*Cd|Op\s*St|BINAL|ZANDR", blob, re.I
    ):
        return True
    return False


def _looks_like_zl_opening_bal_sheet(result: Optional[Dict[str, Any]]) -> bool:
    """Phone screenshot of the ZL Opening_bal_qty / Secondaryrate grid.

    Title is the spreadsheet tab (Sheet1). There is no sales-qty column.
    Other stock statements are not titled SheetN, so they stay on their parsers.
    """
    if not isinstance(result, dict):
        return False
    extra = ((result.get("totals") or {}).get("extra") or {})
    if extra.get("extraction_method") == "zl_opening_bal_sheet_vision":
        return True
    title = str(result.get("report_title") or "").strip()
    if not re.fullmatch(r"Sheet\s*\d+", title, re.I):
        return False
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    if len(items) < 3:
        return False
    if any(abs(_to_float(i.get("sales_qty"))) > 0 for i in items):
        return False
    return sum(1 for i in items if _to_float(i.get("opening_qty")) > 0) >= 3


_ZL_OPENING_BAL_SHEET_PROMPT = """
This image is a spreadsheet screenshot of a Himalaya ZL stock dump.
Use it ONLY when the header row is:
Material_name | Mrp | Secondaryrate | Opening_bal_qty | Primary_qty | Closing_bal_qty

There is NO sales quantity column and NO amount column.
Do not map Secondaryrate to sales_qty. Do not map Mrp to opening or closing.

Column mapping, left to right:
- Material_name -> product_name
- Mrp -> extra.mrp
- Secondaryrate -> extra.unit_rate
- Opening_bal_qty -> opening_qty
- Primary_qty -> receipts_qty
- Closing_bal_qty -> closing_qty
- sales_qty = 0
- sales_value = 0
- closing_value = 0

Ignore the phone status bar, Excel toolbar, and row numbers.
Read only rows that are fully visible. Do not invent products.
Copy printed numbers. Do not recompute closing from opening.

Example: CONFIDO TABS (FC) 60s (AG)
opening_qty=100, receipts_qty=0, closing_qty=85, extra.mrp=255, extra.unit_rate=172.23

Return ONLY JSON:
{
  "stockist_name": null,
  "stockist_address": null,
  "company_name": null,
  "period_from": null,
  "period_to": null,
  "report_title": "Sheet1",
  "line_items": [
    {
      "product_code": null,
      "product_name": string,
      "packing": null,
      "opening_qty": number,
      "receipts_qty": number,
      "sales_qty": 0,
      "sales_value": 0,
      "closing_qty": number,
      "closing_value": 0,
      "extra": {"mrp": number, "unit_rate": number}
    }
  ]
}
""".strip()


def _finalize_zl_opening_bal_sheet(result: Dict[str, Any]) -> Dict[str, Any]:
    """Match the Excel ZL layout: rate and MRP live in extra, sales qty stays 0."""
    items: List[Dict[str, Any]] = []
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or "").strip()
        if not name or re.fullmatch(r"Material_?name|Mrp|Sheet\s*\d+", name, re.I):
            continue
        extra = item.get("extra")
        if not isinstance(extra, dict):
            extra = {}
            item["extra"] = extra
        extra["layout"] = "zl_opening_primary_closing"
        if extra.get("mrp") not in (None, ""):
            extra["mrp"] = _to_float(extra.get("mrp"))
        rate = extra.get("unit_rate")
        if rate not in (None, ""):
            rate = _to_float(rate)
            extra["unit_rate"] = rate
        item["sales_qty"] = 0.0
        item["sales_value"] = 0.0
        if rate not in (None, "", 0, 0.0) and item.get("closing_qty") is not None:
            item["closing_value"] = round(_to_float(item.get("closing_qty")) * rate, 2)
        items.append(item)
    result["line_items"] = items
    result["report_title"] = result.get("report_title") or "Sheet1"
    result.setdefault("totals", {}).setdefault("extra", {})
    result["totals"]["extra"]["extraction_method"] = "zl_opening_bal_sheet_vision"
    result["totals"]["extra"]["layout"] = "zl_opening_primary_closing"
    return result


def _extract_zl_opening_bal_sheet_vision(
    file_bytes: bytes,
    filename: str,
    ext: str = ".png",
) -> Optional[Dict[str, Any]]:
    """Re-read a ZL Opening_bal_qty screenshot with a column-locked prompt."""
    import os

    from services.vertex_gemini_client import generate_content_via_vertex

    mime = _image_mime(ext)
    b64 = base64.b64encode(file_bytes).decode("ascii")
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _ZL_OPENING_BAL_SHEET_PROMPT},
                    {"inline_data": {"mime_type": mime, "data": b64}},
                ],
            }
        ],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 32768},
    }
    parsed = None
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = generate_content_via_vertex(
                model=model, payload=payload, timeout=120
            )
            parsed = _extract_json_object(_gemini_response_text(response))
            if parsed and parsed.get("line_items"):
                break
        except Exception as exc:
            last_err = exc
            time.sleep(min(2 ** attempt, 8))
    if not parsed or not parsed.get("line_items"):
        if last_err:
            logger.warning(
                "ZL opening-bal sheet vision failed for %s: %s", filename, last_err
            )
        return None
    result = empty_result(filename, ext.lstrip(".") or "png")
    result = _apply_parsed_sales_json(result, parsed)
    return _finalize_zl_opening_bal_sheet(result)


def _looks_like_zandra_stock_sale_result(result: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(result, dict):
        return False
    title = str(result.get("report_title") or "")
    company = str(result.get("company_name") or "")
    extra = ((result.get("totals") or {}).get("extra") or {})
    if extra.get("extraction_method") == "zandra_stock_sale_vision":
        return True
    return bool(
        re.search(r"Stock\s+and\s+Sale", title, re.I)
        or re.search(r"ZANDRA", company, re.I)
    )


_ZANDRA_STOCK_SALE_VISION_PROMPT = """
This image is a Himalaya ZANDRA "Stock and Sale Statement" grid.
It is NOT a Product Stock Report and NOT an OpBal sheet.

Columns LEFT TO RIGHT. A blank cell is 0. NEVER shift a later number left into a blank cell.

1 Item Cd -> product_code
2 Item Name -> product_name
3 Op Stk -> opening_qty
4 P Qty -> receipts_qty
5 P S Qty -> extra.purchase_scheme_qty
6 P Val -> extra.purchase_value
7 S Qty -> sales_qty
8 S S Qty -> extra.sales_scheme_qty
9 S Val -> sales_value
10 Cl Stk -> closing_qty
11 Cl Val -> closing_value
12 Order -> extra.order_qty

S Qty is sales quantity. S S Qty is scheme quantity (different column).
S Val is sales money. Cl Stk is closing quantity. Cl Val is closing money.
Do not copy P Val or Cl Val into closing_qty. Do not copy the next product's numbers.

Skip the ZANDRA / HIMALAYA ZANDRA DIVISION header row.
Skip TOTAL / Grand Total / Aprox Order Value.
Copy printed cells only. Do not invent or recompute qty.
This page only.

Examples of correct mapping:
- ARJUNA TABLET: opening=49, receipts=0, sales=2, scheme=0, sales_value=495, closing=47, closing_value=9333
- BONNISAN LIQUID: opening=386, sales=132, scheme=5, sales_value=8710, closing=249, closing_value=14217
- BONNISAN LIQUID 200ML: opening=32, sales=6, scheme=0, sales_value=665, closing=26, closing_value=2371
- BONNISON DROPS: opening=333, sales=276, scheme=15, sales_value=20457, closing=42, closing_value=2641

Return ONLY JSON:
{
  "stockist_name": string|null,
  "stockist_address": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": "Stock and Sale Statement",
  "line_items": [
    {
      "product_code": string|null,
      "product_name": string,
      "packing": null,
      "opening_qty": number,
      "receipts_qty": number,
      "sales_qty": number,
      "sales_value": number,
      "closing_qty": number,
      "closing_value": number,
      "extra": {
        "purchase_scheme_qty": number,
        "purchase_value": number,
        "sales_scheme_qty": number,
        "order_qty": number
      }
    }
  ]
}
""".strip()


def _finalize_zandra_stock_sale(result: Dict[str, Any]) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or "")
        if re.search(
            r"ZANDRA\s+DIVISION|HIMALAYA\s+ZANDRA|^TOTAL\b|^GRAND\s*TOTAL",
            name,
            re.I,
        ):
            continue
        extra = item.setdefault("extra", {})
        if isinstance(extra, dict):
            extra.setdefault("purchase_scheme_qty", 0)
            extra.setdefault("sales_scheme_qty", extra.get("sales_scheme") or 0)
        items.append(item)
    result["line_items"] = _drop_trailing_statement_total_item(items)
    result["report_title"] = result.get("report_title") or "Stock and Sale Statement"
    result.setdefault("totals", {}).setdefault("extra", {})
    result["totals"]["extra"]["extraction_method"] = "zandra_stock_sale_vision"
    return result


def _extract_zandra_stock_sale_vision(
    file_bytes: bytes,
    filename: str,
    ext: str = ".png",
) -> Optional[Dict[str, Any]]:
    """Read one ZANDRA Stock and Sale Statement page with a column-locked prompt."""
    import os

    from services.vertex_gemini_client import generate_content_via_vertex

    mime = _image_mime(ext)
    b64 = base64.b64encode(file_bytes).decode("ascii")
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _ZANDRA_STOCK_SALE_VISION_PROMPT},
                    {"inline_data": {"mime_type": mime, "data": b64}},
                ],
            }
        ],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192},
    }
    parsed = None
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = generate_content_via_vertex(
                model=model, payload=payload, timeout=120
            )
            parsed = _extract_json_object(_gemini_response_text(response))
            if parsed and parsed.get("line_items"):
                break
        except Exception as exc:
            last_err = exc
            time.sleep(min(2 ** attempt, 8))
    if last_err and not (parsed and parsed.get("line_items")):
        logger.warning("ZANDRA stock-sale vision failed for %s: %s", filename, last_err)
        return None
    if not parsed or not parsed.get("line_items"):
        return None
    result = empty_result(filename, ext.lstrip(".") or "png")
    result = _apply_parsed_sales_json(result, parsed)
    return _finalize_zandra_stock_sale(result)


def _qv_header_rows(
    rows: List[Dict[str, Any]],
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """RATE / OPENING / RECEIPT / ISSUE / CLOSING header plus its QTY VALUE row."""
    for idx, row in enumerate(rows):
        tokens = set(_ssa_row_tokens(row))
        if not {"rate", "opening", "receipt", "issue", "closing"} <= tokens:
            continue
        for nxt in rows[idx + 1 : idx + 4]:
            labels = [str(box[4]).upper().rstrip(".") for box in nxt["words"]]
            if labels.count("QTY") >= 4 and labels.count("VALUE") >= 4:
                return row, nxt
    return None


def _qv_column_anchors(
    header: Dict[str, Any], sub: Dict[str, Any]
) -> Optional[List[Tuple[str, float]]]:
    rate = next(
        (box for box in header["words"] if _ssa_token(box[4]) == "rate"), None
    )
    if rate is None:
        return None
    pairs = (
        ("opening_qty", "opening_value"),
        ("receipts_qty", "receipts_value"),
        ("sales_qty", "sales_value"),
        ("closing_qty", "closing_value"),
    )
    labels = [(str(box[4]).upper().rstrip("."), (box[0] + box[2]) / 2.0) for box in sub["words"]]
    anchors: List[Tuple[str, float]] = [("rate", (rate[0] + rate[2]) / 2.0)]
    cursor = 0
    for qty_name, value_name in pairs:
        while cursor < len(labels) and labels[cursor][0] != "QTY":
            cursor += 1
        if cursor >= len(labels):
            return None
        anchors.append((qty_name, labels[cursor][1]))
        cursor += 1
        while cursor < len(labels) and labels[cursor][0] != "VALUE":
            cursor += 1
        if cursor >= len(labels):
            return None
        anchors.append((value_name, labels[cursor][1]))
        cursor += 1
    return anchors


def _qv_dump_header_rows(
    rows: List[Dict[str, Any]],
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """OPENING / RECEIPT / ISSUE / CLOSING / DUMP header plus QTY VALUE."""
    for idx, row in enumerate(rows):
        tokens = set(_ssa_row_tokens(row))
        if "rate" in tokens:
            continue
        if not {"opening", "receipt", "issue", "closing", "dump"} <= tokens:
            continue
        for nxt in rows[idx + 1 : idx + 4]:
            labels = [str(box[4]).upper().rstrip(".") for box in nxt["words"]]
            if labels.count("QTY") >= 5 and labels.count("VALUE") >= 4:
                return row, nxt
    return None


def _qv_dump_column_anchors(
    _header: Dict[str, Any], sub: Dict[str, Any]
) -> Optional[List[Tuple[str, float]]]:
    """Column centers for Qty/Value pairs plus the final Dump Qty."""
    labels = [
        (str(box[4]).upper().rstrip("."), (box[0] + box[2]) / 2.0)
        for box in sub["words"]
    ]
    pairs = (
        ("opening_qty", "opening_value"),
        ("receipts_qty", "receipts_value"),
        ("sales_qty", "sales_value"),
        ("closing_qty", "closing_value"),
    )
    anchors: List[Tuple[str, float]] = []
    cursor = 0
    for qty_name, value_name in pairs:
        while cursor < len(labels) and labels[cursor][0] != "QTY":
            cursor += 1
        if cursor >= len(labels):
            return None
        anchors.append((qty_name, labels[cursor][1]))
        cursor += 1
        while cursor < len(labels) and labels[cursor][0] != "VALUE":
            cursor += 1
        if cursor >= len(labels):
            return None
        anchors.append((value_name, labels[cursor][1]))
        cursor += 1
    while cursor < len(labels) and labels[cursor][0] != "QTY":
        cursor += 1
    if cursor >= len(labels):
        return None
    anchors.append(("dump_qty", labels[cursor][1]))
    return anchors


def _qv_split_packing(name: str) -> Tuple[str, Optional[str]]:
    tokens = _clean_name(name).split()
    if not tokens:
        return "", None
    last = tokens[-1]
    if _looks_like_packing_token(last) or re.fullmatch(
        r"\d+\*\d+[A-Za-z']*|\d+(?:\.\d+)?(?:GM|G|KG)", last, re.I
    ):
        return _clean_name(" ".join(tokens[:-1])), last
    return _clean_name(name), None


def _parse_rate_qty_value_statement(
    pages: List[Dict[str, Any]], filename: str
) -> Optional[Dict[str, Any]]:
    """Map RATE + OPENING/RECEIPT/ISSUE/CLOSING QTY/VALUE cells by word x position.

    A printed dash stays in its own column. Later numbers are not shifted left
    into Opening or Closing.
    """
    items: List[Dict[str, Any]] = []
    blob_parts: List[str] = []
    carried: Optional[List[Tuple[str, float]]] = None
    for page in pages:
        words = page.get("words") or []
        if not words:
            continue
        blob_parts.append(" ".join(str(w[4]) for w in words if len(w) > 4))
        rows = _ssa_cluster_rows(words)
        found = _qv_header_rows(rows)
        anchors = _qv_column_anchors(*found) if found else None
        if not anchors:
            dump_header = _qv_dump_header_rows(rows)
            anchors = _qv_dump_column_anchors(*dump_header) if dump_header else None
        if anchors:
            carried = anchors
        elif carried:
            anchors = carried
        else:
            continue
        centers = [x for _name, x in anchors]
        gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
        max_dist = max(12.0, min(gaps) / 2.0) if gaps else 18.0
        for row in rows:
            if _qv_header_rows([row]) or any(
                str(box[4]).upper().rstrip(".") == "QTY" for box in row["words"]
            ):
                continue
            cells: Dict[str, List[str]] = {name: [] for name, _x in anchors}
            name_words: List[Tuple[float, str]] = []
            for box in row["words"]:
                cx = (box[0] + box[2]) / 2.0
                nearest = min(range(len(anchors)), key=lambda i: abs(cx - centers[i]))
                if abs(cx - centers[nearest]) <= max_dist and cx >= centers[0] - max_dist:
                    cells[anchors[nearest][0]].append(box[4])
                elif cx < centers[0]:
                    name_words.append((box[0], box[4]))
            name_words.sort(key=lambda pair: pair[0])
            raw_name = _clean_name(" ".join(text for _x, text in name_words))
            if _ssa_skip_product(raw_name) or re.search(
                r"phone|gstin|licence|e-?mail|whatsap|\btin\b|page\s*no|continued",
                raw_name,
                re.I,
            ):
                continue
            parsed: Dict[str, Optional[float]] = {}
            for field, _x in anchors:
                bits = cells.get(field) or []
                parsed[field] = _ocr_qty_token(bits[0]) if bits else None
            if parsed.get("rate") is None and not any(
                parsed.get(field) is not None
                for field in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
            ):
                continue
            # This grid prints a value beside every qty, including 0.00.
            # A lone amount from a footer is not a product row.
            if any(name == "dump_qty" for name, _x in anchors) and any(
                parsed.get(field) is None
                for field in (
                    "opening_value",
                    "receipts_value",
                    "sales_value",
                    "closing_value",
                )
            ):
                continue
            product_name, packing = _qv_split_packing(raw_name)
            if _ssa_skip_product(product_name):
                continue
            item = empty_line_item()
            item["product_name"] = product_name
            item["packing"] = packing
            for field in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
                if parsed.get(field) is not None:
                    item[field] = parsed[field]
            if parsed.get("sales_value") is not None:
                item["sales_value"] = parsed["sales_value"]
            if parsed.get("closing_value") is not None:
                item["closing_value"] = parsed["closing_value"]
            if parsed.get("opening_value") is not None:
                item["opening_value"] = parsed["opening_value"]
            has_dump = any(name == "dump_qty" for name, _x in anchors)
            extra = {"layout": "qty_value_dump" if has_dump else "rate_qty_value"}
            if parsed.get("rate") is not None:
                extra["unit_rate"] = parsed["rate"]
            if has_dump:
                extra["dump_qty"] = (
                    parsed["dump_qty"] if parsed.get("dump_qty") is not None else 0.0
                )
                if parsed.get("receipts_value") is not None:
                    extra["receipts_value"] = parsed["receipts_value"]
                if parsed.get("opening_value") is not None:
                    extra["opening_value"] = parsed["opening_value"]
            item["extra"] = extra
            items.append(item)
    if len(items) < 3:
        return None
    blob = " ".join(blob_parts)
    if not re.search(r"STOCK\s*&\s*SALES", blob, re.I):
        return None
    result = empty_result(filename, "pdf")
    result["report_title"] = "STOCK & SALES ANALYSIS"
    result["line_items"] = items
    period = re.search(
        r"(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\s*[-–]+\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})",
        blob,
    )
    if period:
        result["period_from"] = _normalize_date(period.group(1))
        result["period_to"] = _normalize_date(period.group(2))
    for page in pages:
        found = _detect_stockist_from_page_text(page.get("text") or "")
        if found and not re.search(r"LAST\s+MONTH\s+SALE", found, re.I):
            result["stockist_name"] = found
            break
    result["totals"]["extra"]["extraction_method"] = "rate_qty_value_columns"
    return result


def _extract_statement_from_pdf_group(
    group: Dict[str, Any], filename: str
) -> Dict[str, Any]:
    """Extract one stockist statement from its page group (text and/or images)."""
    pages = group["pages"]
    page_nos = [p["page_index"] + 1 for p in pages]
    combined_text = "\n\n".join(p.get("text") or "" for p in pages).strip()
    has_page_images = any(p.get("image_bytes") for p in pages)

    result: Optional[Dict[str, Any]] = None
    image_only = _pdf_pages_are_image_only(pages) and has_page_images
    zandra_hint = _looks_like_zandra_stock_sale_text(combined_text)

    geometry = _parse_stock_sales_analysis_words(pages, filename)
    if geometry and geometry.get("line_items"):
        result = geometry

    if result is None:
        qty_value = _parse_rate_qty_value_statement(pages, filename)
        if qty_value and qty_value.get("line_items"):
            result = qty_value

    # Scanned grids: Tesseract+Gemini-text often returns names with qty=0.
    if (
        result is None
        and not image_only
        and len(re.sub(r"\s+", "", combined_text)) >= 80
    ):
        result = _structure_sales_text(combined_text, filename, "pdf")
        if result.get("line_items"):
            result["totals"]["extra"]["extraction_method"] = (
                result.get("totals", {}).get("extra", {}).get("extraction_method")
                or "pdf_split_text"
            )

    text_nonzero = _statement_nonzero_rows(result)
    text_weak = (
        image_only
        or zandra_hint
        or not result
        or not result.get("line_items")
        or _statement_numbers_are_blank(result)
        or text_nonzero < 8
    )

    if has_page_images and text_weak:
        merged = empty_result(filename, "pdf")
        merged_items: List[Dict[str, Any]] = []
        used_zandra = False
        saw_swil_receipt_ocr = False
        page_results: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for p in pages:
            img = p.get("image_bytes")
            if not img:
                continue
            page_name = f"{filename}#page{p['page_index'] + 1}"
            page_result: Optional[Dict[str, Any]] = None
            upright_swil = False
            try:
                upright_swil = _is_swil_opening_receipt_value_statement(
                    _ocr_image_to_text(_upright_swil_receipt_image(img))
                )
            except Exception:
                upright_swil = False
            if upright_swil:
                saw_swil_receipt_ocr = True
                page_result = _extract_swil_receipt_value_vision(
                    img, page_name, ".png"
                )
            if (
                (not page_result or not page_result.get("line_items"))
                and not upright_swil
                and (
                    zandra_hint
                    or _looks_like_zandra_stock_sale_text(p.get("text") or "")
                )
            ):
                page_result = _extract_zandra_stock_sale_vision(
                    img, page_name, ".png"
                )
                used_zandra = used_zandra or bool(
                    page_result and page_result.get("line_items")
                )
            if not page_result or not page_result.get("line_items"):
                page_result = _parse_image(img, page_name, ".png")
                if (
                    _looks_like_zandra_stock_sale_result(page_result)
                    and not upright_swil
                    and not re.search(
                        r"Sales\s*&\s*Stock",
                        str(page_result.get("report_title") or ""),
                        re.I,
                    )
                ):
                    zandra = _extract_zandra_stock_sale_vision(img, page_name, ".png")
                    if zandra and zandra.get("line_items"):
                        page_result = zandra
                        used_zandra = True
            page_results.append((p, page_result or empty_result(page_name, "pdf")))

        # Continuation pages often omit the Sales & Stock header. If one page
        # already kept Receipt/Pur Value, reread pages that dropped it.
        if saw_swil_receipt_ocr or any(
            _swil_receipt_value_count(pr) > 0 for _p, pr in page_results
        ):
            for idx, (p, pr) in enumerate(page_results):
                img = p.get("image_bytes")
                if not img or _swil_receipt_value_count(pr) > 0:
                    continue
                if not (pr.get("line_items") or []):
                    continue
                page_name = f"{filename}#page{p['page_index'] + 1}"
                swil = _extract_swil_receipt_value_vision(img, page_name, ".png")
                if swil and swil.get("line_items"):
                    page_results[idx] = (p, swil)

        used_swil_receipt = False
        for _p, page_result in page_results:
            method = str(
                ((page_result.get("totals") or {}).get("extra") or {}).get(
                    "extraction_method"
                )
                or ""
            )
            used_swil_receipt = used_swil_receipt or method == "swil_receipt_pur_value_vision"
            for key in (
                "stockist_name",
                "stockist_address",
                "company_name",
                "period_from",
                "period_to",
                "report_title",
            ):
                if page_result.get(key) and not merged.get(key):
                    merged[key] = page_result[key]
            merged_items.extend(page_result.get("line_items") or [])
            totals = page_result.get("totals") or {}
            if totals.get("sales_value") is not None:
                merged["totals"]["sales_value"] = totals.get("sales_value")
            if totals.get("closing_value") is not None:
                merged["totals"]["closing_value"] = totals.get("closing_value")
            if totals.get("receipts_value") is not None:
                merged["totals"]["receipts_value"] = totals.get("receipts_value")
                merged["totals"]["extra"]["receipts_value"] = totals.get("receipts_value")
        merged["line_items"] = _drop_trailing_statement_total_item(merged_items)
        swil_receipt_doc = (
            used_swil_receipt
            or saw_swil_receipt_ocr
            or _looks_like_swil_receipt_pur_result(merged)
        )
        if not swil_receipt_doc:
            title = str(merged.get("report_title") or "")
            swil_receipt_doc = bool(
                re.search(r"Sales\s*&\s*Stock", title, re.I)
                and len(merged.get("line_items") or []) >= 8
            )
        if swil_receipt_doc:
            merged["line_items"] = _dedupe_swil_receipt_overlap_items(
                merged["line_items"]
            )
            used_swil_receipt = True
            printed = None
            for p, _pr in reversed(page_results):
                img = p.get("image_bytes")
                if not img:
                    continue
                try:
                    from PIL import Image

                    image = Image.open(io.BytesIO(img))
                    if image.mode not in ("RGB", "L"):
                        image = image.convert("RGB")
                    for deg in (0, 90, 270):
                        rot = image if deg == 0 else image.rotate(deg, expand=True)
                        buf = io.BytesIO()
                        rot.save(buf, format="PNG")
                        printed = _parse_swil_receipt_printed_totals(
                            _ocr_image_to_text(buf.getvalue())
                        )
                        if printed and printed.get("closing_value"):
                            break
                        printed = None
                except Exception:
                    printed = None
                if printed and printed.get("closing_value"):
                    break
            if printed:
                merged["totals"]["sales_value"] = printed["sales_value"]
                merged["totals"]["closing_value"] = printed["closing_value"]
                merged["totals"]["receipts_value"] = printed["receipts_value"]
                extra_t = merged["totals"]["extra"]
                extra_t["opening_value"] = printed["opening_value"]
                extra_t["receipts_value"] = printed["receipts_value"]
                extra_t["total_row_source"] = "swil_receipt_pur_total_value"
        if used_swil_receipt:
            merged["totals"]["extra"]["extraction_method"] = "swil_receipt_pur_value_vision"
        elif used_zandra:
            merged["totals"]["extra"]["extraction_method"] = "zandra_stock_sale_vision"
        else:
            merged["totals"]["extra"]["extraction_method"] = "pdf_split_page_images"
        image_nonzero = _statement_nonzero_rows(merged)
        prefer_images = (
            image_nonzero > text_nonzero
            or (image_only and merged.get("line_items"))
            or (_statement_numbers_are_blank(result) and image_nonzero > 0)
            or used_zandra
            or used_swil_receipt
        )
        if prefer_images:
            logger.info(
                "Sales statement image read for %s kept (%s rows) over text parse (%s rows)",
                filename,
                image_nonzero,
                text_nonzero,
            )
            result = merged
        elif not result or not result.get("line_items"):
            result = merged

    if result is None:
        result = empty_result(filename, "pdf")

    # Ensure stockist name from split detection wins when vision/OCR confuses manufacturer
    detected = group.get("stockist_name")
    if detected and not re.search(r"^Statement_\d+$", detected):
        current = result.get("stockist_name") or ""
        if (
            not current
            or _MANUFACTURER_HINT.search(current)
            or _normalize_stockist_key(current) != _normalize_stockist_key(detected)
        ):
            # Keep manufacturer if it was misplaced into stockist
            if current and _MANUFACTURER_HINT.search(current) and not result.get(
                "company_name"
            ):
                result["company_name"] = current
            result["stockist_name"] = detected

    result["source_file"] = filename
    result["source_format"] = "pdf"
    result["totals"]["extra"]["split_pages"] = page_nos
    result["totals"]["extra"]["stockist_key"] = group.get("stockist_key")

    # Rebuild combined text with footer OCR boost if TOTAL still missing
    if combined_text and not re.search(r"^\s*TOTAL\b.*\d", combined_text, re.I | re.M):
        boosted_parts = []
        for p in pages:
            boosted_parts.append(
                _merge_footer_total_into_text(p.get("text") or "", p.get("image_bytes"))
            )
        combined_text = "\n\n".join(boosted_parts)

    if combined_text:
        result = _apply_total_row_to_result(result, combined_text)
    return result


# PROMPT "Stock Statement (Datewise)" column x-ranges (A4, points).
# Plain text extraction interleaves these columns; positions stay stable.
_PROMPT_DATEWISE_BUCKETS = (
    ("sr", 0.0, 40.0),
    ("name", 40.0, 145.0),
    ("pack", 145.0, 200.0),
    ("opening_qty", 200.0, 245.0),
    ("receipts_qty", 245.0, 285.0),
    ("sales_qty", 285.0, 325.0),
    ("sales_value", 325.0, 370.0),
    ("closing_qty", 370.0, 408.0),
    ("closing_value", 408.0, 460.0),
    ("a3mn", 460.0, 485.0),
    ("ee", 485.0, 520.0),
    ("age", 520.0, 560.0),
    ("exp", 560.0, 10000.0),
)


def _prompt_datewise_bucket(
    x: float, buckets: Optional[Tuple] = None
) -> Optional[str]:
    for name, lo, hi in buckets or _PROMPT_DATEWISE_BUCKETS:
        if lo <= x < hi:
            return name
    return None


def _prompt_datewise_buckets_for_words(words: List) -> Tuple:
    """Keep the standard columns unless Sales Qty sits inside the Pur span.

    On this Datewise variant the Sales Qty header is near x=275, so those
    numbers fall in receipts_qty (245–285). Other PROMPT files, where Sales
    Qty is already at or right of 285, keep the original ranges.
    """
    sales_x = None
    free_x = None
    qty_xs: List[float] = []
    for w in words or []:
        token = str(w[4]).strip()
        x = float(w[0])
        if token == "Sales":
            sales_x = x
        elif token == "Free":
            free_x = x
        elif token == "Qty":
            qty_xs.append(x)
    if sales_x is None:
        return _PROMPT_DATEWISE_BUCKETS
    sales_qty_x = max((x for x in qty_xs if x < sales_x), default=None)
    if sales_qty_x is None or sales_qty_x >= 285:
        return _PROMPT_DATEWISE_BUCKETS
    pur_qty_x = max((x for x in qty_xs if x < sales_qty_x - 5), default=245.0)
    split = max(250.0, min((pur_qty_x + sales_qty_x) / 2.0, 284.0))
    sales_hi = 325.0
    if free_x is not None and free_x > sales_qty_x + 8:
        sales_hi = min(sales_hi, free_x - 4)
    adjusted = []
    for name, lo, hi in _PROMPT_DATEWISE_BUCKETS:
        if name == "receipts_qty":
            adjusted.append((name, lo, split))
        elif name == "sales_qty":
            adjusted.append((name, split, sales_hi))
        elif name == "sales_value":
            adjusted.append((name, sales_hi, hi))
        else:
            adjusted.append((name, lo, hi))
    return tuple(adjusted)


def _is_prompt_datewise_stock_statement(text: str) -> bool:
    """PROMPT software datewise stock statement (OpStk / Pur / Sales / ClStk)."""
    if not text:
        return False
    return bool(
        re.search(r"Stock\s+Statement", text, re.I)
        and re.search(r"\bOpStk\b", text)
        and re.search(r"\bClStk\b", text)
    )


def _prompt_cell_number(tokens: List[str]) -> Optional[float]:
    """Last plain number in a cell. Ignores labels that share the row (AppExp)."""
    found = None
    for tok in tokens:
        raw = str(tok).replace(",", "").strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
            found = float(raw)
    return found


def _parse_prompt_datewise_stock_statement(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Parse PROMPT Stock Statement (Datewise) from word positions.

    Returns None when the PDF is a different statement format.
    """
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_prompt_datewise_stock_statement(t) for t in page_texts):
        return None

    result = empty_result(filename, "pdf")
    result["report_title"] = "Stock Statement (Datewise)"
    items: List[Dict[str, Any]] = []
    company_name = None

    for page, text in zip(doc, page_texts):
        words = page.get_text("words") or []
        words = sorted(words, key=lambda w: (round(w[1], 1), w[0]))
        buckets = _prompt_datewise_buckets_for_words(words)
        rows: List[Dict[str, Any]] = []
        for w in words:
            x0, y0, _x1, _y1, token = w[0], w[1], w[2], w[3], w[4]
            bucket = _prompt_datewise_bucket(x0, buckets)
            if not bucket or not str(token).strip():
                continue
            if rows and abs(y0 - rows[-1]["y"]) <= 3.0:
                row = rows[-1]
            else:
                row = {
                    "y": y0,
                    "tokens": [],
                    "cells": {name: [] for name, _lo, _hi in _PROMPT_DATEWISE_BUCKETS},
                }
                rows.append(row)
            row["tokens"].append((x0, str(token)))
            row["cells"][bucket].append(str(token))

        header_y = None
        for row in rows:
            blob = " ".join(tok for cell in row["cells"].values() for tok in cell)
            if re.search(r"\bOpStk\b", blob) and re.search(r"\bClStk\b", blob):
                header_y = row["y"]
            if result["period_from"] is None and re.search(r"From\s*:", blob, re.I):
                dates = re.findall(r"\d{1,2}-\d{1,2}-\d{4}", blob)
                if dates:
                    result["period_from"] = _normalize_date(dates[0])
                if len(dates) > 1:
                    result["period_to"] = _normalize_date(dates[1])

        if not result["stockist_name"]:
            for row in rows:
                if header_y is not None and row["y"] >= header_y - 2:
                    break
                # Header lines sit left of the period dates (From/To).
                left = " ".join(
                    tok for x, tok in sorted(row["tokens"], key=lambda t: t[0]) if x < 400
                ).strip()
                if not left or re.search(r"Phone|Stock\s+Statement|From\s*:", left, re.I):
                    continue
                if not result["stockist_name"]:
                    result["stockist_name"] = _clean_name(left)
                else:
                    result["stockist_address"] = _clean_name(
                        (result.get("stockist_address") or "") + " " + left
                    ).strip()

        for row in rows:
            if header_y is not None and row["y"] <= header_y + 8:
                continue
            cells = row["cells"]
            sr_txt = "".join(cells["sr"]).strip()
            name = _clean_name(" ".join(cells["name"]))
            label = _clean_name(" ".join(cells["sr"] + cells["name"]))
            if re.match(r"^Total\s*:?\s*$", label, re.I):
                result["totals"]["sales_value"] = _prompt_cell_number(cells["sales_value"])
                result["totals"]["closing_value"] = _prompt_cell_number(cells["closing_value"])
                extra = result["totals"]["extra"]
                extra["opening_qty"] = _prompt_cell_number(cells["opening_qty"])
                extra["receipts_qty"] = _prompt_cell_number(cells["receipts_qty"])
                extra["sales_qty"] = _prompt_cell_number(cells["sales_qty"])
                extra["closing_qty"] = _prompt_cell_number(cells["closing_qty"])
                extra["total_row_source"] = "prompt_datewise_footer"
                continue
            if not re.fullmatch(r"\d{1,4}", sr_txt):
                banner = _clean_name(" ".join(cells["sr"] + cells["name"]))
                if (
                    company_name is None
                    and not items
                    and banner
                    and not re.match(r"^(Total|Bills|Product|Pack)\b", banner, re.I)
                    and not _prompt_cell_number(cells["opening_qty"])
                ):
                    company_name = banner
                continue
            if name and not re.match(r"^(Total|Bills)\b", name, re.I):
                item = empty_line_item()
                item["product_code"] = sr_txt
                item["product_name"] = name
                pack = " ".join(cells["pack"]).strip()
                item["packing"] = pack or None
                item["opening_qty"] = _prompt_cell_number(cells["opening_qty"]) or 0.0
                item["receipts_qty"] = _prompt_cell_number(cells["receipts_qty"]) or 0.0
                item["sales_qty"] = _prompt_cell_number(cells["sales_qty"]) or 0.0
                item["sales_value"] = _prompt_cell_number(cells["sales_value"]) or 0.0
                item["closing_qty"] = _prompt_cell_number(cells["closing_qty"]) or 0.0
                item["closing_value"] = _prompt_cell_number(cells["closing_value"]) or 0.0
                extra = item["extra"]
                extra["layout"] = "prompt_datewise"
                for key in ("a3mn", "ee", "age", "exp"):
                    val = " ".join(cells[key]).strip()
                    if val and val not in {"-", "—"}:
                        extra[key] = val
                items.append(item)

    if company_name:
        result["company_name"] = company_name
    if not items:
        return None
    result["line_items"] = items
    result["totals"]["extra"]["extraction_method"] = "prompt_datewise_layout"
    return result


_SWIL_VALUE_BUCKETS = (
    ("name", 0.0, 96.0),
    ("pack", 96.0, 155.0),
    ("opening_qty", 155.0, 200.0),
    ("opening_value", 200.0, 240.0),
    ("receipts_qty", 240.0, 272.0),
    ("receipts_value", 272.0, 330.0),
    ("total_qty", 330.0, 365.0),
    ("sales_qty", 365.0, 405.0),
    ("sales_value", 405.0, 455.0),
    ("closing_qty", 455.0, 490.0),
    ("closing_value", 490.0, 548.0),
    ("near_expiry", 548.0, 700.0),
)


def _swil_value_bucket(x: float) -> Optional[str]:
    for name, lo, hi in _SWIL_VALUE_BUCKETS:
        if lo <= x < hi:
            return name
    return None


def _is_swil_opening_receipt_value_statement(text: str) -> bool:
    """SwilERP Sales & Stock with Opening/Receipt qty and value columns."""
    if not text:
        return False
    return bool(
        re.search(r"Sales\s*&\s*Stock", text, re.I)
        and re.search(r"Receipt/Pur", text, re.I)
        and re.search(r"Opening", text, re.I)
    )


def _parse_swil_opening_receipt_value_statement(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Parse SwilERP statements that print a Receipt value next to Receipt qty.

    Returns None for other layouts, including qty-only OpBal statements.
    """
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_swil_opening_receipt_value_statement(t) for t in page_texts):
        return None

    result = empty_result(filename, "pdf")
    items: List[Dict[str, Any]] = []

    for page, text in zip(doc, page_texts):
        if not result.get("report_title"):
            m = re.search(
                r"Sales\s*&\s*Stock\s*Statement\s*\(\s*From\s+"
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+Upto\s+"
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
                text,
                re.I,
            )
            if m:
                result["report_title"] = "Sales & Stock Statement"
                result["period_from"] = _normalize_date(m.group(1))
                result["period_to"] = _normalize_date(m.group(2))

        words = sorted(page.get_text("words") or [], key=lambda w: (round(w[1], 1), w[0]))
        rows: List[Dict[str, Any]] = []
        for w in words:
            x0, y0, x1, _y1, token = w[0], w[1], w[2], w[3], w[4]
            center = (x0 + x1) / 2.0
            bucket = _swil_value_bucket(center)
            if not bucket or not str(token).strip():
                continue
            if rows and abs(y0 - rows[-1]["y"]) <= 2.5:
                row = rows[-1]
            else:
                row = {
                    "y": y0,
                    "cells": {name: [] for name, _lo, _hi in _SWIL_VALUE_BUCKETS},
                }
                rows.append(row)
            row["cells"][bucket].append(str(token))

        header_y = None
        for row in rows:
            blob = " ".join(tok for cell in row["cells"].values() for tok in cell)
            if re.search(r"Receipt/Pur", blob, re.I) and re.search(r"Opening", blob, re.I):
                header_y = row["y"]

        if not result.get("stockist_name"):
            for row in rows:
                if header_y is not None and row["y"] >= header_y - 2:
                    break
                line = " ".join(row["cells"]["name"]).strip()
                wide = " ".join(tok for cell in row["cells"].values() for tok in cell).strip()
                if re.search(r"Page\s*No|Sales\s*&\s*Stock|PRODUCT", wide, re.I):
                    continue
                if not result["stockist_name"] and wide and not re.search(r"HIMALAYA|W/NO", wide, re.I):
                    result["stockist_name"] = _clean_name(wide)
                elif not result.get("stockist_address") and re.search(r"W/NO|NEAR|HALL|ROAD", wide, re.I):
                    result["stockist_address"] = _clean_name(wide)
                elif not result.get("company_name") and line and re.fullmatch(r"[A-Z][A-Z .&-]{2,}", line):
                    result["company_name"] = _clean_name(line)

        for row in rows:
            if header_y is not None and row["y"] <= header_y + 14:
                continue
            cells = row["cells"]
            name = _clean_name(" ".join(cells["name"]))
            if not name or re.match(
                r"^(PRODUCT|Page|Powered|\*+|-+|GRAND)\b", name, re.I
            ):
                if re.match(r"^GRAND\s*TOTAL\b", name, re.I):
                    extra = result["totals"]["extra"]
                    if _prompt_cell_number(cells["opening_value"]) is not None:
                        extra["opening_value"] = _prompt_cell_number(cells["opening_value"])
                    rec_val = _prompt_cell_number(cells["receipts_value"])
                    if rec_val is not None:
                        result["totals"]["receipts_value"] = rec_val
                    if _prompt_cell_number(cells["sales_value"]) is not None:
                        result["totals"]["sales_value"] = _prompt_cell_number(cells["sales_value"])
                    if _prompt_cell_number(cells["closing_value"]) is not None:
                        result["totals"]["closing_value"] = _prompt_cell_number(cells["closing_value"])
                    extra["total_row_source"] = "swil_grand_total"
                continue
            if _prompt_cell_number(cells["opening_qty"]) is None:
                continue
            item = empty_line_item()
            item["product_name"] = name
            pack = " ".join(cells["pack"]).strip()
            item["packing"] = pack or None
            item["opening_qty"] = _prompt_cell_number(cells["opening_qty"]) or 0.0
            item["receipts_qty"] = _prompt_cell_number(cells["receipts_qty"]) or 0.0
            rec_val = _prompt_cell_number(cells["receipts_value"])
            item["sales_qty"] = _prompt_cell_number(cells["sales_qty"]) or 0.0
            item["closing_qty"] = _prompt_cell_number(cells["closing_qty"]) or 0.0
            if _prompt_cell_number(cells["sales_value"]) is not None:
                item["sales_value"] = _prompt_cell_number(cells["sales_value"])
            if _prompt_cell_number(cells["closing_value"]) is not None:
                item["closing_value"] = _prompt_cell_number(cells["closing_value"])
            extra = item["extra"]
            if _prompt_cell_number(cells["opening_value"]) is not None:
                extra["opening_value"] = _prompt_cell_number(cells["opening_value"])
            if rec_val is not None:
                ordered = {}
                for key, val in item.items():
                    ordered[key] = val
                    if key == "receipts_qty":
                        ordered["receipts_value"] = rec_val
                item = ordered
            if _prompt_cell_number(cells["total_qty"]) is not None:
                extra["total_stock_qty"] = _prompt_cell_number(cells["total_qty"])
            if _prompt_cell_number(cells["near_expiry"]) is not None:
                extra["near_expiry_qty"] = _prompt_cell_number(cells["near_expiry"])
            items.append(item)

    if not items:
        return None
    result["line_items"] = items
    result["totals"]["extra"]["extraction_method"] = "swil_opening_receipt_value"
    return result


# Landscape SwilERP Sales & Stock (Code / PRODUCT NAME / PACKING / Qty+Value pairs).
# The fixed _SWIL_VALUE_BUCKETS above target a narrower page. This layout is
# ~A4 landscape (~842pt) with extra Shortage/Expiry/Dump columns.
_SWIL_LAND_MIN_WIDTH = 780.0
_SWIL_LAND_CODE_RE = re.compile(r"^[A-Z]{1,6}\d+[A-Z0-9]*$", re.I)
_SWIL_LAND_SKIP_RE = re.compile(
    r"Page\s*No|Sales\s*&\s*Stock|PRODUCT\s+NAME|\bPACKING\b|"
    r"Powered\s+By|GRAND\s*TOTAL|^TOTAL\b|Purchase\s+Invoice|"
    r"Receipt\s+Date|Amount\s+Rs|Continued|^\*+|^-+|\bDtd\.?\b",
    re.I,
)
_SWIL_LAND_GROUP_QTY_VALUE = {
    "op_group": ("opening_qty", "opening_value"),
    "rec_group": ("receipts_qty", "receipts_value"),
    "total_group": ("total_qty", None),
    "issue_group": ("sales_qty", "sales_value"),
    "shortage_group": ("shortage_qty", "shortage_value"),
    "expiry_group": ("expiry_qty", "expiry_value"),
    "closing_group": ("closing_qty", "closing_value"),
    "dump_group": ("dump_qty", None),
    "ne_group": ("near_expiry", None),
}


def _is_swil_landscape_qty_value_text(text: str) -> bool:
    """Detect landscape SwilERP Code/PACKING/Issue-Sales statements from text."""
    if not text:
        return False
    return bool(
        re.search(r"Sales\s*&\s*Stock", text, re.I)
        and re.search(r"Receipt/Pur", text, re.I)
        and re.search(r"\bPACKING\b", text)
        and re.search(r"\bCode\b", text)
        and re.search(r"Issue/Sales", text, re.I)
        and re.search(r"Closing", text, re.I)
    )


def _is_swil_landscape_qty_value_doc(doc) -> bool:
    """True only for wide landscape SwilERP pages with the extra qty/value grid."""
    try:
        widths = [float(page.rect.width) for page in doc]
    except Exception:
        return False
    if not widths or max(widths) < _SWIL_LAND_MIN_WIDTH:
        return False
    return any(
        _is_swil_landscape_qty_value_text(page.get_text("text") or "") for page in doc
    )


def _is_swil_qty_value_pair_text(text: str) -> bool:
    """Sales & Stock with two-line Qty/Value pairs (Opening Bal / Issue/Sales / Closing Bala).

    Portrait pages often omit Code. This is header-based, not filename-based.
    """
    if not text:
        return False
    return bool(
        re.search(r"Sales\s*&\s*Stock", text, re.I)
        and re.search(r"Receipt/Pur", text, re.I)
        and re.search(r"\bPACKING\b", text)
        and re.search(r"Opening\s+Bal", text, re.I)
        and re.search(r"Issue/Sales", text, re.I)
        and re.search(r"Closing\s+Bala", text, re.I)
    )


def _swil_land_header_field(token: str) -> Optional[str]:
    norm = re.sub(r"[^a-z0-9/]", "", (token or "").lower())
    if norm == "code":
        return "code"
    if norm in {"product", "name"}:
        return "product"
    if norm == "packing":
        return "pack"
    if norm in {"op", "opening", "bal"}:
        return "op_group"
    if norm.startswith("receipt"):
        return "rec_group"
    if norm == "total":
        return "total_group"
    if norm.startswith("issue"):
        return "issue_group"
    if norm == "shortage":
        return "shortage_group"
    if norm in {"expiry", "breakage"}:
        return "expiry_group"
    if norm in {"closing", "bala"}:
        return "closing_group"
    if norm == "dump":
        return "dump_group"
    if norm in {"ne", "expi", "near"}:
        return "ne_group"
    return None


def _swil_land_sub_role(token: str) -> Optional[str]:
    norm = re.sub(r"[^a-z]", "", (token or "").lower())
    if norm in {"qty", "breakage", "stock", "expi"}:
        return "qty"
    if norm == "value":
        return "value"
    return None


def _swil_land_group_words(words: List[Any], y_tol: float = 3.0) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for w in sorted(words, key=lambda item: (round(item[1], 1), item[0])):
        x0, y0, x1, _y1, token = w[0], w[1], w[2], w[3], str(w[4]).strip()
        if not token:
            continue
        xc = (x0 + x1) / 2.0
        if rows and abs(y0 - rows[-1]["y"]) <= y_tol:
            rows[-1]["words"].append((x0, x1, xc, token))
        else:
            rows.append({"y": y0, "words": [(x0, x1, xc, token)]})
    return rows


def _swil_land_row_blob(row: Dict[str, Any]) -> str:
    return " ".join(tok for _x0, _x1, _xc, tok in row.get("words") or [])


def _swil_land_build_buckets(
    header_row: Dict[str, Any], sub_row: Dict[str, Any]
) -> Optional[List[Tuple[str, float, float]]]:
    """Build x-ranges from this page's header + Qty/Value sub-row (not page-fixed)."""
    groups: List[Dict[str, Any]] = []
    for x0, x1, xc, token in header_row.get("words") or []:
        field = _swil_land_header_field(token)
        if not field:
            continue
        if groups and groups[-1]["field"] == field:
            groups[-1]["x0"] = min(groups[-1]["x0"], x0)
            groups[-1]["x1"] = max(groups[-1]["x1"], x1)
            groups[-1]["centers"].append(xc)
        else:
            groups.append({"field": field, "x0": x0, "x1": x1, "centers": [xc]})
    if not any(g["field"] == "product" for g in groups):
        return None
    if not any(g["field"] == "op_group" for g in groups):
        return None

    group_by = {g["field"]: g for g in groups}
    assigned: Dict[str, List[Tuple[float, str]]] = {
        g["field"]: [] for g in groups if g["field"] in _SWIL_LAND_GROUP_QTY_VALUE
    }
    for _x0, _x1, xc, token in sub_row.get("words") or []:
        role = _swil_land_sub_role(token)
        if not role or not assigned:
            continue
        nearest = min(
            assigned.keys(),
            key=lambda name: abs(
                (group_by[name]["x0"] + group_by[name]["x1"]) / 2.0 - xc
            ),
        )
        assigned[nearest].append((xc, role))

    centers: List[Tuple[str, float]] = []
    for group in groups:
        field = group["field"]
        mid = sum(group["centers"]) / len(group["centers"])
        if field in {"code", "product", "pack"}:
            centers.append((field, mid))
            continue
        qty_name, val_name = _SWIL_LAND_GROUP_QTY_VALUE.get(field, (None, None))
        roles = sorted(assigned.get(field) or [], key=lambda item: item[0])
        qty_x = next((xc for xc, role in roles if role == "qty"), None)
        val_x = next((xc for xc, role in roles if role == "value"), None)
        if qty_name and qty_x is not None:
            centers.append((qty_name, qty_x))
        elif qty_name:
            centers.append((qty_name, group["x0"] + 4.0))
        if val_name and val_x is not None:
            centers.append((val_name, val_x))
        elif val_name:
            centers.append((val_name, group["x1"] - 4.0))

    if not any(name == "opening_qty" for name, _xc in centers):
        return None
    centers.sort(key=lambda item: item[1])
    buckets: List[Tuple[str, float, float]] = []
    for idx, (name, xc) in enumerate(centers):
        lo = 0.0 if idx == 0 else (centers[idx - 1][1] + xc) / 2.0
        hi = 10000.0 if idx == len(centers) - 1 else (xc + centers[idx + 1][1]) / 2.0
        buckets.append((name, lo, hi))
    return buckets


def _swil_land_bucket(x: float, buckets: List[Tuple[str, float, float]]) -> Optional[str]:
    for name, lo, hi in buckets:
        if lo <= x < hi:
            return name
    return None


def _swil_land_identity_fail_count(items: List[Dict[str, Any]]) -> int:
    fails = 0
    for item in items:
        opening = _to_float(item.get("opening_qty"))
        receipts = _to_float(item.get("receipts_qty"))
        sales = _to_float(item.get("sales_qty"))
        closing = _to_float(item.get("closing_qty"))
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        total = _to_float(extra.get("total_stock"))
        if total <= 0:
            total = opening + receipts
        if abs(round(total - sales, 2) - round(closing, 2)) > 0.51:
            fails += 1
    return fails


def _swil_fixed_bucket_result_is_weak(result: Dict[str, Any], doc) -> bool:
    """True when the narrow Swil x-buckets shifted columns on a landscape grid."""
    if not _is_swil_landscape_qty_value_doc(doc):
        return False
    items = result.get("line_items") or []
    if not items:
        return True
    code_in_name = 0
    for item in items:
        name = str(item.get("product_name") or "")
        if re.match(r"^(?:HIM|HM)\w*\s+\S", name, re.I):
            code_in_name += 1
    n = len(items)
    fails = _swil_land_identity_fail_count(items)
    return (fails / n) >= 0.4 or code_in_name >= max(2, n // 3) or n < 12


def _parse_swil_landscape_qty_value_statement(
    doc, filename: str, *, allow_portrait_pair: bool = False
) -> Optional[Dict[str, Any]]:
    """Parse SwilERP Sales & Stock using header-derived Qty/Value columns.

    Default path is the existing landscape Code/PACKING grid.
    allow_portrait_pair is only for Opening Bal / Issue/Sales / Closing Bala
    pages that the fixed portrait buckets mis-read. Landscape docs stay on
    the original detector so their results do not change.
    """
    if allow_portrait_pair:
        if _is_swil_landscape_qty_value_doc(doc):
            return None
        page_texts = [(page.get_text("text") or "") for page in doc]
        if not any(_is_swil_qty_value_pair_text(t) for t in page_texts):
            return None
    elif not _is_swil_landscape_qty_value_doc(doc):
        return None

    result = empty_result(filename, "pdf")
    items: List[Dict[str, Any]] = []
    buckets: Optional[List[Tuple[str, float, float]]] = None
    column_mapping: Dict[str, Any] = {}
    pages_used = 0

    for page in doc:
        pages_used += 1
        text = page.get_text("text") or ""
        if not result.get("report_title"):
            m = re.search(
                r"Sales\s*&\s*Stock\s*Statement\s*\(\s*From\s+"
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+Upto\s+"
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
                text,
                re.I,
            )
            if m:
                result["report_title"] = "Sales & Stock Statement"
                result["period_from"] = _normalize_date(m.group(1))
                result["period_to"] = _normalize_date(m.group(2))

        rows = _swil_land_group_words(page.get_text("words") or [])
        header_row = next(
            (
                row
                for row in rows
                if re.search(r"PRODUCT", _swil_land_row_blob(row), re.I)
                and re.search(r"PACKING", _swil_land_row_blob(row), re.I)
                and re.search(r"Opening|Receipt", _swil_land_row_blob(row), re.I)
            ),
            None,
        )
        sub_row = None
        if header_row is not None:
            header_idx = rows.index(header_row)
            for row in rows[header_idx + 1 : header_idx + 4]:
                blob = _swil_land_row_blob(row)
                if re.search(r"Qty\.?", blob, re.I) and re.search(r"Value", blob, re.I):
                    sub_row = row
                    break
            built = (
                _swil_land_build_buckets(header_row, sub_row)
                if sub_row is not None
                else None
            )
            if built:
                buckets = built
                if not column_mapping:
                    column_mapping = {
                        name: {"x0": lo, "x1": hi} for name, lo, hi in buckets
                    }

        if not result.get("stockist_name"):
            limit_y = header_row["y"] if header_row is not None else 120.0
            for row in rows:
                if row["y"] >= limit_y - 2:
                    break
                wide = _swil_land_row_blob(row).strip()
                if re.search(r"Page\s*No|Sales\s*&\s*Stock|PRODUCT", wide, re.I):
                    continue
                if not result["stockist_name"] and wide and not re.search(
                    r"HIMALAYA|W/NO", wide, re.I
                ):
                    result["stockist_name"] = _clean_name(wide)
                elif not result.get("stockist_address") and wide and not re.search(
                    r"HIMALAYA", wide, re.I
                ):
                    result["stockist_address"] = _clean_name(wide)
                elif not result.get("company_name") and re.search(r"HIMALAYA", wide, re.I):
                    result["company_name"] = _clean_name(
                        re.sub(r"\($", "", wide).strip()
                    )

        if not buckets:
            continue

        skip_y = 0.0
        if header_row is not None:
            skip_y = header_row["y"]
        if sub_row is not None:
            skip_y = max(skip_y, sub_row["y"]) + 8.0
        in_footer = False
        field_names = [name for name, _lo, _hi in buckets]

        for row in rows:
            if row["y"] <= skip_y:
                continue
            cells: Dict[str, List[str]] = {name: [] for name in field_names}
            for _x0, _x1, xc, token in row["words"]:
                bucket = _swil_land_bucket(xc, buckets)
                if bucket:
                    cells[bucket].append(token)
            blob = _swil_land_row_blob(row)
            name = _clean_name(" ".join(cells.get("product") or []))
            if re.search(r"GRAND\s*TOTAL|^TOTAL\b", blob, re.I):
                extra = result["totals"]["extra"]
                open_val = _prompt_cell_number(cells.get("opening_value") or [])
                rec_val = _prompt_cell_number(cells.get("receipts_value") or [])
                sale_val = _prompt_cell_number(cells.get("sales_value") or [])
                close_val = _prompt_cell_number(cells.get("closing_value") or [])
                if open_val is not None:
                    extra["opening_value"] = open_val
                if rec_val is not None:
                    extra["receipts_value"] = rec_val
                    result["totals"]["receipts_value"] = rec_val
                if sale_val is not None:
                    result["totals"]["sales_value"] = sale_val
                if close_val is not None:
                    result["totals"]["closing_value"] = close_val
                extra["total_row_source"] = "swil_landscape_grand_total"
                in_footer = True
                continue
            if in_footer or _SWIL_LAND_SKIP_RE.search(blob or ""):
                if re.search(r"Purchase\s+Invoice", blob, re.I):
                    in_footer = True
                continue

            code = _clean_name(" ".join(cells.get("code") or [])) or None
            code_ok = bool(code and _SWIL_LAND_CODE_RE.match(code.split()[0]))
            has_qty = any(
                _prompt_cell_number(cells.get(field) or []) is not None
                for field in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
            )
            if name and not has_qty and not code_ok and items:
                items[-1]["product_name"] = _clean_name(
                    str(items[-1].get("product_name") or "") + " " + name
                )
                continue
            if not code_ok and not (name and has_qty):
                continue
            if not name:
                continue

            item = empty_line_item()
            item["product_code"] = code
            item["product_name"] = name
            pack = " ".join(cells.get("pack") or []).strip()
            item["packing"] = pack or None
            item["opening_qty"] = _prompt_cell_number(cells.get("opening_qty") or []) or 0.0
            item["receipts_qty"] = _prompt_cell_number(cells.get("receipts_qty") or []) or 0.0
            item["sales_qty"] = _prompt_cell_number(cells.get("sales_qty") or []) or 0.0
            item["closing_qty"] = _prompt_cell_number(cells.get("closing_qty") or []) or 0.0
            sale_val = _prompt_cell_number(cells.get("sales_value") or [])
            close_val = _prompt_cell_number(cells.get("closing_value") or [])
            if sale_val is not None:
                item["sales_value"] = sale_val
            if close_val is not None:
                item["closing_value"] = close_val
            extra = item["extra"]
            open_val = _prompt_cell_number(cells.get("opening_value") or [])
            rec_val = _prompt_cell_number(cells.get("receipts_value") or [])
            if open_val is not None:
                extra["opening_value"] = open_val
            if rec_val is not None:
                extra["receipts_value"] = rec_val
                ordered: Dict[str, Any] = {}
                for key, val in item.items():
                    ordered[key] = val
                    if key == "receipts_qty":
                        ordered["receipts_value"] = rec_val
                item = ordered
                extra = item["extra"]
            total_qty = _prompt_cell_number(cells.get("total_qty") or [])
            if total_qty is not None:
                extra["total_stock"] = total_qty
            for extra_key, cell_key in (
                ("shortage_qty", "shortage_qty"),
                ("expiry_qty", "expiry_qty"),
                ("dump_qty", "dump_qty"),
                ("near_expiry_qty", "near_expiry"),
            ):
                number = _prompt_cell_number(cells.get(cell_key) or [])
                if number is not None:
                    extra[extra_key] = number
            items.append(item)

    if not items:
        return None

    valid = len(items) - _swil_land_identity_fail_count(items)
    extra = result["totals"]["extra"]
    extra["extraction_method"] = (
        "swil_qty_value_pair" if allow_portrait_pair else "swil_landscape_qty_value"
    )
    extra["detected_format"] = (
        "swil_qty_value_pair" if allow_portrait_pair else "swil_landscape_qty_value"
    )
    extra["source_type"] = "pdf_text"
    extra["vertex_ai_used"] = False
    extra["pages"] = pages_used
    extra["header_detected"] = result.get("report_title")
    extra["column_mapping"] = column_mapping
    extra["rows_detected"] = len(items)
    extra["valid_rows"] = valid
    extra["invalid_rows"] = len(items) - valid
    extra["header_detection_confidence"] = 1.0 if column_mapping else 0.0
    extra["validation_status"] = "pass" if valid == len(items) else "partial"
    extra["stock_identity_fail_count_pre"] = len(items) - valid
    result["line_items"] = items
    logger.info(
        "Swil qty/value statement %s method=%s pages=%s rows=%s valid=%s invalid=%s",
        filename,
        extra["extraction_method"],
        pages_used,
        len(items),
        valid,
        len(items) - valid,
    )
    return result


_CODE_ITEM_CODE_RE = re.compile(r"^\d{4,6}$")
_CODE_ITEM_SKIP_RE = re.compile(
    r"Stock\s+Stat(?:e)?ment|Item\s+Description|GANESH|Page\s+\d|"
    r"\bTOTALS?\b|^Batch\b|ExPDt|Stock\s*:|MRP\s*:|^\-+$",
    re.I,
)


def _is_code_item_stock_statement_text(text: str) -> bool:
    """Ganesh-style Stock Statement: Code / Item / Packing / Opening / Purchase."""
    if not text:
        return False
    return bool(
        re.search(r"Stock\s+Stat(?:e)?ment", text, re.I)
        and re.search(r"Item\s+Description", text, re.I)
        and re.search(r"\bPacking\b", text, re.I)
        and re.search(r"\bOpening\b", text, re.I)
        and re.search(r"\bPurchase\b", text, re.I)
        and re.search(r"Stock-Value", text, re.I)
        and re.search(r"Sales-Value", text, re.I)
    )


def _code_item_header_field(token: str) -> Optional[str]:
    norm = re.sub(r"[^a-z0-9]", "", (token or "").lower())
    if norm == "code":
        return "code"
    if norm in {"item", "description"}:
        return "product"
    if norm == "packing":
        return "pack"
    if norm == "opening":
        return "opening_qty"
    if norm == "purchase":
        return "receipts_qty"
    if norm == "sales":
        return "sales_qty"
    if norm == "closing":
        return "closing_qty"
    if norm == "stockvalue":
        return "closing_value"
    if norm == "salesvalue":
        return "sales_value"
    return None


def _code_item_build_buckets(
    header_row: Dict[str, Any],
) -> Optional[List[Tuple[str, float, float]]]:
    centers: List[Tuple[str, float]] = []
    for _x0, _x1, xc, token in header_row.get("words") or []:
        field = _code_item_header_field(token)
        if not field:
            continue
        if centers and centers[-1][0] == field:
            prev_name, prev_xc = centers[-1]
            centers[-1] = (prev_name, (prev_xc + xc) / 2.0)
        else:
            centers.append((field, xc))
    names = {name for name, _xc in centers}
    if "product" not in names or "opening_qty" not in names:
        return None
    centers.sort(key=lambda item: item[1])
    buckets: List[Tuple[str, float, float]] = []
    for idx, (name, xc) in enumerate(centers):
        lo = 0.0 if idx == 0 else (centers[idx - 1][1] + xc) / 2.0
        hi = 10000.0 if idx == len(centers) - 1 else (xc + centers[idx + 1][1]) / 2.0
        buckets.append((name, lo, hi))
    return buckets


def _code_item_bucket(
    x: float, buckets: List[Tuple[str, float, float]]
) -> Optional[str]:
    for name, lo, hi in buckets:
        if lo <= x < hi:
            return name
    return None


def _parse_code_item_stock_statement(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Parse Code/Item Description/Packing/Opening/Purchase/Sales/Closing PDFs."""
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_code_item_stock_statement_text(t) for t in page_texts):
        return None

    result = empty_result(filename, "pdf")
    items: List[Dict[str, Any]] = []
    buckets: Optional[List[Tuple[str, float, float]]] = None
    column_mapping: Dict[str, Any] = {}

    for page, text in zip(doc, page_texts):
        if not result.get("report_title"):
            m = re.search(
                r"Stock\s+Stat(?:e)?ment\s*:\s*(.+)$",
                text,
                re.I | re.M,
            )
            if m:
                result["report_title"] = "Stock Statement"
                company = _clean_name(m.group(1))
                company = re.sub(r"\s+\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\s*$", "", company)
                if company:
                    result["company_name"] = company
        if not result.get("period_to"):
            m_date = re.search(
                r"Stock\s+Stat(?:e)?ment[\s\S]{0,80}?(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
                text,
                re.I,
            )
            if m_date:
                result["period_to"] = _normalize_date(m_date.group(1))
        if not result.get("stockist_name"):
            for ln in text.splitlines():
                raw = ln.strip()
                if raw and not re.search(
                    r"Page\s+\d|Stock\s+Stat|Code\s+Item|^-+$", raw, re.I
                ):
                    result["stockist_name"] = _clean_name(raw)
                    break

        rows = _swil_land_group_words(page.get_text("words") or [], y_tol=4.0)
        header_row = next(
            (
                row
                for row in rows
                if re.search(r"Item", _swil_land_row_blob(row), re.I)
                and re.search(r"Packing", _swil_land_row_blob(row), re.I)
                and re.search(r"Opening", _swil_land_row_blob(row), re.I)
            ),
            None,
        )
        if header_row is not None:
            built = _code_item_build_buckets(header_row)
            if built:
                buckets = built
                if not column_mapping:
                    column_mapping = {
                        name: {"x0": lo, "x1": hi} for name, lo, hi in buckets
                    }
        if not buckets:
            continue

        field_names = [name for name, _lo, _hi in buckets]
        skip_y = header_row["y"] + 8.0 if header_row is not None else 0.0
        for row in rows:
            if row["y"] <= skip_y:
                continue
            blob = _swil_land_row_blob(row)
            if _CODE_ITEM_SKIP_RE.search(blob or ""):
                if re.search(r"\bTOTALS?\b", blob, re.I):
                    cells = {name: [] for name in field_names}
                    for _x0, _x1, xc, token in row["words"]:
                        bucket = _code_item_bucket(xc, buckets)
                        if bucket:
                            cells[bucket].append(token)
                    stock_val = _prompt_cell_number(cells.get("closing_value") or [])
                    sale_val = _prompt_cell_number(cells.get("sales_value") or [])
                    extra = result["totals"]["extra"]
                    if stock_val is not None:
                        result["totals"]["closing_value"] = stock_val
                    if sale_val is not None:
                        result["totals"]["sales_value"] = sale_val
                    extra["total_row_source"] = "code_item_totals"
                continue

            cells = {name: [] for name in field_names}
            for _x0, _x1, xc, token in row["words"]:
                bucket = _code_item_bucket(xc, buckets)
                if bucket:
                    cells[bucket].append(token)
            code = _clean_name(" ".join(cells.get("code") or []))
            name = _clean_name(" ".join(cells.get("product") or []))
            if not code or not _CODE_ITEM_CODE_RE.match(code):
                continue
            if not name or re.match(r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$", name):
                continue

            item = empty_line_item()
            item["product_code"] = code
            item["product_name"] = name
            pack = " ".join(cells.get("pack") or []).strip()
            item["packing"] = pack or None
            item["opening_qty"] = _prompt_cell_number(cells.get("opening_qty") or []) or 0.0
            item["receipts_qty"] = _prompt_cell_number(cells.get("receipts_qty") or []) or 0.0
            item["sales_qty"] = _prompt_cell_number(cells.get("sales_qty") or []) or 0.0
            item["closing_qty"] = _prompt_cell_number(cells.get("closing_qty") or []) or 0.0
            sale_val = _prompt_cell_number(cells.get("sales_value") or [])
            close_val = _prompt_cell_number(cells.get("closing_value") or [])
            if sale_val is not None:
                item["sales_value"] = sale_val
            if close_val is not None:
                item["closing_value"] = close_val
            items.append(item)

    if not items:
        return None
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "code_item_stock_statement"
    extra["column_mapping"] = column_mapping
    extra["rows_detected"] = len(items)
    extra["header_detected"] = result.get("report_title")
    extra["fallback_used"] = False
    logger.info(
        "Code/item stock statement %s rows=%s stockist=%s",
        filename,
        len(items),
        result.get("stockist_name"),
    )
    return result


_SALEABLE_ROW_RE = re.compile(
    r"^(?P<name>.+?)\s*\|\s*\|?\s*"
    r"(?P<opn>[\d.,]+|[—–\-]+)\s*\|\s*"
    r"(?P<rec>[\d.,]+|[—–\-]+)\s*\|\s*"
    r"(?P<issue>[\d.,]+|[—–\-]+)\s*\|\s*"
    r"(?P<bal>[\d.,]+|[—–\-]+)\s*$"
)
_SALEABLE_PACK_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<pack>(?:\d+\s*X\s*)?\d+(?:\.\d+)?\s*"
    r"(?:X\s*\d+\s*)?(?:TAB|CAPS?|GM|ML|S)?\.?)$",
    re.I,
)
_SALEABLE_SKIP_NAME_RE = re.compile(
    r"^(?:Particular|COMPANY(?:\s+Total)?|Firm\s+Total|Saleable|"
    r"Page\s+No|\(From|\(Q\+F\)|Tel\.|EMail|A\b)",
    re.I,
)


def _is_saleable_stock_report_text(text: str) -> bool:
    if not text:
        return False
    return bool(
        re.search(r"Saleable\s+Stock\s+Report", text, re.I)
        and re.search(r"\bParticular\b", text, re.I)
        and re.search(r"\bOpn\b", text, re.I)
        and re.search(r"\bIssue\b", text, re.I)
        and re.search(r"\bBal\b", text, re.I)
    )


def _saleable_qty(token: str) -> float:
    raw = str(token or "").strip()
    if not raw or re.fullmatch(r"[—–\-\s]+", raw):
        return 0.0
    return _to_float(raw)


def _saleable_split_packing(particular: str) -> Tuple[str, Optional[str]]:
    text = _clean_name(particular)
    glued = re.search(
        r"^(?P<name>.+?)(?P<pack>\d+\s*X\s*\d+\s*(?:TAB|CAPS?|GM|ML|S)?\.?)$",
        text,
        re.I,
    )
    if glued:
        name = _clean_name(glued.group("name"))
        pack = _clean_name(glued.group("pack"))
        if name and len(name) >= 3:
            return name, pack or None
    match = _SALEABLE_PACK_RE.match(text)
    if not match:
        return text, None
    name = _clean_name(match.group("name"))
    pack = _clean_name(match.group("pack"))
    if not name or len(name) < 3:
        return text, None
    return name, pack or None


def _parse_saleable_stock_report(
    text: str, filename: str, source_format: str = "pdf"
) -> Optional[Dict[str, Any]]:
    """Parse Saleable Stock Report (Particular | Opn | Rec | Issue | Bal)."""
    if not _is_saleable_stock_report_text(text):
        return None

    result = empty_result(filename, source_format)
    result["report_title"] = "Saleable Stock Report"
    items: List[Dict[str, Any]] = []

    for ln in text.splitlines():
        raw = ln.strip()
        if not raw:
            continue
        if not result.get("stockist_name") and re.search(
            r"MEDICO|AGENC|STORES|PHARMA|DISTRIBUT", raw, re.I
        ):
            result["stockist_name"] = _clean_name(raw)
            continue
        if not result.get("stockist_address") and re.search(
            r"ROAD|COMPLEX|NAGAR|SONEPAT|HOUSE", raw, re.I
        ):
            result["stockist_address"] = _clean_name(raw)
            continue
        m_period = re.search(
            r"\(From\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+To\s+"
            r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\)",
            raw,
            re.I,
        )
        if m_period:
            result["period_from"] = _normalize_date(m_period.group(1))
            result["period_to"] = _normalize_date(m_period.group(2))
            continue
        m_co = re.search(r"^COMPANY\s*:\s*(.+)$", raw, re.I)
        if m_co:
            company = re.sub(r"\s+\d+\s*$", "", m_co.group(1)).strip()
            result["company_name"] = _clean_name(company)
            continue

        if re.search(r"^(?:COMPANY|Firm)\s+Total\b", raw, re.I):
            nums = re.findall(r"-?\d+(?:\.\d+)?", raw)
            if len(nums) >= 4 and raw.count("|") >= 3:
                extra = result["totals"]["extra"]
                extra["opening_qty"] = _to_float(nums[-4])
                extra["receipts_qty"] = _to_float(nums[-3])
                extra["sales_qty"] = _to_float(nums[-2])
                extra["closing_qty"] = _to_float(nums[-1])
                extra["total_row_source"] = "saleable_company_total"
            continue
        if re.search(r"\|A\|", raw):
            nums = re.findall(r"-?\d+(?:\.\d+)?", raw)
            if len(nums) >= 4:
                extra = result["totals"]["extra"]
                extra["opening_value"] = _to_float(nums[-4])
                extra["receipts_value"] = _to_float(nums[-3])
                extra["printed_sales_value"] = _to_float(nums[-2])
                extra["printed_closing_value"] = _to_float(nums[-1])
            continue

        match = _SALEABLE_ROW_RE.match(raw)
        if not match:
            continue
        name = _clean_name(match.group("name"))
        if not name or _SALEABLE_SKIP_NAME_RE.search(name):
            continue
        name, pack = _saleable_split_packing(name)
        item = empty_line_item()
        item["product_name"] = name
        item["packing"] = pack
        item["opening_qty"] = _saleable_qty(match.group("opn"))
        item["receipts_qty"] = _saleable_qty(match.group("rec"))
        item["sales_qty"] = _saleable_qty(match.group("issue"))
        item["closing_qty"] = _saleable_qty(match.group("bal"))
        items.append(item)

    if not items:
        return None
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "saleable_stock_report"
    extra["layout"] = "saleable_opn_rec_issue_bal"
    extra["rows_detected"] = len(items)
    extra["fallback_used"] = False
    return result


def _parse_saleable_stock_report_doc(doc, filename: str) -> Optional[Dict[str, Any]]:
    text = "\n".join((page.get_text("text") or "") for page in doc)
    return _parse_saleable_stock_report(text, filename, "pdf")


_ORDER_FORM_NUM_RE = re.compile(r"-?\d+(?:,\d{3})*\.\d{2}")


def _is_order_form_stock_statement_text(text: str) -> bool:
    if not text:
        return False
    return bool(
        re.search(r"STOCK\s+STATEMENT", text, re.I)
        and re.search(r"ORDER\s+FORM", text, re.I)
        and re.search(r"PRODUCT\s+NAME", text, re.I)
        and re.search(r"\bPACK\b", text, re.I)
        and re.search(r"PURCH\.RET", text, re.I)
        and re.search(r"STOCK\s+VALUE", text, re.I)
    )


def _parse_order_form_stock_statement(
    text: str, filename: str, source_format: str = "pdf"
) -> Optional[Dict[str, Any]]:
    """Parse CODE / PRODUCT NAME / PACK / OPENING / PURCHASE / SALE / STOCK VALUE."""
    if not _is_order_form_stock_statement_text(text):
        return None

    result = empty_result(filename, source_format)
    result["report_title"] = "Stock Statement Order Form"
    items: List[Dict[str, Any]] = []
    seen: set = set()

    for ln in text.splitlines():
        raw = ln.replace("\xa0", " ").strip()
        if not raw or raw.startswith("="):
            continue
        if not result.get("stockist_name") and re.search(
            r"MEDICAL|AGENC|STORES|PHARMA|DISTRIBUT", raw, re.I
        ):
            if not re.search(r"STOCK\s+STATEMENT|FROM\s+DATE|PRODUCT\s+NAME", raw, re.I):
                result["stockist_name"] = _clean_name(raw)
                continue
        m_period = re.search(
            r"FROM\s+DATE\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+TO\s+"
            r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
            raw,
            re.I,
        )
        if m_period:
            result["period_from"] = _normalize_date(m_period.group(1))
            result["period_to"] = _normalize_date(m_period.group(2))
            continue
        m_co = re.search(r"STOCK\s+STATEMENT\s+ORDER\s+FORM\s+(.+)$", raw, re.I)
        if m_co:
            result["company_name"] = _clean_name(m_co.group(1))
            continue
        if re.match(r"^TOTAL\s+VALUE\b", raw, re.I):
            nums = _ORDER_FORM_NUM_RE.findall(raw)
            extra = result["totals"]["extra"]
            if len(nums) >= 6:
                extra["opening_value"] = _to_float(nums[0])
                extra["receipts_value"] = _to_float(nums[1])
                result["totals"]["sales_value"] = _to_float(nums[3])
                result["totals"]["closing_value"] = _to_float(nums[-1])
            elif len(nums) >= 2:
                result["totals"]["closing_value"] = _to_float(nums[-1])
            extra["total_row_source"] = "order_form_total_value"
            continue
        m_actual = re.search(r"ACTUAL\s+SALE\s+VALUE\s*=\s*([-\d,.]+)", raw, re.I)
        if m_actual:
            result["totals"]["extra"]["actual_sale_value"] = _to_float(m_actual.group(1))
            continue
        if not re.match(r"^\d{6}\s+\S", raw) or len(raw) < 50:
            continue
        code = raw[:6]
        if code in seen:
            continue
        name = _clean_name(raw[7:33])
        pack = _clean_name(raw[33:44]) or None
        nums = _ORDER_FORM_NUM_RE.findall(raw[44:] if len(raw) > 44 else "")
        if not name or len(nums) < 8:
            continue
        item = empty_line_item()
        item["product_code"] = code
        item["product_name"] = name
        item["packing"] = pack
        item["opening_qty"] = _to_float(nums[0])
        item["receipts_qty"] = _to_float(nums[1])
        purch_ret = _to_float(nums[2])
        item["sales_qty"] = _to_float(nums[3])
        item["sales_value"] = _to_float(nums[4])
        sale_ret = _to_float(nums[5])
        item["closing_qty"] = _to_float(nums[6])
        item["closing_value"] = _to_float(nums[7])
        extra = item.setdefault("extra", {})
        if isinstance(extra, dict):
            if purch_ret:
                extra["purchase_return_qty"] = purch_ret
            if sale_ret:
                extra["sale_return_qty"] = sale_ret
        items.append(item)
        seen.add(code)

    if not items:
        return None
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "order_form_stock_statement"
    extra["layout"] = "code_name_pack_opening_purchase_sale_stock"
    extra["rows_detected"] = len(items)
    extra["fallback_used"] = False
    return result


def _parse_order_form_stock_statement_doc(doc, filename: str) -> Optional[Dict[str, Any]]:
    text = "\n".join((page.get_text("text") or "") for page in doc)
    return _parse_order_form_stock_statement(text, filename, "pdf")


_SSA_ORI_TOKEN_RE = re.compile(r"^(?:-|\d+(?:\.\d+)?)$")
_SSA_ORI_PACK_RE = re.compile(
    r"^(?:\d+\s*\*\s*\d+|\d+X\d+|\d+(?:\.\d+)?(?:ML|GM|GMS|TAB|CC|S)?)$",
    re.I,
)


def _is_ssa_opening_receipt_issue_value_text(text: str) -> bool:
    """STOCK & SALES ANALYSIS with Opening/Receipt/Issue/Closing qty+value + DUMP."""
    if not text:
        return False
    if not re.search(r"STOCK\s*&\s*SALES\s*ANALYSIS", text, re.I):
        return False
    if not re.search(r"ITEM\s+DESCRIPTION", text, re.I):
        return False
    if not re.search(r"\bRECEIPT\b", text, re.I):
        return False
    if not re.search(r"\bISSUE\b", text, re.I):
        return False
    if not re.search(r"\bCLOSING\b", text, re.I):
        return False
    if not re.search(r"\bDUMP\b", text, re.I):
        return False
    if not re.search(r"\bVALUE\b", text, re.I):
        return False
    if (
        re.search(r"\bPurchases\b", text)
        and re.search(r"\bOthers\b", text)
        and re.search(r"\bRate\b", text)
    ):
        return False
    return True


def _ssa_ori_number(token: str) -> float:
    raw = str(token or "").strip()
    if raw in {"", "-", "—", "--"}:
        return 0.0
    return _to_float(raw)


def _ssa_ori_split_pack(name: str) -> Tuple[str, Optional[str]]:
    parts = _clean_name(name).split()
    if len(parts) >= 2 and parts[-1].upper() in {"ML", "GM", "GMS"} and re.fullmatch(
        r"\d+(?:\.\d+)?", parts[-2]
    ):
        return _clean_name(" ".join(parts[:-2])), f"{parts[-2]} {parts[-1]}"
    if len(parts) >= 2 and _SSA_ORI_PACK_RE.match(parts[-1]):
        last = parts[-1]
        if last.isdigit() and len(last) > 3:
            return _clean_name(name), None
        return _clean_name(" ".join(parts[:-1])), last
    return _clean_name(name), None


def _parse_ssa_opening_receipt_issue_value(
    text: str, filename: str, source_format: str = "pdf"
) -> Optional[Dict[str, Any]]:
    """Parse STOCK & SALES ANALYSIS Opening/Receipt/Issue/Closing qty+value."""
    if not _is_ssa_opening_receipt_issue_value_text(text):
        return None

    result = empty_result(filename, source_format)
    result["report_title"] = "STOCK & SALES ANALYSIS"
    items: List[Dict[str, Any]] = []
    in_purchase_detail = False

    for ln in text.splitlines():
        raw = ln.replace("\xa0", " ").rstrip()
        stripped = raw.strip()
        if not stripped or set(stripped) <= {"-", "="}:
            continue
        if re.match(r"^PURCHASE\s+DETAIL", stripped, re.I):
            in_purchase_detail = True
            continue
        if in_purchase_detail:
            continue
        if re.search(r"STOCK\s*&\s*SALES\s*ANALYSIS", stripped, re.I):
            m_period = re.search(
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*[-–]\s*"
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
                stripped,
            )
            if m_period:
                result["period_from"] = _normalize_date(m_period.group(1))
                result["period_to"] = _normalize_date(m_period.group(2))
            continue
        if not result.get("stockist_name") and re.search(
            r"MEDICAL|STORE|AGENC|MEDICO|PHARMA", stripped, re.I
        ):
            if not re.search(r"STOCK\s*&\s*SALES|ITEM\s+DESCRIPTION", stripped, re.I):
                result["stockist_name"] = _clean_name(stripped)
            continue
        if re.search(
            r"Phone\s*:|E-Mail|GSTIN|D\.L\.No|ITEM\s+DESCRIPTION|^QTY\.|"
            r"SADAR|SUPPLIER\s+NAME",
            stripped,
            re.I,
        ):
            continue
        if re.match(r"^TOTAL\b", stripped, re.I):
            tokens = stripped.split()
            nums = [t for t in tokens[1:] if _SSA_ORI_TOKEN_RE.match(t)]
            if len(nums) >= 8:
                extra = result["totals"]["extra"]
                extra["opening_qty"] = _ssa_ori_number(nums[0])
                extra["opening_value"] = _ssa_ori_number(nums[1])
                extra["receipts_qty"] = _ssa_ori_number(nums[2])
                extra["receipts_value"] = _ssa_ori_number(nums[3])
                extra["sales_qty"] = _ssa_ori_number(nums[4])
                result["totals"]["sales_value"] = _ssa_ori_number(nums[5])
                extra["closing_qty"] = _ssa_ori_number(nums[6])
                result["totals"]["closing_value"] = _ssa_ori_number(nums[7])
                extra["total_row_source"] = "ssa_ori_grand_total"
            continue

        tokens = stripped.split()
        if len(tokens) < 10:
            if (
                not result.get("company_name")
                and re.search(r"HIMALAYA|HIMALYA", stripped, re.I)
                and not re.search(r"\d{2,}", stripped)
            ):
                result["company_name"] = _clean_name(stripped)
            continue
        tail = tokens[-9:]
        if not all(_SSA_ORI_TOKEN_RE.match(tok) for tok in tail):
            continue
        name = " ".join(tokens[:-9]).strip()
        if not name or _ssa_skip_product(name) or re.match(r"^TOTAL\b", name, re.I):
            continue
        name, pack = _ssa_ori_split_pack(name)
        if not name:
            continue
        item = empty_line_item()
        item["product_name"] = name
        item["packing"] = pack
        item["opening_qty"] = _ssa_ori_number(tail[0])
        item["receipts_qty"] = _ssa_ori_number(tail[2])
        item["sales_qty"] = _ssa_ori_number(tail[4])
        item["sales_value"] = _ssa_ori_number(tail[5])
        item["closing_qty"] = _ssa_ori_number(tail[6])
        item["closing_value"] = _ssa_ori_number(tail[7])
        extra = item.setdefault("extra", {})
        if isinstance(extra, dict):
            extra["opening_value"] = _ssa_ori_number(tail[1])
            extra["receipts_value"] = _ssa_ori_number(tail[3])
            extra["purchase_value"] = _ssa_ori_number(tail[3])
            extra["dump_qty"] = _ssa_ori_number(tail[8])
        items.append(item)

    if not items:
        return None
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "ssa_opening_receipt_issue_value"
    extra["layout"] = "ssa_opening_receipt_issue_value"
    extra["rows_detected"] = len(items)
    extra["fallback_used"] = False
    return result


def _parse_ssa_opening_receipt_issue_value_doc(doc, filename: str) -> Optional[Dict[str, Any]]:
    text = "\n".join((page.get_text("text") or "") for page in doc)
    return _parse_ssa_opening_receipt_issue_value(text, filename, "pdf")


_SSA_MEXP_DATE_RE = re.compile(r"^\d{1,2}/\d{2}$")
_SSA_MEXP_BUCKETS = (
    ("name", 0.0, 172.0),
    ("pack", 172.0, 238.0),
    ("opening_qty", 238.0, 290.0),
    ("opening_value", 290.0, 353.0),
    ("receipts_qty", 353.0, 406.0),
    ("receipts_value", 406.0, 463.0),
    ("sales_qty", 463.0, 514.0),
    ("sales_value", 514.0, 571.0),
    ("closing_qty", 571.0, 622.0),
    ("closing_value", 622.0, 678.0),
    ("dump_qty", 678.0, 708.0),
    ("mexp", 708.0, 760.0),
)


def _is_ssa_mexp_stock_sales_text(text: str) -> bool:
    """Landscape STOCK & SALES ANALYSIS with DUMP + M.EXP (not Prakash dash-DUMP)."""
    if not _is_ssa_opening_receipt_issue_value_text(text):
        return False
    return bool(re.search(r"\bM\.EXP\b", text, re.I))


def _ssa_mexp_cell_number(tokens: List[str]) -> Optional[float]:
    cleaned = [str(t).strip() for t in tokens if str(t).strip()]
    if cleaned and all(tok in {"-", "—", "--"} for tok in cleaned):
        return 0.0
    return _prompt_cell_number(cleaned)


def _ssa_mexp_row_cells(row: Dict[str, Any]) -> Dict[str, List[str]]:
    cells: Dict[str, List[str]] = {name: [] for name, _lo, _hi in _SSA_MEXP_BUCKETS}
    for _x0, _x1, xc, token in row.get("words") or []:
        for name, lo, hi in _SSA_MEXP_BUCKETS:
            if lo <= xc < hi:
                cells[name].append(token)
                break
    return cells


def _ssa_mexp_apply_total_tokens(result: Dict[str, Any], nums: List[str]) -> None:
    if len(nums) < 8:
        return
    extra = result["totals"]["extra"]
    extra["opening_qty"] = _ssa_ori_number(nums[0])
    extra["opening_value"] = _ssa_ori_number(nums[1])
    extra["receipts_qty"] = _ssa_ori_number(nums[2])
    extra["receipts_value"] = _ssa_ori_number(nums[3])
    extra["sales_qty"] = _ssa_ori_number(nums[4])
    result["totals"]["sales_value"] = _ssa_ori_number(nums[5])
    extra["closing_qty"] = _ssa_ori_number(nums[6])
    result["totals"]["closing_value"] = _ssa_ori_number(nums[7])
    extra["total_row_source"] = "ssa_mexp_grand_total"


def _ssa_mexp_fill_item(name: str, pack: Optional[str], nums: List[str]) -> Dict[str, Any]:
    item = empty_line_item()
    item["product_name"] = name
    item["packing"] = pack
    item["opening_qty"] = _ssa_ori_number(nums[0])
    item["receipts_qty"] = _ssa_ori_number(nums[2])
    item["sales_qty"] = _ssa_ori_number(nums[4])
    item["sales_value"] = _ssa_ori_number(nums[5])
    item["closing_qty"] = _ssa_ori_number(nums[6])
    item["closing_value"] = _ssa_ori_number(nums[7])
    extra = item.setdefault("extra", {})
    if isinstance(extra, dict):
        extra["opening_value"] = _ssa_ori_number(nums[1])
        extra["receipts_value"] = _ssa_ori_number(nums[3])
        extra["purchase_value"] = _ssa_ori_number(nums[3])
        extra["dump_qty"] = _ssa_ori_number(nums[8]) if len(nums) > 8 else 0.0
    return item


def _parse_ssa_mexp_stock_sales(
    text: str, filename: str, source_format: str = "pdf"
) -> Optional[Dict[str, Any]]:
    """Text fallback: strip M.EXP dates, then last 9 Opening/Receipt/Issue/Closing tokens."""
    if not _is_ssa_mexp_stock_sales_text(text):
        return None

    result = empty_result(filename, source_format)
    result["report_title"] = "STOCK & SALES ANALYSIS"
    items: List[Dict[str, Any]] = []

    for ln in text.splitlines():
        stripped = ln.replace("\xa0", " ").strip()
        if not stripped or set(stripped) <= {"-", "="}:
            continue
        if re.search(r"STOCK\s*&\s*SALES\s*ANALYSIS", stripped, re.I):
            m_period = re.search(
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*[-–]\s*"
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
                stripped,
            )
            if m_period:
                result["period_from"] = _normalize_date(m_period.group(1))
                result["period_to"] = _normalize_date(m_period.group(2))
            continue
        if not result.get("stockist_name") and re.search(
            r"MEDICAL|STORE|AGENC|MEDICO|PHARMA", stripped, re.I
        ):
            if not re.search(r"STOCK\s*&\s*SALES|ITEM\s+DESCRIPTION", stripped, re.I):
                result["stockist_name"] = _clean_name(stripped)
            continue
        if re.search(
            r"Phone\s*:|E-Mail|GSTIN|D\.L\.No|ITEM\s+DESCRIPTION|^QTY\.|"
            r"Continued|Page\s+No",
            stripped,
            re.I,
        ):
            continue
        if re.match(r"^TOTAL\b", stripped, re.I):
            tokens = stripped.split()
            if tokens and _SSA_MEXP_DATE_RE.match(tokens[-1]):
                tokens = tokens[:-1]
            nums = [t for t in tokens[1:] if _SSA_ORI_TOKEN_RE.match(t)]
            _ssa_mexp_apply_total_tokens(result, nums)
            continue
        tokens = stripped.split()
        if tokens and _SSA_MEXP_DATE_RE.match(tokens[-1]):
            tokens = tokens[:-1]
        if len(tokens) < 10:
            if (
                not result.get("company_name")
                and re.search(r"HIMALAYA|HIMALYA", stripped, re.I)
                and not re.search(r"\d{2,}", stripped)
            ):
                result["company_name"] = _clean_name(stripped)
            continue
        tail = tokens[-9:]
        if not all(_SSA_ORI_TOKEN_RE.match(tok) for tok in tail):
            continue
        name = " ".join(tokens[:-9]).strip()
        if not name or _ssa_skip_product(name) or re.match(r"^TOTAL\b", name, re.I):
            continue
        name, pack = _ssa_ori_split_pack(name)
        if not name:
            continue
        items.append(_ssa_mexp_fill_item(name, pack, tail))

    if not items:
        return None
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "ssa_mexp_stock_sales"
    extra["layout"] = "ssa_opening_receipt_issue_value_mexp"
    extra["rows_detected"] = len(items)
    extra["fallback_used"] = False
    return result


def _parse_ssa_mexp_stock_sales_doc(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Word-position parser for landscape SSA with M.EXP (avoids page-break text cuts)."""
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_ssa_mexp_stock_sales_text(t) for t in page_texts):
        return None

    result = empty_result(filename, "pdf")
    result["report_title"] = "STOCK & SALES ANALYSIS"
    items: List[Dict[str, Any]] = []

    for page, text in zip(doc, page_texts):
        if not result.get("stockist_name"):
            for ln in text.splitlines():
                raw = ln.strip()
                if re.search(r"MEDICAL|STORE|AGENC|MEDICO|PHARMA", raw, re.I) and not re.search(
                    r"STOCK\s*&\s*SALES|ITEM\s+DESCRIPTION", raw, re.I
                ):
                    result["stockist_name"] = _clean_name(raw)
                    break
        m_period = re.search(
            r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*[-–]\s*"
            r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
            text,
        )
        if m_period:
            result["period_from"] = _normalize_date(m_period.group(1))
            result["period_to"] = _normalize_date(m_period.group(2))
        if not result.get("company_name"):
            m_co = re.search(r"^(HIMALAYA\b[^\d\n]{0,40})$", text, re.I | re.M)
            if m_co:
                result["company_name"] = _clean_name(m_co.group(1))

        rows = _swil_land_group_words(page.get_text("words") or [], y_tol=2.8)
        for row in rows:
            blob = _swil_land_row_blob(row)
            if re.search(
                r"ITEM\s+DESCRIPTION|^QTY\.|STOCK\s*&\s*SALES|Continued|Page\s+No|"
                r"Phone\s*:|E-Mail|GSTIN",
                blob,
                re.I,
            ):
                continue
            if re.match(r"^TOTAL\b", blob.strip(), re.I):
                cells = _ssa_mexp_row_cells(row)
                nums = []
                for key in (
                    "opening_qty",
                    "opening_value",
                    "receipts_qty",
                    "receipts_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                    "dump_qty",
                ):
                    n = _ssa_mexp_cell_number(cells.get(key) or [])
                    if n is not None:
                        nums.append(str(n))
                _ssa_mexp_apply_total_tokens(result, nums)
                continue
            cells = _ssa_mexp_row_cells(row)
            name = _clean_name(" ".join(cells.get("name") or []))
            pack = _clean_name(" ".join(cells.get("pack") or []))
            if not name:
                continue
            if re.search(r"HIMALAYA|HIMALYA", name, re.I) and _ssa_mexp_cell_number(
                cells.get("opening_qty") or []
            ) is None:
                if not result.get("company_name"):
                    result["company_name"] = name
                continue
            if _ssa_skip_product(name) or re.match(r"^TOTAL\b", name, re.I):
                continue
            qty_hits = sum(
                1
                for key in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
                if _ssa_mexp_cell_number(cells.get(key) or []) is not None
            )
            if qty_hits < 3:
                continue
            if not pack:
                name, pack = _ssa_ori_split_pack(name)
            nums = [
                str(_ssa_mexp_cell_number(cells.get(key) or []) or 0.0)
                for key in (
                    "opening_qty",
                    "opening_value",
                    "receipts_qty",
                    "receipts_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                    "dump_qty",
                )
            ]
            items.append(_ssa_mexp_fill_item(name, pack or None, nums))
            extra_item = items[-1].setdefault("extra", {})
            if isinstance(extra_item, dict):
                mexp = " ".join(cells.get("mexp") or []).strip()
                if mexp:
                    extra_item["m_exp"] = mexp

    if not items:
        return None
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "ssa_mexp_stock_sales"
    extra["layout"] = "ssa_opening_receipt_issue_value_mexp"
    extra["rows_detected"] = len(items)
    extra["fallback_used"] = False
    return result


_VED_STOCK_BUCKETS = (
    ("name", 0.0, 150.0),
    ("pack", 150.0, 205.0),
    ("opening_qty", 205.0, 247.0),
    ("receipts_qty", 247.0, 289.0),
    ("sale_return", 289.0, 337.0),
    ("sales_qty", 337.0, 368.0),
    ("misc_out", 368.0, 409.0),
    ("closing_qty", 409.0, 458.0),
    ("closing_value", 458.0, 530.0),
    ("sales_value", 530.0, 700.0),
)
_VED_SKIP_NAME_RE = re.compile(
    r"^(GEN|TOTAL|GRAND|PAGE|PARTICULARS|PKG|OPEN|PURCH|SALES|CLOSE|"
    r"STOCK|VALUE|COMPANY|REPORT|LIST|PRODUCT|BATCHNO)\b",
    re.I,
)


def _is_ved_stock_sales_statement_text(text: str) -> bool:
    if not text:
        return False
    return bool(
        re.search(r"Stock\s+and\s+sales\s+Statement", text, re.I)
        and re.search(r"\bParticulars\b", text, re.I)
        and re.search(r"Purch\.?\s*Qty", text, re.I)
        and re.search(r"Close\s*Stock", text, re.I)
        and re.search(r"Sales\s*&\s*DC", text, re.I)
        and re.search(r"\bPkg\.?\b", text, re.I)
    )


def _ved_bucket(x: float) -> Optional[str]:
    for name, lo, hi in _VED_STOCK_BUCKETS:
        if lo <= x < hi:
            return name
    return None


def _ved_cell_number(tokens: List[str]) -> Optional[float]:
    cleaned = [str(t).strip() for t in tokens if str(t).strip()]
    if cleaned and all(tok in {"-", "—", "--"} for tok in cleaned):
        return 0.0
    return _prompt_cell_number(cleaned)


def _ved_row_cells(row: Dict[str, Any]) -> Dict[str, List[str]]:
    cells: Dict[str, List[str]] = {name: [] for name, _lo, _hi in _VED_STOCK_BUCKETS}
    for _x0, _x1, xc, token in row.get("words") or []:
        bucket = _ved_bucket(xc)
        if bucket:
            cells[bucket].append(token)
    return cells


def _ved_row_has_qty(cells: Dict[str, List[str]]) -> bool:
    for key in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
        if _ved_cell_number(cells.get(key) or []) is not None:
            return True
    return False


def _parse_ved_stock_sales_statement(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Parse VED-style Particulars / Open Qty / Purch Qty / Sales & DC / Close Stock."""
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_ved_stock_sales_statement_text(t) for t in page_texts):
        return None

    result = empty_result(filename, "pdf")
    result["report_title"] = "Stock and sales Statement"
    items: List[Dict[str, Any]] = []

    for page, text in zip(doc, page_texts):
        if not result.get("stockist_name"):
            for ln in text.splitlines():
                raw = ln.strip()
                if re.search(r"PRIVATE\s+LIMITED|MEDISALES|AGENC|STORES|MEDICO", raw, re.I):
                    if not re.search(r"Stock\s+and\s+sales|Particulars|Company\s*:", raw, re.I):
                        result["stockist_name"] = _clean_name(raw)
                        break
        if not result.get("stockist_address"):
            m_addr = re.search(r"(PLOT\.?NO\.?.+)", text, re.I)
            if m_addr:
                result["stockist_address"] = _clean_name(m_addr.group(1))
        m_period = re.search(
            r"from\s*:\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+to\s+"
            r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
            text,
            re.I,
        )
        if m_period:
            result["period_from"] = _normalize_date(m_period.group(1))
            result["period_to"] = _normalize_date(m_period.group(2))
        m_co = re.search(r"Company\s*:\s*(.+?)(?:\s+Report\s+type|\s+Page\s*:|$)", text, re.I)
        if m_co:
            result["company_name"] = _clean_name(m_co.group(1))

        rows = _swil_land_group_words(page.get_text("words") or [], y_tol=4.5)
        header_y = 0.0
        in_expiry = False
        pending_name = ""
        skip_idx: set = set()
        for idx, row in enumerate(rows):
            if idx in skip_idx:
                continue
            blob = _swil_land_row_blob(row)
            if re.search(r"List\s+of\s+Items\s+with\s+expiry", blob, re.I):
                in_expiry = True
                continue
            if in_expiry:
                continue
            if re.search(r"Particulars", blob, re.I) and re.search(r"Pkg", blob, re.I):
                header_y = max(header_y, row["y"])
                continue
            if re.search(r"Close\s*Stock|Purch\.?\s*Qty|Sales\s*&\s*DC", blob, re.I):
                header_y = max(header_y, row["y"])
                continue
            if re.search(
                r"Company\s*:|Page\s*:|Report\s+type|Stock\s+and\s+sales|"
                r"Bill\s+No|Gross\s+Amount|Net\s+Amount",
                blob,
                re.I,
            ):
                continue
            if row["y"] <= header_y + 6:
                continue
            cells = _ved_row_cells(row)
            name = _clean_name(" ".join(cells.get("name") or []))
            close_val = _ved_cell_number(cells.get("closing_value") or [])
            sale_val = _ved_cell_number(cells.get("sales_value") or [])
            is_grand = bool(re.search(r"Grand\s*Total", blob, re.I) or re.search(r"Grand\s*Total", name, re.I))
            is_money_footer = (
                not name
                and close_val is not None
                and sale_val is not None
                and close_val > 100
                and sale_val > 100
            )
            if is_grand or is_money_footer:
                if close_val:
                    result["totals"]["closing_value"] = close_val
                if sale_val:
                    result["totals"]["sales_value"] = sale_val
                result["totals"]["extra"]["total_row_source"] = "ved_grand_total"
                continue
            if re.search(r"Grand\s*Total", blob, re.I) or re.search(r"Grand\s*Total", name, re.I):
                continue
            has_qty = _ved_row_has_qty(cells)
            if not has_qty:
                if name and not _VED_SKIP_NAME_RE.search(name):
                    pending_name = _clean_name(f"{pending_name} {name}")
                continue

            follow = []
            for nidx, nxt in enumerate(rows[idx + 1 : idx + 3], start=idx + 1):
                if nxt["y"] - row["y"] > 14:
                    break
                nxt_cells = _ved_row_cells(nxt)
                if _ved_row_has_qty(nxt_cells):
                    break
                extra_name = _clean_name(" ".join(nxt_cells.get("name") or []))
                if extra_name and not _VED_SKIP_NAME_RE.search(extra_name):
                    follow.append(extra_name)
                    skip_idx.add(nidx)
            name = _clean_name(" ".join([pending_name, name] + follow))
            pending_name = ""
            if not name or _VED_SKIP_NAME_RE.search(name):
                continue

            item = empty_line_item()
            item["product_name"] = name
            pack = " ".join(cells.get("pack") or []).strip()
            item["packing"] = pack or None
            item["opening_qty"] = _ved_cell_number(cells.get("opening_qty") or []) or 0.0
            item["receipts_qty"] = _ved_cell_number(cells.get("receipts_qty") or []) or 0.0
            item["sales_qty"] = _ved_cell_number(cells.get("sales_qty") or []) or 0.0
            item["closing_qty"] = _ved_cell_number(cells.get("closing_qty") or []) or 0.0
            sale_val = _ved_cell_number(cells.get("sales_value") or [])
            close_val = _ved_cell_number(cells.get("closing_value") or [])
            if sale_val is not None:
                item["sales_value"] = sale_val
            if close_val is not None:
                item["closing_value"] = close_val
            extra = item.setdefault("extra", {})
            if isinstance(extra, dict):
                ret = _ved_cell_number(cells.get("sale_return") or [])
                misc = _ved_cell_number(cells.get("misc_out") or [])
                if ret:
                    extra["sale_return_qty"] = ret
                if misc:
                    extra["misc_out_qty"] = misc
            items.append(item)

    if not items:
        return None
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "ved_stock_sales_statement"
    extra["layout"] = "particulars_open_purch_sales_dc_close"
    extra["rows_detected"] = len(items)
    extra["fallback_used"] = False
    return result


_DAXIN_DEFAULT_BUCKETS = (
    ("name", 0.0, 175.0),
    ("pack", 175.0, 230.0),
    ("opening_qty", 230.0, 280.0),
    ("receipts_qty", 280.0, 335.0),
    ("total_stock", 335.0, 385.0),
    ("sales_qty", 385.0, 435.0),
    ("closing_qty", 435.0, 490.0),
    ("closing_value", 490.0, 543.0),
    ("age", 543.0, 620.0),
)
_DAXIN_SKIP_NAME_RE = re.compile(
    r"^(DAXINSOFT|PROFITMAKER|COMPANY|PRODUCT|PACKING|OPENING|PURCHASE|"
    r"CLOSING|SALE\s+VALUE|GENERATED|PAGE|STOCK)\b",
    re.I,
)


def _is_daxinsoft_stock_sales_text(text: str) -> bool:
    """Profitmaker Stock & Sales Statement: O.Stk / Purc / Tot / Sale / Qoh / Age."""
    if not text:
        return False
    if re.search(r"\bParticulars\b", text, re.I) and re.search(r"Purch\.?\s*Qty", text, re.I):
        return False
    if re.search(r"STOCK\s*&\s*SALES\s*ANALYSIS", text, re.I):
        return False
    if re.search(r"Saleable\s+Stock\s+Report", text, re.I):
        return False
    return bool(
        re.search(r"Stock\s*&\s*Sales\s*Statement", text, re.I)
        and re.search(r"Product\s+Name", text, re.I)
        and re.search(r"\bPacking\b", text, re.I)
        and re.search(r"O\.Stk", text, re.I)
        and re.search(r"\bPurc\b", text, re.I)
        and re.search(r"\bQoh\b", text, re.I)
        and re.search(r"\bAge\b", text, re.I)
    )


def _daxin_header_field(token: str) -> Optional[str]:
    norm = re.sub(r"[^a-z0-9.]", "", (token or "").lower())
    return {
        "product": "name",
        "name": "name",
        "packing": "pack",
        "ostk": "opening_qty",
        "o.stk": "opening_qty",
        "purc": "receipts_qty",
        "tot": "total_stock",
        "sale": "sales_qty",
        "qoh": "closing_qty",
        "value": "closing_value",
        "age": "age",
    }.get(norm)


def _daxin_buckets_from_header(row: Dict[str, Any]) -> Optional[List[Tuple[str, float, float]]]:
    found: Dict[str, Tuple[float, float]] = {}
    for x0, x1, _xc, token in row.get("words") or []:
        field = _daxin_header_field(token)
        if not field:
            continue
        if field in found:
            found[field] = (min(found[field][0], x0), max(found[field][1], x1))
        else:
            found[field] = (x0, x1)
    needed = {"name", "pack", "opening_qty", "sales_qty", "closing_qty", "closing_value"}
    if not needed.issubset(found):
        return None
    ordered = sorted(found.items(), key=lambda item: item[1][0])
    buckets: List[Tuple[str, float, float]] = []
    for idx, (field, (x0, _x1)) in enumerate(ordered):
        lo = 0.0 if idx == 0 else max(0.0, x0 - 2.0)
        if idx + 1 < len(ordered):
            hi = ordered[idx + 1][1][0] - 2.0
        else:
            hi = 700.0
        if hi <= lo:
            hi = lo + 8.0
        buckets.append((field, lo, hi))
    if buckets and buckets[0][0] == "name":
        buckets[0] = ("name", 0.0, buckets[0][2])
    return buckets


def _daxin_cell_number(tokens: List[str]) -> Optional[float]:
    cleaned = [str(t).strip() for t in tokens if str(t).strip()]
    if cleaned and all(tok in {"-", "—", "--"} for tok in cleaned):
        return 0.0
    return _prompt_cell_number(cleaned)


def _daxin_row_cells(
    row: Dict[str, Any], buckets: List[Tuple[str, float, float]]
) -> Dict[str, List[str]]:
    cells: Dict[str, List[str]] = {name: [] for name, _lo, _hi in buckets}
    for _x0, _x1, xc, token in row.get("words") or []:
        for name, lo, hi in buckets:
            if lo <= xc < hi:
                cells[name].append(token)
                break
    return cells


def _parse_daxinsoft_stock_sales_statement(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Parse Profitmaker O.Stk / Purc / Sale / Qoh / Value (Age is not money)."""
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_daxinsoft_stock_sales_text(t) for t in page_texts):
        return None

    result = empty_result(filename, "pdf")
    result["report_title"] = "Stock & Sales Statement"
    items: List[Dict[str, Any]] = []

    for page, text in zip(doc, page_texts):
        if not result.get("stockist_name"):
            for ln in text.splitlines():
                raw = ln.strip()
                if not raw or len(raw) > 80:
                    continue
                if re.search(r"DAXINSOFT|PROFITMAKER|Stock\s*&|Product\s+Name|Company\s*:", raw, re.I):
                    continue
                if re.search(r"AGEN|STORES|MEDICO|DISTRIBUT|MEDICAL", raw, re.I):
                    result["stockist_name"] = _clean_name(raw)
                    break
        m_period = re.search(
            r"From\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+To\s+"
            r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
            text,
            re.I,
        )
        if m_period:
            result["period_from"] = _normalize_date(m_period.group(1))
            result["period_to"] = _normalize_date(m_period.group(2))
        m_co = re.search(r"Company\s*:?\s*([A-Z][A-Z0-9 .&()-]+)", text, re.I)
        if m_co:
            result["company_name"] = _clean_name(m_co.group(1))
        m_sale = re.search(r"Sale\s+Value\s*:\s*([\d,.]+)", text, re.I)
        m_close = re.search(r"Closing\s+Value\s*:\s*([\d,.]+)", text, re.I)
        if m_sale:
            result["totals"]["sales_value"] = _to_float(m_sale.group(1))
        if m_close:
            result["totals"]["closing_value"] = _to_float(m_close.group(1))
        if m_sale or m_close:
            result["totals"]["extra"]["total_row_source"] = "daxinsoft_footer"
        m_open = re.search(r"Opening\s+Value\s*:\s*([\d,.]+)", text, re.I)
        m_pur = re.search(r"Purchase\s+Value\s*:\s*([\d,.]+)", text, re.I)
        extra = result["totals"]["extra"]
        if m_open:
            extra["opening_value"] = _to_float(m_open.group(1))
        if m_pur:
            extra["purchase_value"] = _to_float(m_pur.group(1))

        rows = _swil_land_group_words(page.get_text("words") or [], y_tol=3.5)
        buckets = list(_DAXIN_DEFAULT_BUCKETS)
        header_y = 0.0
        for row in rows:
            blob = _swil_land_row_blob(row)
            if re.search(r"Product\s+Name", blob, re.I) and re.search(r"O\.Stk", blob, re.I):
                built = _daxin_buckets_from_header(row)
                if built:
                    buckets = built
                header_y = max(header_y, row["y"])
                continue
            if re.search(
                r"Company\s*:|Opening\s+Value|Purchase\s+Value|Generated\s+in|"
                r"PROFITMAKER|Stock\s*&\s*Sales|Page\s*:",
                blob,
                re.I,
            ):
                continue
            if row["y"] <= header_y + 4:
                continue
            cells = _daxin_row_cells(row, buckets)
            name = _clean_name(" ".join(cells.get("name") or []))
            if not name or _DAXIN_SKIP_NAME_RE.search(name):
                continue
            qty_hits = sum(
                1
                for key in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
                if _daxin_cell_number(cells.get(key) or []) is not None
            )
            if qty_hits < 3:
                continue

            item = empty_line_item()
            item["product_name"] = name
            pack = _clean_name(" ".join(cells.get("pack") or []))
            item["packing"] = pack or None
            item["opening_qty"] = _daxin_cell_number(cells.get("opening_qty") or []) or 0.0
            item["receipts_qty"] = _daxin_cell_number(cells.get("receipts_qty") or []) or 0.0
            item["sales_qty"] = _daxin_cell_number(cells.get("sales_qty") or []) or 0.0
            item["closing_qty"] = _daxin_cell_number(cells.get("closing_qty") or []) or 0.0
            close_val = _daxin_cell_number(cells.get("closing_value") or [])
            if close_val is not None:
                item["closing_value"] = close_val
            extra_item = item.setdefault("extra", {})
            if isinstance(extra_item, dict):
                tot = _daxin_cell_number(cells.get("total_stock") or [])
                age = _daxin_cell_number(cells.get("age") or [])
                if tot is not None:
                    extra_item["total_stock"] = tot
                if age is not None:
                    extra_item["age_days"] = age
            items.append(item)

    if not items:
        return None
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "daxinsoft_stock_sales"
    extra["layout"] = "ostk_purc_tot_sale_qoh_value_age"
    extra["rows_detected"] = len(items)
    extra["fallback_used"] = False
    return result


_OSP_BUCKETS = (
    ("name", 0.0, 120.0),
    ("pack", 120.0, 185.0),
    ("opening_qty", 185.0, 240.0),
    ("sales_qty", 240.0, 285.0),
    ("sale_free", 285.0, 320.0),
    ("sales_value", 320.0, 365.0),
    ("sales_return", 365.0, 400.0),
    ("receipts_qty", 400.0, 445.0),
    ("purchase_free", 445.0, 490.0),
    ("purchase_value", 490.0, 535.0),
    ("purchase_return", 535.0, 575.0),
    ("other", 575.0, 618.0),
    ("closing_qty", 618.0, 668.0),
    ("closing_value", 668.0, 715.0),
    ("expiry_in", 715.0, 750.0),
    ("expiry_out", 750.0, 800.0),
)
_OSP_SKIP_NAME_RE = re.compile(
    r"^(ITEM|DESCRIPTION|OPENING|SALES|SALE|PURCHASE|CLOSING|EXPIRY|"
    r"BALANCE|PENDING|SUPPLIER|DEBIT|TOTAL|THE\s+HIMALAYA)\b",
    re.I,
)


def _is_opening_sales_purchase_statement_text(text: str) -> bool:
    """Item Description + Opening Balance + Sales Qty/Amount + Purchase Qty + Closing."""
    if not text:
        return False
    if re.search(r"STOCK\s*&\s*SALES\s*ANALYSIS", text, re.I):
        return False
    if re.search(r"\bParticulars\b", text, re.I) and re.search(r"Purch\.?\s*Qty", text, re.I):
        return False
    if re.search(r"O\.Stk", text, re.I) and re.search(r"\bQoh\b", text, re.I):
        return False
    return bool(
        re.search(r"Stock\s+and\s+Sales\s+Statement", text, re.I)
        and re.search(r"Item\s+Description", text, re.I)
        and re.search(r"Opening", text, re.I)
        and re.search(r"\bBalance\b", text, re.I)
        and re.search(r"Sales", text, re.I)
        and re.search(r"\bQty\.?\b", text, re.I)
        and re.search(r"Purchase", text, re.I)
        and re.search(r"Closing", text, re.I)
        and re.search(r"\bAmount\b", text, re.I)
        and re.search(r"Sale\s+Free|Expiry", text, re.I)
    )


def _osp_cell_number(tokens: List[str]) -> Optional[float]:
    cleaned = [str(t).strip() for t in tokens if str(t).strip()]
    if cleaned and all(tok in {"-", "—", "--"} for tok in cleaned):
        return 0.0
    return _prompt_cell_number(cleaned)


def _osp_row_cells(row: Dict[str, Any]) -> Dict[str, List[str]]:
    cells: Dict[str, List[str]] = {name: [] for name, _lo, _hi in _OSP_BUCKETS}
    for _x0, _x1, xc, token in row.get("words") or []:
        for name, lo, hi in _OSP_BUCKETS:
            if lo <= xc < hi:
                cells[name].append(token)
                break
    return cells


def _parse_opening_sales_purchase_statement(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Parse Item Description / Opening Balance / Sales Qty / Purchase Qty / Closing."""
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_opening_sales_purchase_statement_text(t) for t in page_texts):
        return None

    result = empty_result(filename, "pdf")
    result["report_title"] = "Stock and Sales Statement"
    items: List[Dict[str, Any]] = []
    in_debit_notes = False

    for page, text in zip(doc, page_texts):
        if not result.get("stockist_name"):
            for ln in text.splitlines():
                raw = ln.strip()
                if not raw or len(raw) > 80:
                    continue
                if re.search(
                    r"HIMALAYA|Stock\s+and\s+Sales|Item\s+Description|Phone\s*:|E-Mail|GSTIN|D\.L\.No",
                    raw,
                    re.I,
                ):
                    continue
                if re.search(r"DRUG|AGENC|STORE|MEDICO|MEDICAL|DISTRIBUT", raw, re.I):
                    result["stockist_name"] = _clean_name(raw)
                    break
        m_period = re.search(
            r"from\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+to\s+"
            r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
            text,
            re.I,
        )
        if m_period:
            result["period_from"] = _normalize_date(m_period.group(1))
            result["period_to"] = _normalize_date(m_period.group(2))
        if not result.get("company_name"):
            m_co = re.search(r"THE\s+HIMALAYA[^\n]*", text, re.I)
            if m_co:
                result["company_name"] = _clean_name(m_co.group(0))

        rows = _swil_land_group_words(page.get_text("words") or [], y_tol=2.8)
        for row in rows:
            blob = _swil_land_row_blob(row)
            if re.search(r"PENDING\s+DEBIT\s+NOTES", blob, re.I):
                in_debit_notes = True
                continue
            if in_debit_notes:
                continue
            if re.search(
                r"Item\s+Description|Stock\s+and\s+Sales|^Balance\s+Qty|"
                r"Phone\s*:|E-Mail|GSTIN|D\.L\.No|THE\s+HIMALAYA",
                blob,
                re.I,
            ):
                continue
            cells = _osp_row_cells(row)
            name = _clean_name(" ".join(cells.get("name") or []))
            if re.match(r"^TOTAL\b", blob.strip(), re.I) or re.match(r"^TOTAL\b", name, re.I):
                open_q = _osp_cell_number(cells.get("opening_qty") or [])
                sale_v = _osp_cell_number(cells.get("sales_value") or [])
                close_v = _osp_cell_number(cells.get("closing_value") or [])
                if open_q and sale_v and close_v:
                    extra = result["totals"]["extra"]
                    extra["opening_qty"] = open_q
                    extra["sales_qty"] = _osp_cell_number(cells.get("sales_qty") or [])
                    extra["receipts_qty"] = _osp_cell_number(cells.get("receipts_qty") or [])
                    extra["receipts_value"] = _osp_cell_number(cells.get("purchase_value") or [])
                    extra["purchase_value"] = extra["receipts_value"]
                    extra["closing_qty"] = _osp_cell_number(cells.get("closing_qty") or [])
                    result["totals"]["sales_value"] = sale_v
                    result["totals"]["closing_value"] = close_v
                    extra["total_row_source"] = "osp_grand_total"
                continue
            if not name or _OSP_SKIP_NAME_RE.search(name):
                continue
            qty_hits = sum(
                1
                for key in ("opening_qty", "sales_qty", "receipts_qty", "closing_qty")
                if _osp_cell_number(cells.get(key) or []) is not None
            )
            if qty_hits < 2:
                continue
            pack = _clean_name(" ".join(cells.get("pack") or []))
            item = empty_line_item()
            item["product_name"] = name
            item["packing"] = pack or None
            item["opening_qty"] = _osp_cell_number(cells.get("opening_qty") or []) or 0.0
            item["receipts_qty"] = _osp_cell_number(cells.get("receipts_qty") or []) or 0.0
            item["sales_qty"] = _osp_cell_number(cells.get("sales_qty") or []) or 0.0
            item["closing_qty"] = _osp_cell_number(cells.get("closing_qty") or []) or 0.0
            sale_v = _osp_cell_number(cells.get("sales_value") or [])
            close_v = _osp_cell_number(cells.get("closing_value") or [])
            if sale_v is not None:
                item["sales_value"] = sale_v
            if close_v is not None:
                item["closing_value"] = close_v
            extra_item = item.setdefault("extra", {})
            if isinstance(extra_item, dict):
                purch_v = _osp_cell_number(cells.get("purchase_value") or [])
                if purch_v:
                    extra_item["purchase_value"] = purch_v
                    extra_item["receipts_value"] = purch_v
                for src, dest in (
                    ("sale_free", "sale_free_qty"),
                    ("sales_return", "sale_return_qty"),
                    ("purchase_free", "purchase_free_qty"),
                    ("purchase_return", "purchase_return_qty"),
                    ("other", "other_qty"),
                    ("expiry_in", "expiry_in_qty"),
                    ("expiry_out", "expiry_out_qty"),
                ):
                    val = _osp_cell_number(cells.get(src) or [])
                    if val:
                        extra_item[dest] = val
            items.append(item)

    if not items:
        return None
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "item_desc_opening_sales_purchase"
    extra["layout"] = "opening_sales_purchase_closing"
    extra["rows_detected"] = len(items)
    extra["fallback_used"] = False
    return result


def _is_purc_sale_cl_header(blob: str) -> bool:
    """MediVision Stock and Sales: Purc / Purc val / Sale / Sa val / Cl qty / Cl val."""
    if not blob:
        return False
    return bool(
        re.search(r"\bProduct\b", blob, re.I)
        and re.search(r"\bUnit\b", blob, re.I)
        and re.search(r"\bPurc\b", blob, re.I)
        and re.search(r"\bSale\b", blob, re.I)
        and re.search(r"Cl\s*qty", blob, re.I)
        and re.search(r"Cl\s*val", blob, re.I)
    )


def _purc_sale_cl_columns(header_words: List[Tuple[float, str]]) -> Optional[List[Tuple[str, float]]]:
    """Column starts from this header only. Other layouts never reach here."""
    starts: List[Tuple[str, float]] = []
    seen_purc = False
    seen_cl = False
    for x, token in header_words:
        norm = re.sub(r"[^a-z]", "", token.lower())
        field = None
        if norm in {"product", "name"} and not any(n == "name" for n, _ in starts):
            field = "name"
        elif norm == "unit":
            field = "pack"
        elif norm == "purc":
            field = "receipts_value" if seen_purc else "receipts_qty"
            seen_purc = True
        elif norm == "sale":
            field = "sales_qty"
        elif norm == "sa":
            field = "sales_value"
        elif norm == "cl":
            field = "closing_value" if seen_cl else "closing_qty"
            seen_cl = True
        if field:
            starts.append((field, x))
    needed = {
        "name",
        "pack",
        "receipts_qty",
        "receipts_value",
        "sales_qty",
        "sales_value",
        "closing_qty",
        "closing_value",
    }
    if not needed.issubset({name for name, _x in starts}):
        return None
    return starts


def _purc_sale_cl_field(x: float, starts: List[Tuple[str, float]]) -> Optional[str]:
    field = None
    for name, start in starts:
        if x + 0.5 >= start:
            field = name
        else:
            break
    return field


def _parse_purc_sale_cl_statement(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Parse Purc / Sale / Cl qty sheets from word positions.

    Returns None unless that header is printed, so other PDFs stay unchanged.
    """
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_purc_sale_cl_header(text) for text in page_texts):
        return None

    result = empty_result(filename, "pdf")
    result["report_title"] = "Stock and Sales"
    items: List[Dict[str, Any]] = []
    columns: Optional[List[Tuple[str, float]]] = None

    for page, text in zip(doc, page_texts):
        if result["period_from"] is None:
            m = re.search(
                r"(\d{1,2}-\d{1,2}-\d{2,4})\s+to\s+(\d{1,2}-\d{1,2}-\d{2,4})",
                text,
                re.I,
            )
            if m:
                result["period_from"] = _normalize_date(m.group(1))
                result["period_to"] = _normalize_date(m.group(2))
        company = re.search(r"Company:\s*(.+)", text, re.I)
        if company and not result.get("company_name"):
            result["company_name"] = _clean_name(company.group(1).split("\n")[0])

        words = sorted(page.get_text("words") or [], key=lambda w: (round(w[1], 1), w[0]))
        rows: List[Dict[str, Any]] = []
        for w in words:
            x0, y0, token = float(w[0]), float(w[1]), str(w[4]).strip()
            if not token:
                continue
            if rows and abs(y0 - rows[-1]["y"]) <= 2.0:
                row = rows[-1]
            else:
                row = {"y": y0, "words": []}
                rows.append(row)
            row["words"].append((x0, token))

        for row in rows:
            blob = " ".join(tok for _x, tok in row["words"])
            if _is_purc_sale_cl_header(blob):
                found = _purc_sale_cl_columns(row["words"])
                if found:
                    columns = found
                if not result.get("stockist_name"):
                    for prev in rows:
                        if prev["y"] >= row["y"] - 2:
                            break
                        line = " ".join(tok for _x, tok in prev["words"]).strip()
                        if not line or re.search(
                            r"Phone|Email|Stock|Company|Page|MediVision|@",
                            line,
                            re.I,
                        ):
                            continue
                        result["stockist_name"] = _clean_name(line)
                        break
                continue
            if not columns:
                continue
            if re.search(r"Continued|Page\s*No|MediVision", blob, re.I):
                continue
            cells: Dict[str, List[str]] = {}
            for x, token in row["words"]:
                field = _purc_sale_cl_field(x, columns)
                if field:
                    cells.setdefault(field, []).append(token)
            name = _clean_name(" ".join(cells.get("name") or []))
            if not name or re.match(r"^(Product|Unit|Total|Page)\b", name, re.I):
                continue
            item = empty_line_item()
            item["product_name"] = name
            pack = " ".join(cells.get("pack") or []).strip()
            item["packing"] = pack or None
            item["opening_qty"] = 0.0
            item["receipts_qty"] = _prompt_cell_number(cells.get("receipts_qty") or []) or 0.0
            item["sales_qty"] = _prompt_cell_number(cells.get("sales_qty") or []) or 0.0
            item["sales_value"] = _prompt_cell_number(cells.get("sales_value") or []) or 0.0
            item["closing_qty"] = _prompt_cell_number(cells.get("closing_qty") or []) or 0.0
            item["closing_value"] = _prompt_cell_number(cells.get("closing_value") or []) or 0.0
            extra = item["extra"]
            extra["layout"] = "purc_sale_cl"
            pur_val = _prompt_cell_number(cells.get("receipts_value") or [])
            if pur_val is not None:
                extra["purchase_value"] = pur_val
            items.append(item)

    if not items:
        return None
    result["line_items"] = items
    result["totals"]["extra"]["extraction_method"] = "purc_sale_cl_layout"
    result["totals"]["extra"]["layout"] = "purc_sale_cl"
    return result


_DETAIL_OPVAL_LABELS = {
    "sl.no": "sr",
    "slno": "sr",
    "op.qty": "opening_qty",
    "opqty": "opening_qty",
    "op.val": "opening_value",
    "opval": "opening_value",
    "p.qty": "receipts_qty",
    "p.sch": "purchase_scheme",
    "p.val": "purchase_value",
    "s.qty": "sales_qty",
    "s.sch": "sales_scheme",
    "s.val": "sales_value",
    "cl.qty": "closing_qty",
    "cl.val": "closing_value",
}


def _detail_opval_number(tokens: List[str]) -> Optional[float]:
    found = None
    for tok in tokens:
        raw = str(tok).replace(",", "").strip().rstrip("Ll")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
            found = float(raw)
    return found


def _parse_stock_sales_detail_opval(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Landscape Stock and Sales Detail with a printed Op.Val column.

    Returns None for every other statement so existing parsers are unchanged.
    """
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(
        re.search(r"Stock\s+and\s+Sales\s+Detail", text, re.I)
        and re.search(r"Op\.Val", text, re.I)
        for text in page_texts
    ):
        return None

    result = empty_result(filename, "pdf")
    result["report_title"] = "Stock and Sales Detail Report"
    items: List[Dict[str, Any]] = []

    for page, text in zip(doc, page_texts):
        seller = re.search(r"Seller\s*:\s*(.+)", text, re.I)
        if seller and not result.get("stockist_name"):
            result["stockist_name"] = _clean_name(seller.group(1).split("From")[0])
        if result["period_from"] is None:
            m = re.search(
                r"From\s+(\d{1,2}-\d{1,2}-\d{4})\s+to\s+(\d{1,2}-\d{1,2}-\d{4})",
                text,
                re.I,
            )
            if m:
                result["period_from"] = _normalize_date(m.group(1))
                result["period_to"] = _normalize_date(m.group(2))

        words = sorted(page.get_text("words") or [], key=lambda w: (round(w[1], 1), w[0]))
        rows: List[Dict[str, Any]] = []
        for w in words:
            x0, x1, y0, token = float(w[0]), float(w[2]), float(w[1]), str(w[4]).strip()
            if not token:
                continue
            if rows and abs(y0 - rows[-1]["y"]) <= 2.0:
                row = rows[-1]
            else:
                row = {"y": y0, "words": []}
                rows.append(row)
            row["words"].append((x0, x1, token))

        columns: Optional[List[Tuple[str, float]]] = None
        header_i = None
        for i, row in enumerate(rows):
            blob = " ".join(tok for _a, _b, tok in row["words"])
            if re.search(r"Op\.Qty", blob) and re.search(r"Op\.Val", blob) and re.search(r"Cl\.Qty", blob):
                starts: List[Tuple[Optional[str], float]] = []
                for x0, _x1, token in row["words"]:
                    key = re.sub(r"[^a-z.]", "", token.lower())
                    field = _DETAIL_OPVAL_LABELS.get(key)
                    starts.append((field, x0))
                if any(name == "opening_value" for name, _x in starts) and any(
                    name == "opening_qty" for name, _x in starts
                ):
                    columns = starts
                    header_i = i
        if not columns:
            continue
        op_qty_x = next(x for name, x in columns if name == "opening_qty")

        spans: List[Tuple[Optional[str], float, float]] = []
        for idx, (name, start) in enumerate(columns):
            end = columns[idx + 1][1] if idx + 1 < len(columns) else 10000.0
            spans.append((name, start, end))

        def field_for(x1: float) -> Optional[str]:
            # Right edge of a right-aligned number, and only inside that header's span.
            probe = x1 - 0.5
            for name, start, end in spans:
                if start <= probe < end:
                    return name
            return None

        serial_at: List[int] = []
        for i, row in enumerate(rows):
            if header_i is not None and i <= header_i:
                continue
            for x0, x1, token in row["words"]:
                if (x0 + x1) / 2.0 < 50 and re.fullmatch(r"\d{1,4}", token):
                    serial_at.append(i)
                    break

        def item_words(row: Dict[str, Any]) -> List[str]:
            out = []
            for x0, x1, token in row["words"]:
                center = (x0 + x1) / 2.0
                if 48 <= center < op_qty_x and not re.fullmatch(r"\d{1,4}", token):
                    out.append(token)
            return out

        for n, i in enumerate(serial_at):
            names: List[str] = []
            prev_y = rows[i]["y"]
            j = i - 1
            while j > (header_i or 0):
                if rows[j]["y"] < prev_y - 16:
                    break
                if j in serial_at:
                    break
                part = item_words(rows[j])
                if part:
                    names = part + names
                prev_y = rows[j]["y"]
                j -= 1
            names.extend(item_words(rows[i]))
            prev_y = rows[i]["y"]
            j = i + 1
            next_serial = serial_at[n + 1] if n + 1 < len(serial_at) else len(rows)
            while j < next_serial:
                if rows[j]["y"] > prev_y + 16:
                    break
                part = item_words(rows[j])
                if not part:
                    break
                names.extend(part)
                prev_y = rows[j]["y"]
                j += 1

            cells: Dict[str, List[str]] = {}
            for _x0, x1, token in rows[i]["words"]:
                field = field_for(x1)
                if field and field != "sr":
                    cells.setdefault(field, []).append(token)
            name = _clean_name(" ".join(names))
            if not name:
                continue
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = _detail_opval_number(cells.get("opening_qty") or []) or 0.0
            item["receipts_qty"] = _detail_opval_number(cells.get("receipts_qty") or []) or 0.0
            item["sales_qty"] = _detail_opval_number(cells.get("sales_qty") or []) or 0.0
            item["sales_value"] = _detail_opval_number(cells.get("sales_value") or []) or 0.0
            item["closing_qty"] = _detail_opval_number(cells.get("closing_qty") or []) or 0.0
            item["closing_value"] = _detail_opval_number(cells.get("closing_value") or []) or 0.0
            opening_value = _detail_opval_number(cells.get("opening_value") or []) or 0.0
            ordered: Dict[str, Any] = {}
            for key, value in item.items():
                ordered[key] = value
                if key == "opening_qty":
                    ordered["opening_value"] = opening_value
            item.clear()
            item.update(ordered)
            extra = item["extra"]
            extra["layout"] = "stock_sales_detail_opval"
            for key in ("purchase_scheme", "purchase_value", "sales_scheme"):
                num = _detail_opval_number(cells.get(key) or [])
                if num is not None:
                    extra[key] = num
            items.append(item)

        for row in rows:
            blob = " ".join(tok for _a, _b, tok in row["words"])
            if not re.search(r"\bTotal\b", blob, re.I):
                continue
            cells = {}
            for _x0, x1, token in row["words"]:
                field = field_for(x1)
                if field:
                    cells.setdefault(field, []).append(token)
            op_val = _detail_opval_number(cells.get("opening_value") or [])
            if op_val is not None:
                result["totals"]["opening_value"] = op_val
            if not result.get("company_name"):
                for prev in rows:
                    if prev["y"] >= row["y"]:
                        break
                    left = " ".join(
                        tok for x0, _x1, tok in prev["words"] if x0 < 200
                    ).strip()
                    if left and not re.search(r"Stock|Seller|Sl\.No|Op\.Qty", left, re.I):
                        result["company_name"] = _clean_name(left)
                        break

    if not items:
        return None
    result["line_items"] = items
    result["totals"]["extra"]["extraction_method"] = "stock_sales_detail_opval"
    result["totals"]["extra"]["layout"] = "stock_sales_detail_opval"
    return result


def _parse_pdf(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """Split multi-stockist PDFs into statements, then extract each."""
    import os

    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise ValueError("PyMuPDF is required for PDF sales statements") from exc

    max_pages = int(os.getenv("SALES_PDF_MAX_PAGES", "20"))
    zoom = float(os.getenv("SALES_PDF_RENDER_ZOOM", "2.0"))

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    try:
        detail_stmt = _parse_stock_sales_detail_opval(doc, filename)
        if detail_stmt and detail_stmt.get("line_items"):
            detail_stmt["totals"]["extra"]["statement_count"] = 1
            return detail_stmt

        purc_stmt = _parse_purc_sale_cl_statement(doc, filename)
        if purc_stmt and purc_stmt.get("line_items"):
            purc_stmt["totals"]["extra"]["statement_count"] = 1
            return purc_stmt

        prompt_stmt = _parse_prompt_datewise_stock_statement(doc, filename)
        if prompt_stmt and prompt_stmt.get("line_items"):
            prompt_stmt["totals"]["extra"]["statement_count"] = 1
            return prompt_stmt

        swil_pair = _parse_swil_landscape_qty_value_statement(
            doc, filename, allow_portrait_pair=True
        )
        if swil_pair and swil_pair.get("line_items"):
            swil_pair["totals"]["extra"]["statement_count"] = 1
            swil_pair["totals"]["extra"]["fallback_used"] = False
            return swil_pair

        swil_stmt = _parse_swil_opening_receipt_value_statement(doc, filename)
        if swil_stmt and swil_stmt.get("line_items"):
            if _swil_fixed_bucket_result_is_weak(swil_stmt, doc):
                landscape = _parse_swil_landscape_qty_value_statement(doc, filename)
                if landscape and landscape.get("line_items"):
                    landscape["totals"]["extra"]["statement_count"] = 1
                    landscape["totals"]["extra"]["fallback_used"] = True
                    landscape["totals"]["extra"]["fallback_from"] = (
                        "swil_opening_receipt_value"
                    )
                    return landscape
            swil_stmt["totals"]["extra"]["statement_count"] = 1
            swil_stmt["totals"]["extra"]["fallback_used"] = False
            return swil_stmt
        if _is_swil_landscape_qty_value_doc(doc):
            landscape = _parse_swil_landscape_qty_value_statement(doc, filename)
            if landscape and landscape.get("line_items"):
                landscape["totals"]["extra"]["statement_count"] = 1
                landscape["totals"]["extra"]["fallback_used"] = True
                return landscape

        code_item = _parse_code_item_stock_statement(doc, filename)
        if code_item and code_item.get("line_items"):
            code_item["totals"]["extra"]["statement_count"] = 1
            return code_item

        saleable = _parse_saleable_stock_report_doc(doc, filename)
        if saleable and saleable.get("line_items"):
            saleable["totals"]["extra"]["statement_count"] = 1
            return saleable

        order_form = _parse_order_form_stock_statement_doc(doc, filename)
        if order_form and order_form.get("line_items"):
            order_form["totals"]["extra"]["statement_count"] = 1
            return order_form

        ssa_mexp = _parse_ssa_mexp_stock_sales_doc(doc, filename)
        if ssa_mexp and ssa_mexp.get("line_items"):
            ssa_mexp["totals"]["extra"]["statement_count"] = 1
            return ssa_mexp

        ssa_ori = _parse_ssa_opening_receipt_issue_value_doc(doc, filename)
        if ssa_ori and ssa_ori.get("line_items"):
            ssa_ori["totals"]["extra"]["statement_count"] = 1
            return ssa_ori

        ved = _parse_ved_stock_sales_statement(doc, filename)
        if ved and ved.get("line_items"):
            ved["totals"]["extra"]["statement_count"] = 1
            return ved

        daxin = _parse_daxinsoft_stock_sales_statement(doc, filename)
        if daxin and daxin.get("line_items"):
            daxin["totals"]["extra"]["statement_count"] = 1
            return daxin

        osp = _parse_opening_sales_purchase_statement(doc, filename)
        if osp and osp.get("line_items"):
            osp["totals"]["extra"]["statement_count"] = 1
            return osp

        qty_pages = []
        for page_index, page in enumerate(doc):
            if page_index >= max_pages:
                break
            qty_pages.append(
                {
                    "page_index": page_index,
                    "text": page.get_text("text") or "",
                    "words": page.get_text("words") or [],
                }
            )
        qty_value = _parse_rate_qty_value_statement(qty_pages, filename)
        if qty_value and qty_value.get("line_items"):
            qty_value["totals"]["extra"]["statement_count"] = 1
            return qty_value

        page_infos: List[Dict[str, Any]] = []
        for page_index, page in enumerate(doc):
            if page_index >= max_pages:
                break
            embedded = (page.get_text("text") or "").strip()
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            image_bytes = pix.tobytes("png")
            text = embedded
            if len(re.sub(r"\s+", "", embedded)) < 40:
                # 3.5x needed so digits like Issue=12 are not read as 2 on dense scans
                text, _ocr_img = _ocr_pdf_page_text(page, zoom=max(zoom, 3.5))
            else:
                text = _merge_footer_total_into_text(text, image_bytes)

            stockist = _detect_stockist_from_page_text(text)
            page_infos.append(
                {
                    "page_index": page_index,
                    "text": text,
                    "image_bytes": image_bytes,
                    "stockist": stockist,
                    "words": page.get_text("words") or [],
                }
            )

        groups = _group_pdf_pages_by_stockist(page_infos)
        # Deduplicate adjacent identical keys already handled; merge non-adjacent
        # same stockist only if user wants — keep separate segments in page order.

        if not groups:
            return empty_result(filename, "pdf")

        statements = [
            _extract_statement_from_pdf_group(group, filename) for group in groups
        ]

        # Single stockist → keep flat unified schema (backward compatible)
        if len(statements) == 1:
            single = statements[0]
            single["totals"]["extra"]["statement_count"] = 1
            return single

        return {
            "source_file": filename,
            "source_format": "pdf",
            "multi_statement": True,
            "statement_count": len(statements),
            "statements": statements,
        }
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Product Stock Report (Himalaya ZANDRA / Opening-Purchase-Total-Sale-Cls Amt)
# ---------------------------------------------------------------------------

_PRODUCT_STOCK_REPORT_TITLE = re.compile(r"Product\s+Stock\s+Report", re.I)
_PSR_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "product_name": ("productname", "product", "itemname", "item"),
    "opening_qty": ("opening", "openingqty", "opqty"),
    "receipts_qty": ("purchase", "purchaseqty", "purqty"),
    "total_stock": ("total", "tot"),
    # SaleRet must sit before sales_qty so "saleret" is never treated as Sale.
    "sale_return": ("saleret", "salereturn", "salesreturn", "returnqty"),
    "sales_qty": ("sale", "sales", "saleqty", "salesqty"),
    "sample_qty": ("sample", "smpl", "free"),
    "exp_damage": ("expdmg", "expdamage", "expirydamage"),
    "expiry_qty": ("expclos", "expcls", "exp", "expiry", "expiryqty"),
    "closing_qty": ("closing", "closingqty", "clsqty", "closingstock"),
    "closing_value": (
        "clsamt",
        "closingamt",
        "closingvalue",
        "clsamount",
        "stkval",
        "stockval",
        "stockvalue",
        "value",
        "amount",
    ),
    "order_qty": ("orderqty", "ordqty"),
    "sales_value": ("saleamt", "salesamt", "salevalue", "salesvalue", "netamt"),
}

STOCK_IDENTITY_SALERET = "opening_purchase_sale_saleret_expdmg"
STOCK_IDENTITY_SAMPLE = "opening_purchase_sale_sample_expclos"
STOCK_IDENTITY_OPBAL = "opening_receipt_issue_closing"
STOCK_IDENTITY_GENERIC = "opening_receipts_sales_closing"


def _is_product_stock_report_text(text: str) -> bool:
    """True only for Product Stock Report (Opening/Purchase/Sale), not OpBal/PS Pharma."""
    if not text or not _PRODUCT_STOCK_REPORT_TITLE.search(text):
        return False
    if _is_opbal_issue_closing_format(text):
        return False
    if re.search(r"OPENING\s+RECEIPT\s+ISSUE\s+CLOSING", text, re.I):
        return False
    header = "\n".join(ln for ln in text.splitlines()[:50] if ln.strip())
    return bool(
        re.search(r"\bOpening\b", header, re.I)
        and re.search(r"\bPurchase\b", header, re.I)
        and re.search(r"\bSale\b", header, re.I)
    )


def _is_product_stock_report_result(result: Dict[str, Any]) -> bool:
    title = str((result or {}).get("report_title") or "")
    return bool(_PRODUCT_STOCK_REPORT_TITLE.search(title))


def _psr_text_has_saleret(text: str) -> bool:
    return bool(re.search(r"Sale\s*Ret|SaleRet|Sale\s*Return", text or "", re.I))


def _psr_text_has_sample_layout(text: str) -> bool:
    return bool(re.search(r"\bSample\b|\bExp\s*/\s*Clos", text or "", re.I))


def _psr_column_layout_from_text(text: str) -> str:
    if _psr_text_has_saleret(text):
        return "saleret"
    if _psr_text_has_sample_layout(text):
        return "sample"
    return "saleret"


def _psr_column_layout_from_colmap(colmap: Optional[Dict[str, int]]) -> Optional[str]:
    if not colmap:
        return None
    if "sale_return" in colmap or "exp_damage" in colmap:
        return "saleret"
    if "sample_qty" in colmap:
        return "sample"
    return None


def _item_extra_qty(extra: Dict[str, Any], *keys: str) -> float:
    for key in keys:
        if extra and key in extra:
            return _to_float(extra.get(key))
    return 0.0


def _stock_identity_kind(result: Dict[str, Any]) -> str:
    extra_t = ((result or {}).get("totals") or {}).get("extra") or {}
    explicit = extra_t.get("stock_identity_kind") or extra_t.get("psr_column_layout")
    if explicit == "saleret" or explicit == STOCK_IDENTITY_SALERET:
        return STOCK_IDENTITY_SALERET
    if explicit == "sample" or explicit == STOCK_IDENTITY_SAMPLE:
        return STOCK_IDENTITY_SAMPLE
    method = str(extra_t.get("extraction_method") or "")
    if "opbal" in method or "ps_pharma" in method:
        return STOCK_IDENTITY_OPBAL
    has_sr = False
    has_sample = False
    for item in (result or {}).get("line_items") or []:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if any(k in extra for k in ("sale_return", "sale_return_qty", "exp_damage", "exp_dmg")):
            has_sr = True
        if extra.get("sample_qty"):
            has_sample = True
    if _is_product_stock_report_result(result):
        if has_sr:
            return STOCK_IDENTITY_SALERET
        if has_sample:
            return STOCK_IDENTITY_SAMPLE
        return STOCK_IDENTITY_SALERET
    if has_sr:
        return STOCK_IDENTITY_SALERET
    return STOCK_IDENTITY_GENERIC


def _stock_expected_total(item: Dict[str, Any]) -> float:
    return round(
        _to_float(item.get("opening_qty")) + _to_float(item.get("receipts_qty")),
        2,
    )


def _stock_expected_closing(item: Dict[str, Any], kind: str) -> float:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    opening = _to_float(item.get("opening_qty"))
    receipts = _to_float(item.get("receipts_qty"))
    sales_qty = _to_float(item.get("sales_qty"))
    total = _to_float(extra.get("total_stock"))
    if total <= 0:
        total = opening + receipts
    if kind == STOCK_IDENTITY_SALERET:
        sale_return = _item_extra_qty(extra, "sale_return", "sale_return_qty", "saleret")
        exp_damage = _item_extra_qty(extra, "exp_damage", "exp_dmg", "expiry_damage")
        return round(total - sales_qty + sale_return - exp_damage, 2)
    if kind == STOCK_IDENTITY_SAMPLE:
        sample = _to_float(extra.get("sample_qty"))
        expiry = _to_float(extra.get("expiry_qty"))
        return round(total - sales_qty - sample - expiry, 2)
    # OpBal / generic: Opening + Receipt - Sale - scheme (ZANDRA S S Qty)
    scheme = _to_float(extra.get("sales_scheme_qty") or extra.get("sales_scheme"))
    return round(opening + receipts - sales_qty - scheme, 2)


def _stock_row_identity_ok(item: Dict[str, Any], kind: str, tol: float = 0.05) -> bool:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    printed_total = _to_float(extra.get("total_stock"))
    expected_total = _stock_expected_total(item)
    if printed_total > 0 and abs(printed_total - expected_total) > tol:
        return False
    expected_close = _stock_expected_closing(item, kind)
    closing = _to_float(item.get("closing_qty"))
    return abs(closing - expected_close) <= tol


def _product_stock_report_values_missing(result: Dict[str, Any]) -> bool:
    """Gemini often returns this format with qty only and Cls Amt dropped."""
    if not _is_product_stock_report_result(result):
        return False
    items = result.get("line_items") or []
    if not items:
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        if _to_float(item.get("sales_value")) > 0 or _to_float(item.get("closing_value")) > 0:
            return False
    return True


def _psr_fill_line_money_totals(result: Dict[str, Any]) -> None:
    items = result.get("line_items") or []
    sum_sales = sum(
        _to_float(i.get("sales_value")) for i in items if isinstance(i, dict)
    )
    sum_close = sum(
        _to_float(i.get("closing_value")) for i in items if isinstance(i, dict)
    )
    totals = result.setdefault(
        "totals", {"sales_value": None, "closing_value": None, "extra": {}}
    )
    if not isinstance(totals.get("extra"), dict):
        totals["extra"] = {}
    if sum_sales > 0:
        totals["sales_value"] = sum_sales
    if sum_close > 0:
        totals["closing_value"] = sum_close


def _psr_ensure_extra(item: Dict[str, Any]) -> Dict[str, Any]:
    extra = item.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        item["extra"] = extra
    return extra


def _psr_median_rate(items: List[Any]) -> Optional[float]:
    rates: List[float] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        qty = _to_float(item.get("closing_qty"))
        val = _to_float(item.get("closing_value"))
        if qty > 0 and val > 0:
            rate = val / qty
            if 0 < rate < 800:
                rates.append(rate)
    if not rates:
        return None
    rates.sort()
    return rates[len(rates) // 2]


def _psr_repair_cls_amt_scale(items: List[Any]) -> None:
    """Fix Cls Amt with a dropped decimal (7214.71 read as 721471)."""
    median = _psr_median_rate(items)
    for item in items:
        if not isinstance(item, dict):
            continue
        qty = _to_float(item.get("closing_qty"))
        val = _to_float(item.get("closing_value"))
        if val <= 0 or qty <= 0:
            continue
        rate = val / qty
        outlier = rate > 1000 if median is None else rate > max(median * 8, median + 400)
        if not outlier:
            continue
        extra = _psr_ensure_extra(item)
        best = val
        best_err = abs(rate - (median or 0))
        for div in (10, 100):
            cand = val / div
            cand_rate = cand / qty
            err = abs(cand_rate - (median or 60.0))
            if err < best_err and cand_rate < 800:
                best = cand
                best_err = err
        if best != val:
            extra["cls_amt_scale_repaired"] = round(val / best)
            item["closing_value"] = round(best, 2)


def _qty_is_truncated_form(seen: float, expected: float) -> bool:
    """True when `seen` is a dropped-digit form of `expected` (2 vs 27, 9 vs 90).

    Only expands truncated values. Does not shrink a longer printed qty.
    """
    if seen < 0 or expected < 0:
        return False
    if abs(seen - expected) < 0.05:
        return False
    for mul in (10.0, 100.0):
        if abs(seen * mul - expected) < 0.05:
            return True
    a = str(int(round(seen)))
    b = str(int(round(expected)))
    if not a.isdigit() or not b.isdigit():
        return False
    if len(a) >= len(b) or not (1 <= len(b) - len(a) <= 2):
        return False
    return b.startswith(a) or b.endswith(a)


def _psr_qty_row_needs_verify(item: Dict[str, Any], kind: str) -> bool:
    """True when one PSR row has dropped digits or fails stock identity."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    opening = _to_float(item.get("opening_qty"))
    receipts = _to_float(item.get("receipts_qty"))
    sales_qty = _to_float(item.get("sales_qty"))
    closing = _to_float(item.get("closing_qty"))
    total = _to_float(extra.get("total_stock")) or (opening + receipts)
    close_val = _to_float(item.get("closing_value"))
    if not _stock_row_identity_ok(item, kind):
        return True
    # Purchase landed in stock but Sale is 0 and Closing==Total (6.00→0.00)
    if (
        sales_qty <= 0
        and receipts > 0.05
        and abs(closing - total) < 0.05
        and total > opening + 0.05
    ):
        return True
    # Two-digit opening truncated (27.00→2.00) while Cls Amt still looks like money
    if (
        0 < opening < 10
        and abs(opening - closing) < 0.05
        and abs(closing - total) < 0.05
        and close_val > 50
    ):
        return True
    # Sale copied from Closing (LIQ 200 ML Sale 26 read as Closing 1)
    if sales_qty > 0 and closing > 0 and abs(sales_qty - closing) < 0.05 and total > closing + 1:
        return True
    return False


def _psr_qty_needs_verify(result: Dict[str, Any]) -> bool:
    """True when printed Sale/Opening digits look dropped or identity fails."""
    if not _is_product_stock_report_result(result):
        return False
    kind = _stock_identity_kind(result)
    for item in result.get("line_items") or []:
        if isinstance(item, dict) and _psr_qty_row_needs_verify(item, kind):
            return True
    return False


def _psr_qty_trial(item: Dict[str, Any], extra: Dict[str, Any], **updates: Any) -> Dict[str, Any]:
    trial = dict(item)
    trial_extra = dict(extra)
    if "total_stock" in updates:
        trial_extra["total_stock"] = updates.pop("total_stock")
    trial.update(updates)
    trial["extra"] = trial_extra
    return trial


def _psr_repair_qty_identity(item: Dict[str, Any], kind: str = STOCK_IDENTITY_SALERET) -> None:
    """Digit-only repairs when identity already fails. Does not invent qty
    merely to make the formula balance. Opening 27 read as 2 is recovered
    from printed Total (Total - Purchase) when it is a truncated form and
    the repaired row then satisfies stock identity. Sale 0 is recovered as
    Total-Closing+SaleRet-Exp/Dmg only when that qty is not Purchase/SaleRet/Exp.
    """
    extra = _psr_ensure_extra(item)
    opening = _to_float(item.get("opening_qty"))
    receipts = _to_float(item.get("receipts_qty"))
    sales_qty = _to_float(item.get("sales_qty"))
    closing = _to_float(item.get("closing_qty"))
    total = _to_float(extra.get("total_stock"))
    sale_return = _item_extra_qty(extra, "sale_return", "sale_return_qty", "saleret")
    exp_damage = _item_extra_qty(extra, "exp_damage", "exp_dmg", "expiry_damage")

    if _stock_row_identity_ok(item, kind):
        return

    def _commit(trial: Dict[str, Any], flags: Dict[str, Any]) -> None:
        item["opening_qty"] = trial.get("opening_qty")
        item["receipts_qty"] = trial.get("receipts_qty")
        item["sales_qty"] = trial.get("sales_qty")
        extra.update(trial.get("extra") or {})
        extra.update(flags)

    # Recover truncated Opening/Purchase from printed Total = Opening + Purchase
    if total > 0:
        expected_opening = round(total - receipts, 2)
        if expected_opening >= 0 and _qty_is_truncated_form(opening, expected_opening):
            trial = _psr_qty_trial(item, extra, opening_qty=expected_opening)
            if _stock_row_identity_ok(trial, kind):
                _commit(trial, {"opening_qty_from_total": True})
                return
        expected_receipts = round(total - opening, 2)
        if expected_receipts >= 0 and _qty_is_truncated_form(receipts, expected_receipts):
            trial = _psr_qty_trial(item, extra, receipts_qty=expected_receipts)
            if _stock_row_identity_ok(trial, kind):
                _commit(trial, {"receipts_qty_from_total": True})
                return

    expected_total = round(opening + receipts, 2)
    if expected_total > 0 and (total <= 0 or _qty_is_truncated_form(total, expected_total)):
        trial = _psr_qty_trial(item, extra, total_stock=expected_total)
        if _stock_row_identity_ok(trial, kind):
            flags = {"total_stock": expected_total}
            if total > 0:
                flags["total_stock_from_opening_purchase"] = True
            _commit(trial, flags)
            return

    # SaleRet layout: if Sale/SaleRet/Exp/Closing look complete, recover
    # truncated Opening/Total (27-26+2-2=1 ⇒ Total 27).
    if (
        kind == STOCK_IDENTITY_SALERET
        and sales_qty > 0
        and abs(sales_qty - closing) > 0.05
    ):
        implied_total = round(closing + sales_qty - sale_return + exp_damage, 2)
        if implied_total > 0:
            new_total = total
            new_opening = opening
            flags: Dict[str, Any] = {}
            if total <= 0 or _qty_is_truncated_form(total, implied_total):
                new_total = implied_total
                flags["total_stock_from_closing_identity"] = True
            expected_opening = round(implied_total - receipts, 2)
            if expected_opening >= 0 and _qty_is_truncated_form(opening, expected_opening):
                new_opening = expected_opening
                flags["opening_qty_from_total"] = True
            if flags:
                trial = _psr_qty_trial(
                    item, extra, opening_qty=new_opening, total_stock=new_total
                )
                if _stock_row_identity_ok(trial, kind):
                    _commit(trial, flags)
                    return

    use_total = total if total > 0 else round(opening + receipts, 2)
    if kind == STOCK_IDENTITY_SALERET and use_total > 0:
        implied_sale = round(use_total - closing + sale_return - exp_damage, 2)
        total_ok = abs(use_total - (opening + receipts)) < 0.05
        # Dropped Sale 0.00 (BONNISAN DROPS 115-109=6). Only when Total already
        # equals Opening+Purchase and the gap is not SaleRet / Exp/Dmg / Purchase.
        if (
            sales_qty <= 0
            and total_ok
            and implied_sale > 0.05
            and not (
                (sale_return > 0.05 and abs(implied_sale - sale_return) < 0.05)
                or (exp_damage > 0.05 and abs(implied_sale - exp_damage) < 0.05)
                or (receipts > 0.05 and abs(implied_sale - receipts) < 0.05)
            )
        ):
            trial = _psr_qty_trial(item, extra, sales_qty=implied_sale)
            if _stock_row_identity_ok(trial, kind):
                _commit(trial, {"sales_qty_from_identity": True})
                return
        if implied_sale >= 0 and _qty_is_truncated_form(sales_qty, implied_sale):
            trial = _psr_qty_trial(item, extra, sales_qty=implied_sale)
            if _stock_row_identity_ok(trial, kind):
                _commit(trial, {"sales_qty_from_identity": True})
                return
        expected_opening = round(use_total - receipts, 2)
        if (
            implied_sale >= 0
            and expected_opening >= 0
            and _qty_is_truncated_form(opening, expected_opening)
            and _qty_is_truncated_form(sales_qty, implied_sale)
        ):
            trial = _psr_qty_trial(
                item,
                extra,
                opening_qty=expected_opening,
                sales_qty=implied_sale,
                total_stock=use_total,
            )
            if _stock_row_identity_ok(trial, kind):
                _commit(
                    trial,
                    {
                        "opening_qty_from_total": True,
                        "sales_qty_from_identity": True,
                    },
                )
                return

    # Extra trailing zero: 90/10/80 read as 900/100/80
    if opening >= 100 and sales_qty >= 10 and closing > 0:
        trial = dict(item)
        trial_extra = dict(extra)
        trial["opening_qty"] = opening / 10.0
        trial["sales_qty"] = sales_qty / 10.0
        trial_extra["total_stock"] = opening / 10.0 + receipts
        trial["extra"] = trial_extra
        if _stock_row_identity_ok(trial, kind):
            item["opening_qty"] = opening / 10.0
            item["sales_qty"] = sales_qty / 10.0
            extra["total_stock"] = opening / 10.0 + receipts
            extra["qty_extra_zero_repaired"] = True


def _psr_norm_product(name: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(name or "").upper())


def _psr_overlay_ocr_qty(
    result: Dict[str, Any], ocr_result: Dict[str, Any]
) -> Dict[str, Any]:
    """Copy OCR qty onto PSR rows that still look truncated. PSR-only."""
    ocr_items = (ocr_result or {}).get("line_items") or []
    if not ocr_items:
        return result
    by_name: Dict[str, Dict[str, Any]] = {}
    for src in ocr_items:
        if not isinstance(src, dict):
            continue
        key = _psr_norm_product(src.get("product_name"))
        if key:
            by_name[key] = src
    kind = _stock_identity_kind(result)
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        if not _psr_qty_row_needs_verify(item, kind):
            continue
        key = _psr_norm_product(item.get("product_name"))
        src = by_name.get(key)
        if src is None and key:
            for ocr_key, candidate in by_name.items():
                if key in ocr_key or ocr_key in key:
                    if min(len(key), len(ocr_key)) >= 10:
                        src = candidate
                        break
        if src is None or not _stock_row_identity_ok(src, kind):
            continue
        extra = _psr_ensure_extra(item)
        for field in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
            item[field] = src.get(field)
        src_extra = src.get("extra") if isinstance(src.get("extra"), dict) else {}
        for field in (
            "total_stock",
            "sale_return",
            "sale_return_qty",
            "exp_damage",
            "exp_dmg",
            "order_qty",
        ):
            if field in src_extra:
                extra[field] = src_extra[field]
        extra["qty_from_ocr"] = True
    return result


def _psr_repair_qty_from_ocr(
    result: Dict[str, Any],
    file_bytes: bytes,
    filename: str,
    ext: str,
) -> Dict[str, Any]:
    """Local OCR fallback for truncated PSR qty when vision dropped a digit."""
    try:
        ocr_text = _ocr_image_to_text(file_bytes)
    except Exception as exc:
        logger.warning("Product Stock Report qty OCR skipped: %s", exc)
        return result
    source_format = (ext or ".png").lstrip(".") or "png"
    parsed = _parse_product_stock_report(ocr_text, filename, source_format)
    if not parsed or not parsed.get("line_items"):
        return result
    return _psr_overlay_ocr_qty(result, parsed)


def _psr_sales_value_is_copied_cls_amt(item: Dict[str, Any]) -> bool:
    sales_value = _to_float(item.get("sales_value"))
    close_val = _to_float(item.get("closing_value"))
    return sales_value > 0 and close_val > 0 and abs(sales_value - close_val) < 0.021


def _psr_fill_missing_sales(result: Dict[str, Any]) -> Dict[str, Any]:
    """Product Stock Report only: Cls Amt scale, digit extra-zero, and sale
    value from Cls Amt unit rate. Does not invent Sale/Closing to force
    identity. Does not change other statement formats.
    """
    if not isinstance(result, dict) or not _is_product_stock_report_result(result):
        return result
    kind = _stock_identity_kind(result)
    items = result.get("line_items") or []
    _psr_repair_cls_amt_scale(items)
    for item in items:
        if not isinstance(item, dict):
            continue
        extra = _psr_ensure_extra(item)
        _psr_repair_qty_identity(item, kind)
        sales_qty = _to_float(item.get("sales_qty"))
        closing = _to_float(item.get("closing_qty"))
        copied = _psr_sales_value_is_copied_cls_amt(item)
        close_val = _to_float(item.get("closing_value"))
        if sales_qty <= 0:
            if copied:
                item["sales_value"] = 0.0
            continue
        if closing <= 0 or close_val <= 0:
            if copied:
                item["sales_value"] = 0.0
            continue
        sales_value = _to_float(item.get("sales_value"))
        if sales_value > 0 and not copied:
            continue
        rate = close_val / closing
        item["sales_value"] = round(sales_qty * rate, 2)
        extra["sales_value_from_cls_amt_rate"] = True
        extra["unit_rate_from_cls_amt"] = round(rate, 4)
    _psr_fill_line_money_totals(result)
    return result


_PRODUCT_STOCK_REPORT_VISION_PROMPT = """
This image is a Himalaya / ZANDRA "Product Stock Report" HTML table (NOT a qty-only OpBal sheet).

Return ONLY valid JSON (no markdown) with this exact shape:
{
  "stockist_name": string|null,
  "stockist_address": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": "Product Stock Report",
  "line_items": [
    {
      "product_code": null,
      "product_name": string,
      "packing": null,
      "opening_qty": number,
      "receipts_qty": number,
      "sales_qty": number,
      "sales_value": 0,
      "closing_qty": number,
      "closing_value": number,
      "extra": {
        "total_stock": number,
        "sale_return": number,
        "exp_damage": number,
        "order_qty": number
      }
    }
  ],
  "totals": { "sales_value": null, "closing_value": number|null, "extra": {} }
}

Numeric columns after Product Name, left to right (copy each printed cell):
1 Opening -> opening_qty
2 Purchase -> receipts_qty
3 Total -> extra.total_stock (NEVER closing_qty or sales_qty)
4 Sale -> sales_qty. This is NOT Closing. BONNISAN LIQ 200 ML Sale is 26.00, not 1.00.
5 SaleRet -> extra.sale_return (sales returns; ADD back in stock). Never merge into sales_qty. Never call this Sample.
6 Exp/Dmg -> extra.exp_damage. Never call this Exp/Clos unless the header actually says Exp/Clos.
7 Closing Stock -> closing_qty (qty immediately before Closing Amount)
8 Closing Amount / Cls Amt -> closing_value (money with a decimal point)
9 Order Qty -> extra.order_qty (ignore for stock identity)

sales_value MUST be 0. Never put Cls Amt / Closing Amount into sales_value.

Stock identity for THIS format (SaleRet / Exp/Dmg):
- Total MUST equal Opening + Purchase
- Closing Stock MUST equal Total - Sale + SaleRet - Exp/Dmg
If either fails, you misread a cell. Look at that row again. Do not invent a number that is not printed.

Examples:
- BONNISAN DROPS: Opening 65, Purchase 50, Total 115, Sale 6, SaleRet 0, Exp/Dmg 0, Closing 109. 115-6+0-0=109.
- BONNISAN LIQ 100 ML: Opening 101, Purchase 0, Total 101, Sale 13, Closing 88.
- BONNISAN LIQ 200 ML: Opening 27 (not 2), Purchase 0, Total 27, Sale 26 (not 1), SaleRet 2, Exp/Dmg 2, Closing 1. 27-26+2-2=1.

Digit traps:
- 6.00 is not 0.00. 27.00 is not 2. 90.00 is not 900. 26.00 is not 1.00.
- Cls Amt keeps the decimal: 7214.71 not 721471.

Rules:
- Include EVERY product row top to bottom.
- Keep digits inside names (Liv 52, 100 ML, 200 ML).
- extra must be an object {}, never an array.
- stockist_name = agency header; company_name = Mfg Company line.
- Dates are DD/MM/YYYY (01/08/2026 -> 2026-08-01).
""".strip()


def _collapse_psr_header_line(line: str) -> str:
    """Join split header words so 'Cls Amt' / 'SaleRet' / 'Exp/Dmg' map as one token."""
    text = line or ""
    replacements = (
        (r"Product\s*Name", "ProductName"),
        (r"Mfg\s*Company", "MfgCompany"),
        (r"Cls\s*\.?\s*Amt", "ClsAmt"),
        (r"Closing\s*Amt", "ClsAmt"),
        (r"Closing\s*Amount", "ClsAmt"),
        (r"Closing\s*Stock", "Closing"),
        (r"Sale\s*Ret(?:urn)?", "SaleRet"),
        (r"Exp\s*/\s*Clos", "ExpClos"),
        (r"Exp\s*/?\s*Dmg", "ExpDmg"),
        (r"Exp\s*/?\s*Damage", "ExpDmg"),
        (r"Order\s*Qty", "OrderQty"),
        (r"Stock\s*Val(?:ue)?", "StkVal"),
        (r"Disc\s*\.?\s*Amt", "DiscAmt"),
        (r"Sale\s*Amt", "SaleAmt"),
        (r"Net\s*Amt", "NetAmt"),
    )
    for pat, repl in replacements:
        text = re.sub(pat, repl, text, flags=re.I)
    return text


def _psr_colmap_from_headers(headers: List[str]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for idx, header in enumerate(headers):
        key = re.sub(r"[^a-z0-9]", "", str(header or "").lower())
        if not key:
            continue
        for field, aliases in _PSR_FIELD_ALIASES.items():
            if field in mapping:
                continue
            if key == field.replace("_", "") or key in aliases:
                mapping[field] = idx
                break
    return mapping


def _psr_header_tokens(line: str) -> List[str]:
    collapsed = _collapse_psr_header_line(line)
    return [tok for tok in collapsed.split() if tok]


def _is_psr_header_line(line: str) -> bool:
    blob = _collapse_psr_header_line(line)
    return bool(
        re.search(r"\bOpening\b", blob, re.I)
        and re.search(r"\bPurchase\b", blob, re.I)
        and re.search(r"\bSale\b", blob, re.I)
    )


def _psr_plain_number(token: str) -> Optional[float]:
    text = str(token or "").strip().replace(",", "")
    if not text or not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    return _to_float(text)


def _split_psr_name_and_numbers(line: str) -> Tuple[str, List[float]]:
    """Keep digits inside product names (Liv 52); only take trailing qty/amount tokens."""
    tokens = (line or "").split()
    nums: List[float] = []
    i = len(tokens)
    while i > 0:
        val = _psr_plain_number(tokens[i - 1])
        if val is None:
            break
        nums.append(val)
        i -= 1
    nums.reverse()
    name = _clean_name(" ".join(tokens[:i]))
    return name, nums


def _map_psr_numbers(
    nums: List[float],
    colmap: Optional[Dict[str, int]] = None,
    layout: str = "saleret",
) -> Dict[str, Any]:
    """Map Opening/Purchase/Total/Sale/.../Cls Amt numbers onto the unified item."""
    item = empty_line_item()
    if colmap:
        # Numeric `nums` follow header order excluding the product-name column.
        ordered = [
            field
            for field, idx in sorted(
                ((f, i) for f, i in colmap.items() if f != "product_name"),
                key=lambda kv: kv[1],
            )
        ]
        values: Dict[str, float] = {}
        for i, field in enumerate(ordered):
            if i < len(nums):
                values[field] = nums[i]
        item["opening_qty"] = values.get("opening_qty", 0.0)
        item["receipts_qty"] = values.get("receipts_qty", 0.0)
        item["sales_qty"] = values.get("sales_qty", 0.0)
        item["sales_value"] = values.get("sales_value", 0.0)
        item["closing_qty"] = values.get("closing_qty", 0.0)
        item["closing_value"] = values.get("closing_value", 0.0)
        extra: Dict[str, Any] = {}
        if "total_stock" in values:
            extra["total_stock"] = values["total_stock"]
        if "sale_return" in values:
            extra["sale_return"] = values.get("sale_return", 0.0)
        if values.get("sample_qty"):
            extra["sample_qty"] = values["sample_qty"]
        if "exp_damage" in values:
            extra["exp_damage"] = values.get("exp_damage", 0.0)
        if values.get("expiry_qty"):
            extra["expiry_qty"] = values["expiry_qty"]
        if "order_qty" in values:
            extra["order_qty"] = values.get("order_qty", 0.0)
        if extra:
            item["extra"] = extra
        return item

    # Positional fallback. SaleRet layout (Atul Medico / ZANDRA portal):
    # Opening, Purchase, Total, Sale, SaleRet, Exp/Dmg, Closing, Cls Amt, Order Qty
    # Sample layout (legacy): Opening, Purchase, Total, Sale, Sample, Exp/Clos, Closing, Cls Amt
    n = len(nums)
    if layout != "sample":
        layout = "saleret"
    if n >= 8:
        item["opening_qty"] = nums[0]
        item["receipts_qty"] = nums[1]
        item["extra"]["total_stock"] = nums[2]
        item["sales_qty"] = nums[3]
        if layout == "sample":
            if nums[4]:
                item["extra"]["sample_qty"] = nums[4]
            if nums[5]:
                item["extra"]["expiry_qty"] = nums[5]
        else:
            item["extra"]["sale_return"] = nums[4]
            item["extra"]["exp_damage"] = nums[5]
        item["closing_qty"] = nums[6]
        item["closing_value"] = nums[7]
        if n >= 9 and nums[8]:
            item["extra"]["order_qty"] = nums[8]
    elif n == 7:
        item["opening_qty"] = nums[0]
        item["receipts_qty"] = nums[1]
        item["extra"]["total_stock"] = nums[2]
        item["sales_qty"] = nums[3]
        item["closing_qty"] = nums[5]
        item["closing_value"] = nums[6]
    elif n == 6:
        item["opening_qty"] = nums[0]
        item["receipts_qty"] = nums[1]
        item["sales_qty"] = nums[3] if n > 3 else 0.0
        item["extra"]["total_stock"] = nums[2]
        item["closing_qty"] = nums[4]
        item["closing_value"] = nums[5]
    elif n >= 4:
        item["opening_qty"] = nums[0]
        item["receipts_qty"] = nums[1]
        item["sales_qty"] = nums[2]
        item["closing_qty"] = nums[3]
        if n >= 5:
            item["closing_value"] = nums[4]
    elif n:
        item["extra"]["raw_numbers"] = nums
    return item


def _psr_fill_metadata(result: Dict[str, Any], text: str) -> None:
    result["report_title"] = "Product Stock Report"
    m_mfg = re.search(r"Mfg\s*Company\s*:?\s*(.+)$", text, re.I | re.M)
    if m_mfg:
        result["company_name"] = _clean_name(m_mfg.group(1))
    m_range = re.search(
        r"(?:From|FORM)\s*:?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*"
        r"(?:To|:|[-–])\s*:?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
        text,
        re.I,
    )
    if m_range:
        result["period_from"] = _normalize_date(m_range.group(1))
        result["period_to"] = _normalize_date(m_range.group(2))
    if not result.get("stockist_name"):
        for ln in text.splitlines():
            s = ln.strip()
            if not s:
                continue
            if _PRODUCT_STOCK_REPORT_TITLE.search(s):
                continue
            if re.search(r"Mfg\s*Company|From\s*:|Product\s*Name|Opening", s, re.I):
                continue
            result["stockist_name"] = _clean_name(s)
            break


def _parse_product_stock_report(
    text: str, filename: str, source_format: str = "png"
) -> Optional[Dict[str, Any]]:
    """Parse Himalaya/ZANDRA Product Stock Report including Cls Amt values."""
    if not _is_product_stock_report_text(text or ""):
        return None

    result = empty_result(filename, source_format)
    _psr_fill_metadata(result, text)

    colmap: Optional[Dict[str, int]] = None
    layout = _psr_column_layout_from_text(text)
    items: List[Dict[str, Any]] = []
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if _is_psr_header_line(ln):
            headers = _psr_header_tokens(ln)
            colmap = _psr_colmap_from_headers(headers) or None
            layout = _psr_column_layout_from_colmap(colmap) or layout
            continue
        if re.match(r"^(Mfg\s*Company|From\s*:|Product\s+Stock\s+Report)", ln, re.I):
            continue
        if re.match(r"^(SUB\s*TOTAL|GRAND\s*TOTAL|TOTAL)\b", ln, re.I):
            continue
        name, nums = _split_psr_name_and_numbers(ln)
        if not name or len(nums) < 4:
            continue
        if re.search(r"^(Opening|Purchase|ProductName|Product)$", name, re.I):
            continue
        item = _map_psr_numbers(nums, colmap, layout)
        item["product_name"] = name
        items.append(item)

    if not items:
        return None

    result["line_items"] = items
    sum_sales = sum(_to_float(i.get("sales_value")) for i in items)
    sum_closing = sum(_to_float(i.get("closing_value")) for i in items)
    result["totals"]["sales_value"] = sum_sales if sum_sales > 0 else None
    result["totals"]["closing_value"] = sum_closing if sum_closing > 0 else None
    result["totals"]["extra"]["extraction_method"] = "product_stock_report"
    result["totals"]["extra"]["psr_column_layout"] = layout
    result["totals"]["extra"]["stock_identity_kind"] = (
        STOCK_IDENTITY_SAMPLE if layout == "sample" else STOCK_IDENTITY_SALERET
    )
    return _psr_fill_missing_sales(result)


def _merge_psr_header_cells(cells: List[str]) -> List[str]:
    """Join adjacent header cells like 'Cls' + 'Amt' into ClsAmt."""
    out: List[str] = []
    i = 0
    while i < len(cells):
        cur = str(cells[i] or "").strip()
        nxt = str(cells[i + 1] or "").strip() if i + 1 < len(cells) else ""
        pair = f"{cur} {nxt}".strip()
        if nxt and re.search(
            r"^(Cls|Closing|Disc|Stock|Sale|Net)\s+(Amt|Val|Value|Amount)$",
            pair,
            re.I,
        ):
            out.append(_collapse_psr_header_line(pair))
            i += 2
            continue
        if nxt and re.search(r"^Sale\s+(Ret|Return)$", pair, re.I):
            out.append("SaleRet")
            i += 2
            continue
        if nxt and re.search(r"^Exp\s*/?\s*(Dmg|Damage|Clos)$", pair, re.I):
            out.append(_collapse_psr_header_line(pair))
            i += 2
            continue
        if nxt and re.search(r"^Closing\s+Stock$", pair, re.I):
            out.append("Closing")
            i += 2
            continue
        if nxt and re.search(r"^Order\s+Qty$", pair, re.I):
            out.append("OrderQty")
            i += 2
            continue
        out.append(_collapse_psr_header_line(cur))
        i += 1
    return out


def _parse_product_stock_report_from_rows(
    rows: List[List[str]],
    filename: str,
    source_format: str,
    page_text: str,
) -> Optional[Dict[str, Any]]:
    """Table-cell parser for Product Stock Report HTML (keeps Cls Amt aligned)."""
    header_idx = None
    colmap: Dict[str, int] = {}
    for i, cells in enumerate(rows):
        joined = " ".join(cells)
        if not _is_psr_header_line(joined):
            continue
        headers = _merge_psr_header_cells(cells)
        colmap = _psr_colmap_from_headers(headers)
        header_idx = i
        break
    if header_idx is None or "opening_qty" not in colmap:
        return _parse_product_stock_report(page_text, filename, source_format)
    if "closing_value" not in colmap and "sales_value" not in colmap:
        return _parse_product_stock_report(page_text, filename, source_format)

    result = empty_result(filename, source_format)
    _psr_fill_metadata(result, page_text)
    items: List[Dict[str, Any]] = []
    name_idx = colmap.get("product_name", 0)
    for cells in rows[header_idx + 1 :]:
        if len(cells) < 4:
            continue
        name = _clean_name(cells[name_idx] if name_idx < len(cells) else "")
        if not name or re.search(
            r"^(product(\s*name)?|opening|total|sub\s*total|grand\s*total)$",
            name,
            re.I,
        ):
            continue

        def _cell(field: str) -> float:
            idx = colmap.get(field)
            if idx is None or idx >= len(cells):
                return 0.0
            return _to_float(cells[idx])

        item = empty_line_item()
        item["product_name"] = name
        item["opening_qty"] = _cell("opening_qty")
        item["receipts_qty"] = _cell("receipts_qty")
        item["sales_qty"] = _cell("sales_qty")
        item["sales_value"] = _cell("sales_value")
        item["closing_qty"] = _cell("closing_qty")
        item["closing_value"] = _cell("closing_value")
        extra: Dict[str, Any] = {}
        tot = _cell("total_stock")
        extra["total_stock"] = tot
        if "sale_return" in colmap:
            extra["sale_return"] = _cell("sale_return")
        smp = _cell("sample_qty")
        if smp:
            extra["sample_qty"] = smp
        if "exp_damage" in colmap:
            extra["exp_damage"] = _cell("exp_damage")
        exp = _cell("expiry_qty")
        if exp:
            extra["expiry_qty"] = exp
        if "order_qty" in colmap:
            extra["order_qty"] = _cell("order_qty")
        item["extra"] = extra
        items.append(item)

    if not items:
        return _parse_product_stock_report(page_text, filename, source_format)

    result["line_items"] = items
    sum_sales = sum(_to_float(i.get("sales_value")) for i in items)
    sum_closing = sum(_to_float(i.get("closing_value")) for i in items)
    result["totals"]["sales_value"] = sum_sales if sum_sales > 0 else None
    result["totals"]["closing_value"] = sum_closing if sum_closing > 0 else None
    result["totals"]["extra"]["extraction_method"] = "product_stock_report_html"
    layout = _psr_column_layout_from_colmap(colmap) or _psr_column_layout_from_text(page_text)
    result["totals"]["extra"]["psr_column_layout"] = layout
    result["totals"]["extra"]["stock_identity_kind"] = (
        STOCK_IDENTITY_SAMPLE if layout == "sample" else STOCK_IDENTITY_SALERET
    )
    return _psr_fill_missing_sales(result)


def _maybe_repair_product_stock_report(
    result: Dict[str, Any],
    ocr_text: str,
    filename: str,
    source_format: str,
) -> Dict[str, Any]:
    """Replace zero-value Gemini output when Cls Amt can be parsed from OCR/text."""
    if not _product_stock_report_values_missing(result):
        return _psr_fill_missing_sales(result)
    parsed = _parse_product_stock_report(ocr_text, filename, source_format)
    if not parsed or not parsed.get("line_items"):
        return _psr_fill_missing_sales(result)
    if not any(
        _to_float(i.get("closing_value")) > 0 or _to_float(i.get("sales_value")) > 0
        for i in parsed["line_items"]
        if isinstance(i, dict)
    ):
        return _psr_fill_missing_sales(result)
    for key in ("stockist_name", "company_name", "period_from", "period_to", "report_title"):
        if not parsed.get(key) and result.get(key):
            parsed[key] = result[key]
    parsed["source_file"] = result.get("source_file") or filename
    parsed["source_format"] = result.get("source_format") or source_format
    parsed["totals"]["extra"]["repaired_from"] = (
        (result.get("totals") or {}).get("extra") or {}
    ).get("extraction_method")
    return _psr_fill_missing_sales(parsed)


def _reextract_product_stock_report_image(
    result: Dict[str, Any],
    file_bytes: bytes,
    filename: str,
    ext: str,
    mime: str,
    b64: str,
    model: str,
) -> Dict[str, Any]:
    """Second, format-specific vision pass when Cls Amt was dropped. Does not change other formats."""
    from services.vertex_gemini_client import generate_content_via_vertex

    source_format = (ext or ".png").lstrip(".") or "png"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _PRODUCT_STOCK_REPORT_VISION_PROMPT},
                    {"inline_data": {"mime_type": mime, "data": b64}},
                ],
            }
        ],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192},
    }
    try:
        response = generate_content_via_vertex(
            model=model, payload=payload, timeout=120
        )
        parsed = _extract_json_object(_gemini_response_text(response))
        if parsed and parsed.get("line_items"):
            repaired = empty_result(filename, source_format)
            repaired = _apply_parsed_sales_json(repaired, parsed)
            if not _product_stock_report_values_missing(repaired):
                _psr_fill_line_money_totals(repaired)
                repaired["totals"]["extra"]["extraction_method"] = (
                    "product_stock_report_vision"
                )
                repaired["totals"]["extra"]["repaired_from"] = "gemini_vision"
                repaired["totals"]["extra"]["psr_column_layout"] = "saleret"
                repaired["totals"]["extra"]["stock_identity_kind"] = (
                    STOCK_IDENTITY_SALERET
                )
                logger.info(
                    "Product Stock Report vision repair: items=%s closing_value=%s",
                    len(repaired.get("line_items") or []),
                    repaired.get("totals", {}).get("closing_value"),
                )
                return _psr_fill_missing_sales(repaired)
            logger.warning(
                "Product Stock Report vision retry still missing Cls Amt values"
            )
    except Exception as exc:
        logger.warning("Product Stock Report vision retry failed: %s", exc)

    try:
        ocr_text = _ocr_image_to_text(file_bytes)
    except Exception as exc:
        logger.warning("Product Stock Report OCR repair skipped: %s", exc)
        return result
    return _maybe_repair_product_stock_report(
        result, ocr_text, filename, source_format
    )


def _psr_qty_correct_prompt(draft: Dict[str, Any]) -> str:
    compact = []
    for item in draft.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        compact.append(
            {
                "product_name": item.get("product_name"),
                "opening_qty": item.get("opening_qty"),
                "receipts_qty": item.get("receipts_qty"),
                "total_stock": extra.get("total_stock"),
                "sales_qty": item.get("sales_qty"),
                "sale_return": extra.get("sale_return"),
                "exp_damage": extra.get("exp_damage"),
                "closing_qty": item.get("closing_qty"),
                "closing_value": item.get("closing_value"),
            }
        )
    return (
        _PRODUCT_STOCK_REPORT_VISION_PROMPT
        + "\n\nA first pass may have dropped digits. Trust the IMAGE, not this draft.\n"
        + "Correct opening/purchase/total/sale/closing/cls_amt for every row.\n"
        + "DRAFT JSON:\n"
        + json.dumps(compact, default=str)
    )


def _psr_correct_qty_from_image(
    result: Dict[str, Any],
    filename: str,
    ext: str,
    mime: str,
    b64: str,
    model: str,
) -> Dict[str, Any]:
    """PSR-only reread when Sale/Opening digits fail identity or look truncated."""
    from services.vertex_gemini_client import generate_content_via_vertex

    source_format = (ext or ".png").lstrip(".") or "png"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _psr_qty_correct_prompt(result)},
                    {"inline_data": {"mime_type": mime, "data": b64}},
                ],
            }
        ],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8192},
    }
    import time

    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = generate_content_via_vertex(
                model=model, payload=payload, timeout=120
            )
            parsed = _extract_json_object(_gemini_response_text(response))
            if not parsed or not parsed.get("line_items"):
                return result
            corrected = empty_result(filename, source_format)
            corrected = _apply_parsed_sales_json(corrected, parsed)
            if not corrected.get("line_items"):
                return result
            for key in (
                "stockist_name",
                "company_name",
                "period_from",
                "period_to",
                "report_title",
            ):
                if not corrected.get(key) and result.get(key):
                    corrected[key] = result[key]
            corrected["totals"]["extra"]["extraction_method"] = (
                "product_stock_report_vision"
            )
            corrected["totals"]["extra"]["qty_reread"] = True
            corrected["totals"]["extra"]["psr_column_layout"] = "saleret"
            corrected["totals"]["extra"]["stock_identity_kind"] = STOCK_IDENTITY_SALERET
            logger.info(
                "Product Stock Report qty reread: items=%s",
                len(corrected.get("line_items") or []),
            )
            return corrected
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                time.sleep(min(2 ** attempt, 8))
                continue
            logger.warning("Product Stock Report qty reread failed: %s", exc)
            return result
    logger.warning("Product Stock Report qty reread failed: %s", last_exc)
    return result


def _parse_semantic_text_table(
    text: str, filename: str, source_format: str
) -> Optional[Dict[str, Any]]:
    """Extract a stock table from PDF/OCR text using the Excel header aliases."""
    if not text or not text.strip():
        return None
    rows: List[List[Any]] = []
    for ln in text.splitlines():
        raw = ln.replace("\u00a0", " ").strip()
        if not raw:
            continue
        if "\t" in raw:
            cells = [c.strip() for c in raw.split("\t")]
        else:
            cells = [c.strip() for c in re.split(r"\s{2,}", raw) if c.strip()]
        if cells:
            rows.append(cells)
    header_idx, colmap, score = _xls_best_header(rows)
    if header_idx is None or score < 6 or not colmap:
        return None
    result = empty_result(filename, source_format)
    formats: List[List[Optional[str]]] = [[None] * len(r) for r in rows]
    result = _xls_fill_from_rows(result, rows, formats, sheet_name="pdf_text")
    if len(result.get("line_items") or []) < 1:
        return None
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["extraction_method"] = "semantic_text_table"
    extra["parser_used"] = "semantic_text_table"
    return result


def _structure_sales_text(text: str, filename: str, source_format: str) -> Dict[str, Any]:
    """Turn free-form sales-statement text into unified JSON."""
    import os

    # 0) P.S.PHARMACEUTICALS OPENING/RECEIPT/ISSUE/CLOSING (before Gemini)
    ps = _parse_ps_pharma_statement(text, filename)
    if ps and (ps.get("line_items") or ps.get("totals", {}).get("opening_qty") is not None):
        ps["source_format"] = source_format
        return ps

    # 0b) Mahajan-style OpBal/Receipt/Total/Issue/Closing (before Gemini)
    opbal = _parse_opbal_receipt_issue_statement(text, filename, source_format)
    if opbal and opbal.get("line_items"):
        return opbal

    # 0c) Himalaya/ZANDRA Product Stock Report (Opening/Purchase/Total/Sale/Cls Amt)
    psr = _parse_product_stock_report(text, filename, source_format)
    if psr and psr.get("line_items"):
        psr["source_format"] = source_format
        return psr

    # 0d) Saleable Stock Report Particular | Opn | Rec | Issue | Bal
    saleable = _parse_saleable_stock_report(text, filename, source_format)
    if saleable and saleable.get("line_items"):
        saleable["source_format"] = source_format
        return saleable

    # 0e) STOCK STATEMENT ORDER FORM (CODE / PRODUCT NAME / PACK / OPENING)
    order_form = _parse_order_form_stock_statement(text, filename, source_format)
    if order_form and order_form.get("line_items"):
        order_form["source_format"] = source_format
        return order_form

    # 0f) STOCK & SALES ANALYSIS with DUMP + M.EXP (before dash-DUMP SSA)
    ssa_mexp = _parse_ssa_mexp_stock_sales(text, filename, source_format)
    if ssa_mexp and ssa_mexp.get("line_items"):
        ssa_mexp["source_format"] = source_format
        return ssa_mexp

    # 0g) STOCK & SALES ANALYSIS Opening/Receipt/Issue/Closing qty+value + DUMP
    ssa_ori = _parse_ssa_opening_receipt_issue_value(text, filename, source_format)
    if ssa_ori and ssa_ori.get("line_items"):
        ssa_ori["source_format"] = source_format
        return ssa_ori

    # 1) Local TXT heuristics (works well for Kaveri-like layouts)
    local = _parse_txt(text.encode("utf-8", errors="ignore"), filename)
    local["source_format"] = source_format
    if local.get("line_items"):
        local["totals"]["extra"]["extraction_method"] = "pdf_text_heuristic"
        return _apply_total_row_to_result(local, text)

    # 1b) Semantic header-alias table (Mat Name / Item Name / wrapped columns)
    semantic = _parse_semantic_text_table(text, filename, source_format)
    if semantic and semantic.get("line_items"):
        return _apply_total_row_to_result(semantic, text)

    # 2) Gemini text structuring
    try:
        from services.vertex_gemini_client import generate_content_via_vertex

        model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                _SALES_STATEMENT_TEXT_PROMPT
                                + "\n\nDOCUMENT TEXT:\n"
                                + text[:20000]
                            )
                        }
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192},
        }
        response = generate_content_via_vertex(
            model=model, payload=payload, timeout=120
        )
        parsed = _extract_json_object(_gemini_response_text(response))
        if parsed and parsed.get("line_items"):
            result = empty_result(filename, source_format)
            result = _apply_parsed_sales_json(result, parsed)
            result["totals"]["extra"]["extraction_method"] = "pdf_text_gemini"
            return _apply_total_row_to_result(result, text)
    except Exception as exc:
        logger.warning("PDF text Gemini structuring failed: %s", exc)

    # 3) Generic row heuristic parser
    heuristic = _parse_vikash_ocr_text(text, filename, "." + source_format)
    heuristic["source_format"] = source_format
    return _apply_total_row_to_result(heuristic, text)


# ---------------------------------------------------------------------------
# Image (Gemini Vision)
# ---------------------------------------------------------------------------

_SALES_STATEMENT_VISION_PROMPT = """
You extract pharmaceutical STOCK AND SALES / STOCK REPORT statements (NOT tax invoices).

Return ONLY valid JSON (no markdown) with this exact shape:
{
  "stockist_name": string|null,
  "stockist_address": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": string|null,
  "line_items": [
    {
      "product_code": string|null,
      "product_name": string,
      "packing": string|null,
      "opening_qty": number,
      "receipts_qty": number,
      "sales_qty": number,
      "sales_value": number,
      "closing_qty": number,
      "closing_value": number,
      "extra": {}
    }
  ],
  "totals": {
    "sales_value": number|null,
    "closing_value": number|null,
    "extra": {}
  }
}

Rules:
- Ignore handwritten notes, circles, and signatures outside the table.
- On an ORDER FORM (SAP Code, Product, Pack, Qty, Value, two side-by-side tables), a number written in a Qty cell is sales_qty, including handwriting. List every left-table product row first, then every right-table product row. A blank Qty cell is sales_qty 0. The Value column is sales_value and is 0 when that cell is blank. Do not put a Qty number into sales_value, and do not copy Qty from another row.
- stockist_name = the agency/seller header (e.g. NEW VIKASH MEDICAL AGENCY), NOT the manufacturer.
- company_name = manufacturer/division line (e.g. VERITAZ HEALTHCARE LTD).
- Dates like 01/06/26 mean DD/MM/YY (year 26 -> 2026). Never invent years like 2001 or 2030.
- Map Pur.Qnt / Purchase / Receipts / P Qty -> receipts_qty
- Map Sl.Qnt / Sales qty / Issue / S Qty -> sales_qty; Sl.Value / S Val -> sales_value
- Map Cl.Qnt / Closing / Cl Stk -> closing_qty; Cl.Value / Cl Val -> closing_value
- Map Op.Qnt / Opening / OpBal / Op Stk -> opening_qty
- For OpBal|Receipt|Total|Issue|Closing: sales_qty=Issue (NOT Total); Dump is not closing_value.
- "Stock and Sale Statement" grid (Item Cd, Item Name, Op Stk, P Qty, P S Qty, P Val, S Qty, S S Qty, S Val, Cl Stk, Cl Val):
  opening_qty=Op Stk, receipts_qty=P Qty, sales_qty=S Qty (NOT S S Qty),
  sales_value=S Val, closing_qty=Cl Stk, closing_value=Cl Val.
  Put P S Qty in extra.purchase_scheme_qty, P Val in extra.purchase_value,
  S S Qty in extra.sales_scheme_qty.
  Blank cells are 0. Do NOT shift later columns left when a cell is blank.
  This layout has money columns. It is NOT qty-only. Never set sales_value or
  closing_value to 0 when S Val / Cl Val is printed (example: S Qty=2, S Val=495).
- Use 0 only for a cell that is actually blank.
- Include every printed product row.
""".strip()


_SALES_STATEMENT_TEXT_PROMPT = """
You extract pharmaceutical STOCK AND SALES / STOCK REPORT statements from OCR text (NOT tax invoices).

Return ONLY valid JSON (no markdown) with this exact shape:
{
  "stockist_name": string|null,
  "stockist_address": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": string|null,
  "line_items": [
    {
      "product_code": string|null,
      "product_name": string,
      "packing": string|null,
      "opening_qty": number,
      "receipts_qty": number,
      "sales_qty": number,
      "sales_value": number,
      "closing_qty": number,
      "closing_value": number,
      "extra": {}
    }
  ],
  "totals": {
    "sales_value": number|null,
    "closing_value": number|null,
    "extra": {}
  }
}

Rules:
- Ignore handwritten notes.
- stockist_name = agency/seller header; company_name = manufacturer/division.
- Dates like 01/06/26 mean DD/MM/YY (year 26 -> 2026).
- Map Op.Qnt/Opening/OpBal/Op Stk -> opening_qty; Pur.Qnt/Purchase/Receipt/P Qty -> receipts_qty;
  Sl.Qnt/Sales/Issue/S Qty -> sales_qty; Sl.Value/S Val/Amount -> sales_value;
  Cl.Qnt/Closing Balance/Cl Stk -> closing_qty; Cl.Value/Cl Val -> closing_value.
- "Stock and Sale Statement" columns Op Stk | P Qty | P S Qty | P Val | S Qty | S S Qty | S Val | Cl Stk | Cl Val:
  sales_qty is S Qty, not the scheme column S S Qty. sales_value is S Val (rupees).
  closing_qty is Cl Stk, closing_value is Cl Val. Blank cells stay 0 but do not
  shift the next printed number into an earlier column. If S Val or Cl Val is
  printed, this is NOT qty-only — do not zero those amounts.
- For OpBal | Receipt | Total | Issue | Closing columns:
  opening_qty=OpBal, receipts_qty=Receipt, sales_qty=Issue (NOT Total),
  closing_qty=Closing. Total/Dump/NearExpiry go in extra only; never map Total to sales_qty
  or Dump to closing_value. These are qty-only (sales_value=0, totals money=null).
- Many stockist statements are QTY-ONLY (OpBal/Receipt/Total/Issue/Closing).
  For qty-only reports set sales_value=0 on lines and totals.sales_value=null.
  NEVER invent a huge sales_value from a TOTAL row of quantities.
- TOTAL rows like "TOTAL 107256 81014 188271 75374 123024 35426" are quantities,
  not rupees. Do not put those in totals.sales_value / totals.closing_value.
- For P.S.PHARMACEUTICALS style headers OPENING RECEIPT ISSUE CLOSING, the footer
  "TOTAL 290022 230450 292807 262432" must be captured as:
  totals.opening_qty=290022, totals.receipts_qty=230450,
  totals.sales_qty=292807 (ISSUE), totals.closing_qty=262432.
- Reject OCR-merged numbers (e.g. 75374123024). Prefer null over garbage money.
- Use 0 for missing numeric fields. Include every product row with numbers.
""".strip()


def _image_mime(ext: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")


def _ocr_image_to_text(file_bytes: bytes) -> str:
    """Local Tesseract OCR fallback for sales-statement images."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("pytesseract/Pillow required for image OCR fallback") from exc

    import os

    cmd = os.getenv("TESSERACT_CMD", "").strip()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    elif os.name == "nt":
        win_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(win_cmd):
            pytesseract.pytesseract.tesseract_cmd = win_cmd

    image = Image.open(io.BytesIO(file_bytes))
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    # psm 6 (uniform block) reads dense OpBal/Issue qty tables more reliably than default
    return pytesseract.image_to_string(image, config="--psm 6") or ""


def _gemini_response_text(response) -> str:
    data = response.json() if response else {}
    try:
        return (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            or ""
        )
    except Exception:
        return ""


def _apply_parsed_sales_json(
    result: Dict[str, Any], parsed: Dict[str, Any]
) -> Dict[str, Any]:
    for key in (
        "stockist_name",
        "stockist_address",
        "company_name",
        "period_from",
        "period_to",
        "report_title",
    ):
        if parsed.get(key):
            val = parsed[key]
            if key in {"period_from", "period_to"}:
                val = _normalize_date(str(val)) or val
            result[key] = val

    # Common vision mix-up: manufacturer vs stockist agency name
    stockist = (result.get("stockist_name") or "").upper()
    company = (result.get("company_name") or "").upper()
    stockist_looks_mfr = bool(
        re.search(r"HEALTHCARE|PHARMA\s*LTD|LABORATOR|LIMITED", stockist)
    ) and not re.search(r"AGENC|STORES|MEDICO|MEDICAL\s+AGENC", stockist)
    company_looks_stockist = bool(
        re.search(r"AGENC|STORES|MEDICO|MEDICAL\s+AGENC", company)
    )
    if stockist_looks_mfr and company_looks_stockist:
        result["stockist_name"], result["company_name"] = (
            result.get("company_name"),
            result.get("stockist_name"),
        )

    title = result.get("report_title") or ""
    m_range = re.search(
        r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*[-–to]+\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
        title,
        re.I,
    )
    if m_range:
        pf = _normalize_date(m_range.group(1))
        pt = _normalize_date(m_range.group(2))
        if pf and pt:
            result["period_from"], result["period_to"] = pf, pt
    else:
        for pk in ("period_from", "period_to"):
            raw = result.get(pk)
            if not (isinstance(raw, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", raw)):
                continue
            year = int(raw[:4])
            if 2020 <= year <= 2028:
                continue
            yyyy, mm, dd = raw.split("-")
            fixed = _normalize_date(f"{yyyy[2:]}/{mm}/{dd}")
            if fixed:
                result[pk] = fixed
        pf, pt = result.get("period_from"), result.get("period_to")
        if (
            isinstance(pf, str)
            and isinstance(pt, str)
            and re.match(r"^\d{4}-\d{2}-\d{2}$", pf)
            and re.match(r"^\d{4}-\d{2}-\d{2}$", pt)
            and pf[:4] != pt[:4]
        ):
            _y, mm, dd = pt.split("-")
            aligned = f"{pf[:4]}-{mm}-{dd}"
            try:
                datetime.strptime(aligned, "%Y-%m-%d")
                result["period_to"] = aligned
            except ValueError:
                pass

    items = []
    for raw in parsed.get("line_items") or []:
        if not isinstance(raw, dict):
            continue
        item = empty_line_item()
        item["product_code"] = raw.get("product_code")
        item["product_name"] = _clean_name(str(raw.get("product_name") or ""))
        item["packing"] = raw.get("packing")
        item["opening_qty"] = _to_float(raw.get("opening_qty"))
        item["receipts_qty"] = _to_float(raw.get("receipts_qty"))
        item["sales_qty"] = _to_float(raw.get("sales_qty"))
        item["sales_value"] = _to_float(raw.get("sales_value"))
        item["closing_qty"] = _to_float(raw.get("closing_qty"))
        item["closing_value"] = _to_float(raw.get("closing_value"))
        if isinstance(raw.get("extra"), dict):
            item["extra"] = raw["extra"]
        if item["product_name"]:
            items.append(item)
    result["line_items"] = items

    totals = parsed.get("totals") or {}
    if isinstance(totals, dict):
        result["totals"]["sales_value"] = _to_nullable_float(totals.get("sales_value"))
        result["totals"]["closing_value"] = _to_nullable_float(totals.get("closing_value"))
        if isinstance(totals.get("extra"), dict):
            result["totals"]["extra"].update(totals["extra"])
    return result


def _parse_vikash_ocr_text(ocr_text: str, filename: str, ext: str) -> Dict[str, Any]:
    """Heuristic parse for Vikash-style stock report OCR text."""
    # Prefer OpBal/Issue/Closing mapping when that header is present
    opbal = _parse_opbal_receipt_issue_statement(
        ocr_text, filename, (ext or ".pdf").lstrip(".") or "pdf"
    )
    if opbal and opbal.get("line_items"):
        return opbal

    psr = _parse_product_stock_report(
        ocr_text, filename, (ext or ".pdf").lstrip(".") or "pdf"
    )
    if psr and psr.get("line_items"):
        return psr

    result = empty_result(filename, ext.lstrip("."))
    lines = [ln.strip() for ln in ocr_text.splitlines() if ln.strip()]
    if lines:
        # Prefer agency-looking header over manufacturer
        for ln in lines[:8]:
            if re.search(r"AGENC|STORES|MEDICO|MEDICAL", ln, re.I):
                result["stockist_name"] = _clean_name(ln)
                break
        if not result["stockist_name"]:
            result["stockist_name"] = _clean_name(lines[0])
    for ln in lines[:15]:
        if re.search(r"HEALTHCARE|PHARMA\s*LTD|VERITAZ|AUROBINDO", ln, re.I):
            if not re.search(r"AGENC|STORES|MEDICO", ln, re.I):
                result["company_name"] = _clean_name(ln)
        if re.search(r"Stock\s+Report|Stock\s+and\s+Sale", ln, re.I):
            result["report_title"] = _clean_name(ln)
            m = re.search(
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*[-–to]+\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
                ln,
                re.I,
            )
            if m:
                result["period_from"] = _normalize_date(m.group(1))
                result["period_to"] = _normalize_date(m.group(2))

    # Product rows: NAME ... numbers (op, pur, sl, sl_val, cl, cl_val) — flexible
    row_re = re.compile(
        r"^([A-Z0-9][A-Z0-9 \-./']+?)\s+(-?\d+(?:\.\d+)?(?:\s+-?\d+(?:\.\d+)?){2,})$",
        re.I,
    )
    items: List[Dict[str, Any]] = []
    for ln in lines:
        if re.search(r"Product\s*Name|Op\.Qnt|COMPANY\s*Total|Qnt->", ln, re.I):
            continue
        m = row_re.match(ln)
        if not m:
            # looser: split trailing numbers
            nums = re.findall(r"-?\d+(?:\.\d+)?", ln)
            if len(nums) < 3:
                continue
            name_part = re.split(r"\s+-?\d", ln, maxsplit=1)[0].strip()
            if len(name_part) < 3 or re.search(r"Total|Page|Email|Phone", name_part, re.I):
                continue
            floats = [_to_float(n) for n in nums]
        else:
            name_part = m.group(1).strip()
            floats = [_to_float(n) for n in m.group(2).split()]

        item = empty_line_item()
        item["product_name"] = _clean_name(name_part)
        # Common Vikash: Op Pur Sl SlVal Cl ClVal
        if len(floats) >= 6:
            item["opening_qty"] = floats[0]
            item["receipts_qty"] = floats[1]
            item["sales_qty"] = floats[2]
            item["sales_value"] = floats[3]
            item["closing_qty"] = floats[4]
            item["closing_value"] = floats[5]
        elif len(floats) == 5:
            item["opening_qty"] = floats[0]
            item["sales_qty"] = floats[1]
            item["sales_value"] = floats[2]
            item["closing_qty"] = floats[3]
            item["closing_value"] = floats[4]
        elif len(floats) == 4:
            item["opening_qty"] = floats[0]
            item["sales_qty"] = floats[1]
            item["closing_qty"] = floats[2]
            item["closing_value"] = floats[3]
        else:
            item["extra"]["raw_numbers"] = floats
        items.append(item)

    for ln in lines:
        if re.search(r"COMPANY\s*Total|^\s*TOTAL\b", ln, re.I):
            raw_nums = re.findall(r"-?\d+(?:\.\d+)?", ln)
            # Drop OCR-merged monsters (>8 digits) before mapping to money totals
            nums = [_to_float(n) for n in raw_nums if len(n.split(".")[0].lstrip("-")) <= 8]
            if len(nums) >= 2:
                candidate_sales = nums[-2] if len(nums) >= 3 else None
                candidate_closing = nums[-1]
                # Qty-only TOTAL rows (OpBal/Receipt/Issue/Closing) must not become money
                if _is_implausible_money(candidate_sales) or (
                    candidate_sales is not None and float(candidate_sales) > 1_000_000
                    and float(candidate_closing or 0) > 0
                    and float(candidate_sales) > float(candidate_closing) * 100
                ):
                    result["totals"]["extra"]["total_row_qtys"] = nums
                else:
                    result["totals"]["sales_value"] = candidate_sales
                    result["totals"]["closing_value"] = candidate_closing
            break

    result["line_items"] = items
    result["totals"]["extra"]["extraction_method"] = "tesseract_heuristic"
    return result


_SSA_QTY_VALUE_VISION_PROMPT = """
This image is a "STOCK & SALES ANALYSIS" printout with paired QTY + VALUE columns.
It is NOT a Product Stock Report, NOT ZANDRA Stock and Sale, and NOT an OpBal sheet.

Columns LEFT TO RIGHT. A dash or blank cell is 0. NEVER shift a later number left.

1 ITEM DESCRIPTION -> product_name
2 pack / unit (75G, 60, Pcs) -> packing
3 OPENING QTY -> opening_qty
4 OPENING VALUE -> opening_value
5 RECEIPT QTY -> receipts_qty
6 RECEIPT VALUE -> receipts_value
7 ISSUE QTY -> sales_qty
8 ISSUE VALUE -> sales_value
9 CLOSING QTY -> closing_qty
10 CLOSING VALUE -> closing_value
11 DUMP QTY -> extra.dump_qty

Return ONLY valid JSON:
{
  "stockist_name": string|null,
  "stockist_address": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": "STOCK & SALES ANALYSIS",
  "line_items": [
    {
      "product_name": string,
      "packing": string|null,
      "opening_qty": number,
      "opening_value": number,
      "receipts_qty": number,
      "receipts_value": number,
      "sales_qty": number,
      "sales_value": number,
      "closing_qty": number,
      "closing_value": number,
      "extra": {"dump_qty": number}
    }
  ],
  "totals": {"sales_value": number|null, "closing_value": number|null, "extra": {}}
}

Rules:
- stockist_name = agency header (e.g. RAHUL AGENCY), not HIMALAYA ZEAL.
- company_name = HIMALAYA ZEAL / manufacturer line.
- OPENING VALUE is a printed money column. Copy it into opening_value.
  Do not leave opening_value 0 when OPENING VALUE is printed.
- ISSUE QTY is sales_qty. ISSUE VALUE is sales_value.
- DUMP QTY is extra.dump_qty, not closing_qty.
- Skip the HIMALAYA ZEAL banner row and the TOTAL footer row as products.
- Example: AACTARIL SOAP 75G opening 37 / 2648.83, receipt 0 / 0, issue 0 / 0, closing 37 / 2648.83.
- Example: ABANA TABS 60 opening 63 / 9361.80, receipt 200 / value, issue 30 / 4700.06, closing 233 / 34623.80.
- Preserve explicit printed values. Do not recalculate closing_value from qty * rate.
""".strip()


def _looks_like_ssa_qty_value_result(result: Optional[Dict[str, Any]]) -> bool:
    """Busy SSA is qty-only. This printout pairs OPENING/RECEIPT/ISSUE/CLOSING with VALUE."""
    if not isinstance(result, dict):
        return False
    title = str(result.get("report_title") or "")
    if not re.search(r"STOCK\s*&\s*SALES\s*ANALYSIS", title, re.I):
        return False
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    money = sum(
        1
        for item in items
        if _to_float(item.get("closing_value")) > 0
        or _to_float(item.get("sales_value")) > 0
    )
    return money >= 3


def _ssa_qty_value_opening_count(result: Optional[Dict[str, Any]]) -> int:
    count = 0
    for item in (result or {}).get("line_items") or []:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        value = extra.get("opening_value")
        if value is None:
            value = item.get("opening_value")
        if _to_float(value) > 0:
            count += 1
    return count


def _ssa_qty_value_opening_missing(result: Optional[Dict[str, Any]]) -> bool:
    if not _looks_like_ssa_qty_value_result(result):
        return False
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    opened = sum(1 for item in items if _to_float(item.get("opening_qty")) > 0)
    return opened >= 3 and _ssa_qty_value_opening_count(result) == 0


def _apply_ssa_qty_value_fields(
    result: Dict[str, Any], parsed: Dict[str, Any]
) -> Dict[str, Any]:
    result = _apply_parsed_sales_json(result, parsed)
    raw_items = parsed.get("line_items") or []
    for item, raw in zip(result.get("line_items") or [], raw_items):
        if not isinstance(item, dict) or not isinstance(raw, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        item["extra"] = extra
        for key in ("opening_value", "receipts_value", "dump_qty"):
            value = raw.get(key)
            if value is None and isinstance(raw.get("extra"), dict):
                value = raw["extra"].get(key)
            if value is not None:
                extra[key] = _to_float(value)
    extra_t = result.setdefault("totals", {}).setdefault("extra", {})
    extra_t["extraction_method"] = "ssa_qty_value_vision"
    extra_t["layout"] = "ssa_opening_receipt_issue_value"
    return result


def _extract_ssa_qty_value_vision(
    file_bytes: bytes,
    filename: str,
    ext: str = ".jpg",
) -> Optional[Dict[str, Any]]:
    """Read STOCK & SALES ANALYSIS QTY/VALUE printouts. Other layouts return None."""
    import os

    from services.vertex_gemini_client import generate_content_via_vertex

    mime = _image_mime(ext)
    b64 = base64.b64encode(file_bytes).decode("ascii")
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _SSA_QTY_VALUE_VISION_PROMPT},
                    {"inline_data": {"mime_type": mime, "data": b64}},
                ],
            }
        ],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192},
    }
    parsed = None
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = generate_content_via_vertex(
                model=model, payload=payload, timeout=120
            )
            parsed = _extract_json_object(_gemini_response_text(response))
            if parsed and parsed.get("line_items"):
                break
        except Exception as exc:
            last_err = exc
            time.sleep(min(2 ** attempt, 8))
    if last_err and not (parsed and parsed.get("line_items")):
        logger.warning("SSA qty/value vision failed for %s: %s", filename, last_err)
        return None
    if not parsed or not parsed.get("line_items"):
        return None
    result = empty_result(filename, ext.lstrip(".") or "jpg")
    result = _apply_ssa_qty_value_fields(result, parsed)
    if _ssa_qty_value_opening_count(result) == 0:
        return None
    return result


_SWIL_RECEIPT_VALUE_VISION_PROMPT = """
You extract a SwilERP Sales & Stock Statement that prints Qty and Value pairs.

The photo may be rotated 90°. Read the table after mentally rotating it upright.

Columns (left to right):
PRODUCT NAME | PACKING | Opening Qty | Opening Value | Receipt Qty | Receipt/Pur Value
| Total Qty | Issue/Sales Qty | Issue/Sales Value | Closing Qty | Closing Value

Map:
1 PRODUCT NAME -> product_name
2 PACKING -> packing
3 Opening Qty -> opening_qty
4 Opening Value -> opening_value
5 Receipt Qty -> receipts_qty
6 Receipt/Pur Value -> receipts_value
7 Total Qty -> extra.total_stock
8 Issue/Sales Qty -> sales_qty
9 Issue/Sales Value -> sales_value
10 Closing Qty -> closing_qty
11 Closing Value -> closing_value

Return ONLY valid JSON:
{
  "stockist_name": string|null,
  "stockist_address": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": "Sales & Stock Statement",
  "line_items": [
    {
      "product_name": string,
      "packing": string|null,
      "opening_qty": number,
      "opening_value": number,
      "receipts_qty": number,
      "receipts_value": number,
      "sales_qty": number,
      "sales_value": number,
      "closing_qty": number,
      "closing_value": number,
      "extra": {"total_stock": number}
    }
  ],
  "totals": {"sales_value": number|null, "closing_value": number|null, "receipts_value": number|null, "extra": {}}
}

Rules:
- Receipt/Pur Value is printed money. Copy it into receipts_value. Do not leave
  receipts_value missing when Receipt/Pur Value is printed (including 0.00).
- Opening Value is extra.opening_value / opening_value. Do not invent amounts.
- Issue/Sales Value is sales_value. Closing Value is closing_value.
- Skip header, HIMALAYA banner, GRAND TOTAL as a product.
- Period: From DD/MM/YYYY Upto DD/MM/YYYY.
- company_name = HIMALAYA WELLNESS COMPANY / manufacturer, not the stockist.
""".strip()


def _line_receipts_value(item: Dict[str, Any]) -> float:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    for candidate in (
        item.get("receipts_value"),
        extra.get("receipts_value"),
        extra.get("purchase_value"),
    ):
        if candidate not in (None, ""):
            return _to_float(candidate)
    return 0.0


def _looks_like_swil_receipt_pur_result(result: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(result, dict):
        return False
    method = str(
        ((result.get("totals") or {}).get("extra") or {}).get("extraction_method") or ""
    )
    if method in {
        "swil_opening_receipt_value",
        "swil_landscape_qty_value",
        "swil_receipt_pur_value_vision",
    }:
        return False
    title = str(result.get("report_title") or "")
    if not re.search(r"Sales\s*&\s*Stock", title, re.I):
        return False
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    money = sum(
        1
        for item in items
        if _to_float(item.get("sales_value")) > 0
        or _to_float(item.get("closing_value")) > 0
    )
    rec_qty = sum(1 for item in items if _to_float(item.get("receipts_qty")) > 0)
    return len(items) >= 3 and money >= 2 and rec_qty >= 1


def _swil_receipt_value_count(result: Optional[Dict[str, Any]]) -> int:
    return sum(
        1
        for item in (result or {}).get("line_items") or []
        if isinstance(item, dict) and _line_receipts_value(item) > 0
    )


def _swil_receipt_value_missing(result: Optional[Dict[str, Any]]) -> bool:
    return _looks_like_swil_receipt_pur_result(result) and _swil_receipt_value_count(result) == 0


_SWIL_PRINTED_TOTAL_RE = re.compile(
    r"TOTAL\s*\(\s*value[^)]*\)\s*"
    r"(-?[\d,.]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)",
    re.I,
)


def _swil_receipt_item_key(item: Dict[str, Any]) -> Tuple[str, str, float, float, float, float]:
    name = re.sub(r"[^A-Z0-9]+", "", str(item.get("product_name") or "").upper())
    pack = re.sub(r"[^A-Z0-9]+", "", str(item.get("packing") or "").upper())
    if pack and name.endswith(pack):
        name = name[: -len(pack)]
    return (
        name[:20],
        pack,
        round(_to_float(item.get("receipts_qty")), 2),
        round(_to_float(item.get("sales_qty")), 2),
        round(_to_float(item.get("closing_qty")), 2),
        round(_line_receipts_value(item), 2),
    )


def _swil_receipt_items_overlap(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    name_a, pack_a, rec_a, sale_a, close_a, rval_a = _swil_receipt_item_key(left)
    name_b, pack_b, rec_b, sale_b, close_b, rval_b = _swil_receipt_item_key(right)
    names_match = bool(
        name_a
        and name_b
        and (name_a == name_b or name_a.startswith(name_b) or name_b.startswith(name_a))
    )
    if not names_match:
        return False
    cv_a = round(_to_float(left.get("closing_value")), 2)
    cv_b = round(_to_float(right.get("closing_value")), 2)
    if cv_a > 0 and cv_a == cv_b:
        return True
    if pack_a and pack_b and pack_a != pack_b:
        return False
    if (rec_a, sale_a, rval_a) != (rec_b, sale_b, rval_b):
        return False
    if close_a and close_b and close_a != close_b:
        return False
    return True


def _dedupe_swil_receipt_overlap_items(
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Drop continuation-page rows that repeat an earlier product+qty+value."""
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if any(_swil_receipt_items_overlap(prev, item) for prev in out):
            continue
        out.append(item)
    return out


def _parse_swil_receipt_printed_totals(text: str) -> Optional[Dict[str, float]]:
    """Parse TOTAL (value IN Rs.) opening / receipt / sales / closing."""
    if not text:
        return None
    blob = re.sub(r"(\d)\s*[.,]\s+(\d)", r"\1.\2", text)
    match = _SWIL_PRINTED_TOTAL_RE.search(blob)
    if match:
        return {
            "opening_value": _to_float(match.group(1)),
            "receipts_value": _to_float(match.group(2)),
            "sales_value": _to_float(match.group(3)),
            "closing_value": _to_float(match.group(4)),
        }
    for ln in reversed(blob.splitlines()):
        if not re.search(r"\bTOTAL\b", ln, re.I):
            continue
        if not re.search(r"value|Rs\.?", ln, re.I):
            continue
        nums = [_to_float(tok) for tok in re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?", ln)]
        money = [n for n in nums if n == 0 or abs(n) >= 100]
        if len(money) < 4:
            continue
        return {
            "opening_value": money[-4],
            "receipts_value": money[-3],
            "sales_value": money[-2],
            "closing_value": money[-1],
        }
    return None


def _attach_receipts_value(item: Dict[str, Any], rec_val: float) -> Dict[str, Any]:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    extra["receipts_value"] = rec_val
    extra["purchase_value"] = rec_val
    item["extra"] = extra
    ordered: Dict[str, Any] = {}
    for key, val in item.items():
        ordered[key] = val
        if key == "receipts_qty":
            ordered["receipts_value"] = rec_val
    if "receipts_value" not in ordered:
        ordered["receipts_value"] = rec_val
    return ordered


def _apply_swil_receipt_value_fields(
    result: Dict[str, Any], parsed: Dict[str, Any]
) -> Dict[str, Any]:
    result = _apply_parsed_sales_json(result, parsed)
    raw_items = parsed.get("line_items") or []
    applied: List[Dict[str, Any]] = []
    for item, raw in zip(result.get("line_items") or [], raw_items):
        if not isinstance(item, dict) or not isinstance(raw, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        item["extra"] = extra
        open_val = raw.get("opening_value")
        rec_val = raw.get("receipts_value")
        if isinstance(raw.get("extra"), dict):
            if open_val is None:
                open_val = raw["extra"].get("opening_value")
            if rec_val is None:
                rec_val = raw["extra"].get("receipts_value")
                if rec_val is None:
                    rec_val = raw["extra"].get("purchase_value")
        if open_val is not None:
            extra["opening_value"] = _to_float(open_val)
        if rec_val is not None:
            item = _attach_receipts_value(item, _to_float(rec_val))
        applied.append(item)
    result["line_items"] = applied
    extra_t = result.setdefault("totals", {}).setdefault("extra", {})
    extra_t["extraction_method"] = "swil_receipt_pur_value_vision"
    extra_t["layout"] = "swil_receipt_pur_value"
    totals = parsed.get("totals") if isinstance(parsed.get("totals"), dict) else {}
    if totals.get("receipts_value") is not None:
        rec_total = _to_float(totals.get("receipts_value"))
        extra_t["receipts_value"] = rec_total
        result["totals"]["receipts_value"] = rec_total
    return result


def _upright_swil_receipt_image(file_bytes: bytes) -> bytes:
    """Rotate a sideways Sales & Stock screenshot when Receipt/Pur is readable."""
    try:
        from PIL import Image
    except ImportError:
        return file_bytes
    try:
        image = Image.open(io.BytesIO(file_bytes))
    except Exception:
        return file_bytes
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    for deg in (0, 90, 270):
        rot = image if deg == 0 else image.rotate(deg, expand=True)
        buf = io.BytesIO()
        rot.save(buf, format="PNG")
        data = buf.getvalue()
        try:
            text = _ocr_image_to_text(data)
        except Exception:
            continue
        if _is_swil_opening_receipt_value_statement(text):
            return data
    return file_bytes


def _extract_swil_receipt_value_vision(
    file_bytes: bytes,
    filename: str,
    ext: str = ".png",
) -> Optional[Dict[str, Any]]:
    """Read Sales & Stock Receipt/Pur Value. Other layouts return None."""
    import os

    from services.vertex_gemini_client import generate_content_via_vertex

    upright = _upright_swil_receipt_image(file_bytes)
    mime = "image/png" if upright is not file_bytes else _image_mime(ext)
    b64 = base64.b64encode(upright).decode("ascii")
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _SWIL_RECEIPT_VALUE_VISION_PROMPT},
                    {"inline_data": {"mime_type": mime, "data": b64}},
                ],
            }
        ],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192},
    }
    parsed = None
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = generate_content_via_vertex(
                model=model, payload=payload, timeout=120
            )
            parsed = _extract_json_object(_gemini_response_text(response))
            if parsed and parsed.get("line_items"):
                break
        except Exception as exc:
            last_err = exc
            time.sleep(min(2 ** attempt, 8))
    if last_err and not (parsed and parsed.get("line_items")):
        logger.warning("Swil Receipt/Pur vision failed for %s: %s", filename, last_err)
        return None
    if not parsed or not parsed.get("line_items"):
        return None
    result = empty_result(filename, ext.lstrip(".") or "png")
    result = _apply_swil_receipt_value_fields(result, parsed)
    if _swil_receipt_value_count(result) == 0:
        return None
    return result


def _order_form_row_bands(px, width: int, height: int):
    """Ruled product rows beside the Qty column."""
    found = _order_form_row_bands_fixed(px, width, height)
    # A short run is a partial grid. The full table is about 35 rows.
    if len(found) >= 20:
        return found
    scaled = _order_form_row_bands_scaled(px, width, height)
    if len(scaled) > len(found):
        return scaled
    return found


def _order_form_row_bands_fixed(px, width: int, height: int):
    probe_x = int(width * 0.42)
    span = max(1, 70)
    raw = []
    in_band = False
    start = 0
    x_lo = max(0, probe_x - 40)
    x_hi = min(width, probe_x + 80)
    for y in range(int(height * 0.16), int(height * 0.90)):
        dark = sum(1 for x in range(x_lo, x_hi, 2) if px[x, y][0] < 70)
        hot = dark >= span * 0.40
        if hot and not in_band:
            start = y
            in_band = True
        elif not hot and in_band:
            if 1 <= (y - start) <= 12:
                raw.append((start + y - 1) // 2)
            in_band = False
    merged: List[int] = []
    for center in raw:
        if merged and center - merged[-1] < 18:
            merged[-1] = (merged[-1] + center) // 2
        else:
            merged.append(center)
    if len(merged) < 10:
        return []
    gaps = [merged[i + 1] - merged[i] for i in range(len(merged) - 1)]
    body = sorted(gap for gap in gaps if 28 <= gap <= 90)
    if len(body) < 8:
        return []
    pitch = body[len(body) // 2]
    best = (0, 0)
    run_start = None
    for index, gap in enumerate(gaps):
        if abs(gap - pitch) <= max(8, pitch * 0.25):
            if run_start is None:
                run_start = index
        elif run_start is not None:
            if index - run_start > best[1] - best[0]:
                best = (run_start, index)
            run_start = None
    if run_start is not None and (len(gaps) - run_start) > (best[1] - best[0]):
        best = (run_start, len(gaps))
    start_i, end_i = best
    if end_i - start_i < 8:
        return []
    return [(merged[i] + 2, merged[i + 1] - 2) for i in range(start_i, end_i)]


def _order_form_row_bands_scaled(px, width: int, height: int):
    """Same ruled-row search for a smaller or lighter photo of the form."""
    samples = [
        px[x, y][0]
        for y in range(int(height * 0.25), int(height * 0.80), 6)
        for x in range(int(width * 0.15), int(width * 0.85), 6)
    ]
    if not samples:
        return []
    samples.sort()
    rmax = samples[len(samples) // 2] - 40
    if rmax < 60:
        return []
    candidates = []
    for frac in (0.18, 0.28, 0.36, 0.46, 0.56, 0.66, 0.76, 0.86):
        probe_x = int(width * frac)
        x_lo = max(0, probe_x - int(width * 0.03))
        x_hi = min(width, probe_x + int(width * 0.06))
        span = max(1, (x_hi - x_lo) // 2)
        raw: List[int] = []
        in_band = False
        start = 0
        thick = max(4, int(height * 0.004))
        for y in range(int(height * 0.18), int(height * 0.94)):
            dark = sum(1 for x in range(x_lo, x_hi) if px[x, y][0] < rmax)
            hot = dark >= span * 0.45
            if hot and not in_band:
                start = y
                in_band = True
            elif not hot and in_band:
                if 1 <= (y - start) <= thick:
                    raw.append((start + y - 1) // 2)
                in_band = False
        merge_d = max(4, int(height * 0.005))
        merged: List[int] = []
        for center in raw:
            if merged and center - merged[-1] < merge_d:
                merged[-1] = (merged[-1] + center) // 2
            else:
                merged.append(center)
        if len(merged) < 12:
            continue
        # A stroke crossing one rule is seen as two short gaps. Join them
        # back into a single row when they add up to the row pitch.
        joined = True
        while joined and len(merged) >= 12:
            joined = False
            gaps = [merged[i + 1] - merged[i] for i in range(len(merged) - 1)]
            body = sorted(
                gap for gap in gaps if int(height * 0.010) <= gap <= int(height * 0.030)
            )
            if len(body) < 8:
                break
            pitch = body[len(body) // 2]
            limit = max(3, pitch * 0.12)
            for index in range(len(gaps) - 1):
                pair = gaps[index] + gaps[index + 1]
                if (
                    gaps[index] < pitch * 0.65
                    and gaps[index + 1] < pitch * 0.65
                    and abs(pair - pitch) <= limit
                ):
                    del merged[index + 1]
                    joined = True
                    break
        gaps = [merged[i + 1] - merged[i] for i in range(len(merged) - 1)]
        body = sorted(
            gap for gap in gaps if int(height * 0.010) <= gap <= int(height * 0.030)
        )
        if len(body) < 8:
            continue
        pitch = body[len(body) // 2]
        best = (0, 0)
        run_start = None
        for index, gap in enumerate(gaps):
            if abs(gap - pitch) <= max(3, pitch * 0.28):
                if run_start is None:
                    run_start = index
            elif run_start is not None:
                if index - run_start > best[1] - best[0]:
                    best = (run_start, index)
                run_start = None
        if run_start is not None and (len(gaps) - run_start) > (best[1] - best[0]):
            best = (run_start, len(gaps))
        start_i, end_i = best
        if end_i - start_i < 8:
            continue
        candidates.append((
            end_i - start_i,
            merged[start_i],
            [(merged[i] + 1, merged[i + 1] - 1) for i in range(start_i, end_i)],
        ))
    if not candidates:
        return []
    longest = max(item[0] for item in candidates)
    close = [item for item in candidates if item[0] >= longest - 2]
    close.sort(key=lambda item: item[1])
    return close[-1][2]


def _order_form_sparse_qty_window(px, bands, x_start: int, x_end: int, page_width: int):
    """Qty column that holds only a few handwritten numbers."""
    win = max(22, int(page_width * 0.034))
    step = max(4, win // 6)
    rule_hits = max(6, int(len(bands) * 0.28))
    best = None
    for x0 in range(x_start, max(x_start, x_end - win), step):
        x1 = x0 + win
        if any(
            sum(
                1
                for top, bottom in bands
                if any(px[x, y][0] < 80 for y in range(top, bottom, 2))
            )
            >= rule_hits
            for x in range(x0, x1, 2)
        ):
            continue
        scores = [
            _order_form_ink(px, x0, x1, top, bottom, 80)
            for top, bottom in bands
        ]
        ordered = sorted(scores)
        median = ordered[len(ordered) // 2]
        if median > 8:
            continue
        strong = [
            index
            for index, score in enumerate(scores)
            if score >= median + 40 and score >= 40
        ]
        if not (1 <= len(strong) <= 14):
            continue
        total = sum(scores[index] for index in strong)
        if best is None or total > best[0]:
            best = (total, (x0, x1), strong)
    return best


def _order_form_light_pen_window(px, bands, x_start: int, x_end: int, page_width: int):
    """Qty column written with a pen the stricter ink test does not see.

    Printed pack text also darkens, but only further down the table. A real
    Qty column has a number in one of the first rows.
    """
    win = max(22, int(page_width * 0.034))
    step = max(4, win // 6)
    rule_hits = max(6, int(len(bands) * 0.28))
    best = None
    for x0 in range(x_start, max(x_start, x_end - win), step):
        x1 = x0 + win
        if any(
            sum(
                1
                for top, bottom in bands
                if any(px[x, y][0] < 80 for y in range(top, bottom, 2))
            )
            >= rule_hits
            for x in range(x0, x1, 2)
        ):
            continue
        scores = [
            _order_form_ink(px, x0, x1, top, bottom, 105)
            for top, bottom in bands
        ]
        ordered = sorted(scores)
        median = ordered[len(ordered) // 2]
        if median > 30:
            continue
        strong = []
        for index, score in enumerate(scores):
            if score < max(50, median + 20):
                continue
            top, bottom = bands[index]
            ink_rows = [
                y
                for y in range(top, bottom)
                if sum(1 for x in range(x0, x1) if px[x, y][0] < 105) >= 3
            ]
            if len(ink_rows) < 6:
                continue
            if ink_rows[-1] - ink_rows[0] + 1 < (bottom - top) * 0.20:
                continue
            strong.append(index)
        if not (1 <= len(strong) <= 14):
            continue
        if not any(index < 5 for index in strong):
            continue
        total = sum(scores[index] for index in strong)
        if best is None or total > best[0]:
            best = (total, (x0, x1), strong)
    return best


def _order_form_vertical_rules(px, x0, x1, y0, y1, rmax):
    scores = [
        sum(1 for y in range(y0, y1, 3) if px[x, y][0] < rmax)
        for x in range(x0, x1)
    ]
    peak = max(scores) if scores else 0
    rules: List[int] = []
    in_band = False
    start = 0
    for index, score in enumerate(scores):
        hot = bool(peak) and score >= peak * 0.42 and score > 30
        if hot and not in_band:
            start = index
            in_band = True
        elif not hot and in_band:
            segment = scores[start:index]
            rules.append(x0 + start + segment.index(max(segment)))
            in_band = False
    if in_band:
        segment = scores[start:]
        rules.append(x0 + start + segment.index(max(segment)))
    merged: List[int] = []
    for rule in rules:
        if merged and rule - merged[-1] < 14:
            merged[-1] = (merged[-1] + rule) // 2
        else:
            merged.append(rule)
    return merged


def _order_form_ink(px, x0, x1, y0, y1, rmax) -> int:
    return sum(
        1
        for y in range(y0, y1)
        for x in range(x0, x1)
        if px[x, y][0] < rmax
    )


def _order_form_qty_window(px, rules, bands, rmax):
    best = None
    for left, right in zip(rules, rules[1:]):
        width = right - left
        if not (45 <= width <= 170):
            continue
        windows = [(left + 6, right - 6)]
        if width > 80:
            windows.append((right - 68, right - 6))
        for x0, x1 in windows:
            if x1 - x0 < 28:
                continue
            scores = [
                _order_form_ink(px, x0, x1, top, bottom, rmax)
                for top, bottom in bands
            ]
            ordered = sorted(scores)
            median = ordered[len(ordered) // 2]
            peak = ordered[-1]
            if peak < 180:
                continue
            cut = median + 0.35 * (peak - median)
            strong = [
                index
                for index, score in enumerate(scores)
                if score >= cut and score >= median + 150
            ]
            if not (4 <= len(strong) <= 14):
                continue
            gap = min(scores[index] for index in strong) - median
            if best is None or gap > best[0]:
                best = (gap, (x0, x1), strong)
    return best


def _descender_crosses_rule(px, x0, x1, rule_y, height: int) -> bool:
    if rule_y < 6 or rule_y + 6 >= height:
        return False
    mid = x0 + max(8, (x1 - x0) // 2)
    shared = 0
    for x in range(x0, mid):
        above = any(px[x, rule_y - dy][0] < 75 for dy in range(2, 7))
        below = any(px[x, rule_y + dy][0] < 75 for dy in range(2, 7))
        if above and below:
            shared += 1
    return shared >= 3


def _order_form_extra_ring(px, x0: int, x1: int, top: int, bottom: int, width: int, height: int) -> bool:
    """True when a loop drawn to the right of the digits is wider than the digit beside it."""
    x0 = max(0, x0 - 8)
    x1 = min(width, x1 + 24)
    top = max(0, top - 3)
    bottom = min(height, bottom + 4)
    seen = set()
    comps = []
    for y in range(top, bottom):
        for x in range(x0, x1):
            if (x, y) in seen or px[x, y][0] >= 105:
                continue
            stack = [(x, y)]
            seen.add((x, y))
            pts = []
            while stack:
                cx, cy = stack.pop()
                pts.append((cx, cy))
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if nx < x0 or ny < top or nx >= x1 or ny >= bottom or (nx, ny) in seen:
                        continue
                    if px[nx, ny][0] >= 105:
                        continue
                    seen.add((nx, ny))
                    stack.append((nx, ny))
            if len(pts) >= 20:
                xs = [point[0] for point in pts]
                ys = [point[1] for point in pts]
                comps.append((min(xs), max(xs), min(ys), max(ys), len(pts)))
    substantial = [item for item in comps if item[4] >= 40]
    if len(substantial) < 2:
        return False
    substantial.sort(key=lambda item: item[0])
    prev = substantial[-2]
    ring = substantial[-1]
    prev_w = prev[1] - prev[0] + 1
    ring_w = ring[1] - ring[0] + 1
    return ring[0] > prev[1] and ring_w > prev_w + 4


def _drop_borrowed_leading_one(value: int, borrowed: bool, previous: Optional[int]) -> int:
    """A tail from the cell above is not an extra leading 1."""
    if not borrowed or value < 11:
        return value
    text = str(int(value))
    if not text.startswith("1"):
        return value
    if value >= 100:
        return int(text[1:])
    if previous is not None and int(previous) % 10 == 0 and 11 <= value <= 19:
        return int(text[1:])
    return value


def _order_form_upright_image(image):
    """Turn a sideways photo so product rows run horizontally.

    Portrait pages, and landscape pages that already have a full row grid,
    are returned unchanged.
    """
    from PIL import Image

    width, height = image.size
    if width <= height:
        return image
    px = image.load()
    if len(_order_form_row_bands(px, width, height)) >= 20:
        return image

    def header_penalty(turned) -> int:
        tw, th = turned.size
        tpx = turned.load()

        def rules(y0, y1) -> int:
            count = 0
            previous = False
            for y in range(y0, y1, 2):
                dark = sum(
                    1
                    for x in range(int(tw * 0.2), int(tw * 0.8), 4)
                    if tpx[x, y][0] < 90
                )
                hot = dark > tw * 0.08
                if hot and not previous:
                    count += 1
                previous = hot
            return count

        top = rules(int(th * 0.02), int(th * 0.16))
        bottom = rules(int(th * 0.84), int(th * 0.98))
        return top - bottom

    best = None
    for mode in (Image.ROTATE_270, Image.ROTATE_90):
        turned = image.transpose(mode)
        tw, th = turned.size
        tbands = _order_form_row_bands(turned.load(), tw, th)
        if len(tbands) < 20:
            continue
        score = (len(tbands), -header_penalty(turned))
        if best is None or score > best[0]:
            best = (score, turned)
    if best is None:
        return image
    return best[1]


def _read_order_form_qty_cells(file_bytes: bytes, model: str):
    """Read handwritten Qty by ruled-row position, using the existing vision model."""
    from PIL import Image, ImageEnhance, ImageOps

    from services.vertex_gemini_client import generate_content_via_vertex

    image = ImageOps.exif_transpose(Image.open(io.BytesIO(file_bytes))).convert("RGB")
    image = _order_form_upright_image(image)
    width, height = image.size
    px = image.load()
    bands = _order_form_row_bands(px, width, height)
    if len(bands) < 8:
        return None
    y0, y1 = bands[0][0], bands[-1][1]
    left = _order_form_qty_window(
        px, _order_form_vertical_rules(px, 40, width // 2, y0, y1, 55), bands, 50
    )
    right = _order_form_qty_window(
        px,
        _order_form_vertical_rules(px, width // 2, width - 30, y0, y1, 100),
        bands,
        78,
    )
    if not left:
        left = _order_form_sparse_qty_window(px, bands, int(width * 0.08), width // 2, width)
    if not right:
        right = _order_form_sparse_qty_window(
            px, bands, width // 2, int(width * 0.90), width
        )

    def _outside_qty(found, limit: float) -> bool:
        if not found or found[1][1] <= found[1][0]:
            return True
        return found[1][0] < width * limit

    # A window on the page margin, or on printed text beside a lighter pen,
    # is not the Qty column. The known dark-pen forms sit further in and are
    # left on the result above.
    if _outside_qty(left, 0.22):
        light = _order_form_light_pen_window(
            px, bands, int(width * 0.20), width // 2, width
        )
        left = light or None
    if _outside_qty(right, 0.72):
        light = _order_form_light_pen_window(
            px, bands, width // 2, int(width * 0.90), width
        )
        if light:
            right = light
    if not left and not right:
        return None
    if not left:
        left = (0, (0, 0), [])
    if not right:
        right = (0, (0, 0), [])

    def groups(indexes):
        grouped: List[List[int]] = []
        current: List[int] = []
        for index in indexes:
            if current and index != current[-1] + 1:
                grouped.append(current)
                current = []
            current.append(index)
        if current:
            grouped.append(current)
        return grouped

    parts = [{
        "text": (
            "Each image is one or more Qty cells separated by horizontal lines, top to bottom. "
            "The label gives the id and the exact cell count. "
            "Return ONLY JSON {\"reads\":[{\"id\":\"\",\"values\":[number, ...]}]}. "
            "values must contain exactly that many integers, one per cell, top to bottom. "
            "A tail hanging across the line from the cell above is not an extra digit. "
            "A circle drawn around a number is not an extra digit."
        )
    }]
    meta = []
    for side, picked in (("L", left), ("R", right)):
        x0, x1 = picked[1]
        for group in groups(picked[2]):
            top = bands[group[0]][0]
            bottom = bands[group[-1]][1]
            if len(group) > 1:
                pitch = max(20, bands[group[0]][1] - bands[group[0]][0])
                top = max(0, top - int(pitch * 0.45))
                bottom = min(height, bottom + int(pitch * 0.45))
            pad_x = min(110, max(36, (x1 - x0) + 8))
            crop = image.crop((
                max(0, x0 - min(24, pad_x)),
                max(0, top),
                min(width, x1 + pad_x),
                bottom,
            ))
            if crop.height < 90:
                cw, ch = crop.size
                local = crop.load()
                xs = []
                ys = []
                for cy in range(ch):
                    dark_cols = [cx for cx in range(cw) if local[cx, cy][0] < 115]
                    if len(dark_cols) > cw * 0.55:
                        continue
                    for cx in dark_cols:
                        xs.append(cx)
                        ys.append(cy)
                if xs:
                    pad = 5
                    crop = crop.crop((
                        max(0, min(xs) - pad),
                        max(0, min(ys) - pad),
                        min(cw, max(xs) + pad + 1),
                        min(ch, max(ys) + pad + 1),
                    ))
                crop = ImageEnhance.Contrast(crop).enhance(1.6)
                scale = max(4, 180 // max(1, crop.height))
                crop = crop.resize(
                    (crop.width * scale, crop.height * scale),
                    Image.Resampling.LANCZOS,
                )
            buf = io.BytesIO()
            crop.save(buf, format="JPEG", quality=92)
            gid = f"{side}{group[0]}"
            parts.append({"text": f"id={gid} cells={len(group)}"})
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(buf.getvalue()).decode("ascii"),
                }
            })
            meta.append((side, group, x0, x1))

    response = generate_content_via_vertex(
        model=model,
        payload={
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 1024},
        },
        timeout=120,
    )
    parsed = _extract_json_object(_gemini_response_text(response)) or {}
    by_id = {
        str(row.get("id")): row.get("values")
        for row in (parsed.get("reads") or [])
        if isinstance(row, dict)
    }
    left_qty: Dict[int, float] = {}
    right_qty: Dict[int, float] = {}
    for side, group, x0, x1 in meta:
        gid = f"{side}{group[0]}"
        values = by_id.get(gid)
        if not isinstance(values, list) or len(values) != len(group):
            # A blank cell comes back with no number. That must not throw
            # away the numbers read from the other cells.
            if len(group) == 1 and (values is None or values == []):
                continue
            return None
        target = left_qty if side == "L" else right_qty
        previous = None
        for index, raw in zip(group, values):
            try:
                number = int(float(raw))
            except (TypeError, ValueError):
                return None
            if number < 0 or number > 9999:
                return None
            borrowed = _descender_crosses_rule(px, x0, x1 + 40, bands[index][0], height)
            number = _drop_borrowed_leading_one(number, borrowed, previous)
            if number >= 100 and _order_form_extra_ring(
                px, x0, x1, bands[index][0], bands[index][1], width, height
            ):
                number = int(str(number)[:-1])
            target[index] = float(number)
            previous = number
    return {"bands": len(bands), "left": left_qty, "right": right_qty}


def _apply_order_form_handwritten_qty(result: Dict[str, Any], file_bytes: bytes, model: str) -> None:
    """Put each Qty-cell number on that printed row's sales_qty."""
    title = str(result.get("report_title") or "")
    items = result.get("line_items") or []
    if "ORDER FORM" not in title.upper() or len(items) < 8:
        return
    import time

    from services.vertex_gemini_client import GeminiProviderError

    reads = None
    for attempt in range(3):
        try:
            reads = _read_order_form_qty_cells(file_bytes, model)
            break
        except GeminiProviderError as exc:
            logger.warning("Order-form Qty cell read throttled: %s", exc)
            time.sleep(min(2 ** attempt, 8))
        except Exception as exc:
            logger.warning("Order-form Qty cell read failed: %s", exc)
            return
    if not reads:
        return
    left_n = int(reads["bands"])
    if not (left_n < len(items) and len(items) - left_n <= left_n):
        return
    left_items = items[:left_n]
    right_items = items[left_n:]
    if len(right_items) > left_n:
        return

    def paint(rows, qty_by_index):
        for index, item in enumerate(rows):
            extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
            extra["layout"] = "order_form"
            item["extra"] = extra
            if index in qty_by_index:
                item["sales_qty"] = qty_by_index[index]
            else:
                item["sales_qty"] = 0.0
            # The Value column is blank. A whole number there is the Qty mis-filed.
            item["sales_value"] = 0.0

    paint(left_items, reads["left"])
    paint(right_items, reads["right"])


def _parse_image(file_bytes: bytes, filename: str, ext: str) -> Dict[str, Any]:
    """Extract sales statement from image via Gemini Vision, with OCR fallback."""
    import os
    import time

    from services.vertex_gemini_client import (
        GeminiProviderError,
        generate_content_via_vertex,
    )

    result = empty_result(filename, ext.lstrip("."))
    mime = _image_mime(ext)
    b64 = base64.b64encode(file_bytes).decode("ascii")
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()

    vision_payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _SALES_STATEMENT_VISION_PROMPT},
                    {"inline_data": {"mime_type": mime, "data": b64}},
                ],
            }
        ],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192},
    }

    # Retry Gemini Vision briefly on 429/503, then fall back to OCR.
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = generate_content_via_vertex(
                model=model, payload=vision_payload, timeout=120
            )
            text = _gemini_response_text(response)
            parsed = _extract_json_object(text)
            if parsed and (parsed.get("line_items") or parsed.get("stockist_name")):
                result = _apply_parsed_sales_json(result, parsed)
                result["totals"]["extra"]["extraction_method"] = "gemini_vision"
                if _looks_like_zl_opening_bal_sheet(result):
                    zl_sheet = _extract_zl_opening_bal_sheet_vision(
                        file_bytes, filename, ext
                    )
                    if zl_sheet and zl_sheet.get("line_items"):
                        return zl_sheet
                if _looks_like_zandra_stock_sale_result(result):
                    zandra = _extract_zandra_stock_sale_vision(
                        file_bytes, filename, ext
                    )
                    if zandra and zandra.get("line_items"):
                        return zandra
                _apply_order_form_handwritten_qty(result, file_bytes, model)
                if _product_stock_report_values_missing(result):
                    logger.info(
                        "Product Stock Report missing Cls Amt values; retrying format-specific vision"
                    )
                    result = _reextract_product_stock_report_image(
                        result,
                        file_bytes,
                        filename,
                        ext,
                        mime,
                        b64,
                        model,
                    )
                if _is_product_stock_report_result(result):
                    result = _psr_fill_missing_sales(result)
                    if _psr_qty_needs_verify(result):
                        result = _psr_repair_qty_from_ocr(
                            result, file_bytes, filename, ext
                        )
                        result = _psr_fill_missing_sales(result)
                    if _psr_qty_needs_verify(result):
                        logger.info(
                            "Product Stock Report qty identity mismatch; rereading columns"
                        )
                        result = _psr_correct_qty_from_image(
                            result, filename, ext, mime, b64, model
                        )
                        result = _psr_fill_missing_sales(result)
                    if _psr_qty_needs_verify(result):
                        result = _psr_repair_qty_from_ocr(
                            result, file_bytes, filename, ext
                        )
                        result = _psr_fill_missing_sales(result)
                    return result
                if _ssa_qty_value_opening_missing(result):
                    ssa = _extract_ssa_qty_value_vision(file_bytes, filename, ext)
                    if ssa and ssa.get("line_items"):
                        logger.info(
                            "STOCK & SALES ANALYSIS qty/value reread for %s kept opening_value on %s rows",
                            filename,
                            _ssa_qty_value_opening_count(ssa),
                        )
                        return ssa
                if _swil_receipt_value_missing(result):
                    swil = _extract_swil_receipt_value_vision(file_bytes, filename, ext)
                    if swil and swil.get("line_items"):
                        logger.info(
                            "Sales & Stock Receipt/Pur reread for %s kept receipts_value on %s rows",
                            filename,
                            _swil_receipt_value_count(swil),
                        )
                        return swil
                return result
            last_err = ValueError(f"non-JSON vision response: {(text or '')[:200]}")
        except GeminiProviderError as exc:
            last_err = exc
            time.sleep(min(2 ** attempt, 8))
        except Exception as exc:
            last_err = exc
            logger.warning("Gemini vision failed for %s: %s", filename, exc)
            break

    logger.warning(
        "Falling back to Tesseract OCR for sales image %s (%s)",
        filename,
        last_err,
    )

    try:
        ocr_text = _ocr_image_to_text(file_bytes)
    except Exception as exc:
        logger.error("Tesseract OCR failed for %s: %s", filename, exc)
        result["totals"]["extra"]["error"] = f"vision_failed: {last_err}; ocr_failed: {exc}"
        return result

    if len(re.sub(r"\s+", "", ocr_text or "")) < 80:
        upright = _upright_swil_receipt_image(file_bytes)
        if upright is not file_bytes:
            swil = _extract_swil_receipt_value_vision(file_bytes, filename, ext)
            if swil and swil.get("line_items"):
                return swil

    if not ocr_text.strip():
        result["totals"]["extra"]["error"] = "empty OCR text"
        return result

    # Prefer Gemini text structuring of OCR; fall back to heuristic parser.
    text_payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            _SALES_STATEMENT_TEXT_PROMPT
                            + "\n\nOCR TEXT:\n"
                            + ocr_text[:20000]
                        )
                    }
                ],
            }
        ],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192},
    }
    try:
        response = generate_content_via_vertex(
            model=model, payload=text_payload, timeout=120
        )
        parsed = _extract_json_object(_gemini_response_text(response))
        if parsed and parsed.get("line_items"):
            result = _apply_parsed_sales_json(result, parsed)
            result["totals"]["extra"]["extraction_method"] = "tesseract_plus_gemini_text"
            if _swil_receipt_value_count(result) == 0 and (
                _looks_like_swil_receipt_pur_result(result)
                or _is_swil_opening_receipt_value_statement(ocr_text)
            ):
                swil = _extract_swil_receipt_value_vision(file_bytes, filename, ext)
                if swil and swil.get("line_items"):
                    return swil
            return _maybe_repair_product_stock_report(
                result, ocr_text, filename, ext.lstrip(".") or "png"
            )
    except Exception as exc:
        logger.warning("Gemini text structuring after OCR failed: %s", exc)

    heuristic = _parse_vikash_ocr_text(ocr_text, filename, ext)
    if heuristic.get("line_items"):
        return heuristic

    result["totals"]["extra"]["ocr_preview"] = ocr_text[:2000]
    result["totals"]["extra"]["error"] = "could not structure OCR text"
    return result


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
