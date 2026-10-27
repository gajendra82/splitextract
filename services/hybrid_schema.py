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


def ensure_json_value(value: Any, path: str = "value") -> Any:
    """Return ``value`` when it is JSON data. Reject Python-only objects."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise HybridSchemaError(f"{path} is not a JSON number")
        return value
    if isinstance(value, list):
        return [ensure_json_value(item, f"{path}[]") for item in value]
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise HybridSchemaError(f"{path} keys must be strings")
            cleaned[key] = ensure_json_value(item, f"{path}.{key}")
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


_JSON_VALUE_SCHEMA = {
    "anyOf": [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
        {"type": "array", "items": {"type": ["string", "number", "boolean", "null", "object", "array"]}},
        {"type": "object", "additionalProperties": True},
    ]
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
            properties["dynamic"] = {
                "type": "object",
                "additionalProperties": _JSON_VALUE_SCHEMA,
            }
        for value in node.values():
            _mark_dynamic_objects(value)
    elif isinstance(node, list):
        for item in node:
            _mark_dynamic_objects(item)


__all__ = [
    "HybridSchemaError",
    "SalesExtractionOutput",
    "SalesHeaderFixed",
    "SalesLineFixed",
    "SalesLineOutput",
    "ValidationError",
    "ensure_json_value",
    "parse_recovery_number",
    "sales_recovery_json_schema",
]
