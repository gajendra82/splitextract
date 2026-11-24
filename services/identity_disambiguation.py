"""Disambiguate stockist_name vs company_name for Indian stock statements."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_STOCKIST_HINT = re.compile(
    r"(?i)\b(distributors?|pharmacy|pharma(?:ceuticals?)?|traders?|suppliers?|"
    r"agencies|agency|medicals?|medico|stores?|chemists?)\b"
)
_COMPANY_HINT = re.compile(
    r"(?i)\b(co\.?|company|limited|ltd\.?|pvt\.?|private|inc\.?|labs?|"
    r"laboratories|drugs?|wellness)\b"
)
_DIVISION_HINT = re.compile(
    r"(?i)\b(div\.?|division)\b|HIM-?[A-Z0-9-]*DIVI?"
)
_FROM_TO = re.compile(r"(?i)^\s*(?:from|to|m/s|messrs?|the)\b")
_PERIOD_ONLY = re.compile(
    r"(?i)^\s*(?:from\s*:?\s*)?\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
    r"\s*(?:to|-|–)\s*\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\s*$"
)
_STATEMENT_OF = re.compile(
    r"(?i)stock\s*(?:&|and)?\s*sales\s*statement\s+of\s+(.+)$"
)
_STOCK_SALES_FOR_COMPANY = re.compile(
    r"(?i)stock\s*(?:&|and)?\s*sales\b.{0,120}?\bfor\s*:?\s*(.+)$"
)
_TITLE_NOISE = re.compile(
    r"(?i)stock\s*(?:&|and)?\s*sales|statement|page\s*no|from\s+\d|upto|period"
)
_STATUS_OR_FILE_NOISE = re.compile(
    r"(?i)^\d{1,2}:\d{2}\b"  # phone clock "10:38 Tue…"
    r"|^\d{5,}\.pdf\b"  # screenshot PDF title
    r"|^(?:page\s*:?\s*\d+)\s*$"
    r"|^\d+%\s*$"
)


def disambiguate_identity(
    result: Dict[str, Any],
    *,
    header_lines: Optional[List[str]] = None,
    ocr_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply positional / lexical rules; mutate and return ``result``."""
    if not isinstance(result, dict):
        return result

    lines = list(header_lines or [])
    if not lines and ocr_text:
        lines = _header_candidate_lines(ocr_text)
    if not lines:
        lines = _lines_from_result(result)

    stockist, company, division = _pick_names(lines, result)
    if stockist:
        result["stockist_name"] = stockist
    if company and not _PERIOD_ONLY.match(str(company)):
        result["company_name"] = company
    elif company and _PERIOD_ONLY.match(str(company)):
        # Clear period strings previously mistaken for company.
        result["company_name"] = None
    if division:
        extra = result.setdefault("totals", {}).setdefault("extra", {})
        if isinstance(extra, dict) and not extra.get("division"):
            extra["division"] = division

    # Swap if clearly reversed (e.g. company=OM PHARMA, stockist=HIMALAYA …).
    s = str(result.get("stockist_name") or "")
    c = str(result.get("company_name") or "")
    if s and c and _COMPANY_HINT.search(s) and _STOCKIST_HINT.search(c):
        result["stockist_name"], result["company_name"] = c, s
        logger.info(
            "identity_disambiguation swapped stockist/company → stockist=%s company=%s",
            result.get("stockist_name"),
            result.get("company_name"),
        )
    elif (
        s
        and c
        and _STOCKIST_HINT.search(c)
        and not _STOCKIST_HINT.search(s)
        and _COMPANY_HINT.search(s)
    ):
        result["stockist_name"], result["company_name"] = c, s
        logger.info(
            "identity_disambiguation swapped pharma-as-company → stockist=%s company=%s",
            result.get("stockist_name"),
            result.get("company_name"),
        )
    elif stockist or company:
        logger.info(
            "identity_disambiguation stockist=%s company=%s",
            result.get("stockist_name"),
            result.get("company_name"),
        )
    return result


