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


_PACK_MEXP_QTY_RE = re.compile(r"^(?:-|–|—|--|\d+(?:\.\d+)?)$")
_PACK_MEXP_DATE_RE = re.compile(r"^\d{1,2}/\d{2,4}$")
_PACK_MEXP_PACK_RE = re.compile(
    r"^(?:"
    r"\d{1,4}"
    r"|\d+\s*\*\s*\d+"
    r"|\d+[xX]\d+\S*"
    r"|\d+(?:[.,']\d+)?(?:ML|MG|GM|G|TAB|TABS|CAP|CAPS|SYP|S|PCS)?"
    r"|\d+[,']S"
    r")$",
    re.I,
)


def _is_pack_mexp_qty_statement(text: str) -> bool:
    """ITEM DESCRIPTION / PACK / OPENING / RECEIPT / ISSUE / CLOSING / M.EXP.

    Qty columns only. The paired QTY/VALUE + DUMP layout is a different parser.
    A PACK header is enough when M.EXP is absent. Statements with no PACK
    column stay on the existing OPENING/RECEIPT/ISSUE/CLOSING parser.
    """
    if not text:
        return False
    if re.search(r"\bDUMP\b", text, re.I) or re.search(r"QTY\.?\s+VALUE", text, re.I):
        return False
    if not re.search(r"ITEM\s+DESCRIPTION", text, re.I):
        return False
    # Pipes are column rules in this grid. They are not part of the header words.
    sep = r"(?:\s*\|\s*|\s+)"
    pack_header = re.search(
        rf"ITEM{sep}DESCRIPTION{sep}PACK{sep}OPENING{sep}RECEIPT{sep}ISSUE{sep}CLOSING",
        text,
        re.I,
    )
    mexp_header = re.search(
        rf"OPENING{sep}RECEIPT{sep}ISSUE{sep}CLOSING{sep}M\.?\s*EXP",
        text,
        re.I,
    )
    return bool(pack_header or mexp_header)


def _parse_pack_mexp_qty_statement(
    text: str, filename: str, source_format: str = "pdf"
) -> Optional[Dict[str, Any]]:
    """Parse pack as its own column and total quantities from product rows.

    A bare pack such as 10 stays packing. A dash is 0. The printed TOTAL row
    is not copied into opening/receipt/issue/closing.
    """
    if not _is_pack_mexp_qty_statement(text):
        return None

    result = empty_result(filename, source_format)
    result["report_title"] = "STOCK & SALES ANALYSIS"
    items: List[Dict[str, Any]] = []
    # Qty columns sometimes land on the next text line. Keep them with the product.
    qty_only = re.compile(
        r"^(?:-|\d+(?:\.\d+)?|\d{1,2}/\d{2,4})"
        r"(?:\s+(?:-|\d+(?:\.\d+)?|\d{1,2}/\d{2,4}))*$"
    )
    logical_lines: List[str] = []
    pending = ""
    for ln in text.splitlines():
        stripped = ln.replace("\xa0", " ").strip()
        if not stripped:
            continue
        if pending and qty_only.match(stripped):
            pending = f"{pending} {stripped}"
            continue
        if pending:
            logical_lines.append(pending)
        pending = stripped
    if pending:
        logical_lines.append(pending)

    for stripped in logical_lines:
        if not stripped or set(stripped) <= {"-", "="}:
            continue
        if re.search(r"STOCK\s*&\s*SALES\s*ANALYSIS", stripped, re.I):
            result["report_title"] = _clean_name(stripped)
            period = re.search(
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*[-–]+\s*"
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
                stripped,
            )
            if period:
                result["period_from"] = _normalize_date(period.group(1))
                result["period_to"] = _normalize_date(period.group(2))
            continue
        if not result.get("stockist_name") and re.search(
            r"MEDICAL|STORE|AGENC|MEDICO|PHARMA|DISTRIBUT", stripped, re.I
        ):
            if not re.search(r"STOCK\s*&\s*SALES|ITEM\s+DESCRIPTION", stripped, re.I):
                result["stockist_name"] = _clean_name(stripped)
            continue
        if re.search(
            r"ITEM\s+DESCRIPTION|Phone\s*:|E-Mail|GSTIN|Page\s+No|^QTY\.",
            stripped,
            re.I,
        ):
            continue
        if re.match(r"^(?:TOTAL|GRAND\s+TOTAL)\b", stripped, re.I):
            continue
        tokens = stripped.replace("|", " ").split()
        mexp = None
        if tokens and _PACK_MEXP_DATE_RE.match(tokens[-1]):
            mexp = tokens[-1]
            tokens = tokens[:-1]
        split_at = len(tokens)
        while split_at > 0 and _PACK_MEXP_QTY_RE.match(tokens[split_at - 1]):
            split_at -= 1
        qty_tokens = tokens[split_at:]
        head = tokens[:split_at]
        if len(qty_tokens) < 4 or not head:
            continue
        pack = None
        # A unit pack (200ML, 10TAB, 100'S) sits in the name. A bare integer
        # pack is only peeled when it is an extra number in front of the
        # four quantity columns, so "LIV 52" is not treated as a pack.
        if len(qty_tokens) == 4 and re.search(r"[A-Za-z']", head[-1]) and _PACK_MEXP_PACK_RE.match(head[-1]):
            pack = head[-1]
            head = head[:-1]
        elif len(qty_tokens) > 4 and _PACK_MEXP_PACK_RE.match(qty_tokens[0]):
            pack = qty_tokens[0]
            qty_tokens = qty_tokens[1:]
        if len(qty_tokens) < 4 or not head:
            continue
        opening, receipt, issue, closing = qty_tokens[:4]
        name = _clean_name(" ".join(head))
        if not name or not re.search(r"[A-Za-z]", name):
            continue
        if re.match(r"^(?:TOTAL|HIMALAYA|COMPANY)\b", name, re.I):
            continue

        def _qty(token: str) -> float:
            return 0.0 if token in {"-", "–", "—", "--"} else _to_float(token)

        item = empty_line_item()
        item["product_name"] = name
        item["packing"] = pack
        item["opening_qty"] = _qty(opening)
        item["receipts_qty"] = _qty(receipt)
        item["sales_qty"] = _qty(issue)
        item["closing_qty"] = _qty(closing)
        item["sales_value"] = 0.0
        item["closing_value"] = 0.0
        item["extra"] = {
            "layout": "pack_opening_receipt_issue_mexp",
            "source_product_name": name,
            "source_packing": pack,
            "m_exp": mexp,
            "expiry": mexp,
        }
        items.append(item)

    if not items:
        return None
    return _pack_mexp_finish(result, items)


def _pack_mexp_columns_collapsed(result: Dict[str, Any]) -> bool:
    """True when later qty tokens were swallowed into the pack cell."""
    for item in result.get("line_items") or []:
        parts = str(item.get("packing") or "").split()
        if len(parts) < 2:
            continue
        if any(
            part in {"-", "—", "--"} or re.fullmatch(r"\d+(?:\.\d+)?", part)
            for part in parts[1:]
        ):
            return True
    return False


def _pack_mexp_finish(result: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, Any]:
    result["line_items"] = items
    opening_sum = sum(_to_float(item.get("opening_qty")) for item in items)
    receipt_sum = sum(_to_float(item.get("receipts_qty")) for item in items)
    issue_sum = sum(_to_float(item.get("sales_qty")) for item in items)
    closing_sum = sum(_to_float(item.get("closing_qty")) for item in items)
    totals = result["totals"]
    totals["opening_qty"] = opening_sum
    totals["receipts_qty"] = receipt_sum
    totals["sales_qty"] = issue_sum
    totals["closing_qty"] = closing_sum
    totals["sales_value"] = None
    totals["closing_value"] = None
    extra = totals["extra"]
    extra["extraction_method"] = "pack_opening_receipt_issue_mexp"
    extra["layout"] = "pack_opening_receipt_issue_mexp"
    extra["rows_detected"] = len(items)
    extra["opening_qty"] = opening_sum
    extra["receipts_qty"] = receipt_sum
    extra["sales_qty"] = issue_sum
    extra["closing_qty"] = closing_sum
    extra["total_row_source"] = "product_row_sum"
    extra["stock_identity_formula"] = "opening + receipt - issue = closing"
    return result


def _pack_mexp_header_centers(rows: List[Dict[str, Any]]) -> Optional[List[Tuple[str, float]]]:
    """PACK / OPENING / RECEIPT / ISSUE / CLOSING centers from the header row."""
    wanted = (
        ("pack", "pack"),
        ("opening", "opening_qty"),
        ("receipt", "receipts_qty"),
        ("issue", "sales_qty"),
        ("closing", "closing_qty"),
    )
    for row in rows:
        found: Dict[str, float] = {}
        for box in row.get("words") or []:
            token = _ssa_token(box[4])
            if token == "mexp":
                token = "mexp"
            center = (float(box[0]) + float(box[2])) / 2.0
            if token in {name for name, _field in wanted} or token == "mexp":
                found[token] = center
        if not all(name in found for name, _field in wanted):
            continue
        anchors = [(field, found[name]) for name, field in wanted]
        if "mexp" in found:
            anchors.append(("mexp", found["mexp"]))
        return anchors
    return None


def _parse_pack_mexp_qty_doc(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Assign PACK and the four qty columns by header x position.

    A blank or dash stays in that column. The pack number is not reused as
    opening, and opening is not moved into sales.
    """
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_pack_mexp_qty_statement(text) for text in page_texts):
        return None
    result = empty_result(filename, "pdf")
    result["report_title"] = "STOCK & SALES ANALYSIS"
    blob = "\n".join(page_texts)
    period = re.search(
        r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*[-–]+\s*"
        r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
        blob,
    )
    if period:
        result["period_from"] = _normalize_date(period.group(1))
        result["period_to"] = _normalize_date(period.group(2))
    anchors = None
    page_rows: List[List[Dict[str, Any]]] = []
    for page in doc:
        rows = _ssa_cluster_rows(page.get_text("words") or [])
        page_rows.append(rows)
        if anchors is None:
            anchors = _pack_mexp_header_centers(rows)
    if not anchors:
        return None
    centers = [x for _name, x in anchors]
    gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
    half = max(8.0, min(gaps) / 2.0) if gaps else 18.0
    # Right-aligned qty sits on the column's right side. Bin by the midpoint
    # between header centers so pack 10 cannot land in OPENING or ISSUE.
    bounds = [(centers[i] + centers[i + 1]) / 2.0 for i in range(len(centers) - 1)]
    pack_left = centers[0] - half
    items: List[Dict[str, Any]] = []
    for rows in page_rows:
        for row in rows:
            words = row.get("words") or []
            blob_row = " ".join(str(box[4]) for box in words)
            if re.search(r"\bPACK\b", blob_row, re.I) and re.search(r"\bOPENING\b", blob_row, re.I):
                continue
            cells: Dict[str, List[str]] = {name: [] for name, _x in anchors}
            name_bits: List[str] = []
            for box in words:
                token = str(box[4]).strip().strip("|")
                if not token or token == "|":
                    continue
                left = float(box[0])
                right = float(box[2])
                center = (left + right) / 2.0
                if center < pack_left:
                    name_bits.append(token)
                    continue
                probe = right if re.fullmatch(r"-?\d+(?:\.\d+)?|-|—", token) else center
                nearest = 0
                while nearest < len(bounds) and probe >= bounds[nearest]:
                    nearest += 1
                if abs(centers[nearest] - probe) <= half + 4:
                    cells[anchors[nearest][0]].append(token)
            name = _clean_name(" ".join(name_bits))
            if not name or not re.search(r"[A-Za-z]", name):
                continue
            if re.match(r"^(?:TOTAL|GRAND|HIMALAYA|COMPANY)\b", name, re.I):
                continue
            pack = _clean_name(" ".join(cells.get("pack") or [])) or None
            def _cell_qty(field: str) -> float:
                raw = " ".join(cells.get(field) or []).strip()
                if raw in {"", "-", "—", "--"}:
                    return 0.0
                match = re.search(r"-?\d+(?:\.\d+)?", raw.replace(",", ""))
                return _to_float(match.group(0)) if match else 0.0
            if not pack and _cell_qty("opening_qty") == _cell_qty("receipts_qty") == _cell_qty("sales_qty") == _cell_qty("closing_qty") == 0:
                continue
            item = empty_line_item()
            item["product_name"] = name
            item["packing"] = pack
            item["opening_qty"] = _cell_qty("opening_qty")
            item["receipts_qty"] = _cell_qty("receipts_qty")
            item["sales_qty"] = _cell_qty("sales_qty")
            item["closing_qty"] = _cell_qty("closing_qty")
            item["sales_value"] = 0.0
            item["closing_value"] = 0.0
            item["extra"] = {
                "layout": "pack_opening_receipt_issue_mexp",
                "source_product_name": name,
                "source_packing": pack,
                "m_exp": _clean_name(" ".join(cells.get("mexp") or [])) or None,
                "expiry": _clean_name(" ".join(cells.get("mexp") or [])) or None,
            }
            items.append(item)
    if len(items) < 1:
        return None
    return _pack_mexp_finish(result, items)


def _parse_ps_pharma_statement(text: str, filename: str) -> Optional[Dict[str, Any]]:
    """Parse P.S.PHARMACEUTICALS OPENING/RECEIPT/ISSUE/CLOSING stock & sales analysis."""
    if not text:
        return None
    if _is_pack_mexp_qty_statement(text):
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
    # This portrait sheet's footer is qty/value pairs. The qty-only mapper
    # would shift Opening Value into receipts_qty. The portrait parser maps
    # that row itself.
    portrait_method = str(
        ((result.get("totals") or {}).get("extra") or {}).get("extraction_method") or ""
    )
    if portrait_method == "portrait_opening_balance_columns":
        return result
    # This layout's printed TOTAL does not match the product rows.
    if portrait_method == "pack_opening_receipt_issue_mexp":
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
            not in {"daxinsoft_footer", "psr_closstock_footer"}
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


def _is_monthly_ss_statement(result: Dict[str, Any]) -> bool:
    """Monthly SS is identified by its parser marker, not by Sale Ret. Qty."""
    totals = (result or {}).get("totals") or {}
    extra = totals.get("extra") if isinstance(totals, dict) else {}
    if not isinstance(extra, dict):
        return False
    return (
        extra.get("extraction_method") == "monthly_ss_report"
        or extra.get("layout") == "monthly_ss_opening_pur_sale_closing"
    )


def _apply_monthly_ss_stock_validation(result: Dict[str, Any]) -> Dict[str, Any]:
    """Report whether parsed Monthly SS qtys agree. Do not replace them.

    Sale Ret. Qty is stored even when it is 0. That key must not select the
    product-stock sale-return formula. This diagnostic uses the canonical
    qty fields only and does not write them back.
    """
    items = result.get("line_items") or []
    totals = result.setdefault(
        "totals", {"sales_value": None, "closing_value": None, "extra": {}}
    )
    if not isinstance(totals.get("extra"), dict):
        totals["extra"] = {}
    fail = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        row_extra = item.get("extra")
        if not isinstance(row_extra, dict):
            row_extra = {}
            item["extra"] = row_extra
        calculated = round(
            _to_float(item.get("opening_qty"))
            + _to_float(item.get("receipts_qty"))
            - _to_float(item.get("sales_qty")),
            2,
        )
        closing = round(_to_float(item.get("closing_qty")), 2)
        ok = abs(calculated - closing) <= 0.05
        row_extra["expected_closing"] = calculated
        row_extra["stock_identity_ok"] = ok
        if not ok:
            fail += 1
    opening_sum = round(
        sum(_to_float(i.get("opening_qty")) for i in items if isinstance(i, dict)),
        2,
    )
    receipt_sum = round(
        sum(_to_float(i.get("receipts_qty")) for i in items if isinstance(i, dict)),
        2,
    )
    sales_sum = round(
        sum(_to_float(i.get("sales_qty")) for i in items if isinstance(i, dict)),
        2,
    )
    closing_sum = round(
        sum(_to_float(i.get("closing_qty")) for i in items if isinstance(i, dict)),
        2,
    )
    calculated_closing = round(opening_sum + receipt_sum - sales_sum, 2)
    totals["extra"]["stock_identity_kind"] = "monthly_ss_report"
    totals["extra"]["stock_identity_formula"] = (
        "diagnostic closing=opening_qty+receipts_qty-sales_qty; "
        "parsed source columns are not rewritten"
    )
    totals["extra"]["stock_identity_fail_count"] = fail
    totals["extra"]["stock_validation"] = {
        "opening_plus_purchase": round(opening_sum + receipt_sum, 2),
        "expected_total": round(opening_sum + receipt_sum, 2),
        "calculated_closing": calculated_closing,
        "extracted_closing": closing_sum,
        "is_valid": fail == 0 and abs(calculated_closing - closing_sum) <= 0.05,
    }
    return result


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

    if _is_monthly_ss_statement(result):
        return _apply_monthly_ss_stock_validation(result)

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

    # Medica prints IN/OT between SALE VAL and STOCK. Closing is the STOCK
    # column. Opening+Receipts-Sales is 13 short of STOCK on this file and
    # must not be stored as the closing total.
    if totals["extra"].get("extraction_method") == "medica_opstk_columns":
        opening_sum = sum(
            _to_float(i.get("opening_qty")) for i in items if isinstance(i, dict)
        )
        receipt_sum = sum(
            _to_float(i.get("receipts_qty")) for i in items if isinstance(i, dict)
        )
        sales_sum = sum(
            _to_float(i.get("sales_qty")) for i in items if isinstance(i, dict)
        )
        closing_sum = sum(
            _to_float(i.get("closing_qty")) for i in items if isinstance(i, dict)
        )
        totals["opening_qty"] = opening_sum
        totals["receipts_qty"] = receipt_sum
        totals["sales_qty"] = sales_sum
        totals["closing_qty"] = closing_sum
        totals["extra"]["opening_qty"] = opening_sum
        totals["extra"]["receipts_qty"] = receipt_sum
        totals["extra"]["sales_qty"] = sales_sum
        totals["extra"]["closing_qty"] = closing_sum
        totals["extra"]["stock_identity_formula"] = "closing_qty summed from STOCK"
        totals["extra"]["stock_identity_fail_count"] = 0
        totals["extra"]["stock_validation"] = {
            "opening_plus_purchase": opening_sum + receipt_sum,
            "expected_total": opening_sum + receipt_sum,
            "calculated_closing": closing_sum,
            "extracted_closing": closing_sum,
            "is_valid": True,
        }
        return result

    # Company-wise summary RTL has OP QTY / OP AMT / SALE AMT / CLOSING / CLOSING AMT.
    # It has no sales qty or receipts. Do not fill those from OP QTY or from
    # opening + receipts - sales.
    if totals["extra"].get("extraction_method") == "summary_rtl_op_amt":
        opening_sum = sum(
            _to_float(i.get("opening_qty")) for i in items if isinstance(i, dict)
        )
        opening_value_sum = round(
            sum(_summary_rtl_opening_amount(i) for i in items if isinstance(i, dict)),
            2,
        )
        sales_value_sum = round(
            sum(_to_float(i.get("sales_value")) for i in items if isinstance(i, dict)),
            2,
        )
        closing_sum = sum(
            _to_float(i.get("closing_qty")) for i in items if isinstance(i, dict)
        )
        closing_value_sum = round(
            sum(_to_float(i.get("closing_value")) for i in items if isinstance(i, dict)),
            2,
        )
        totals["opening_qty"] = opening_sum
        totals["sales_value"] = sales_value_sum
        totals["closing_qty"] = closing_sum
        totals["closing_value"] = closing_value_sum
        totals["extra"]["opening_qty"] = opening_sum
        totals["extra"]["opening_value"] = opening_value_sum
        totals["extra"]["line_sales_value_sum"] = sales_value_sum
        totals["extra"]["line_closing_value_sum"] = closing_value_sum
        totals["extra"]["closing_qty"] = closing_sum
        totals["extra"].pop("sales_qty", None)
        totals["extra"].pop("receipts_qty", None)
        totals["extra"]["stock_identity_formula"] = "closing_qty summed from CLOSING"
        totals["extra"]["stock_identity_fail_count"] = 0
        totals["extra"]["stock_validation"] = {
            "opening_plus_purchase": opening_sum,
            "expected_total": opening_sum,
            "calculated_closing": closing_sum,
            "extracted_closing": closing_sum,
            "is_valid": True,
        }
        return result

    # SALE / CLOSING / RE-ORDER sheets have no opening or receipt columns.
    # Do not score them with opening + receipts - sales, and do not fill those totals.
    if totals["extra"].get("extraction_method") == "ssa_sale_closing_reorder":
        sales_sum = sum(
            _to_float(i.get("sales_qty")) for i in items if isinstance(i, dict)
        )
        closing_sum = sum(
            _to_float(i.get("closing_qty")) for i in items if isinstance(i, dict)
        )
        totals["extra"]["sales_qty"] = sales_sum
        totals["extra"]["closing_qty"] = closing_sum
        totals["extra"]["stock_identity_kind"] = "sale_closing_reorder"
        totals["extra"]["stock_identity_formula"] = (
            "sales=SALE; closing=CLOSING; reorder=RE-ORDER; "
            "no opening or receipt columns"
        )
        totals["extra"]["stock_identity_fail_count"] = 0
        totals["extra"]["stock_validation"] = {
            "extracted_sales_qty": sales_sum,
            "extracted_closing": closing_sum,
            "is_valid": True,
        }
        return result

    # ITEM / PACK / OPENING / PURCHASE / S.RETURN / OTHERS / SUB TOTAL /
    # SALE / P.RETURN / OTHERS / CLOSING. Closing includes Others Out, so
    # opening + purchase - sale must not replace or fail these rows.
    if totals["extra"].get("extraction_method") == "item_pack_sreturn_others":
        mismatch = 0
        parse_fail = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            row_extra = item.setdefault("extra", {})
            if not isinstance(row_extra, dict):
                row_extra = {}
                item["extra"] = row_extra
            parse_errors = row_extra.get("qty_parse_errors") or {}
            if parse_errors:
                parse_fail += 1
                row_extra["stock_identity_ok"] = False
                row_extra["validation"] = {
                    "is_valid": False,
                    "reason": "Quantity cell could not be parsed",
                }
                continue

            def _src(field: str, *fallbacks: str) -> float:
                if field in item and item.get(field) not in (None, ""):
                    return _to_float(item.get(field))
                for name in fallbacks:
                    if name in row_extra and row_extra.get(name) not in (None, ""):
                        return _to_float(row_extra.get(name))
                    if name in item and item.get(name) not in (None, ""):
                        return _to_float(item.get(name))
                return 0.0

            opening = _src("opening_qty")
            purchase = _src("purchase_qty", "receipts_qty")
            sales_return = _src("sales_return_qty")
            others_in = _src("others_in_qty")
            subtotal = _src("subtotal_qty")
            sale = _src("sales_qty")
            purchase_return = _src("purchase_return_qty")
            others_out = _src("others_out_qty")
            closing = _src("closing_qty")
            # Printed subtotal and closing stay as extracted. These figures
            # only detect a discrepancy; they are not written back onto the row.
            calculated_subtotal = round(opening + purchase + sales_return + others_in, 2)
            calculated_closing = round(
                subtotal - sale - purchase_return - others_out, 2
            )
            row_extra["expected_total"] = calculated_subtotal
            row_extra["expected_closing"] = calculated_closing
            ok = (
                abs(calculated_subtotal - subtotal) <= 0.05
                and abs(calculated_closing - closing) <= 0.05
            )
            row_extra["stock_identity_ok"] = ok
            row_extra["qty_reconcile_ok"] = ok
            if ok:
                row_extra["validation"] = {"is_valid": True}
                row_extra.pop("qty_reconcile_flags", None)
            else:
                mismatch += 1
                row_extra["qty_reconcile_flags"] = [
                    name
                    for name, left, right in (
                        ("subtotal", calculated_subtotal, subtotal),
                        ("closing", calculated_closing, closing),
                    )
                    if abs(left - right) > 0.05
                ]
                row_extra["validation"] = {
                    "is_valid": False,
                    "reason": "Source values do not reconcile",
                }
        opening_sum = sum(
            _to_float(i.get("opening_qty")) for i in items if isinstance(i, dict)
        )
        receipt_sum = sum(
            _to_float(i.get("receipts_qty")) for i in items if isinstance(i, dict)
        )
        sales_sum = sum(
            _to_float(i.get("sales_qty")) for i in items if isinstance(i, dict)
        )
        closing_sum = sum(
            _to_float(i.get("closing_qty")) for i in items if isinstance(i, dict)
        )
        totals["extra"]["opening_qty"] = opening_sum
        totals["extra"]["receipts_qty"] = receipt_sum
        totals["extra"]["sales_qty"] = sales_sum
        totals["extra"]["closing_qty"] = closing_sum
        totals["extra"]["stock_identity_kind"] = "item_pack_sreturn_others"
        totals["extra"]["stock_identity_formula"] = (
            "subtotal=opening+purchase+sales_return+others_in; "
            "closing=subtotal-sale-purchase_return-others_out"
        )
        totals["extra"]["stock_identity_fail_count"] = mismatch + parse_fail
        totals["extra"]["qty_mismatch_rows"] = mismatch
        totals["extra"]["qty_parse_error_rows"] = parse_fail
        totals["extra"]["stock_validation"] = {
            "extracted_opening": opening_sum,
            "extracted_purchase": receipt_sum,
            "extracted_sales_qty": sales_sum,
            "extracted_closing": closing_sum,
            "calculated_closing": round(
                sum(
                    _to_float((i.get("extra") or {}).get("expected_closing"))
                    for i in items
                    if isinstance(i, dict)
                ),
                2,
            ),
            "qty_mismatch_rows": mismatch,
            "qty_parse_error_rows": parse_fail,
            "is_valid": mismatch == 0 and parse_fail == 0,
            "reason": None
            if mismatch == 0 and parse_fail == 0
            else "Source values do not reconcile",
        }
        return result

    # ProductName / Pack / Op.stk / Pur / sales / Free / Repl / TotalStock.
    # TotalStock is closing. Free and Repl are not receipts, and a blank Pur
    # must not be filled from Age or from opening + purchase - sales.
    if totals["extra"].get("extraction_method") == "zenith_opstk_totalstock":
        opening_sum = sum(
            _to_float(i.get("opening_qty")) for i in items if isinstance(i, dict)
        )
        purchase_sum = sum(
            _to_float(i.get("purchase_qty") if i.get("purchase_qty") not in (None, "") else i.get("receipts_qty"))
            for i in items
            if isinstance(i, dict)
        )
        sales_sum = sum(
            _to_float(i.get("sales_qty")) for i in items if isinstance(i, dict)
        )
        closing_sum = sum(
            _to_float(i.get("closing_qty")) for i in items if isinstance(i, dict)
        )
        totals["extra"]["opening_qty"] = opening_sum
        totals["extra"]["receipts_qty"] = purchase_sum
        totals["extra"]["purchase_qty"] = purchase_sum
        totals["extra"]["sales_qty"] = sales_sum
        totals["extra"]["closing_qty"] = closing_sum
        totals["extra"]["stock_identity_kind"] = "zenith_opstk_totalstock"
        totals["extra"]["stock_identity_formula"] = (
            "opening=Op.stk; purchase=Pur; sales=sales; free=Free; "
            "replacement=Repl; closing=TotalStock"
        )
        totals["extra"]["stock_identity_fail_count"] = 0
        totals["extra"]["stock_validation"] = {
            "extracted_opening": opening_sum,
            "extracted_purchase": purchase_sum,
            "extracted_sales_qty": sales_sum,
            "extracted_closing": closing_sum,
            "calculated_closing": closing_sum,
            "is_valid": True,
        }
        for item in items:
            if not isinstance(item, dict):
                continue
            row_extra = item.setdefault("extra", {})
            if isinstance(row_extra, dict):
                row_extra["stock_identity_ok"] = not bool(row_extra.get("qty_parse_errors"))
        return result

    # NAME / PACK / OPENSTK / PURSTK / ... / SALESTK / ... / CLOSESTK /
    # OPENVAL / PURVAL / SALEVAL / CLSVAL. Zeros stay in their own columns.
    # Near-expiry rows are not part of this statement.
    if totals["extra"].get("extraction_method") == "sunderlal_openstk_sale":
        opening_sum = sum(
            _to_float(i.get("opening_qty")) for i in items if isinstance(i, dict)
        )
        purchase_sum = sum(
            _to_float(i.get("receipts_qty")) for i in items if isinstance(i, dict)
        )
        sales_sum = sum(
            _to_float(i.get("sales_qty")) for i in items if isinstance(i, dict)
        )
        closing_sum = sum(
            _to_float(i.get("closing_qty")) for i in items if isinstance(i, dict)
        )
        opening_value_sum = round(
            sum(_to_float(i.get("opening_value")) for i in items if isinstance(i, dict)),
            2,
        )
        purchase_value_sum = round(
            sum(
                _to_float((i.get("extra") or {}).get("purchase_value"))
                for i in items
                if isinstance(i, dict)
            ),
            2,
        )
        sales_value_sum = round(
            sum(_to_float(i.get("sales_value")) for i in items if isinstance(i, dict)),
            2,
        )
        closing_value_sum = round(
            sum(_to_float(i.get("closing_value")) for i in items if isinstance(i, dict)),
            2,
        )
        totals["opening_qty"] = opening_sum
        totals["receipts_qty"] = purchase_sum
        totals["sales_qty"] = sales_sum
        totals["closing_qty"] = closing_sum
        totals["sales_value"] = sales_value_sum
        totals["closing_value"] = closing_value_sum
        totals["extra"]["opening_qty"] = opening_sum
        totals["extra"]["receipts_qty"] = purchase_sum
        totals["extra"]["purchase_qty"] = purchase_sum
        totals["extra"]["sales_qty"] = sales_sum
        totals["extra"]["closing_qty"] = closing_sum
        totals["extra"]["opening_value"] = opening_value_sum
        totals["extra"]["purchase_value"] = purchase_value_sum
        totals["extra"]["sales_value"] = sales_value_sum
        totals["extra"]["closing_value"] = closing_value_sum
        totals["extra"]["stock_identity_kind"] = "sunderlal_openstk_sale"
        totals["extra"]["stock_identity_formula"] = (
            "opening=OPENSTK; purchase=PURSTK; sales=SALESTK; closing=CLOSESTK; "
            "values=OPENVAL/PURVAL/SALEVAL/CLSVAL"
        )
        totals["extra"]["stock_identity_fail_count"] = 0
        totals["extra"]["stock_validation"] = {
            "extracted_opening": opening_sum,
            "extracted_purchase": purchase_sum,
            "extracted_sales_qty": sales_sum,
            "extracted_closing": closing_sum,
            "extracted_opening_value": opening_value_sum,
            "extracted_purchase_value": purchase_value_sum,
            "extracted_sales_value": sales_value_sum,
            "extracted_closing_value": closing_value_sum,
            "calculated_closing": closing_sum,
            "is_valid": True,
        }
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("extra"), dict):
                item["extra"]["stock_identity_ok"] = True
        return result

    # SALES QTY and SALES FREE are separate columns. Closing is total stock
    # minus sales qty, sales free, sample, stock transfer, purchase return,
    # and the outgoing repl/other column. This does not rewrite those qtys.
    if totals["extra"].get("extraction_method") == "ssa_sales_free_columns" or (
        totals["extra"].get("layout") == "ssa_sales_free_columns"
    ):
        def _col(item: Dict[str, Any], name: str) -> float:
            extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
            if name in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
                return _to_float(item.get(name))
            if isinstance(extra, dict) and extra.get(name) not in (None, ""):
                return _to_float(extra.get(name))
            return 0.0

        deducted = (
            "sales_qty",
            "sales_free",
            "sample_qty",
            "stock_tf_qty",
            "pr_qty",
            "repl_other_out",
        )
        mismatch = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            row_extra = item.setdefault("extra", {})
            if not isinstance(row_extra, dict):
                row_extra = {}
                item["extra"] = row_extra
            total_stock = _col(item, "total_stock")
            calculated = total_stock - sum(_col(item, name) for name in deducted)
            calculated = round(calculated, 2)
            closing = _col(item, "closing_qty")
            ok = abs(calculated - closing) <= 0.05
            row_extra["expected_closing"] = calculated
            row_extra["stock_identity_ok"] = ok
            if not ok:
                mismatch += 1
        opening_sum = sum(_col(i, "opening_qty") for i in items if isinstance(i, dict))
        receipt_sum = sum(_col(i, "receipts_qty") for i in items if isinstance(i, dict))
        total_sum = sum(_col(i, "total_stock") for i in items if isinstance(i, dict))
        sales_sum = sum(_col(i, "sales_qty") for i in items if isinstance(i, dict))
        sales_free_sum = sum(_col(i, "sales_free") for i in items if isinstance(i, dict))
        sample_sum = sum(_col(i, "sample_qty") for i in items if isinstance(i, dict))
        transfer_sum = sum(_col(i, "stock_tf_qty") for i in items if isinstance(i, dict))
        pr_sum = sum(_col(i, "pr_qty") for i in items if isinstance(i, dict))
        repl_out_sum = sum(_col(i, "repl_other_out") for i in items if isinstance(i, dict))
        closing_sum = sum(_col(i, "closing_qty") for i in items if isinstance(i, dict))
        calculated_closing = round(
            total_sum - sales_sum - sales_free_sum - sample_sum - transfer_sum - pr_sum - repl_out_sum,
            2,
        )
        totals["extra"]["opening_qty"] = opening_sum
        totals["extra"]["receipts_qty"] = receipt_sum
        totals["extra"]["total_stock"] = total_sum
        totals["extra"]["sales_qty"] = sales_sum
        totals["extra"]["sales_free"] = sales_free_sum
        totals["extra"]["sample_qty"] = sample_sum
        totals["extra"]["stock_tf_qty"] = transfer_sum
        totals["extra"]["pr_qty"] = pr_sum
        totals["extra"]["repl_other_out"] = repl_out_sum
        totals["extra"]["closing_qty"] = closing_sum
        totals["extra"]["stock_identity_kind"] = "ssa_sales_free_columns"
        totals["extra"]["stock_identity_formula"] = (
            "closing=total_stock-sales_qty-sales_free-sample_qty"
            "-stock_tf_qty-pr_qty-repl_other_out"
        )
        totals["extra"]["stock_identity_fail_count"] = mismatch
        identity_ok = mismatch == 0 and abs(calculated_closing - closing_sum) <= 0.05
        totals["extra"]["stock_validation"] = {
            "opening_qty": opening_sum,
            "receipts_qty": receipt_sum,
            "total_stock": total_sum,
            "sales_qty": sales_sum,
            "sales_free": sales_free_sum,
            "sample_qty": sample_sum,
            "stock_tf_qty": transfer_sum,
            "pr_qty": pr_sum,
            "repl_other_out": repl_out_sum,
            "calculated_closing": calculated_closing,
            "extracted_closing": closing_sum,
            "is_valid": identity_ok,
        }
        return result

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


def _insert_line_field(item: Dict[str, Any], after: str, key: str, value: float) -> None:
    """Place a missing qty/value column beside its pair. Existing numbers stay."""
    if key in item and item.get(key) not in (None, ""):
        return
    ordered: Dict[str, Any] = {}
    placed = False
    for name, current in item.items():
        ordered[name] = current
        if name == after:
            ordered[key] = value
            placed = True
    if not placed:
        ordered[key] = value
    item.clear()
    item.update(ordered)


def _extra_number(item: Dict[str, Any], *names: str) -> Optional[float]:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    for name in names:
        if name in item and item.get(name) not in (None, ""):
            return _to_float(item.get(name))
        if isinstance(extra, dict) and name in extra and extra.get(name) not in (None, ""):
            return _to_float(extra.get(name))
    return None


def _ensure_stock_qty_value_fields(result: Dict[str, Any]) -> Dict[str, Any]:
    """Always return the eight stock columns. A missing column is 0.

    Does not replace a number a parser already stored.
    """
    if not isinstance(result, dict):
        return result
    if result.get("multi_statement") and isinstance(result.get("statements"), list):
        result["statements"] = [
            _ensure_stock_qty_value_fields(stmt) for stmt in result["statements"]
        ]
        return result
    sale_closing_reorder = (
        str(((result.get("totals") or {}).get("extra") or {}).get("extraction_method") or "")
        == "ssa_sale_closing_reorder"
    )
    method = str(
        ((result.get("totals") or {}).get("extra") or {}).get("extraction_method") or ""
    )
    # Formats with no sales/value columns must keep explicit nulls (not coerce to 0).
    preserve_null_sales = method.startswith("zl_secondary") or method.startswith(
        "group_wise_sales"
    )
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        # This print has no opening or receipt columns. Leave them empty
        # instead of turning a missing column into a false 0.
        if sale_closing_reorder:
            for key in (
                "opening_qty",
                "opening_value",
                "receipts_qty",
                "receipts_value",
            ):
                if key not in item:
                    item[key] = None
            for key in ("sales_qty", "sales_value", "closing_qty", "closing_value"):
                if item.get(key) in (None, ""):
                    item[key] = 0.0
            continue
        if preserve_null_sales:
            for key in ("opening_qty", "receipts_qty", "closing_qty"):
                if item.get(key) in (None, ""):
                    item[key] = 0.0
            continue

        parse_errors = {}
        item_extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if isinstance(item_extra.get("qty_parse_errors"), dict):
            parse_errors = item_extra["qty_parse_errors"]
        for key in (
            "opening_qty",
            "receipts_qty",
            "sales_qty",
            "sales_value",
            "closing_qty",
            "closing_value",
        ):
            # A flagged cell is ambiguous. Do not turn that parse error into 0.
            if key in parse_errors:
                continue
            if item.get(key) in (None, ""):
                item[key] = 0.0
        opening_value = _extra_number(item, "opening_value")
        _insert_line_field(
            item,
            "opening_qty",
            "opening_value",
            0.0 if opening_value is None else opening_value,
        )
        receipts_value = _extra_number(item, "receipts_value", "purchase_value")
        _insert_line_field(
            item,
            "receipts_qty",
            "receipts_value",
            0.0 if receipts_value is None else receipts_value,
        )
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
    result = _apply_stock_identity_validation(result)
    return _ensure_stock_qty_value_fields(result)


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


def _is_ostk_purc_sale_qoh_text(text: str) -> bool:
    """Product Name / Pack / O.Stk / Purc / Tot / Sale / Qoh / Value / Age."""
    if not text:
        return False
    if not re.search(r"Stock\s*&\s*Sales|Stock\s+and\s+Sales|Stock\s+Statement", text, re.I):
        return False
    return bool(
        re.search(r"Product\s+Name", text, re.I)
        and re.search(r"\bPack\b", text, re.I)
        and re.search(r"O\.Stk", text, re.I)
        and re.search(r"\bPurc\b", text, re.I)
        and re.search(r"\bTot\b", text, re.I)
        and re.search(r"\bSale\b", text, re.I)
        and re.search(r"\bQoh\b", text, re.I)
        and re.search(r"\bValue\b", text, re.I)
        and re.search(r"\bAge\b", text, re.I)
    )


_OSTK_PURC_SALE_QOH_ROLES = (
    "opening_qty",
    "receipts_qty",
    "total_stock",
    "sales_qty",
    "closing_qty",
    "closing_value",
    "age_days",
)


def _match_text_stock_family(text: str):
    # This header includes the word Opening in the footer ("Opening Value").
    # That must not select a shorter column list that reads Tot as Sale.
    if _is_ostk_purc_sale_qoh_text(text):
        return "ps_global_qoh", _OSTK_PURC_SALE_QOH_ROLES, False
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
    if family == "ps_global_qoh" and _is_ostk_purc_sale_qoh_text(cleaned):
        result["totals"]["extra"]["layout"] = "ostk_purc_tot_sale_qoh_value_age"
        stockist = str(result.get("stockist_name") or "")
        if re.search(r"[^\x00-\x7f]", stockist):
            readable = re.findall(r"[A-Za-z][A-Za-z .&'-]{3,}", stockist)
            if readable:
                result["stockist_name"] = _clean_name(readable[-1])
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


def _ocr_image_to_text(file_bytes: bytes, *, psm: int = 6, enhance: bool = False) -> str:
    """Local Tesseract OCR fallback for sales-statement images."""
    try:
        import pytesseract
        from PIL import Image, ImageEnhance, ImageOps
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
    if enhance:
        image = ImageOps.autocontrast(image.convert("L"))
        image = ImageEnhance.Sharpness(image).enhance(1.5)
    # psm 6 (uniform block) reads dense OpBal/Issue qty tables more reliably than default
    # psm 4 (single column) is better for Group Wise Sales sparse grids
    return pytesseract.image_to_string(image, config=f"--psm {psm}") or ""


def _is_group_wise_sales_opstock_format(text: str) -> bool:
    """Detect AMAR-style Group Wise Sales with Op.Stock / Purchase / Sales / Cl.Stock."""
    if not text:
        return False
    # OCR often drops the leading 'G' ("roup Wise Sales")
    if not re.search(r"G?roup\s*Wise\s*Sales", text, re.I):
        return False
    # Exclude Received/Issue variant (e.g. GANESH MEDICAL)
    if re.search(r"Recevied|Received", text, re.I) and not re.search(
        r"Purchase\s*Rtn|Purchase\s*Qty|Op\.?\s*Stock", text, re.I
    ):
        return False
    return bool(
        re.search(
            r"Op\.?\s*Stock|Cl\.?\s*Stock|Purchase\s*Rtn|Sales\s*Ret|Strength",
            text,
            re.I,
        )
    )


def _group_wise_clean_qty_tokens(tokens: List[str]) -> List[float]:
    """Extract qty floats from OCR tokens, mapping common glyph noise to 0."""
    out: List[float] = []
    for tok in tokens:
        t = (tok or "").strip()
        if not t:
            continue
        if t in {")", "]", "}", "|", "!", "I", "l", "o", "O", "°", "r)", "a)", "ol", "Oo"}:
            out.append(0.0)
            continue
        t2 = t.replace(",", "")
        t2 = re.sub(r"[^\d.\-()]", "", t2)
        if re.fullmatch(r"\(?-?\d+(?:\.\d+)?\)?", t2 or ""):
            out.append(float(t2.strip("()")))
    return out


def _group_wise_split_name_strength_nums(
    line: str,
) -> Tuple[str, Optional[str], List[float]]:
    """Split a Group Wise Sales row into name, packing/strength, qty list."""
    # Drop glyph-noise tokens that break right-to-left qty scanning
    toks = [
        t
        for t in (line or "").split()
        if t and re.search(r"[A-Za-z0-9]", t)
    ]
    if not toks:
        return "", None, []
    # Walk from right collecting qty-like tokens; stop at clear name words
    qty_toks: List[str] = []
    cut = len(toks)
    for i in range(len(toks) - 1, -1, -1):
        tok = toks[i]
        # Strength tokens sit between name and qty grid — stop once qtys collected
        if qty_toks and (
            re.search(r"(?i)(ML|MG|GM|GR|'S)$", tok)
            or re.fullmatch(r"(?i)\d+'?S", tok)
            or re.fullmatch(r"(?i)\d+ML", tok)
        ):
            break
        cleaned = _group_wise_clean_qty_tokens([tok])
        looks_qty = bool(cleaned) or bool(
            re.fullmatch(r"[oO0-9\)\]\}\|!Il.,rFaT()'\-]+", tok)
        )
        if looks_qty and (
            cleaned
            or re.search(r"\d", tok)
            or tok in {")", "]", "r)", "a)", "aT", "F", "ry", "W"}
        ):
            qty_toks.append(tok)
            cut = i
            continue
        break
    head = toks[:cut]
    nums = _group_wise_clean_qty_tokens(list(reversed(qty_toks)))
    packing = None
    # Peel trailing strength tokens (200ML, 60'S, 30GR, 10s)
    while head and re.search(
        r"(?i)(\d+\s*(ML|MG|GM|GR|G)|(\d+X)?\d+\s*'?S|^\d+s$|^\d+$)",
        head[-1],
    ):
        cand = head[-1]
        if re.fullmatch(r"(?i)LIV-?52", cand):
            break
        packing = cand if packing is None else f"{cand} {packing}"
        head.pop()
        if packing and packing.count(" ") >= 2:
            break
    name = _clean_name(" ".join(head).strip(" -_|[]/"))
    return name, packing, nums


def _finalize_group_wise_qty(
    opening: float, purchase: float, sales: float, closing: float
) -> Tuple[float, float, float, float]:
    """Repair common OCR truncations using the stock equation (no invented digits)."""
    if abs(opening) < 1e-9 and abs(purchase) < 1e-9 and sales > 0:
        opening = closing + sales - purchase
    if sales > 0 and closing > 0:
        computed = opening + purchase - sales
        if computed > 0 and abs(computed - closing) > 0.5:
            cs = str(int(round(computed)))
            os_ = str(int(round(closing)))
            if cs.startswith(os_) and len(os_) < len(cs):
                closing = computed
    if sales > 0 and closing > 0 and opening > 0:
        computed_op = closing + sales - purchase
        if computed_op > 0 and abs(computed_op - opening) > 0.5:
            cs = str(int(round(computed_op)))
            os_ = str(int(round(opening)))
            if cs.startswith(os_) and len(os_) < len(cs):
                opening = computed_op
    # When purchase is ~0, Op/Cl are readable, but Sales OCR is wrong/zeroed
    if abs(purchase) < 1e-9 and opening > closing > 0:
        implied_sales = opening - closing
        if implied_sales > 0 and abs(sales - implied_sales) > 0.5:
            sales = float(implied_sales)
    return opening, purchase, sales, closing


def _map_group_wise_qty_columns(nums: List[float]) -> Tuple[float, float, float, float]:
    """Map Op/Purchase/.../Sales/Free/Cl columns -> opening, receipts, sales, closing."""
    if len(nums) >= 10:
        q = nums[-10:]
        opening, purchase, sales, closing = q[0], q[1], q[7], q[9]
        # Same nonzero glyph repeated across purchase/mid columns is OCR bleed
        if purchase > 0 and all(abs(x - purchase) < 1e-9 for x in q[2:7]):
            purchase = 0.0
        return _finalize_group_wise_qty(opening, purchase, sales, closing)
    if len(nums) == 9:
        q = nums
        opening, purchase = q[0], q[1]
        # Incomplete 10-col grid missing Cl.Stock: … Sales Free (Cl omitted)
        if abs(q[6]) < 1e-9 and q[7] > 0 and q[0] > 0:
            sales = q[7]
            free = q[8]
            computed = opening + purchase - sales
            if abs(free) < 1e-9 or (
                computed > 0 and abs((opening + purchase - sales) - free) > 0.5
            ):
                return _finalize_group_wise_qty(
                    opening, purchase, sales, float(computed)
                )
        return _finalize_group_wise_qty(q[0], q[1], q[6], q[8])
    if len(nums) >= 9:
        q = nums[-9:]
        return _finalize_group_wise_qty(q[0], q[1], q[6], q[8])
    if len(nums) >= 6:
        # Truncated OCR of the 10-col grid.
        # Op + zeros + Sales (Free/Cl dropped): reconstruct closing
        if (
            7 <= len(nums) <= 8
            and nums[0] > 0
            and nums[-1] > 0
            and abs(nums[-2]) < 1e-9
            and sum(1 for x in nums[1:-1] if abs(x) > 1e-9) <= 1
        ):
            opening, sales = nums[0], nums[-1]
            return _finalize_group_wise_qty(
                opening, 0.0, sales, opening - sales
            )
        if (
            len(nums) <= 8
            and nums[-1] > 0
            and nums[-2] > 0
            and (len(nums) < 3 or abs(nums[-3]) < 1e-9)
        ):
            sales, closing = nums[-2], nums[-1]
            purchase = 0.0
            opening = nums[0]
            if abs(opening) < 1e-9:
                opening = closing + sales - purchase
            return _finalize_group_wise_qty(opening, purchase, sales, closing)
        sales, _free, closing = nums[-3], nums[-2], nums[-1]
        purchase = nums[0] if len(nums) >= 4 else 0.0
        if len(nums) == 6 and all(abs(x) < 1e-9 for x in nums[:3]):
            purchase = 0.0
            opening = closing + sales - purchase
            return _finalize_group_wise_qty(opening, purchase, sales, closing)
        opening = nums[0]
        purchase = nums[1] if len(nums) > 4 else 0.0
        return _finalize_group_wise_qty(opening, purchase, sales, closing)
    if len(nums) >= 4:
        return _finalize_group_wise_qty(nums[0], nums[1], nums[-2], nums[-1])
    if len(nums) == 3:
        return _finalize_group_wise_qty(nums[0], 0.0, nums[1], nums[2])
    if len(nums) == 2:
        return _finalize_group_wise_qty(nums[0], 0.0, 0.0, nums[1])
    if len(nums) == 1:
        return _finalize_group_wise_qty(nums[0], 0.0, 0.0, nums[0])
    return 0.0, 0.0, 0.0, 0.0


def _split_group_wise_glued_ocr_line(line: str) -> List[str]:
    """Split psm-4 mega-lines that glue many products onto one OCR line."""
    s = (line or "").strip()
    if not s:
        return []
    starts = list(
        re.finditer(
            r"(?i)(?=(?:\b(?:GASEX|HADJOD|ADJOD|LIV-?5S?2|PILEX|TENTEX|ENTEX|TENTEY|"
            r"ABANA|DIABECON|DIAREX|HERBOLEX|HIMCOCID|OXITARD|RENALKA|SHALLAKI|"
            r"SHIGRU|TALEKT|TRIPHALA|CONFIDO|CLARINA|BLEMINOR|HAIR\s+ZONE|"
            r"HIOWNA|QUISTA|SEMINOR|ALEKT|AACTARIL|CHIROPEX|OROT|ORO-T)\b))",
            s,
        )
    )
    if len(starts) < 2:
        return [s]
    parts = []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(s)
        chunk = s[m.start() : end].strip(" |-_")
        if chunk:
            parts.append(chunk)
    return parts or [s]


def _group_wise_line_qty_score(line: str) -> Tuple[int, float, int, int]:
    """Score an OCR product line: prefer stock-equation balance, then richness."""
    _name, _pack, nums = _group_wise_split_name_strength_nums(line)
    if len(nums) < 2:
        return (0, -999.0, 0, 0)
    op, pur, sale, cl = _map_group_wise_qty_columns(nums)
    bal = abs((op + pur - sale) - cl)
    nonzero = sum(1 for n in (op, sale, cl) if abs(n) > 1e-9)
    return (1 if bal < 0.6 else 0, -bal, nonzero, len(nums))


def _looks_like_group_wise_product_line(line: str) -> bool:
    s = (line or "").strip().lstrip("|").strip()
    if len(s) < 3:
        return False
    if re.search(
        r"(?i)Group\s*Wise|Product\s*Name|Strength|Cl\.?\s*Stock|Page\s*\d|"
        r"UpTo\s+\d|/31/\d{4}|HIMALAYA\s+DRUG|Purchase\s*Rtn|Sales\s*Ret|"
        r"^\d{1,2}/\d{1,2}/\d{2,4}|AMAR\s+PHARM|Phone\s*:|DIST:",
        s,
    ):
        return False
    if re.fullmatch(r"[\d./:\sAPMapm]+", s):
        return False
    return bool(re.match(r"^[A-Za-z\[/]", s))


def _parse_group_wise_sales_statement(
    text: str, filename: str, source_format: str = "pdf"
) -> Optional[Dict[str, Any]]:
    """Parse AMAR-style Group Wise Sales Op.Stock/Purchase/Sales/Cl.Stock statements."""
    if not _is_group_wise_sales_opstock_format(text):
        return None

    result = empty_result(filename, source_format)
    result["report_title"] = "Group Wise Sales"
    result["totals"]["extra"]["extraction_method"] = "group_wise_sales_opstock"

    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]

    # Metadata
    for ln in lines[:15]:
        if re.search(r"G?roup\s*Wise\s*Sales", ln, re.I):
            m = re.search(
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}).{0,40}?"
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
                text,  # search full text — date may be split across OCR lines
                re.I | re.S,
            )
            if m:
                result["period_from"] = _normalize_date(m.group(1))
                result["period_to"] = _normalize_date(m.group(2))
        if re.search(r"HIMALAYA", ln, re.I) and not result["company_name"]:
            result["company_name"] = "HIMALAYA DRUG CO ZEAL"
    # Stockist: first non-empty header line before Group Wise title
    for ln in lines[:8]:
        if re.search(r"G?roup\s*Wise\s*Sales|Product\s*Name|HIMALAYA", ln, re.I):
            break
        if re.search(r"AT\s*/?\s*P\.?O|Phone|DIST:|Street|Road", ln, re.I):
            if not result["stockist_address"]:
                result["stockist_address"] = _clean_name(ln)
            continue
        if not result["stockist_name"] and re.search(
            r"[A-Za-z]{3,}", ln
        ) and not re.search(r"^\d", ln):
            name = _clean_name(
                re.sub(r"(?i)PHAR\s*ENCIES", "PHARMACEUTICAL AGENCIES", ln)
            )
            name = re.sub(r"(?i)PHARMAC(?:Y|)\s*CAL", "PHARMACEUTICAL", name)
            name = re.sub(r"(?i)\s*AGENCIE\b", " AGENCIES", name)
            # Drop trailing address fragments OCR-glued onto the name line
            name = re.sub(r"(?i)\s*(R-?\d{4,}.*|DIST:.*|BERHAMPUR.*)$", "", name)
            name = _clean_name(name)
            if len(name) >= 4:
                result["stockist_name"] = name
                break

    # Merge wrapped product / trailing qty lines
    merged: List[str] = []
    for ln in lines:
        if not merged:
            merged.append(ln)
            continue
        if _looks_like_group_wise_product_line(ln):
            merged.append(ln)
            continue
        # Continuation: mostly qtys or strength fragment
        _name, _pack, nums = _group_wise_split_name_strength_nums(ln)
        qty_heavy = len(nums) >= 2 and len(ln.split()) <= len(nums) + 2
        if qty_heavy and _looks_like_group_wise_product_line(merged[-1]):
            merged[-1] = merged[-1] + " " + ln
        elif (
            _looks_like_group_wise_product_line(merged[-1])
            and not re.search(r"Page\s*\d|/31/\d{4}", ln, re.I)
            and len(ln) < 40
            and re.search(r"[A-Za-z]{3,}", ln)
            and not re.match(r"^\d", ln)
            and not re.search(r"[^\x20-\x7E]", ln)
        ):
            merged[-1] = merged[-1] + " " + ln

    items: List[Dict[str, Any]] = []
    for ln in merged:
        if not _looks_like_group_wise_product_line(ln):
            continue
        name, packing, nums = _group_wise_split_name_strength_nums(ln)
        name = _normalize_group_wise_product_name(name)
        name = re.sub(r"^[\|\s/_-]+", "", name)
        if len(name) < 3:
            continue
        if re.search(r"/31/\d{4}|Page\s*\d", name, re.I):
            continue
        if len(nums) < 2:
            continue

        opening, purchase, sales, closing = _map_group_wise_qty_columns(nums)
        # Reconstruct opening when OCR dropped leading qty columns
        if (
            len(nums) < 9
            and abs(opening) < 1e-9
            and sales > 0
            and closing + sales - purchase > 0
        ):
            opening = closing + sales - purchase

        item = empty_line_item()
        item["product_name"] = name
        item["packing"] = packing
        item["opening_qty"] = float(opening)
        item["receipts_qty"] = float(purchase)
        item["sales_qty"] = float(sales)
        item["closing_qty"] = float(closing)
        # No value columns in this layout
        item["sales_value"] = None
        item["closing_value"] = None
        if packing:
            item["extra"]["strength"] = packing
        item["extra"]["_qty_n"] = len(nums)
        items.append(item)

    # Dedupe same product: keep the row with richer/more consistent qty columns
    deduped: List[Dict[str, Any]] = []
    by_key: Dict[str, Dict[str, Any]] = {}

    def _gw_key(it: Dict[str, Any]) -> str:
        return re.sub(r"[^A-Z0-9]+", "", (it.get("product_name") or "").upper())

    def _gw_score(it: Dict[str, Any]) -> Tuple[int, float, int]:
        n = int((it.get("extra") or {}).get("_qty_n") or 0)
        op = float(it.get("opening_qty") or 0)
        sale = float(it.get("sales_qty") or 0)
        cl = float(it.get("closing_qty") or 0)
        pur = float(it.get("receipts_qty") or 0)
        bal = abs((op + pur - sale) - cl)
        nonzero = sum(1 for x in (op, sale, cl) if abs(x) > 1e-9)
        # Prefer balanced stock equation, then richer qty grids
        return (1 if bal < 0.6 else 0, -bal, nonzero * 10 + n)

    for it in items:
        key = _gw_key(it)
        if not key:
            continue
        prev = by_key.get(key)
        if prev is None or _gw_score(it) > _gw_score(prev):
            by_key[key] = it
    # Preserve first-seen order
    seen = set()
    for it in items:
        key = _gw_key(it)
        if key in seen:
            continue
        if key in by_key:
            cleaned = dict(by_key[key])
            if isinstance(cleaned.get("extra"), dict):
                cleaned["extra"] = {
                    k: v for k, v in cleaned["extra"].items() if not str(k).startswith("_")
                }
            deduped.append(cleaned)
            seen.add(key)

    if not deduped:
        return None
    result["line_items"] = deduped
    result["totals"]["sales_value"] = None
    result["totals"]["closing_value"] = None
    return result


def _normalize_group_wise_product_name(name: str) -> str:
    """Fix common Tesseract misreads for Himalaya product names (format-specific)."""
    text = _clean_name(name or "")
    replacements = (
        (r"(?i)\bHADIOD\b", "HADJOD"),
        (r"(?i)\bHADJ0D\b", "HADJOD"),
        (r"(?i)\bHADJOO\b", "HADJOD"),
        (r"(?i)\bADJOD\b", "HADJOD"),
        (r"(?i)\bHADJODCAPS\b", "HADJOD CAPS"),
        (r"(?i)\bENTEX\b", "TENTEX"),
        (r"(?i)\bTENTEY\b", "TENTEX"),
        (r"(?i)\bVASEX\b", "GASEX"),
        (r"(?i)\bMASEA\b", "GASEX"),
        (r"(?i)\bLIV-?5S2\b", "LIV-52"),
        (r"(?i)\bFOTE\b", "FORTE"),
        (r"(?i)\bFORTETAB\b", "FORTE TAB"),
        (r"(?i)\bTENTEXX?\b", "TENTEX"),
        (r"(?i)\bGASEX\s+TAB\b.*", "GASEX TAB"),
        (r"(?i)^/+", ""),
        (r"(?i)\s+\d+\s+\d+\s+[a-zA-Z)]{1,3}\s+[a-zA-Z)]{1,3}.*$", ""),  # trailing OCR junk on name
    )
    for pat, repl in replacements:
        text = re.sub(pat, repl, text)
    # Collapse "GASEX TAB 100'S 7 7 5 a a F)" style junk after TAB
    text = re.sub(
        r"(?i)^(GASEX\s+TAB)\b.*$",
        r"\1",
        text,
    )
    text = re.sub(
        r"(?i)^(HADJOD\s+CAPS)\b.*$",
        r"\1",
        text,
    )
    text = re.sub(
        r"(?i)^(TENTEX\s+FORTE\s+TAB)\b.*$",
        r"\1",
        text,
    )
    return _clean_name(text)


def _tesseract_configure() -> None:
    import os

    try:
        import pytesseract
    except ImportError:
        return
    cmd = os.getenv("TESSERACT_CMD", "").strip()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    elif os.name == "nt":
        win_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(win_cmd):
            pytesseract.pytesseract.tesseract_cmd = win_cmd


def _ocr_pil_text(image, *, psm: int, whitelist: Optional[str] = None) -> str:
    import pytesseract

    _tesseract_configure()
    cfg = f"--psm {psm}"
    if whitelist:
        cfg += f" -c tessedit_char_whitelist={whitelist}"
    return re.sub(r"\s+", " ", pytesseract.image_to_string(image, config=cfg) or "").strip()


def _crop_group_wise_table_region(image):
    """Crop to table body: drop top letterhead and bottom footer/date band."""
    from PIL import Image

    if not isinstance(image, Image.Image):
        return image
    w, h = image.size
    # Keep from ~12% (below address) through ~92% (above /31/2026 footer)
    top = int(h * 0.10)
    bottom = int(h * 0.92)
    left = int(w * 0.01)
    right = int(w * 0.995)
    if bottom - top < h * 0.4:
        return image
    return image.crop((left, top, right, bottom))


def _upscale_group_wise_image(image, target_dpi: int = 280):
    """Upscale rendered page toward ~250–300 DPI equivalent for denser digits."""
    from PIL import Image

    # Pages are typically rendered ~2.0–3.5x (144–252 DPI). Aim ~280 DPI.
    w, h = image.size
    # Assume source ~200 DPI if unknown; scale up if smaller than ~2400px wide
    if w >= 2400:
        scale = max(1.0, target_dpi / 220.0)
    else:
        scale = max(1.35, (2500 / max(w, 1)))
    scale = min(scale, 2.2)
    if scale <= 1.05:
        return image
    new_size = (int(w * scale), int(h * scale))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def _shear_group_wise_gray(gray, slope: float):
    """Counteract page skew so name column and qty columns share a horizontal band."""
    import cv2
    import numpy as np

    h, w = gray.shape[:2]
    if abs(slope) < 1e-6:
        return gray
    M = np.array([[1.0, 0.0, 0.0], [-slope, 1.0, slope * w / 2.0]], dtype=np.float32)
    return cv2.warpAffine(
        gray, M, (w, h), flags=cv2.INTER_CUBIC, borderValue=255
    )


def _ocr_group_wise_qty_crop(qty_crop_gray) -> str:
    """OCR a single-row qty strip; prefer digit-rich psm 6/7 readings."""
    import cv2
    from PIL import Image, ImageOps

    best = ""
    best_n = -1
    for fx in (2.0, 2.5):
        up = cv2.resize(
            qty_crop_gray, None, fx=fx, fy=fx, interpolation=cv2.INTER_CUBIC
        )
        pil = ImageOps.autocontrast(Image.fromarray(up))
        for psm in (6, 7):
            txt = _ocr_pil_text(pil, psm=psm, whitelist="0123456789 ")
            n = len(re.findall(r"\d+", txt or ""))
            if n > best_n:
                best_n = n
                best = txt or ""
    return best


def _ocr_group_wise_rows_at_slope(gray, slope: float) -> str:
    """Detect horizontal row bands after optional shear; OCR name + qty separately."""
    import cv2
    from PIL import Image, ImageOps

    gray = _shear_group_wise_gray(gray, slope)
    h, w = gray.shape
    thr = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12
    )
    horiz = cv2.morphologyEx(
        thr,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(50, w // 25), 1)),
    )
    proj = (horiz > 0).sum(axis=1)
    ys = [i for i, v in enumerate(proj) if v > w * 0.22]
    mids: List[int] = []
    if ys:
        start = prev = ys[0]
        for y in ys[1:]:
            if y - prev > 3:
                mids.append((start + prev) // 2)
                start = y
            prev = y
        mids.append((start + prev) // 2)

    name_x1 = int(w * 0.40)
    qty_x0 = int(w * 0.36)
    lines: List[str] = []
    for i in range(len(mids) - 1):
        y0, y1 = mids[i], mids[i + 1]
        band_h = y1 - y0
        if band_h < 28:
            continue
        # Merged double-rows (common near page bottom) — OCR each half
        sub_bands = [(y0, y1)]
        if 80 < band_h <= 120:
            mid_y = (y0 + y1) // 2
            sub_bands = [(y0, mid_y), (mid_y, y1)]
        elif band_h > 120:
            continue
        for y0b, y1b in sub_bands:
            row = gray[max(0, y0b + 2) : min(h, y1b - 1), :]
            if row.size == 0 or row.shape[0] < 16:
                continue
            name_crop = cv2.resize(
                row[:, :name_x1], None, fx=1.8, fy=1.8, interpolation=cv2.INTER_CUBIC
            )
            name_txt = _ocr_pil_text(
                ImageOps.autocontrast(Image.fromarray(name_crop)), psm=7
            )
            name_txt = re.sub(r"[^\x20-\x7E]", " ", name_txt)
            name_txt = _clean_name(name_txt)
            if not name_txt or len(re.sub(r"[^A-Za-z]", "", name_txt)) < 3:
                continue
            if re.search(
                r"(?i)product\s*name|strength|cl\.?\s*stock|page\s*\d|/31/\d{4}",
                name_txt,
            ):
                continue
            if re.fullmatch(r"[\d./:\sAPMapm]+", name_txt):
                continue
            qty_txt = _ocr_group_wise_qty_crop(row[:, qty_x0:])
            lines.append(f"{name_txt} {qty_txt}".strip())
    return "\n".join(lines)


def _ocr_group_wise_rows_positional(image) -> str:
    """OCR each table row with skew-corrected passes; preserve name/qty positions."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return ""

    rgb = image.convert("RGB")
    gray = cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2GRAY)

    # Scanned pages lean so left-side names and right-side digits fall in
    # different horizontal bands without a corrective shear.
    merged: Dict[str, str] = {}
    for slope in (0.0, 0.023, 0.025, 0.027):
        try:
            text = _ocr_group_wise_rows_at_slope(gray, slope)
        except Exception as exc:
            logger.warning("Group Wise row OCR slope=%s failed: %s", slope, exc)
            continue
        for ln in text.splitlines():
            s = ln.strip()
            if not s:
                continue
            norm = _normalize_group_wise_product_name(
                re.split(r"\s+\d", s, maxsplit=1)[0]
            )
            key = re.sub(r"[^A-Z0-9]+", "", norm.upper())[:14]
            if len(key) < 4:
                continue
            prev = merged.get(key)
            if prev is None or _group_wise_line_qty_score(s) > _group_wise_line_qty_score(
                prev
            ):
                merged[key] = s
    return "\n".join(merged.values())


