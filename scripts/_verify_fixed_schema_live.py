"""Live check: fixed SalesExtractionOutput schema returns valid structured JSON."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from google.genai import types

from services.hybrid_schema import SalesExtractionOutput, sales_recovery_json_schema
from services.vertex_gemini_client import (
    get_vertex_gemini_client,
    payload_to_contents,
    payload_to_generate_config,
    sdk_response_to_rest_dict,
)


def main() -> None:
    schema = sales_recovery_json_schema()
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "Return JSON matching the schema with exactly one line_item. "
                            "fixed.product_name must be TEST TAB. "
                            "fixed.opening_qty=10, fixed.receipts_qty=0, "
                            "fixed.sales_qty=2, fixed.closing_qty=8. "
                            "Set all header fields to null. "
                            "Set totals to an empty object. Set dynamic to an empty object. "
                            "Each line_item must include fixed and dynamic."
                        )
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
        },
    }
    client = get_vertex_gemini_client()
    contents = payload_to_contents(payload)
    config = payload_to_generate_config(payload)
    print("schema_bytes", len(json.dumps(schema)))
    print("dynamic_additionalProperties", schema["properties"]["dynamic"]["additionalProperties"])
    print("mime", getattr(config, "response_mime_type", None))
    raw = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=contents,
        config=config,
    )
    print("raw_type", type(raw))
    print("raw_text", repr(getattr(raw, "text", None))[:500])
    usage = getattr(raw, "usage_metadata", None)
    if usage is not None:
        print(
            "usage",
            {
                "prompt": getattr(usage, "prompt_token_count", None),
                "candidates": getattr(usage, "candidates_token_count", None),
                "total": getattr(usage, "total_token_count", None),
            },
        )
    cands = getattr(raw, "candidates", None) or []
    print("candidates", len(cands))
    for i, cand in enumerate(cands):
        print(
            "cand",
            i,
            "finish",
            getattr(cand, "finish_reason", None),
            "safety",
            getattr(cand, "safety_ratings", None),
        )
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []
        print("parts", len(parts))
        for j, part in enumerate(parts):
            print(
                "part",
                j,
                "keys",
                [k for k in dir(part) if not k.startswith("_")],
            )
            print("part_text", repr(getattr(part, "text", None))[:500])
            thought = getattr(part, "thought", None)
            if thought is not None:
                print("part_thought", thought)
    rest = sdk_response_to_rest_dict(raw)
    print("rest", json.dumps(rest, default=str)[:1000])
    text = ""
    parts = ((rest.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text") or "" for p in parts if isinstance(p, dict)).strip()
    if not text:
        text = (getattr(raw, "text", None) or "").strip()
    print("extracted_text_len", len(text))
    if not text:
        raise SystemExit("EMPTY_STRUCTURED_OUTPUT")
    parsed = SalesExtractionOutput.model_validate_json(text)
    print(
        "PARSED_OK",
        json.dumps(parsed.line_items[0].fixed.model_dump(), default=str)
        if parsed.line_items
        else None,
    )


if __name__ == "__main__":
    main()
