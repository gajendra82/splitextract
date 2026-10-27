"""Generic Excel grid reader.

Maps columns from the header text on each sheet. Ambiguous headers are left
unmapped until a caller supplies a model interpretation of the original grid.
Quantities are not rewritten to satisfy the stock equation.
"""

from __future__ import annotations

import io
import json
import logging
import re
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_PHRASES = (
    (
        "product_name",
        (
            "product name",
            "item description",
            "item name",
            "material name",
            "particulars",
            "particular",
            "description",
            "medicine",
            "product",
            "material",
            "item",
        ),
    ),
    (
        "opening_qty",
        (
            "opening balance",
            "opening stock",
            "opening qty",
            "open qty",
            "op bal",
            "op qty",
            "opening",
        ),
    ),
    (
        "receipts_qty",
        (
            "purchase qty",
            "receipt qty",
            "received qty",
            "purchase",
            "receipts",
            "receipt",
            "received",
            "inward",
        ),
    ),
    (
        "sales_qty",
        (
            "sales qty",
            "sale qty",
            "issue qty",
            "sales",
            "sale",
            "issues",
            "issued",
            "outward",
        ),
    ),
    (
        "closing_qty",
        (
            "closing balance",
            "closing stock",
            "closing qty",
            "close qty",
            "balance qty",
            "bal qty",
            "cl stk",
            "closing",
        ),
    ),
    ("pack", ("pack size", "packing", "pack")),
)

_QTY_FIELDS = ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")