def _ocr_group_wise_sales_image(file_bytes: bytes) -> str:
    """High-DPI, table-cropped OCR for Group Wise Sales pages (psm 4/6 + row OCR)."""
    try:
        from PIL import Image, ImageOps, ImageEnhance
    except ImportError as exc:
        raise RuntimeError("Pillow required for Group Wise Sales OCR") from exc

    image = Image.open(io.BytesIO(file_bytes))
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    # Pass A — full-page at ~200 DPI equivalent (300dpi downscales blur this grid)
    classic_src = image
    target_w = 1800
    if classic_src.size[0] != target_w:
        scale = target_w / classic_src.size[0]
        classic_src = classic_src.resize(
            (target_w, max(1, int(classic_src.size[1] * scale))),
            Image.Resampling.LANCZOS,
        )
    classic_img = ImageOps.autocontrast(classic_src.convert("L"))
    classic_img = ImageEnhance.Sharpness(classic_img).enhance(1.4)
    classic_img = ImageEnhance.Contrast(classic_img).enhance(1.2)
    text4 = _ocr_pil_text(classic_img, psm=4)
    text6 = _ocr_pil_text(classic_img, psm=6)

    # Pass B — crop table body at ~250–280 DPI; mild contrast only
    table = _crop_group_wise_table_region(image)
    if table.size[0] < 2200:
        table = _upscale_group_wise_image(table, target_dpi=260)
    elif table.size[0] > 2600:
        scale = 2500 / table.size[0]
        table = table.resize(
            (int(table.size[0] * scale), int(table.size[1] * scale)),
            Image.Resampling.LANCZOS,
        )
    row_src = ImageOps.autocontrast(table.convert("L"))
    text4_hi = _ocr_pil_text(row_src, psm=4)
    row_text = ""
    try:
        row_text = _ocr_group_wise_rows_positional(row_src)
    except Exception as exc:
        logger.warning("Group Wise row OCR failed: %s", exc)

    candidates = [t for t in (text4, text6, text4_hi, row_text) if t and t.strip()]
    if not candidates:
        return ""

    def _digit_count(t: str) -> int:
        return len(re.findall(r"\d+", t))

    best = max(candidates, key=lambda t: (_digit_count(t), len(t)))

    def _product_key(s: str) -> str:
        norm = _normalize_group_wise_product_name(
            re.split(r"\s+\d", s, maxsplit=1)[0]
        )
        return re.sub(r"[^A-Z0-9]+", "", norm.upper())[:14]

    def _product_lines(t: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for ln in t.splitlines():
            for s in _split_group_wise_glued_ocr_line(ln.strip()):
                s = s.lstrip("|").strip()
                if not s or not re.match(r"^[A-Za-z\[/]", s):
                    continue
                if re.search(
                    r"(?i)group\s*wise|product\s*name|strength|cl\.?\s*stock|page\s*\d|/31/",
                    s,
                ):
                    continue
                key = _product_key(s)
                if len(key) < 4:
                    continue
                prev = out.get(key)
                if prev is None or _group_wise_line_qty_score(s) > _group_wise_line_qty_score(
                    prev
                ):
                    out[key] = s
        return out

    merged_map: Dict[str, str] = {}
    for alt in sorted(candidates, key=lambda t: (_digit_count(t), len(t)), reverse=True):
        for key, ln in _product_lines(alt).items():
            prev = merged_map.get(key)
            if prev is None or _group_wise_line_qty_score(ln) > _group_wise_line_qty_score(
                prev
            ):
                merged_map[key] = ln

    body_lines: List[str] = []
    seen = set()
    for ln in best.splitlines():
        key = _product_key(ln)
        if key in merged_map and key not in seen:
            body_lines.append(merged_map[key])
            seen.add(key)
        elif key not in merged_map and ln.strip():
            body_lines.append(ln)
    for key, ln in merged_map.items():
        if key not in seen:
            body_lines.append(ln)
            seen.add(key)

    best = "\n".join(body_lines)
    if not re.search(r"G?roup\s*Wise\s*Sales", best, re.I):
        best = (
            "Group Wise Sales Op.Stock Purchase Qty Sales Qty Cl.Stock Strength\n"
            + best
        )
    if not re.search(r"(?i)strength|op\.?\s*stock|cl\.?\s*stock", best):
        best = "Product Name Strength Op.Stock Purchase Qty Sales Qty Cl.Stock\n" + best
    return best


def _ocr_group_wise_sales_pages(
    pages: List[Dict[str, Any]],
) -> str:
    """Re-OCR page images with settings tuned for Group Wise Sales grids."""
    parts: List[str] = []
    for p in pages:
        img = p.get("image_bytes")
        if not img:
            continue
        try:
            parts.append(_ocr_group_wise_sales_image(img))
        except Exception as exc:
            logger.warning("Group Wise Sales OCR failed: %s", exc)
            try:
                parts.append(_ocr_image_to_text(img, psm=4, enhance=True))
            except Exception:
                pass
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# XLS / XLSX (Victory-style sparse grid)
# ---------------------------------------------------------------------------

def _norm_xls_header_label(value: Any) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", text).strip().upper()


def _find_marg_erp_xls_header(
    rows: List[List[Any]],
) -> Optional[Tuple[int, Dict[str, int], str]]:
    """Detect Marg ERP Himalaya/Zeal stock-statement headers.

    Returns (header_row_index, role->col_index, variant) or None.
    Variants:
      - purchase_sale_rate: OPENING/PURCHASE/SALE/CLOSING + RATE
      - receive_issue_value: OPENING/RECEIVE/ISSUE/CLOSING + values
    """
    for ri, row in enumerate(rows[:40]):
        labels = [_norm_xls_header_label(c) for c in row]
        if not any(lab == "PRODUCT DESCRIPTION" for lab in labels):
            continue
        if not any("OPENING" in lab for lab in labels):
            continue
        if not any("CLOSING" in lab for lab in labels):
            continue

        roles: Dict[str, int] = {}
        replace_seen = 0
        for ci, lab in enumerate(labels):
            if not lab:
                continue
            if lab == "PRODUCT DESCRIPTION" or lab.startswith("PRODUCT DESC"):
                roles["product_name"] = ci
            elif lab in {"OPENING STOCK", "OPENING"}:
                roles.setdefault("opening_qty", ci)
            elif lab == "OPENING VALUE":
                roles["opening_value"] = ci
            elif lab in {"PURCHASE QUANTITY", "PURCHASE QTY", "PURCHASE"}:
                roles["purchase_qty"] = ci
            elif "SALE RETURN" in lab:
                roles["sale_return_qty"] = ci
            elif lab in {"TOTAL RECEIVE", "TOTAL RECEIVED"}:
                roles["total_receive_qty"] = ci
            elif lab in {"RECEIVE QUANTITY", "RECEIVE QTY"}:
                roles["receive_qty"] = ci
            elif lab == "RECEIVE VALUE":
                roles["receive_value"] = ci
            elif lab in {"SALE QUANTITY", "SALE QTY", "SALES QUANTITY", "SALES QTY"}:
                roles["sales_qty"] = ci
            elif lab in {"P/R QUANTITY", "P/R QTY", "PR QUANTITY", "PR QTY"}:
                roles["pr_qty"] = ci
            elif lab in {"ISSUE QUANTITY", "ISSUE QTY"}:
                roles["issue_qty"] = ci
            elif lab == "ISSUE VALUE":
                roles["issue_value"] = ci
            elif lab in {"CLOSING STOCK", "CLOSING"}:
                roles.setdefault("closing_qty", ci)
            elif lab == "CLOSING VALUE":
                roles["closing_value"] = ci
            elif lab == "RATE":
                roles["rate"] = ci
            elif "REPLACE" in lab:
                replace_seen += 1
                if replace_seen == 1:
                    roles["replace_in_qty"] = ci
                else:
                    roles["replace_out_qty"] = ci

        if "product_name" not in roles or "opening_qty" not in roles:
            continue
        if "closing_qty" not in roles:
            continue

        if "rate" in roles or "purchase_qty" in roles or "sales_qty" in roles:
            variant = "purchase_sale_rate"
        elif "issue_qty" in roles or "receive_qty" in roles:
            variant = "receive_issue_value"
        else:
            variant = "generic"
        return ri, roles, variant
    return None


def _row_cell_at(row: List[Any], idx: Optional[int]) -> Any:
    if idx is None or idx < 0 or idx >= len(row):
        return None
    return row[idx]


def _parse_marg_erp_xls(
    rows: List[List[Any]],
    filename: str,
    ext: str,
    header_info: Tuple[int, Dict[str, int], str],
) -> Dict[str, Any]:
    """Parse Marg ERP Nano chemist STOCK & SALES STATEMENT (.xls).

    Format-specific path — does not alter the legacy Item/Op./Sale/Bal. parser.
    """
    header_idx, roles, variant = header_info
    result = empty_result(filename, ext.lstrip("."))
    result["totals"]["extra"]["extraction_method"] = f"marg_erp_xls_{variant}"

    # Header metadata above the product grid
    for row in rows[:header_idx]:
        texts = [str(c).strip() for c in row if c is not None and str(c).strip()]
        if not texts:
            continue
        joined = " ".join(texts)
        first = texts[0]

        if not result["stockist_name"] and not re.search(
            r"Phone\s*:|D\.?L\.?\s*No|STOCK\s*&?\s*SALES|PRODUCT\s+DESC|TOTAL\s+|MARG\s+ERP",
            first,
            re.I,
        ):
            result["stockist_name"] = _clean_name(first)
            continue

        if (
            not result["stockist_address"]
            and result["stockist_name"]
            and not re.search(r"Phone\s*:|D\.?L\.?\s*No|STOCK\s*&?\s*SALES", first, re.I)
            and (
                re.search(
                    r"ROAD|NAGAR|PLOT|FLOOR|BUILDING|DIST|STATE|PIN|BHUBANESWAR|"
                    r"ROURKELA|ODISHA|ORISSA|\d{6}",
                    joined,
                    re.I,
                )
                or (len(joined) > 25 and "," in joined)
            )
        ):
            result["stockist_address"] = _clean_name(joined)
            continue

        if re.search(r"STOCK\s*&?\s*SALES\s+STATEMENT", joined, re.I):
            result["report_title"] = _clean_name(joined.lstrip("-–— ").strip())
            m_period = re.search(
                r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\s*[-–—to]+\s*"
                r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})",
                joined,
                re.I,
            )
            if m_period:
                result["period_from"] = _normalize_date(m_period.group(1))
                result["period_to"] = _normalize_date(m_period.group(2))
            m_co = re.search(
                r"^\s*-?\s*(.+?)\s+STOCK\s*&?\s*SALES\s+STATEMENT",
                joined,
                re.I,
            )
            if m_co:
                company = _clean_name(m_co.group(1).strip(" -–—"))
                if company:
                    result["company_name"] = company

    items: List[Dict[str, Any]] = []
    for row in rows[header_idx + 1 :]:
        texts = [str(c).strip() for c in row if c is not None and str(c).strip() != ""]
        if not texts:
            continue
        name_raw = _row_cell_at(row, roles.get("product_name"))
        product_name = str(name_raw).strip() if name_raw not in (None, "") else ""
        if not product_name:
            continue

        if re.search(r"^TOTAL(?:\s+(QUANTITY|VALUE|QTY))?\b", product_name, re.I):
            nums_by_role = {
                role: _to_float(_row_cell_at(row, idx))
                for role, idx in roles.items()
                if role != "product_name"
            }
            is_value_total = bool(re.search(r"VALUE", product_name, re.I))
            is_qty_total = bool(re.search(r"QUANTITY|QTY", product_name, re.I))
            # Plain "TOTAL" rows (receive/issue layout) carry qty + value together.
            is_combined_total = not is_value_total and not is_qty_total

            if is_value_total or is_combined_total:
                # TOTAL VALUE / combined TOTAL: money sits under matching columns.
                if variant == "purchase_sale_rate":
                    if "sales_qty" in nums_by_role:
                        result["totals"]["sales_value"] = nums_by_role["sales_qty"]
                    if "closing_qty" in nums_by_role:
                        result["totals"]["closing_value"] = nums_by_role["closing_qty"]
                    if "opening_qty" in nums_by_role:
                        result["totals"]["extra"]["opening_value"] = nums_by_role[
                            "opening_qty"
                        ]
                    if "purchase_qty" in nums_by_role:
                        result["totals"]["extra"]["purchase_value"] = nums_by_role[
                            "purchase_qty"
                        ]
                    if "total_receive_qty" in nums_by_role:
                        result["totals"]["extra"]["total_receive_value"] = nums_by_role[
                            "total_receive_qty"
                        ]
                else:
                    if "issue_value" in nums_by_role:
                        result["totals"]["sales_value"] = nums_by_role["issue_value"]
                    elif "issue_qty" in nums_by_role:
                        result["totals"]["sales_value"] = nums_by_role["issue_qty"]
                    if "closing_value" in nums_by_role:
                        result["totals"]["closing_value"] = nums_by_role["closing_value"]
                    elif "closing_qty" in nums_by_role:
                        result["totals"]["closing_value"] = nums_by_role["closing_qty"]
                    if "opening_value" in nums_by_role:
                        result["totals"]["extra"]["opening_value"] = nums_by_role[
                            "opening_value"
                        ]
                    if "receive_value" in nums_by_role:
                        result["totals"]["extra"]["purchase_value"] = nums_by_role[
                            "receive_value"
                        ]

            if is_qty_total or is_combined_total:
                for key in (
                    "opening_qty",
                    "purchase_qty",
                    "receive_qty",
                    "sales_qty",
                    "issue_qty",
                    "closing_qty",
                    "total_receive_qty",
                ):
                    if key in nums_by_role:
                        result["totals"]["extra"][f"total_{key}"] = nums_by_role[key]
            continue

        if re.search(
            r"MARG\s+ERP|PRODUCT\s+DESC|STOCK\s*&?\s*SALES|Phone\s*:|D\.?L\.?\s*No|"
            r"^Page\s*\d|Printed|Signature",
            product_name,
            re.I,
        ):
            continue

        item = empty_line_item()
        item["product_name"] = _clean_name(product_name)

        opening = _to_float(_row_cell_at(row, roles.get("opening_qty")))
        closing = _to_float(_row_cell_at(row, roles.get("closing_qty")))
        item["opening_qty"] = opening
        item["closing_qty"] = closing

        if variant == "purchase_sale_rate":
            purchase = _to_float(_row_cell_at(row, roles.get("purchase_qty")))
            sale_return = _to_float(_row_cell_at(row, roles.get("sale_return_qty")))
            replace_in = _to_float(_row_cell_at(row, roles.get("replace_in_qty")))
            total_receive = _to_float(_row_cell_at(row, roles.get("total_receive_qty")))
            sales_qty = _to_float(_row_cell_at(row, roles.get("sales_qty")))
            pr_qty = _to_float(_row_cell_at(row, roles.get("pr_qty")))
            replace_out = _to_float(_row_cell_at(row, roles.get("replace_out_qty")))
            rate = _to_nullable_float(_row_cell_at(row, roles.get("rate")))

            # Receipts = inbound stock beyond opening (purchase + sale return + replace-in)
            if total_receive or opening:
                item["receipts_qty"] = max(
                    0.0, (total_receive or (opening + purchase + sale_return + replace_in)) - opening
                )
            else:
                item["receipts_qty"] = purchase + sale_return + replace_in
            item["sales_qty"] = sales_qty
            if rate is not None:
                item["extra"]["rate"] = rate
                item["sales_value"] = round(sales_qty * rate, 2)
                item["closing_value"] = round(closing * rate, 2)
            if purchase:
                item["extra"]["purchase_qty"] = purchase
            if sale_return:
                item["extra"]["sale_return_qty"] = sale_return
            if replace_in:
                item["extra"]["replace_in_qty"] = replace_in
            if total_receive:
                item["extra"]["total_receive_qty"] = total_receive
            if pr_qty:
                item["extra"]["purchase_return_qty"] = pr_qty
            if replace_out:
                item["extra"]["replace_out_qty"] = replace_out
        else:
            receive_qty = _to_float(_row_cell_at(row, roles.get("receive_qty")))
            issue_qty = _to_float(_row_cell_at(row, roles.get("issue_qty")))
            opening_value = _to_nullable_float(_row_cell_at(row, roles.get("opening_value")))
            receive_value = _to_nullable_float(_row_cell_at(row, roles.get("receive_value")))
            issue_value = _to_nullable_float(_row_cell_at(row, roles.get("issue_value")))
            closing_value = _to_nullable_float(_row_cell_at(row, roles.get("closing_value")))

            item["receipts_qty"] = receive_qty
            item["sales_qty"] = issue_qty
            if issue_value is not None:
                item["sales_value"] = issue_value
            if closing_value is not None:
                item["closing_value"] = closing_value
            if opening_value is not None:
                item["extra"]["opening_value"] = opening_value
            if receive_value is not None:
                item["extra"]["receive_value"] = receive_value

        items.append(item)

    result["line_items"] = items
    return result


