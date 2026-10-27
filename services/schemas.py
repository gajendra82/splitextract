"""Sales-statement schema with missing values as null and printed zero as 0.

This module does not rewrite parser arithmetic. Project an existing dict
through normalize_sales_statement. The original API dict stays unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

_MISSING_TEXT = {
    "",
    "-",
    "—",
    "--",
    "–",
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

_LINE_NUMBER_FIELDS = (
    "opening_qty",
    "receipts_qty",
    "sales_qty",
    "closing_qty",
    "opening_value",
    "receipts_value",
    "sales_value",
    "closing_value",
)


class SchemaValidationError(ValueError):
    """A source token is neither empty nor a number."""


def parse_optional_number(value: Any, *, field: str = "value") -> Optional[float]:
    """Return null for a missing token, 0 for an explicit zero, else a float."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise SchemaValidationError(f"invalid numeric value for {field}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\u00a0", " ")
    if text.upper() in _MISSING_TEXT:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    compact = text.replace(",", "")
    try:
        number = float(compact)
    except ValueError as exc:
        raise SchemaValidationError(
            f"invalid numeric value for {field}: {value!r}"
        ) from exc
    if negative:
        return -abs(number)
    return number


def parse_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass
class SalesLineItem:
    product_name: Optional[str] = None
    product_code: Optional[str] = None
    opening_qty: Optional[float] = None
    receipts_qty: Optional[float] = None
    sales_qty: Optional[float] = None
    closing_qty: Optional[float] = None
    opening_value: Optional[float] = None
    receipts_value: Optional[float] = None
    sales_value: Optional[float] = None
    closing_value: Optional[float] = None

    @classmethod
    def from_mapping(cls, raw: Dict[str, Any]) -> "SalesLineItem":
        if not isinstance(raw, dict):
            raise SchemaValidationError("line item must be an object")
        numbers = {
            name: parse_optional_number(raw.get(name), field=name)
            for name in _LINE_NUMBER_FIELDS
        }
        return cls(
            product_name=parse_optional_text(raw.get("product_name")),
            product_code=parse_optional_text(raw.get("product_code")),
            **numbers,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "product_name": self.product_name,
            "product_code": self.product_code,
            "opening_qty": self.opening_qty,
            "receipts_qty": self.receipts_qty,
            "sales_qty": self.sales_qty,
            "closing_qty": self.closing_qty,
            "opening_value": self.opening_value,
            "receipts_value": self.receipts_value,
            "sales_value": self.sales_value,
            "closing_value": self.closing_value,
        }


def empty_sales_statement() -> Dict[str, Any]:
    return {
        "stockist_name": None,
        "period_from": None,
        "period_to": None,
        "line_items": [],
        "totals": {},
        "metadata": {},
    }


def normalize_sales_statement(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a null-safe projection. Does not mutate ``result``.

    Float 0 already stored by a parser stays 0. Only blank tokens and None
    become null. Invalid tokens become null and are listed in metadata.
    """
    if not isinstance(result, dict):
        projected = empty_sales_statement()
        projected["metadata"]["schema_errors"] = ["result_not_object"]
        projected["metadata"]["response_line_items_unchanged"] = True
        return projected

    errors: List[str] = []
    statements = result.get("statements")
    if result.get("multi_statement") and isinstance(statements, list):
        projected_statements = [
            _project_one(item, errors, prefix=f"statements[{index}]")
            for index, item in enumerate(statements)
            if isinstance(item, dict)
        ]
        projected = empty_sales_statement()
        projected["metadata"] = {
            "schema_errors": errors,
            "response_line_items_unchanged": True,
            "null_policy": "missing_is_null_explicit_zero_stays_zero",
            "multi_statement": True,
        }
        projected["statements"] = projected_statements
        return projected

    projected = _project_one(result, errors, prefix="")
    projected["metadata"]["schema_errors"] = errors
    projected["metadata"]["response_line_items_unchanged"] = True
    projected["metadata"]["null_policy"] = "missing_is_null_explicit_zero_stays_zero"
    return projected


def _project_one(result: Dict[str, Any], errors: List[str], prefix: str) -> Dict[str, Any]:
    items_raw = result.get("line_items")
    line_items: List[Dict[str, Any]] = []
    if isinstance(items_raw, list):
        for index, raw in enumerate(items_raw):
            label = f"{prefix}line_items[{index}]".lstrip(".")
            if not isinstance(raw, dict):
                errors.append(f"{label}: not_an_object")
                continue
            line_items.append(_project_line(raw, errors, label))
    elif items_raw is not None:
        errors.append(f"{prefix}line_items: not_a_list".lstrip("."))

    totals_raw = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    totals = {
        "sales_value": _lenient_number(totals_raw.get("sales_value"), "totals.sales_value", errors),
        "closing_value": _lenient_number(
            totals_raw.get("closing_value"), "totals.closing_value", errors
        ),
    }
    extra = totals_raw.get("extra") if isinstance(totals_raw.get("extra"), dict) else {}
    totals["dynamic"] = _dynamic_copy(extra)
    return {
        "stockist_name": parse_optional_text(result.get("stockist_name")),
        "period_from": parse_optional_text(result.get("period_from")),
        "period_to": parse_optional_text(result.get("period_to")),
        "line_items": line_items,
        "totals": totals,
        "metadata": {
            "extraction_method": extra.get("extraction_method"),
            "source_file": result.get("source_file"),
            "source_format": result.get("source_format"),
        },
    }


def _project_line(raw: Dict[str, Any], errors: List[str], label: str) -> Dict[str, Any]:
    item = SalesLineItem(
        product_name=parse_optional_text(raw.get("product_name")),
        product_code=parse_optional_text(raw.get("product_code")),
    )
    for name in _LINE_NUMBER_FIELDS:
        try:
            setattr(item, name, parse_optional_number(raw.get(name), field=name))
        except SchemaValidationError as exc:
            errors.append(f"{label}.{name}: {exc}")
            setattr(item, name, None)
    projected = item.to_dict()
    projected["dynamic"] = _dynamic_copy(raw.get("extra"))
    return projected


def _dynamic_copy(value: Any) -> Dict[str, Any]:
    """JSON-safe copy of parser ``extra``. Non-JSON values are omitted."""
    from services.hybrid_schema import HybridSchemaError, ensure_json_value

    if not isinstance(value, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        try:
            cleaned[key] = ensure_json_value(item)
        except HybridSchemaError:
            continue
    return cleaned


def _lenient_number(value: Any, field: str, errors: List[str]) -> Optional[float]:
    try:
        return parse_optional_number(value, field=field)
    except SchemaValidationError as exc:
        errors.append(f"{field}: {exc}")
        return None
