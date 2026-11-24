"""Dump Phase 2 recovery schema and probe Vertex structured-output acceptance."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from services.hybrid_schema import sales_recovery_json_schema  # noqa: E402


def main() -> None:
    schema = sales_recovery_json_schema()
    out = ROOT / "phase4" / "phase2_ab_gt20" / "schema_probe"
    out.mkdir(parents=True, exist_ok=True)
    (out / "sales_recovery_json_schema.json").write_text(
        json.dumps(schema, indent=2), encoding="utf-8"
    )
    print("title", schema.get("title"))
    print("top_keys", sorted(schema.keys()))
    print("has_$defs", "$defs" in schema)
    print("has_definitions", "definitions" in schema)
    print("has_$ref", "$ref" in json.dumps(schema))
    print("has_anyOf", "anyOf" in json.dumps(schema))
    print("has_additionalProperties", "additionalProperties" in json.dumps(schema))
    print("bytes", len(json.dumps(schema)))


if __name__ == "__main__":
    main()