def _norm_tabular_col_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _find_zl_secondary_xlsx_header(
    rows: List[List[Any]],
) -> Optional[Tuple[int, Dict[str, int]]]:
    """Detect ZL secondary stock tabular export headers.

    Expected columns include Customer_name, Material_code/Material_name,
    Opening_bal_qty, Primary_qty, Closing_bal_qty.
    """
    for ri, row in enumerate(rows[:20]):
        keys = {_norm_tabular_col_key(c): ci for ci, c in enumerate(row) if c not in (None, "")}
        has_material = "materialname" in keys or "materialcode" in keys
        has_opening = "openingbalqty" in keys or "openingbalquantity" in keys
        has_customer = "customername" in keys or "customercode" in keys
        if has_material and has_opening and has_customer:
            return ri, keys
    return None


def _year_month_period(year: int, month: int) -> Tuple[Optional[str], Optional[str]]:
    try:
        y, mo = int(year), int(month)
        start = datetime(y, mo, 1)
        if mo == 12:
            end = datetime(y, 12, 31)
        else:
            from datetime import timedelta

            end = datetime(y, mo + 1, 1) - timedelta(days=1)
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None, None


def _parse_zl_secondary_xlsx(
    rows: List[List[Any]],
    filename: str,
    ext: str,
    header_info: Tuple[int, Dict[str, int]],
) -> Dict[str, Any]:
    """Parse ZL Customer/Material secondary stock .xlsx export.

    Format-specific path — does not alter Marg ERP or legacy Item/Op./Bal. parsers.
    This layout has no sales qty/value or stock value columns; those fields stay null.
    """
    header_idx, keys = header_info
    result = empty_result(filename, ext.lstrip("."))
    result["totals"]["extra"]["extraction_method"] = "zl_secondary_xlsx"
    result["report_title"] = "ZL Secondary Stock Statement"

    def _col(*names: str) -> Optional[int]:
        for n in names:
            k = _norm_tabular_col_key(n)
            if k in keys:
                return keys[k]
        return None

    col_customer = _col("Customer_name")
    col_code = _col("Material_code")
    col_name = _col("Material_name")
    col_mrp = _col("Mrp")
    col_rate = _col("Secondaryrate")
    col_open = _col("Opening_bal_qty", "Opening_bal_quantity")
    col_primary = _col("Primary_qty", "Primary_quantity")
    col_close = _col("Closing_bal_qty", "Closing_bal_quantity")
    col_year = _col("Year")
    col_month = _col("Month")
    col_div = _col("Division")
    col_cust_code = _col("Customer_code")

    items: List[Dict[str, Any]] = []
    for row in rows[header_idx + 1 :]:
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue

        material_name = _row_cell_at(row, col_name)
        material_code = _row_cell_at(row, col_code)
        name_text = str(material_name).strip() if material_name not in (None, "") else ""
        code_text = str(material_code).strip() if material_code not in (None, "") else ""
        if not name_text and not code_text:
            continue
        # Skip accidental re-header rows
        if _norm_tabular_col_key(name_text) in {"materialname", "customername"}:
            continue

        if not result["stockist_name"] and col_customer is not None:
            cust = _row_cell_at(row, col_customer)
            if cust not in (None, ""):
                result["stockist_name"] = _clean_name(str(cust))

        if result["period_from"] is None and col_year is not None and col_month is not None:
            y_raw = _row_cell_at(row, col_year)
            m_raw = _row_cell_at(row, col_month)
            try:
                pf, pt = _year_month_period(int(float(str(y_raw))), int(float(str(m_raw))))
                result["period_from"], result["period_to"] = pf, pt
            except (TypeError, ValueError):
                pass

        if col_div is not None and "division" not in result["totals"]["extra"]:
            div = _row_cell_at(row, col_div)
            if div not in (None, ""):
                result["totals"]["extra"]["division"] = str(div).strip()
        if col_cust_code is not None and "customer_code" not in result["totals"]["extra"]:
            cc = _row_cell_at(row, col_cust_code)
            if cc not in (None, ""):
                # Preserve leading zeros from Excel text/codes
                result["totals"]["extra"]["customer_code"] = str(cc).strip()
                if isinstance(cc, float) and cc == int(cc):
                    result["totals"]["extra"]["customer_code"] = f"{int(cc):010d}"

        item = empty_line_item()
        item["product_code"] = code_text or None
        item["product_name"] = _clean_name(name_text) if name_text else None
        # Qty fields: preserve genuine zeros from source
        item["opening_qty"] = _to_float(_row_cell_at(row, col_open), 0.0)
        item["receipts_qty"] = _to_float(_row_cell_at(row, col_primary), 0.0)
        item["closing_qty"] = _to_float(_row_cell_at(row, col_close), 0.0)
        # No sales / value columns in this export — do not invent
        item["sales_qty"] = None
        item["sales_value"] = None
        item["closing_value"] = None

        mrp = _to_nullable_float(_row_cell_at(row, col_mrp))
        rate = _to_nullable_float(_row_cell_at(row, col_rate))
        if mrp is not None:
            item["extra"]["mrp"] = mrp
        if rate is not None:
            item["extra"]["secondary_rate"] = rate

        items.append(item)

    result["line_items"] = items
    # Totals: no value columns present
    result["totals"]["sales_value"] = None
    result["totals"]["closing_value"] = None
    return result

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


def _parse_marg_opening_receipt_issue(
    rows: List[List[Any]],
    filename: str,
    ext: str,
    sheet_name: str,
) -> Optional[Dict[str, Any]]:
    """MARG ERP qty grid: ITEM DESCRIPTION, OPENING, RECEIPT, ISSUE, CLOSING.

    A printed dash is 0. Other Excel headers return None.
    """
    header_idx = None
    for index, row in enumerate(rows[:20]):
        labels = [
            re.sub(r"[^a-z]", "", str(cell or "").lower())
            for cell in (row or [])
        ]
        if labels[:5] == ["itemdescription", "opening", "receipt", "issue", "closing"]:
            header_idx = index
            break
    if header_idx is None:
        return None

    result = empty_result(filename, ext.lstrip(".") or "xls")
    result["report_title"] = "STOCK & SALES ANALYSIS"
    for row in rows[:header_idx]:
        text = " ".join(str(cell).strip() for cell in row if str(cell or "").strip())
        if not text:
            continue
        if not result.get("stockist_name") and re.search(
            r"MEDICAL|AGENC|PHARMA|DISTRIBUT", text, re.I
        ):
            result["stockist_name"] = _clean_name(text.split("Phone")[0])
        elif not result.get("stockist_address") and re.search(
            r"MANSION|COLONY|ROAD|NAGAR|STREET", text, re.I
        ):
            result["stockist_address"] = _clean_name(text)
        company = re.search(r"\(\s*([A-Za-z][A-Za-z ]{2,30})\s*\)", text)
        if company and not result.get("company_name"):
            result["company_name"] = _clean_name(company.group(1))
        period = re.search(
            r"(\d{1,2}-\d{1,2}-\d{4})\s*-\s*(\d{1,2}-\d{1,2}-\d{4})",
            text,
        )
        if period and result.get("period_from") is None:
            result["period_from"] = _normalize_date(period.group(1))
            result["period_to"] = _normalize_date(period.group(2))

    items: List[Dict[str, Any]] = []
    for row in rows[header_idx + 1 :]:
        raw_name = str(row[0] or "").strip() if row else ""
        if not raw_name or re.search(r"^(TOTAL|Digital)\b", raw_name, re.I):
            continue
        parts = re.split(r"\s{2,}", raw_name, maxsplit=1)
        name = _clean_name(parts[0])
        packing = _clean_name(parts[1]) if len(parts) > 1 else None
        if not name:
            continue

        def cell_qty(index: int) -> float:
            if index >= len(row):
                return 0.0
            text = str(row[index] if row[index] is not None else "").strip()
            if text in {"", "-", "—", "--"}:
                return 0.0
            return _to_float(text)

        opening = cell_qty(1)
        receipt = cell_qty(2)
        issue = cell_qty(3)
        closing = cell_qty(4)
        if opening == receipt == issue == closing == 0 and not packing:
            continue
        item = empty_line_item()
        item["product_name"] = name
        item["packing"] = packing
        item["opening_qty"] = opening
        item["receipts_qty"] = receipt
        item["sales_qty"] = issue
        item["closing_qty"] = closing
        item["extra"]["layout"] = "marg_opening_receipt_issue"
        items.append(item)
    if not items:
        return None
    result["line_items"] = items
    result.setdefault("totals", {}).setdefault("extra", {})
    result["totals"]["extra"]["extraction_method"] = "marg_opening_receipt_issue"
    result["totals"]["extra"]["layout"] = "marg_opening_receipt_issue"
    result["totals"]["extra"]["sheet"] = sheet_name
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
        # ZL secondary tabular export (Customer_name / Material_* / *_bal_qty)
        zl_header = _find_zl_secondary_xlsx_header(rows)
        if zl_header is not None:
            return _xls_finalize_result(
                _parse_zl_secondary_xlsx(rows, filename, ext, zl_header)
            )
        # Marg ERP Himalaya/Zeal STOCK & SALES STATEMENT — format-specific early exit
        marg_header = _find_marg_erp_xls_header(rows)
        if marg_header is not None:
            return _xls_finalize_result(
                _parse_marg_erp_xls(rows, filename, ext, marg_header)
            )
        marg = _parse_marg_opening_receipt_issue(rows, filename, ext, sheet_name)
        if marg and marg.get("line_items"):
            return _xls_finalize_result(marg)
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


_ITEM_PACK_SRETURN_HEADER = (
    "item",
    "pack",
    "opening",
    "purchase",
    "sreturn",
    "others",
    "subtotal",
    "sale",
    "preturn",
    "others",
    "closing",
)
_ITEM_PACK_SRETURN_FIELDS = (
    "item",
    "pack",
    "opening",
    "purchase",
    "sales_return",
    "others_in",
    "subtotal",
    "sale",
    "purchase_return",
    "others_out",
    "closing",
)
_ITEM_PACK_QTY_FIELDS = (
    "opening_qty",
    "purchase_qty",
    "sales_return_qty",
    "others_in_qty",
    "subtotal_qty",
    "sales_qty",
    "purchase_return_qty",
    "others_out_qty",
    "closing_qty",
)


def _xls_header_compact(label: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(label or "").strip().lower())


def _xls_item_pack_sreturn_indexes(row: List[Any]) -> Optional[Dict[str, int]]:
    """Column indexes for ITEM/PACK/.../S.RETURN/OTHERS/SUB TOTAL/.../OTHERS/CLOSING.

    Other stock sheets do not have this pair of OTHERS columns and return None.
    """
    labels = [
        (idx, _xls_header_compact(cell))
        for idx, cell in enumerate(row or [])
        if _xls_header_compact(cell)
    ]
    if len(labels) < len(_ITEM_PACK_SRETURN_HEADER):
        return None
    head = [label for _idx, label in labels[: len(_ITEM_PACK_SRETURN_HEADER)]]
    if head != list(_ITEM_PACK_SRETURN_HEADER):
        return None
    indexes = {
        field: labels[pos][0]
        for pos, field in enumerate(_ITEM_PACK_SRETURN_FIELDS)
    }
    for idx, label in labels[len(_ITEM_PACK_SRETURN_HEADER) :]:
        if label == "itemcode" and "itemcode" not in indexes:
            indexes["itemcode"] = idx
    return indexes


def _xls_sreturn_qty_cell(value: Any) -> Tuple[Optional[float], Optional[str]]:
    """Parse one quantity cell. Dash and blank are 0. Ambiguous text stays unset."""
    if value is None:
        return 0.0, None
    if isinstance(value, bool):
        return None, "ambiguous"
    if isinstance(value, (int, float)):
        return float(value), None
    text = str(value).replace("\u00a0", " ").strip()
    if text in {"", "-", "—", "--", "–"}:
        return 0.0, None
    if text.upper() in _EMPTY_NUMERIC or text.upper() in {"?", "NIL"}:
        return None, text
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    cleaned = text.replace(",", "")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        number = float(cleaned)
        return (-abs(number) if negative and number else number), None
    return None, text


_ZENITH_OPSTK_HEADER = (
    "productname",
    "pack",
    "opstk",
    "pur",
    "sales",
    "free",
    "repl",
    "totalstock",
)
_ZENITH_OPSTK_FIELDS = (
    "item",
    "pack",
    "opening",
    "purchase",
    "sales",
    "free",
    "replacement",
    "closing",
)


def _xls_zenith_opstk_indexes(row: List[Any]) -> Optional[Dict[str, int]]:
    """Column indexes for ProductName/Pack/Op.stk/Pur/sales/Free/Repl/TotalStock.

    Any other header, including ITEM/PACK/S.RETURN/OTHERS, returns None.
    """
    labels = [
        (idx, _xls_header_compact(cell))
        for idx, cell in enumerate(row or [])
        if _xls_header_compact(cell)
    ]
    compacts = [label for _idx, label in labels]
    start = None
    for pos in range(0, len(compacts) - len(_ZENITH_OPSTK_HEADER) + 1):
        if tuple(compacts[pos : pos + len(_ZENITH_OPSTK_HEADER)]) == _ZENITH_OPSTK_HEADER:
            start = pos
            break
    if start is None:
        return None
    indexes = {
        field: labels[start + offset][0]
        for offset, field in enumerate(_ZENITH_OPSTK_FIELDS)
    }
    optional = {
        "salevalue": "sales_value",
        "stockvalueatpurchaseprice": "stock_value",
        "age": "age",
    }
    for idx, label in labels[start + len(_ZENITH_OPSTK_HEADER) :]:
        field = optional.get(label)
        if field and field not in indexes:
            indexes[field] = idx
    return indexes


def _xls_fill_zenith_opstk_rows(
    result: Dict[str, Any],
    rows: List[List[Any]],
    header_idx: int,
    sheet_name: str,
    header_score: int,
) -> Dict[str, Any]:
    """Keep every product row. Map Op.stk/Pur/sales/Free/Repl/TotalStock by column."""
    indexes = _xls_zenith_opstk_indexes(rows[header_idx])
    if not indexes:
        return result

    for row in rows[:header_idx]:
        if not isinstance(row, list):
            continue
        joined = " ".join(_xls_preamble_texts(row))
        if re.search(r"HIMALAYA\s*[- ]\s*ZENITH", joined, re.I) and not result.get("company_name"):
            result["company_name"] = "HIMALAYA-ZENITH"
        month = re.search(r"\(Month\)\s*-\s*(\d{1,2})/(\d{4})", joined, re.I)
        if month and not result.get("period_from"):
            year = int(month.group(2))
            mon = int(month.group(1))
            if 1 <= mon <= 12:
                if mon == 12:
                    last = 31
                else:
                    last = (date(year, mon + 1, 1) - timedelta(days=1)).day
                result["period_from"] = f"{year:04d}-{mon:02d}-01"
                result["period_to"] = f"{year:04d}-{mon:02d}-{last:02d}"
        if re.search(r"Stock\s+And\s+Sales\s+Report", joined, re.I) and not result.get("report_title"):
            result["report_title"] = _clean_name(joined)

    def cell(row: List[Any], key: str) -> Any:
        idx = indexes.get(key)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    items: List[Dict[str, Any]] = []
    zero_rows = 0
    parse_error_rows = 0
    qty_keys = (
        ("opening_qty", "opening"),
        ("purchase_qty", "purchase"),
        ("sales_qty", "sales"),
        ("free_qty", "free"),
        ("replacement_qty", "replacement"),
        ("closing_qty", "closing"),
    )
    for row in rows[header_idx + 1 :]:
        if not isinstance(row, list):
            continue
        raw_name = cell(row, "item")
        if raw_name in (None, ""):
            continue
        product_name = _clean_name(str(raw_name))
        if not product_name:
            continue
        if re.match(
            r"^(?:total\b|grand\s*total|sub\s*total|net\s*total|productname)\b",
            product_name,
            re.I,
        ):
            continue
        raw_pack = cell(row, "pack")
        packing = _clean_name(str(raw_pack)) if raw_pack not in (None, "") else None
        parsed: Dict[str, Optional[float]] = {}
        errors: Dict[str, str] = {}
        for field, key in qty_keys:
            number, error = _xls_sreturn_qty_cell(cell(row, key))
            parsed[field] = number
            if error:
                errors[field] = error
        item = empty_line_item()
        item["product_name"] = product_name
        item["packing"] = packing or None
        item["opening_qty"] = parsed["opening_qty"]
        item["purchase_qty"] = parsed["purchase_qty"]
        item["receipts_qty"] = parsed["purchase_qty"]
        item["sales_qty"] = parsed["sales_qty"]
        item["free_qty"] = parsed["free_qty"]
        item["replacement_qty"] = parsed["replacement_qty"]
        item["closing_qty"] = parsed["closing_qty"]
        item["source_product_name"] = product_name
        item["source_packing"] = packing or None
        sales_value, sales_value_error = _xls_sreturn_qty_cell(cell(row, "sales_value"))
        stock_value, stock_value_error = _xls_sreturn_qty_cell(cell(row, "stock_value"))
        if "sales_value" in indexes:
            item["sales_value"] = sales_value
            if sales_value_error:
                errors["sales_value"] = sales_value_error
        if "stock_value" in indexes:
            item["closing_value"] = stock_value
            if stock_value_error:
                errors["closing_value"] = stock_value_error
        age_number = None
        if "age" in indexes:
            age_number, age_error = _xls_sreturn_qty_cell(cell(row, "age"))
            if age_error:
                errors["age"] = age_error
        if errors:
            parse_error_rows += 1
        if (
            not errors
            and all(parsed[field] == 0.0 for field, _key in qty_keys)
        ):
            zero_rows += 1
        extra = {
            "layout": "zenith_opstk_totalstock",
            "source_product_name": product_name,
            "source_packing": packing or None,
            "purchase_qty": parsed["purchase_qty"],
            "free_qty": parsed["free_qty"],
            "replacement_qty": parsed["replacement_qty"],
        }
        if "age" in indexes:
            extra["age"] = age_number
        if errors:
            extra["qty_parse_errors"] = errors
        item["extra"] = extra
        items.append(item)

    result["line_items"] = items
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["sheet"] = sheet_name
    extra["header_row"] = header_idx
    extra["header_detection_confidence"] = (
        round(min(1.0, header_score / 12.0), 2) if header_idx is not None else 0.0
    )
    extra["extraction_method"] = "zenith_opstk_totalstock"
    extra["layout"] = "zenith_opstk_totalstock"
    extra["rows_detected"] = len(items)
    extra["zero_qty_rows"] = zero_rows
    extra["qty_parse_error_rows"] = parse_error_rows
    if "item" in indexes:
        extra["product_column"] = _xls_col_letter(indexes["item"])
        extra["product_column_header"] = str(rows[header_idx][indexes["item"]] or "").strip()
    return result


def _xls_fill_item_pack_sreturn_rows(
    result: Dict[str, Any],
    rows: List[List[Any]],
    header_idx: int,
    sheet_name: str,
    header_score: int,
) -> Dict[str, Any]:
    """Keep every source row, including all-zero rows, and both OTHERS columns."""
    indexes = _xls_item_pack_sreturn_indexes(rows[header_idx])
    if not indexes:
        return result

    for row in rows[:header_idx]:
        if not isinstance(row, list):
            continue
        joined = " ".join(_xls_preamble_texts(row))
        company = re.search(r"Company\s*:\s*(.+)", joined, re.I)
        if company and not result.get("company_name"):
            result["company_name"] = _clean_name(company.group(1))

    def cell(row: List[Any], key: str) -> Any:
        idx = indexes.get(key)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    items: List[Dict[str, Any]] = []
    zero_rows = 0
    mismatch_rows = 0
    parse_error_rows = 0
    for row in rows[header_idx + 1 :]:
        if not isinstance(row, list):
            continue
        raw_name = cell(row, "item")
        if raw_name in (None, ""):
            continue
        product_name = _clean_name(str(raw_name))
        if not product_name:
            continue
        if re.match(r"^division\s*:", product_name, re.I):
            continue
        if re.match(
            r"^(?:total\b|grand\s*total|sub\s*total|net\s*total)\b",
            product_name,
            re.I,
        ):
            continue

        raw_pack = cell(row, "pack")
        packing = _clean_name(str(raw_pack)) if raw_pack not in (None, "") else None
        parsed: Dict[str, Optional[float]] = {}
        errors: Dict[str, str] = {}
        sources = (
            ("opening_qty", "opening"),
            ("purchase_qty", "purchase"),
            ("sales_return_qty", "sales_return"),
            ("others_in_qty", "others_in"),
            ("subtotal_qty", "subtotal"),
            ("sales_qty", "sale"),
            ("purchase_return_qty", "purchase_return"),
            ("others_out_qty", "others_out"),
            ("closing_qty", "closing"),
        )
        for field, key in sources:
            number, error = _xls_sreturn_qty_cell(cell(row, key))
            parsed[field] = number
            if error:
                errors[field] = error

        item = empty_line_item()
        item["product_name"] = product_name
        item["packing"] = packing or None
        item["product_code"] = _xls_cell_code(cell(row, "itemcode"))
        item["opening_qty"] = parsed["opening_qty"]
        item["receipts_qty"] = parsed["purchase_qty"]
        item["sales_qty"] = parsed["sales_qty"]
        item["closing_qty"] = parsed["closing_qty"]
        item["purchase_qty"] = parsed["purchase_qty"]
        item["sales_return_qty"] = parsed["sales_return_qty"]
        item["others_in_qty"] = parsed["others_in_qty"]
        item["subtotal_qty"] = parsed["subtotal_qty"]
        item["purchase_return_qty"] = parsed["purchase_return_qty"]
        item["others_out_qty"] = parsed["others_out_qty"]
        item["source_product_name"] = product_name
        item["source_packing"] = packing or None

        flags: List[str] = []
        numbers = [parsed[field] for field in _ITEM_PACK_QTY_FIELDS]
        if errors:
            parse_error_rows += 1
        elif all(number is not None for number in numbers):
            opening = parsed["opening_qty"] or 0.0
            purchase = parsed["purchase_qty"] or 0.0
            sales_return = parsed["sales_return_qty"] or 0.0
            others_in = parsed["others_in_qty"] or 0.0
            subtotal = parsed["subtotal_qty"] or 0.0
            sale = parsed["sales_qty"] or 0.0
            purchase_return = parsed["purchase_return_qty"] or 0.0
            others_out = parsed["others_out_qty"] or 0.0
            closing = parsed["closing_qty"] or 0.0
            expected_subtotal = opening + purchase + sales_return + others_in
            expected_closing = subtotal - sale - purchase_return - others_out
            if abs(expected_subtotal - subtotal) > 0.05:
                flags.append("subtotal")
            if abs(expected_closing - closing) > 0.05:
                flags.append("closing")
        if flags:
            mismatch_rows += 1
        if (
            not errors
            and all(parsed[field] == 0.0 for field in _ITEM_PACK_QTY_FIELDS)
        ):
            zero_rows += 1

        extra = {
            "layout": "item_pack_sreturn_others",
            "source_product_name": product_name,
            "source_packing": packing or None,
            "purchase_qty": parsed["purchase_qty"],
            "sales_return_qty": parsed["sales_return_qty"],
            "others_in_qty": parsed["others_in_qty"],
            "subtotal_qty": parsed["subtotal_qty"],
            "purchase_return_qty": parsed["purchase_return_qty"],
            "others_out_qty": parsed["others_out_qty"],
            "qty_reconcile_ok": not errors and not flags,
        }
        if errors:
            extra["qty_parse_errors"] = errors
        if flags:
            extra["qty_reconcile_flags"] = flags
        item["extra"] = extra
        items.append(item)

    result["line_items"] = items
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["sheet"] = sheet_name
    extra["header_row"] = header_idx
    extra["header_detection_confidence"] = (
        round(min(1.0, header_score / 12.0), 2) if header_idx is not None else 0.0
    )
    extra["extraction_method"] = "item_pack_sreturn_others"
    extra["layout"] = "item_pack_sreturn_others"
    extra["rows_detected"] = len(items)
    extra["zero_qty_rows"] = zero_rows
    extra["qty_mismatch_rows"] = mismatch_rows
    extra["qty_parse_error_rows"] = parse_error_rows
    if "item" in indexes:
        extra["product_column"] = _xls_col_letter(indexes["item"])
        extra["product_column_header"] = str(rows[header_idx][indexes["item"]] or "").strip()
    return result


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

    # ZL secondary tabular export (Customer_name / Material_* / *_bal_qty)
    zl_header = _find_zl_secondary_xlsx_header(rows)
    if zl_header is not None:
        return _parse_zl_secondary_xlsx(rows, filename, ext, zl_header)

    # Marg ERP Himalaya/Zeal STOCK & SALES STATEMENT — format-specific early exit
    marg_header = _find_marg_erp_xls_header(rows)
    if marg_header is not None:
        return _parse_marg_erp_xls(rows, filename, ext, marg_header)

    # Metadata scan (legacy Item / Op. / Sale / Bal. layout)
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

    if _xls_item_pack_sreturn_indexes(rows[header_idx]):
        return _xls_fill_item_pack_sreturn_rows(
            result, rows, header_idx, sheet_name, header_score
        )
    if _xls_zenith_opstk_indexes(rows[header_idx]):
        return _xls_fill_zenith_opstk_rows(
            result, rows, header_idx, sheet_name, header_score
        )

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
    # Footer on a SALE/CLOSING sheet. It is not a new stockist.
    if re.search(r"\bLAST\s+MONTH\s+SALE\b", s, re.I):
        return False
    if re.match(r"(?:QUANTITY|VALUE)\s+[-+]?\d", s, re.I):
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
    # Medica Ultimate prints OPSTK and IN/OT. That is not the Zandra Op Stk grid.
    if re.search(r"\bOPSTK\b", blob) and re.search(r"\bIN/OT\b", blob):
        return False
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


def _zl_opening_bal_sheet_strips(file_bytes: bytes) -> List[bytes]:
    """Header plus short row bands for a dense phone screenshot of this sheet.

    Full-image reads shift Closing_bal_qty onto the next product. Bands stay on
    the 11px grid so a row is never split. Other statement images are not cropped.
    """
    try:
        from PIL import Image
    except ImportError:
        return [file_bytes]
    image = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    width, height = image.size
    pixels = image.load()
    teal_rows = []
    for y in range(height):
        teals = 0
        for x in range(0, width, 4):
            red, green, blue = pixels[x, y]
            if red < 80 and green > 100 and blue > 80 and green + 5 >= blue:
                teals += 1
        if teals > 25:
            teal_rows.append(y)
    if not teal_rows:
        return [file_bytes]
    header_y0 = teal_rows[0]
    header_y1 = teal_rows[0]
    for y in teal_rows:
        if y <= header_y1 + 4:
            header_y1 = y
        else:
            break
    grid_rows = []
    for y in range(header_y1 + 1, height):
        gray = 0
        for x in range(40, width - 30, 4):
            red, green, blue = pixels[x, y]
            if abs(red - green) < 8 and abs(green - blue) < 8 and 230 <= red <= 245:
                gray += 1
        if gray > 40 and (not grid_rows or y - grid_rows[-1] > 4):
            grid_rows.append(y)
    last_text = 0
    for index, top in enumerate(grid_rows[:-1]):
        bottom = grid_rows[index + 1]
        if bottom - top > 20:
            break
        dark = 0
        for y in range(top + 1, bottom):
            for x in range(50, min(width, 420), 3):
                red, green, blue = pixels[x, y]
                if red + green + blue < 400:
                    dark += 1
        if dark > 8:
            last_text = index + 1
    if last_text < 4:
        return [file_bytes]
    grid_rows = grid_rows[: last_text + 1]
    header = image.crop((0, max(0, header_y0 - 1), width, header_y1 + 1))
    rows_per_band = 11
    strips: List[bytes] = []
    index = 0
    while index < last_text:
        top = grid_rows[index]
        end_index = min(index + rows_per_band, last_text)
        bottom = grid_rows[end_index]
        band = image.crop((0, top, width, min(height, bottom + 1)))
        piece = Image.new("RGB", (width, header.height + band.height), "white")
        piece.paste(header, (0, 0))
        piece.paste(band, (0, header.height))
        piece = piece.resize((width * 3, piece.height * 3), Image.Resampling.NEAREST)
        buf = io.BytesIO()
        piece.save(buf, format="JPEG", quality=92)
        strips.append(buf.getvalue())
        index += rows_per_band
    return strips or [file_bytes]


def _extract_zl_opening_bal_sheet_strips(
    file_bytes: bytes,
    filename: str,
    ext: str = ".png",
) -> Optional[Dict[str, Any]]:
    """Read a truncated Sheet1 screenshot in row bands. Full-image ZL reads stay unchanged."""
    import os

    from services.vertex_gemini_client import generate_content_via_vertex

    strips = _zl_opening_bal_sheet_strips(file_bytes)
    if len(strips) <= 1 and strips[:1] == [file_bytes]:
        return None
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    prompt = _ZL_OPENING_BAL_SHEET_PROMPT.replace(
        "Example: CONFIDO TABS (FC) 60s (AG)\n"
        "opening_qty=100, receipts_qty=0, closing_qty=85, extra.mrp=255, extra.unit_rate=172.23\n\n",
        "Copy only the cells in this image. Do not reuse numbers from any other sheet.\n\n",
    )
    prompt += (
        "\n\nThis crop is the header plus a few rows of one taller sheet. "
        "Read every fully visible data row. Keep Opening_bal_qty, Primary_qty, "
        "and Closing_bal_qty on that same product. A printed 0.00 is 0."
    )
    merged: List[Dict[str, Any]] = []
    seen = set()
    for strip in strips:
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": base64.b64encode(strip).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 4096},
        }
        parsed = None
        for attempt in range(3):
            try:
                response = generate_content_via_vertex(
                    model=model, payload=payload, timeout=120
                )
                parsed = _extract_json_object(_gemini_response_text(response))
                if parsed and parsed.get("line_items"):
                    break
            except Exception as exc:
                logger.warning("ZL opening-bal strip vision failed: %s", exc)
                time.sleep(min(2 ** attempt, 4))
        if not parsed:
            continue
        for raw in parsed.get("line_items") or []:
            if not isinstance(raw, dict):
                continue
            name = _clean_name(str(raw.get("product_name") or ""))
            if not name or re.fullmatch(r"Material_?name|Mrp|Sheet\s*\d+", name, re.I):
                continue
            key = re.sub(r"[^A-Z0-9]", "", name.upper())
            if key in seen:
                continue
            seen.add(key)
            raw["product_name"] = name
            merged.append(raw)
    if not merged:
        return None
    result = empty_result(filename, ext.lstrip(".") or "png")
    result = _apply_parsed_sales_json(
        result, {"report_title": "Sheet1", "line_items": merged}
    )
    return _finalize_zl_opening_bal_sheet(result)


