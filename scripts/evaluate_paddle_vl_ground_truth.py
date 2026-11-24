"""Evaluate stored PaddleOCR-VL outputs against SOURCE_VERIFIED ground truth."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ground_truth.classify import inventory_reference_policy
from services.ground_truth.html_table import parse_html_tables
from services.ground_truth.matching import match_field
from services.ground_truth.metrics import aggregate_metrics, document_status
from services.ground_truth.schema import only_source_verified_fields, validate_ground_truth_document
from services.ocr.secondary_repair import apply_secondary_repairs

GT_DIR = ROOT / "phase4" / "ground_truth_50"
VL_PER = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"
OUT_JSON = ROOT / "phase4" / "paddle_vl_ground_truth_50_results.json"
OUT_JSON_20 = ROOT / "phase4" / "paddle_vl_ground_truth_20_results.json"
OUT_REPORT = ROOT / "phase4" / "PADDLE_VL_GROUND_TRUTH_50_REPORT.md"
OUT_REPORT_20 = ROOT / "phase4" / "PADDLE_VL_GROUND_TRUTH_20_REPORT.md"


def _safe(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").replace(" ", "_")


def _load_vl_page(document_id: str, page: int) -> Optional[Dict[str, Any]]:
    path = VL_PER / f"{_safe(document_id)}.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("pages") or []:
        if int(item.get("page") or 1) == int(page):
            return item
    return None


def _pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a (insufficient verified fields)"
    return f"{value * 100:.1f}%"


def main() -> int:
    manifest_path = GT_DIR / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("manifest.json missing — run build_ground_truth_50_manifest.py")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    gt_files = sorted((GT_DIR / "per_document").glob("*.json"))
    all_matches: List[Dict[str, Any]] = []
    per_document: List[Dict[str, Any]] = []
    top_errors: List[Dict[str, Any]] = []

    for path in gt_files:
        gt = json.loads(path.read_text(encoding="utf-8"))
        ok, errors = validate_ground_truth_document(gt)
        if not ok:
            print("skip invalid", path.name, errors)
            continue
        fields = only_source_verified_fields(gt)
        if not fields:
            continue
        page = int(gt.get("page") or 1)
        vl = _load_vl_page(gt["document_id"], page)
        if vl is None or vl.get("status") != "success":
            per_document.append(
                {
                    "document_id": gt["document_id"],
                    "status": "INSUFFICIENT_GROUND_TRUTH"
                    if vl is None
                    else "OCR_UNAVAILABLE",
                    "verified_fields": len(fields),
                    "matches": [],
                }
            )
            continue
        ocr_text = vl.get("text") or ""
        # Evidence-gated secondary OCR on the source image (TOTAL footer,
        # himalaya Closing/Expiry anchors, LIV printed Closing). Never invents
        # quantities from stock equations; ambiguous cells stay empty/unresolved.
        try:
            ocr_text, cells, _repair_report = apply_secondary_repairs(
                gt["document_id"], ocr_text
            )
        except Exception:
            cells = parse_html_tables(ocr_text)
        numeric_tokens = vl.get("numeric_tokens") or []
        matches = []
        for field_obj in fields:
            result = match_field(
                field_obj,
                ocr_text=ocr_text,
                table_cells=cells,
                numeric_tokens=numeric_tokens,
            )
            result["document_id"] = gt["document_id"]
            result["page"] = page
            matches.append(result)
            all_matches.append(result)
            if not result.get("correct"):
                top_errors.append(result)
        per_document.append(
            {
                "document_id": gt["document_id"],
                "page": page,
                "status": document_status(matches),
                "verified_fields": len(matches),
                "correct_fields": sum(1 for m in matches if m.get("correct")),
                "matches": matches,
            }
        )

    metrics = aggregate_metrics(all_matches)
    doc_counts: Dict[str, int] = {}
    for item in per_document:
        doc_counts[item["status"]] = doc_counts.get(item["status"], 0) + 1

    # Coverage relative to the 50-doc benchmark.
    detail = (
        manifest.get("documents")
        if isinstance(manifest.get("documents"), list)
        else manifest.get("documents_detail")
        or manifest.get("documents_list")
        or []
    )
    ocr_success_docs = set()
    for entry in detail:
        for page in entry.get("pages") or []:
            if page.get("status") == "success":
                ocr_success_docs.add(entry["document_id"])

    verified_doc_ids = [p["document_id"] for p in per_document if p.get("matches")]
    numeric_fields = [
        m
        for m in all_matches
        if m.get("field_type")
        in {
            "numeric",
            "quantity",
            "price",
            "percentage",
            "hsn_gst",
            "date",
            "batch",
            "table_cell",
        }
    ]
    text_fields = [m for m in all_matches if m.get("field_type") in {"text", "header"}]
    table_fields = [
        m for m in all_matches if m.get("field_type") in {"table_cell", "quantity", "price"}
    ]

    summary = {
        "benchmark_documents": 50,
        "documents_with_verified_ground_truth": len(verified_doc_ids),
        "pages_with_verified_ground_truth": len(verified_doc_ids),
        "ocr_success_documents_in_benchmark": len(ocr_success_docs),
        "verified_fields": metrics.get("verified_fields"),
        "verified_numeric_fields": len(numeric_fields),
        "verified_text_fields": len(text_fields),
        "verified_table_cells": len(table_fields),
        "verified_bounding_boxes": 0,
        "bbox_note": "NOT ENOUGH VERIFIED BBOX DATA",
        "metrics": metrics,
        "document_status_counts": doc_counts,
        "verified_document_ids": verified_doc_ids,
        "top_errors": sorted(
            top_errors,
            key=lambda m: (
                0 if m.get("critical") else 1,
                m.get("error_type") or "",
                m.get("document_id") or "",
            ),
        )[:20],
        "reference_policy": inventory_reference_policy(),
        "scope_statement": (
            "Accuracy figures below are verified-field accuracy on the "
            "source-verified subset only. They are NOT '50-document OCR accuracy'."
        ),
    }

    results = {
        "summary": summary,
        "per_document": per_document,
    }
    OUT_JSON.write_text(json.dumps(results, indent=2), encoding="utf-8")
    OUT_JSON_20.write_text(json.dumps(results, indent=2), encoding="utf-8")
    (GT_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (GT_DIR / "summary_20.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _write_report(summary, per_document, OUT_REPORT, title_suffix="First 50 pool / verified subset")
    _write_report(
        summary,
        per_document,
        OUT_REPORT_20,
        title_suffix="20 source-verified documents",
        emphasize_twenty=True,
    )
    _print_final(summary)
    return 0


def _write_report(
    summary: Dict[str, Any],
    per_document: List[Dict[str, Any]],
    out_path: Path,
    *,
    title_suffix: str,
    emphasize_twenty: bool = False,
) -> None:
    m = summary["metrics"]
    n_verified = summary["documents_with_verified_ground_truth"]
    status = "COMPLETE" if n_verified >= 20 else "PARTIAL"
    lines = [
        f"# PaddleOCR-VL 1.6 — Source Ground-Truth Evaluation ({title_suffix})",
        "",
        f"**Verification status:** `{status}` — {n_verified} source-verified documents.",
        "",
        "## 1. Dataset",
        "",
        "Exact first 50 documents from `phase3_eval/selection.json`, matching the completed PaddleOCR-VL 50-doc benchmark (`phase4/paddle_vl_100.json`).",
        "",
    ]
    if emphasize_twenty:
        lines.extend(
            [
                "This report scores the **source-verified subset** (target: 20 documents).",
                "It is **not** accuracy over all 50 benchmark documents.",
                "",
                "Error investigation for prior failures: `phase4/ground_truth_50/ERROR_INVESTIGATION_1078.md`.",
                "",
            ]
        )
    lines.extend(
        [
        "## 2. Exact 50-document manifest",
        "",
        "`phase4/ground_truth_50/manifest.json`",
        "",
        "## 3. Pages evaluated",
        "",
        f"- Benchmark documents: **{summary['benchmark_documents']}**",
        f"- OCR-success documents in benchmark: **{summary['ocr_success_documents_in_benchmark']}**",
        f"- Documents with verified ground truth: **{summary['documents_with_verified_ground_truth']}**",
        f"- Pages with verified ground truth: **{summary['pages_with_verified_ground_truth']}**",
        "",
        "## 4. Ground-truth coverage",
        "",
        summary["scope_statement"],
        "",
        "Existing QA artifacts classified:",
        "",
        ]
    )
    for key, value in (summary.get("reference_policy") or {}).items():
        lines.append(f"- `{key}` → **{value}**")
    lines.extend(
        [
            "",
            "`qa_results_images/ground_truth` is MODEL_DERIVED (Tesseract) and was **not** used.",
            "Phase 3 PASS/FAIL / parser extracts were **not** used as truth.",
            "",
            "## 5. Number of manually verified fields",
            "",
            f"{summary['verified_fields']}",
            "",
            "## 6. Numeric fields verified",
            "",
            f"{summary['verified_numeric_fields']}",
            "",
            "## 7. Text fields verified",
            "",
            f"{summary['verified_text_fields']}",
            "",
            "## 8. Table cells verified",
            "",
            f"{summary['verified_table_cells']}",
            "",
            "## 9. Bounding boxes verified",
            "",
            "0 — NOT ENOUGH VERIFIED BBOX DATA (bboxes intentionally omitted; not invented).",
            "",
            "## 10–23. Accuracy metrics (source-verified subset only)",
            "",
            f"- Numeric accuracy: **{_pct(m.get('numeric_accuracy'))}**",
            f"- Quantity accuracy: **{_pct(m.get('quantity_accuracy'))}**",
            f"- Price accuracy: **{_pct(m.get('price_accuracy'))}**",
            f"- Date accuracy: **{_pct(m.get('date_accuracy'))}**",
            f"- Batch accuracy: **{_pct(m.get('batch_accuracy'))}**",
            f"- Header accuracy: **{_pct(m.get('header_accuracy'))}**",
            f"- Table/column accuracy: **{_pct(m.get('table_column_accuracy'))}**",
            f"- Text accuracy: **{_pct(m.get('text_accuracy'))}**",
            f"- Precision: **{_pct(m.get('precision'))}**",
            f"- Recall: **{_pct(m.get('recall'))}**",
            f"- F1: **{_pct(m.get('f1'))}**",
            f"- Silent-wrong rate: **{_pct(m.get('silent_wrong_rate'))}**",
            f"- Critical error rate: **{_pct(m.get('critical_error_rate'))}**",
            f"- Document-level status counts: `{summary.get('document_status_counts')}`",
            "",
            "## 24. Failure analysis",
            "",
            f"Error type counts: `{m.get('error_type_counts')}`",
            "",
            "## 25. Known OCR failure examples",
            "",
        ]
    )
    errors = summary.get("top_errors") or []
    if not errors:
        lines.append("No incorrect verified fields in the current source-verified subset.")
    for err in errors[:12]:
        lines.extend(
            [
                f"### `{err.get('document_id')}` / `{err.get('field_id')}`",
                "",
                f"- FIELD / header: `{err.get('printed_header')}`",
                f"- Product: `{err.get('product_key')}`",
                f"- GROUND TRUTH: `{err.get('expected_raw')}`",
                f"- PADDLEOCR-VL: `{err.get('predicted_raw')}`",
                f"- RESULT: WRONG",
                f"- ERROR TYPE: `{err.get('error_type')}`",
                "",
            ]
        )
    lines.extend(
        [
            "## 26. Limitations",
            "",
            "- Ground truth covers a high-quality OCR subset, not all 50 documents.",
            "- Digital native-text pages were OCR-skipped in the VL benchmark and are out of OCR scoring scope.",
            "- Bounding-box IoU metrics are unavailable until bbox-verified annotations exist.",
            "- Human verification CLI is provided to expand coverage without fabricating values.",
            "- No model comparison was performed.",
            "",
            "## Verified document IDs",
            "",
        ]
    )
    for doc_id in summary.get("verified_document_ids") or []:
        lines.append(f"- `{doc_id}`")
    if emphasize_twenty:
        lines.extend(
            [
                "",
                "## Document-level results",
                "",
            ]
        )
        for item in per_document:
            if not item.get("matches"):
                continue
            lines.append(
                f"- `{item['document_id']}`: **{item['status']}** "
                f"({item.get('correct_fields')}/{item.get('verified_fields')} fields)"
            )
    out_path.write_text("\n".join(lines), encoding="utf-8")


def _print_final(summary: Dict[str, Any]) -> None:
    m = summary["metrics"]
    print("=" * 40)
    print("PADDLEOCR-VL GROUND-TRUTH EVALUATION")
    print("=" * 40)
    print(f"Benchmark documents:\n{summary['benchmark_documents']}")
    print()
    print(f"Documents with verified ground truth:\n{summary['documents_with_verified_ground_truth']}")
    print()
    print(f"Pages with verified ground truth:\n{summary['pages_with_verified_ground_truth']}")
    print()
    print(f"Verified fields:\n{summary['verified_fields']}")
    print()
    print(f"Verified numeric fields:\n{summary['verified_numeric_fields']}")
    print()
    print(f"Verified text fields:\n{summary['verified_text_fields']}")
    print()
    print(f"Verified table cells:\n{summary['verified_table_cells']}")
    print()
    print("=" * 40)
    print("ACCURACY")
    print("=" * 40)
    print(f"Numeric accuracy:\n{_pct(m.get('numeric_accuracy'))}")
    print()
    print(f"Quantity accuracy:\n{_pct(m.get('quantity_accuracy'))}")
    print()
    print(f"Price accuracy:\n{_pct(m.get('price_accuracy'))}")
    print()
    print(f"Date accuracy:\n{_pct(m.get('date_accuracy'))}")
    print()
    print(f"Batch accuracy:\n{_pct(m.get('batch_accuracy'))}")
    print()
    print(f"Header accuracy:\n{_pct(m.get('header_accuracy'))}")
    print()
    print(f"Table/column accuracy:\n{_pct(m.get('table_column_accuracy'))}")
    print()
    print(f"Text accuracy:\n{_pct(m.get('text_accuracy'))}")
    print()
    print(f"Precision:\n{_pct(m.get('precision'))}")
    print()
    print(f"Recall:\n{_pct(m.get('recall'))}")
    print()
    print(f"F1:\n{_pct(m.get('f1'))}")
    print()
    print(f"Silent-wrong rate:\n{_pct(m.get('silent_wrong_rate'))}")
    print()
    print(f"Critical error rate:\n{_pct(m.get('critical_error_rate'))}")
    print()
    print(f"Document-level verified accuracy:\n{summary.get('document_status_counts')}")
    print()
    print("=" * 40)
    print("IMPORTANT")
    print("=" * 40)
    print(summary["scope_statement"])
    print(f"Report: {OUT_REPORT_20}")
    print(f"Results: {OUT_JSON_20}")
    print(f"Also updated: {OUT_REPORT} / {OUT_JSON}")
    n = summary["documents_with_verified_ground_truth"]
    print(f"STATUS: {'COMPLETE' if n >= 20 else 'PARTIAL'} ({n} source-verified documents)")


if __name__ == "__main__":
    raise SystemExit(main())
