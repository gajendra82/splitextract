"""Parse PaddleOCR-VL HTML tables into sales-statement line items.

Delegates column mapping to ``services.html_table`` (template → fuzzy → LLM).
Headers are explicit in the markup, so spatial column geometry is not required.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from services.html_table.html_table_parser import (
    EXTRACTION_METHOD,
    parse_html_table,
    parse_numeric,
)

logger = logging.getLogger(__name__)

# Re-export for existing callers / tests.
__all__ = [
    "EXTRACTION_METHOD",
    "extract_vl_table_html",
    "parse_numeric",
    "parse_vl_html_table",
    "resolve_vl_or_text_extraction",
    "table_html_available",
]


def extract_vl_table_html(text: str) -> str:
    """Return concatenated ``<table>...</table>`` fragments from OCR text."""
    if not text or "<table" not in text.lower():
        return ""
    matches = re.findall(r"(?is)<table\b[^>]*>.*?</table>", text)
    if matches:
        return "\n".join(m.strip() for m in matches if m.strip())
    lower = text.lower()
    start = lower.find("<table")
    if start >= 0:
        return text[start:].strip()
    return ""


def table_html_available(html_or_text: Optional[str]) -> bool:
    html = extract_vl_table_html(html_or_text or "")
    return bool(html and html.strip())


def _llm_allowed() -> bool:
    return os.getenv("ENABLE_LLM_COLUMN_MAPPER", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def parse_vl_html_table(
    html_str: str,
    *,
    filename: str = "",
    source_format: str = "",
    allow_llm: Optional[bool] = None,
) -> Dict[str, Any]:
    """Parse PaddleOCR-VL table HTML into statement line_items + totals.

    Uses multi-vendor column mapping (template → fuzzy → LLM). Returns
    ``extraction_method=vl_html_table_parse``. TOTAL rows are removed from
    ``line_items`` and folded into ``totals``.
    """
    html = extract_vl_table_html(html_str) or (html_str or "").strip()
    use_llm = _llm_allowed() if allow_llm is None else bool(allow_llm)

    page_chunks = re.findall(r"(?is)<table\b[^>]*>.*?</table>", html) if html else []
    if len(page_chunks) > 1:
        from services.html_table.multi_page import merge_page_tables

        parsed = merge_page_tables(page_chunks, allow_llm=use_llm)
    elif html:
        parsed = parse_html_table(html, allow_llm=use_llm)
    else:
        parsed = {
            "line_items": [],
            "totals": None,
            "extraction_method": EXTRACTION_METHOD,
            "columns_matched": {},
            "columns_unmatched": [],
            "mapping_source": "fuzzy",
        }

    line_items: List[Dict[str, Any]] = list(parsed.get("line_items") or [])
    parsed_totals = parsed.get("totals")
    mapping_source = str(parsed.get("mapping_source") or "fuzzy")
    columns_matched = parsed.get("columns_matched") or {}
    columns_unmatched = parsed.get("columns_unmatched") or []
    logger.info(
        "vl_html_table_parse file=%s mapping_source=%s items=%s unmatched=%s",
        filename or "?",
        mapping_source,
        len(line_items),
        len(columns_unmatched),
    )

    totals: Dict[str, Any] = {
        "extra": {
            "extraction_method": EXTRACTION_METHOD,
            "table_available": bool(line_items) or bool(columns_matched),
            "ocr_table_html": html[:20000] if html else "",
            "vl_table_html": True,
            "mapping_source": mapping_source,
            "columns_matched": {str(k): v for k, v in dict(columns_matched).items()},
            "columns_unmatched": list(columns_unmatched),
            "page_count": parsed.get("page_count"),
        }
    }
    if isinstance(parsed_totals, dict):
        parsed_extra = parsed_totals.get("extra") if isinstance(
            parsed_totals.get("extra"), dict
        ) else {}
        for key, value in parsed_totals.items():
            if key == "extra":
                continue
            if totals.get(key) is None:
                totals[key] = value
        if parsed_extra:
            for ek, ev in parsed_extra.items():
                if ek == "summary_values" and isinstance(ev, dict):
                    merged = dict(totals["extra"].get("summary_values") or {})
                    merged.update(ev)
                    totals["extra"]["summary_values"] = merged
                elif ek not in totals["extra"] or totals["extra"].get(ek) in (None, ""):
                    totals["extra"][ek] = ev
        if any(
            parsed_totals.get(k) is not None
            for k in ("sales_value", "closing_value", "opening_value", "receipts_value")
        ):
            totals["extra"]["totals_row_detected"] = True
        if parsed_extra.get("summary_rows_filtered"):
            totals["extra"]["summary_rows_filtered"] = parsed_extra.get(
                "summary_rows_filtered"
            )

    if not html or "<table" not in html.lower():
        totals["extra"]["table_available"] = False
        return {
            "source_file": filename or "",
            "source_format": (source_format or "").lstrip(".") or "ocr",
            "line_items": [],
            "totals": totals,
        }

    mapped_fields = set(dict(columns_matched).values())
    required_any_qty = {"opening_qty", "sales_qty", "closing_qty", "receive_qty"}
    if "product_name" not in mapped_fields or not (mapped_fields & required_any_qty):
        totals["extra"]["table_available"] = False
        totals["extra"]["vl_html_unmapped_headers"] = True
        return {
            "source_file": filename or "",
            "source_format": (source_format or "").lstrip(".") or "ocr",
            "line_items": [],
            "totals": totals,
        }

    if not line_items:
        totals["extra"]["table_available"] = bool(columns_matched)
        if not columns_matched:
            totals["extra"]["vl_html_unmapped_headers"] = True

    result = {
        "source_file": filename or "",
        "source_format": (source_format or "").lstrip(".") or "ocr",
        "line_items": line_items,
        "totals": totals,
    }
    if parsed.get("statements"):
        result["statements"] = parsed["statements"]

    # Prefatory OCR text before <table> often carries stockist banner + period.
    prefix = ""
    if html_str and "<table" in html_str.lower():
        prefix = html_str[: html_str.lower().find("<table")]
    try:
        from services.identity_disambiguation import disambiguate_identity

        disambiguate_identity(result, ocr_text=prefix or html_str)
    except Exception:
        logger.debug("identity_disambiguation skipped", exc_info=True)
    try:
        _attach_period_from_text(result, prefix or html_str or "")
    except Exception:
        logger.debug("period scrape skipped", exc_info=True)
    return result


_PERIOD_FROM_TO_RE = re.compile(
    r"(?is)(?:from|period\s*[:\-]?\s*|upto\s*)?"
    r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})"
    r".{0,40}?"
    r"(?:to|upto|up\s*to|[-–—])\s*"
    r"(\d{1,2}[/\-.]\d{1,2}[/\-.](?:\d{2,4})(?:/\d{4})?)"
)


def _attach_period_from_text(result: Dict[str, Any], text: str) -> None:
    """Fill ``period_from`` / ``period_to`` from Indian DD-MM-YYYY banner text."""
    if result.get("period_from") and result.get("period_to"):
        return
    from services.indian_normalize import parse_indian_date

    match = _PERIOD_FROM_TO_RE.search(text or "")
    if not match:
        # Single "from 01/08/2026 to 29/08/2026" already covered; try looser.
        dates = re.findall(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}(?:/\d{4})?\b", text or "")
        if len(dates) >= 2:
            start = parse_indian_date(dates[0])
            end = parse_indian_date(dates[1])
            if start and not result.get("period_from"):
                result["period_from"] = start
            if end and not result.get("period_to"):
                result["period_to"] = end
        return
    start = parse_indian_date(match.group(1))
    end = parse_indian_date(match.group(2))
    if start and not result.get("period_from"):
        result["period_from"] = start
    if end and not result.get("period_to"):
        result["period_to"] = end


def _vl_parse_has_mapped_stock_headers(result: Dict[str, Any]) -> bool:
    """True when HTML parse produced usable product + qty rows."""
    extra = ((result.get("totals") or {}).get("extra") or {})
    if not extra.get("table_available"):
        return False
    if extra.get("vl_html_unmapped_headers"):
        return False
    items = result.get("line_items") or []
    if not items:
        return False
    qty_keys = (
        "opening_qty",
        "closing_qty",
        "sales_qty",
        "receive_qty",
        "receipts_qty",
    )
    return any(
        isinstance(item, dict)
        and str(item.get("product_name") or "").strip()
        and any(item.get(k) is not None for k in qty_keys)
        for item in items
    )


def resolve_vl_or_text_extraction(
    ocr_text: str,
    *,
    filename: str = "",
    source_format: str = "",
    table_html: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Priority resolver: VL HTML table → plain OCR text → None (Gemini later).

    1. Non-empty ``table_html`` with mapped stock headers → ``parse_vl_html_table``
    2. Non-empty OCR text / unmapped HTML → ``statement_from_ocr_html``
    3. Otherwise → ``None`` so the caller may use Gemini only when OCR is empty
    """
    html = (table_html or "").strip() or extract_vl_table_html(ocr_text or "")
    if html:
        # Prefer full OCR text (banner + <table>) so identity/period survive.
        parse_src = html
        if ocr_text and "<table" in ocr_text.lower():
            parse_src = ocr_text
        result = parse_vl_html_table(
            parse_src, filename=filename, source_format=source_format
        )
        if _vl_parse_has_mapped_stock_headers(result):
            if ocr_text and ocr_text.strip() and parse_src is html:
                try:
                    from services.identity_disambiguation import disambiguate_identity

                    disambiguate_identity(result, ocr_text=ocr_text)
                    _attach_period_from_text(result, ocr_text)
                except Exception:
                    logger.debug("identity re-apply skipped", exc_info=True)
            return result

    text = (ocr_text or "").strip()
    if not text:
        return None

    from services.ocr.secondary_repair import statement_from_ocr_html

    return statement_from_ocr_html(
        text, filename=filename, source_format=source_format
    )
