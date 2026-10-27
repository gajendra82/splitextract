"""One promotion rule for every recovery response.

A canonical quantity is kept only when this request's trusted mapping names
a printed header and the model response includes that header's own value.
A bare canonical number, a valid identity equation, and a valid JSON schema
are not evidence that the model read the printed cell.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

from services.column_mapping import header_key
from services.focused_row_request import CANONICAL_FIELDS, clean_column_mapping

_FIXED_CANONICAL = tuple(field for field in CANONICAL_FIELDS if field != "stock_out_qty")
_MISSING_TEXT = {"", "-", "--", "—", "–", "na", "n/a", "null", "none"}
_RESERVED = set(CANONICAL_FIELDS) | {
    "product_name",
    "dynamic",
    "fixed",
    "line_items",
    "totals",
}


def project_recovery_candidate(
    model_output: Any,
    trusted_mapping: Optional[Mapping[str, str]] = None,
    evidence_headers: Optional[list] = None,
    evidence_context: Optional[Mapping[str, Any]] = None,
    existing_row: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Project one model response into canonical fields the merge may copy.

    ``existing_row`` is not copied into the candidate. Unpromoted fields stay
    null so the merge can keep the parser value. When ``evidence_headers`` is
    given, a mapping entry whose header is absent from that evidence is ignored.
    """
    del evidence_context, existing_row
    trusted_mapping = _mapping_for_evidence(trusted_mapping, evidence_headers)
    if isinstance(model_output, str):
        try:
            model_output = json.loads(model_output)
        except json.JSONDecodeError:
            return {}
    if not isinstance(model_output, dict):
        return {}
    if isinstance(model_output.get("line_items"), list):
        return project_sales_document(model_output, trusted_mapping)
    return _semantic_document(model_output, trusted_mapping)


def project_sales_document(
    document: Dict[str, Any],
    trusted_mapping: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    projected = dict(document)
    rows = []
    for item in document.get("line_items") or []:
        if not isinstance(item, dict):
            rows.append(item)
            continue
        fixed = item.get("fixed") if isinstance(item.get("fixed"), dict) else {}
        dynamic = item.get("dynamic") if isinstance(item.get("dynamic"), dict) else {}
        flat = dict(fixed)
        flat["dynamic"] = dynamic
        semantic = _semantic_document(flat, trusted_mapping)
        row = semantic["line_items"][0]
        kept = {
            key: value
            for key, value in fixed.items()
            if key not in _FIXED_CANONICAL and key != "stock_out_qty"
        }
        kept.update(row["fixed"])
        rows.append({"fixed": kept, "dynamic": row["dynamic"]})
    projected["line_items"] = rows
    return projected


def _semantic_document(
    data: Mapping[str, Any],
    trusted_mapping: Optional[Mapping[str, str]],
) -> Dict[str, Any]:
    mapping = clean_column_mapping(trusted_mapping)
    incoming = data.get("dynamic") if isinstance(data.get("dynamic"), dict) else {}
    fixed = {field: None for field in _FIXED_CANONICAL}
    product = data.get("product_name")
    fixed["product_name"] = product if isinstance(product, str) else None
    dynamic: Dict[str, Any] = {}
    consumed = set()

    for header, field in mapping.items():
        found, value = _header_value(header, data, incoming)
        if not found:
            continue
        consumed.add(header_key(header))
        if _missing(value):
            continue
        number = _number(value)
        if number is None:
            continue
        if field == "stock_out_qty":
            dynamic["stock_out_qty"] = number
        else:
            fixed[field] = number

    for source in (incoming, data):
        for key, value in source.items():
            if key in _RESERVED or header_key(str(key)) in consumed:
                continue
            if any(header_key(header) == header_key(str(key)) for header in mapping):
                continue
            if key not in dynamic:
                dynamic[key] = value
    return {"line_items": [{"fixed": fixed, "dynamic": dynamic}], "dynamic": {}}


def _mapping_for_evidence(
    trusted_mapping: Optional[Mapping[str, str]],
    evidence_headers: Optional[list],
) -> Dict[str, str]:
    mapping = clean_column_mapping(trusted_mapping)
    if evidence_headers is None:
        return mapping
    present = {header_key(header) for header in evidence_headers}
    return {
        header: field
        for header, field in mapping.items()
        if header_key(header) in present
    }


_EMPTY_SALES_JSON = '{"line_items":[]}'


def unwrap_model_text(text: str) -> str:
    """Remove one markdown fence so projection sees the same JSON the parser would.

    The recovery parser strips ```json fences before validation. Projection
    must do that first. Returning the fenced text unchanged lets the parser
    merge canonical numbers the guard never saw.
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[: -3].strip()
    return cleaned


def project_sales_text(text: str, trusted_mapping: Optional[Mapping[str, str]] = None) -> str:
    try:
        document = json.loads(unwrap_model_text(text) or "null")
    except json.JSONDecodeError:
        return _EMPTY_SALES_JSON
    if not isinstance(document, dict):
        return _EMPTY_SALES_JSON
    return json.dumps(project_sales_document(document, trusted_mapping))


def _header_value(header: str, data: Mapping[str, Any], dynamic: Mapping[str, Any]):
    wanted = header_key(header)
    for source in (data, dynamic):
        for key, value in source.items():
            if key in _RESERVED:
                continue
            if header_key(str(key)) == wanted:
                return True, value
    return False, None


def _missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().casefold() in _MISSING_TEXT:
        return True
    return False


def _number(value: Any):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text.casefold() in _MISSING_TEXT:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None
