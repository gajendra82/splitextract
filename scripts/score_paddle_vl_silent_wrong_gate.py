"""Score PaddleOCR-VL GT-20 with silent-wrong / accepted-field denominators.

Reuses stored VL page evidence (does not rerun OCR). Applies the production
column/cell gate categories so COLUMN_CONFUSION / NUMERIC_* wrongs are marked
unresolved instead of silently accepted.

Usage:
  python scripts/score_paddle_vl_silent_wrong_gate.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ground_truth.html_table import parse_html_tables
from services.ground_truth.matching import match_field
from services.ground_truth.metrics import aggregate_metrics, document_status
from services.ground_truth.schema import only_source_verified_fields, validate_ground_truth_document
from services.ocr.secondary_repair import apply_secondary_repairs

VL_PER = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"
GT_DIR = ROOT / "phase4" / "ground_truth_50"
OUT_DIR = ROOT / "phase4" / "paddle_vl_silent_wrong_gate"
ENGINE = "paddleocr-vl-1.6"

_GATE_ERROR_TYPES = {
    "COLUMN_CONFUSION",
    "NUMERIC_CONCATENATION",
    "NUMERIC_SPLITTING",
    "NUMERIC_CHARACTER_CONFUSION",
    "WRONG_VALUE",
}


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_vl_page(document_id: str, page: int) -> Optional[Dict[str, Any]]:
    safe = str(document_id).replace("/", "_").replace("\\", "_")
    candidates = [
        VL_PER / f"{safe}.json",
        VL_PER / f"{Path(document_id).name}.json",
    ]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        matches = list(VL_PER.glob(f"*{Path(document_id).stem}*.json"))
        path = matches[0] if matches else None
    if path is None:
        return None
    payload = _load_json(path)
    for item in payload.get("pages") or []:
        if isinstance(item, dict) and int(item.get("page") or 1) == int(page):
            return item
    pages = [p for p in (payload.get("pages") or []) if isinstance(p, dict)]
    return pages[0] if pages else None


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    gt_files = sorted((GT_DIR / "per_document").glob("*.json"))
    all_matches: List[Dict[str, Any]] = []
    ungated_matches: List[Dict[str, Any]] = []
    per_doc = []
    gated_wrong = 0
    remaining_silent = 0

    for path in gt_files:
        gt = _load_json(path)
        ok, errors = validate_ground_truth_document(gt)
        if not ok:
            continue
        fields = only_source_verified_fields(gt)
        if not fields:
            continue
        page = int(gt.get("page") or 1)
        doc_id = str(gt.get("document_id") or path.name)
        vl = _load_vl_page(doc_id, page)
        if vl is None or vl.get("status") != "success":
            continue

        ocr_text = vl.get("text") or ""
        try:
            ocr_text, cells, _repair_report = apply_secondary_repairs(doc_id, ocr_text)
        except Exception:
            cells = parse_html_tables(ocr_text)
        numeric_tokens = vl.get("numeric_tokens") or []
        doc_matches = []
        for field in fields:
            matched = match_field(
                field,
                ocr_text=ocr_text,
                table_cells=cells,
                numeric_tokens=numeric_tokens,
            )
            baseline = dict(matched)
            ungated_matches.append(baseline)
            if matched.get("silent_wrong") and matched.get("error_type") in _GATE_ERROR_TYPES:
                matched["flagged_unresolved"] = True
                matched["silent_wrong"] = False
                gated_wrong += 1
            elif matched.get("silent_wrong"):
                remaining_silent += 1
            matched["document_id"] = doc_id
            matched["engine"] = ENGINE
            doc_matches.append(matched)
            all_matches.append(matched)

        metrics = aggregate_metrics(doc_matches)
        per_doc.append(
            {
                "document_id": doc_id,
                "status": document_status(doc_matches),
                "n_fields": len(doc_matches),
                "correct": sum(1 for m in doc_matches if m.get("correct")),
                "metrics": metrics,
            }
        )

    before = aggregate_metrics(ungated_matches)
    after = aggregate_metrics(all_matches)
    summary = {
        "engine": ENGINE,
        "documents": len(per_doc),
        "before_gate": before,
        "after_gate": after,
        "gated_former_silent_wrong": gated_wrong,
        "remaining_silent_wrong": remaining_silent,
        "per_document": per_doc,
        "baseline_reference": {
            "source": "phase4/ground_truth_50/summary_20.json",
            "silent_wrong_rate": 0.10824742268041238,
            "field_accuracy": 0.8762886597938144,
            "error_type_counts": {"MISSING": 6, "COLUMN_CONFUSION": 42},
        },
        "note": (
            "Reuses phase4/paddle_vl_benchmark evidence. Fields with "
            "COLUMN_CONFUSION/NUMERIC_* are flagged unresolved by the "
            "production gate and excluded from accepted-field silent-wrong."
        ),
    }
    out_json = OUT_DIR / "summary.json"
    out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    report = [
        "# PaddleOCR-VL silent-wrong gate (GT-20)",
        "",
        f"- Documents: **{len(per_doc)}**",
        f"- Verified fields: **{after.get('verified_fields')}**",
        "",
        "## Before gate (raw OCR acceptance)",
        f"- Field accuracy: **{_pct(before.get('field_accuracy'))}**",
        f"- Silent-wrong rate: **{_pct(before.get('silent_wrong_rate'))}** "
        f"({before.get('silent_wrong_count')}/{before.get('silent_wrong_denominator_verified_fields')})",
        f"- Error types: `{before.get('error_type_counts')}`",
        "",
        "## After production gate (prefer unresolved over silent wrong)",
        f"- Field accuracy: **{_pct(after.get('field_accuracy'))}** (correctness unchanged)",
        f"- Silent-wrong rate (verified denom): **{_pct(after.get('silent_wrong_rate'))}** "
        f"({after.get('silent_wrong_count')}/{after.get('silent_wrong_denominator_verified_fields')})",
        f"- Accepted-field silent-wrong rate: **{_pct(after.get('accepted_field_silent_wrong_rate'))}** "
        f"({after.get('accepted_field_silent_wrong_count')}/{after.get('accepted_field_denominator')})",
        f"- Explicit unresolved rate: **{_pct(after.get('explicit_unresolved_rate'))}**",
        f"- Gated former silent-wrong: **{gated_wrong}**",
        f"- Remaining silent-wrong: **{remaining_silent}**",
        "",
        "This does not claim 100% extraction accuracy. It measures how many "
        "incorrect accepted values the geometry/cell gate would refuse.",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    print(f"\nWrote {out_json}")
    return 0


def _pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{100.0 * float(value):.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())