def _lines_from_result(result: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for key in ("stockist_name", "company_name", "report_title"):
        val = result.get(key)
        if val:
            out.append(str(val))
    extra = ((result.get("totals") or {}).get("extra") or {})
    html = str(extra.get("ocr_table_html") or "")
    # Prefatory text before first <table> often holds the stockist banner.
    if "<table" in html.lower():
        prefix = html[: html.lower().find("<table")]
        out.extend(_header_candidate_lines(prefix))
    return out


def _header_candidate_lines(text: str) -> List[str]:
    lines: List[str] = []
    for raw in str(text or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or len(line) < 3:
            continue
        # Skip HTML markup / table openers (not identity banner text).
        if line.startswith("<") or "</" in line or line.lower().startswith("table"):
            continue
        if _TITLE_NOISE.fullmatch(line):
            continue
        if _STATUS_OR_FILE_NOISE.search(line):
            continue
        if re.fullmatch(r"[\d\s/.\-]+", line):
            continue
        lines.append(line)
        if len(lines) >= 12:
            break
    return lines


def _pick_names(
    lines: List[str], result: Dict[str, Any]
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    stockist: Optional[str] = str(result.get("stockist_name") or "").strip() or None
    company: Optional[str] = str(result.get("company_name") or "").strip() or None
    division: Optional[str] = None
    extra = ((result.get("totals") or {}).get("extra") or {})
    if isinstance(extra, dict) and extra.get("division"):
        division = str(extra.get("division") or "").strip() or None

    for line in lines:
        m = _STATEMENT_OF.search(line)
        if m:
            stockist = _clean(m.group(1))
            break

    for index, line in enumerate(lines):
        cleaned = _clean(line)
        if not cleaned:
            continue
        # "Stock & Sales from … for : HIMALAYA WELLNESS COMPANY (DIV.: …)"
        for_m = _STOCK_SALES_FOR_COMPANY.search(cleaned)
        if for_m:
            raw_co = _clean(for_m.group(1))
            div_m = re.search(
                r"(?i)(?:div\.?\s*:?\s*|division\s*:?\s*)([A-Z0-9_.\-/ ]{3,})",
                raw_co,
            )
            if div_m:
                division = division or _clean(div_m.group(1)).strip("() ")
            cleaned_co = re.sub(r"(?i)\s*\(?\s*div\.?.*$", "", raw_co).strip(" -()")
            if cleaned_co and (
                not company
                or (
                    _STOCKIST_HINT.search(company)
                    and not _STOCKIST_HINT.search(cleaned_co)
                )
            ):
                company = cleaned_co
            continue
        # Title lines
        if _TITLE_NOISE.search(cleaned) and not _STOCKIST_HINT.search(cleaned):
            continue
        if _PERIOD_ONLY.match(cleaned) or _PERIOD_ONLY.match(
            _FROM_TO.sub("", cleaned).strip(" :,-")
        ):
            # "From: 01/08/2026 To 31/08/2026" is a period, never company_name.
            continue
        if _DIVISION_HINT.search(cleaned) and (
            "div" in cleaned.casefold() or "divi" in cleaned.casefold()
        ):
            # Keep full banner; also stash division token when present.
            div_m = re.search(
                r"(?i)(?:div\.?\s*:?\s*|division\s*:?\s*)([A-Z0-9_.\-/ ]{3,})",
                cleaned,
            )
            if div_m:
                division = division or _clean(div_m.group(1)).strip("() ")
            if _COMPANY_HINT.search(cleaned):
                # "HIMALAYA WELLNESS COMPANY (DIV.: HIM-OTX-DIVI)"
                cleaned_co = re.sub(
                    r"(?i)\s*\(?\s*div\.?.*$", "", cleaned
                ).strip(" -()")
                if not company or (
                    _STOCKIST_HINT.search(company)
                    and not _STOCKIST_HINT.search(cleaned_co)
                ):
                    company = cleaned_co
            continue
        if _FROM_TO.match(cleaned):
            # "M/s Himalaya…" / "From: …" → company when looks corporate
            name = _clean(_FROM_TO.sub("", cleaned).strip(" :,-"))
            if name and _PERIOD_ONLY.match(name):
                continue
            if name and (_COMPANY_HINT.search(name) or not company):
                company = company or name
            continue
        if _STOCKIST_HINT.search(cleaned) and not _COMPANY_HINT.search(cleaned):
            if not stockist or index == 0:
                stockist = cleaned
            continue
        if _COMPANY_HINT.search(cleaned):
            # Prefer a true company banner over a stockist-looking company_name.
            if not company or (
                _STOCKIST_HINT.search(company) and not _STOCKIST_HINT.search(cleaned)
            ):
                company = cleaned
            continue
        # First non-title header line → stockist by position
        # (OM PHARMA-style banners often sit above the company line).
        if not stockist and not _STATUS_OR_FILE_NOISE.search(cleaned):
            # Prefer early banner lines that are not corporate "company" names.
            if not _COMPANY_HINT.search(cleaned) or _STOCKIST_HINT.search(cleaned):
                stockist = cleaned
                continue

    # If company still looks like a stockist and we have a stockist slot empty,
    # demote it.
    if company and _STOCKIST_HINT.search(company) and not _COMPANY_HINT.search(company):
        if not stockist:
            stockist = company
        if stockist == company:
            company = None

    # Prefer a later stockist-hint line over a weak first banner (clock/PDF).
    if stockist and _STATUS_OR_FILE_NOISE.search(stockist):
        stockist = None
        for line in lines:
            cleaned = _clean(line)
            if (
                cleaned
                and _STOCKIST_HINT.search(cleaned)
                and not _COMPANY_HINT.search(cleaned)
            ):
                stockist = cleaned
                break

    return stockist, company, division


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip(" -\t")
