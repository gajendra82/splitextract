"""Shared source representation for text, tables, rows, and cells.

Built from a reader that already opened the file. It records printed text,
headers, page or sheet, and coordinates only when the reader measured them.
It does not invent a value, a box, a language, or a handwriting judgement.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

_BLANK = {"", "-", "—", "--", "–", "NA", "N/A", "NULL", "NONE", "."}
_QTY_FIELDS = {
    "opening_qty",
    "receipts_qty",
    "sales_qty",
    "closing_qty",
    "total_stock",
    "shortage_qty",
    "expiry_breakage",
    "near_expiry_qty",
}
_MAX_ROWS = 200


def classify_cell(value: Any, *, quantity: bool) -> str:
    """Separate blank, printed zero, a number, text, and an unreadable token."""
    if value is None:
        return "blank"
    if isinstance(value, str):
        text = value.strip()
        if not text or text.upper() in _BLANK:
            return "blank"
    if isinstance(value, bool):
        return "unreadable" if quantity else "text"
    number = _number(value)
    if number is None:
        return "unreadable" if quantity else "text"
    if number == 0:
        return "explicit_zero"
    return "number"


def cell_record(
    value: Any,
    *,
    row: int,
    column: int,
    header: Optional[str],
    field: Optional[str],
    page: Optional[int] = None,
    sheet: Optional[str] = None,
    coordinates: Optional[Dict[str, float]] = None,
    present: bool = True,
) -> Dict[str, Any]:
    """One source cell. A missing column is not the same as a blank cell."""
    if not present:
        kind = "missing"
        text = None
    else:
        kind = classify_cell(value, quantity=field in _QTY_FIELDS)
        text = _printed(value)
    return {
        "text": text,
        "kind": kind,
        "row": row,
        "column": column,
        "header": header or None,
        "field": field,
        "page": page,
        "sheet": sheet,
        "coordinates": dict(coordinates) if coordinates else None,
    }


def table_record(
    *,
    reader: str,
    headers: List[Dict[str, Any]],
    rows: List[Dict[str, Any]],
    page: Optional[int] = None,
    sheet: Optional[str] = None,
    ambiguous_columns: Optional[Iterable[str]] = None,
    row_count: Optional[int] = None,
    truncated: bool = False,
) -> Dict[str, Any]:
    count = row_count if row_count is not None else len(rows)
    return {
        "reader": reader,
        "page": page,
        "sheet": sheet,
        "headers": headers,
        "rows": rows,
        "row_count": count,
        "truncated": truncated or count > len(rows),
        "ambiguous_columns": list(ambiguous_columns or []),
    }


def assemble_document(
    *,
    file_format: str,
    reader: Optional[str],
    tables: List[Dict[str, Any]],
    sufficient: bool,
    reason: Optional[str] = None,
    blocks_verification: bool = False,
) -> Dict[str, Any]:
    """Public document. Cell text is the retained source text."""
    sheets = []
    pages = []
    coordinates = False
    for table in tables:
        sheet = table.get("sheet")
        if sheet and sheet not in sheets:
            sheets.append(sheet)
        page = table.get("page")
        if page and page not in pages:
            pages.append(page)
        for row in table.get("rows") or []:
            for cell in row.get("cells") or []:
                if cell.get("coordinates"):
                    coordinates = True
        for cell in table.get("headers") or []:
            if cell.get("coordinates"):
                coordinates = True
    return {
        "format": file_format,
        "reader": reader,
        "tables": tables,
        "sheet_names": sheets,
        "pages": pages,
        "truncated": any(bool(table.get("truncated")) for table in tables),
        "evidence": {
            "sufficient": sufficient,
            "reason": reason,
            "blocks_verification": blocks_verification,
            "coordinates_available": coordinates,
            "text_retained": "cells",
            "script": "unspecified",
            "language": "unspecified",
        },
    }


def empty_document(
    file_format: str,
    reader: Optional[str],
    reason: str,
    *,
    blocks_verification: bool = False,
) -> Dict[str, Any]:
    return assemble_document(
        file_format=file_format,
        reader=reader,
        tables=[],
        sufficient=False,
        reason=reason,
        blocks_verification=blocks_verification,
    )


def limit_rows(rows: List[Any]) -> tuple:
    if len(rows) <= _MAX_ROWS:
        return rows, len(rows), False
    return rows[:_MAX_ROWS], len(rows), True


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).strip().replace(",", "")
    if not text or text.upper() in _BLANK:
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _printed(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
