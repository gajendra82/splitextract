"""Compare Tesseract vs PaddleOCR-VL on the 20 source-verified GT documents.

- Reuses stored PaddleOCR-VL page outputs (no VL rerun).
- Looks for existing Tesseract page dumps; if absent, runs TesseractEngine
  once per missing page (benchmark-only; does not change production OCR).
- Scores both engines with the same GT fields and match_field / aggregate_metrics.
"""

from __future__ import annotations

import json
import re
import sys
import time
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
from services.ocr.paddle_vl_io import load_page_image, page_needs_ocr
from services.ocr.tesseract_engine import TesseractEngine

GT_DIR = ROOT / "phase4" / "ground_truth_50"
VL_PER = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"
OUT_DIR = ROOT / "phase4" / "tesseract_vs_paddle_vl_ground_truth_20"
TESS_DIR = OUT_DIR / "tesseract_per_document"
DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")

NUMERIC_TOKEN_RE = re.compile(
    r"(?<![\w./-])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+)(?![\w./-])"
)


def _safe(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").replace(" ", "_")


def _pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _load_gt_docs() -> List[Dict[str, Any]]:
    docs = []
    for path in sorted((GT_DIR / "per_document").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        ok, errors = validate_ground_truth_document(payload)
        if not ok:
            continue
        fields = only_source_verified_fields(payload)
        if not fields:
            continue
        docs.append(payload)
    return docs


def _load_vl_page(document_id: str, page: int) -> Optional[Dict[str, Any]]:
    path = VL_PER / f"{_safe(document_id)}.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("pages") or []:
        if int(item.get("page") or 1) == int(page):
            return item
    return None


def _discover_existing_tesseract(document_id: str, page: int) -> Optional[Dict[str, Any]]:
    """Search known artifact locations for reusable Tesseract page text."""
    # 1) Prior run of this comparison script
    local = TESS_DIR / f"{_safe(document_id)}.json"
    if local.is_file():
        payload = json.loads(local.read_text(encoding="utf-8"))
        for item in payload.get("pages") or []:
            if int(item.get("page") or 1) == int(page) and item.get("text") is not None:
                item = dict(item)
                item["source"] = "comparison_cache"
                return item

    # 2) No other page-level Tesseract dumps exist in qa_results* / phase3_eval
    #    (verified: extracted/ground_truth/records lack raw OCR text fields).
    return None


def _run_tesseract_page(
    engine: TesseractEngine,
    source_file: Path,
    page: int,
) -> Dict[str, Any]:
    page_index = max(0, page - 1)
    needs, native_meta = page_needs_ocr(source_file, page_index=page_index)
    if not needs and source_file.suffix.lower() == ".pdf":
        # Still OCR for fair engine comparison on the same visual page used by VL
        # when VL ran OCR. Caller only invokes this for VL-success OCR pages.
        pass
    started = time.perf_counter()
    image_bytes, image_meta = load_page_image(source_file, page_index=page_index)
    if not image_bytes:
        return {
            "page": page,
            "engine": "tesseract",
            "status": "error",
            "text": "",
            "numeric_tokens": [],
            "error": image_meta.get("error") or "image_load_failed",
            "latency_ms": (time.perf_counter() - started) * 1000.0,
            "source": "fresh_tesseract_run",
        }
    result = engine.recognize(
        image_bytes,
        page=page,
        metadata={"source_path": str(source_file), **image_meta},
    )
    text = result.text or ""
    tokens = extract_numeric_tokens(text)
    if not tokens:
        tokens = NUMERIC_TOKEN_RE.findall(text)
    return {
        "page": page,
        "engine": "tesseract",
        "status": "success" if text.strip() else "error",
        "text": text,
        "normalized_text": " ".join(text.split()),
        "numeric_tokens": tokens,
        "word_count": len(result.words or []),
        "confidence": result.confidence,
        "words": [w.to_dict() for w in (result.words or [])],
        "latency_ms": (result.metadata or {}).get("latency_ms")
        or ((time.perf_counter() - started) * 1000.0),
        "image_meta": image_meta,
        "native_meta": native_meta,
        "source": "fresh_tesseract_run",
        "error": (result.metadata or {}).get("error"),
    }


def _ensure_tesseract_outputs(gt_docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    TESS_DIR.mkdir(parents=True, exist_ok=True)
    inventory = {
        "existing_cache_hits": 0,
        "fresh_runs": 0,
        "missing_prior_repo_dumps": True,
        "notes": [
            "No page-level Tesseract OCR text dumps were found under qa_results/, "
            "qa_results_images/, or phase3_eval/ for these 20 documents.",
            "qa_results*_ground_truth is MODEL_DERIVED structured GT (Tesseract-backed "
            "for some files) and is NOT used as OCR evidence or as scoring truth.",
            "Fresh TesseractEngine runs are stored only under this comparison folder.",
        ],
        "documents": {},
    }
    engine = TesseractEngine()
    if not engine.available():
        inventory["error"] = "tesseract_unavailable"
        return inventory

    for gt in gt_docs:
        doc_id = gt["document_id"]
        page = int(gt.get("page") or 1)
        source = Path((gt.get("source") or {}).get("file") or (DATASET / doc_id))
        existing = _discover_existing_tesseract(doc_id, page)
        out_path = TESS_DIR / f"{_safe(doc_id)}.json"
        if existing and existing.get("source") == "comparison_cache":
            inventory["existing_cache_hits"] += 1
            inventory["documents"][doc_id] = {"page": page, "status": existing.get("status"), "source": "comparison_cache"}
            continue

        # Merge into per-doc file
        payload = {"document_id": doc_id, "source_file": str(source), "pages": []}
        if out_path.is_file():
            payload = json.loads(out_path.read_text(encoding="utf-8"))
        pages = [p for p in (payload.get("pages") or []) if int(p.get("page") or 1) != page]
        page_rec = _run_tesseract_page(engine, source, page)
        pages.append(page_rec)
        payload["pages"] = sorted(pages, key=lambda p: int(p.get("page") or 1))
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        inventory["fresh_runs"] += 1
        inventory["documents"][doc_id] = {
            "page": page,
            "status": page_rec.get("status"),
            "source": page_rec.get("source"),
            "text_length": len(page_rec.get("text") or ""),
        }
        print(f"[tesseract] {doc_id} p{page} status={page_rec.get('status')} chars={len(page_rec.get('text') or '')}", flush=True)
    return inventory


def _score_engine(
    gt_docs: List[Dict[str, Any]],
    *,
    engine_name: str,
    load_page,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    all_matches: List[Dict[str, Any]] = []
    per_document: List[Dict[str, Any]] = []
    for gt in gt_docs:
        fields = only_source_verified_fields(gt)
        page = int(gt.get("page") or 1)
        evidence = load_page(gt["document_id"], page)
        if evidence is None or evidence.get("status") != "success":
            per_document.append(
                {
                    "document_id": gt["document_id"],
                    "engine": engine_name,
                    "status": "OCR_UNAVAILABLE",
                    "verified_fields": len(fields),
                    "correct_fields": 0,
                    "matches": [],
                }
            )
            # Still emit MISSING matches so field sets stay aligned.
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
                all_matches.append(match)
            continue

        ocr_text = evidence.get("text") or ""
        cells = parse_html_tables(ocr_text)
        numeric_tokens = evidence.get("numeric_tokens") or extract_numeric_tokens(ocr_text)
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
            result["engine"] = engine_name
            matches.append(result)
            all_matches.append(result)
        per_document.append(
            {
                "document_id": gt["document_id"],
                "engine": engine_name,
                "status": document_status(matches),
                "verified_fields": len(matches),
                "correct_fields": sum(1 for m in matches if m.get("correct")),
                "matches": matches,
            }
        )
    metrics = aggregate_metrics(all_matches)
    return all_matches, per_document, metrics


def _load_tess_page(document_id: str, page: int) -> Optional[Dict[str, Any]]:
    path = TESS_DIR / f"{_safe(document_id)}.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("pages") or []:
        if int(item.get("page") or 1) == int(page):
            return item
    return None


def _delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return a - b


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    gt_docs = _load_gt_docs()
    if len(gt_docs) != 20:
        print(f"WARNING: expected 20 GT docs, found {len(gt_docs)}")

    field_count = sum(len(only_source_verified_fields(g)) for g in gt_docs)
    print(f"GT docs={len(gt_docs)} fields={field_count}")

    # Inventory prior Tesseract dumps
    missing_note = {
        "prior_tesseract_page_dumps_found": False,
        "searched": [
            "qa_results/",
            "qa_results_images/",
            "phase3_eval/",
            "phase4/ (except this comparison folder)",
        ],
        "incompatible_artifacts": [
            {
                "path_glob": "qa_results*/ground_truth/*.json",
                "reason": "MODEL_DERIVED structured extraction; not page OCR evidence; not SOURCE_VERIFIED truth",
            },
            {
                "path_glob": "qa_results*/extracted/*.json",
                "reason": "Parser/API extract; not raw OCR text",
            },
            {
                "path_glob": "phase3_eval/records/*.json",
                "reason": "Phase 3 telemetry/outcomes; no raw OCR text",
            },
        ],
    }

    tess_inventory = _ensure_tesseract_outputs(gt_docs)
    tess_inventory["prior_repo_search"] = missing_note

    vl_matches, vl_docs, vl_metrics = _score_engine(
        gt_docs, engine_name="paddleocr-vl-1.6", load_page=_load_vl_page
    )
    tess_matches, tess_docs, tess_metrics = _score_engine(
        gt_docs, engine_name="tesseract", load_page=_load_tess_page
    )

    # Align on identical field keys
    def key(m: Dict[str, Any]) -> Tuple[str, str]:
        return (str(m.get("document_id")), str(m.get("field_id")))

    vl_map = {key(m): m for m in vl_matches}
    tess_map = {key(m): m for m in tess_matches}
    shared_keys = sorted(set(vl_map) & set(tess_map))
    vl_only = sorted(set(vl_map) - set(tess_map))
    tess_only = sorted(set(tess_map) - set(vl_map))

    shared_vl = [vl_map[k] for k in shared_keys]
    shared_tess = [tess_map[k] for k in shared_keys]
    shared_vl_metrics = aggregate_metrics(shared_vl)
    shared_tess_metrics = aggregate_metrics(shared_tess)

    # Pairwise agreement on shared fields
    both_correct = sum(
        1
        for k in shared_keys
        if vl_map[k].get("correct") and tess_map[k].get("correct")
    )
    vl_only_correct = sum(
        1
        for k in shared_keys
        if vl_map[k].get("correct") and not tess_map[k].get("correct")
    )
    tess_only_correct = sum(
        1
        for k in shared_keys
        if tess_map[k].get("correct") and not vl_map[k].get("correct")
    )
    both_wrong = sum(
        1
        for k in shared_keys
        if (not vl_map[k].get("correct")) and (not tess_map[k].get("correct"))
    )

    def doc_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for item in items:
            out[item["status"]] = out.get(item["status"], 0) + 1
        return out

    comparison = {
        "scope": {
            "benchmark_pool_documents": 50,
            "source_verified_documents": len(gt_docs),
            "verified_fields_total": field_count,
            "shared_overlapping_fields": len(shared_keys),
            "vl_only_fields": len(vl_only),
            "tesseract_only_fields": len(tess_only),
            "ground_truth": "SOURCE_VERIFIED human/visual inspection of original documents",
            "not_used_as_truth": [
                "parser output",
                "Phase 3 PASS/FAIL",
                "qa_results*_ground_truth",
                "either OCR engine",
            ],
        },
        "tesseract_inventory": tess_inventory,
        "paddleocr_vl": {
            "engine": "paddleocr-vl-1.6",
            "evidence_source": "phase4/paddle_vl_benchmark/per_document (reused, not rerun)",
            "metrics_all_fields": vl_metrics,
            "metrics_shared_fields": shared_vl_metrics,
            "document_status_counts": doc_counts(vl_docs),
            "fully_correct_documents": sum(
                1 for d in vl_docs if d.get("status") == "FULLY_CORRECT"
            ),
        },
        "tesseract": {
            "engine": "tesseract",
            "evidence_source": str(TESS_DIR),
            "metrics_all_fields": tess_metrics,
            "metrics_shared_fields": shared_tess_metrics,
            "document_status_counts": doc_counts(tess_docs),
            "fully_correct_documents": sum(
                1 for d in tess_docs if d.get("status") == "FULLY_CORRECT"
            ),
        },
        "deltas_shared_fields_vl_minus_tesseract": {
            "numeric_accuracy": _delta(
                shared_vl_metrics.get("numeric_accuracy"),
                shared_tess_metrics.get("numeric_accuracy"),
            ),
            "quantity_accuracy": _delta(
                shared_vl_metrics.get("quantity_accuracy"),
                shared_tess_metrics.get("quantity_accuracy"),
            ),
            "price_accuracy": _delta(
                shared_vl_metrics.get("price_accuracy"),
                shared_tess_metrics.get("price_accuracy"),
            ),
            "date_accuracy": _delta(
                shared_vl_metrics.get("date_accuracy"),
                shared_tess_metrics.get("date_accuracy"),
            ),
            "text_accuracy": _delta(
                shared_vl_metrics.get("text_accuracy"),
                shared_tess_metrics.get("text_accuracy"),
            ),
            "table_column_accuracy": _delta(
                shared_vl_metrics.get("table_column_accuracy"),
                shared_tess_metrics.get("table_column_accuracy"),
            ),
            "silent_wrong_rate": _delta(
                shared_vl_metrics.get("silent_wrong_rate"),
                shared_tess_metrics.get("silent_wrong_rate"),
            ),
            "critical_error_rate": _delta(
                shared_vl_metrics.get("critical_error_rate"),
                shared_tess_metrics.get("critical_error_rate"),
            ),
            "field_accuracy": _delta(
                shared_vl_metrics.get("field_accuracy"),
                shared_tess_metrics.get("field_accuracy"),
            ),
        },
        "pairwise_shared_fields": {
            "both_correct": both_correct,
            "vl_only_correct": vl_only_correct,
            "tesseract_only_correct": tess_only_correct,
            "both_wrong": both_wrong,
        },
        "limitations": [
            "Prior page-level Tesseract OCR dumps were not available; Tesseract evidence was collected for this comparison using the existing TesseractEngine adapter.",
            "PaddleOCR-VL often emits HTML tables enabling column-aware matching; Tesseract plain text usually does not — table/column gaps partly reflect evidence structure.",
            "Accuracy is verified-field accuracy on the 20 SOURCE_VERIFIED documents, not 50-document dataset accuracy.",
            "No bbox IoU comparison (insufficient verified bbox GT).",
        ],
    }

    results = {
        "comparison": comparison,
        "per_document": {
            "paddleocr_vl": vl_docs,
            "tesseract": tess_docs,
        },
        "shared_field_matches": {
            "paddleocr_vl": shared_vl,
            "tesseract": shared_tess,
        },
    }

    out_json = OUT_DIR / "comparison_results.json"
    out_json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    (OUT_DIR / "summary.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
    _write_report(comparison, OUT_DIR / "COMPARISON_REPORT.md")
    _print_final(comparison)
    print(f"Wrote {out_json}")
    return 0


def _write_report(comparison: Dict[str, Any], path: Path) -> None:
    scope = comparison["scope"]
    vl = comparison["paddleocr_vl"]
    tess = comparison["tesseract"]
    svm = vl["metrics_shared_fields"]
    stm = tess["metrics_shared_fields"]
    deltas = comparison["deltas_shared_fields_vl_minus_tesseract"]
    inv = comparison["tesseract_inventory"]
    lines = [
        "# Tesseract vs PaddleOCR-VL 1.6 — Source Ground-Truth Comparison (20 docs)",
        "",
        "## Scope",
        "",
        f"- Benchmark pool: **{scope['benchmark_pool_documents']}** documents (Phase 3 first-50)",
        f"- Source-verified documents scored: **{scope['source_verified_documents']}**",
        f"- Verified fields: **{scope['verified_fields_total']}**",
        f"- Identical overlapping fields used for comparison: **{scope['shared_overlapping_fields']}**",
        "",
        "Ground truth = human/visual inspection of original documents (`SOURCE_VERIFIED`).",
        "Parser output, Phase 3 PASS/FAIL, and OCR engine output were **not** used as truth.",
        "",
        "## Tesseract evidence availability",
        "",
        f"- Prior page-level Tesseract dumps in repo: **{inv.get('prior_repo_search', {}).get('prior_tesseract_page_dumps_found')}**",
        f"- Comparison-folder cache hits: **{inv.get('existing_cache_hits')}**",
        f"- Fresh TesseractEngine runs for this comparison: **{inv.get('fresh_runs')}**",
        "",
        "Missing / incompatible artifacts:",
        "",
    ]
    for item in (inv.get("prior_repo_search") or {}).get("incompatible_artifacts") or []:
        lines.append(f"- `{item['path_glob']}` — {item['reason']}")
    for note in inv.get("notes") or []:
        lines.append(f"- {note}")
    lines.extend(
        [
            "",
            "## Engine evidence sources",
            "",
            f"- PaddleOCR-VL: `{vl['evidence_source']}`",
            f"- Tesseract: `{tess['evidence_source']}`",
            "",
            "## Shared-field accuracy (fair comparison)",
            "",
            "| Metric | Tesseract | PaddleOCR-VL | Δ (VL − Tess) |",
            "| --- | ---: | ---: | ---: |",
            f"| Field accuracy | {_pct(stm.get('field_accuracy'))} | {_pct(svm.get('field_accuracy'))} | {_pct(deltas.get('field_accuracy'))} |",
            f"| Numeric | {_pct(stm.get('numeric_accuracy'))} | {_pct(svm.get('numeric_accuracy'))} | {_pct(deltas.get('numeric_accuracy'))} |",
            f"| Quantity | {_pct(stm.get('quantity_accuracy'))} | {_pct(svm.get('quantity_accuracy'))} | {_pct(deltas.get('quantity_accuracy'))} |",
            f"| Price | {_pct(stm.get('price_accuracy'))} | {_pct(svm.get('price_accuracy'))} | {_pct(deltas.get('price_accuracy'))} |",
            f"| Date | {_pct(stm.get('date_accuracy'))} | {_pct(svm.get('date_accuracy'))} | {_pct(deltas.get('date_accuracy'))} |",
            f"| Text | {_pct(stm.get('text_accuracy'))} | {_pct(svm.get('text_accuracy'))} | {_pct(deltas.get('text_accuracy'))} |",
            f"| Header | {_pct(stm.get('header_accuracy'))} | {_pct(svm.get('header_accuracy'))} | n/a |",
            f"| Table/column | {_pct(stm.get('table_column_accuracy'))} | {_pct(svm.get('table_column_accuracy'))} | {_pct(deltas.get('table_column_accuracy'))} |",
            f"| Silent-wrong rate | {_pct(stm.get('silent_wrong_rate'))} | {_pct(svm.get('silent_wrong_rate'))} | {_pct(deltas.get('silent_wrong_rate'))} |",
            f"| Critical-error rate | {_pct(stm.get('critical_error_rate'))} | {_pct(svm.get('critical_error_rate'))} | {_pct(deltas.get('critical_error_rate'))} |",
            f"| Precision | {_pct(stm.get('precision'))} | {_pct(svm.get('precision'))} | n/a |",
            f"| Recall | {_pct(stm.get('recall'))} | {_pct(svm.get('recall'))} | n/a |",
            f"| F1 | {_pct(stm.get('f1'))} | {_pct(svm.get('f1'))} | n/a |",
            "",
            "## Document-level (FULLY_CORRECT)",
            "",
            f"- Tesseract: **{tess['fully_correct_documents']}** / {scope['source_verified_documents']}",
            f"- PaddleOCR-VL: **{vl['fully_correct_documents']}** / {scope['source_verified_documents']}",
            f"- Tesseract status counts: `{tess['document_status_counts']}`",
            f"- PaddleOCR-VL status counts: `{vl['document_status_counts']}`",
            "",
            "## Pairwise field outcomes (shared fields)",
            "",
            f"- Both correct: **{comparison['pairwise_shared_fields']['both_correct']}**",
            f"- VL only correct: **{comparison['pairwise_shared_fields']['vl_only_correct']}**",
            f"- Tesseract only correct: **{comparison['pairwise_shared_fields']['tesseract_only_correct']}**",
            f"- Both wrong: **{comparison['pairwise_shared_fields']['both_wrong']}**",
            "",
            "## Limitations",
            "",
        ]
    )
    for lim in comparison.get("limitations") or []:
        lines.append(f"- {lim}")
    lines.extend(
        [
            "",
            "## Important",
            "",
            "These figures are **verified-field accuracy on the 20 SOURCE_VERIFIED documents**,",
            "not overall accuracy on the 50-document benchmark pool.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _print_final(comparison: Dict[str, Any]) -> None:
    scope = comparison["scope"]
    vl = comparison["paddleocr_vl"]["metrics_shared_fields"]
    tess = comparison["tesseract"]["metrics_shared_fields"]
    print("=" * 40)
    print("TESSERACT vs PADDLEOCR-VL GT-20")
    print("=" * 40)
    print("Source-verified documents:", scope["source_verified_documents"])
    print("Shared overlapping fields:", scope["shared_overlapping_fields"])
    print("Prior Tesseract dumps found:", comparison["tesseract_inventory"].get("prior_repo_search", {}).get("prior_tesseract_page_dumps_found"))
    print("Fresh Tesseract runs:", comparison["tesseract_inventory"].get("fresh_runs"))
    print()
    print("Metric | Tesseract | PaddleOCR-VL")
    for label, key in [
        ("Numeric", "numeric_accuracy"),
        ("Quantity", "quantity_accuracy"),
        ("Price", "price_accuracy"),
        ("Date", "date_accuracy"),
        ("Text", "text_accuracy"),
        ("Table/column", "table_column_accuracy"),
        ("Silent-wrong", "silent_wrong_rate"),
        ("Field", "field_accuracy"),
    ]:
        print(f"{label}: {_pct(tess.get(key))} | {_pct(vl.get(key))}")
    print(
        "FULLY_CORRECT docs:",
        comparison["tesseract"]["fully_correct_documents"],
        "|",
        comparison["paddleocr_vl"]["fully_correct_documents"],
    )


if __name__ == "__main__":
    raise SystemExit(main())
