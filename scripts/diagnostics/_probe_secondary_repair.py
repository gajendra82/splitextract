import json
from pathlib import Path

from services.ground_truth.matching import match_field
from services.ground_truth.schema import only_source_verified_fields
from services.ocr.secondary_repair import apply_secondary_repairs

ROOT = Path(__file__).resolve().parents[2]
GT = ROOT / "phase4" / "ground_truth_50" / "per_document"
VL = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"

DOCS = [
    "0000701078_2026_08_ZL_03_362_05092026080346.jpeg",
    "0000700275_2026_08_ZL_18_374_04092026171615.pdf",
    "0000701151_2026_08_ZL_14_361_03092026025650.jpg",
    "0000700209_2026_08_ZL_18_374_04092026060313.pdf",
    "0000700124_2026_08_ZL_06_521_02092026204703.pdf",
]

for doc in DOCS:
    gt = json.loads((GT / f"{doc}.json").read_text(encoding="utf-8"))
    vl = json.loads((VL / f"{doc}.json").read_text(encoding="utf-8"))
    page = vl["pages"][0]
    text = page.get("text") or ""
    text2, cells, report = apply_secondary_repairs(doc, text)
    print("=" * 60, doc[:40])
    print("repairs", report.repairs)
    print("unresolved", report.unresolved)
    fields = only_source_verified_fields(gt)
    for f in fields:
        if f.get("field_id") in {
            "aact_close",
            "talekt_exp",
            "liv100_cl",
            "tot_op",
            "tot_cl",
            "aact_op",
            "aact_cl",
            "h_op",
        }:
            m = match_field(
                f,
                ocr_text=text2,
                table_cells=cells,
                numeric_tokens=page.get("numeric_tokens"),
            )
            print(
                f.get("field_id"),
                "OK" if m.get("correct") else m.get("error_type"),
                "exp",
                f.get("expected_raw"),
                "pred",
                m.get("predicted_raw"),
                m.get("match_mode"),
            )
