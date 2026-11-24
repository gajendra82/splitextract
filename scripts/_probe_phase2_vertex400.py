"""Bisect Vertex 400 INVALID_ARGUMENT for Phase 2 responseJsonSchema.

Writes redacted request metadata + full error body (no credentials).
Does not change production code.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

OUT = ROOT / "phase4" / "phase2_ab_gt20" / "schema_probe"
OUT.mkdir(parents=True, exist_ok=True)


def _strip_defaults(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: _strip_defaults(v)
            for k, v in node.items()
            if k != "default"
        }
    if isinstance(node, list):
        return [_strip_defaults(x) for x in node]
    return node


def _simplify_dynamic(node: Any, mode: str) -> Any:
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict) and "dynamic" in props:
            if mode == "additional_true":
                props["dynamic"] = {"type": "object", "additionalProperties": True}
            elif mode == "leaf_types":
                props["dynamic"] = {
                    "type": "object",
                    "additionalProperties": {
                        "type": ["string", "number", "boolean", "null"]
                    },
                }
            elif mode == "shallow_anyof":
                props["dynamic"] = {
                    "type": "object",
                    "additionalProperties": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "number"},
                            {"type": "boolean"},
                            {"type": "null"},
                            {"type": "object"},
                            {"type": "array"},
                        ]
                    },
                }
        for v in node.values():
            _simplify_dynamic(v, mode)
    elif isinstance(node, list):
        for item in node:
            _simplify_dynamic(item, mode)
    return node


def _inline_refs(schema: Dict[str, Any]) -> Dict[str, Any]:
    defs = schema.get("$defs") or {}

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node and len(node) == 1:
                ref = node["$ref"]
                name = ref.split("/")[-1]
                if name in defs:
                    return resolve(copy.deepcopy(defs[name]))
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    out = resolve(copy.deepcopy(schema))
    out.pop("$defs", None)
    return out


def _variants() -> List[Tuple[str, Dict[str, Any]]]:
    from services.hybrid_schema import sales_recovery_json_schema

    base = sales_recovery_json_schema()
    variants: List[Tuple[str, Dict[str, Any]]] = []
    variants.append(("raw_pydantic", copy.deepcopy(base)))
    variants.append(("strip_defaults", _strip_defaults(copy.deepcopy(base))))
    for mode in ("additional_true", "leaf_types", "shallow_anyof"):
        s = _strip_defaults(copy.deepcopy(base))
        _simplify_dynamic(s, mode)
        variants.append((f"strip_defaults+{mode}", s))
    s = _strip_defaults(copy.deepcopy(base))
    _simplify_dynamic(s, "additional_true")
    variants.append(("inline_refs+strip+additional_true", _inline_refs(s)))
    # Minimal hand-built contract matching SalesExtractionOutput shape
    minimal = {
        "title": "SalesExtractionOutput",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "header": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "stockist_name": {"type": ["string", "null"]},
                    "stockist_address": {"type": ["string", "null"]},
                    "company_name": {"type": ["string", "null"]},
                    "period_from": {"type": ["string", "null"]},
                    "period_to": {"type": ["string", "null"]},
                    "report_title": {"type": ["string", "null"]},
                },
            },
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "fixed": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "product_name": {"type": ["string", "null"]},
                                "product_code": {"type": ["string", "null"]},
                                "packing": {"type": ["string", "null"]},
                                "opening_qty": {"type": ["number", "null"]},
                                "receipts_qty": {"type": ["number", "null"]},
                                "sales_qty": {"type": ["number", "null"]},
                                "closing_qty": {"type": ["number", "null"]},
                                "opening_value": {"type": ["number", "null"]},
                                "receipts_value": {"type": ["number", "null"]},
                                "sales_value": {"type": ["number", "null"]},
                                "closing_value": {"type": ["number", "null"]},
                            },
                        },
                        "dynamic": {
                            "type": "object",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["fixed"],
                },
            },
            "totals": {
                "type": "object",
                "additionalProperties": {"type": ["number", "null"]},
            },
            "dynamic": {"type": "object", "additionalProperties": True},
        },
    }
    variants.append(("hand_minimal", minimal))
    return variants


def _call(schema: Dict[str, Any], label: str) -> Dict[str, Any]:
    from services.vertex_gemini_client import (
        generate_content_via_vertex,
        get_vertex_gemini_client,
        payload_to_generate_config,
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "Return JSON for one secondary-sales row. "
                            "product_name=TEST TAB opening_qty=1 receipts_qty=0 "
                            "sales_qty=0 closing_qty=1. Use null for missing values."
                        )
                    }
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
    meta = {
        "label": label,
        "model": "gemini-2.5-flash-lite",
        "endpoint_host": "aiplatform.googleapis.com",
        "responseMimeType": "application/json",
        "schema_title": schema.get("title"),
        "schema_bytes": len(json.dumps(schema)),
        "schema_has_$defs": "$defs" in schema,
        "schema_has_default": "default" in json.dumps(schema),
        "schema_anyOf_count": json.dumps(schema).count("anyOf"),
        "parts": [{"kind": "text", "chars": len(payload["contents"][0]["parts"][0]["text"])}],
        "credentials_redacted": True,
    }
    # Confirm config mapping
    cfg = payload_to_generate_config(payload)
    meta["sdk_response_mime_type"] = getattr(cfg, "response_mime_type", None)
    meta["sdk_has_response_json_schema"] = getattr(cfg, "response_json_schema", None) is not None
    meta["sdk_has_response_schema"] = bool(getattr(cfg, "response_schema", None))

    # Ensure client exists (no secrets logged)
    client = get_vertex_gemini_client()
    meta["client_ok"] = client is not None

    try:
        resp = generate_content_via_vertex(
            model="gemini-2.5-flash-lite",
            payload=payload,
            timeout=60,
        )
        text = ""
        if isinstance(resp, dict):
            cands = resp.get("candidates") or []
            if cands:
                parts = ((cands[0].get("content") or {}).get("parts") or [])
                text = "".join(p.get("text") or "" for p in parts if isinstance(p, dict))
            usage = resp.get("usageMetadata") or {}
        else:
            usage = {}
        meta["ok"] = True
        meta["http_status"] = 200
        meta["response_text_preview"] = text[:500]
        meta["usage"] = usage
        return meta
    except Exception as exc:
        details = getattr(exc, "details", None)
        meta["ok"] = False
        meta["http_status"] = getattr(exc, "code", None)
        meta["status"] = getattr(exc, "status", None)
        meta["message"] = str(getattr(exc, "message", None) or exc)
        meta["error_type"] = type(exc).__name__
        # Full sanitized body if present
        try:
            meta["error_details"] = details
        except Exception:
            meta["error_details"] = None
        meta["error_repr"] = repr(exc)[:2000]
        meta["traceback"] = traceback.format_exc()[-2000:]
        return meta


def main() -> None:
    results = []
    for label, schema in _variants():
        (OUT / f"schema_{label}.json").write_text(
            json.dumps(schema, indent=2), encoding="utf-8"
        )
        print(f"probing {label} bytes={len(json.dumps(schema))} ...", flush=True)
        meta = _call(schema, label)
        results.append(meta)
        print(
            f"  -> ok={meta.get('ok')} status={meta.get('http_status')} "
            f"msg={str(meta.get('message') or '')[:120]}",
            flush=True,
        )
        if meta.get("ok"):
            # Keep probing to find all working variants; do not stop early.
            pass
    report = {"probes": results}
    (OUT / "vertex400_bisect.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print("wrote", OUT / "vertex400_bisect.json")


if __name__ == "__main__":
    main()
