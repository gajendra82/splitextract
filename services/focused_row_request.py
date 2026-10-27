"""Gemini request used by the focused-row crop diagnostic.

Production targeted stock recovery imports this. Do not copy the prompt
or schema anywhere else.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

FOCUSED_ROW_MODEL = "gemini-2.5-flash-lite"
FOCUSED_ROW_MIME = "image/png"

CANONICAL_QTY_FIELDS = (
    "opening_qty",
    "receipts_qty",
    "sales_qty",
    "stock_out_qty",
    "closing_qty",
)
CANONICAL_VALUE_FIELDS = (
    "opening_value",
    "receipts_value",
    "sales_value",
    "closing_value",
)
CANONICAL_FIELDS = CANONICAL_QTY_FIELDS + CANONICAL_VALUE_FIELDS

# Generic reader contract. Header names are added only from a supplied mapping.
EXTRACTION_CONTRACT = (
    "Read only the supplied evidence for this one row.\n"
    "Copy each visible value exactly as it is printed.\n"
    "Do not calculate a missing quantity.\n"
    "Do not infer a value from arithmetic.\n"
    "Do not reconcile a contradictory printed value.\n"
    "Do not use a neighboring row to fill a missing value.\n"
    "Do not use a stock identity equation to fill a missing value.\n"
    "Return null when a value is not visible or not readable.\n"
    "A printed zero is 0. A blank cell, a dash, or a missing cell is null.\n"
    "Do not turn a missing number into zero.\n"
    "Put every printed column that is not in the column mapping into dynamic, "
    "using the printed header as the key.\n"
    "Do not rename an unmapped column into a canonical quantity field."
)

PRINTED_PROMPT = (
    "Read the printed cells exactly as they appear in this table row.\n"
    "\n"
    "Transcribe exactly what is physically printed in these columns:\n"
    "Op\n"
    "Pur\n"
    "Sale\n"
    "SS Sp Qty\n"
    "Bal\n"
    "\n"
    "SS Sp Qty is the narrow printed column between Sale and Sale Val.\n"
    "Copy the printed digits in each named cell.\n"
    "Do not calculate any value.\n"
    "Do not use the stock equation.\n"
    "Do not infer a missing value.\n"
    "Return null only if that printed cell is genuinely unreadable or absent."
)

SEMANTIC_SCHEMA = {
    "type": "object",
    "title": "SemanticRow",
    "properties": {
        "product_name": {"type": ["string", "null"]},
        "opening_qty": {"type": ["number", "null"]},
        "receipts_qty": {"type": ["number", "null"]},
        "sales_qty": {"type": ["number", "null"]},
        "stock_out_qty": {"type": ["number", "null"]},
        "closing_qty": {"type": ["number", "null"]},
        "dynamic": {
            "type": "object",
            "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
        },
    },
    "required": [
        "product_name",
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "stock_out_qty",
        "closing_qty",
    ],
}

PRINTED_SCHEMA = {
    "type": "object",
    "title": "PrintedColumns",
    "properties": {
        "product_name": {"type": ["string", "null"]},
        "Op": {"type": ["number", "null"]},
        "Pur": {"type": ["number", "null"]},
        "Sale": {"type": ["number", "null"]},
        "SS Sp Qty": {"type": ["number", "null"]},
        "Bal": {"type": ["number", "null"]},
    },
    "required": [
        "product_name",
        "Op",
        "Pur",
        "Sale",
        "SS Sp Qty",
        "Bal",
    ],
}


def clean_column_mapping(column_mapping: Any) -> Dict[str, str]:
    """Keep header -> canonical field pairs. Unknown targets are ignored."""
    if not isinstance(column_mapping, dict):
        return {}
    cleaned: Dict[str, str] = {}
    for header, field in column_mapping.items():
        if not isinstance(header, str) or not header.strip():
            continue
        if not isinstance(field, str) or field not in CANONICAL_FIELDS:
            continue
        cleaned[header.strip()] = field
    return cleaned


def extraction_contract(
    column_mapping: Any = None,
    headers: Any = None,
    row_label: Any = None,
) -> str:
    """Reader instructions. A mapping is printed only when one was supplied."""
    lines = [EXTRACTION_CONTRACT]
    label = str(row_label or "").strip()
    if label:
        lines.append("Target row:\n" + label)
    printed = [str(header).strip() for header in (headers or []) if str(header).strip()]
    if printed:
        lines.append("Printed headers:\n" + " | ".join(printed))
    allowed = clean_column_mapping(column_mapping)
    if allowed:
        lines.append("Column mapping:")
        for header, field in allowed.items():
            lines.append("%s = %s" % (header, field))
        lines.append("Only these printed headers may be copied into canonical fields.")
    else:
        lines.append(
            "No column mapping was supplied. "
            "Leave canonical quantity fields null unless that field is explicitly mapped. "
            "Put each printed column in dynamic under its printed header."
        )
    return "\n".join(lines)


SEMANTIC_PROMPT = extraction_contract()


def mapping_declared_in_prompt(prompt: str) -> Dict[str, str]:
    """Read the mapping lines this request actually sent. Nothing else is trusted."""
    found: Dict[str, str] = {}
    collecting = False
    for line in (prompt or "").splitlines():
        if line.strip() == "Column mapping:":
            collecting = True
            continue
        if not collecting:
            continue
        if not line.strip() or line.startswith("Only these"):
            break
        if " = " not in line:
            break
        header, field = line.split(" = ", 1)
        header = header.strip()
        field = field.strip()
        if header and field in CANONICAL_FIELDS:
            found[header] = field
    return found


def project_semantic_row(data: Dict[str, Any], column_mapping: Any = None) -> Dict[str, Any]:
    """Copy a row the model read. Do not calculate a missing quantity.

    The shared projection guard is the only promotion rule. A canonical number
    is kept only when the mapped printed header is present in this response.
    """
    from services.recovery_projection import project_recovery_candidate

    return project_recovery_candidate(data, column_mapping)


def build_focused_row_payload(
    png_bytes: bytes,
    *,
    kind: str = "semantic",
    column_mapping: Any = None,
    headers: Any = None,
    row_label: Any = None,
) -> Dict[str, Any]:
    """Request body for one evidence image. Does not locate or crop the row."""
    if kind == "printed":
        prompt, schema = PRINTED_PROMPT, PRINTED_SCHEMA
    else:
        prompt, schema = extraction_contract(column_mapping, headers, row_label), SEMANTIC_SCHEMA
    return {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": FOCUSED_ROW_MIME,
                            "data": png_bytes,
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
        },
    }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_fingerprint(
    payload: Dict[str, Any],
    *,
    product: str,
    width: int,
    height: int,
) -> Dict[str, Any]:
    parts = payload["contents"][0]["parts"]
    prompt = parts[0]["text"]
    inline = parts[1]["inline_data"]
    raw = inline["data"]
    if isinstance(raw, str):
        import base64

        raw = base64.b64decode(raw)
    schema = payload["generationConfig"]["responseJsonSchema"]
    schema_bytes = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "product": product,
        "crop_width": width,
        "crop_height": height,
        "mime": inline.get("mime_type"),
        "crop_sha256": _sha256(raw),
        "prompt_sha256": _sha256(prompt.encode("utf-8")),
        "schema_sha256": _sha256(schema_bytes),
        "model": FOCUSED_ROW_MODEL,
    }
