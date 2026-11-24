"""LLM recovery contract for sales statements.

This is not the public API, the parser dict, or the Phase 1 projection.
Null stays null. Explicit zero stays zero. Invalid numbers are rejected.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_MISSING_TEXT = {"", "-", "—", "--", "–", "NA", "N/A", "NULL", "NONE"}


class HybridSchemaError(ValueError):
    """A recovery payload is not valid JSON or not the sales contract."""


_MAX_JSON_DEPTH = 32


def ensure_json_value(value: Any, path: str = "value", *, _depth: int = 0) -> Any:
    """Return ``value`` when it is JSON data. Reject Python-only objects."""
    too_deep = _depth > _MAX_JSON_DEPTH or (
        _depth == _MAX_JSON_DEPTH and isinstance(value, (list, dict))
    )
    if too_deep:
        raise HybridSchemaError(f"{path} exceeds {_MAX_JSON_DEPTH} JSON levels")
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise HybridSchemaError(f"{path} is not a JSON number")
        return value
    if isinstance(value, list):
        return [
            ensure_json_value(item, f"{path}[]", _depth=_depth + 1) for item in value
        ]
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise HybridSchemaError(f"{path} keys must be strings")
            cleaned[key] = ensure_json_value(item, f"{path}.{key}", _depth=_depth + 1)
        return cleaned
    raise HybridSchemaError(f"{path} is not JSON compatible: {type(value).__name__}")


def parse_recovery_number(value: Any, field: str) -> Optional[float]:
    """Missing and blank tokens are null. Printed zero is 0. Garbage raises."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise HybridSchemaError(f"invalid numeric value for {field}")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (
            value != value or value in {float("inf"), float("-inf")}
        ):
            raise HybridSchemaError(f"invalid numeric value for {field}")
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace("\u00a0", " ")
        if text.upper() in _MISSING_TEXT:
            return None
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1].strip()
        try:
            number = float(text.replace(",", ""))
        except ValueError as exc:
            raise HybridSchemaError(
                f"invalid numeric value for {field}: {value!r}"
            ) from exc
        if number != number or number in {float("inf"), float("-inf")}:
            raise HybridSchemaError(f"invalid numeric value for {field}")
        return -abs(number) if negative else number
    raise HybridSchemaError(f"invalid numeric value for {field}")


class SalesLineFixed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_name: Optional[str] = None
    product_code: Optional[str] = None
    packing: Optional[str] = None
    opening_qty: Optional[float] = None
    receipts_qty: Optional[float] = None
    sales_qty: Optional[float] = None
    closing_qty: Optional[float] = None
    opening_value: Optional[float] = None
    receipts_value: Optional[float] = None
    sales_value: Optional[float] = None
    closing_value: Optional[float] = None

    @field_validator(
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "closing_qty",
        "opening_value",
        "receipts_value",
        "sales_value",
        "closing_value",
        mode="before",
    )
    @classmethod
    def _keep_null_and_zero(cls, value: Any, info) -> Optional[float]:
        return parse_recovery_number(value, str(info.field_name))


class SalesLineOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixed: SalesLineFixed
    dynamic: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("dynamic", mode="before")
    @classmethod
    def _dynamic_is_json(cls, value: Any) -> Dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise HybridSchemaError("dynamic must be an object")
        return ensure_json_value(value, "dynamic")


class SalesHeaderFixed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stockist_name: Optional[str] = None
    stockist_address: Optional[str] = None
    company_name: Optional[str] = None
    period_from: Optional[str] = None
    period_to: Optional[str] = None
    report_title: Optional[str] = None


class SalesExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    header: SalesHeaderFixed = Field(default_factory=SalesHeaderFixed)
    line_items: List[SalesLineOutput] = Field(default_factory=list)
    totals: Dict[str, Optional[float]] = Field(default_factory=dict)
    dynamic: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("totals", mode="before")
    @classmethod
    def _totals_are_numbers(cls, value: Any) -> Dict[str, Optional[float]]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise HybridSchemaError("totals must be an object")
        return {
            str(key): parse_recovery_number(item, f"totals.{key}")
            for key, item in value.items()
        }

    @field_validator("dynamic", mode="before")
    @classmethod
    def _top_dynamic_is_json(cls, value: Any) -> Dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise HybridSchemaError("dynamic must be an object")
        return ensure_json_value(value, "dynamic")


# Vertex Gemini response_json_schema rejects deeply nested anyOf trees under
# additionalProperties (HTTP 400 INVALID_ARGUMENT). Keep dynamic keys open with
# a shallow object schema; Pydantic still validates nested JSON on parse.
_GEMINI_DYNAMIC_OBJECT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}


def sales_recovery_json_schema() -> Dict[str, Any]:
    """JSON Schema for Gemini response_json_schema. Dynamic keys stay open."""
    schema = SalesExtractionOutput.model_json_schema()
    _mark_dynamic_objects(schema)
    return schema