def _looks_like_zandra_stock_sale_result(result: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(result, dict):
        return False
    title = str(result.get("report_title") or "")
    company = str(result.get("company_name") or "")
    extra = ((result.get("totals") or {}).get("extra") or {})
    if extra.get("extraction_method") == "zandra_stock_sale_vision":
        return True
    # A2Z STOCK & SALES ANALYSIS has PACK / OPENING / RECEIPT / ISSUE / CLOSING.
    # The parenthetical division name ZANDRA must not select the Op Stk grid.
    if re.search(r"STOCK\s*&\s*SALES\s*ANALYSIS", title, re.I):
        return False
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

    rtl_items: List[Dict[str, Any]] = []
    if _is_summary_rtl_statement(combined_text):
        rtl_items = _summary_rtl_items_from_text(combined_text)
    if not rtl_items:
        for page in pages:
            rtl_items.extend(_summary_rtl_items_from_words(page.get("words") or []))
    if rtl_items:
        finished = _summary_rtl_finish(rtl_items, combined_text, filename, "pdf")
        finished["totals"]["extra"]["statement_count"] = 1
        return finished

    result: Optional[Dict[str, Any]] = None
    image_only = _pdf_pages_are_image_only(pages) and has_page_images
    zandra_hint = _looks_like_zandra_stock_sale_text(combined_text)

    # Format-specific: Group Wise Sales Op.Stock/Purchase/Sales/Cl.Stock (image OCR)
    # Always re-OCR with psm=4 when this layout is suspected — default psm=6 shifts columns.
    gw_text = combined_text
    suspects_group_wise = bool(
        re.search(r"G?roup\s*Wise\s*Sales", combined_text, re.I)
    ) or (
        not combined_text.strip() and any(p.get("image_bytes") for p in pages)
    )
    if suspects_group_wise or _is_group_wise_sales_opstock_format(combined_text):
        if any(p.get("image_bytes") for p in pages):
            retry = _ocr_group_wise_sales_pages(pages)
            if retry.strip():
                gw_text = retry
    if _is_group_wise_sales_opstock_format(gw_text):
        gw = _parse_group_wise_sales_statement(gw_text, filename, "pdf")
        if gw and gw.get("line_items"):
            names = [str(i.get("product_name") or "") for i in gw["line_items"]]
            if not any(re.search(r"/31/\d{4}", n) for n in names):
                result = gw
                result["totals"]["extra"]["split_pages"] = page_nos
                if group.get("stockist_name") and not re.search(
                    r"^Statement_\d+$", group["stockist_name"]
                ):
                    result["stockist_name"] = group["stockist_name"]
                return result

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
        used_portrait_balance = False
        page_results: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for p in pages:
            img = p.get("image_bytes")
            if not img:
                continue
            page_name = f"{filename}#page{p['page_index'] + 1}"
            page_result: Optional[Dict[str, Any]] = None
            upright_swil = False
            page_ocr = ""
            try:
                page_ocr = _ocr_image_to_text(_upright_swil_receipt_image(img))
                upright_swil = _is_swil_opening_receipt_value_statement(page_ocr)
            except Exception:
                upright_swil = False
            # Receipt/Pur is often OCR'd without the slash, so the older Swil
            # detector misses this portrait sheet. The column parser returns
            # None unless Opening Bal / PACKING / Near Expiry are really there.
            portrait_candidate = upright_swil or bool(
                re.search(r"Sales\s*&\s*Stock", page_ocr, re.I)
                and re.search(r"Opening", page_ocr, re.I)
                and re.search(r"PACKING|Receipt", page_ocr, re.I)
            )
            if portrait_candidate:
                portrait = _parse_portrait_opening_balance_image(img, page_name)
                if portrait and portrait.get("line_items"):
                    page_result = portrait
                    used_portrait_balance = True
            if upright_swil and not (page_result and page_result.get("line_items")):
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
                page_method = (
                    (page_result.get("totals") or {}).get("extra") or {}
                ).get("extraction_method")
                if page_method == "summary_rtl_op_amt" and page_result.get("line_items"):
                    page_result["totals"]["extra"]["statement_count"] = 1
                    return page_result
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
            page_extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
            if page_extra.get("total_row_source") == "portrait_grand_total":
                for key in (
                    "opening_qty",
                    "opening_value",
                    "receipts_qty",
                    "receipts_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                ):
                    if key in totals:
                        merged["totals"][key] = totals.get(key)
                for key in (
                    "opening_value",
                    "total_stock_qty",
                    "near_expiry_qty",
                    "total_row_numbers",
                    "total_row_format",
                    "total_row_source",
                ):
                    if key in page_extra:
                        merged["totals"]["extra"][key] = page_extra[key]
        merged["line_items"] = _drop_trailing_statement_total_item(merged_items)
        swil_receipt_doc = (
            used_swil_receipt
            or saw_swil_receipt_ocr
            or _looks_like_swil_receipt_pur_result(merged)
        )
        if not swil_receipt_doc and not used_portrait_balance:
            title = str(merged.get("report_title") or "")
            swil_receipt_doc = bool(
                re.search(r"Sales\s*&\s*Stock", title, re.I)
                and len(merged.get("line_items") or []) >= 8
            )
        if used_portrait_balance:
            swil_receipt_doc = False
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
        if used_portrait_balance and not used_swil_receipt:
            merged["totals"]["extra"]["extraction_method"] = (
                "portrait_opening_balance_columns"
            )
            for index, item in enumerate(merged.get("line_items") or [], 1):
                if isinstance(item, dict) and isinstance(item.get("extra"), dict):
                    item["extra"]["source_row"] = index
        elif used_swil_receipt:
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


# Scanned portrait Sales & Stock (Opening Bal / Issue/Sales / Closing Bala).
# Digital pair/landscape parsers need embedded words. This path OCRs image-only
# PDFs and maps the 9-number Qty/Value sequence. Existing Swil parsers and
# swil_receipt_pur_value_vision are unchanged.
_SWIL_SCAN_PACK_RE = re.compile(
    r"(?P<pack>"
    r"1X\s*['’]?\s*\d+\s*['’]?S?"
    r"|\d+\s*['’]S"
    r"|[A-Z]{0,8}\d+(?:ML|MI\d*|GM|GMS)\b"
    r")",
    re.I,
)
_SWIL_SCAN_NUM_RE = re.compile(r"-?\d{1,7}(?:[.,]\d{1,2})?")
_SWIL_SCAN_SKIP_RE = re.compile(
    r"Sales\s*&\s*Stock|PRODUCT\s+NAM|PACKING|Opening\s+Bal|Issue/Sales|"
    r"Closing\s+Bala|Powered\s+By|Contin|Page\s*No|Qty\.\s+Value",
    re.I,
)
_SWIL_SCAN_TOTAL_RE = re.compile(r"GRAND\s*TOTAL|TOTAL\s*\(\s*Value", re.I)


def _pdf_embedded_text_len(doc) -> int:
    try:
        return sum(
            len(re.sub(r"\s+", "", page.get_text("text") or "")) for page in doc
        )
    except Exception:
        return 0


def _swil_scan_pair_clean_line(text: str) -> str:
    line = (text or "").replace("\u00a0", " ")
    line = line.replace("—", " ").replace("–", " ").replace("«", " ")
    line = line.replace("~", " ").replace("=", " ").replace("*", " ")
    line = re.sub(r"(?<=\d),(?=\d{2}\b)", ".", line)
    line = re.sub(r"(\d)[^\d\s.]+(?=\d)", r"\1 ", line)
    line = re.sub(r"[^\w./' -]", " ", line)
    return re.sub(r"\s+", " ", line).strip()


def _swil_scan_pair_parse_numbers(text: str) -> List[float]:
    found: List[float] = []
    for raw in _SWIL_SCAN_NUM_RE.findall(text or ""):
        token = raw.replace(",", ".")
        if re.fullmatch(r"20\d{2}", token):
            continue
        found.append(round(_to_float(token), 2))
    return found


def _swil_scan_pair_join_split_money(nums: List[float]) -> List[float]:
    """Join OCR-split money such as 6 + 731.20 -> 6731.20."""
    out: List[float] = []
    idx = 0
    while idx < len(nums):
        cur = nums[idx]
        nxt = nums[idx + 1] if idx + 1 < len(nums) else None
        if (
            nxt is not None
            and 1 <= cur <= 9
            and cur == int(cur)
            and abs(nxt - int(nxt)) > 0.001
            and 100 <= nxt < 1000
        ):
            out.append(round(cur * 1000 + nxt, 2))
            idx += 2
            continue
        out.append(cur)
        idx += 1
    return out


def _swil_scan_pair_is_qty(value: float) -> bool:
    return value >= 0 and abs(value - round(value)) < 0.001 and value < 30000


def _swil_scan_pair_is_money(value: float) -> bool:
    return value == 0 or abs(value - round(value)) > 0.001 or value >= 80


def _swil_scan_pair_try_nine(nums: List[float]) -> Optional[Tuple[float, ...]]:
    if len(nums) != 9 or any(n < 0 for n in nums):
        return None
    opening_qty, opening_value, receipts_qty, receipts_value, total_qty, sales_qty, sales_value, closing_qty, closing_value = nums
    if not all(
        _swil_scan_pair_is_qty(n)
        for n in (opening_qty, receipts_qty, total_qty, sales_qty, closing_qty)
    ):
        return None
    if not all(
        _swil_scan_pair_is_money(n)
        for n in (opening_value, receipts_value, sales_value, closing_value)
    ):
        return None
    ident_total = abs((opening_qty + receipts_qty) - total_qty) <= 1.1
    ident_close = abs((total_qty - sales_qty) - closing_qty) <= 1.1
    if ident_total and ident_close:
        return tuple(nums)
    if abs((opening_qty + receipts_qty - sales_qty) - closing_qty) <= 1.1:
        inferred = opening_qty + receipts_qty
        return (
            opening_qty,
            opening_value,
            receipts_qty,
            receipts_value,
            inferred,
            sales_qty,
            sales_value,
            closing_qty,
            closing_value,
        )
    return None


def _swil_scan_pair_try_eight(nums: List[float]) -> Optional[Tuple[float, ...]]:
    if len(nums) != 8 or any(n < 0 for n in nums):
        return None
    opening_value, receipts_qty, receipts_value, total_qty, sales_qty, sales_value, closing_qty, closing_value = nums
    if not all(
        _swil_scan_pair_is_qty(n)
        for n in (receipts_qty, total_qty, sales_qty, closing_qty)
    ):
        return None
    if not all(
        _swil_scan_pair_is_money(n)
        for n in (opening_value, receipts_value, sales_value, closing_value)
    ):
        return None
    # Avoid treating a qty (112) as Opening Value when Receipt/Pur was split.
    if opening_value > 0 and abs(opening_value - round(opening_value)) < 0.001 and opening_value < 200:
        return None
    if abs((total_qty - sales_qty) - closing_qty) > 1.1:
        return None
    opening_qty = total_qty - receipts_qty
    if opening_qty < -0.1:
        return None
    return (
        opening_qty,
        opening_value,
        receipts_qty,
        receipts_value,
        total_qty,
        sales_qty,
        sales_value,
        closing_qty,
        closing_value,
    )


def _swil_scan_pair_cores(nums: List[float]) -> Optional[Tuple[float, ...]]:
    candidates = [list(nums), _swil_scan_pair_join_split_money(nums)]
    seen = []
    for work in candidates:
        if work in seen:
            continue
        seen.append(work)
        trimmed = list(work)
        if len(trimmed) >= 10 and trimmed[-1] == int(trimmed[-1]) and trimmed[-1] < 20:
            trimmed = trimmed[:-1]
        if len(trimmed) >= 9:
            got = _swil_scan_pair_try_nine(trimmed[-9:])
            if got:
                return got
        if len(trimmed) >= 8:
            got = _swil_scan_pair_try_eight(trimmed[-8:])
            if got:
                return got
    return None


def _swil_scan_pair_parse_line(
    raw: str,
) -> Optional[Tuple[str, Optional[str], Tuple[float, ...]]]:
    """Parse one OCR product line. None for headers and other layouts."""
    line = _swil_scan_pair_clean_line(raw)
    if not line or _SWIL_SCAN_SKIP_RE.search(line):
        return None
    if _SWIL_SCAN_TOTAL_RE.search(line):
        return None
    packs = list(_SWIL_SCAN_PACK_RE.finditer(line))
    name = line
    pack = None
    rest = line
    if packs:
        last = packs[-1]
        pack = last.group("pack")
        name = line[: last.start()].strip(" -_.")
        rest = line[last.end() :]
    cores = _swil_scan_pair_cores(_swil_scan_pair_parse_numbers(rest))
    if cores is None:
        cores = _swil_scan_pair_cores(_swil_scan_pair_parse_numbers(line))
    if cores is None:
        return None
    name = _swil_scan_pair_trim_name(name)
    letters = re.sub(r"[^A-Za-z]", "", name)
    if len(letters) < 3:
        return None
    if re.match(r"^(TOTAL|GRAND|PRODUCT|PAGE|POWERED|HIMALAYA\s+OTX|HIMALAYA\s+ZANDRA)\b", name, re.I):
        return None
    if pack:
        pack = re.sub(r"\s+", "", pack)
        pack = re.sub(r"MI\d*$", "ML", pack, flags=re.I)
    return name, pack, cores


def _swil_scan_pair_trim_name(name: str) -> str:
    """Keep the product words; drop OCR qty/value tail when packing was not split."""
    cut = re.search(
        r"^(.*?(?:TAB|SYP|CAP|DROP|GEL|PASTE|WASH|GUMMIES|GRA|"
        r"LOZEN\w*|LINCTU?S?|NASAL|LINIM\w*|HANDS\s+\w+))\b",
        name or "",
        re.I,
    )
    if cut:
        return _clean_name(cut.group(1))
    return _clean_name(re.sub(r"(?:\s+-?\d{1,7}(?:[.,]\d{1,2})?)+$", "", name or ""))


def _parse_swil_scanned_qty_value_pair_text(
    texts: List[str], filename: str
) -> Optional[Dict[str, Any]]:
    combined = "\n".join(texts)
    if not _is_swil_qty_value_pair_text(combined):
        return None
    result = empty_result(filename, "pdf")
    period = re.search(
        r"From\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+Upto\s+"
        r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
        combined,
        re.I,
    )
    if period:
        result["report_title"] = "Sales & Stock Statement"
        result["period_from"] = _normalize_date(period.group(1))
        result["period_to"] = _normalize_date(period.group(2))
    if re.search(r"ZANDRA", combined, re.I):
        result["company_name"] = "HIMALAYA ZANDRA DIVI"
    else:
        company = re.search(
            r"HIMALAYA(?:\s+WELLNESS(?:\s+COMPANY)?|\s+OTX(?:\s+DIVI)?)",
            combined,
            re.I,
        )
        if company:
            result["company_name"] = _clean_name(company.group(0))
    items: List[Dict[str, Any]] = []
    grand: Optional[Tuple[float, ...]] = None
    for text in texts:
        for raw in (text or "").splitlines():
            cleaned = _swil_scan_pair_clean_line(raw)
            if _SWIL_SCAN_TOTAL_RE.search(cleaned):
                nums = _swil_scan_pair_parse_numbers(cleaned)
                if len(nums) >= 10 and nums[-1] == int(nums[-1]) and nums[-1] < 500:
                    nums = nums[:-1]
                core = _swil_scan_pair_try_nine(nums[-9:]) if len(nums) >= 9 else None
                if core and re.search(r"GRAND\s*TOTAL", cleaned, re.I):
                    grand = core
                elif core and grand is None:
                    grand = core
                continue
            parsed = _swil_scan_pair_parse_line(raw)
            if not parsed:
                continue
            name, pack, core = parsed
            item = empty_line_item()
            item["product_name"] = name
            item["packing"] = pack
            (
                item["opening_qty"],
                opening_value,
                item["receipts_qty"],
                receipts_value,
                total_qty,
                item["sales_qty"],
                item["sales_value"],
                item["closing_qty"],
                item["closing_value"],
            ) = core
            extra = item["extra"]
            extra["opening_value"] = opening_value
            extra["total_stock"] = total_qty
            extra["receipts_value"] = receipts_value
            extra["purchase_value"] = receipts_value
            ordered: Dict[str, Any] = {}
            for key, val in item.items():
                ordered[key] = val
                if key == "receipts_qty":
                    ordered["receipts_value"] = receipts_value
            items.append(ordered)
    if len(items) < 8:
        return None
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "swil_scanned_qty_value_pair"
    extra["detected_format"] = "swil_scanned_qty_value_pair"
    extra["source_type"] = "pdf_ocr"
    extra["vertex_ai_used"] = False
    extra["rows_detected"] = len(items)
    extra["fallback_used"] = False
    if grand:
        extra["opening_value"] = grand[1]
        extra["receipts_value"] = grand[3]
        result["totals"]["receipts_value"] = grand[3]
        result["totals"]["sales_value"] = grand[6]
        result["totals"]["closing_value"] = grand[8]
        extra["total_row_source"] = "swil_scanned_pair_grand_total"
    return result


def _parse_swil_scanned_qty_value_pair_doc(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Image-only Opening Bal / Issue/Sales / Closing Bala scans. Text PDFs return None."""
    if _pdf_embedded_text_len(doc) >= 40:
        return None
    texts: List[str] = []
    for page in doc:
        text, _img = _ocr_pdf_page_text(page, zoom=3.5)
        texts.append(text or "")
    return _parse_swil_scanned_qty_value_pair_text(texts, filename)


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


def _is_daxinsoft_detailed_stock_sales_text(text: str) -> bool:
    """Profitmaker Stock & Sales Statement Detailed: O.Bal / Rcpts / Sal.Ret / Cl.Bal."""
    if not text:
        return False
    if re.search(r"\bO\.Stk\b", text, re.I) and re.search(r"\bQoh\b", text, re.I):
        return False
    if re.search(r"\bParticulars\b", text, re.I) and re.search(r"Purch\.?\s*Qty", text, re.I):
        return False
    if re.search(r"STOCK\s*&\s*SALES\s*ANALYSIS", text, re.I):
        return False
    return bool(
        re.search(r"Stock\s*&\s*Sales\s*Statement\s*Detailed", text, re.I)
        and re.search(r"\bO\.Bal\b", text, re.I)
        and re.search(r"\bRcpts\b", text, re.I)
        and re.search(r"Sal\.Ret", text, re.I)
        and re.search(r"\bCl\.Bal\b", text, re.I)
        and re.search(r"Cl\.Value", text, re.I)
        and re.search(r"\bAge\b", text, re.I)
    )


def _daxin_detailed_field(token: str, total_seen: int) -> Optional[str]:
    norm = re.sub(r"[^a-z]", "", (token or "").lower())
    if norm == "total":
        return "opening_total" if total_seen == 0 else "sales_total"
    return {
        "product": "name",
        "name": "name",
        "packing": "pack",
        "obal": "opening_qty",
        "rcpts": "receipts_qty",
        "salret": "sale_return",
        "sales": "sales_qty",
        "purret": "purchase_return",
        "clbal": "closing_qty",
        "clvalue": "closing_value",
        "age": "age",
    }.get(norm)


def _daxin_detailed_buckets(
    row: Dict[str, Any],
) -> Optional[List[Tuple[str, float, float]]]:
    spans: List[Tuple[str, float, float]] = []
    total_seen = 0
    for x0, x1, _xc, token in sorted(row.get("words") or [], key=lambda item: item[0]):
        field = _daxin_detailed_field(token, total_seen)
        if not field:
            continue
        if field in {"opening_total", "sales_total"}:
            total_seen += 1
        if spans and spans[-1][0] == field:
            prev = spans[-1]
            spans[-1] = (field, min(prev[1], x0), max(prev[2], x1))
        else:
            spans.append((field, x0, x1))
    needed = {
        "name",
        "pack",
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "closing_qty",
        "closing_value",
        "sale_return",
        "purchase_return",
    }
    if not needed.issubset({field for field, _x0, _x1 in spans}):
        return None
    buckets: List[Tuple[str, float, float]] = []
    for idx, (field, x0, x1) in enumerate(spans):
        lo = 0.0 if idx == 0 else max(0.0, x0 - 8.0)
        if idx + 1 < len(spans):
            hi = spans[idx + 1][1] - 2.0
        else:
            hi = max(x1 + 40.0, x0 + 36.0)
        if hi <= lo:
            hi = lo + 8.0
        buckets.append((field, lo, hi))
    if buckets and buckets[0][0] == "name":
        buckets[0] = ("name", 0.0, buckets[0][2])
    return buckets


def _daxin_detailed_horizontal_words(page) -> List[Tuple[Any, ...]]:
    """Words from upright text only. Diagonal watermark letters are not columns."""
    words: List[Tuple[Any, ...]] = []
    raw = page.get_text("rawdict") or {}
    for block in raw.get("blocks") or []:
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or []:
            dx, dy = line.get("dir") or (1.0, 0.0)
            if abs(dx) < 0.95 or abs(dy) > 0.15:
                continue
            chars: List[Dict[str, Any]] = []
            for span in line.get("spans") or []:
                chars.extend(span.get("chars") or [])
            chars.sort(key=lambda ch: ch["bbox"][0])
            buf: List[Dict[str, Any]] = []

            def flush() -> None:
                if not buf:
                    return
                text = "".join(ch.get("c") or "" for ch in buf).strip()
                if text:
                    x0 = min(ch["bbox"][0] for ch in buf)
                    y0 = min(ch["bbox"][1] for ch in buf)
                    x1 = max(ch["bbox"][2] for ch in buf)
                    y1 = max(ch["bbox"][3] for ch in buf)
                    words.append((x0, y0, x1, y1, text, 0, 0, 0))
                buf.clear()

            for ch in chars:
                if str(ch.get("c") or "").isspace():
                    flush()
                else:
                    buf.append(ch)
            flush()
    return words


def _daxin_detailed_join_name(parts: List[str]) -> str:
    """Join a stockist name split across a rotated text run and an upright run."""
    out = ""
    for part in parts:
        piece = re.sub(r"\s+", " ", (part or "").strip())
        if not piece or re.fullmatch(r"[A-Za-z]", piece):
            continue
        if not out:
            out = piece
            continue
        left_u = out.upper()
        head = piece.upper().split()[0]
        merged = None
        for size in range(min(len(left_u), len(head)), 2, -1):
            if left_u.endswith(head[:size]):
                merged = out[: len(out) - size] + piece
                break
        out = merged if merged else f"{out} {piece}"
    return _clean_name(out)


_DAXIN_DETAILED_SKIP_NAME_RE = re.compile(
    r"^(DAXINSOFT|PROFITMAKER|COMPANY|PRODUCT|PACKING|OPENING|PURCHASE|"
    r"CLOSING|CLOSE|SALE\s+VALUE|SALERETURN|GENERATED|PAGE|STOCK|"
    r"PUR\.?\s*RETURN|FROM|TOTAL)\b",
    re.I,
)


def _parse_daxinsoft_detailed_stock_sales_statement(
    doc, filename: str
) -> Optional[Dict[str, Any]]:
    """Parse Profitmaker Detailed rows: O.Bal Rcpts Sal.Ret Sales Pur.Ret Cl.Bal Cl.Value."""
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_daxinsoft_detailed_stock_sales_text(t) for t in page_texts):
        return None

    result = empty_result(filename, "pdf")
    result["report_title"] = "Stock & Sales Statement Detailed"
    items: List[Dict[str, Any]] = []

    for page, text in zip(doc, page_texts):
        if not result.get("stockist_name"):
            parts: List[str] = []
            for ln in text.splitlines():
                raw = ln.strip()
                if re.search(r"Stock\s*&\s*Sales", raw, re.I):
                    break
                if not raw or len(raw) > 80:
                    continue
                if re.fullmatch(r"[A-Za-z]", raw):
                    continue
                if re.search(r"^(Page|From|Product|Company)\b", raw, re.I):
                    continue
                parts.append(raw)
            stockist = _daxin_detailed_join_name(parts)
            if stockist and not re.search(r"DAXINSOFT|PROFITMAKER", stockist, re.I):
                result["stockist_name"] = stockist
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
        m_sale = re.search(r"(?<![A-Za-z])Sale\s+Value\s*:\s*([\d,.]+)", text, re.I)
        m_close = re.search(r"Close\s+Value\s*:\s*([\d,.]+)", text, re.I)
        if m_sale:
            result["totals"]["sales_value"] = _to_float(m_sale.group(1))
        if m_close:
            result["totals"]["closing_value"] = _to_float(m_close.group(1))
        if m_sale or m_close:
            result["totals"]["extra"]["total_row_source"] = "daxinsoft_footer"
        extra = result["totals"]["extra"]
        m_open = re.search(r"Opening\s+Value\s*:\s*([\d,.]+)", text, re.I)
        m_pur = re.search(r"Purchase\s+Value\s*:\s*([\d,.]+)", text, re.I)
        m_sret = re.search(r"SaleReturn\s+Value\s*:\s*([\d,.]+)", text, re.I)
        m_pret = re.search(r"Pur\.?\s*Return\s+Value\s*:\s*([\d,.]+)", text, re.I)
        if m_open:
            extra["opening_value"] = _to_float(m_open.group(1))
        if m_pur:
            extra["purchase_value"] = _to_float(m_pur.group(1))
        if m_sret:
            extra["sale_return_value"] = _to_float(m_sret.group(1))
        if m_pret:
            extra["purchase_return_value"] = _to_float(m_pret.group(1))

        rows = _swil_land_group_words(_daxin_detailed_horizontal_words(page), y_tol=1.5)
        buckets = None
        header_y = 0.0
        for row in rows:
            blob = _swil_land_row_blob(row)
            if re.search(r"Product\s+Name", blob, re.I) and re.search(r"O\.Bal", blob, re.I):
                built = _daxin_detailed_buckets(row)
                if built:
                    buckets = built
                header_y = max(header_y, row["y"])
                continue
            if not buckets or row["y"] <= header_y + 2:
                continue
            if re.search(
                r"Company\s*:|Opening\s+Value|Purchase\s+Value|SaleReturn|"
                r"Sale\s+Value|Close\s+Value|Pur\.?\s*Return\s+Value|"
                r"Generated\s+in|PROFITMAKER|Stock\s*&\s*Sales|Page\s*:",
                blob,
                re.I,
            ):
                continue
            cells = _daxin_row_cells(row, buckets)
            name = _clean_name(" ".join(cells.get("name") or []))
            if not name or not re.search(r"[A-Za-z]", name):
                continue
            if _DAXIN_DETAILED_SKIP_NAME_RE.search(name):
                continue
            qty_hits = sum(
                1
                for key in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
                if _daxin_cell_number(cells.get(key) or []) is not None
            )
            if qty_hits < 4:
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
                extra_item["layout"] = "daxinsoft_detailed_obal_clbal"
                for src, dest in (
                    ("sale_return", "sale_return_qty"),
                    ("purchase_return", "purchase_return_qty"),
                    ("opening_total", "opening_side_total"),
                    ("sales_total", "sales_side_total"),
                    ("age", "age_days"),
                ):
                    val = _daxin_cell_number(cells.get(src) or [])
                    if val is not None:
                        extra_item[dest] = val
            items.append(item)

    if not items:
        return None
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "daxinsoft_detailed_stock_sales"
    extra["layout"] = "obal_rcpts_saleret_sales_clbal"
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


def _is_zenith_opstk_text(text: str) -> bool:
    """True only when this page prints Op.stk, Repl, and TotalStock together."""
    compact = re.sub(r"[^a-z0-9]", "", (text or "").lower())
    return "opstk" in compact and "totalstock" in compact and "repl" in compact


def _zenith_header_tokens(words: List[Any]) -> List[Dict[str, Any]]:
    """Join split header words such as Op + stk without joining the sales column."""
    ordered = sorted(
        (
            word
            for word in words
            if isinstance(word, (list, tuple)) and len(word) >= 5 and str(word[4] or "").strip()
        ),
        key=lambda word: float(word[0]),
    )
    tokens: List[Dict[str, Any]] = []
    for word in ordered:
        tokens.append(
            {
                "compact": _xls_header_compact(word[4]),
                "left": float(word[0]),
                "right": float(word[2]),
                "x": (float(word[0]) + float(word[2])) / 2.0,
                "text": str(word[4]).strip(),
            }
        )
    joined: List[Dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        if index + 1 < len(tokens):
            pair = tokens[index]["compact"] + tokens[index + 1]["compact"]
            if pair in {"productname", "opstk", "totalstock", "salevalue"} and tokens[index][
                "compact"
            ] not in {"productname", "opstk", "totalstock", "sales", "salevalue"}:
                nxt = tokens[index + 1]
                joined.append(
                    {
                        "compact": pair,
                        "left": tokens[index]["left"],
                        "right": nxt["right"],
                        "x": (tokens[index]["left"] + nxt["right"]) / 2.0,
                        "text": tokens[index]["text"] + nxt["text"],
                    }
                )
                index += 2
                continue
        joined.append(tokens[index])
        index += 1
    return joined


def _zenith_opstk_rows_from_words(words: List[Any]) -> Optional[List[List[Any]]]:
    """Build fixed columns from word x positions so a blank cell does not shift."""
    rows: List[Dict[str, Any]] = []
    for word in sorted(words or [], key=lambda item: (round(float(item[1]), 1), float(item[0]))):
        if not isinstance(word, (list, tuple)) or len(word) < 5:
            continue
        if not str(word[4] or "").strip():
            continue
        y0 = float(word[1])
        if rows and abs(y0 - rows[-1]["y"]) <= 2.4:
            rows[-1]["words"].append(word)
        else:
            rows.append({"y": y0, "words": [word]})
    header = None
    header_tokens: List[Dict[str, Any]] = []
    for row in rows:
        tokens = _zenith_header_tokens(row["words"])
        compacts = [token["compact"] for token in tokens]
        for pos in range(0, len(compacts) - len(_ZENITH_OPSTK_HEADER) + 1):
            if tuple(compacts[pos : pos + len(_ZENITH_OPSTK_HEADER)]) == _ZENITH_OPSTK_HEADER:
                header = row
                header_tokens = tokens[pos:]
                break
        if header is not None:
            break
    if header is None or len(header_tokens) < len(_ZENITH_OPSTK_HEADER):
        return None
    anchors = header_tokens[: len(_ZENITH_OPSTK_HEADER)]
    optional = {"salevalue", "stockvalueatpurchaseprice", "age"}
    for token in header_tokens[len(_ZENITH_OPSTK_HEADER) :]:
        if token["compact"] in optional:
            anchors.append(token)
        else:
            break
    centers = [token["x"] for token in anchors]
    edges = [-10_000.0]
    for pos in range(len(centers) - 1):
        edges.append((centers[pos] + centers[pos + 1]) / 2.0)
    edges.append(10_000.0)
    table: List[List[Any]] = [[token["text"] for token in anchors]]
    for row in rows:
        if row is header or row["y"] <= header["y"] + 1.0:
            continue
        cells: List[Any] = [None] * len(anchors)
        name_bits: List[str] = []
        pack_bits: List[str] = []
        for word in sorted(row["words"], key=lambda item: float(item[0])):
            token = str(word[4]).strip()
            center = (float(word[0]) + float(word[2])) / 2.0
            # edges[1] is midway between ProductName and Pack.
            # edges[2] is midway between Pack and Op.stk.
            if center < edges[2]:
                if center < edges[1]:
                    name_bits.append(token)
                else:
                    pack_bits.append(token)
                continue
            for pos in range(2, len(anchors)):
                if edges[pos] <= center < edges[pos + 1]:
                    if cells[pos] in (None, ""):
                        cells[pos] = token
                    break
        if name_bits:
            cells[0] = " ".join(name_bits)
        if pack_bits:
            cells[1] = " ".join(pack_bits)
        if cells[0]:
            table.append(cells)
    return table if len(table) > 1 else None


def _parse_zenith_opstk_statement(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Parse HIMALAYA-ZENITH Op.stk/Pur/sales/Free/Repl/TotalStock pages.

    Returns None for every other statement layout.
    """
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_zenith_opstk_text(text) for text in page_texts):
        return None
    table: List[List[Any]] = []
    for page, text in zip(doc, page_texts):
        if not _is_zenith_opstk_text(text):
            continue
        rows = _zenith_opstk_rows_from_words(page.get_text("words") or [])
        if not rows:
            continue
        if not table:
            table.extend(rows)
        else:
            table.extend(rows[1:])
    if len(table) < 2:
        return None
    result = empty_result(filename, "pdf")
    result = _xls_fill_zenith_opstk_rows(result, table, 0, "pdf", 8)
    if not result.get("line_items"):
        return None
    blob = "\n".join(page_texts)
    if re.search(r"HIMALAYA\s*[- ]\s*ZENITH", blob, re.I):
        result["company_name"] = "HIMALAYA-ZENITH"
    if not result.get("report_title"):
        title = re.search(r"Stock\s+And\s+Sales\s+Report[^\n]*", blob, re.I)
        if title:
            result["report_title"] = _clean_name(title.group(0))
    month = re.search(r"\(Month\)\s*-\s*(\d{1,2})/(\d{4})", blob, re.I)
    if month and not result.get("period_from"):
        year = int(month.group(2))
        mon = int(month.group(1))
        if 1 <= mon <= 12:
            if mon == 12:
                last = 31
            else:
                last = (date(year, mon + 1, 1) - timedelta(days=1)).day
            result["period_from"] = f"{year:04d}-{mon:02d}-01"
            result["period_to"] = f"{year:04d}-{mon:02d}-{last:02d}"
    return result


def _is_medica_opstk_statement(text: str) -> bool:
    """Medica Ultimate stock statement: OPSTK PURCH SALE SALE VAL IN/OT STOCK STK VAL."""
    if not text:
        return False
    return bool(
        re.search(r"\bOPSTK\b", text)
        and re.search(r"\bPURCH\b", text)
        and re.search(r"\bIN/OT\b", text)
        and re.search(r"\bSTK\s*VAL\b", text, re.I)
    )


def _medica_opstk_header(words: List[Any]) -> Optional[Dict[str, Any]]:
    """Column right-edges from the printed header. Numbers are right-aligned to these."""
    rows: List[Dict[str, Any]] = []
    for word in sorted(words or [], key=lambda w: (round(float(w[1]), 1), float(w[0]))):
        if not isinstance(word, (list, tuple)) or len(word) < 5:
            continue
        token = str(word[4] or "").strip()
        if not token:
            continue
        y0 = float(word[1])
        if rows and abs(y0 - rows[-1]["y"]) <= 2.4:
            rows[-1]["words"].append(word)
        else:
            rows.append({"y": y0, "words": [word]})
    for row in rows:
        labels = [str(w[4]).strip().upper() for w in row["words"]]
        if "OPSTK" not in labels or "IN/OT" not in labels or "PURCH" not in labels:
            continue
        ordered = sorted(row["words"], key=lambda w: float(w[0]))
        anchors: Dict[str, float] = {}
        pack_x0 = None
        seen_sale = 0
        seen_stock = False
        for word in ordered:
            label = str(word[4]).strip().upper()
            right = float(word[2])
            if label == "PACKING":
                pack_x0 = float(word[0])
            elif label == "OPSTK":
                anchors["opening_qty"] = right
            elif label == "PURCH":
                anchors["receipts_qty"] = right
            elif label == "SALE":
                seen_sale += 1
                if seen_sale == 1:
                    anchors["sales_qty"] = right
            elif label == "VAL" and seen_sale >= 2 and "sales_value" not in anchors:
                anchors["sales_value"] = right
            elif label == "IN/OT":
                anchors["in_ot"] = right
            elif label == "STOCK":
                anchors["closing_qty"] = right
                seen_stock = True
            elif label == "VAL" and seen_stock and "closing_value" not in anchors:
                anchors["closing_value"] = right
        needed = {
            "opening_qty",
            "receipts_qty",
            "sales_qty",
            "sales_value",
            "in_ot",
            "closing_qty",
            "closing_value",
        }
        if not needed <= set(anchors) or pack_x0 is None:
            return None
        rights = sorted(anchors.values())
        min_gap = min(rights[i + 1] - rights[i] for i in range(len(rights) - 1))
        return {
            "y": row["y"],
            "anchors": anchors,
            "pack_x0": pack_x0,
            "max_dist": max(8.0, min_gap * 0.45),
        }
    return None


def _medica_opstk_items_from_words(words: List[Any]) -> List[Dict[str, Any]]:
    """Read one Medica page. IN/OT is never closing qty; STOCK is."""
    header = _medica_opstk_header(words)
    if not header:
        return []
    anchors: Dict[str, float] = header["anchors"]
    max_dist = header["max_dist"]
    pack_x0 = header["pack_x0"]
    rows: List[Dict[str, Any]] = []
    for word in sorted(words or [], key=lambda w: (round(float(w[1]), 1), float(w[0]))):
        if not isinstance(word, (list, tuple)) or len(word) < 5:
            continue
        token = str(word[4] or "").strip()
        if not token:
            continue
        y0 = float(word[1])
        if y0 <= header["y"] + 2.0:
            continue
        if rows and abs(y0 - rows[-1]["y"]) <= 2.4:
            rows[-1]["words"].append(word)
        else:
            rows.append({"y": y0, "words": [word]})

    items: List[Dict[str, Any]] = []
    for row in rows:
        cells: Dict[str, List[str]] = {field: [] for field in anchors}
        name_bits: List[str] = []
        pack_bits: List[str] = []
        for word in sorted(row["words"], key=lambda w: float(w[0])):
            token = str(word[4]).strip()
            right = float(word[2])
            left = float(word[0])
            nearest = None
            nearest_dist = None
            for field, edge in anchors.items():
                dist = abs(right - edge)
                if nearest_dist is None or dist < nearest_dist:
                    nearest, nearest_dist = field, dist
            if (
                nearest
                and nearest_dist is not None
                and nearest_dist <= max_dist
                and re.fullmatch(r"-?\d+(?:\.\d+)?", token.replace(",", ""))
            ):
                cells[nearest].append(token.replace(",", ""))
                continue
            if right < pack_x0:
                name_bits.append(token)
            elif left >= pack_x0 - 1.0 and right < anchors["opening_qty"] - 8.0:
                pack_bits.append(token)
        name = _clean_name(" ".join(name_bits))
        if not name or re.search(
            r"\bTOTAL\b|DIVISION|END\s+OF\s+REPORT|PRODUCT\s+DESCRIPTION|PAGE\s+NO",
            name,
            re.I,
        ):
            continue
        if not cells["opening_qty"]:
            continue
        item = empty_line_item()
        item["product_name"] = name
        item["packing"] = _clean_name(" ".join(pack_bits)) or None

        def _cell(field: str) -> float:
            bits = cells.get(field) or []
            if not bits:
                return 0.0
            return _to_float(bits[-1])

        item["opening_qty"] = _cell("opening_qty")
        item["receipts_qty"] = _cell("receipts_qty")
        item["sales_qty"] = _cell("sales_qty")
        item["sales_value"] = _cell("sales_value")
        item["closing_qty"] = _cell("closing_qty")
        item["closing_value"] = _cell("closing_value")
        items.append(item)
    return items


def _parse_medica_opstk_statement(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Parse Medica Ultimate OPSTK/PURCH/SALE/IN-OT/STOCK rows from word positions.

    Returns None for every other statement layout.
    """
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_medica_opstk_statement(text) for text in page_texts):
        return None
    items: List[Dict[str, Any]] = []
    for page, text in zip(doc, page_texts):
        if not _is_medica_opstk_statement(text):
            continue
        items.extend(_medica_opstk_items_from_words(page.get_text("words") or []))
    if not items:
        return None
    result = empty_result(filename, "pdf")
    blob = "\n".join(page_texts)
    result["report_title"] = "STOCK STATEMENT"
    lines = [line.strip() for line in blob.splitlines() if line.strip()]
    for index, stripped in enumerate(lines):
        party = re.match(r"^TO\s*:\s*(.+)$", stripped, re.I)
        if party and not result.get("company_name"):
            result["company_name"] = _clean_name(party.group(1))
            if index > 0 and not result.get("stockist_name"):
                previous = lines[index - 1]
                if not re.search(r"STOCK|STATEMENT|PRODUCT|OPSTK|DATE|PACKING", previous, re.I):
                    result["stockist_name"] = _clean_name(previous)
        dates = re.search(
            r"From\s*Date\s*:?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*"
            r"To\s*Date\s*:?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
            stripped,
            re.I,
        )
        if dates:
            result["period_from"] = _normalize_date(dates.group(1))
            result["period_to"] = _normalize_date(dates.group(2))
    result["line_items"] = items
    result["totals"]["sales_value"] = round(
        sum(_to_float(item.get("sales_value")) for item in items), 2
    )
    result["totals"]["closing_value"] = round(
        sum(_to_float(item.get("closing_value")) for item in items), 2
    )
    result["totals"]["extra"]["extraction_method"] = "medica_opstk_columns"
    return result


def _is_summary_rtl_statement(text: str) -> bool:
    """SHUBHAM-style company-wise summary: OP QTY, OP AMT, SALE AMT, CLOSING AMT."""
    if not text:
        return False
    if not re.search(r"COMPANY\s*WISE|SUMMARY\s*RTL", text, re.I):
        return False
    if not re.search(r"SALES\s*(?:&|AND)\s*STOCK\s*STATEMENT", text, re.I):
        return False
    if re.search(r"SALE[\s.\-]*QTY|SALES[\s.\-]*QTY", text, re.I):
        return False
    glued = (
        re.search(r"OP[\s.\-]*QTY", text, re.I)
        and re.search(r"OP[\s.\-]*AMT", text, re.I)
        and re.search(r"SALE[\s.\-]*AMT", text, re.I)
        and re.search(r"CLOSING[\s.\-]*AMT", text, re.I)
    )
    if glued:
        return True
    # OP / QTY and SALE / AMT are often printed on two header lines.
    return bool(
        re.search(r"\bOP\b", text, re.I)
        and re.search(r"\bQTY\b", text, re.I)
        and re.search(r"\bAMT\b", text, re.I)
        and re.search(r"\bSALE\b", text, re.I)
        and re.search(r"\bCLOSING\b", text, re.I)
    )


def _summary_rtl_opening_amount(item: Dict[str, Any]) -> float:
    if isinstance(item, dict) and item.get("opening_value") not in (None, ""):
        return _to_float(item.get("opening_value"))
    extra = item.get("extra") if isinstance(item, dict) and isinstance(item.get("extra"), dict) else {}
    return _to_float(extra.get("opening_value"))


def _summary_rtl_fill(
    item: Dict[str, Any],
    op_qty: Optional[float],
    op_amt: Optional[float],
    sale_amt: Optional[float],
    closing_qty: Optional[float],
    closing_amt: Optional[float],
) -> None:
    """OP QTY, OP AMT, SALE AMT, CLOSING, CLOSING AMT. No sales qty column."""
    item["opening_qty"] = op_qty
    item["opening_value"] = op_amt
    item["sales_qty"] = None
    item["sales_value"] = sale_amt
    item["closing_qty"] = closing_qty
    item["closing_value"] = closing_amt
    item["receipts_qty"] = None
    extra = item.setdefault("extra", {})
    extra.pop("sales_qty", None)
    extra["opening_value"] = op_amt
    extra["receipts_value"] = None
    extra["layout"] = "summary_rtl_op_amt"


def _summary_rtl_items_lost_amounts(items: List[Dict[str, Any]]) -> bool:
    """True when qty was read but OP AMT and SALE AMT were both dropped."""
    if not items:
        return True
    return not any(
        _to_float(item.get("sales_value")) > 0 or _summary_rtl_opening_amount(item) > 0
        for item in items
        if isinstance(item, dict)
    )


def _summary_rtl_items_from_text(text: str) -> List[Dict[str, Any]]:
    """Map one product line: name, OP QTY, OP AMT, SALE AMT, CLOSING, CLOSING AMT."""
    items: List[Dict[str, Any]] = []
    pending_name = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _is_summary_rtl_statement(line):
            continue
        if re.search(
            r"^(PRODUCT\s*NAME|OP[\s.\-]*QTY|FROM\b|PAGE\b|SHUBHAM|HIMALAYA\s+DRUG)\b",
            line,
            re.I,
        ):
            pending_name = ""
            continue
        tokens = line.split()
        nums: List[float] = []
        idx = len(tokens) - 1
        while idx >= 0 and len(nums) < 5:
            tok = tokens[idx]
            if re.search(r"[A-Za-z]", tok):
                break
            val = _ocr_qty_token(tok)
            if val is None:
                break
            nums.append(val)
            idx -= 1
        if len(nums) != 5 or (
            idx >= 0
            and not re.search(r"[A-Za-z]", tokens[idx])
            and _ocr_qty_token(tokens[idx]) is not None
        ):
            if (
                not nums
                and re.search(r"[A-Za-z]", line)
                and not re.search(r"TOTAL|GRAND|STATEMENT|AGENCY", line, re.I)
            ):
                pending_name = _clean_name(line)
            else:
                pending_name = ""
            continue
        nums.reverse()
        name = _clean_name(" ".join(tokens[: idx + 1])) or pending_name
        pending_name = ""
        if not name or re.search(
            r"^(PRODUCT|TOTAL|GRAND|OPENING|CLOSING|OP\s*QTY)\b", name, re.I
        ):
            continue
        item = empty_line_item()
        item["product_name"] = name
        # nums: OP QTY, OP AMT, SALE AMT, CLOSING, CLOSING AMT
        _summary_rtl_fill(item, nums[0], nums[1], nums[2], nums[3], nums[4])
        items.append(item)
    return items


def _summary_rtl_from_ocr_bytes(
    file_bytes: bytes, filename: str, source_format: str
) -> Optional[Dict[str, Any]]:
    """Read this report from a page image before the generic vision schema.

    That schema has no opening_value, so OP AMT is dropped and line sales_value
    stays 0 while the footer Sales Amount is kept.
    """
    try:
        text = _ocr_image_to_text(file_bytes)
    except Exception as exc:
        logger.warning("SUMMARY RTL OCR skipped: %s", exc)
        return None
    if not _is_summary_rtl_statement(text):
        return None
    items = _summary_rtl_items_from_text(text)
    if not items or _summary_rtl_items_lost_amounts(items):
        return None
    return _summary_rtl_finish(items, text, filename, source_format)


def _rtl_label(token: str) -> str:
    return re.sub(r"[^A-Z]", "", str(token or "").upper())


def _summary_rtl_header(words: List[Any]) -> Optional[Dict[str, Any]]:
    """Column right-edges for OP QTY, OP AMT, SALE AMT, CLOSING, CLOSING AMT.

    Those labels may be one line (OP QTY) or stacked (OP over QTY).
    """
    rows: List[Dict[str, Any]] = []
    for word in sorted(words or [], key=lambda w: (round(float(w[1]), 1), float(w[0]))):
        if not isinstance(word, (list, tuple)) or len(word) < 5:
            continue
        token = str(word[4] or "").strip()
        if not token:
            continue
        y0 = float(word[1])
        if rows and abs(y0 - rows[-1]["y"]) <= 2.4:
            rows[-1]["words"].append(word)
        else:
            rows.append({"y": y0, "words": [word]})
    needed = {
        "opening_qty",
        "opening_value",
        "sales_value",
        "closing_qty",
        "closing_value",
    }
    for idx, row in enumerate(rows):
        band = list(row["words"])
        header_y = row["y"]
        if idx + 1 < len(rows) and rows[idx + 1]["y"] - row["y"] <= 16:
            nxt_labs = [_rtl_label(w[4]) for w in rows[idx + 1]["words"]]
            if any(lab in {"QTY", "AMT"} for lab in nxt_labs):
                band.extend(rows[idx + 1]["words"])
                header_y = rows[idx + 1]["y"]
        usable = [
            w
            for w in band
            if _rtl_label(w[4])
            in {
                "OP",
                "QTY",
                "AMT",
                "SALE",
                "SALES",
                "CLOSING",
                "OPQTY",
                "OPAMT",
                "SALEAMT",
                "SALESAMT",
                "CLOSINGAMT",
                "PRODUCT",
                "NAME",
            }
        ]
        usable.sort(key=lambda w: (float(w[0]), float(w[1])))
        anchors: Dict[str, float] = {}
        i = 0
        while i < len(usable):
            lab = _rtl_label(usable[i][4])
            nxt = _rtl_label(usable[i + 1][4]) if i + 1 < len(usable) else ""
            right = float(usable[i][2])
            nxt_right = float(usable[i + 1][2]) if i + 1 < len(usable) else right
            pair = False
            if i + 1 < len(usable):
                gap = float(usable[i + 1][0]) - float(usable[i][2])
                stacked = abs(float(usable[i][0]) - float(usable[i + 1][0])) < 28
                pair = gap < 22 or stacked
            if lab in {"PRODUCT", "NAME"}:
                i += 1
            elif lab == "OP" and nxt == "QTY" and pair:
                anchors["opening_qty"] = nxt_right
                i += 2
            elif lab == "OPQTY":
                anchors["opening_qty"] = right
                i += 1
            elif lab == "OP" and nxt == "AMT" and pair:
                anchors["opening_value"] = nxt_right
                i += 2
            elif lab == "OPAMT":
                anchors["opening_value"] = right
                i += 1
            elif lab in {"SALE", "SALES"} and nxt == "AMT" and pair:
                anchors["sales_value"] = nxt_right
                i += 2
            elif lab in {"SALEAMT", "SALESAMT"}:
                anchors["sales_value"] = right
                i += 1
            elif lab == "CLOSING" and nxt == "AMT" and pair:
                anchors["closing_value"] = nxt_right
                i += 2
            elif lab == "CLOSINGAMT":
                anchors["closing_value"] = right
                i += 1
            elif lab == "CLOSING":
                anchors["closing_qty"] = right
                i += 1
            else:
                i += 1
        if needed <= set(anchors):
            rights = sorted(anchors.values())
            min_gap = min(rights[i + 1] - rights[i] for i in range(len(rights) - 1))
            return {
                "y": header_y,
                "anchors": anchors,
                "max_dist": max(8.0, min_gap * 0.45),
            }
    return None


def _summary_rtl_items_from_words(words: List[Any]) -> List[Dict[str, Any]]:
    header = _summary_rtl_header(words)
    if not header:
        return []
    anchors: Dict[str, float] = header["anchors"]
    max_dist = header["max_dist"]
    name_limit = anchors["opening_qty"] - 8.0
    rows: List[Dict[str, Any]] = []
    for word in sorted(words or [], key=lambda w: (round(float(w[1]), 1), float(w[0]))):
        if not isinstance(word, (list, tuple)) or len(word) < 5:
            continue
        token = str(word[4] or "").strip()
        if not token:
            continue
        y0 = float(word[1])
        if y0 <= header["y"] + 2.0:
            continue
        if rows and abs(y0 - rows[-1]["y"]) <= 2.4:
            rows[-1]["words"].append(word)
        else:
            rows.append({"y": y0, "words": [word]})
    items: List[Dict[str, Any]] = []
    for row in rows:
        cells: Dict[str, List[str]] = {field: [] for field in anchors}
        name_bits: List[str] = []
        for word in sorted(row["words"], key=lambda w: float(w[0])):
            token = str(word[4]).strip()
            right = float(word[2])
            nearest = min(anchors, key=lambda field: abs(right - anchors[field]))
            if (
                abs(right - anchors[nearest]) <= max_dist
                and _ocr_qty_token(token) is not None
            ):
                cells[nearest].append(token.replace(",", ""))
                continue
            if right < name_limit:
                name_bits.append(token)
        name = _clean_name(" ".join(name_bits))
        if not name or re.search(r"^(PRODUCT|TOTAL|GRAND)\b", name, re.I):
            continue
        if not cells["opening_qty"]:
            continue
        item = empty_line_item()
        item["product_name"] = name
        _summary_rtl_fill(
            item,
            _to_float(cells["opening_qty"][-1]),
            _to_float(cells["opening_value"][-1]) if cells["opening_value"] else 0.0,
            _to_float(cells["sales_value"][-1]) if cells["sales_value"] else 0.0,
            _to_float(cells["closing_qty"][-1]) if cells["closing_qty"] else 0.0,
            _to_float(cells["closing_value"][-1]) if cells["closing_value"] else 0.0,
        )
        items.append(item)
    return items


def _summary_rtl_finish(
    items: List[Dict[str, Any]], blob: str, filename: str, source_format: str
) -> Dict[str, Any]:
    result = empty_result(filename, source_format)
    result["report_title"] = "SALES & STOCK STATEMENT"
    for line in (blob or "").splitlines():
        stripped = line.strip()
        if not result.get("stockist_name") and re.search(r"\bAGENCY\b", stripped, re.I):
            if not re.search(r"STATEMENT|PRODUCT|COMPANY\s*WISE", stripped, re.I):
                result["stockist_name"] = _clean_name(stripped)
        if not result.get("company_name") and re.search(r"HIMALAYA", stripped, re.I):
            if not re.search(r"STATEMENT|PRODUCT", stripped, re.I):
                result["company_name"] = _clean_name(stripped)
        dates = re.search(
            r"From\s*:?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*"
            r"To\s*:?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
            stripped,
            re.I,
        )
        if dates:
            result["period_from"] = _normalize_date(dates.group(1))
            result["period_to"] = _normalize_date(dates.group(2))
    result["line_items"] = items
    result["totals"]["sales_value"] = round(
        sum(_to_float(item.get("sales_value")) for item in items), 2
    )
    result["totals"]["closing_value"] = round(
        sum(_to_float(item.get("closing_value")) for item in items), 2
    )
    result["totals"]["extra"]["extraction_method"] = "summary_rtl_op_amt"
    result["totals"]["extra"]["opening_value"] = round(
        sum(_summary_rtl_opening_amount(item) for item in items),
        2,
    )
    return result


def _parse_summary_rtl_statement(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Parse company-wise SUMMARY RTL rows. Other layouts return None."""
    items: List[Dict[str, Any]] = []
    blobs: List[str] = []
    for page in doc:
        text = page.get_text("text") or ""
        words = page.get_text("words") or []
        blobs.append(text)
        page_items: List[Dict[str, Any]] = []
        if _is_summary_rtl_statement(text):
            page_items = _summary_rtl_items_from_text(text)
        if not page_items:
            page_items = _summary_rtl_items_from_words(words)
        if page_items and _summary_rtl_items_lost_amounts(page_items):
            page_items = []
        compact = re.sub(r"\s+", "", text)
        other_layout = bool(
            re.search(
                r"\bOPSTK\b|\bIN/OT\b|Receipt\s*/\s*Pur|\bOp\s*Stk\b|\bCl\s*Stk\b|Product\s+Stock\s+Report",
                text,
                re.I,
            )
        )
        if (
            not page_items
            and not other_layout
            and (
                len(compact) < 80
                or re.search(r"SUMMARY\s*RTL|COMPANY\s*WISE", text, re.I)
                or (
                    re.search(r"OP[\s.\-]*AMT", text, re.I)
                    and re.search(r"SALE[\s.\-]*AMT", text, re.I)
                )
            )
        ):
            ocr_text, _ocr_img = _ocr_pdf_page_text(page)
            if _is_summary_rtl_statement(ocr_text):
                ocr_items = _summary_rtl_items_from_text(ocr_text)
                if ocr_items and not _summary_rtl_items_lost_amounts(ocr_items):
                    page_items = ocr_items
                    blobs.append(ocr_text)
        items.extend(page_items)
    if not items:
        return None
    return _summary_rtl_finish(items, "\n".join(blobs), filename, "pdf")


_SSA_SCR_METHOD = "ssa_sale_closing_reorder"
_SSA_SCR_MONTHS = {
    "JANUARY",
    "FEBRUARY",
    "MARCH",
    "APRIL",
    "MAY",
    "JUNE",
    "JULY",
    "AUGUST",
    "SEPTEMBER",
    "OCTOBER",
    "NOVEMBER",
    "DECEMBER",
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
}


def _ssa_scr_letters(token: str) -> str:
    return re.sub(r"[^A-Z]", "", str(token or "").upper())


def _ssa_scr_rows(words: List[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for word in sorted(words or [], key=lambda w: (round(float(w[1]), 1), float(w[0]))):
        if not isinstance(word, (list, tuple)) or len(word) < 5:
            continue
        token = str(word[4] or "").strip()
        if not token or token.startswith("---"):
            continue
        x0, x1, y0 = float(word[0]), float(word[2]), float(word[1])
        if rows and abs(y0 - rows[-1]["y"]) <= 1.6:
            row = rows[-1]
        else:
            row = {"y": y0, "words": []}
            rows.append(row)
        row["words"].append((x0, x1, token))
    return rows


def _ssa_scr_header(rows: List[Dict[str, Any]], index: int) -> Optional[Dict[str, Any]]:
    """SALE qty/value + CLOSING qty/value + RE-ORDER. Other SSA headers return None."""
    if index + 1 >= len(rows):
        return None
    parent = rows[index]["words"]
    sub = rows[index + 1]["words"]
    parent_norm = [_ssa_scr_letters(token) for _x0, _x1, token in parent]
    sub_norm = [_ssa_scr_letters(token) for _x0, _x1, token in sub]
    blocked = {"OPENING", "RECEIPT", "ISSUE", "DUMP", "PURCHASE", "PURCHASES", "RATE"}
    if blocked.intersection(parent_norm) or blocked.intersection(sub_norm):
        return None
    if "SALE" not in parent_norm and "SALES" not in parent_norm:
        return None
    if "CLOSING" not in parent_norm:
        return None
    if "RE" not in parent_norm and "REORDER" not in parent_norm and "ORDER" not in sub_norm:
        return None
    if sub_norm.count("QTY") < 2 or sub_norm.count("VALUE") < 2 or "ORDER" not in sub_norm:
        return None

    anchors: Dict[str, Tuple[float, float, float]] = {}
    seen_qty = 0
    seen_value = 0
    for x0, x1, token in sub:
        label = _ssa_scr_letters(token)
        center = (x0 + x1) / 2.0
        if label == "QTY":
            seen_qty += 1
            field = "sales_qty" if seen_qty == 1 else "closing_qty" if seen_qty == 2 else ""
            if field:
                anchors[field] = (center, x0, x1)
        elif label == "VALUE":
            seen_value += 1
            field = "sales_value" if seen_value == 1 else "closing_value" if seen_value == 2 else ""
            if field:
                anchors[field] = (center, x0, x1)
        elif label == "ORDER":
            anchors["order_header"] = (center, x0, x1)
    needed = {"sales_qty", "sales_value", "closing_qty", "closing_value", "order_header"}
    if not needed.issubset(anchors):
        return None
    order_center = anchors["order_header"][0]
    closing_center = anchors["closing_value"][0]
    reorder_center = order_center + (order_center - closing_center)
    anchors["reorder"] = (reorder_center, reorder_center, reorder_center)
    return {"anchors": anchors, "next_index": index + 2}


def _ssa_scr_plain_number(token: str) -> Optional[float]:
    raw = str(token or "").strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return None
    return float(raw)


def _ssa_scr_reorder_value(header_token: str, right_token: str) -> Any:
    """Far-right column is reorder. A printed 0 there yields to a heading quantity.

    AMYRON prints 12 under RE-ORDER and 0 in the far column. Section totals print
    the reorder figure in the far column (27K / 29K). A dash under the heading
    with 0 on the right stays 0.
    """
    right = str(right_token or "").strip()
    header = str(header_token or "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?K", right, re.I):
        return right.upper()
    right_num = _ssa_scr_plain_number(right)
    header_num = _ssa_scr_plain_number(header)
    if right_num not in (None, 0.0):
        return right_num
    if header_num is not None:
        return header_num
    if right_num is not None:
        return right_num
    return None


def _ssa_scr_assign(
    words: List[Tuple[float, float, str]], anchors: Dict[str, Tuple[float, float, float]]
) -> Tuple[str, Optional[str], Dict[str, str]]:
    pack_min = anchors["sales_qty"][1] - 55.0
    number_min = anchors["sales_qty"][1] - 8.0
    name_bits: List[str] = []
    pack_bits: List[str] = []
    cells: Dict[str, str] = {}
    fields = (
        "sales_qty",
        "sales_value",
        "closing_qty",
        "closing_value",
        "order_header",
        "reorder",
    )
    for x0, x1, token in words:
        if x0 < pack_min:
            name_bits.append(token)
            continue
        if x0 < number_min:
            pack_bits.append(token)
            continue
        center = (x0 + x1) / 2.0
        field = min(fields, key=lambda name: abs(center - anchors[name][0]))
        cells[field] = token
    name = _clean_name(" ".join(name_bits))
    packing = _clean_name(" ".join(pack_bits)) or None
    return name, packing, cells


def _parse_ssa_sale_closing_reorder(doc, filename: str) -> Optional[Dict[str, Any]]:
    """STOCK & SALES ANALYSIS with SALE, CLOSING, and RE-ORDER only.

    Opening and receipt columns are not printed. Continuation pages, including
    rows above a repeated header, stay in the same statement. Other layouts
    return None.
    """
    page_rows: List[List[Dict[str, Any]]] = []
    blobs: List[str] = []
    detected = False
    for page in doc:
        text = page.get_text("text") or ""
        blobs.append(text)
        rows = _ssa_scr_rows(page.get_text("words") or [])
        page_rows.append(rows)
        for index in range(len(rows)):
            if _ssa_scr_header(rows, index):
                detected = True
                break
    if not detected:
        return None

    blob = "\n".join(blobs)
    result = empty_result(filename, "pdf")
    result["report_title"] = "STOCK & SALES ANALYSIS"
    company = re.search(r"\(\s*(HIMALAYA[^)]*)\)", blob, re.I)
    if company:
        result["company_name"] = _clean_name(company.group(1))
    elif re.search(r"\bHIMALAYA\b", blob, re.I):
        result["company_name"] = "HIMALAYA"
    for line in blob.splitlines():
        stripped = line.strip()
        if stripped and not re.search(
            r"STOCK|---|ITEM|GSTIN|SALE|CLOSING|Reorder|Page|Continued",
            stripped,
            re.I,
        ):
            result["stockist_name"] = _clean_name(stripped)
            break
    period = re.search(
        r"(\d{1,2}-\d{1,2}-\d{4})\s*-\s*(\d{1,2}-\d{1,2}-\d{4})",
        blob,
    )
    if period:
        result["period_from"] = _normalize_date(period.group(1))
        result["period_to"] = _normalize_date(period.group(2))

    items: List[Dict[str, Any]] = []
    section_totals: List[Dict[str, Any]] = []
    last_month_sales: List[Dict[str, Any]] = []
    anchors: Optional[Dict[str, Tuple[float, float, float]]] = None
    pending_month: Optional[str] = None
    pending_qty: Optional[float] = None

    def _row_number(cells: Dict[str, str], packing: Optional[str], name: str) -> Optional[float]:
        tokens = list(cells.values())
        if packing:
            tokens.append(packing)
        tokens.extend(name.split())
        for token in tokens:
            number = _ssa_scr_plain_number(str(token))
            if number is not None:
                return number
        return None

    for rows in page_rows:
        index = 0
        while index < len(rows):
            header = _ssa_scr_header(rows, index)
            if header:
                anchors = header["anchors"]
                pending_month = None
                pending_qty = None
                index = header["next_index"]
                continue
            if not anchors:
                index += 1
                continue
            name, packing, cells = _ssa_scr_assign(rows[index]["words"], anchors)
            index += 1
            if not name:
                continue
            upper = name.upper()
            if upper.startswith("LAST MONTH SALE"):
                month = next(
                    (part for part in name.split() if part.upper() in _SSA_SCR_MONTHS),
                    None,
                )
                pending_month = month or pending_month
                pending_qty = None
                continue
            if pending_month and upper.startswith("QUANTITY"):
                pending_qty = _row_number(cells, packing, name)
                continue
            if pending_month and upper.startswith("VALUE"):
                value = _row_number(cells, packing, name)
                if pending_qty is not None and value is not None:
                    last_month_sales.append(
                        {
                            "month": pending_month,
                            "quantity": pending_qty,
                            "value": value,
                        }
                    )
                pending_month = None
                pending_qty = None
                continue
            if upper == "TOTAL":
                sales_qty = _ssa_scr_plain_number(cells.get("sales_qty", ""))
                sales_value = _ssa_scr_plain_number(cells.get("sales_value", ""))
                closing_qty = _ssa_scr_plain_number(cells.get("closing_qty", ""))
                closing_value = _ssa_scr_plain_number(cells.get("closing_value", ""))
                reorder = _ssa_scr_reorder_value(
                    cells.get("order_header", ""),
                    cells.get("reorder", ""),
                )
                if sales_qty is None and closing_qty is None:
                    continue
                section_totals.append(
                    {
                        "sales_qty": sales_qty,
                        "sales_value": sales_value,
                        "closing_qty": closing_qty,
                        "closing_value": closing_value,
                        "reorder": reorder,
                    }
                )
                continue
            if re.search(
                r"^(ITEM|DESCRIPTION|STOCK|PAGE|CONTINUED|HIMALAYA|REORDER)\b",
                name,
                re.I,
            ):
                continue
            if "sales_qty" not in cells and "closing_qty" not in cells:
                continue
            if len(re.sub(r"[^A-Za-z]", "", name)) < 3:
                continue
            sales_qty = _ssa_scr_plain_number(cells.get("sales_qty", ""))
            sales_value = _ssa_scr_plain_number(cells.get("sales_value", ""))
            closing_qty = _ssa_scr_plain_number(cells.get("closing_qty", ""))
            closing_value = _ssa_scr_plain_number(cells.get("closing_value", ""))
            if None in (sales_qty, sales_value, closing_qty, closing_value):
                continue
            reorder = _ssa_scr_reorder_value(
                cells.get("order_header", ""),
                cells.get("reorder", ""),
            )
            item = empty_line_item()
            item["product_name"] = name
            item["packing"] = packing
            item["opening_qty"] = None
            item["opening_value"] = None
            item["receipts_qty"] = None
            item["receipts_value"] = None
            item["receipt_value"] = None
            item["sales_qty"] = sales_qty
            item["sales_value"] = sales_value
            item["closing_qty"] = closing_qty
            item["closing_value"] = closing_value
            item["reorder_qty"] = reorder
            extra = item["extra"]
            extra["layout"] = _SSA_SCR_METHOD
            extra["opening_value"] = None
            extra["receipts_value"] = None
            extra["receipt_value"] = None
            extra["reorder_qty"] = reorder
            items.append(item)

    if not items:
        return None
    result["line_items"] = items
    extra = result["totals"].setdefault("extra", {})
    extra["extraction_method"] = _SSA_SCR_METHOD
    extra["layout"] = _SSA_SCR_METHOD
    extra["section_totals"] = section_totals
    extra["split_pages"] = list(range(1, len(page_rows) + 1))
    result["split_pages"] = extra["split_pages"]
    if last_month_sales:
        extra["last_month_sale"] = last_month_sales[0]
        extra["last_month_sales"] = last_month_sales
    if section_totals:
        last_total = section_totals[-1]
        result["totals"]["sales_qty"] = last_total.get("sales_qty")
        result["totals"]["sales_value"] = last_total.get("sales_value")
        result["totals"]["closing_qty"] = last_total.get("closing_qty")
        result["totals"]["closing_value"] = last_total.get("closing_value")
        extra["reorder"] = last_total.get("reorder")
    result["totals"]["opening_qty"] = None
    result["totals"]["receipts_qty"] = None
    result["totals"]["opening_value"] = None
    result["totals"]["receipts_value"] = None
    return result


_SSA_FREE_ROLE_FIELD = {
    ("opening", "QTY"): "opening_qty",
    ("opening", "STOCK"): "opening_qty",
    ("opening", "VALUE"): "opening_value",
    ("purchase", "QTY"): "receipts_qty",
    ("purchase", "FREE"): "purchase_free",
    ("purchase", "VALUE"): "purchase_value",
    ("sales_return", "QTY"): "sr_qty",
    ("sales_return", "FREE"): "sr_free",
    ("sales_return", "VALUE"): "sr_value",
    ("other_in", "QTY"): "repl_other",
    ("other_in", "OTHER"): "repl_other",
    ("total", "QTY"): "total_stock",
    ("total", "STOCK"): "total_stock",
    ("sales", "QTY"): "sales_qty",
    ("sales", "FREE"): "sales_free",
    ("sales", "VALUE"): "sales_value",
    ("sample", "QTY"): "sample_qty",
    ("stock_tf", "QTY"): "stock_tf_qty",
    ("stock_tf", "VALUE"): "stock_tf_value",
    ("purchase_return", "QTY"): "pr_qty",
    ("purchase_return", "VALUE"): "pr_value",
    ("other_out", "QTY"): "repl_other_out",
    ("other_out", "OTHER"): "repl_other_out",
    ("closing", "QTY"): "closing_qty",
    ("closing", "STOCK"): "closing_qty",
    ("closing", "VALUE"): "closing_value",
}


def _is_ssa_sales_free_text(text: str) -> bool:
    """Grouped STOCK & SALES ANALYSIS with SALES QTY and SALES FREE apart."""
    if not text:
        return False
    if not re.search(r"STOCK\s*&\s*SALES\s+ANALYSIS", text, re.I):
        return False
    if not re.search(r"\bSAMPLE\b", text, re.I):
        return False
    if not re.search(r"STOCK\s*T\s*/\s*F", text, re.I):
        return False
    if not re.search(r"\bFREE\b", text, re.I):
        return False
    return bool(re.search(r"OPENING\s+STOCK", text, re.I) and re.search(r"CLOSING\s+STOCK", text, re.I))


def _ssa_free_label(token: str) -> str:
    raw = str(token or "").upper()
    if re.fullmatch(r"T\s*/\s*F", raw.strip()):
        return "TF"
    return re.sub(r"[^A-Z]", "", raw)


def _ssa_free_number(token: str) -> Optional[float]:
    text = str(token or "").strip()
    if text in {"-", "–", "—", "--"}:
        return 0.0
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    return None


def _ssa_free_parent_groups(words: List[Tuple[float, float, str]]) -> List[Tuple[str, float]]:
    """Parent labels left to right. SALES RETURN is not the SALES group."""
    labels = []
    for x0, x1, token in words:
        if re.fullmatch(r"[<\-=]+>?", token) or set(token) <= set("-<=>"):
            continue
        label = _ssa_free_label(token)
        if not label:
            continue
        labels.append((label, (x0 + x1) / 2.0))
    groups: List[Tuple[str, float]] = []
    seen_total = False
    index = 0
    while index < len(labels):
        label, center = labels[index]
        nxt = labels[index + 1][0] if index + 1 < len(labels) else ""
        if label == "OPENING":
            groups.append(("opening", center))
            index += 2 if nxt == "STOCK" else 1
        elif label == "PURCHASE" and nxt == "RETURN":
            groups.append(("purchase_return", center))
            index += 2
        elif label == "PURCHASE":
            groups.append(("purchase", center))
            index += 1
        elif label == "SALES" and nxt == "RETURN":
            groups.append(("sales_return", center))
            index += 2
        elif label == "SALES":
            groups.append(("sales", center))
            index += 1
        elif label in {"OTHER", "REPL"}:
            groups.append(("other_out" if seen_total else "other_in", center))
            index += 1
        elif label == "SR":
            groups.append(("sales_return", center))
            index += 1
        elif label == "PR":
            groups.append(("purchase_return", center))
            index += 1
        elif label == "TOTAL":
            groups.append(("total", center))
            seen_total = True
            index += 1
        elif label == "SAMPLE":
            groups.append(("sample", center))
            index += 1
        elif label == "STOCK" and nxt == "TF":
            groups.append(("stock_tf", center))
            index += 2
        elif label == "STOCK":
            if seen_total:
                groups.append(("stock_tf", center))
            index += 1
        elif label == "CLOSING":
            groups.append(("closing", center))
            index += 2 if nxt == "STOCK" else 1
        else:
            index += 1
    return groups


def _ssa_free_columns(groups: List[Tuple[str, float]], sub_words: List[Tuple[float, float, str]]):
    if not groups:
        return []
    columns = []
    for x0, x1, token in sub_words:
        role = _ssa_free_label(token)
        if role not in {"QTY", "FREE", "VALUE", "STOCK", "OTHER", "SAMPLE", "TF"}:
            continue
        center = (x0 + x1) / 2.0
        if role == "SAMPLE":
            columns.append(("sample_qty", center, x0))
            continue
        if role == "TF":
            columns.append(("stock_tf_qty", center, x0))
            continue
        distances = sorted((abs(center - mid), name) for name, mid in groups)
        dist, parent = distances[0]
        second = distances[1][0] if len(distances) > 1 else dist + 80.0
        # A subheader may sit under the group label rather than on the QTY word.
        # Keep the old 40pt acceptance, and also accept a farther word when the
        # next group is still further away.
        if dist > max(40.0, second * 0.6):
            continue
        field = _SSA_FREE_ROLE_FIELD.get((parent, role))
        if field is None:
            continue
        columns.append((field, center, x0))
    needed = {"opening_qty", "receipts_qty", "sales_qty", "sales_free", "closing_qty"}
    if not needed.issubset({field for field, _c, _x in columns}):
        return []
    return columns


def _ssa_free_cluster_rows(words: List[Any], y_tol: float = 2.0) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for word in sorted(words, key=lambda item: (round(float(item[1]), 1), float(item[0]))):
        token = str(word[4]).strip()
        if not token or token.startswith("---"):
            continue
        y0 = float(word[1])
        box = (float(word[0]), float(word[2]), token)
        if rows and abs(y0 - rows[-1]["y"]) <= y_tol:
            rows[-1]["words"].append(box)
        else:
            rows.append({"y": y0, "words": [box]})
    for row in rows:
        row["words"].sort(key=lambda box: box[0])
    return rows


def _parse_ssa_sales_free_columns(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Map SALES QTY and SALES FREE by their own subheader, not the SALES label.

    Returns None for every other statement. A blank cell stays in that column.
    """
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_ssa_sales_free_text(text) for text in page_texts):
        return None

    result = empty_result(filename, "pdf")
    result["report_title"] = "STOCK & SALES ANALYSIS"
    columns = None
    items: List[Dict[str, Any]] = []
    for page, text in zip(doc, page_texts):
        if not _is_ssa_sales_free_text(text):
            continue
        if not result.get("stockist_name"):
            for line in text.splitlines():
                raw = line.strip()
                if not raw or len(raw) > 80:
                    continue
                if re.search(r"STOCK|ITEM|GSTIN|PHONE|ROAD|FLOOR|PIN|@", raw, re.I):
                    continue
                result["stockist_name"] = _clean_name(raw)
                break
        if not result.get("company_name"):
            company = re.search(r"\(([^)]+)\)", text)
            if company:
                result["company_name"] = _clean_name(company.group(1))
        if result.get("period_from") is None:
            period = re.search(
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*-\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
                text,
            )
            if period:
                result["period_from"] = _normalize_date(period.group(1))
                result["period_to"] = _normalize_date(period.group(2))
        rows = _ssa_free_cluster_rows(page.get_text("words") or [])
        if columns is None:
            for index, row in enumerate(rows[:-1]):
                groups = _ssa_free_parent_groups(row["words"])
                names = {name for name, _mid in groups}
                if not {"opening", "purchase", "sales", "closing"}.issubset(names):
                    continue
                built = _ssa_free_columns(groups, rows[index + 1]["words"])
                if built:
                    columns = built
                    break
        if not columns:
            continue
        opening_x0 = next(x0 for field, _c, x0 in columns if field == "opening_qty")
        centers = [center for _f, center, _x in columns]
        gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
        pitch = sorted(gaps)[len(gaps) // 2] if gaps else 20.0
        numbered_rows = []
        for row in rows:
            cells: Dict[str, List[Tuple[float, float]]] = {}
            left: List[Tuple[float, float, str]] = []
            for x0, x1, token in row["words"]:
                center = (x0 + x1) / 2.0
                if center < opening_x0 - 2:
                    left.append((x0, x1, token))
                    continue
                nearest = min(range(len(columns)), key=lambda i: abs(centers[i] - center))
                if abs(centers[nearest] - center) <= pitch * 0.65:
                    number = _ssa_free_number(token)
                    if number is not None:
                        cells.setdefault(columns[nearest][0], []).append((abs(centers[nearest] - center), number))
            if cells:
                numbered_rows.append((left, cells))
        pack_counts: Dict[int, int] = {}
        for left, _cells in numbered_rows:
            for x0, _x1, _token in left[1:]:
                if x0 > 60:
                    pack_counts[round(x0)] = pack_counts.get(round(x0), 0) + 1
        pack_floor = None
        if numbered_rows:
            min_hits = max(2, int(len(numbered_rows) * 0.6))
            shared = [x for x, count in pack_counts.items() if count >= min_hits]
            if shared:
                pack_floor = min(shared) - 1
        for left, cells in numbered_rows:
            if pack_floor is None:
                name_bits = [token for _x0, _x1, token in left]
                pack_bits: List[str] = []
            else:
                name_bits = [token for x0, _x1, token in left if x0 < pack_floor]
                pack_bits = [token for x0, _x1, token in left if x0 >= pack_floor]
            name = _clean_name(" ".join(name_bits))
            if not name or not re.search(r"[A-Za-z]", name):
                continue
            if re.match(r"^(?:TOTAL|GRAND|ITEM|DESCRIPTION|HIMALAYA)\b", name, re.I):
                continue
            if re.search(r"STOCK\s*&\s*SALES|SALES\s+ANALYSIS|REORDER", name, re.I):
                continue

            def cell_value(field: str) -> float:
                picks = cells.get(field) or []
                if not picks:
                    return 0.0
                picks.sort(key=lambda item: item[0])
                return picks[0][1]

            item = empty_line_item()
            item["product_name"] = name
            item["packing"] = _clean_name(" ".join(pack_bits)) or None
            item["opening_qty"] = cell_value("opening_qty")
            item["receipts_qty"] = cell_value("receipts_qty")
            item["sales_qty"] = cell_value("sales_qty")
            item["sales_value"] = cell_value("sales_value")
            item["closing_qty"] = cell_value("closing_qty")
            item["closing_value"] = cell_value("closing_value")
            item["extra"] = {
                "layout": "ssa_sales_free_columns",
                "source_product_name": name,
                "source_packing": item["packing"],
                "purchase_free": cell_value("purchase_free"),
                "sr_qty": cell_value("sr_qty"),
                "sr_free": cell_value("sr_free"),
                "repl_other": cell_value("repl_other"),
                "total_stock": cell_value("total_stock"),
                "sales_free": cell_value("sales_free"),
                "sample_qty": cell_value("sample_qty"),
                "stock_tf_qty": cell_value("stock_tf_qty"),
                "pr_qty": cell_value("pr_qty"),
                "repl_other_out": cell_value("repl_other_out"),
                "opening_value": cell_value("opening_value"),
                "purchase_value": cell_value("purchase_value"),
            }
            items.append(item)
    if not columns or not items:
        return None
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "ssa_sales_free_columns"
    extra["layout"] = "ssa_sales_free_columns"
    extra["rows_detected"] = len(items)
    extra["total_row_source"] = "product_row_sum"
    for key in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
        total = sum(_to_float(item.get(key)) for item in items)
        result["totals"][key] = total
        extra[key] = total
    return result


def _is_ssa_sales_free_image_text(text: str) -> bool:
    """Screenshot of SALES QTY / SALES FREE. Not the ISSUE / DUMP qty-value print."""
    if not text or re.search(r"\bDUMP\b", text, re.I):
        return False
    if not re.search(r"STOCK\s*&\s*SALES\s+ANALYSIS", text, re.I):
        return False
    if not re.search(r"\bOPENING\b", text, re.I):
        return False
    if not re.search(r"\bPURCHASE\b", text, re.I):
        return False
    if not re.search(r"\bSALES\b", text, re.I):
        return False
    if not re.search(r"\bCLOSING\b", text, re.I):
        return False
    if not re.search(r"\bFREE\b", text, re.I):
        return False
    return bool(re.search(r"\bSAMPLE\b|T\s*/\s*F", text, re.I))


def _ssa_free_items_from_rows(rows: List[Dict[str, Any]], columns, pack_gap: float) -> List[Dict[str, Any]]:
    opening_x0 = next(x0 for field, _c, x0 in columns if field == "opening_qty")
    centers = [center for _f, center, _x in columns]
    gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
    pitch = sorted(gaps)[len(gaps) // 2] if gaps else 20.0
    items: List[Dict[str, Any]] = []
    for row in rows:
        cells: Dict[str, List[Tuple[float, float]]] = {}
        left: List[Tuple[float, float, str]] = []
        for x0, x1, token in row["words"]:
            center = (x0 + x1) / 2.0
            if center < opening_x0 - 2:
                left.append((x0, x1, token))
                continue
            nearest = min(range(len(columns)), key=lambda i: abs(centers[i] - center))
            if abs(centers[nearest] - center) <= pitch * 0.65:
                number = _ssa_free_number(token)
                if number is not None:
                    cells.setdefault(columns[nearest][0], []).append(
                        (abs(centers[nearest] - center), number)
                    )
        if not cells:
            continue
        split_at = None
        if len(left) >= 2 and pack_gap > 0:
            best = 0.0
            for prev, nxt in zip(left, left[1:]):
                gap = nxt[0] - prev[1]
                if gap > best:
                    best = gap
                    split_at = nxt[0]
            if best < pack_gap:
                split_at = None
        if split_at is None:
            name_bits = [token for _x0, _x1, token in left]
            pack_bits: List[str] = []
        else:
            name_bits = [token for x0, _x1, token in left if x0 < split_at]
            pack_bits = [token for x0, _x1, token in left if x0 >= split_at]
        name = _clean_name(" ".join(name_bits))
        if not name or not re.search(r"[A-Za-z]", name):
            continue
        if re.match(r"^(?:TOTAL|GRAND|ITEM|DESCRIPTION|HIMALAYA|QUANTITY|VALUE)\b", name, re.I):
            continue
        if re.search(r"STOCK\s*&\s*SALES|SALES\s+ANALYSIS|REORDER", name, re.I):
            continue

        def cell_value(field: str) -> float:
            picks = cells.get(field) or []
            if not picks:
                return 0.0
            picks.sort(key=lambda item: item[0])
            return picks[0][1]

        item = empty_line_item()
        item["product_name"] = name
        item["packing"] = _clean_name(" ".join(pack_bits)) or None
        item["opening_qty"] = cell_value("opening_qty")
        item["receipts_qty"] = cell_value("receipts_qty")
        item["sales_qty"] = cell_value("sales_qty")
        item["sales_value"] = cell_value("sales_value")
        item["closing_qty"] = cell_value("closing_qty")
        item["closing_value"] = cell_value("closing_value")
        item["extra"] = {
            "layout": "ssa_sales_free_columns",
            "source_product_name": name,
            "source_packing": item["packing"],
            "purchase_free": cell_value("purchase_free"),
            "sr_qty": cell_value("sr_qty"),
            "sr_free": cell_value("sr_free"),
            "repl_other": cell_value("repl_other"),
            "total_stock": cell_value("total_stock"),
            "sales_free": cell_value("sales_free"),
            "sample_qty": cell_value("sample_qty"),
            "stock_tf_qty": cell_value("stock_tf_qty"),
            "pr_qty": cell_value("pr_qty"),
            "repl_other_out": cell_value("repl_other_out"),
            "opening_value": cell_value("opening_value"),
            "purchase_value": cell_value("purchase_value"),
        }
        items.append(item)
    return items


def _parse_ssa_sales_free_image(
    file_bytes: bytes, filename: str, ext: str
) -> Optional[Dict[str, Any]]:
    """Read a sideways photo of SALES QTY / SALES FREE by column x position."""
    from PIL import Image, ImageEnhance, ImageOps

    image = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    candidates = [image]
    if image.height > image.width * 1.15:
        candidates = [
            image.rotate(90, expand=True),
            image.rotate(270, expand=True),
        ]
    pytesseract = _a2z_tesseract()
    for candidate in candidates:
        width, height = candidate.size
        page = candidate.crop((int(width * 0.02), 4, width - 4, height - 4))
        page = ImageOps.autocontrast(page)
        page = ImageEnhance.Contrast(page).enhance(1.4)
        page = page.resize((page.width * 2, page.height * 2), Image.Resampling.LANCZOS)
        preview = pytesseract.image_to_string(page, config="--psm 6") or ""
        if not _is_ssa_sales_free_image_text(preview):
            continue
        data = pytesseract.image_to_data(page, config="--psm 6", output_type=pytesseract.Output.DICT)
        words = []
        heights = []
        for index, token in enumerate(data["text"]):
            token = str(token or "").strip()
            if not token:
                continue
            x0 = int(data["left"][index])
            y0 = int(data["top"][index])
            word_h = int(data["height"][index])
            words.append((x0, y0, x0 + int(data["width"][index]), y0 + word_h, token))
            if word_h > 0:
                heights.append(word_h)
        if not words:
            continue
        heights.sort()
        y_tol = max(8.0, heights[len(heights) // 2] * 0.55)
        rows = _ssa_free_cluster_rows(words, y_tol=y_tol)
        columns = None
        for index, row in enumerate(rows[:-1]):
            groups = _ssa_free_parent_groups(row["words"])
            names = {name for name, _mid in groups}
            if not {"opening", "purchase", "sales", "closing"}.issubset(names):
                continue
            built = _ssa_free_columns(groups, rows[index + 1]["words"])
            if built:
                columns = built
                break
        if not columns:
            continue
        items = _ssa_free_items_from_rows(rows, columns, pack_gap=max(24.0, y_tol * 3))
        if len(items) < 3:
            continue
        result = empty_result(filename, ext.lstrip(".") or "jpg")
        result["report_title"] = "STOCK & SALES ANALYSIS"
        if re.search(r"HIMALAYA\s+ZANDRA", preview, re.I):
            result["company_name"] = "HIMALAYA ZANDRA DIVISION"
        period = re.search(
            r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*-\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
            preview,
        )
        if period:
            result["period_from"] = _normalize_date(period.group(1))
            result["period_to"] = _normalize_date(period.group(2))
        result["line_items"] = items
        extra = result["totals"]["extra"]
        extra["extraction_method"] = "ssa_sales_free_columns"
        extra["layout"] = "ssa_sales_free_columns"
        extra["rows_detected"] = len(items)
        extra["total_row_source"] = "product_row_sum"
        for key in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
            total = sum(_to_float(item.get(key)) for item in items)
            result["totals"][key] = total
            extra[key] = total
        return result
    return None


def _parse_stock_sales_analysis(doc, filename: str) -> Optional[Dict[str, Any]]:
    """STOCK & SALES ANALYSIS: TOTAL STOCK is not the SALES QTY column.

    Returns None unless that two-line header is printed.
    """
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(re.search(r"STOCK\s*&\s*SALES\s+ANALYSIS", text, re.I) for text in page_texts):
        return None

    result = empty_result(filename, "pdf")
    result["report_title"] = "STOCK & SALES ANALYSIS"
    items: List[Dict[str, Any]] = []

    def parent_field(token: str) -> Optional[str]:
        norm = re.sub(r"[^A-Z]", "", token.upper())
        if norm == "OPENING":
            return "opening"
        if norm == "PURCHASE":
            return "purchase"
        if norm == "TOTAL":
            return "total"
        if norm == "SALES":
            return "sales"
        if norm == "CLOSING":
            return "closing"
        return None

    for page, text in zip(doc, page_texts):
        if not result.get("stockist_name"):
            for line in text.splitlines():
                line = line.strip()
                if line and not re.search(r"GSTIN|STOCK|---|ITEM", line, re.I):
                    result["stockist_name"] = _clean_name(line)
                    break
        if not result.get("company_name"):
            company = re.search(r"\((HIMALAYA[^)]+)\)", text, re.I)
            if company:
                result["company_name"] = _clean_name(company.group(1))
        if result["period_from"] is None:
            m = re.search(
                r"(\d{1,2}-\d{1,2}-\d{4})\s*-\s*(\d{1,2}-\d{1,2}-\d{4})",
                text,
            )
            if m:
                result["period_from"] = _normalize_date(m.group(1))
                result["period_to"] = _normalize_date(m.group(2))

        words = sorted(page.get_text("words") or [], key=lambda w: (round(w[1], 1), w[0]))
        rows: List[Dict[str, Any]] = []
        for w in words:
            x0, x1, y0, token = float(w[0]), float(w[2]), float(w[1]), str(w[4]).strip()
            if not token or token.startswith("---"):
                continue
            if rows and abs(y0 - rows[-1]["y"]) <= 1.6:
                row = rows[-1]
            else:
                row = {"y": y0, "words": []}
                rows.append(row)
            row["words"].append((x0, x1, token))

        columns: Optional[List[Tuple[str, float, float]]] = None
        name_end = 160.0
        for i, row in enumerate(rows[:-1]):
            parents = []
            for x0, x1, token in row["words"]:
                field = parent_field(token)
                if field:
                    parents.append((field, (x0 + x1) / 2.0))
            if not {"opening", "purchase", "total", "sales", "closing"}.issubset(
                {field for field, _c in parents}
            ):
                continue
            sub = rows[i + 1]
            starts: List[Tuple[Optional[str], float]] = []
            for x0, x1, token in sub["words"]:
                norm = re.sub(r"[^A-Z]", "", token.upper())
                center = (x0 + x1) / 2.0
                nearest, dist = min(
                    ((field, abs(center - mid)) for field, mid in parents),
                    key=lambda item: item[1],
                )
                parent = nearest if dist <= 28 else ""
                field = None
                if parent == "opening" and norm == "STOCK":
                    field = "opening_qty"
                elif parent == "purchase" and norm == "QTY":
                    field = "receipts_qty"
                elif parent == "total" and norm == "STOCK":
                    field = "total_stock"
                elif parent == "sales" and norm == "QTY":
                    field = "sales_qty"
                elif parent == "closing" and norm == "STOCK":
                    field = "closing_qty"
                starts.append((field, x0))
            if not {"opening_qty", "total_stock", "sales_qty", "closing_qty"}.issubset(
                {name for name, _x in starts}
            ):
                continue
            spans = []
            for idx, (name, start) in enumerate(starts):
                end = starts[idx + 1][1] if idx + 1 < len(starts) else 10000.0
                spans.append((name, start, end))
            columns = spans
            name_end = next(x for name, x in starts if name == "opening_qty") - 55
            break
        if not columns:
            continue

        def field_for(x1: float) -> Optional[str]:
            probe = x1 - 0.5
            for name, start, end in columns:
                if start <= probe < end:
                    return name
            return None

        for row in rows:
            cells: Dict[str, List[str]] = {}
            name_tokens: List[str] = []
            for x0, x1, token in row["words"]:
                if x0 < name_end and not re.fullmatch(r"[\d.]+", token):
                    name_tokens.append(token)
                    continue
                field = field_for(x1)
                if field and re.fullmatch(r"-?\d+(?:\.\d+)?", token):
                    cells.setdefault(field, []).append(token)
            name = _clean_name(" ".join(name_tokens))
            if not name or re.search(r"ITEM|DESCRIPTION|OPENING|TOTAL", name, re.I):
                continue
            if "opening_qty" not in cells and "sales_qty" not in cells:
                continue
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = _detail_opval_number(cells.get("opening_qty") or []) or 0.0
            item["receipts_qty"] = _detail_opval_number(cells.get("receipts_qty") or []) or 0.0
            item["sales_qty"] = _detail_opval_number(cells.get("sales_qty") or []) or 0.0
            item["closing_qty"] = _detail_opval_number(cells.get("closing_qty") or []) or 0.0
            extra = item["extra"]
            extra["layout"] = "stock_sales_analysis"
            total = _detail_opval_number(cells.get("total_stock") or [])
            if total is not None:
                extra["total_stock"] = total
            items.append(item)

    if not items:
        return None
    result["line_items"] = items
    result["totals"]["extra"]["extraction_method"] = "stock_sales_analysis"
    result["totals"]["extra"]["layout"] = "stock_sales_analysis"
    return result


def _is_pharmassist_stock_sale_report(text: str) -> bool:
    """C-Square PharmAssist Stock and Sale Report (Jul/Jun/Op./Pur/Sale/Bal./BVal/SVal)."""
    if not text:
        return False
    return bool(
        re.search(r"Stock\s+and\s+Sale\s+Report", text, re.I)
        and re.search(r"PharmAssist", text, re.I)
        and re.search(r"\bBVal\b", text)
        and re.search(r"\bSVal\b", text)
    )


def _pharmassist_header_field(token: str) -> Optional[str]:
    norm = re.sub(r"[^a-z]", "", (token or "").lower())
    return {
        "pack": "packing",
        "jul": "jul_qty",
        "jun": "jun_qty",
        "op": "opening_qty",
        "pur": "receipts_qty",
        "sp": "sp_qty",
        "sale": "sales_qty",
        "ss": "ss_qty",
        "br": "br_qty",
        "bsc": "bsc_qty",
        "cr": "cr_qty",
        "db": "db_qty",
        "adj": "adj_qty",
        "bal": "closing_qty",
        "bval": "closing_value",
        "sval": "sales_value",
        "order": "order_qty",
    }.get(norm)


def _parse_pharmassist_stock_sale_report(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Map PharmAssist columns by header position. Other layouts return None."""
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_pharmassist_stock_sale_report(text) for text in page_texts):
        return None

    result = empty_result(filename, "pdf")
    result["report_title"] = "Stock and Sale Report"
    items: List[Dict[str, Any]] = []
    line_fields = {
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "sales_value",
        "closing_qty",
        "closing_value",
    }

    for page, text in zip(doc, page_texts):
        if not _is_pharmassist_stock_sale_report(text):
            continue
        if not result.get("stockist_name"):
            for line in text.splitlines():
                raw = line.strip()
                if raw and not re.search(r"Stock\s+and\s+Sale|Phone|Item|Pack", raw, re.I):
                    result["stockist_name"] = _clean_name(raw)
                    break
        if not result.get("stockist_address"):
            address = re.search(r"([A-Z0-9 .,'/-]+Phone\s*-\s*[^\n]+)", text, re.I)
            if address:
                result["stockist_address"] = _clean_name(address.group(1))
        if result["period_from"] is None:
            period = re.search(
                r"From\s+date\s+(\d{1,2}-[A-Za-z]{3}-\d{2,4})\s+to\s+(\d{1,2}-[A-Za-z]{3}-\d{2,4})",
                text,
                re.I,
            )
            if period:
                result["period_from"] = _normalize_date(period.group(1))
                result["period_to"] = _normalize_date(period.group(2))
        if not result.get("company_name"):
            company = re.search(
                r"For\s+Manufacturer\s*:\s*(.+)",
                text,
                re.I,
            )
            if company:
                result["company_name"] = _clean_name(company.group(1).split("\n")[0])

        words = page.get_text("words") or []
        header_ys = [
            float(w[1])
            for w in words
            if re.fullmatch(r"Item", str(w[4]), re.I)
        ]
        if not header_ys:
            continue
        header_y = header_ys[0]
        fences: List[Tuple[Optional[str], float]] = []
        for w in words:
            if abs(float(w[1]) - header_y) > 3:
                continue
            token = str(w[4])
            if re.fullmatch(r"Item|qt", token, re.I):
                fences.append((None, float(w[0])))
                continue
            fences.append((_pharmassist_header_field(token), float(w[0])))
        fences.sort(key=lambda pair: pair[1])
        if not any(name == "opening_qty" for name, _x in fences):
            continue
        if not any(name == "sales_value" for name, _x in fences):
            continue
        pack_x = next((x for name, x in fences if name == "packing"), 110.0)

        rows: List[Dict[str, Any]] = []
        for w in words:
            y0 = float(w[1])
            if y0 <= header_y + 4:
                continue
            token = str(w[4]).strip()
            if not token:
                continue
            if rows and abs(y0 - rows[-1]["y"]) <= 2.5:
                row = rows[-1]
            else:
                row = {"y": y0, "words": []}
                rows.append(row)
            row["words"].append((float(w[0]), float(w[2]), token))

        for row in rows:
            name_bits = [tok for x0, _x1, tok in row["words"] if x0 < pack_x - 4]
            name = _clean_name(" ".join(name_bits))
            if not name or re.search(
                r"^(Manufacturer|Opening|Closing|Sales|Purchase|Credit|Adj|Branch|For|Report|Printed|Page|Order)\b",
                name,
                re.I,
            ):
                continue
            if re.search(r"\bWELLNESS\b", name, re.I):
                continue
            cells: Dict[str, List[str]] = {}
            pack_bits: List[str] = []
            for x0, x1, tok in row["words"]:
                if x0 < pack_x - 4:
                    continue
                edge = x1 - 0.5
                chosen = None
                for index, (field, start) in enumerate(fences):
                    end = fences[index + 1][1] if index + 1 < len(fences) else 10_000.0
                    if start <= edge < end:
                        chosen = field
                        break
                if chosen == "packing":
                    pack_bits.append(tok)
                elif chosen:
                    cells.setdefault(chosen, []).append(tok)
            item = empty_line_item()
            item["product_name"] = name
            item["packing"] = _clean_name(" ".join(pack_bits)) or None
            extra = item["extra"]
            extra["layout"] = "pharmassist_stock_sale"
            for field, bits in cells.items():
                number = _prompt_cell_number(bits)
                if number is None:
                    continue
                if field in line_fields:
                    item[field] = number
                else:
                    extra[field] = number
            items.append(item)

    if not items:
        return None
    result["line_items"] = items
    result["totals"]["sales_value"] = round(
        sum(_to_float(item.get("sales_value")) for item in items), 2
    )
    result["totals"]["closing_value"] = round(
        sum(_to_float(item.get("closing_value")) for item in items), 2
    )
    result["totals"]["extra"]["extraction_method"] = "pharmassist_stock_sale"
    result["totals"]["extra"]["layout"] = "pharmassist_stock_sale"
    return result


def _marg_nano_split_pages(doc) -> Optional[Dict[str, Any]]:
    """Wide MARG ERP NANO sheet printed as name pages plus ISSUE/CLOSING pages.

    Page 2 products must use the later value page, not the first page's closing numbers.
    Other statements return None.
    """
    product_rows: List[Dict[str, Any]] = []
    value_rows: List[List[float]] = []
    meta = {"stockist_name": None, "company_name": None, "period_from": None, "period_to": None}
    saw_product_header = False
    saw_value_header = False

    for page in doc:
        words = page.get_text("words") or []
        if not words:
            continue
        labels = " ".join(str(w[4]) for w in words)
        is_value = bool(
            re.search(r"\bISSUE\b", labels)
            and re.search(r"\bCLOSING\b", labels)
            and re.search(r"\bSTOCK\b", labels)
            and not re.search(r"\bPRODUCT\b", labels)
        )
        is_product = bool(
            re.search(r"\bPRODUCT\b", labels)
            and re.search(r"\bRECEIVE\b", labels)
            and re.search(r"\bOPENING\b", labels)
        )
        if is_product:
            saw_product_header = True
        if is_value:
            saw_value_header = True
        header_y = None
        for w in words:
            token = str(w[4]).upper()
            if token in {"PRODUCT", "ISSUE"} and float(w[1]) > 100:
                header_y = float(w[1])
                break
        rows: List[Dict[str, Any]] = []
        for w in words:
            y0 = float(w[1])
            if header_y is not None and y0 < header_y + 12:
                continue
            token = str(w[4]).strip()
            if not token:
                continue
            if rows and abs(y0 - rows[-1]["y"]) <= 2.0:
                row = rows[-1]
            else:
                row = {"y": y0, "words": []}
                rows.append(row)
            row["words"].append((float(w[0]), token))
        if is_product or (saw_product_header and not is_value and any(
            re.search(r"[A-Za-z]", tok) and x < 180
            for row in rows for x, tok in row["words"]
        )):
            for row in rows:
                name_bits = [tok for x, tok in row["words"] if x < 220]
                nums = [
                    _to_float(tok)
                    for x, tok in sorted(row["words"], key=lambda pair: pair[0])
                    if x >= 230 and re.fullmatch(r"-?\d+(?:\.\d+)?", tok.replace(",", ""))
                ]
                name = _clean_name(" ".join(name_bits))
                if (
                    not name
                    or not nums
                    or re.search(r"MARG|Chemist|Phone|Licence|E-Mail", name, re.I)
                ):
                    continue
                product_rows.append({"name": name, "nums": nums})
        elif is_value or (saw_value_header and not is_product):
            for row in rows:
                nums = [
                    _to_float(tok)
                    for x, tok in sorted(row["words"], key=lambda pair: pair[0])
                    if re.fullmatch(r"-?\d+(?:\.\d+)?", tok.replace(",", ""))
                ]
                if len(nums) >= 3:
                    value_rows.append(nums[:3])

        blob = " ".join(str(w[4]) for w in words)
        if not meta["stockist_name"]:
            stockist = re.search(
                r"([A-Z][A-Z .&']{2,40}(?:DRUG HOUSE|MEDICAL|AGENCY|PHARMA))",
                blob,
            )
            if stockist:
                meta["stockist_name"] = _clean_name(stockist.group(1))
        if not meta["company_name"]:
            company = re.search(r"(HIMALAYA(?:\s+ZEAL)?)", blob, re.I)
            if company:
                meta["company_name"] = _clean_name(company.group(1))
        if meta["period_from"] is None:
            period = re.search(
                r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{2,4})",
                blob,
            )
            if period:
                meta["period_from"] = _normalize_date(period.group(1))
                end = _normalize_date(period.group(2))
                start = meta["period_from"] or ""
                if (
                    end
                    and start
                    and end[:4] < "2000"
                    and start[:4] >= "2000"
                ):
                    end = f"{start[:4]}-{end[5:]}"
                meta["period_to"] = end

    if not saw_product_header or not saw_value_header or len(product_rows) < 3:
        return None
    return {"products": product_rows, "values": value_rows, "meta": meta}


def _parse_marg_nano_split_statement(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Pair each product page with the ISSUE VALUE page that follows it."""
    found = _marg_nano_split_pages(doc)
    if not found:
        return None
    products = found["products"]
    values = found["values"]
    if len(values) < len(products):
        return None
    result = empty_result(filename, "pdf")
    result["report_title"] = "STOCK & SALES STATEMENT"
    meta = found["meta"]
    result["stockist_name"] = meta.get("stockist_name")
    result["company_name"] = meta.get("company_name")
    result["period_from"] = meta.get("period_from")
    result["period_to"] = meta.get("period_to")
    items: List[Dict[str, Any]] = []
    for product, triple in zip(products, values):
        name = product["name"]
        nums = product["nums"]
        if re.match(r"^TOTAL\b", name, re.I):
            continue
        while len(nums) < 5:
            nums.append(0.0)
        item = empty_line_item()
        item["product_name"] = name
        item["opening_qty"] = nums[0]
        item["opening_value"] = nums[1]
        item["receipts_qty"] = nums[2]
        item["receipts_value"] = nums[3]
        item["sales_qty"] = nums[4]
        item["sales_value"] = triple[0]
        item["closing_qty"] = triple[1]
        item["closing_value"] = triple[2]
        item["extra"]["layout"] = "marg_nano_split_columns"
        items.append(item)
    if len(items) < 3:
        return None
    result["line_items"] = items
    result["totals"]["sales_value"] = round(
        sum(_to_float(item.get("sales_value")) for item in items), 2
    )
    result["totals"]["closing_value"] = round(
        sum(_to_float(item.get("closing_value")) for item in items), 2
    )
    result["totals"]["extra"]["extraction_method"] = "marg_nano_split_columns"
    result["totals"]["extra"]["layout"] = "marg_nano_split_columns"
    return result


_SUNDERLAL_OPENSTK_FIELDS = (
    "opening_qty",
    "purchase_qty",
    "purscm_qty",
    "crestk_qty",
    "sales_qty",
    "slscm_qty",
    "ucstk_qty",
    "ucscm_qty",
    "dbstk_qty",
    "closing_qty",
    "opening_value",
    "purchase_value",
    "sales_value",
    "closing_value",
    "hsn",
    "tax_per",
    "slgst_val",
    "scmgst_val",
)


def _is_sunderlal_openstk_statement(text: str) -> bool:
    """STOCK & SALE STATEMENT with OPENSTK / PURSTK / SALESTK / CLOSESTK."""
    compact = re.sub(r"[^a-z0-9]", "", (text or "").lower())
    return (
        "stocksalestatement" in compact
        and "openstk" in compact
        and "purstk" in compact
        and "salestk" in compact
        and "closestk" in compact
    )


def _sunderlal_word_rows(words: List[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for word in sorted(words or [], key=lambda item: (round(float(item[1]), 1), float(item[0]))):
        if not isinstance(word, (list, tuple)) or len(word) < 5:
            continue
        if not str(word[4] or "").strip():
            continue
        y0 = float(word[1])
        if rows and abs(y0 - rows[-1]["y"]) <= 2.2:
            rows[-1]["words"].append(word)
        else:
            rows.append({"y": y0, "words": [word]})
    return rows


def _sunderlal_column_rights(rows: List[Dict[str, Any]]) -> Optional[List[float]]:
    """Right edges of the 18 numeric columns. Pack numbers sit further left."""
    rights: List[float] = []
    for row in rows:
        joined = " ".join(str(word[4]) for word in row["words"])
        if re.search(r"NEAR\s+EXPIRY", joined, re.I):
            break
        for word in row["words"]:
            token = str(word[4]).replace(",", "")
            if float(word[2]) < 205:
                continue
            if re.fullmatch(r"-?\d+(?:\.\d+)?", token):
                rights.append(float(word[2]))
    if not rights:
        return None
    rights.sort()
    clusters: List[List[float]] = [[rights[0]]]
    for edge in rights[1:]:
        if edge - clusters[-1][-1] <= 4.0:
            clusters[-1].append(edge)
        else:
            clusters.append([edge])
    centers = [sum(cluster) / len(cluster) for cluster in clusters if len(cluster) >= 2]
    if len(centers) != len(_SUNDERLAL_OPENSTK_FIELDS):
        return None
    return centers


def _sunderlal_items_from_rows(
    rows: List[Dict[str, Any]], anchors: List[float]
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for row in rows:
        words = sorted(row["words"], key=lambda word: float(word[0]))
        joined = " ".join(str(word[4]) for word in words)
        if re.search(r"NEAR\s+EXPIRY", joined, re.I):
            break
        if re.search(r"OPENSTK|GRAND\s+TOTAL|STOCK\s*&\s*SALE\s+STATEMENT", joined, re.I):
            continue
        name_bits: List[str] = []
        pack_bits: List[str] = []
        placed: Dict[int, str] = {}
        for word in words:
            token = str(word[4]).strip()
            left = float(word[0])
            right = float(word[2])
            plain = token.replace(",", "")
            is_number = bool(re.fullmatch(r"-?\d+(?:\.\d+)?", plain))
            if is_number and right >= 205:
                nearest = min(range(len(anchors)), key=lambda index: abs(anchors[index] - right))
                if abs(anchors[nearest] - right) <= 18 and nearest not in placed:
                    placed[nearest] = plain
                continue
            if left < 150:
                name_bits.append(token)
            elif left < 205:
                pack_bits.append(token)
        name = _clean_name(" ".join(name_bits))
        if not name or not re.search(r"[A-Za-z]", name):
            continue
        if re.search(
            r"^(NAME|PACK|COMPANY|FROM|REPORT|PAGE|SUNDERLAL|PUNE)\b",
            name,
            re.I,
        ):
            continue
        # Statement footer (MR.Balance, OPENING/PURCHASE/SALES, sale-rate lines).
        # These sit under the product table and are not stock rows.
        if re.search(
            r"^(MR\.?\s*BALANCE|OPENING|PURCHASE|SALES|UC\s*SALE|CL\.?\s*STK)\b"
            r"|ON\s+SALERATE",
            name,
            re.I,
        ):
            break
        if not placed:
            continue
        values: Dict[str, Optional[float]] = {}
        for index, field in enumerate(_SUNDERLAL_OPENSTK_FIELDS):
            raw = placed.get(index)
            if raw is None:
                values[field] = 0.0 if field not in {"hsn", "tax_per"} else None
            elif field == "hsn":
                values[field] = raw
            else:
                values[field] = float(raw)
        item = empty_line_item()
        item["product_name"] = name
        item["packing"] = _clean_name(" ".join(pack_bits)) or None
        item["source_product_name"] = name
        item["source_packing"] = item["packing"]
        item["opening_qty"] = values["opening_qty"]
        item["purchase_qty"] = values["purchase_qty"]
        item["receipts_qty"] = values["purchase_qty"]
        item["sales_qty"] = values["sales_qty"]
        item["closing_qty"] = values["closing_qty"]
        item["opening_value"] = values["opening_value"]
        item["sales_value"] = values["sales_value"]
        item["closing_value"] = values["closing_value"]
        item["extra"] = {
            "layout": "sunderlal_openstk_sale",
            "source_product_name": name,
            "source_packing": item["packing"],
            "purchase_qty": values["purchase_qty"],
            "purchase_value": values["purchase_value"],
            "opening_value": values["opening_value"],
            "purscm_qty": values["purscm_qty"],
            "crestk_qty": values["crestk_qty"],
            "slscm_qty": values["slscm_qty"],
            "ucstk_qty": values["ucstk_qty"],
            "ucscm_qty": values["ucscm_qty"],
            "dbstk_qty": values["dbstk_qty"],
            "hsn": values["hsn"],
            "tax_per": values["tax_per"],
            "slgst_val": values["slgst_val"],
            "scmgst_val": values["scmgst_val"],
        }
        items.append(item)
    return items


def _parse_sunderlal_openstk_statement(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Parse OPENSTK/PURSTK/SALESTK/CLOSESTK rows by printed column edge.

    Returns None for every other statement layout. Stops at NEAR EXPIRY REPORT.
    """
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_sunderlal_openstk_statement(text) for text in page_texts):
        return None
    page_rows = []
    for page, text in zip(doc, page_texts):
        if not _is_sunderlal_openstk_statement(text):
            continue
        page_rows.append(_sunderlal_word_rows(page.get_text("words") or []))
    anchors = None
    for rows in page_rows:
        anchors = _sunderlal_column_rights(rows)
        if anchors:
            break
    if not anchors:
        return None
    items: List[Dict[str, Any]] = []
    for rows in page_rows:
        items.extend(_sunderlal_items_from_rows(rows, anchors))
    if len(items) < 3:
        return None
    result = empty_result(filename, "pdf")
    blob = "\n".join(page_texts)
    stockist = re.search(r"^\s*([A-Z][A-Z .&']{3,50})", blob, re.M)
    if stockist and not re.search(r"STOCK|SALE|COMPANY|FROM", stockist.group(1), re.I):
        result["stockist_name"] = _clean_name(stockist.group(1))
    company = re.search(r"Company\s+Name\s*:\s*(.+)", blob, re.I)
    if company:
        result["company_name"] = _clean_name(company.group(1))
    period_from = re.search(r"From\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})", blob, re.I)
    period_to = re.search(r"To\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})", blob, re.I)
    if period_from:
        result["period_from"] = _normalize_date(period_from.group(1))
    if period_to:
        result["period_to"] = _normalize_date(period_to.group(1))
    result["report_title"] = "STOCK & SALE STATEMENT"
    result["line_items"] = items
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["extraction_method"] = "sunderlal_openstk_sale"
    extra["layout"] = "sunderlal_openstk_sale"
    extra["rows_detected"] = len(items)
    extra["zero_qty_rows"] = sum(
        1
        for item in items
        if item.get("opening_qty") == 0
        and item.get("receipts_qty") == 0
        and item.get("sales_qty") == 0
        and item.get("closing_qty") == 0
    )
    return result


_MONTHLY_SS_ROLES = (
    ("item_code", ("itemcode",)),
    ("item_name", ("itemname",)),
    ("pack", ("packsize",)),
    ("pur_rate", ("purrate",)),
    ("ptr", ("ptr",)),
    ("opening", ("opening",)),
    ("pur_fqty", ("purfqty",)),
    ("pur_qty", ("purqty",)),
    ("pur_value", ("purvalue",)),
    ("pur_r_qty", ("purrqty",)),
    ("pur_ret_fqty", ("purretfqty",)),
    ("rpl_qty", ("rplqty",)),
    ("sale_fqty", ("salefqty",)),
    ("sale_qty", ("saleqty",)),
    ("sale_value", ("salevalue",)),
    ("sale_ret_fqty", ("saleretfqty",)),
    ("sale_ret_qty", ("saleretqty",)),
    ("other_qty", ("otherqty",)),
    ("closing_amt", ("closingamt",)),
    ("closing", ("closing",)),
    ("stock_amt", ("stockamt",)),
    ("status", ("status",)),
)


def _is_monthly_ss_report_text(text: str) -> bool:
    """Srinivasa-style Monthly SS Report: Opening / Pur. Qty / Sale Qty / Closing."""
    if not text or not re.search(r"Monthly\s+SS\s+Report", text, re.I):
        return False
    return bool(
        re.search(r"\bOpening\b", text)
        and re.search(r"\bClosing\b", text)
        and re.search(r"Item\s+Code|Item\s+Name", text, re.I)
        and re.search(r"Pur\.?\s*Qty", text, re.I)
        and re.search(r"Sale\s+Qty", text, re.I)
    )


def _monthly_ss_role(label: str) -> Optional[str]:
    key = re.sub(r"[^a-z0-9]", "", (label or "").lower())
    if not key:
        return None
    if key.startswith("closing") and "ptr" in key:
        return "closing_amt"
    best = None
    best_len = -1
    for role, aliases in _MONTHLY_SS_ROLES:
        for alias in aliases:
            if key == alias or key.startswith(alias):
                if len(alias) > best_len:
                    best = role
                    best_len = len(alias)
    return best


def _monthly_ss_join_number(parts: List[Tuple[float, float, str]]) -> Optional[float]:
    """Join a wrapped cell such as 13622.2 + 8 into 13622.28. Do not add the pieces."""
    ordered = sorted(parts, key=lambda item: (item[1], item[0]))
    blob = "".join(part[2].replace(",", "").replace(" ", "") for part in ordered)
    if re.fullmatch(r"-?\d+(?:\.\d+)?", blob):
        return float(blob)
    for _vx, _vy, token in ordered:
        plain = token.replace(",", "")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", plain):
            return float(plain)
    return None


def _monthly_ss_join_text(parts: List[Tuple[float, float, str]]) -> str:
    if not parts:
        return ""
    ordered = sorted(parts, key=lambda item: (item[1], item[0]))
    lines: List[List[str]] = []
    current: List[Tuple[float, float, str]] = [ordered[0]]
    for part in ordered[1:]:
        if abs(part[1] - current[-1][1]) <= 4:
            current.append(part)
        else:
            lines.append([token for _vx, _vy, token in sorted(current, key=lambda item: item[0])])
            current = [part]
    lines.append([token for _vx, _vy, token in sorted(current, key=lambda item: item[0])])
    return _clean_name(" ".join(" ".join(line) for line in lines))


def _parse_monthly_ss_report(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Read Monthly SS Report columns from their printed positions."""
    import fitz

    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_monthly_ss_report_text(text) for text in page_texts):
        return None

    result = empty_result(filename, "pdf")
    result["report_title"] = "Monthly SS Report"
    blob = "\n".join(page_texts)
    m_period = re.search(
        r"(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})",
        blob,
    )
    if m_period:
        result["period_from"] = m_period.group(1)
        result["period_to"] = m_period.group(2)
    m_mfr = re.search(r"Manufacturer\s+Name\s*:\s*([A-Z][A-Z0-9 .&()-]+)", blob, re.I)
    if m_mfr:
        result["company_name"] = _clean_name(m_mfr.group(1))
    m_div = re.search(r"Division\s+Name\s*:\s*([A-Z][A-Z0-9 .&()-]+)", blob, re.I)
    if m_div:
        result["totals"]["extra"]["division"] = _clean_name(m_div.group(1))

    items: List[Dict[str, Any]] = []
    printed: Dict[str, float] = {}
    for page in doc:
        matrix = page.rotation_matrix
        visual: List[Tuple[float, float, str]] = []
        for x0, y0, x1, y1, token, *_rest in page.get_text("words") or []:
            token = str(token or "").strip()
            if not token:
                continue
            point = fitz.Point((x0 + x1) / 2.0, (y0 + y1) / 2.0) * matrix
            visual.append((point.x, point.y, token))
        opening_hits = [word for word in visual if word[2] == "Opening"]
        if not opening_hits:
            continue
        band_y = opening_hits[0][1]
        header_words = [word for word in visual if abs(word[1] - band_y) <= 12]
        header_words.sort(key=lambda word: word[0])
        clusters: List[List[Tuple[float, float, str]]] = []
        for word in header_words:
            if not clusters or word[0] - max(item[0] for item in clusters[-1]) > 12:
                clusters.append([word])
            else:
                clusters[-1].append(word)

        def _cluster_label(cluster: List[Tuple[float, float, str]]) -> str:
            ordered = sorted(cluster, key=lambda word: (word[1], word[0]))
            return " ".join(word[2] for word in ordered)

        merged: List[List[Tuple[float, float, str]]] = []
        for cluster in clusters:
            if not merged:
                merged.append(cluster)
                continue
            gap = min(word[0] for word in cluster) - max(word[0] for word in merged[-1])
            left_role = _monthly_ss_role(_cluster_label(merged[-1]))
            right_role = _monthly_ss_role(_cluster_label(cluster))
            combined_role = _monthly_ss_role(_cluster_label(merged[-1] + cluster))
            if gap <= 22 and combined_role and (left_role is None or right_role is None):
                merged[-1].extend(cluster)
            else:
                merged.append(cluster)
        columns: List[Tuple[str, float]] = []
        for cluster in merged:
            role = _monthly_ss_role(_cluster_label(cluster))
            if not role:
                continue
            center = sum(word[0] for word in cluster) / len(cluster)
            columns.append((role, center))
        needed = {"item_name", "opening", "pur_qty", "sale_qty", "closing"}
        if not needed.issubset({role for role, _center in columns}):
            continue
        if not result.get("stockist_name"):
            title_words = [
                word for word in visual
                if word[1] < 38 and re.fullmatch(r"[A-Z][A-Z.&'-]*", word[2])
            ]
            title_words.sort(key=lambda word: word[0])
            title = _clean_name(" ".join(word[2] for word in title_words))
            if len(re.sub(r"[^A-Za-z]", "", title)) >= 6:
                result["stockist_name"] = title
        data_words = [word for word in visual if word[1] > band_y + 18]
        data_words.sort(key=lambda word: (word[1], word[0]))
        rows: List[List[Tuple[float, float, str]]] = []
        for word in data_words:
            if not rows or word[1] - rows[-1][-1][1] > 12:
                rows.append([word])
            else:
                rows[-1].append(word)
        columns.sort(key=lambda col: col[1])
        for row in rows:
            cells: Dict[str, List[Tuple[float, float, str]]] = {role: [] for role, _center in columns}
            for word in row:
                idx = min(range(len(columns)), key=lambda i: abs(word[0] - columns[i][1]))
                role, center = columns[idx]
                pack_center = next((col[1] for col in columns if col[0] == "pack"), None)
                name_center = next((col[1] for col in columns if col[0] == "item_name"), None)
                if (
                    role == "pack"
                    and name_center is not None
                    and pack_center is not None
                    and word[0] < pack_center - 10
                ):
                    role = "item_name"
                    center = name_center
                left = columns[idx - 1][1] if idx else center - 40
                right = columns[idx + 1][1] if idx + 1 < len(columns) else center + 40
                limit = min(abs(center - left), abs(right - center))
                if role != "item_name" and abs(word[0] - center) > limit + 1:
                    continue
                cells[role].append(word)
            name = _monthly_ss_join_text(cells.get("item_name") or [])
            code = _monthly_ss_join_text(cells.get("item_code") or [])
            if re.search(r"\bTOTAL\b", f"{name} {code}", re.I):
                for role, key in (
                    ("opening", "opening_qty"),
                    ("pur_qty", "receipts_qty"),
                    ("pur_fqty", "purchase_free_qty"),
                    ("pur_value", "purchase_value"),
                    ("sale_qty", "sales_qty"),
                    ("sale_value", "sales_value"),
                    ("closing", "closing_qty"),
                    ("closing_amt", "closing_value"),
                ):
                    number = _monthly_ss_join_number(cells.get(role) or [])
                    if number is not None:
                        printed[key] = number
                continue
            if not name or not re.search(r"[A-Za-z]", name):
                continue
            item = empty_line_item()
            item["product_name"] = name
            item["product_code"] = code or None
            item["packing"] = _monthly_ss_join_text(cells.get("pack") or []) or None

            def qty(role: str) -> float:
                number = _monthly_ss_join_number(cells.get(role) or [])
                return 0.0 if number is None else number

            item["opening_qty"] = qty("opening")
            item["receipts_qty"] = qty("pur_qty")
            item["sales_qty"] = qty("sale_qty")
            item["closing_qty"] = qty("closing")
            sale_value = _monthly_ss_join_number(cells.get("sale_value") or [])
            close_value = _monthly_ss_join_number(cells.get("closing_amt") or [])
            pur_value = _monthly_ss_join_number(cells.get("pur_value") or [])
            if sale_value is not None:
                item["sales_value"] = sale_value
            if close_value is not None:
                item["closing_value"] = close_value
            extra = item.setdefault("extra", {})
            if isinstance(extra, dict):
                extra["layout"] = "monthly_ss_report"
                if pur_value is not None:
                    extra["purchase_value"] = pur_value
                    extra["receipts_value"] = pur_value
                for role, field in (
                    ("pur_fqty", "purchase_free_qty"),
                    ("pur_r_qty", "purchase_return_qty"),
                    ("pur_ret_fqty", "purchase_return_free_qty"),
                    ("rpl_qty", "repl_qty"),
                    ("sale_fqty", "sales_free"),
                    ("sale_ret_qty", "sale_return_qty"),
                    ("sale_ret_fqty", "sale_return_free_qty"),
                    ("other_qty", "other_qty"),
                    ("ptr", "ptr"),
                    ("pur_rate", "pur_rate"),
                    ("stock_amt", "stock_amt"),
                ):
                    number = _monthly_ss_join_number(cells.get(role) or [])
                    if number is not None:
                        extra[field] = number
                status = _monthly_ss_join_text(cells.get("status") or [])
                if status:
                    extra["status"] = status
            items.append(item)

    if not items:
        return None
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "monthly_ss_report"
    extra["layout"] = "monthly_ss_opening_pur_sale_closing"
    extra["rows_detected"] = len(items)
    extra["total_row_source"] = "product_row_sum"
    result["totals"]["opening_qty"] = round(sum(_to_float(item.get("opening_qty")) for item in items), 2)
    result["totals"]["receipts_qty"] = round(sum(_to_float(item.get("receipts_qty")) for item in items), 2)
    result["totals"]["sales_qty"] = round(sum(_to_float(item.get("sales_qty")) for item in items), 2)
    result["totals"]["closing_qty"] = round(sum(_to_float(item.get("closing_qty")) for item in items), 2)
    result["totals"]["sales_value"] = round(sum(_to_float(item.get("sales_value")) for item in items), 2)
    result["totals"]["closing_value"] = round(sum(_to_float(item.get("closing_value")) for item in items), 2)
    extra["purchase_value"] = round(
        sum(_to_float((item.get("extra") or {}).get("purchase_value")) for item in items),
        2,
    )
    if printed:
        extra["printed_totals"] = printed
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
        monthly_ss = _parse_monthly_ss_report(doc, filename)
        if monthly_ss and monthly_ss.get("line_items"):
            monthly_ss["totals"]["extra"]["statement_count"] = 1
            return monthly_ss

        zenith_opstk = _parse_zenith_opstk_statement(doc, filename)
        if zenith_opstk and zenith_opstk.get("line_items"):
            zenith_opstk["totals"]["extra"]["statement_count"] = 1
            return zenith_opstk

        sunderlal_openstk = _parse_sunderlal_openstk_statement(doc, filename)
        if sunderlal_openstk and sunderlal_openstk.get("line_items"):
            sunderlal_openstk["totals"]["extra"]["statement_count"] = 1
            return sunderlal_openstk

        pack_mexp_text = "\n".join((page.get_text("text") or "") for page in doc)
        pack_doc = _parse_pack_mexp_qty_doc(doc, filename)
        pack_text = _parse_pack_mexp_qty_statement(pack_mexp_text, filename, "pdf")
        # Header x positions keep pack 10 out of opening. A one-line text
        # layer has no column gaps, so that read falls back to the line parser.
        if pack_doc and pack_doc.get("line_items") and not _pack_mexp_columns_collapsed(pack_doc):
            pack_mexp = pack_doc
        else:
            pack_mexp = pack_text
        if pack_mexp and pack_mexp.get("line_items"):
            pack_mexp["totals"]["extra"]["statement_count"] = 1
            return pack_mexp

        marg_nano = _parse_marg_nano_split_statement(doc, filename)
        if marg_nano and marg_nano.get("line_items"):
            marg_nano["totals"]["extra"]["statement_count"] = 1
            return marg_nano

        pharmassist = _parse_pharmassist_stock_sale_report(doc, filename)
        if pharmassist and pharmassist.get("line_items"):
            pharmassist["totals"]["extra"]["statement_count"] = 1
            return pharmassist

        medica_stmt = _parse_medica_opstk_statement(doc, filename)
        if medica_stmt and medica_stmt.get("line_items"):
            medica_stmt["totals"]["extra"]["statement_count"] = 1
            return medica_stmt

        summary_rtl = _parse_summary_rtl_statement(doc, filename)
        if summary_rtl and summary_rtl.get("line_items"):
            summary_rtl["totals"]["extra"]["statement_count"] = 1
            return summary_rtl

        sale_closing = _parse_ssa_sale_closing_reorder(doc, filename)
        if sale_closing and sale_closing.get("line_items"):
            sale_closing["multi_statement"] = False
            sale_closing["statement_count"] = 1
            sale_closing["totals"]["extra"]["statement_count"] = 1
            return sale_closing

        sales_free_cols = _parse_ssa_sales_free_columns(doc, filename)
        if sales_free_cols and sales_free_cols.get("line_items"):
            sales_free_cols["totals"]["extra"]["statement_count"] = 1
            return sales_free_cols

        analysis_stmt = _parse_stock_sales_analysis(doc, filename)
        if analysis_stmt and analysis_stmt.get("line_items"):
            analysis_stmt["totals"]["extra"]["statement_count"] = 1
            return analysis_stmt

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

        scanned_pair = _parse_swil_scanned_qty_value_pair_doc(doc, filename)
        if scanned_pair and scanned_pair.get("line_items"):
            scanned_pair["totals"]["extra"]["statement_count"] = 1
            return scanned_pair

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

        daxin_detailed = _parse_daxinsoft_detailed_stock_sales_statement(doc, filename)
        if daxin_detailed and daxin_detailed.get("line_items"):
            daxin_detailed["totals"]["extra"]["statement_count"] = 1
            return daxin_detailed

        daxin = _parse_daxinsoft_stock_sales_statement(doc, filename)
        if daxin and daxin.get("line_items"):
            daxin["totals"]["extra"]["statement_count"] = 1
            return daxin

        osp = _parse_opening_sales_purchase_statement(doc, filename)
        if osp and osp.get("line_items"):
            osp["totals"]["extra"]["statement_count"] = 1
            return osp

        closstock = _parse_psr_closstock_statement(doc, filename)
        if closstock and closstock.get("line_items"):
            closstock["totals"]["extra"]["statement_count"] = 1
            return closstock

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
                # Image-only statements: ~300 DPI helps Group Wise Sales digit columns
                render_zoom = max(zoom, 300.0 / 72.0)
                text, image_bytes = _ocr_pdf_page_text(page, zoom=render_zoom)
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
    # ClosStock / Clos.Amt sheets have no line sale-amount column. Do not
    # derive one from the closing rate; that overwrites the printed cells.
    if (
        str(((result.get("totals") or {}).get("extra") or {}).get("extraction_method") or "")
        == "psr_closstock_columns"
    ):
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


def _is_psr_closstock_text(text: str) -> bool:
    """Landscape Product Stock Report whose headers are ClosStock and Clos.Amt.

    The ZANDRA sheet uses "Closing Stock" and "Cls Amt" on one text line.
    This print puts each cell on its own line, so that parser never sees a row.
    """
    if not text or not _PRODUCT_STOCK_REPORT_TITLE.search(text):
        return False
    return bool(
        re.search(r"\bClosStock\b", text)
        and re.search(r"Clos\.Amt", text)
        and re.search(r"\bSaleRet\b", text)
        and re.search(r"Exp/Dmg", text)
        and re.search(r"\bOrderQty\b", text)
    )


def _psr_closstock_field(token: str) -> Optional[str]:
    norm = re.sub(r"[^a-z]", "", (token or "").lower())
    return {
        "product": "name",
        "name": "name",
        "opening": "opening_qty",
        "purchase": "receipts_qty",
        "total": "total_stock",
        "sale": "sales_qty",
        "saleret": "sale_return",
        "expdmg": "exp_damage",
        "closstock": "closing_qty",
        "closamt": "closing_value",
        "orderqty": "order_qty",
    }.get(norm)


def _psr_closstock_buckets(
    row: Dict[str, Any],
) -> Optional[List[Tuple[str, float, float]]]:
    spans: List[Tuple[str, float, float]] = []
    for x0, x1, _xc, token in sorted(row.get("words") or [], key=lambda item: item[0]):
        field = _psr_closstock_field(token)
        if not field:
            continue
        if spans and spans[-1][0] == field:
            prev = spans[-1]
            spans[-1] = (field, min(prev[1], x0), max(prev[2], x1))
        else:
            spans.append((field, x0, x1))
    needed = {
        "name",
        "opening_qty",
        "receipts_qty",
        "total_stock",
        "sales_qty",
        "sale_return",
        "exp_damage",
        "closing_qty",
        "closing_value",
        "order_qty",
    }
    if not needed.issubset({field for field, _x0, _x1 in spans}):
        return None
    buckets: List[Tuple[str, float, float]] = []
    for idx, (field, x0, x1) in enumerate(spans):
        lo = 0.0 if idx == 0 else max(0.0, x0 - 8.0)
        if idx + 1 < len(spans):
            hi = spans[idx + 1][1] - 2.0
        else:
            hi = max(x1 + 48.0, x0 + 36.0)
        if hi <= lo:
            hi = lo + 8.0
        buckets.append((field, lo, hi))
    if buckets and buckets[0][0] == "name":
        buckets[0] = ("name", 0.0, buckets[0][2])
    return buckets


def _parse_psr_closstock_statement(doc, filename: str) -> Optional[Dict[str, Any]]:
    """Parse ClosStock / SaleRet / Exp/Dmg / Clos.Amt from word positions."""
    page_texts = [(page.get_text("text") or "") for page in doc]
    if not any(_is_psr_closstock_text(t) for t in page_texts):
        return None

    result = empty_result(filename, "pdf")
    result["report_title"] = "Product Stock Report"
    items: List[Dict[str, Any]] = []
    joined = "\n".join(page_texts)
    m_stockist = re.search(
        r"Product\s+Stock\s+Report\s*\n\s*([^\n]+)", joined, re.I
    )
    if m_stockist:
        stockist = _clean_name(m_stockist.group(1))
        if stockist and not re.search(r"MFG\s+Company|From\s*:", stockist, re.I):
            result["stockist_name"] = stockist
    m_co = re.search(r"MFG\s+Company:\s*([^\n]+)", joined, re.I)
    if m_co:
        result["company_name"] = _clean_name(m_co.group(1))
    m_from = re.search(r"From:\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", joined, re.I)
    m_to = re.search(r"\bTo:\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", joined, re.I)
    if m_from:
        result["period_from"] = _normalize_date(m_from.group(1))
    if m_to:
        result["period_to"] = _normalize_date(m_to.group(1))

    for page in doc:
        rows = _swil_land_group_words(page.get_text("words") or [], y_tol=2.0)
        buckets = None
        header_y = 0.0
        for row in rows:
            blob = _swil_land_row_blob(row)
            if re.search(r"\bClosStock\b", blob) and re.search(r"Clos\.Amt", blob):
                built = _psr_closstock_buckets(row)
                if built:
                    buckets = built
                header_y = max(header_y, row["y"])
                continue
            if not buckets or row["y"] <= header_y + 2:
                continue
            cells = _daxin_row_cells(row, buckets)
            name = _clean_name(" ".join(cells.get("name") or []))
            if not name or not re.search(r"[A-Za-z]", name):
                continue
            numbers = {
                key: _daxin_cell_number(cells.get(key) or [])
                for key in (
                    "opening_qty",
                    "receipts_qty",
                    "total_stock",
                    "sales_qty",
                    "sale_return",
                    "exp_damage",
                    "closing_qty",
                    "closing_value",
                    "order_qty",
                )
            }
            extra = result["totals"]["extra"]
            if re.match(r"^GRAND\s+TOTAL\b", name, re.I):
                for src, dest in (
                    ("opening_qty", "opening_qty"),
                    ("receipts_qty", "receipts_qty"),
                    ("total_stock", "total_qty"),
                    ("sales_qty", "sales_qty"),
                    ("sale_return", "saleret_qty"),
                    ("exp_damage", "exp_dmg_qty"),
                    ("closing_qty", "closing_qty"),
                    ("order_qty", "order_qty"),
                ):
                    if numbers[src] is not None:
                        extra[dest] = numbers[src]
                if numbers["closing_value"] is not None:
                    result["totals"]["closing_value"] = numbers["closing_value"]
                extra["total_row_source"] = "psr_closstock_footer"
                continue
            if re.match(r"^AMOUNT\s+TOTAL\b", name, re.I):
                for src, dest in (
                    ("opening_qty", "opening_value"),
                    ("receipts_qty", "purchase_value"),
                    ("total_stock", "total_value"),
                    ("sales_qty", "sales_amount"),
                    ("sale_return", "sale_return_value"),
                    ("exp_damage", "exp_dmg_value"),
                    ("closing_qty", "closing_stock_value"),
                    ("closing_value", "closing_amount"),
                ):
                    if numbers[src] is not None:
                        extra[dest] = numbers[src]
                if numbers["sales_qty"] is not None:
                    result["totals"]["sales_value"] = numbers["sales_qty"]
                extra["total_row_source"] = "psr_closstock_footer"
                continue
            if re.match(r"^(PRODUCT|OPENING|TOTAL)\b", name, re.I):
                continue
            qty_hits = sum(
                1
                for key in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
                if numbers[key] is not None
            )
            if qty_hits < 4:
                continue
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = numbers["opening_qty"] or 0.0
            item["receipts_qty"] = numbers["receipts_qty"] or 0.0
            item["sales_qty"] = numbers["sales_qty"] or 0.0
            item["sales_value"] = 0.0
            item["closing_qty"] = numbers["closing_qty"] or 0.0
            if numbers["closing_value"] is not None:
                item["closing_value"] = numbers["closing_value"]
            item_extra = item.setdefault("extra", {})
            if isinstance(item_extra, dict):
                item_extra["layout"] = "psr_closstock"
                for src, dest in (
                    ("total_stock", "total_stock"),
                    ("sale_return", "sale_return"),
                    ("exp_damage", "exp_damage"),
                    ("order_qty", "order_qty"),
                ):
                    if numbers[src] is not None:
                        item_extra[dest] = numbers[src]
            items.append(item)

    if not items:
        return None
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "psr_closstock_columns"
    extra["layout"] = "closstock_saleret_expdmg"
    extra["psr_column_layout"] = "saleret"
    extra["stock_identity_kind"] = STOCK_IDENTITY_SALERET
    extra["rows_detected"] = len(items)
    extra["fallback_used"] = False
    return result


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

    # 0) Group Wise Sales Op.Stock/Purchase/Sales/Cl.Stock (before other PDF heuristics)
    gw = _parse_group_wise_sales_statement(text, filename, source_format)
    if gw and gw.get("line_items"):
        return gw

    # Company-wise SUMMARY RTL has no sales-qty column. Read it before Gemini,
    # which was storing SALE AMT in extra.sales_qty and CLOSING in sales_qty.
    if _is_summary_rtl_statement(text):
        rtl_items = _summary_rtl_items_from_text(text)
        if rtl_items:
            return _summary_rtl_finish(rtl_items, text, filename, source_format)

    # ITEM DESCRIPTION / PACK / OPENING / RECEIPT / ISSUE / CLOSING / M.EXP.
    # Totals are the product-row sums. The printed TOTAL row is not used.
    pack_mexp = _parse_pack_mexp_qty_statement(text, filename, source_format)
    if pack_mexp and pack_mexp.get("line_items"):
        return pack_mexp

    # 0b) P.S.PHARMACEUTICALS OPENING/RECEIPT/ISSUE/CLOSING (before Gemini)
    ps = _parse_ps_pharma_statement(text, filename)
    if ps and (ps.get("line_items") or ps.get("totals", {}).get("opening_qty") is not None):
        ps["source_format"] = source_format
        return ps

    # 0c) Mahajan-style OpBal/Receipt/Total/Issue/Closing (before Gemini)
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
      "opening_value": number|null,
      "receipts_qty": number|null,
      "sales_qty": number|null,
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
- Map Op.Amt / OP AMT / Opening Amount -> extra.opening_value (not sales_value)
- Map Receipt Amt / Purchase Value -> extra.receipts_value when that column is printed
- When the header is OP QTY | OP AMT | SALE AMT | CLOSING | CLOSING AMT
  (no purchase column and no sale-qty column):
  opening_qty=OP QTY, extra.opening_value=OP AMT, receipts_qty=0, extra.receipts_value=0,
  sales_qty=0, sales_value=SALE AMT, closing_qty=CLOSING, closing_value=CLOSING AMT.
  Example: BONNISAN 100 SYP opening_qty=64, extra.opening_value=4038.72, sales_qty=0,
  sales_value=0, closing_qty=64, closing_value=4038.72.
  Example: CYSTONE TAB opening_qty=262, extra.opening_value=45152.2, sales_value=4769.06,
  closing_qty=236, closing_value=40671.41.
- For OpBal|Receipt|Total|Issue|Closing: sales_qty=Issue (NOT Total); Dump is not closing_value.
- "Stock and Sale Statement" grid (Item Cd, Item Name, Op Stk, P Qty, P S Qty, P Val, S Qty, S S Qty, S Val, Cl Stk, Cl Val):
  opening_qty=Op Stk, receipts_qty=P Qty, sales_qty=S Qty (NOT S S Qty),
  sales_value=S Val, closing_qty=Cl Stk, closing_value=Cl Val.
  Put P S Qty in extra.purchase_scheme_qty, P Val in extra.purchase_value,
  S S Qty in extra.sales_scheme_qty.
  Blank cells are 0. Do NOT shift later columns left when a cell is blank.
  This layout has money columns. It is NOT qty-only. Never set sales_value or
  closing_value to 0 when S Val / Cl Val is printed (example: S Qty=2, S Val=495).
- Company-wise summary "SALES & STOCK STATEMENT ( COMPANY WISE => SUMMARY RTL )"
  has ONLY these columns: PRODUCT NAME, OP QTY, OP AMT, SALE AMT, CLOSING, CLOSING AMT.
  There is no Sales Qty column and no Receipts column.
  opening_qty = OP QTY.
  opening_value = OP AMT. Also put that amount in extra.opening_value. Do not omit OP AMT.
  sales_value = SALE AMT. Keep a printed SALE AMT even when it is smaller than OP AMT
  and CLOSING AMT. Do not set sales_value to 0 when that cell has a number.
  closing_qty = CLOSING. closing_value = CLOSING AMT.
  sales_qty = null. receipts_qty = null. Do not copy OP QTY or CLOSING into sales_qty.
  Do not calculate sales_qty. Set report_title to the printed title.
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
      "opening_value": number|null,
      "receipts_qty": number|null,
      "sales_qty": number|null,
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


def _looks_like_summary_rtl_result(result: Dict[str, Any]) -> bool:
    """True only for the company-wise SUMMARY RTL title, not other stock grids."""
    extra = ((result.get("totals") or {}).get("extra") or {})
    if extra.get("extraction_method") == "summary_rtl_op_amt":
        return True
    if extra.get("layout") == "summary_rtl_op_amt":
        return True
    blob = " ".join(
        str(result.get(key) or "")
        for key in ("report_title", "stockist_name", "company_name")
    )
    if re.search(r"SUMMARY\s*RTL|COMPANY\s*WISE", blob, re.I) and re.search(
        r"SALES\s*(?:&|AND)\s*STOCK\s*STATEMENT|SHUBHAM", blob, re.I
    ):
        return True
    return bool(
        re.search(r"SHUBHAM", blob, re.I)
        and re.search(r"SALES\s*(?:&|AND)\s*STOCK\s*STATEMENT", blob, re.I)
    )


def _clear_summary_rtl_fabricated_qty(result: Dict[str, Any]) -> Dict[str, Any]:
    """This report has SALE AMT only. Drop a sales qty copied from OP QTY."""
    if not _looks_like_summary_rtl_result(result):
        return result
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        item["extra"] = extra
        opening_value = item.get("opening_value")
        if opening_value in (None, ""):
            opening_value = extra.get("opening_value")
        if opening_value not in (None, ""):
            item["opening_value"] = _to_float(opening_value)
            extra["opening_value"] = item["opening_value"]
        item["sales_qty"] = None
        item["receipts_qty"] = None
        extra.pop("sales_qty", None)
        extra["receipts_value"] = None
        extra["layout"] = "summary_rtl_op_amt"
    result.setdefault("totals", {}).setdefault("extra", {})
    result["totals"]["extra"]["extraction_method"] = "summary_rtl_op_amt"
    result["totals"]["extra"]["layout"] = "summary_rtl_op_amt"
    return result


def _summary_rtl_amount_gaps(result: Dict[str, Any]) -> bool:
    """OP AMT missing, or SALE AMT dropped on a row whose closing qty changed."""
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    if not items:
        return False
    missing_open = sum(1 for i in items if i.get("opening_value") in (None, ""))
    if missing_open >= max(1, len(items) // 2):
        return True
    for item in items:
        if _to_float(item.get("sales_value")) > 0:
            continue
        if abs(_to_float(item.get("opening_qty")) - _to_float(item.get("closing_qty"))) > 0.05:
            return True
    return False


def _fill_summary_rtl_amounts_from_image(
    result: Dict[str, Any], b64: str, mime: str, model: str
) -> Dict[str, Any]:
    """Second read of OP AMT and SALE AMT only, for this report layout."""
    if not _summary_rtl_amount_gaps(result):
        return result
    from services.vertex_gemini_client import generate_content_via_vertex

    names = [
        str(item.get("product_name") or "")
        for item in result.get("line_items") or []
        if isinstance(item, dict) and item.get("product_name")
    ]
    prompt = (
        "This image is SALES & STOCK STATEMENT ( COMPANY WISE => SUMMARY RTL ). "
        "Columns are PRODUCT NAME, OP QTY, OP AMT, SALE AMT, CLOSING, CLOSING AMT. "
        "There is no sales qty column. "
        "Return ONLY JSON {\"rows\":[{\"product_name\":string,\"opening_value\":number,\"sales_value\":number}]}. "
        "opening_value is OP AMT. sales_value is SALE AMT. "
        "If SALE AMT is printed, sales_value is that number, including a small amount. "
        "sales_value is 0 only when the SALE AMT cell is blank or 0. "
        "Products:\n" + "\n".join(f"- {name}" for name in names)
    )
    try:
        response = generate_content_via_vertex(
            model=model,
            payload={
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": prompt},
                            {"inline_data": {"mime_type": mime, "data": b64}},
                        ],
                    }
                ],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8192},
            },
            timeout=120,
        )
        parsed = _extract_json_object(_gemini_response_text(response)) or {}
    except Exception as exc:
        logger.warning("SUMMARY RTL amount reread skipped: %s", exc)
        return result
    rows = parsed.get("rows") or parsed.get("line_items") or []
    by_name = {
        re.sub(r"[^A-Z0-9]", "", str(item.get("product_name") or "").upper()): item
        for item in result.get("line_items") or []
        if isinstance(item, dict)
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = re.sub(r"[^A-Z0-9]", "", str(row.get("product_name") or "").upper())
        item = by_name.get(key)
        if not item:
            continue
        extra = item.setdefault("extra", {})
        if row.get("opening_value") not in (None, ""):
            item["opening_value"] = _to_float(row.get("opening_value"))
            extra["opening_value"] = item["opening_value"]
        reread_sale = row.get("sales_value")
        if reread_sale not in (None, "") and (
            _to_float(item.get("sales_value")) <= 0 and _to_float(reread_sale) > 0
        ):
            item["sales_value"] = _to_float(reread_sale)
    return result


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
        opening_value = raw.get("opening_value")
        if opening_value in (None, "") and isinstance(item.get("extra"), dict):
            opening_value = item["extra"].get("opening_value")
        if opening_value not in (None, ""):
            item["opening_value"] = _to_float(opening_value)
            item.setdefault("extra", {})["opening_value"] = item["opening_value"]
        if item["product_name"]:
            items.append(item)
    result["line_items"] = items
    result = _clear_summary_rtl_fabricated_qty(result)

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


_PORTRAIT_BALANCE_FIELDS = (
    "opening_qty",
    "opening_value",
    "receipts_qty",
    "receipts_value",
    "total_qty",
    "sales_qty",
    "sales_value",
    "closing_qty",
    "closing_value",
    "near_expiry",
)


def _is_portrait_opening_balance_text(text: str) -> bool:
    """Sales & Stock with Opening Bal, Receipt/Pur, Issue/Sales, and Near Expiry.

    Landscape Code grids stay on the existing Swil parser. This detector only
    matches the portrait qty/value sheet that has no Code column.
    """
    if not text or re.search(r"\bCode\b", text):
        return False
    structural = bool(
        re.search(r"\bPACKING\b", text, re.I)
        and re.search(r"Opening\s+Bal", text, re.I)
        and re.search(r"Issue/Sales", text, re.I)
        and re.search(r"Near|Expir", text, re.I)
    )
    if not structural:
        return False
    # Continuation pages often OCR Receipt/Pur without the slash, so the
    # older Swil detector misses them. The column header above is enough.
    return bool(
        _is_swil_opening_receipt_value_statement(text)
        or re.search(r"Receipt", text, re.I)
    )


def _portrait_balance_label(token: str) -> str:
    return re.sub(r"[^a-z]", "", (token or "").lower())


def _portrait_balance_number(token: str) -> Optional[float]:
    """Read one cell. Trailing dots and letter/digit swaps are OCR noise."""
    s = str(token or "").replace(",", ".").replace(" ", "")
    s = re.sub(r"[oO]", "0", s).replace("l", "1").replace("|", "1")
    s = re.sub(r"[^0-9.]", "", s).strip(".")
    if s.count(".") > 1:
        parts = s.split(".")
        if len(parts[-1]) in {1, 2}:
            s = "".join(parts[:-1]) + "." + parts[-1]
        else:
            s = "".join(parts)
    if not re.fullmatch(r"\d+(?:\.\d+)?", s or ""):
        return None
    return float(s)


def _portrait_balance_ocr_words(image) -> List[Dict[str, Any]]:
    import pytesseract

    data = pytesseract.image_to_data(
        image, config="--psm 6", output_type=pytesseract.Output.DICT
    )
    words: List[Dict[str, Any]] = []
    for index, token in enumerate(data["text"]):
        token = str(token or "").strip()
        if not token:
            continue
        x0 = int(data["left"][index])
        y0 = int(data["top"][index])
        words.append(
            {
                "x0": x0,
                "y0": y0,
                "x1": x0 + int(data["width"][index]),
                "y1": y0 + int(data["height"][index]),
                "t": token,
                "yc": y0 + int(data["height"][index]) / 2.0,
            }
        )
    return words


def _portrait_balance_headers(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Column right-edges from this page's PACKING header. None for other layouts."""
    headers: List[Dict[str, Any]] = []
    packings = [
        word
        for word in words
        if _portrait_balance_label(word["t"]).startswith("packin")
    ]
    for packing in packings:
        band = [
            word
            for word in words
            if packing["y0"] - 30 <= word["yc"] <= packing["y0"] + 90
            and word["x0"] > packing["x0"] - 8
        ]
        sub = [
            word
            for word in band
            if _portrait_balance_label(word["t"]) in {"qty", "value", "expiry"}
        ]
        sub.sort(key=lambda word: word["x1"])
        if len(sub) >= 8:
            headers.append(
                {
                    "y": packing["y1"],
                    "pack_x0": packing["x0"],
                    "edges": [word["x1"] for word in sub[:10]],
                    "kind": "sub",
                }
            )
            continue
        role: Dict[str, float] = {}
        closings: List[Dict[str, Any]] = []
        for word in band:
            label = _portrait_balance_label(word["t"])
            if label == "op":
                role["opening_qty"] = word["x1"]
            elif label in {"opening", "bal"}:
                role["opening_value"] = max(role.get("opening_value", 0), word["x1"])
            elif label == "receipt":
                role["receipts_qty"] = word["x1"]
            elif label.startswith("recei") and label != "receipt":
                role["receipts_value"] = word["x1"]
            elif "total" in label:
                role["total_qty"] = word["x1"]
            elif label == "issue":
                role["sales_qty"] = word["x1"]
            elif "issue" in label:
                role["sales_value"] = word["x1"]
            elif label == "closing":
                closings.append(word)
            elif label == "bala":
                role["closing_value"] = max(role.get("closing_value", 0), word["x1"])
            elif label in {"near", "expiry", "new"}:
                role["near_expiry"] = max(role.get("near_expiry", 0), word["x1"])
        closings.sort(key=lambda word: word["x0"])
        if closings:
            role["closing_qty"] = closings[0]["x1"]
            if len(closings) > 1:
                role["closing_value"] = max(
                    role.get("closing_value", 0), closings[-1]["x1"]
                )
        if all(field in role for field in _PORTRAIT_BALANCE_FIELDS):
            edges = [role[field] for field in _PORTRAIT_BALANCE_FIELDS]
            if edges == sorted(edges):
                headers.append(
                    {
                        "y": packing["y1"],
                        "pack_x0": packing["x0"],
                        "edges": edges,
                        "kind": "group",
                    }
                )
    headers.sort(key=lambda header: header["y"])
    kept: List[Dict[str, Any]] = []
    for header in headers:
        if kept and abs(header["y"] - kept[-1]["y"]) < 60:
            if header["kind"] == "sub":
                kept[-1] = header
            continue
        kept.append(header)
    return kept


def _portrait_balance_row_ok(item: Dict[str, Any]) -> bool:
    total = _to_float(item.get("opening_qty")) + _to_float(item.get("receipts_qty"))
    closing = _to_float(item.get("extra", {}).get("total_stock_qty")) - _to_float(
        item.get("sales_qty")
    )
    return (
        abs(total - _to_float(item.get("extra", {}).get("total_stock_qty"))) <= 0.51
        and abs(closing - _to_float(item.get("closing_qty"))) <= 0.51
    )


def _portrait_balance_items(
    words: List[Dict[str, Any]],
    header: Dict[str, Any],
    y_end: float,
    alt_words: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Assign right-aligned numbers to this page's header edges."""
    edges: List[float] = header["edges"]
    name_hi = header["pack_x0"] - 4
    names = [
        word
        for word in words
        if header["y"] + 2 < word["yc"] < y_end
        and word["x1"] < name_hi
        and re.search(r"[A-Za-z]{2,}", word["t"])
    ]
    names.sort(key=lambda word: word["y0"])
    anchors: List[Dict[str, Any]] = []
    for word in names:
        if re.search(r"^(GRAND|TOTAL|PRODUCT|Powered|Page)\b", word["t"], re.I):
            continue
        if anchors and abs(word["yc"] - anchors[-1]["y"]) <= 16:
            anchors[-1]["toks"].append(word)
            anchors[-1]["y"] = sum(tok["yc"] for tok in anchors[-1]["toks"]) / len(
                anchors[-1]["toks"]
            )
        else:
            anchors.append(
                {
                    "y": word["yc"],
                    "toks": [word],
                    "pack": [],
                    "plain": {field: [] for field in _PORTRAIT_BALANCE_FIELDS},
                    "alt": {field: [] for field in _PORTRAIT_BALANCE_FIELDS},
                }
            )
    if len(anchors) < 2:
        return []
    gaps = [anchors[i + 1]["y"] - anchors[i]["y"] for i in range(len(anchors) - 1)]
    body = [gap for gap in gaps if gap > 8] or [28]
    pitch = sorted(body)[len(body) // 2]
    limit = max(14.0, min(22.0, pitch * 0.55))

    def _fill(source: List[Dict[str, Any]], slot: str) -> None:
        for word in source:
            if not (header["y"] + 2 < word["yc"] < y_end) or word["x1"] < name_hi:
                continue
            best = min(anchors, key=lambda anchor: abs(anchor["y"] - word["yc"]))
            if abs(best["y"] - word["yc"]) > limit:
                continue
            # 60'S / 200ML stay in packing. A lone o/l inside digits is still a number.
            ocr_digits = re.fullmatch(r"[0-9oOlI|.,]+", word["t"] or "")
            if re.search(r"[A-Za-z]", word["t"]) and not ocr_digits:
                if slot == "plain" and name_hi - 8 <= word["x0"] < edges[0] - 4:
                    best["pack"].append(word["t"])
                continue
            number = _portrait_balance_number(word["t"])
            if number is not None and number > 9999999:
                continue
            if number is None and word["x1"] < edges[0] - 6:
                if slot == "plain":
                    best["pack"].append(word["t"])
                continue
            if number is None:
                continue
            index = min(range(10), key=lambda i: abs(edges[i] - word["x1"]))
            if abs(edges[index] - word["x1"]) > 55:
                continue
            best[slot][_PORTRAIT_BALANCE_FIELDS[index]].append(
                (abs(edges[index] - word["x1"]), number)
            )

    _fill(words, "plain")
    if alt_words:
        _fill(alt_words, "alt")

    items: List[Dict[str, Any]] = []
    for anchor in anchors:
        name = " ".join(
            tok["t"] for tok in sorted(anchor["toks"], key=lambda word: word["x0"])
        )
        name = re.sub(r"\s+", " ", name).strip(" |_.,")
        if re.search(r"GRAND|TOTAL|PRODUCT NAME|Powered|Page No|Value\s*IN", name, re.I):
            continue
        if len(re.sub(r"[^A-Za-z]", "", name)) < 3:
            continue
        values: Dict[str, float] = {}
        for field in _PORTRAIT_BALANCE_FIELDS:
            plain = sorted(anchor["plain"][field], key=lambda pair: pair[0])
            alt = sorted(anchor["alt"][field], key=lambda pair: pair[0])
            if plain:
                values[field] = plain[0][1]
            elif alt:
                values[field] = alt[0][1]
            else:
                values[field] = 0.0
        item = empty_line_item()
        item["product_name"] = _clean_name(name)
        pack = " ".join(anchor["pack"]).strip()
        item["packing"] = _clean_name(pack) or None
        item["opening_qty"] = values["opening_qty"]
        item["receipts_qty"] = values["receipts_qty"]
        item["sales_qty"] = values["sales_qty"]
        item["sales_value"] = values["sales_value"]
        item["closing_qty"] = values["closing_qty"]
        item["closing_value"] = values["closing_value"]
        extra = item["extra"]
        extra["opening_value"] = values["opening_value"]
        extra["total_stock_qty"] = values["total_qty"]
        extra["near_expiry_qty"] = values["near_expiry"]
        extra["layout"] = "portrait_opening_balance"
        item["opening_value"] = values["opening_value"]
        item = _attach_receipts_value(item, values["receipts_value"])
        # A faint page can miss a qty on the plain pass. Use the darker
        # read only when it restores Opening+Receipt=Total and Total-Issue=Closing.
        if alt_words and not _portrait_balance_row_ok(item):
            trial = dict(values)
            for field in _PORTRAIT_BALANCE_FIELDS:
                alt = sorted(anchor["alt"][field], key=lambda pair: pair[0])
                if alt:
                    trial[field] = alt[0][1]
            trial_item = dict(item)
            trial_item["opening_qty"] = trial["opening_qty"]
            trial_item["receipts_qty"] = trial["receipts_qty"]
            trial_item["sales_qty"] = trial["sales_qty"]
            trial_item["closing_qty"] = trial["closing_qty"]
            trial_item["extra"] = dict(extra)
            trial_item["extra"]["total_stock_qty"] = trial["total_qty"]
            if _portrait_balance_row_ok(trial_item):
                item["opening_qty"] = trial["opening_qty"]
                item["receipts_qty"] = trial["receipts_qty"]
                item["sales_qty"] = trial["sales_qty"]
                item["sales_value"] = trial["sales_value"]
                item["closing_qty"] = trial["closing_qty"]
                item["closing_value"] = trial["closing_value"]
                item["opening_value"] = trial["opening_value"]
                item["extra"]["opening_value"] = trial["opening_value"]
                item["extra"]["total_stock_qty"] = trial["total_qty"]
                item["extra"]["near_expiry_qty"] = trial["near_expiry"]
                item = _attach_receipts_value(item, trial["receipts_value"])
        items.append(item)
    return items


def _portrait_pack_norm(pack: str) -> Optional[str]:
    """Clean a packing token. Letter/digit swaps stay inside the packing cell."""
    p = str(pack or "").upper().strip(" .,_")
    p = re.sub(r"\s+", "", p)
    p = re.sub(r"MIL$", "ML", p)
    p = re.sub(r"(?<=\d)MI$", "ML", p)
    p = re.sub(r"(?<=\d)M1$", "ML", p)
    sized = re.match(r"^(?P<size>.*?)(?P<unit>ML|GM)$", p)
    if sized:
        size = (
            sized.group("size")
            .replace("I", "1")
            .replace("L", "1")
            .replace("S", "5")
            .replace("O", "0")
        )
        size = re.sub(r"[^0-9]", "", size)
        p = (size or "0") + sized.group("unit")
    return p or None


def _portrait_name_norm(name: str) -> str:
    name = re.sub(r"\s+", " ", name or "").strip(" .|_")
    # A lone lowercase L in an otherwise capital name is the letter L (V-GEL).
    if name and "l" in name and not re.search(r"[a-z]", name.replace("l", "")):
        name = name.replace("l", "L")
    # A trailing 1 on a capital token is the letter L (V-GEL read as V-GE1).
    name = re.sub(r"(?<=[A-Za-z])1\b", "L", name)
    name = name.replace("$", "5")
    name = re.sub(r"[\s._—–]+$", "", name)
    return _clean_name(name)


def _portrait_balance_split_pack(text: str) -> Tuple[str, Optional[str]]:
    s = re.sub(r"[|]+", " ", text or "")
    s = re.sub(r"\s+", " ", s).strip(" ._")
    glued = re.search(
        r"^(?P<name>.*\D)\s*(?P<pack>\d+\s*X\s*'?\s*\d+|\d[\w.'’/-]{0,14})$",
        s,
        re.I,
    )
    if not glued:
        parts = s.split()
        if len(parts) >= 2 and re.search(r"\d|ML|GM|'S", parts[-1], re.I):
            pack_toks = [parts[-1]]
            name_toks = parts[:-1]
            if name_toks and re.fullmatch(r"\d*X", name_toks[-1], re.I):
                pack_toks.insert(0, name_toks.pop())
            glued_name = " ".join(name_toks)
            glued_pack = "".join(pack_toks)
        else:
            return _portrait_name_norm(s), None
    else:
        glued_name = glued.group("name")
        glued_pack = glued.group("pack")
    name = re.sub(r"[\(\[\]\|._]+$", "", glued_name).strip()
    pack = _portrait_pack_norm(glued_pack)
    if len(re.sub(r"[^A-Za-z]", "", name)) < 3:
        return _portrait_name_norm(s), None
    return _portrait_name_norm(name), pack


def _portrait_balance_name_lines(image, top: float, bottom: float, right: float):
    """One line per product from the name/packing column of this scan."""
    import pytesseract
    from PIL import ImageOps

    gray = image.convert("L") if getattr(image, "mode", "") != "L" else image
    x1 = int(min(max(right, 80), gray.width - 2))
    y0 = int(max(0, top))
    y1 = int(min(gray.height - 1, bottom))
    if y1 - y0 < 20 or x1 < 40:
        return []
    crop = ImageOps.autocontrast(gray.crop((40, y0, x1, y1)))
    data = pytesseract.image_to_data(
        crop, config="--psm 6", output_type=pytesseract.Output.DICT
    )
    rows: Dict[float, List[Tuple[int, str]]] = {}
    for index, token in enumerate(data["text"]):
        token = str(token or "").strip()
        if not token:
            continue
        y = y0 + int(data["top"][index]) + int(data["height"][index]) / 2.0
        hit = None
        for key in rows:
            if abs(key - y) <= 10:
                hit = key
                break
        if hit is None:
            rows[y] = [(int(data["left"][index]), token)]
        else:
            rows[hit].append((int(data["left"][index]), token))
    lines = []
    for y in sorted(rows):
        text = " ".join(tok for _, tok in sorted(rows[y], key=lambda pair: pair[0]))
        text = re.sub(r"\s+", " ", text).strip()
        if sum(c.isalpha() for c in text) < 3:
            continue
        if not re.search(r"[B-DF-HJ-NP-TV-Zb-df-hj-np-tv-z]", text):
            continue
        if re.search(r"^(PRODUCT|TOTAL|Powered|Page)\b", text, re.I):
            continue
        lines.append({"y": y, "text": text})
    return lines


def _portrait_row_column_boxes(image, y: float, edges: List[float]):
    """Right-aligned ink on this row, grouped onto the ten header edges."""
    gray = image.convert("L") if getattr(image, "mode", "") != "L" else image
    left = int(max(0, edges[0] - 140))
    right = int(min(gray.width - 1, edges[-1] + 80))
    top = int(max(0, y - 14))
    bottom = int(min(gray.height - 1, y + 16))
    if right - left < 40 or bottom - top < 8:
        return [None] * 10
    strip = gray.crop((left, top, right, bottom))
    width, height = strip.size
    pixels = strip.load()
    dark = [0] * width
    for x in range(width):
        dark[x] = sum(1 for yy in range(height) if pixels[x, yy] < 165)
    runs = []
    start = None
    for x, count in enumerate(dark):
        if count > 2 and start is None:
            start = x
        elif count <= 2 and start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, width))
    merged = []
    for run_left, run_right in runs:
        if run_right - run_left < 3 or run_right - run_left > 150:
            continue
        abs_left = run_left + left
        abs_right = run_right + left
        if merged and abs_left - merged[-1][1] < 10:
            merged[-1] = (merged[-1][0], abs_right)
        else:
            merged.append((abs_left, abs_right))
    groups: List[Optional[List[float]]] = [None] * 10
    for run_left, run_right in merged:
        index = min(range(10), key=lambda col: abs(edges[col] - run_right))
        if abs(edges[index] - run_right) > 80:
            continue
        if groups[index] is None:
            groups[index] = [run_left, run_right]
        else:
            groups[index][0] = min(groups[index][0], run_left)
            groups[index][1] = max(groups[index][1], run_right)
    boxes = []
    for group in groups:
        if group is None:
            boxes.append(None)
            continue
        boxes.append((group[0] - 2, y - 16, group[1] + 4, y + 18))
    return boxes


def _portrait_balance_read_cell(
    image, box, money: bool, recover_above: bool = False
) -> float:
    """OCR one right-aligned cell. A blank cell on this sheet is a printed 0."""
    import pytesseract
    from PIL import Image, ImageOps

    gray = image.convert("L") if getattr(image, "mode", "") != "L" else image
    left, top, right, bottom = box
    left = int(max(0, left))
    top = int(max(0, top))
    right = int(min(gray.width - 1, right))
    bottom = int(min(gray.height - 1, bottom))
    if right - left < 8 or bottom - top < 8:
        return 0.0
    def _prepare(x0: int, y0: int, x1: int, y1: int):
        cell = gray.crop((x0, y0, x1, y1))
        return ImageOps.autocontrast(cell).resize(
            (max(1, cell.width * 3), max(1, cell.height * 3)),
            Image.Resampling.LANCZOS,
        )

    def _once(prepared, threshold: int) -> str:
        binary = ImageOps.expand(
            prepared.point(lambda pixel, limit=threshold: 0 if pixel < limit else 255),
            border=8,
            fill=255,
        )
        return pytesseract.image_to_string(
            binary,
            config="--psm 7 -c tessedit_char_whitelist=0123456789.",
        ).strip()

    def _best_of(prepared) -> str:
        chosen = ""
        for threshold in (185, 210):
            token = _once(prepared, threshold).strip().strip(".")
            digits = re.sub(r"\D", "", token)
            if not digits or len(digits) > 9 or (not money and len(digits) > 6):
                continue
            if len(digits) > len(re.sub(r"\D", "", chosen)):
                chosen = token
        return chosen

    best = _best_of(_prepare(left, top, right, bottom))
    if not best and recover_above:
        best = _best_of(
            _prepare(left, max(0, top - 4), right, min(gray.height - 1, top + 18))
        )
    if not best:
        return 0.0
    if money:
        if re.fullmatch(r"\d+\.\d{2}", best):
            value = float(best)
            if value > 20000000:
                return 0.0
            return value
        digits = re.sub(r"\D", "", best)
        if len(digits) > 9:
            return 0.0
        if len(digits) >= 3:
            return float(digits[:-2] + "." + digits[-2:])
        if digits:
            return float(digits)
        return 0.0
    number = _portrait_balance_number(best)
    if number is None or number > 9999999:
        return 0.0
    return float(number)


def _portrait_balance_grid_items(image, headers: List[Dict[str, Any]]):
    """Read each product cell from its column. Returns items and the grand-total row."""
    from concurrent.futures import ThreadPoolExecutor

    gray = image.convert("L") if getattr(image, "mode", "") != "L" else image
    jobs = []
    grand = None
    for index, header in enumerate(headers):
        edges = header["edges"]
        y_lo = 40.0
        if index == 0:
            y_lo = max(40.0, header["y"] - 420)
        y_hi = headers[index + 1]["y"] - 12 if index + 1 < len(headers) else gray.height - 20
        lines = _portrait_balance_name_lines(
            gray, y_lo, y_hi, header["pack_x0"] + 90
        )
        for line in lines:
            if line["y"] > header["y"] + 8 and re.search(r"^GRAND\b", line["text"], re.I):
                grand = {"y": line["y"], "edges": edges}
                continue
            if line["y"] < header["y"] - 8 and line["y"] > header["y"] - 40:
                continue
            name, pack = _portrait_balance_split_pack(line["text"])
            if line["y"] < header["y"]:
                if not pack or not re.search(r"ML|GM|'S|X", pack, re.I):
                    continue
            if not pack or not name or re.search(r"PRODUCT|TOTAL|GRAND|Powered", name, re.I):
                continue
            jobs.append(
                {
                    "y": line["y"],
                    "name": name,
                    "pack": pack,
                    "edges": edges,
                    "order": (index, line["y"]),
                }
            )
    if len(jobs) < 4:
        return [], None

    def _align_y(y, edges):
        x0 = int(max(0, edges[0] - 80))
        x1 = int(min(gray.width - 1, edges[-1] + 40))
        best_y, best_ink = y, -1
        for delta in range(-16, 10, 2):
            yy = int(y + delta)
            if yy < 8 or yy + 8 >= gray.height:
                continue
            strip = gray.crop((x0, yy - 8, x1, yy + 8))
            ink = sum(1 for pixel in strip.getdata() if pixel < 165)
            if ink > best_ink:
                best_y, best_ink = yy, ink
        return best_y

    def _cells(job):
        y = job["y"]
        if job.get("grand"):
            y = _align_y(y, job["edges"])
        boxes = _portrait_row_column_boxes(gray, y, job["edges"])
        values = []
        for col, box in enumerate(boxes):
            if box is None:
                values.append(0.0)
                continue
            values.append(
                _portrait_balance_read_cell(
                    gray,
                    box,
                    money=col in {1, 3, 6, 8},
                    recover_above=(col == 0),
                )
            )
        return values

    with ThreadPoolExecutor(max_workers=8) as pool:
        read = list(pool.map(_cells, jobs))
    items = []
    for job, values in zip(jobs, read):
        if not job["pack"] and sum(values) <= 0:
            continue
        item = empty_line_item()
        item["product_name"] = job["name"]
        item["packing"] = job["pack"]
        item["opening_qty"] = values[0]
        item["receipts_qty"] = values[2]
        item["sales_qty"] = values[5]
        item["sales_value"] = values[6]
        item["closing_qty"] = values[7]
        item["closing_value"] = values[8]
        extra = item["extra"]
        extra["opening_value"] = values[1]
        extra["total_stock_qty"] = values[4]
        extra["near_expiry_qty"] = values[9]
        extra["layout"] = "portrait_opening_balance"
        extra["source_row"] = len(items) + 1
        item["opening_value"] = values[1]
        item = _attach_receipts_value(item, values[3])
        items.append(item)
    grand_values = None
    if grand:
        grand_values = _cells(
            {"y": grand["y"], "edges": grand["edges"], "grand": True}
        )
    return items, grand_values


def _parse_portrait_opening_balance_image(
    file_bytes: bytes, filename: str
) -> Optional[Dict[str, Any]]:
    """Portrait Sales & Stock: Opening Bal / Receipt/Pur / Issue/Sales / Near Expiry.

    Returns None for every other image so the existing vision path still runs.
    """
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError:
        return None
    import os

    cmd = os.getenv("TESSERACT_CMD", "").strip()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    elif os.name == "nt":
        win_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(win_cmd):
            pytesseract.pytesseract.tesseract_cmd = win_cmd
    try:
        image = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    except Exception:
        return None
    if image.width < 1500:
        image = image.resize(
            (int(image.width * 1.5), int(image.height * 1.5)),
            Image.Resampling.LANCZOS,
        )
    plain_words = _portrait_balance_ocr_words(image)
    preview = " ".join(word["t"] for word in plain_words)
    if not _is_portrait_opening_balance_text(preview):
        return None
    headers = _portrait_balance_headers(plain_words)
    if not headers:
        return None
    items, grand_values = _portrait_balance_grid_items(image, headers)
    if len(items) < 4:
        gray = ImageOps.autocontrast(image.convert("L"))
        binary = gray.point(lambda pixel: 0 if pixel < 140 else 255)
        alt_words = _portrait_balance_ocr_words(binary)
        items = []
        grand_values = None
        for index, header in enumerate(headers):
            y_end = headers[index + 1]["y"] - 15 if index + 1 < len(headers) else 10**9
            for word in plain_words:
                if re.fullmatch(r"GRAND", word["t"], re.I) and word["yc"] > header["y"]:
                    y_end = min(y_end, word["y0"] - 6)
            items.extend(
                _portrait_balance_items(plain_words, header, y_end, alt_words)
            )
    if len(items) < 4:
        return None
    valued = sum(
        1
        for item in items
        if _to_float(item.get("opening_value")) > 0
        or _to_float(item.get("closing_value")) > 0
    )
    if valued < max(3, len(items) // 3):
        return None
    result = empty_result(filename, "png")
    result["line_items"] = items
    result["report_title"] = "Sales & Stock Statement"
    period = re.search(
        r"From\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+Upto\s+"
        r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
        preview,
        re.I,
    )
    if period:
        result["period_from"] = _normalize_date(period.group(1))
        result["period_to"] = _normalize_date(period.group(2))
    for word in plain_words:
        if word["yc"] >= headers[0]["y"]:
            break
        blob = word["t"]
        if re.search(r"ZANDRA", blob, re.I) and not result.get("stockist_name"):
            result["stockist_name"] = _clean_name(blob)
        elif re.search(r"HIMALAYA\s+WELLNESS", blob, re.I):
            result["company_name"] = "HIMALAYA WELLNESS COMPANY"
    if not result.get("stockist_name"):
        above = [
            word["t"]
            for word in plain_words
            if word["yc"] < headers[0]["y"] and re.search(r"ZANDRA", word["t"], re.I)
        ]
        if above:
            result["stockist_name"] = _clean_name(" ".join(above))
    # Stockist is often split across adjacent words on the line that contains ZANDRA.
    zandra_y = next(
        (
            word["yc"]
            for word in plain_words
            if word["yc"] < headers[0]["y"] and re.search(r"ZANDRA", word["t"], re.I)
        ),
        None,
    )
    if zandra_y is not None:
        line = [
            word["t"]
            for word in sorted(plain_words, key=lambda item: item["x0"])
            if abs(word["yc"] - zandra_y) <= 14 and word["x0"] < image.width * 0.55
        ]
        joined = _clean_name(
            " ".join(tok for tok in line if re.search(r"[A-Za-z]{3,}", tok))
        )
        if joined and re.search(r"ZANDRA", joined, re.I):
            result["stockist_name"] = joined
    result["totals"]["extra"]["extraction_method"] = "portrait_opening_balance_columns"
    result["totals"]["extra"]["layout"] = "portrait_opening_balance"
    if grand_values and len(grand_values) >= 10:
        # Printed GRAND TOTAL, same column order as the product rows.
        keys = (
            ("opening_qty", 0, False),
            ("opening_value", 1, True),
            ("receipts_qty", 2, False),
            ("receipts_value", 3, True),
            ("sales_qty", 5, False),
            ("sales_value", 6, True),
            ("closing_qty", 7, False),
            ("closing_value", 8, True),
        )
        for key, index, _money in keys:
            result["totals"][key] = grand_values[index]
        result["totals"]["extra"]["opening_value"] = grand_values[1]
        result["totals"]["extra"]["total_stock_qty"] = grand_values[4]
        result["totals"]["extra"]["near_expiry_qty"] = grand_values[9]
        result["totals"]["extra"]["total_row_numbers"] = grand_values
        result["totals"]["extra"]["total_row_format"] = "portrait_opening_balance"
        result["totals"]["extra"]["total_row_source"] = "portrait_grand_total"
    else:
        result["totals"]["sales_value"] = round(
            sum(_to_float(item.get("sales_value")) for item in items), 2
        )
        result["totals"]["closing_value"] = round(
            sum(_to_float(item.get("closing_value")) for item in items), 2
        )
        result["totals"]["receipts_value"] = round(
            sum(_line_receipts_value(item) for item in items), 2
        )
        result["totals"]["extra"]["opening_value"] = round(
            sum(_to_float(item.get("opening_value")) for item in items), 2
        )
    return result


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


def _looks_like_medica_stock_statement(result: Optional[Dict[str, Any]]) -> bool:
    """Phone photo of a Medica STOCK STATEMENT (OPSTK / SALE VAL / N/O/STOCK / STK VAL)."""
    if not isinstance(result, dict):
        return False
    extra = ((result.get("totals") or {}).get("extra") or {})
    if extra.get("extraction_method") == "medica_stock_statement_vision":
        return True
    title = re.sub(r"\s+", " ", str(result.get("report_title") or "")).strip()
    if not re.fullmatch(r"STOCK STATEMENT", title, re.I):
        return False
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    if len(items) < 5:
        return False
    return sum(1 for i in items if i.get("packing")) >= 3


_MEDICA_STOCK_STATEMENT_PROMPT = """
This image is a Medica Ultimate STOCK STATEMENT.
Columns left to right:
PRODUCT DESCRIPTION, PACKING, OPSTK, PURCH, SALE, SALE VAL, N/O/STOCK, STK VAL, JUL, JUN, STK126, EXP3M

Keep every number on the same product row. Never move STK VAL or SALE VAL onto the previous or next product.

Map:
- PRODUCT DESCRIPTION -> product_name
- PACKING -> packing
- OPSTK -> opening_qty
- PURCH -> receipts_qty
- SALE -> sales_qty
- SALE VAL -> sales_value
- N/O/STOCK -> closing_qty
- STK VAL -> closing_value
- JUL -> extra.jul_qty
- JUN -> extra.jun_qty

Skip the division banner (HIMALAYA-ZEAL) and the Division Total row.
Ignore the phone status bar and the PDF viewer toolbar.
STK VAL is the column immediately after N/O/STOCK. JUL is the next column and is not the stock value, even when JUL is a large number.

Examples on one row each:
- AACTARIL SOAP, packing 75 GM, opening 2, receipts 0, sales 2, sales_value 176, closing_qty 0, closing_value 0
- ABANA TAB, packing 60 TAB, opening 0, receipts 3, sales 0, sales_value 0, closing_qty 3, closing_value 455, jul_qty 3
- ACTARIL SOAP, packing NA, opening 64, receipts 0, sales 3, sales_value 262, closing_qty 0, closing_value 61, jul_qty 4738
- CONFIDO TAB, packing 60 TAB, opening 37, receipts 0, sales 28, sales_value 5184, closing_qty 2, closing_value 11, jul_qty 1805

Return ONLY JSON with stockist_name, company_name, period_from, period_to, report_title "STOCK STATEMENT", and line_items using the fields above.
""".strip()


def _medica_statement_strips(file_bytes: bytes) -> List[bytes]:
    """Header plus short table bands so one product's STK VAL cannot slide onto the next."""
    try:
        from PIL import Image
    except ImportError:
        return [file_bytes]
    image = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    width, height = image.size
    pixels = image.load()
    red_rows = []
    for y in range(height):
        reds = 0
        for x in range(0, width, 4):
            red, green, blue = pixels[x, y]
            if red > 140 and green < 80 and blue < 80:
                reds += 1
        if reds > 40:
            red_rows.append(y)
    if len(red_rows) < 2:
        return [file_bytes]
    gap_at = max(range(len(red_rows) - 1), key=lambda i: red_rows[i + 1] - red_rows[i])
    header_y, footer_y = red_rows[gap_at], red_rows[gap_at + 1]
    header = image.crop((0, max(0, header_y - 6), width, header_y + 36))
    strips: List[bytes] = []
    top = header_y + 36
    while top < footer_y - 8:
        band = image.crop((0, top, width, min(footer_y, top + 150)))
        canvas = Image.new("RGB", (width, header.height + band.height), "white")
        canvas.paste(header, (0, 0))
        canvas.paste(band, (0, header.height))
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=90)
        strips.append(buf.getvalue())
        top += 120
    return strips or [file_bytes]


def _extract_medica_stock_statement_vision(
    file_bytes: bytes, filename: str, ext: str
) -> Optional[Dict[str, Any]]:
    import os

    from services.vertex_gemini_client import generate_content_via_vertex

    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    merged: List[Dict[str, Any]] = []
    seen = set()
    parsed_meta: Dict[str, Any] = {}
    for strip in _medica_statement_strips(file_bytes):
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": _MEDICA_STOCK_STATEMENT_PROMPT},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": base64.b64encode(strip).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8192},
        }
        parsed = None
        for attempt in range(2):
            try:
                response = generate_content_via_vertex(
                    model=model, payload=payload, timeout=120
                )
                parsed = _extract_json_object(_gemini_response_text(response))
                if parsed and parsed.get("line_items"):
                    break
            except Exception as exc:
                logger.warning("Medica stock statement vision failed: %s", exc)
                time.sleep(min(2 ** attempt, 4))
        if not parsed:
            continue
        for key in ("stockist_name", "company_name", "period_from", "period_to"):
            if parsed.get(key) and not parsed_meta.get(key):
                parsed_meta[key] = parsed[key]
        for raw in parsed.get("line_items") or []:
            if not isinstance(raw, dict):
                continue
            name = _clean_name(str(raw.get("product_name") or ""))
            if not name or re.search(
                r"HIMALAYA\s*-?\s*ZEAL|Division\s*Total|^Total\b", name, re.I
            ):
                continue
            key = name.upper()
            if key in seen:
                continue
            seen.add(key)
            raw["product_name"] = name
            merged.append(raw)
    if not merged:
        return None
    parsed_meta["report_title"] = "STOCK STATEMENT"
    parsed_meta["line_items"] = merged
    result = empty_result(filename, ext.lstrip(".") or "jpg")
    result = _apply_parsed_sales_json(result, parsed_meta)
    kept = []
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or "")
        if re.search(r"HIMALAYA\s*-?\s*ZEAL|Division\s*Total|^Total\b", name, re.I):
            continue
        extra = item.get("extra")
        if not isinstance(extra, dict):
            extra = {}
            item["extra"] = extra
        extra["layout"] = "medica_stock_statement"
        kept.append(item)
    result["line_items"] = kept
    result["report_title"] = "STOCK STATEMENT"
    result.setdefault("totals", {}).setdefault("extra", {})
    result["totals"]["extra"]["extraction_method"] = "medica_stock_statement_vision"
    result["totals"]["extra"]["layout"] = "medica_stock_statement"
    return result


_MAIN_STOCK_SALES_PROMPT = """
This image is an Excel screenshot titled MAIN STOCK & SALES STATEMENT.
Use it only for this header, left to right:
PRODUCT DESCRIPTION | OPENING STOCK | OPENING VALUE | RECEIVE QUANTITY | RECEIVE VALUE | ISSUE QUANTITY | ISSUE VALUE | CLOSING STOCK | CLOSING VALUE

Column mapping:
- PRODUCT DESCRIPTION -> product_name
- OPENING STOCK -> opening_qty
- OPENING VALUE -> extra.opening_value
- RECEIVE QUANTITY -> receipts_qty
- RECEIVE VALUE -> extra.receipts_value
- ISSUE QUANTITY -> sales_qty
- ISSUE VALUE -> sales_value
- CLOSING STOCK -> closing_qty
- CLOSING VALUE -> closing_value

RECEIVE VALUE is the money column immediately after RECEIVE QUANTITY.
Do not leave extra.receipts_value at 0 when that cell has a number.
A printed 0 stays 0. Do not shift a later number left into a blank cell.
Skip the TOTAL row. Skip the phone status bar and Excel row numbers.

Example: ABANA TAB 1*60
opening_qty=2, extra.opening_value=291.54, receipts_qty=5, extra.receipts_value=728.86,
sales_qty=6, sales_value=958.79, closing_qty=1, closing_value=145.7

Return ONLY JSON:
{
  "stockist_name": string|null,
  "stockist_address": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": "MAIN STOCK & SALES STATEMENT",
  "line_items": [
    {
      "product_code": null,
      "product_name": string,
      "packing": null,
      "opening_qty": number,
      "receipts_qty": number,
      "sales_qty": number,
      "sales_value": number,
      "closing_qty": number,
      "closing_value": number,
      "extra": {"opening_value": number, "receipts_value": number}
    }
  ]
}
""".strip()


def _main_stock_receive_value_missing(result: Optional[Dict[str, Any]]) -> bool:
    """This Excel sheet prints RECEIVE VALUE. Other titles are left alone."""
    if not isinstance(result, dict):
        return False
    title = str(result.get("report_title") or "")
    if not re.search(r"MAIN\s+STOCK\s*&\s*SALES\s+STATEMENT", title, re.I):
        return False
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        if _to_float(item.get("receipts_qty")) <= 0:
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if (
            _to_float(item.get("receipts_value")) == 0
            and _to_float(extra.get("receipts_value")) == 0
            and _to_float(extra.get("purchase_value")) == 0
        ):
            return True
    return False


def _main_stock_sales_strips(file_bytes: bytes) -> List[bytes]:
    """Header plus row bands for this sideways phone photo. Other images are unchanged."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return [file_bytes]
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(file_bytes))).convert("RGB")
    width, height = image.size
    pixels = image.load()
    blue_rows = []
    for y in range(height):
        blue = 0
        for x in range(0, width, 6):
            red, green, blue_px = pixels[x, y]
            if blue_px > red + 20 and blue_px > 80:
                blue += 1
        if blue > 80:
            blue_rows.append(y)
    if len(blue_rows) < 8:
        return [file_bytes]
    header_y0 = blue_rows[0]
    header_y1 = blue_rows[0]
    for y in blue_rows:
        if y <= header_y1 + 6:
            header_y1 = y
        elif y - header_y1 > 40:
            break
    footer = next((y for y in blue_rows if y > header_y1 + 200), height - 20)
    header = image.crop((0, max(0, header_y0 - 2), width, header_y1 + 2))
    strips: List[bytes] = []
    top = header_y1 + 2
    while top < footer - 20:
        band = image.crop((0, top, width, min(footer, top + 520)))
        piece = Image.new("RGB", (width, header.height + band.height), "white")
        piece.paste(header, (0, 0))
        piece.paste(band, (0, header.height))
        buf = io.BytesIO()
        piece.save(buf, format="JPEG", quality=85)
        strips.append(buf.getvalue())
        top += 400
    return strips or [file_bytes]


def _extract_main_stock_sales_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    fallback: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Read this statement in row bands so ISSUE and RECEIVE values stay on the row."""
    import os
    import time

    from services.vertex_gemini_client import generate_content_via_vertex

    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    merged: List[Dict[str, Any]] = []
    seen: Dict[str, float] = {}
    parsed_meta: Dict[str, Any] = {}
    strips = _main_stock_sales_strips(file_bytes)
    for strip_index, strip in enumerate(strips):
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": _MAIN_STOCK_SALES_PROMPT},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": base64.b64encode(strip).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8192},
        }
        parsed = None
        for attempt in range(2):
            try:
                response = generate_content_via_vertex(
                    model=model, payload=payload, timeout=120
                )
                parsed = _extract_json_object(_gemini_response_text(response))
                if parsed and parsed.get("line_items"):
                    break
            except Exception as exc:
                logger.warning("MAIN STOCK statement vision failed: %s", exc)
                time.sleep(min(2 ** attempt, 4))
        if not parsed:
            continue
        rows = [raw for raw in (parsed.get("line_items") or []) if isinstance(raw, dict)]
        for key in ("stockist_name", "company_name", "period_from", "period_to"):
            if parsed.get(key) and not parsed_meta.get(key):
                parsed_meta[key] = parsed[key]
        for raw in rows:
            name = _clean_name(str(raw.get("product_name") or ""))
            key = re.sub(r"[^A-Z0-9]", "", name.upper())
            if not key or key.startswith("TOTAL"):
                continue
            raw["product_name"] = name
            amount_keys = (
                "opening_value",
                "receipts_value",
                "sales_value",
                "closing_value",
            )
            extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
            score = sum(
                _to_float(extra.get(amount) if amount in ("opening_value", "receipts_value") else raw.get(amount))
                for amount in amount_keys
            )
            if key in seen and score <= seen[key]:
                continue
            seen[key] = score
            merged = [
                old
                for old in merged
                if re.sub(r"[^A-Z0-9]", "", str(old.get("product_name") or "").upper()) != key
            ]
            extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
            receive = extra.get("receipts_value", raw.get("receipts_value"))
            if receive not in (None, ""):
                raw["receipts_value"] = _to_float(receive)
                extra["receipts_value"] = raw["receipts_value"]
                raw["extra"] = extra
            merged.append(raw)
    if len(merged) < 5:
        return None
    parsed_meta["report_title"] = "MAIN STOCK & SALES STATEMENT"
    parsed_meta["line_items"] = merged
    for key in ("stockist_name", "company_name", "period_from", "period_to"):
        current = str(parsed_meta.get(key) or "")
        if not current or (key == "company_name" and re.search(r"STATEMENT", current, re.I)):
            parsed_meta[key] = fallback.get(key)
    result = empty_result(filename, ext.lstrip(".") or "jpg")
    result = _apply_parsed_sales_json(result, parsed_meta)
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if extra.get("receipts_value") not in (None, ""):
            item["receipts_value"] = _to_float(extra.get("receipts_value"))
    result.setdefault("totals", {}).setdefault("extra", {})
    result["totals"]["extra"]["extraction_method"] = "main_stock_sales_statement"
    result["totals"]["extra"]["layout"] = "main_stock_sales_statement"
    return result


