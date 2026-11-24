"""Compare VL-only vs VL+secondary on all SOURCE_VERIFIED GT-20 fields.

Flags regressions (correct → incorrect) and lists repair-driven changes.
"""
from __future__ import annotations

import json
from pathlib import Path

from services.ground_truth.html_table import parse_html_tables
from services.ground_truth.matching import match_field
from services.ground_truth.schema import only_source_verified_fields, validate_ground_truth_document
from services.ocr.secondary_repair import apply_secondary_repairs

ROOT = Path(__file__).resolve().parents[2]
GT = ROOT / "phase4" / "ground_truth_50" / "per_document"
VL = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"


def main() -> None:
    regressions = []
    improvements = []
    repair_log = []
    still_wrong = []
    n_fields = 0
    n_ok_base = 0
    n_ok_sec = 0

    for path in sorted(GT.glob("*.json")):
        gt = json.loads(path.read_text(encoding="utf-8"))
        ok, _ = validate_ground_truth_document(gt)
        if not ok:
            continue
        fields = only_source_verified_fields(gt)
        if not fields:
            continue
        doc = gt["document_id"]
        vl_path = VL / f"{doc}.json"
        if not vl_path.is_file():
            print("SKIP no VL", doc)
            continue
        page = json.loads(vl_path.read_text(encoding="utf-8"))["pages"][0]
        if page.get("status") != "success":
            continue
        text0 = page.get("text") or ""
        cells0 = parse_html_tables(text0)
        tokens = page.get("numeric_tokens") or []
        text1, cells1, report = apply_secondary_repairs(doc, text0)
        if report.repairs:
            repair_log.append(
                {
                    "document_id": doc,
                    "repairs": report.repairs,
                    "unresolved": report.unresolved,
                }
            )
        for f in fields:
            n_fields += 1
            m0 = match_field(f, ocr_text=text0, table_cells=cells0, numeric_tokens=tokens)
            m1 = match_field(f, ocr_text=text1, table_cells=cells1, numeric_tokens=tokens)
            if m0.get("correct"):
                n_ok_base += 1
            if m1.get("correct"):
                n_ok_sec += 1
            if m0.get("correct") and not m1.get("correct"):
                regressions.append(
                    {
                        "document_id": doc,
                        "field_id": f.get("field_id"),
                        "expected": f.get("expected_raw"),
                        "before": m0.get("predicted_raw"),
                        "after": m1.get("predicted_raw"),
                        "error": m1.get("error_type"),
                    }
                )
            if (not m0.get("correct")) and m1.get("correct"):
                improvements.append(
                    {
                        "document_id": doc,
                        "field_id": f.get("field_id"),
                        "expected": f.get("expected_raw"),
                        "before": m0.get("predicted_raw"),
                        "after": m1.get("predicted_raw"),
                        "before_error": m0.get("error_type"),
                    }
                )
            if not m1.get("correct"):
                still_wrong.append(
                    {
                        "document_id": doc,
                        "field_id": f.get("field_id"),
                        "expected": f.get("expected_raw"),
                        "predicted": m1.get("predicted_raw"),
                        "error": m1.get("error_type"),
                        "silent_wrong": m1.get("silent_wrong"),
                    }
                )

    print("FIELDS", n_fields)
    print("VL_ONLY_OK", n_ok_base, f"{n_ok_base/n_fields:.4f}")
    print("WITH_SECONDARY_OK", n_ok_sec, f"{n_ok_sec/n_fields:.4f}")
    print("IMPROVEMENTS", len(improvements))
    for row in improvements:
        print("  +", row)
    print("REGRESSIONS", len(regressions))
    for row in regressions:
        print("  -", row)
    print("STILL_WRONG", len(still_wrong))
    for row in still_wrong:
        print("  !", row)
    print("DOCS_WITH_REPAIRS", len(repair_log))
    for entry in repair_log:
        print(
            "  repair",
            entry["document_id"][:36],
            [r.get("type") for r in entry["repairs"]],
        )


if __name__ == "__main__":
    main()
