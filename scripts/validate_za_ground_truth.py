"""Export and validate the ZA source-verified ground-truth subset.

Writes phase4/za_ground_truth/per_document/*.json from the visual annotations.
Rejects unresolved values, missing provenance, and boxes that were not measured.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ground_truth.schema import validate_ground_truth_document

ANN = ROOT / "phase4" / "za_ground_truth" / "annotations_source.py"
OUT = ROOT / "phase4" / "za_ground_truth" / "per_document"

QTY_HEADERS = {
    "opening_qty": "Op. Qty",
    "receipt_qty": "Receipt Qty.",
    "sales_qty": "Issue Qty.",
    "closing_qty": "Closing Qty.",
    "out_qty": "OUT QTY",
}


def _load_annotations():
    import importlib.util

    spec = importlib.util.spec_from_file_location("za_annotations_source", ANN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.DOCUMENTS


def _field(field_id, field_type, header, raw, *, product_key, normalized, notes):
    return {
        "field_id": field_id,
        "field_type": field_type,
        "printed_header": header,
        "expected_raw": str(raw),
        "expected_normalized": normalized,
        "page": 1,
        "product_key": product_key,
        "bbox": None,
        "bbox_verified": False,
        "verified": True,
        "source_verified": True,
        "priority": "high",
        "notes": notes,
        "provenance": "SOURCE_VERIFIED",
    }


def document_payload(doc: dict) -> dict:
    fields = []
    fields.append(
        _field(
            "header.party",
            "header",
            "Party",
            doc["headers"]["party"],
            product_key=None,
            normalized=doc["headers"]["party"],
            notes="Printed party name on the original image.",
        )
    )
    fields.append(
        _field(
            "header.period",
            "header",
            "Period",
            doc["headers"]["period"],
            product_key=None,
            normalized=doc["headers"]["period"],
            notes="Printed statement period on the original image.",
        )
    )
    for row in doc["rows"]:
        product = row["product"]
        prefix = f"r{row['row']:02d}"
        fields.append(
            _field(
                f"{prefix}.product_name",
                "text",
                "Product Name",
                product,
                product_key=product,
                normalized=product,
                notes=f"Row {row['row']} product name, original image.",
            )
        )
        if row.get("packing"):
            fields.append(
                _field(
                    f"{prefix}.packing",
                    "text",
                    "Packing",
                    row["packing"],
                    product_key=product,
                    normalized=row["packing"],
                    notes=f"Row {row['row']} packing, original image.",
                )
            )
        for key, header in QTY_HEADERS.items():
            if key not in row:
                continue
            value = row[key]
            fields.append(
                _field(
                    f"{prefix}.{key}",
                    "quantity",
                    header,
                    value,
                    product_key=product,
                    normalized=float(value),
                    notes=(
                        f"Row {row['row']} {header}. "
                        "Zero values stay labeled and are excluded from the primary quantity score."
                    ),
                )
            )
    return {
        "document_id": doc["document_id"],
        "page": 1,
        "source": {
            "file": doc["source_file"],
            "source_type": "za_stock_image",
            "source_verified": True,
        },
        "verification_method": "agent_visual_inspection_two_agreeing_reads",
        "provenance": "SOURCE_VERIFIED",
        "layout": doc["layout"],
        "quality": doc["quality"],
        "absent_columns": doc["absent_columns"],
        "fields": fields,
        "notes": doc["notes"],
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    errors = []
    written = 0
    field_count = 0
    for doc in _load_annotations():
        payload = document_payload(doc)
        ok, problems = validate_ground_truth_document(payload)
        if not ok:
            errors.append((payload["document_id"], problems))
            continue
        for field in payload["fields"]:
            if str(field["expected_raw"]).strip().upper() == "UNRESOLVED":
                errors.append((payload["document_id"], ["unresolved_in_scored_file"]))
            if field.get("bbox") is not None:
                errors.append((payload["document_id"], ["bbox_not_allowed"]))
        path = OUT / f"{payload['document_id']}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        written += 1
        field_count += len(payload["fields"])
        print(f"wrote {path.name} fields={len(payload['fields'])}")
    if errors:
        print("INVALID", errors)
        return 1
    print(f"valid documents={written} fields={field_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