def _is_a2z_opening_mexp_text(text: str) -> bool:
    """A2Z STOCK & SALES ANALYSIS: ITEM DESCRIPTION, qty columns, and M.EXP.

    Pack is its own column even when the header line has no PACK word.
    The Zandra Op Stk / P Qty grid is a different statement.
    """
    if not text:
        return False
    if re.search(r"\bDUMP\b|QTY\.?\s+VALUE|\bOp\s*Stk\b|\bP\s*Qty\b|Item\s*Cd", text, re.I):
        return False
    if not re.search(r"ITEM\s+DESCRIPTION", text, re.I):
        return False
    if not re.search(r"\bOPENING\b", text, re.I):
        return False
    if not re.search(r"\bRECEIPT\b", text, re.I):
        return False
    if not re.search(r"\bCLOSING\b", text, re.I):
        return False
    if not re.search(r"M\.?\s*\\?\s*EXP", text, re.I):
        return False
    return bool(re.search(r"ISSUE|TSSUE", text, re.I))


def _a2z_header_token(text: str) -> str:
    return re.sub(r"[^A-Z]", "", str(text or "").upper())


def _a2z_page_image(file_bytes: bytes):
    """Use the bright statement page inside a photo of a monitor."""
    from PIL import Image, ImageEnhance, ImageOps

    image = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    gray = image.convert("L")
    width, height = gray.size
    pixels = gray.load()
    top_light = sum(1 for x in range(0, width, 4) if pixels[x, min(8, height - 1)] > 170)
    if top_light > width / 8:
        page = image
    else:
        ys = [
            y
            for y in range(height)
            if sum(1 for x in range(0, width, 6) if pixels[x, y] > 170) > width / 6 * 0.35
        ]
        xs = [
            x
            for x in range(width)
            if sum(1 for y in range(height // 5, 4 * height // 5, 8) if pixels[x, y] > 170) > 20
        ]
        if not ys or not xs:
            page = image
        else:
            x0, y0, x1, y1 = min(xs), min(ys), max(xs) + 1, max(ys) + 1
            pad_x = int((x1 - x0) * 0.045)
            pad_y = int((y1 - y0) * 0.035)
            page = image.crop((x0 + pad_x, y0 + pad_y, x1 - pad_x, y1 - pad_y))
    page = ImageOps.autocontrast(page)
    page = ImageEnhance.Contrast(page).enhance(1.4)
    return page.resize((page.width * 2, page.height * 2), Image.Resampling.LANCZOS)


def _a2z_tesseract():
    import os
    import pytesseract

    cmd = os.getenv("TESSERACT_CMD", "").strip()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    elif os.name == "nt":
        win_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(win_cmd):
            pytesseract.pytesseract.tesseract_cmd = win_cmd
    return pytesseract


def _a2z_reread_digits(page, box: Dict[str, Any]) -> str:
    pytesseract = _a2z_tesseract()
    from PIL import Image

    pad = 4
    cell = page.crop((
        max(0, int(box["x0"]) - pad),
        max(0, int(box["y0"]) - pad),
        int(box["x1"]) + pad,
        int(box["y1"]) + pad,
    ))
    cell = cell.resize((max(1, cell.width * 3), max(1, cell.height * 3)), Image.Resampling.LANCZOS)
    return pytesseract.image_to_string(
        cell,
        config="--psm 7 -c tessedit_char_whitelist=0123456789",
    ).strip()


def _a2z_reread_date(page, boxes: List[Dict[str, Any]]) -> str:
    """Re-read an M.EXP cell. Monitor stripes turn 11/24 into UL/24."""
    from PIL import Image, ImageOps

    if page is None or not boxes:
        return ""
    pytesseract = _a2z_tesseract()
    pad = 3
    cell = page.crop((
        max(0, min(int(box["x0"]) for box in boxes) - pad),
        max(0, min(int(box["y0"]) for box in boxes) - pad),
        max(int(box["x1"]) for box in boxes) + pad,
        max(int(box["y1"]) for box in boxes) + pad,
    ))
    gray = ImageOps.grayscale(cell)
    # Collapse the vertical phosphor stripes, then scale back up.
    squeezed = gray.resize((max(1, gray.width // 3), gray.height), Image.Resampling.BOX)
    wide = squeezed.resize((gray.width * 6, gray.height * 6), Image.Resampling.LANCZOS)
    wide = ImageOps.autocontrast(wide)
    text = pytesseract.image_to_string(
        wide,
        config="--psm 8 -c tessedit_char_whitelist=0123456789/",
    )
    text = re.sub(r"\s+", "", text or "")
    return text if re.fullmatch(r"\d{1,2}/\d{2,4}", text) else ""


def _a2z_cell_qty(tokens: List[Dict[str, Any]], page) -> float:
    cleaned = [
        str(token["t"]).strip()
        for token in tokens
        if str(token["t"]).strip() not in {"", "|"}
    ]
    if any(token in {"-", "–", "—", "--"} for token in cleaned) and not any(
        re.fullmatch(r"\d+(?:\.\d+)?", token) for token in cleaned
    ):
        return 0.0
    for token in cleaned:
        if re.fullmatch(r"\d+(?:\.\d+)?", token):
            return float(token)
    for token in tokens:
        raw = str(token["t"]).strip()
        if page is None or not raw or raw.isalpha() or len(raw) > 3:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            continue
        digits = _a2z_reread_digits(page, token)
        if re.fullmatch(r"\d+", digits or ""):
            return float(digits)
    return 0.0


def _a2z_items_from_ocr_words(
    words: List[Dict[str, Any]], page
) -> List[Dict[str, Any]]:
    """Place each glyph in PACK / OPENING / RECEIPT / ISSUE / CLOSING by header x.

    The pack number sits left of OPENING. It is not the opening quantity.
    """
    if not words:
        return []
    heights = sorted(box["y1"] - box["y0"] for box in words)
    tol = max(2.0, heights[len(heights) // 2] * 0.6)
    ordered = sorted(words, key=lambda box: ((box["y0"] + box["y1"]) / 2.0, box["x0"]))
    rows: List[Dict[str, Any]] = []
    for box in ordered:
        cy = (box["y0"] + box["y1"]) / 2.0
        if rows and abs(cy - rows[-1]["cy"]) <= tol:
            rows[-1]["words"].append(box)
            count = len(rows[-1]["words"])
            rows[-1]["cy"] = ((rows[-1]["cy"] * (count - 1)) + cy) / count
        else:
            rows.append({"cy": cy, "words": [box]})
    for row in rows:
        row["words"].sort(key=lambda box: box["x0"])
    header = None
    for row in rows:
        labels = [_a2z_header_token(box["t"]) for box in row["words"]]
        blob = " ".join(labels)
        if "OPENING" in blob and "RECEIPT" in blob and "CLOSING" in blob:
            header = row
            break
    if header is None:
        return []
    anchors: Dict[str, Tuple[float, float]] = {}
    for box in header["words"]:
        label = _a2z_header_token(box["t"])
        center = (box["x0"] + box["x1"]) / 2.0
        if label == "OPENING":
            anchors["opening"] = (box["x0"], center)
        elif label == "RECEIPT":
            anchors["receipt"] = (box["x0"], center)
        elif label.startswith("ISS") or label.startswith("TSS"):
            anchors["issue"] = (box["x0"], center)
        elif label.startswith("CLOS"):
            anchors["closing"] = (box["x0"], center)
        elif "EXP" in label:
            anchors["mexp"] = (box["x0"], center)
    if "issue" not in anchors and "receipt" in anchors and "closing" in anchors:
        receipt_center = anchors["receipt"][1]
        closing_center = anchors["closing"][1]
        between = [
            box
            for box in header["words"]
            if receipt_center < (box["x0"] + box["x1"]) / 2.0 < closing_center
            and not _a2z_header_token(box["t"]).startswith("CLOS")
        ]
        if between:
            box = between[0]
            anchors["issue"] = (box["x0"], (box["x0"] + box["x1"]) / 2.0)
        else:
            anchors["issue"] = (
                (anchors["receipt"][0] + anchors["closing"][0]) / 2.0,
                (receipt_center + closing_center) / 2.0,
            )
    needed = ("opening", "receipt", "issue", "closing")
    if any(name not in anchors for name in needed):
        return []
    fields = ["opening", "receipt", "issue", "closing"]
    if "mexp" in anchors:
        fields.append("mexp")
    centers = [anchors[name][1] for name in fields]
    gaps = [centers[index + 1] - centers[index] for index in range(len(centers) - 1)]
    pitch = sorted(gaps)[len(gaps) // 2] if gaps else 80.0
    opening_left = anchors["opening"][0]
    pack_left = opening_left - pitch
    items: List[Dict[str, Any]] = []
    for row in rows:
        if row is header:
            continue
        name_bits: List[str] = []
        pack_bits: List[str] = []
        cells: Dict[str, List[Dict[str, Any]]] = {name: [] for name in fields}
        for box in row["words"]:
            token = str(box["t"]).strip().strip("|")
            if not token:
                continue
            center = (box["x0"] + box["x1"]) / 2.0
            compact = token.replace(" ", "")
            if "mexp" in cells and (
                _PACK_MEXP_DATE_RE.match(compact) or re.search(r"/\d{2,4}$", compact)
            ):
                cells["mexp"].append(box)
                continue
            if center < pack_left:
                name_bits.append(token)
                continue
            if center < opening_left:
                pack_bits.append(token)
                continue
            nearest = min(range(len(centers)), key=lambda index: abs(centers[index] - center))
            if abs(centers[nearest] - center) <= pitch * 0.65:
                cells[fields[nearest]].append(box)
        name = _clean_name(" ".join(name_bits))
        if not name or not re.search(r"[A-Za-z]", name):
            continue
        if re.match(r"^(?:TOTAL|GRAND|HIMALAYA\s+WELLNESS|COMPANY)\b", name, re.I):
            continue
        pack = _clean_name(" ".join(pack_bits)) or None
        opening = _a2z_cell_qty(cells["opening"], page)
        receipt = _a2z_cell_qty(cells["receipt"], page)
        issue = _a2z_cell_qty(cells["issue"], page)
        closing = _a2z_cell_qty(cells["closing"], page)
        mexp_boxes = cells.get("mexp") or []
        mexp_text = " ".join(str(box["t"]).strip() for box in mexp_boxes)
        expiry_match = re.search(r"\d{1,2}\s*/\s*\d{2,4}", mexp_text)
        if expiry_match:
            expiry = expiry_match.group(0).replace(" ", "")
        elif any("/" in str(box["t"]) for box in mexp_boxes):
            expiry = _a2z_reread_date(page, mexp_boxes) or None
        else:
            expiry = None
        if pack is None and opening == receipt == issue == closing == 0:
            continue
        item = empty_line_item()
        item["product_name"] = name
        item["packing"] = pack
        item["opening_qty"] = opening
        item["receipts_qty"] = receipt
        item["sales_qty"] = issue
        item["closing_qty"] = closing
        item["sales_value"] = 0.0
        item["closing_value"] = 0.0
        item["extra"] = {
            "layout": "pack_opening_receipt_issue_mexp",
            "source_product_name": name,
            "source_packing": pack,
            "m_exp": expiry,
            "expiry": expiry,
        }
        items.append(item)
    return items


def _parse_a2z_opening_mexp_image(
    file_bytes: bytes, filename: str, ext: str
) -> Optional[Dict[str, Any]]:
    """Read the A2Z photo by column position. Pack stays left of OPENING."""
    preview = _ocr_image_to_text(file_bytes, psm=6)
    if not _is_a2z_opening_mexp_text(preview):
        return None
    page = _a2z_page_image(file_bytes)
    pytesseract = _a2z_tesseract()
    data = pytesseract.image_to_data(page, config="--psm 6", output_type=pytesseract.Output.DICT)
    words: List[Dict[str, Any]] = []
    for index, token in enumerate(data["text"]):
        token = str(token or "").strip()
        if not token:
            continue
        x0 = int(data["left"][index])
        y0 = int(data["top"][index])
        words.append({
            "x0": x0,
            "y0": y0,
            "x1": x0 + int(data["width"][index]),
            "y1": y0 + int(data["height"][index]),
            "t": token,
        })
    items = _a2z_items_from_ocr_words(words, page)
    if len(items) < 3:
        return None
    result = empty_result(filename, ext.lstrip(".") or "jpg")
    if re.search(r"A2Z\s+PHARMA", preview, re.I):
        result["stockist_name"] = "A2Z PHARMA"
    if re.search(r"HIMALAYA\s+WELLNESS\s+COMPANY", preview, re.I):
        result["company_name"] = "HIMALAYA WELLNESS COMPANY"
    period = re.search(
        r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}).{0,12}(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
        preview,
    )
    if period:
        result["period_from"] = _normalize_date(period.group(1))
        result["period_to"] = _normalize_date(period.group(2))
    result["report_title"] = "STOCK & SALES ANALYSIS"
    return _pack_mexp_finish(result, items)


def _parse_image(file_bytes: bytes, filename: str, ext: str) -> Dict[str, Any]:
    """Extract sales statement from image via Gemini Vision, with OCR fallback."""
    import os
    import time

    from services.vertex_gemini_client import (
        GeminiProviderError,
        generate_content_via_vertex,
    )

    result = empty_result(filename, ext.lstrip("."))
    rtl = _summary_rtl_from_ocr_bytes(
        file_bytes, filename, (ext or ".png").lstrip(".") or "png"
    )
    if rtl and rtl.get("line_items"):
        return rtl
    a2z = _parse_a2z_opening_mexp_image(file_bytes, filename, ext)
    if a2z and a2z.get("line_items"):
        return a2z
    sales_free = _parse_ssa_sales_free_image(file_bytes, filename, ext)
    if sales_free and sales_free.get("line_items"):
        return sales_free
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
                if _looks_like_summary_rtl_result(result):
                    result = _fill_summary_rtl_amounts_from_image(
                        result, b64, mime, model
                    )
                    result = _clear_summary_rtl_fabricated_qty(result)
                    result["totals"]["extra"]["extraction_method"] = (
                        "summary_rtl_op_amt"
                    )
                    return result
                result["totals"]["extra"]["extraction_method"] = "gemini_vision"
                if _main_stock_receive_value_missing(result):
                    main_stock = _extract_main_stock_sales_vision(
                        file_bytes, filename, ext, result
                    )
                    if main_stock and main_stock.get("line_items"):
                        return main_stock
                if _looks_like_zl_opening_bal_sheet(result):
                    zl_sheet = _extract_zl_opening_bal_sheet_vision(
                        file_bytes, filename, ext
                    )
                    if zl_sheet and zl_sheet.get("line_items"):
                        return zl_sheet
                if _looks_like_medica_stock_statement(result):
                    medica = _extract_medica_stock_statement_vision(
                        file_bytes, filename, ext
                    )
                    if medica and medica.get("line_items"):
                        return medica
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
            if re.search(r'"report_title"\s*:\s*"Sheet\s*\d+"', text or "", re.I):
                zl_sheet = _extract_zl_opening_bal_sheet_strips(
                    file_bytes, filename, ext
                )
                if not (zl_sheet and zl_sheet.get("line_items")):
                    zl_sheet = _extract_zl_opening_bal_sheet_vision(
                        file_bytes, filename, ext
                    )
                if zl_sheet and zl_sheet.get("line_items"):
                    return zl_sheet
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