def _mark_dynamic_objects(node: Any) -> None:
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict) and "dynamic" in properties:
            properties["dynamic"] = dict(_GEMINI_DYNAMIC_OBJECT_SCHEMA)
        for value in node.values():
            _mark_dynamic_objects(value)
    elif isinstance(node, list):
        for item in node:
            _mark_dynamic_objects(item)


_HEADER_FIELDS = frozenset({
    "stockist_name",
    "stockist_address",
    "company_name",
    "period_from",
    "period_to",
    "report_title",
})
_LINE_FIELDS = frozenset({
    "product_name",
    "product_code",
    "packing",
    "opening_qty",
    "receipts_qty",
    "sales_qty",
    "closing_qty",
    "opening_value",
    "receipts_value",
    "sales_value",
    "closing_value",
})
_PREDEFINED_FIELDS = _HEADER_FIELDS | _LINE_FIELDS
_LINE_NUMBERS = frozenset({
    "opening_qty",
    "receipts_qty",
    "sales_qty",
    "closing_qty",
    "opening_value",
    "receipts_value",
    "sales_value",
    "closing_value",
})


def _has_predefined_field(data: Any) -> bool:
    """True when a sales field appears where the sales contract expects it.

    A nested copy of a sales name inside an unknown object stays dynamic.
    """
    if not isinstance(data, dict):
        return False
    for key, value in data.items():
        if key in _PREDEFINED_FIELDS:
            return True
        if key in {"header", "fixed"} and isinstance(value, dict):
            if any(name in _PREDEFINED_FIELDS for name in value):
                return True
        if key == "totals" and isinstance(value, dict):
            if any(name in _PREDEFINED_FIELDS for name in value):
                return True
        if key == "line_items" and isinstance(value, list):
            if any(isinstance(row, dict) and _has_predefined_field(row) for row in value):
                return True
    return False


def _unknown_content(data: Dict[str, Any]) -> Dict[str, Any]:
    """Copy keys that are not predefined sales fields, keeping their structure."""
    unknown: Dict[str, Any] = {}
    for key, value in data.items():
        if key in _PREDEFINED_FIELDS:
            continue
        if key == "header" and isinstance(value, dict):
            extra = {name: item for name, item in value.items() if name not in _HEADER_FIELDS}
            if extra:
                unknown["header"] = extra
            continue
        if key == "fixed" and isinstance(value, dict):
            extra = {name: item for name, item in value.items() if name not in _PREDEFINED_FIELDS}
            if extra:
                unknown["fixed"] = extra
            continue
        if key == "totals" and isinstance(value, dict):
            extra = {name: item for name, item in value.items() if name not in _PREDEFINED_FIELDS}
            if extra:
                unknown["totals"] = extra
            continue
        if key == "line_items" and isinstance(value, list):
            rows = []
            for row in value:
                if not isinstance(row, dict):
                    rows.append(row)
                    continue
                row_unknown = _unknown_content(row)
                if row_unknown:
                    rows.append(row_unknown)
            if rows:
                unknown["line_items"] = rows
            continue
        unknown[key] = value
    return unknown


def _known_sales_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    header = {}
    if isinstance(data.get("header"), dict):
        header = {key: value for key, value in data["header"].items() if key in _HEADER_FIELDS}
    for key in _HEADER_FIELDS:
        if key in data:
            header[key] = data[key]
    rows = []
    source_rows = data.get("line_items")
    if isinstance(source_rows, list):
        for row in source_rows:
            if not isinstance(row, dict):
                continue
            fixed = row.get("fixed") if isinstance(row.get("fixed"), dict) else row
            known = {key: value for key, value in fixed.items() if key in _LINE_FIELDS}
            if known or _has_predefined_field(row):
                rows.append({"fixed": known, "dynamic": {}})
    elif any(key in data for key in _LINE_FIELDS):
        rows.append({
            "fixed": {key: data[key] for key in _LINE_FIELDS if key in data},
            "dynamic": {},
        })
    totals = {}
    raw_totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
    for key, value in raw_totals.items():
        if key in _PREDEFINED_FIELDS:
            totals[key] = value
    for key in _PREDEFINED_FIELDS:
        if key in data and key not in _HEADER_FIELDS and key not in _LINE_FIELDS:
            totals[key] = data[key]
    return {"header": header, "line_items": rows, "totals": totals, "dynamic": {}}


