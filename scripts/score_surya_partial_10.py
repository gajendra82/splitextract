"""Score completed Surya pages only (partial 10/20) vs Tess + VL baselines."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ground_truth.html_table import parse_html_tables
from services.ground_truth.matching import match_field
from services.ground_truth.metrics import aggregate_metrics, document_status
from services.ground_truth.schema import only_source_verified_fields, validate_ground_truth_document
from services.ocr.paddle_vl_engine import extract_numeric_tokens

GT_DIR = ROOT / "phase4" / "ground_truth_50" / "per_document"
VL_PER = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"
TESS_DIR = ROOT / "phase4" / "tesseract_vs_paddle_vl_ground_truth_20" / "tesseract_per_document"
SURYA_DIR = ROOT / "phase4" / "surya_vs_ocr_baselines_ground_truth_20" / "surya_per_document"
OUT = ROOT / "phase4" / "surya_vs_ocr_baselines_ground_truth_20"

ORDER = ["tesseract", "paddleocr-vl-1.6", "surya-2"]


def _safe(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").replace(" ", "_")


def _pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _load_page(base: Path, document_id: str, page: int) -> Optional[Dict[str, Any]]:
    path = base / f"{_safe(document_id)}.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("pages") or []:
        if int(item.get("page") or 1) == int(page):
            return item
    return None


def _score(gt_docs, engine_name, loader):
    matches: List[Dict[str, Any]] = []
    per_document: List[Dict[str, Any]] = []
    for gt in gt_docs:
        fields = only_source_verified_fields(gt)
        page = int(gt.get("page") or 1)
        evidence = loader(gt["document_id"], page)
        if evidence is None or evidence.get("status") != "success":
            page_matches = []
            for field_obj in fields:
                match = {
                    "field_id": field_obj.get("field_id"),
                    "field_type": field_obj.get("field_type"),
                    "printed_header": field_obj.get("printed_header"),
                    "product_key": field_obj.get("product_key"),
                    "expected_raw": field_obj.get("expected_raw"),
                    "predicted_raw": None,
                    "match_mode": None,
                    "correct": False,
                    "error_type": "MISSING",
                    "silent_wrong": False,
                    "critical": True,
                    "priority": field_obj.get("priority") or "high",
                    "document_id": gt["document_id"],
                    "page": page,
                    "engine": engine_name,
                }
                page_matches.append(match)
                matches.append(match)
            per_document.append(
                {
                    "document_id": gt["document_id"],
                    "engine": engine_name,
                    "status": "OCR_UNAVAILABLE",
                    "verified_fields": len(fields),
                    "correct_fields": 0,
                    "matches": page_matches,
                }
            )
            continue
        ocr_text = evidence.get("text") or ""
        cells = parse_html_tables(ocr_text)
        tokens = evidence.get("numeric_tokens") or extract_numeric_tokens(ocr_text)
        page_matches = []
        for field_obj in fields:
            result = match_field(
                field_obj,
                ocr_text=ocr_text,
                table_cells=cells,
                numeric_tokens=tokens,
            )
            result["document_id"] = gt["document_id"]
            result["page"] = page
            result["engine"] = engine_name
            page_matches.append(result)
            matches.append(result)
        per_document.append(
            {
                "document_id": gt["document_id"],
                "engine": engine_name,
                "status": document_status(page_matches),
                "verified_fields": len(page_matches),
                "correct_fields": sum(1 for m in page_matches if m.get("correct")),
                "matches": page_matches,
            }
        )
    return matches, per_document, aggregate_metrics(matches)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    gt_docs = []
    for path in sorted(GT_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        ok, _ = validate_ground_truth_document(payload)
        if not ok:
            continue
        if not only_source_verified_fields(payload):
            continue
        sid = _safe(payload["document_id"])
        if not (SURYA_DIR / f"{sid}.json").is_file():
            continue
        page = int(payload.get("page") or 1)
        evidence = _load_page(SURYA_DIR, payload["document_id"], page)
        if not evidence or evidence.get("status") != "success":
            continue
        gt_docs.append(payload)

    field_count = sum(len(only_source_verified_fields(g)) for g in gt_docs)
    print(f"docs={len(gt_docs)} fields={field_count}")

    loaders = {
        "tesseract": lambda d, p: _load_page(TESS_DIR, d, p),
        "paddleocr-vl-1.6": lambda d, p: _load_page(VL_PER, d, p),
        "surya-2": lambda d, p: _load_page(SURYA_DIR, d, p),
    }
    scored = {}
    for name, loader in loaders.items():
        matches, per, metrics = _score(gt_docs, name, loader)
        scored[name] = {"matches": matches, "per": per, "metrics": metrics}

    def key(m: Dict[str, Any]) -> Tuple[str, str]:
        return (str(m.get("document_id")), str(m.get("field_id")))

    maps = {e: {key(m): m for m in scored[e]["matches"]} for e in ORDER}
    shared = sorted(set.intersection(*(set(maps[e]) for e in ORDER)))
    shared_metrics = {e: aggregate_metrics([maps[e][k] for k in shared]) for e in ORDER}

    pairwise: Dict[str, Any] = {}
    for i, left in enumerate(ORDER):
        for right in ORDER[i + 1 :]:
            both = only_l = only_r = both_wrong = 0
            for k in shared:
                a = bool(maps[left][k].get("correct"))
                b = bool(maps[right][k].get("correct"))
                if a and b:
                    both += 1
                elif a and not b:
                    only_l += 1
                elif b and not a:
                    only_r += 1
                else:
                    both_wrong += 1
            pairwise[f"{left}_vs_{right}"] = {
                "both_correct": both,
                f"{left}_only_correct": only_l,
                f"{right}_only_correct": only_r,
                "both_wrong": both_wrong,
            }

    error_categories = {}
    for engine in ORDER:
        counts: Dict[str, int] = {}
        for k in shared:
            match = maps[engine][k]
            label = "CORRECT" if match.get("correct") else str(match.get("error_type") or "UNKNOWN")
            counts[label] = counts.get(label, 0) + 1
        error_categories[engine] = counts

    per_document = []
    for gt in gt_docs:
        row = {
            "document_id": gt["document_id"],
            "verified_fields": len(only_source_verified_fields(gt)),
            "statuses": {},
            "correct_fields": {},
        }
        for engine in ORDER:
            item = next(x for x in scored[engine]["per"] if x["document_id"] == gt["document_id"])
            row["statuses"][engine] = item["status"]
            row["correct_fields"][engine] = item["correct_fields"]
        per_document.append(row)

    fully = {
        e: sum(1 for d in scored[e]["per"] if d.get("status") == "FULLY_CORRECT") for e in ORDER
    }
    field_acc = {e: shared_metrics[e].get("field_accuracy") for e in ORDER}
    silent = {e: shared_metrics[e].get("silent_wrong_rate") for e in ORDER}
    best = sorted(
        ORDER,
        key=lambda e: (
            field_acc[e] is not None,
            field_acc[e] or -1.0,
            -1.0 * (silent[e] if silent[e] is not None else 1.0),
        ),
        reverse=True,
    )[0]
    recommendation = (
        f"On this partial sample ({len(gt_docs)} of 20 SOURCE_VERIFIED docs / "
        f"{len(shared)} fields with completed Surya OCR), {best} has the highest "
        f"measured field accuracy ({_pct(field_acc[best])}). Silent-wrong: "
        + ", ".join(f"{e}={_pct(silent[e])}" for e in ORDER)
        + ". FULLY_CORRECT: "
        + ", ".join(f"{e}={fully[e]}" for e in ORDER)
        + ". This is not a full 20-doc result and not an automatic production winner; "
        "the run was stopped after 10 documents."
    )

    comparison = {
        "status": "PARTIAL_10_OF_20",
        "scope": {
            "intended_documents": 20,
            "scored_documents": len(gt_docs),
            "verified_fields_scored": len(shared),
            "ground_truth": "SOURCE_VERIFIED",
            "document_ids": [g["document_id"] for g in gt_docs],
            "note": "Stopped early at user request; only docs with completed Surya OCR scored.",
        },
        "engines": {
            e: {
                "metrics_shared_fields": shared_metrics[e],
                "fully_correct_documents": fully[e],
                "document_status_counts": {
                    s: sum(1 for d in scored[e]["per"] if d["status"] == s)
                    for s in sorted({d["status"] for d in scored[e]["per"]})
                },
            }
            for e in ORDER
        },
        "pairwise_shared_fields": pairwise,
        "error_categories": error_categories,
        "per_document": per_document,
        "recommendation": recommendation,
    }

    (OUT / "partial_10_comparison_results.json").write_text(
        json.dumps(
            {
                "comparison": comparison,
                "shared_field_matches": {e: [maps[e][k] for k in shared] for e in ORDER},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUT / "partial_10_summary.json").write_text(
        json.dumps(comparison, indent=2), encoding="utf-8"
    )

    metric_rows = [
        ("Field", "field_accuracy"),
        ("Numeric", "numeric_accuracy"),
        ("Quantity", "quantity_accuracy"),
        ("Price", "price_accuracy"),
        ("Date", "date_accuracy"),
        ("Text", "text_accuracy"),
        ("Table/column", "table_column_accuracy"),
        ("Silent-wrong", "silent_wrong_rate"),
        ("Critical-error", "critical_error_rate"),
        ("Precision", "precision"),
        ("Recall", "recall"),
        ("F1", "f1"),
    ]
    lines = [
        "# Surya 2 vs Tesseract vs PaddleOCR-VL — PARTIAL (10 of 20 docs)",
        "",
        "## Status",
        "",
        "**PARTIAL** — full 20-doc run stopped at user request. Only the 10 documents "
        "with completed Surya OCR are scored on identical SOURCE_VERIFIED fields.",
        "",
        f"- Documents scored: **{len(gt_docs)}** / 20",
        f"- Identical overlapping fields: **{len(shared)}**",
        "",
        "## Shared-field accuracy",
        "",
        "| Metric | Tesseract | PaddleOCR-VL | Surya 2 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for label, key_name in metric_rows:
        lines.append(
            "| {label} | {t} | {v} | {s} |".format(
                label=label,
                t=_pct(shared_metrics["tesseract"].get(key_name)),
                v=_pct(shared_metrics["paddleocr-vl-1.6"].get(key_name)),
                s=_pct(shared_metrics["surya-2"].get(key_name)),
            )
        )
    lines.extend(["", "## FULLY_CORRECT docs", ""])
    for engine in ORDER:
        lines.append(f"- {engine}: **{fully[engine]}** / {len(gt_docs)}")
    lines.extend(
        [
            "",
            "## Pairwise",
            "",
            "```json",
            json.dumps(pairwise, indent=2),
            "```",
            "",
            "## Error categories",
            "",
            "```json",
            json.dumps(error_categories, indent=2),
            "```",
            "",
            "## Per-document",
            "",
        ]
    )
    for row in per_document:
        lines.append(
            f"- `{row['document_id']}` n={row['verified_fields']} "
            f"statuses={row['statuses']} correct={row['correct_fields']}"
        )
    lines.extend(["", "## Recommendation", "", recommendation, ""])
    (OUT / "PARTIAL_10_COMPARISON_REPORT.md").write_text("\n".join(lines), encoding="utf-8")

    print("=== PARTIAL 10/20 ===")
    for engine in ORDER:
        m = shared_metrics[engine]
        print(
            f"{engine}: field={_pct(m.get('field_accuracy'))} "
            f"num={_pct(m.get('numeric_accuracy'))} "
            f"qty={_pct(m.get('quantity_accuracy'))} "
            f"price={_pct(m.get('price_accuracy'))} "
            f"date={_pct(m.get('date_accuracy'))} "
            f"text={_pct(m.get('text_accuracy'))} "
            f"table={_pct(m.get('table_column_accuracy'))} "
            f"silent={_pct(m.get('silent_wrong_rate'))} "
            f"FULLY={fully[engine]}"
        )
    print("pairwise", json.dumps(pairwise))
    print("errors", json.dumps(error_categories))
    print("REC:", recommendation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
