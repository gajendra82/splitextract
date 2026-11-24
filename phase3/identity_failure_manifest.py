"""Read-only dump of Phase 3 STOCK_IDENTITY_FAILURE records."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "phase3_eval" / "records"
OUT = Path(__file__).resolve().parents[1] / "phase3_eval" / "identity_failure_manifest.json"


def main() -> None:
    rows = []
    for path in sorted(ROOT.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("failure_type") != "STOCK_IDENTITY_FAILURE":
            continue
        telemetry = record.get("telemetry") or {}
        rows.append(
            {
                "document_id": record.get("document_id"),
                "extension": record.get("file_type"),
                "parser": record.get("extraction_method"),
                "extraction_method": record.get("extraction_method"),
                "layout": telemetry.get("layout"),
                "profile": record.get("profile"),
                "ocr_required": record.get("ocr_required"),
                "ocr_path": record.get("ocr_path"),
                "ocr_quality": record.get("ocr_quality"),
                "baseline_validation": record.get("baseline_outcome"),
                "baseline_row_count": record.get("baseline_row_count"),
                "phase2_strategy": record.get("recovery_strategy"),
                "recovery_result": record.get("recovery_state"),
                "attempts": record.get("attempts"),
                "pdf_kind": record.get("pdf_kind"),
                "transition": record.get("transition"),
            }
        )
    summary = {
        "count": len(rows),
        "by_profile": dict(Counter(row["profile"] for row in rows)),
        "by_method": dict(Counter(row["extraction_method"] for row in rows)),
        "by_layout": dict(Counter(row["layout"] for row in rows)),
        "by_extension": dict(Counter(row["extension"] for row in rows)),
        "by_ocr_required": dict(Counter(str(row["ocr_required"]) for row in rows)),
        "by_ocr_quality": dict(Counter(str(row["ocr_quality"]) for row in rows)),
        "by_ocr_path": dict(Counter(row["ocr_path"] for row in rows)),
        "documents": rows,
    }
    OUT.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in summary if k != "documents"}, indent=2))


if __name__ == "__main__":
    main()