def _scrub_invalid_known(payload: Dict[str, Any]) -> List[str]:
    """Drop invalid predefined values. Keep a printed zero. Do not invent one."""
    errors: List[str] = []
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    for key, value in list(header.items()):
        if value is not None and not isinstance(value, str):
            errors.append(f"header.{key}: invalid_text")
            header.pop(key, None)
    for index, row in enumerate(payload.get("line_items") or []):
        fixed = row.get("fixed") if isinstance(row, dict) else None
        if not isinstance(fixed, dict):
            continue
        for key, value in list(fixed.items()):
            if key in _LINE_NUMBERS:
                try:
                    fixed[key] = parse_recovery_number(value, key)
                except HybridSchemaError as exc:
                    errors.append(f"line_items[{index}].{key}: {exc}")
                    fixed.pop(key, None)
            elif value is not None and not isinstance(value, str):
                errors.append(f"line_items[{index}].{key}: invalid_text")
                fixed.pop(key, None)
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    for key, value in list(totals.items()):
        try:
            totals[key] = parse_recovery_number(value, f"totals.{key}")
        except HybridSchemaError as exc:
            errors.append(f"totals.{key}: {exc}")
            totals.pop(key, None)
    return errors


def _without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_nulls(item) for item in value]
    return value


def accept_document(
    payload: Any,
    source_evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Classify one extracted document and preserve JSON that is not predefined.

    Predefined fields still use the sales models. A document with no predefined
    field is returned unchanged and is never marked verified.
    """
    document = ensure_json_value(payload, "document")
    evidence = None
    if source_evidence is not None:
        evidence = ensure_json_value(source_evidence, "source_evidence")
    if not isinstance(document, dict) or not _has_predefined_field(document):
        return {
            "schema_class": "fully_dynamic",
            "document": document,
            "fixed": None,
            "dynamic": document,
            "schema_errors": [],
            "verified": False,
            "verification_status": "UNRESOLVED",
            "verification_reason": "no_predefined_fields",
            "source_evidence": evidence,
        }
    unknown = _unknown_content(document)
    schema_class = "hybrid" if unknown else "predefined"
    known = _known_sales_payload(document)
    schema_errors = _scrub_invalid_known(known)
    model = SalesExtractionOutput.model_validate(known)
    fixed = _without_nulls(model.model_dump(mode="json"))
    fixed.pop("dynamic", None)
    for row in fixed.get("line_items") or []:
        if isinstance(row, dict):
            row.pop("dynamic", None)
    return {
        "schema_class": schema_class,
        "document": document,
        "fixed": fixed,
        "dynamic": unknown if schema_class == "hybrid" else {},
        "schema_errors": schema_errors,
        "verified": False,
        "verification_status": "UNRESOLVED",
        "verification_reason": (
            "dynamic_fields_not_verified" if schema_class == "hybrid" else "source_evidence_required"
        ),
        "source_evidence": evidence,
    }


def result_from_document(
    payload: Any,
    source_evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Parser-shaped result. Fully dynamic documents get no fabricated rows."""
    envelope = accept_document(payload, source_evidence=source_evidence)
    verification = {
        "status": envelope["verification_status"],
        "verified": False,
        "reason": envelope["verification_reason"],
    }
    extra: Dict[str, Any] = {
        "schema_class": envelope["schema_class"],
        "verification": verification,
    }
    if envelope["source_evidence"] is not None:
        extra["source_grounding"] = envelope["source_evidence"]
    if envelope["schema_errors"]:
        extra["schema_errors"] = envelope["schema_errors"]
    if envelope["schema_class"] == "fully_dynamic":
        extra["dynamic_document"] = envelope["document"]
        return {"line_items": [], "totals": {"extra": extra}}
    fixed = envelope["fixed"] or {}
    result: Dict[str, Any] = {}
    header = fixed.get("header") if isinstance(fixed.get("header"), dict) else {}
    result.update(header)
    lines = []
    unknown_rows = []
    line_extra: Dict[str, Any] = {}
    if envelope["schema_class"] == "hybrid":
        raw_rows = envelope["dynamic"].get("line_items")
        if isinstance(raw_rows, list):
            unknown_rows = raw_rows
        line_extra = {
            key: value
            for key, value in envelope["dynamic"].items()
            if key != "line_items"
        }
        extra["dynamic_document"] = envelope["dynamic"]
    for index, row in enumerate(fixed.get("line_items") or []):
        item = dict(row.get("fixed") or {})
        row_extra: Dict[str, Any] = {}
        if index < len(unknown_rows) and isinstance(unknown_rows[index], dict):
            row_extra.update(unknown_rows[index])
        if index == 0 and line_extra:
            row_extra.update(line_extra)
        if row_extra:
            item["extra"] = row_extra
        lines.append(item)
    result["line_items"] = lines
    totals = dict(fixed.get("totals") or {})
    totals["extra"] = extra
    result["totals"] = totals
    return result


__all__ = [
    "HybridSchemaError",
    "SalesExtractionOutput",
    "SalesHeaderFixed",
    "SalesLineFixed",
    "SalesLineOutput",
    "ValidationError",
    "accept_document",
    "ensure_json_value",
    "parse_recovery_number",
    "result_from_document",
    "sales_recovery_json_schema",
]