def _tokens(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return " ".join(text.split())


def _phrase_in(header: str, phrase: str) -> bool:
    if not header or not phrase:
        return False
    return header == phrase or f" {phrase} " in f" {header} "


def header_candidates(header: Any) -> List[str]:
    text = _tokens(header)
    if not text:
        return []
    hits: List[str] = []
    for field, phrases in _PHRASES:
        if any(_phrase_in(text, phrase) for phrase in phrases):
            hits.append(field)
    if hits:
        return hits
    if text in {"qty", "quantity", "balance", "stock", "bal"}:
        return ["opening_qty", "receipts_qty", "sales_qty", "closing_qty"]
    return []


def map_headers(headers: List[Any]) -> Dict[str, Any]:
    """Assign a header only when it matches one canonical field."""
    columns: Dict[int, str] = {}
    ambiguous: List[Dict[str, Any]] = []
    unmapped: List[Dict[str, Any]] = []
    claimed: Dict[str, int] = {}
    for index, header in enumerate(headers):
        hits = header_candidates(header)
        label = str(header or "").strip()
        if len(hits) == 1:
            field = hits[0]
            if field in claimed:
                ambiguous.append(
                    {
                        "index": claimed[field],
                        "header": str(headers[claimed[field]] or ""),
                        "candidates": [field],
                    }
                )
                ambiguous.append(
                    {"index": index, "header": label, "candidates": hits}
                )
                columns.pop(claimed[field], None)
                claimed.pop(field, None)
                continue
            columns[index] = field
            claimed[field] = index
        elif len(hits) > 1:
            ambiguous.append({"index": index, "header": label, "candidates": hits})
        elif label:
            unmapped.append({"index": index, "header": label})
    return {
        "columns": columns,
        "ambiguous": ambiguous,
        "unmapped": unmapped,
        "confident": "product_name" in columns.values() and not ambiguous,
    }


def _column_letter(index: int) -> str:
    number = index + 1
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _cell_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "—", "NA", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _header_score(row: List[Any]) -> int:
    return sum(1 for cell in row if len(header_candidates(cell)) == 1)


def _find_header(rows: List[List[Any]]) -> Optional[int]:
    best_index = None
    best_score = 1
    for index, row in enumerate(rows[:40]):
        score = _header_score(row)
        if score > best_score:
            best_index = index
            best_score = score
    return best_index


def _is_total_label(name: str) -> bool:
    return bool(re.fullmatch(r"(grand\s+)?total|sub\s*total", _tokens(name)))


def discrepancies_for_items(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compare printed closing with opening + receipts - sales. Do not edit rows."""
    findings: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        numbers = {field: _cell_number(item.get(field)) for field in _QTY_FIELDS}
        source = ((item.get("extra") or {}).get("source") or {})
        if any(numbers[field] is None for field in _QTY_FIELDS):
            findings.append(
                {
                    "product_name": item.get("product_name"),
                    "source": source,
                    "status": "incomplete",
                    "values": numbers,
                }
            )
            continue
        expected = round(
            numbers["opening_qty"] + numbers["receipts_qty"] - numbers["sales_qty"],
            2,
        )
        closing = round(numbers["closing_qty"], 2)
        if abs(expected - closing) > 0.05:
            findings.append(
                {
                    "product_name": item.get("product_name"),
                    "source": source,
                    "status": "stock_identity_failure",
                    "expected_closing": expected,
                    "closing_qty": closing,
                    "values": numbers,
                }
            )
    return findings


def _rows_from_mapping(
    sheet: str,
    rows: List[List[Any]],
    header_index: int,
    mapping: Dict[str, Any],
) -> List[Dict[str, Any]]:
    headers = list(rows[header_index])
    columns = {int(key): value for key, value in (mapping.get("columns") or {}).items()}
    items: List[Dict[str, Any]] = []
    for offset, row in enumerate(rows[header_index + 1 :]):
        row_number = header_index + offset + 2
        raw_cells = {}
        for index, value in enumerate(row):
            if value is None or value == "":
                continue
            raw_cells[_column_letter(index)] = value
        if not raw_cells:
            continue
        item: Dict[str, Any] = {
            "product_code": None,
            "product_name": None,
            "packing": None,
            "opening_qty": None,
            "receipts_qty": None,
            "sales_qty": None,
            "sales_value": None,
            "closing_qty": None,
            "closing_value": None,
            "extra": {
                "source": {"sheet": sheet, "row": row_number, "cells": {}},
                "raw_cells": raw_cells,
                "unmapped": {},
            },
        }
        for index, field in columns.items():
            value = row[index] if index < len(row) else None
            ref = f"{sheet}!{_column_letter(index)}{row_number}"
            item["extra"]["source"]["cells"][field] = ref
            if field == "product_name":
                item["product_name"] = str(value).strip() if value not in (None, "") else None
            elif field == "pack":
                item["packing"] = None if value in (None, "") else str(value).strip()
            elif field in _QTY_FIELDS:
                item[field] = _cell_number(value)
        for entry in mapping.get("unmapped") or []:
            index = int(entry["index"])
            if index < len(row) and row[index] not in (None, ""):
                item["extra"]["unmapped"][str(entry.get("header") or index)] = row[index]
        name = str(item.get("product_name") or "").strip()
        if not name or _is_total_label(name):
            continue
        items.append(item)
    return items


def apply_column_map(
    structure: Dict[str, Any],
    mapping: Dict[str, Any],
) -> List[Dict[str, Any]]:
    sheet = structure.get("sheet") or "Sheet1"
    rows = structure.get("rows") or []
    header_index = structure.get("header_index")
    if header_index is None or not rows:
        return []
    return _rows_from_mapping(str(sheet), rows, int(header_index), mapping)


def sheet_structures(file_bytes: bytes, ext: str) -> List[Dict[str, Any]]:
    ext = (ext or "").lower()
    structures: List[Dict[str, Any]] = []
    if ext == ".xls" and not file_bytes.startswith(b"PK"):
        import xlrd

        book = xlrd.open_workbook(file_contents=file_bytes)
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)
            rows = [
                [sheet.cell_value(row, col) for col in range(sheet.ncols)]
                for row in range(sheet.nrows)
            ]
            header_index = _find_header(rows)
            structures.append(
                {
                    "sheet": sheet.name,
                    "hidden": bool(getattr(sheet, "visibility", 0)),
                    "header_index": header_index,
                    "headers": list(rows[header_index]) if header_index is not None else [],
                    "sample_rows": rows[header_index + 1 : header_index + 4]
                    if header_index is not None
                    else [],
                    "rows": rows,
                }
            )
        return structures

    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    try:
        for sheet in workbook.worksheets:
            rows = [list(row) for row in sheet.iter_rows(values_only=True)]
            header_index = _find_header(rows)
            structures.append(
                {
                    "sheet": sheet.title,
                    "hidden": sheet.sheet_state != "visible",
                    "header_index": header_index,
                    "headers": list(rows[header_index]) if header_index is not None else [],
                    "sample_rows": rows[header_index + 1 : header_index + 4]
                    if header_index is not None
                    else [],
                    "rows": rows,
                }
            )
    finally:
        workbook.close()
    return structures


def _mapping_prompt(structure: Dict[str, Any]) -> str:
    payload = {
        "sheet": structure.get("sheet"),
        "headers": [
            {"index": index, "header": value}
            for index, value in enumerate(structure.get("headers") or [])
        ],
        "sample_rows": structure.get("sample_rows") or [],
        "fields": ["product_name", *_QTY_FIELDS, "pack"],
    }
    return (
        "Map these original Excel headers to the fields. "
        "Return JSON only: {\"columns\": {\"<index>\": \"<field>\"}}. "
        "Omit indexes you cannot assign. Do not calculate quantities.\n"
        + json.dumps(payload, default=str)
    )


def _parse_model_mapping(text: str) -> Optional[Dict[str, Any]]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[-1]
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    columns = payload.get("columns") if isinstance(payload, dict) else None
    if not isinstance(columns, dict):
        return None
    allowed = {"product_name", *_QTY_FIELDS, "pack"}
    cleaned: Dict[int, str] = {}
    for key, field in columns.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if field in allowed and field not in cleaned.values():
            cleaned[index] = str(field)
    if "product_name" not in cleaned.values():
        return None
    header_index = payload.get("header_index")
    try:
        header_index = int(header_index) if header_index is not None else None
    except (TypeError, ValueError):
        header_index = None
    return {
        "columns": cleaned,
        "ambiguous": [],
        "unmapped": [],
        "confident": True,
        "header_index": header_index,
    }


def _grid_rows(structure: Dict[str, Any], limit: int = 20) -> List[Dict[str, Any]]:
    preview = []
    for index, row in enumerate((structure.get("rows") or [])[:limit]):
        cells = []
        for cell in list(row)[:15]:
            if cell is None or cell == "":
                cells.append(None)
            else:
                cells.append(cell if isinstance(cell, (int, float)) else str(cell)[:80])
        if any(cell is not None for cell in cells):
            preview.append({"index": index, "cells": cells})
    return preview


def _unrecognized_grid_prompt(structure: Dict[str, Any]) -> str:
    payload = {
        "sheet": structure.get("sheet"),
        "rows": _grid_rows(structure),
        "fields": ["product_name", *_QTY_FIELDS, "pack"],
    }
    return (
        "The header row was not recognized. Choose it from these original Excel cells. "
        "Return JSON only: {\"header_index\": <row index>, \"columns\": {\"<column index>\": \"<field>\"}}. "
        "Omit columns you cannot assign. Do not calculate quantities.\n"
        + json.dumps(payload, default=str)
    )


def _with_unmapped(structure: Dict[str, Any], interpreted: Dict[str, Any]) -> Dict[str, Any]:
    rows = structure.get("rows") or []
    header_index = interpreted.get("header_index")
    if header_index is None:
        header_index = structure.get("header_index")
    try:
        header_index = int(header_index) if header_index is not None else None
    except (TypeError, ValueError):
        header_index = None
    if header_index is None or header_index < 0 or header_index >= len(rows):
        interpreted["header_index"] = None
        interpreted["confident"] = False
        return interpreted
    assigned = {int(index) for index in (interpreted.get("columns") or {})}
    interpreted["header_index"] = header_index
    interpreted["unmapped"] = [
        {"index": index, "header": str(cell or "").strip()}
        for index, cell in enumerate(rows[header_index])
        if index not in assigned and str(cell or "").strip()
    ]
    interpreted["ambiguous"] = []
    interpreted["confident"] = "product_name" in (interpreted.get("columns") or {}).values()
    return interpreted


def _empty_mapping() -> Dict[str, Any]:
    return {
        "columns": {},
        "ambiguous": [],
        "unmapped": [],
        "confident": False,
        "header_index": None,
    }


def resolve_mapping(
    structure: Dict[str, Any],
    invoke: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    header_index = structure.get("header_index")
    if header_index is None:
        if invoke is None or not _grid_rows(structure):
            return _empty_mapping()
        interpreted = _parse_model_mapping(invoke(_unrecognized_grid_prompt(structure)))
        if not interpreted:
            return _empty_mapping()
        return _with_unmapped(structure, interpreted)

    mapping = map_headers(list(structure.get("headers") or []))
    mapping["header_index"] = int(header_index)
    if mapping["confident"] or invoke is None or not mapping["ambiguous"]:
        return mapping
    interpreted = _parse_model_mapping(invoke(_mapping_prompt(structure)))
    if not interpreted:
        return mapping
    if interpreted.get("header_index") is None:
        interpreted["header_index"] = int(header_index)
    assigned = set(interpreted["columns"])
    interpreted["unmapped"] = [
        entry
        for entry in list(mapping.get("unmapped") or []) + list(mapping.get("ambiguous") or [])
        if int(entry["index"]) not in assigned
    ]
    return interpreted


def extract_workbook(
    file_bytes: bytes,
    filename: str,
    ext: str,
    *,
    invoke: Optional[Callable[[str], str]] = None,
) -> Optional[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    sheets_meta: List[Dict[str, Any]] = []
    ambiguous = False
    for structure in sheet_structures(file_bytes, ext):
        mapping = resolve_mapping(structure, invoke)
        header_index = mapping.get("header_index")
        if header_index is None:
            header_index = structure.get("header_index")
        if header_index is None:
            continue
        structure = dict(structure)
        structure["header_index"] = int(header_index)
        ambiguous = ambiguous or not mapping.get("confident")
        parsed = apply_column_map(structure, mapping)
        items.extend(parsed)
        sheets_meta.append(
            {
                "sheet": structure.get("sheet"),
                "hidden": structure.get("hidden"),
                "header_index": structure.get("header_index"),
                "confident": mapping.get("confident"),
                "ambiguous": mapping.get("ambiguous"),
                "unmapped": mapping.get("unmapped"),
            }
        )
    if not items:
        return None
    findings = discrepancies_for_items(items)
    failures = [row for row in findings if row["status"] == "stock_identity_failure"]
    result = {
        "source_file": filename,
        "source_format": (ext or "").lstrip("."),
        "stockist_name": None,
        "stockist_address": None,
        "company_name": None,
        "period_from": None,
        "period_to": None,
        "report_title": None,
        "line_items": items,
        "totals": {
            "sales_value": None,
            "closing_value": None,
            "extra": {
                "extraction_method": "generic_excel_grid",
                "mapping_ambiguous": ambiguous,
                "sheets": sheets_meta,
                "validation_warnings": ["stock_identity_failure"] if failures else [],
                "stock_identity_discrepancies": findings,
                "stock_identity_fail_count": len(failures),
                "stock_identity_kind": "opening_receipts_sales_closing",
                "stock_identity_formula": "closing = opening + receipts - sales",
            },
        },
    }
    return result


def vertex_column_invoke(prompt: str) -> str:
    """Ask Gemini to label original headers. The prompt is the sheet grid, not extracted rows."""
    import os

    from services.sales_extraction_runtime import sales_generate_content_via_vertex

    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip() or "gemini-2.5-flash-lite"
    response = sales_generate_content_via_vertex(
        model=model,
        payload={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 2048},
        },
        timeout=60,
        label="excel_column_mapping",
    )
    payload = response.json() if hasattr(response, "json") else response
    if not isinstance(payload, dict):
        return str(payload or "")
    candidates = payload.get("candidates") or [{}]
    content = (candidates[0] or {}).get("content") or {}
    parts = content.get("parts") or [{}]
    return str((parts[0] or {}).get("text") or "")
