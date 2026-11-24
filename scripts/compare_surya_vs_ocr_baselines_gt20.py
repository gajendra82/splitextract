"""Three-engine GT comparison: Surya 2 vs Tesseract vs PaddleOCR-VL 1.6.

- Reuses stored PaddleOCR-VL and Tesseract page evidence (no baseline rerun).
- Runs Surya 2 once per missing page into this comparison folder.
- Scores all engines with identical SOURCE_VERIFIED fields via match_field /
  aggregate_metrics.
- Never uses OCR/parser output as ground truth.
- If Surya cannot run, writes a BLOCKED report with the exact reason.
"""

from __future__ import annotations

import json
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
from services.ocr.paddle_vl_io import load_page_image
from services.ocr.surya_engine import SuryaEngine, evidence_from_result

GT_DIR = ROOT / "phase4" / "ground_truth_50"
VL_PER = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"
TESS_DIR = ROOT / "phase4" / "tesseract_vs_paddle_vl_ground_truth_20" / "tesseract_per_document"
OUT_DIR = ROOT / "phase4" / "surya_vs_ocr_baselines_ground_truth_20"
SURYA_DIR = OUT_DIR / "surya_per_document"
DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")

ENGINE_SURYA = "surya-2"
ENGINE_VL = "paddleocr-vl-1.6"
ENGINE_TESS = "tesseract"


def _safe(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").replace(" ", "_")


def _pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return a - b


def _load_gt_docs() -> List[Dict[str, Any]]:
    docs = []
    for path in sorted((GT_DIR / "per_document").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        ok, _errors = validate_ground_truth_document(payload)
        if not ok:
            continue
        if not only_source_verified_fields(payload):
            continue
        docs.append(payload)
    return docs


def _load_engine_page(base: Path, document_id: str, page: int) -> Optional[Dict[str, Any]]:
    path = base / f"{_safe(document_id)}.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("pages") or []:
        if int(item.get("page") or 1) == int(page):
            return item
    return None


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
            matches = []
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
                matches.append(match)
                all_matches.append(match)
            per_document.append(
                {
                    "document_id": gt["document_id"],
                    "engine": engine_name,
                    "status": "OCR_UNAVAILABLE",
                    "verified_fields": len(fields),
                    "correct_fields": 0,
                    "matches": matches,
                }
            )
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
    return all_matches, per_document, aggregate_metrics(all_matches)


def _prepare_surya_runtime_env() -> Dict[str, str]:
    """Point Surya 2 at a local llama-server when present; never invent backends."""
    import os

    notes: Dict[str, str] = {}
    tools = ROOT / ".tools" / "llama.cpp"
    cuda_bin = tools / "cuda-12.4-x64" / "llama-server.exe"
    cpu_bin = tools / "cpu-x64" / "llama-server.exe"
    chosen = None
    if cuda_bin.is_file():
        chosen = cuda_bin
        notes["llama_variant"] = "cuda-12.4-x64"
        # Prefer GPU layers; llama.cpp will clamp if VRAM is insufficient.
        os.environ.setdefault("LLAMA_CPP_NGL", "99")
    elif cpu_bin.is_file():
        chosen = cpu_bin
        notes["llama_variant"] = "cpu-x64"
        os.environ.setdefault("LLAMA_CPP_NGL", "0")
    if chosen is not None:
        os.environ.setdefault("LLAMA_CPP_BINARY", str(chosen))
        llama_dir = str(chosen.parent)
        path_now = os.environ.get("PATH") or ""
        if llama_dir not in path_now.split(os.pathsep):
            os.environ["PATH"] = llama_dir + os.pathsep + path_now
        os.environ.setdefault("SURYA_INFERENCE_BACKEND", "llamacpp")
        os.environ.setdefault("SURYA_INFERENCE_KEEP_ALIVE", "1")
        notes["llama_server"] = str(chosen)
    return notes


def _ensure_surya_outputs(gt_docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    SURYA_DIR.mkdir(parents=True, exist_ok=True)
    runtime_notes = _prepare_surya_runtime_env()
    inventory: Dict[str, Any] = {
        "cache_hits": 0,
        "fresh_runs": 0,
        "errors": 0,
        "blocked": False,
        "block_reason": None,
        "documents": {},
        "engine": ENGINE_SURYA,
        "runtime": runtime_notes,
    }
    engine = SuryaEngine(keep_alive=True)
    if not engine.available():
        inventory["blocked"] = True
        inventory["block_reason"] = engine.block_reason() or engine.model_info().get(
            "init_error"
        )
        inventory["model_info"] = engine.model_info()
        return inventory

    inventory["model_info"] = engine.model_info()
    for gt in gt_docs:
        doc_id = gt["document_id"]
        page = int(gt.get("page") or 1)
        out_path = SURYA_DIR / f"{_safe(doc_id)}.json"
        existing = _load_engine_page(SURYA_DIR, doc_id, page)
        if existing and existing.get("status") == "success" and (existing.get("text") or "").strip():
            inventory["cache_hits"] += 1
            inventory["documents"][doc_id] = {
                "page": page,
                "status": existing.get("status"),
                "source": "comparison_cache",
            }
            continue

        source = Path((gt.get("source") or {}).get("file") or (DATASET / doc_id))
        page_index = max(0, page - 1)
        started = time.perf_counter()
        image_bytes, image_meta = load_page_image(source, page_index=page_index)
        if not image_bytes:
            page_rec = {
                "page": page,
                "engine": ENGINE_SURYA,
                "status": "error",
                "text": "",
                "numeric_tokens": [],
                "error": image_meta.get("error") or "image_load_failed",
                "latency_ms": (time.perf_counter() - started) * 1000.0,
                "source": "fresh_surya_run",
                "image_meta": image_meta,
            }
            inventory["errors"] += 1
        else:
            result = engine.recognize(
                image_bytes,
                page=page,
                metadata={"source_path": str(source), **image_meta},
            )
            page_rec = evidence_from_result(result)
            page_rec["page"] = page
            page_rec["source"] = "fresh_surya_run"
            page_rec["image_meta"] = image_meta
            if page_rec.get("status") != "success":
                inventory["errors"] += 1
                # Hard execution failure → mark blocked for this run.
                br = page_rec.get("block_reason") or (result.metadata or {}).get("block_reason")
                if br and "BLOCKED" in str(br):
                    inventory["blocked"] = True
                    inventory["block_reason"] = br
                    inventory["documents"][doc_id] = {
                        "page": page,
                        "status": page_rec.get("status"),
                        "error": page_rec.get("error"),
                    }
                    # Persist partial page then stop — do not fabricate.
                    payload = {"document_id": doc_id, "source_file": str(source), "pages": [page_rec]}
                    if out_path.is_file():
                        payload = json.loads(out_path.read_text(encoding="utf-8"))
                        pages = [p for p in (payload.get("pages") or []) if int(p.get("page") or 1) != page]
                        pages.append(page_rec)
                        payload["pages"] = sorted(pages, key=lambda p: int(p.get("page") or 1))
                    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    return inventory
            else:
                inventory["fresh_runs"] += 1

        payload = {"document_id": doc_id, "source_file": str(source), "pages": []}
        if out_path.is_file():
            payload = json.loads(out_path.read_text(encoding="utf-8"))
        pages = [p for p in (payload.get("pages") or []) if int(p.get("page") or 1) != page]
        pages.append(page_rec)
        payload["pages"] = sorted(pages, key=lambda p: int(p.get("page") or 1))
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        inventory["documents"][doc_id] = {
            "page": page,
            "status": page_rec.get("status"),
            "source": page_rec.get("source"),
            "text_length": len(page_rec.get("text") or ""),
            "latency_ms": page_rec.get("latency_ms"),
        }
        print(
            f"[surya] {doc_id} p{page} status={page_rec.get('status')} "
            f"chars={len(page_rec.get('text') or '')}",
            flush=True,
        )
    return inventory


def _doc_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for item in items:
        out[item["status"]] = out.get(item["status"], 0) + 1
    return out


def _field_key(m: Dict[str, Any]) -> Tuple[str, str]:
    return (str(m.get("document_id")), str(m.get("field_id")))


def _pairwise(
    maps: Dict[str, Dict[Tuple[str, str], Dict[str, Any]]],
    shared_keys: List[Tuple[str, str]],
) -> Dict[str, Any]:
    engines = list(maps.keys())
    out: Dict[str, Any] = {"engines": engines}
    # Per-engine correct counts already in metrics; add disagreement sets.
    for i, left in enumerate(engines):
        for right in engines[i + 1 :]:
            only_left = 0
            only_right = 0
            both_ok = 0
            both_bad = 0
            for key in shared_keys:
                a = bool(maps[left][key].get("correct"))
                b = bool(maps[right][key].get("correct"))
                if a and b:
                    both_ok += 1
                elif a and not b:
                    only_left += 1
                elif b and not a:
                    only_right += 1
                else:
                    both_bad += 1
            out[f"{left}_vs_{right}"] = {
                "both_correct": both_ok,
                f"{left}_only_correct": only_left,
                f"{right}_only_correct": only_right,
                "both_wrong": both_bad,
            }
    return out


def _per_document_disagreements(
    per_docs: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for engine, items in per_docs.items():
        for item in items:
            doc_id = item["document_id"]
            by_id.setdefault(doc_id, {"document_id": doc_id, "engines": {}})
            by_id[doc_id]["engines"][engine] = {
                "status": item.get("status"),
                "correct_fields": item.get("correct_fields"),
                "verified_fields": item.get("verified_fields"),
            }
    rows = []
    for doc_id, row in sorted(by_id.items()):
        statuses = {e: info["status"] for e, info in row["engines"].items()}
        corrects = {e: info["correct_fields"] for e, info in row["engines"].items()}
        row["status_disagree"] = len(set(statuses.values())) > 1
        row["correct_field_counts"] = corrects
        row["statuses"] = statuses
        rows.append(row)
    return rows


def _error_category_matrix(
    maps: Dict[str, Dict[Tuple[str, str], Dict[str, Any]]],
    shared_keys: List[Tuple[str, str]],
) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for engine, amap in maps.items():
        counts: Dict[str, int] = {}
        for key in shared_keys:
            m = amap[key]
            if m.get("correct"):
                label = "CORRECT"
            else:
                label = str(m.get("error_type") or "UNKNOWN")
            counts[label] = counts.get(label, 0) + 1
        out[engine] = counts
    return out


def _write_blocked_report(reason: str, inventory: Dict[str, Any], path: Path) -> None:
    lines = [
        "# Surya 2 vs OCR Baselines — BLOCKED",
        "",
        "## Status",
        "",
        "**BLOCKED** — Surya 2 results were not fabricated.",
        "",
        "## Exact reason",
        "",
        reason or "unknown",
        "",
        "## What was reused",
        "",
        "- Ground truth: `phase4/ground_truth_50/per_document` (SOURCE_VERIFIED only)",
        "- PaddleOCR-VL evidence: `phase4/paddle_vl_benchmark/per_document` (not rerun)",
        "- Tesseract evidence: `phase4/tesseract_vs_paddle_vl_ground_truth_20/tesseract_per_document` (not rerun)",
        "",
        "## Surya inventory",
        "",
        "```json",
        json.dumps(inventory, indent=2),
        "```",
        "",
        "## Next steps to unblock",
        "",
        "1. Start Docker Desktop with NVIDIA Container Toolkit (vLLM backend), **or**",
        "2. Install `llama-server` from llama.cpp and ensure it is on PATH, **or**",
        "3. Point `SURYA_INFERENCE_URL` at an already-running OpenAI-compatible Surya server.",
        "",
        "Then re-run: `python scripts/compare_surya_vs_ocr_baselines_gt20.py`",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_report(comparison: Dict[str, Any], path: Path) -> None:
    scope = comparison["scope"]
    engines = comparison["engines"]
    order = [ENGINE_TESS, ENGINE_VL, ENGINE_SURYA]
    metric_keys = [
        ("Field accuracy", "field_accuracy"),
        ("Numeric", "numeric_accuracy"),
        ("Quantity", "quantity_accuracy"),
        ("Price", "price_accuracy"),
        ("Date", "date_accuracy"),
        ("Text", "text_accuracy"),
        ("Header", "header_accuracy"),
        ("Table/column", "table_column_accuracy"),
        ("Silent-wrong rate", "silent_wrong_rate"),
        ("Critical-error rate", "critical_error_rate"),
        ("Precision", "precision"),
        ("Recall", "recall"),
        ("F1", "f1"),
        ("Coverage (recall)", "recall"),
    ]
    header = "| Metric | " + " | ".join(order) + " |"
    sep = "| --- | " + " | ".join(["---:"] * len(order)) + " |"
    rows = [header, sep]
    for label, key in metric_keys:
        cells = [_pct(engines[e]["metrics_shared_fields"].get(key)) for e in order]
        rows.append(f"| {label} | " + " | ".join(cells) + " |")

    lines = [
        "# Surya 2 vs Tesseract vs PaddleOCR-VL 1.6 — Source GT Comparison (20 docs)",
        "",
        "## Scope",
        "",
        f"- Source-verified documents: **{scope['source_verified_documents']}**",
        f"- Verified fields: **{scope['verified_fields_total']}**",
        f"- Identical overlapping fields: **{scope['shared_overlapping_fields']}**",
        "",
        "Ground truth = human/visual inspection of original documents (`SOURCE_VERIFIED`).",
        "OCR/parser outputs were **not** used as truth.",
        "",
        "## Evidence sources",
        "",
        f"- Tesseract: `{engines[ENGINE_TESS]['evidence_source']}` (reused)",
        f"- PaddleOCR-VL: `{engines[ENGINE_VL]['evidence_source']}` (reused)",
        f"- Surya 2: `{engines[ENGINE_SURYA]['evidence_source']}`",
        "",
        f"- Surya package/backend: `{engines[ENGINE_SURYA].get('model_info')}`",
        "",
        "## Shared-field accuracy (apples-to-apples)",
        "",
        *rows,
        "",
        "## Document-level FULLY_CORRECT",
        "",
    ]
    for e in order:
        lines.append(
            f"- {e}: **{engines[e]['fully_correct_documents']}** / "
            f"{scope['source_verified_documents']} "
            f"`{engines[e]['document_status_counts']}`"
        )
    lines.extend(
        [
            "",
            "## Pairwise field outcomes (shared fields)",
            "",
            "```json",
            json.dumps(comparison["pairwise_shared_fields"], indent=2),
            "```",
            "",
            "## Error categories (shared fields)",
            "",
            "```json",
            json.dumps(comparison["error_categories"], indent=2),
            "```",
            "",
            "## Per-document disagreements (status / correct counts)",
            "",
        ]
    )
    disagree = [d for d in comparison["per_document_disagreements"] if d.get("status_disagree")]
    lines.append(f"Documents with status disagreement: **{len(disagree)}** / {scope['source_verified_documents']}")
    lines.append("")
    for row in comparison["per_document_disagreements"]:
        lines.append(
            f"- `{row['document_id']}` statuses={row['statuses']} correct={row['correct_field_counts']}"
        )
    lines.extend(
        [
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
            "## Recommendation (not an automatic winner)",
            "",
            comparison.get("recommendation") or "",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _recommendation(
    metrics: Dict[str, Dict[str, Any]],
    *,
    n_docs: int,
    n_fields: int,
) -> str:
    """Human recommendation from measured accuracy + silent-wrong. No auto-winner label."""
    order = [ENGINE_TESS, ENGINE_VL, ENGINE_SURYA]
    field_acc = {e: metrics[e].get("field_accuracy") for e in order}
    silent = {e: metrics[e].get("silent_wrong_rate") for e in order}
    fully = {e: metrics[e].get("_fully_correct") for e in order}
    ranked = sorted(
        order,
        key=lambda e: (
            field_acc[e] is not None,
            field_acc[e] or -1.0,
            -1.0 * (silent[e] if silent[e] is not None else 1.0),
        ),
        reverse=True,
    )
    best = ranked[0]
    parts = [
        f"On this limited sample ({n_docs} SOURCE_VERIFIED documents / {n_fields} fields), "
        f"**{best}** has the highest measured field accuracy "
        f"({_pct(field_acc[best])}) among engines that produced scorable evidence.",
        "Silent-wrong rates: "
        + ", ".join(f"{e}={_pct(silent[e])}" for e in order)
        + ".",
        "FULLY_CORRECT documents: "
        + ", ".join(f"{e}={fully[e]}" for e in order)
        + ".",
    ]
    # Tradeoff note when VL silent-wrong is higher
    if (
        field_acc.get(ENGINE_VL) is not None
        and field_acc.get(ENGINE_SURYA) is not None
        and silent.get(ENGINE_VL) is not None
        and silent.get(ENGINE_SURYA) is not None
        and (field_acc[ENGINE_VL] or 0) >= (field_acc[ENGINE_SURYA] or 0)
        and (silent[ENGINE_VL] or 0) > (silent[ENGINE_SURYA] or 0)
    ):
        parts.append(
            "PaddleOCR-VL leads on field accuracy here but shows a higher silent-wrong "
            "rate than Surya; if silent errors are costlier than misses, re-check VL "
            "COLUMN_CONFUSION cases before preferring it operationally."
        )
    if (
        field_acc.get(ENGINE_TESS) is not None
        and silent.get(ENGINE_TESS) == 0.0
        and (field_acc[ENGINE_TESS] or 0) < (field_acc.get(best) or 0)
    ):
        parts.append(
            "Tesseract’s silent-wrong rate is lowest (often MISSING rather than wrong), "
            "but its field/recall lag makes it a weaker sole engine on this sample."
        )
    parts.append(
        "This is **not** an automatic production winner selection — the 20-document "
        "sample is too small to generalize; expand SOURCE_VERIFIED coverage before "
        "changing OCR defaults."
    )
    return " ".join(parts)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    gt_docs = _load_gt_docs()
    field_count = sum(len(only_source_verified_fields(g)) for g in gt_docs)
    print(f"GT docs={len(gt_docs)} fields={field_count}")

    # Baseline presence checks (reuse only).
    missing_baselines = []
    for gt in gt_docs:
        doc_id = gt["document_id"]
        page = int(gt.get("page") or 1)
        if _load_engine_page(VL_PER, doc_id, page) is None:
            missing_baselines.append(f"vl:{doc_id}")
        if _load_engine_page(TESS_DIR, doc_id, page) is None:
            missing_baselines.append(f"tesseract:{doc_id}")

    surya_inventory = _ensure_surya_outputs(gt_docs)
    if surya_inventory.get("blocked"):
        reason = surya_inventory.get("block_reason") or "surya_blocked"
        blocked_payload = {
            "status": "BLOCKED",
            "block_reason": reason,
            "scope": {
                "source_verified_documents": len(gt_docs),
                "verified_fields_total": field_count,
            },
            "surya_inventory": surya_inventory,
            "missing_baselines": missing_baselines,
            "note": "No fabricated Surya metrics. Baseline artifacts were not modified.",
        }
        (OUT_DIR / "comparison_results.json").write_text(
            json.dumps(blocked_payload, indent=2), encoding="utf-8"
        )
        (OUT_DIR / "summary.json").write_text(
            json.dumps(blocked_payload, indent=2), encoding="utf-8"
        )
        _write_blocked_report(str(reason), surya_inventory, OUT_DIR / "COMPARISON_REPORT.md")
        print("BLOCKED:", reason)
        return 2

    loaders = {
        ENGINE_VL: lambda d, p: _load_engine_page(VL_PER, d, p),
        ENGINE_TESS: lambda d, p: _load_engine_page(TESS_DIR, d, p),
        ENGINE_SURYA: lambda d, p: _load_engine_page(SURYA_DIR, d, p),
    }
    scored: Dict[str, Any] = {}
    maps: Dict[str, Dict[Tuple[str, str], Dict[str, Any]]] = {}
    per_docs: Dict[str, List[Dict[str, Any]]] = {}
    for engine_name, loader in loaders.items():
        matches, docs, metrics = _score_engine(
            gt_docs, engine_name=engine_name, load_page=loader
        )
        scored[engine_name] = {
            "matches": matches,
            "docs": docs,
            "metrics": metrics,
        }
        maps[engine_name] = {_field_key(m): m for m in matches}
        per_docs[engine_name] = docs

    shared_keys = sorted(set.intersection(*(set(m.keys()) for m in maps.values())))
    shared_metrics = {
        e: aggregate_metrics([maps[e][k] for k in shared_keys]) for e in maps
    }
    for e in maps:
        shared_metrics[e]["_fully_correct"] = sum(
            1 for d in per_docs[e] if d.get("status") == "FULLY_CORRECT"
        )

    engines_out = {}
    evidence_sources = {
        ENGINE_VL: "phase4/paddle_vl_benchmark/per_document (reused, not rerun)",
        ENGINE_TESS: str(TESS_DIR) + " (reused, not rerun)",
        ENGINE_SURYA: str(SURYA_DIR),
    }
    for e in maps:
        engines_out[e] = {
            "engine": e,
            "evidence_source": evidence_sources[e],
            "metrics_all_fields": scored[e]["metrics"],
            "metrics_shared_fields": {
                k: v for k, v in shared_metrics[e].items() if not str(k).startswith("_")
            },
            "document_status_counts": _doc_counts(per_docs[e]),
            "fully_correct_documents": shared_metrics[e]["_fully_correct"],
            "model_info": surya_inventory.get("model_info") if e == ENGINE_SURYA else None,
        }

    comparison = {
        "status": "OK",
        "scope": {
            "benchmark_pool_documents": 50,
            "source_verified_documents": len(gt_docs),
            "verified_fields_total": field_count,
            "shared_overlapping_fields": len(shared_keys),
            "ground_truth": "SOURCE_VERIFIED human/visual inspection of original documents",
            "not_used_as_truth": [
                "parser output",
                "Phase 3 PASS/FAIL",
                "qa_results*_ground_truth",
                "any OCR engine output",
            ],
            "missing_baselines": missing_baselines,
        },
        "surya_inventory": surya_inventory,
        "engines": engines_out,
        "deltas_shared_fields": {
            "surya_minus_tesseract": {
                k: _delta(
                    shared_metrics[ENGINE_SURYA].get(k),
                    shared_metrics[ENGINE_TESS].get(k),
                )
                for k in (
                    "field_accuracy",
                    "numeric_accuracy",
                    "quantity_accuracy",
                    "price_accuracy",
                    "date_accuracy",
                    "text_accuracy",
                    "table_column_accuracy",
                    "silent_wrong_rate",
                    "critical_error_rate",
                )
            },
            "surya_minus_paddleocr_vl": {
                k: _delta(
                    shared_metrics[ENGINE_SURYA].get(k),
                    shared_metrics[ENGINE_VL].get(k),
                )
                for k in (
                    "field_accuracy",
                    "numeric_accuracy",
                    "quantity_accuracy",
                    "price_accuracy",
                    "date_accuracy",
                    "text_accuracy",
                    "table_column_accuracy",
                    "silent_wrong_rate",
                    "critical_error_rate",
                )
            },
            "paddleocr_vl_minus_tesseract": {
                k: _delta(
                    shared_metrics[ENGINE_VL].get(k),
                    shared_metrics[ENGINE_TESS].get(k),
                )
                for k in (
                    "field_accuracy",
                    "numeric_accuracy",
                    "quantity_accuracy",
                    "price_accuracy",
                    "date_accuracy",
                    "text_accuracy",
                    "table_column_accuracy",
                    "silent_wrong_rate",
                    "critical_error_rate",
                )
            },
        },
        "pairwise_shared_fields": _pairwise(maps, shared_keys),
        "error_categories": _error_category_matrix(maps, shared_keys),
        "per_document_disagreements": _per_document_disagreements(per_docs),
        "limitations": [
            "Accuracy is verified-field accuracy on 20 SOURCE_VERIFIED documents, not the 50-doc pool.",
            "PaddleOCR-VL/Surya may emit HTML tables enabling column-aware matching; Tesseract usually does not.",
            "No bbox IoU comparison (insufficient verified bbox GT).",
            "Existing VL/Tesseract artifacts reused; only Surya evidence was newly collected under this folder.",
        ],
        "recommendation": _recommendation(
            shared_metrics, n_docs=len(gt_docs), n_fields=len(shared_keys)
        ),
    }

    results = {
        "comparison": comparison,
        "per_document": per_docs,
        "shared_field_matches": {
            e: [maps[e][k] for k in shared_keys] for e in maps
        },
    }
    (OUT_DIR / "comparison_results.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    (OUT_DIR / "summary.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
    _write_report(comparison, OUT_DIR / "COMPARISON_REPORT.md")

    print("=" * 40)
    print("SURYA vs TESSERACT vs PADDLEOCR-VL GT-20")
    print("=" * 40)
    print("Shared fields:", len(shared_keys))
    for e in [ENGINE_TESS, ENGINE_VL, ENGINE_SURYA]:
        m = shared_metrics[e]
        print(
            f"{e}: field={_pct(m.get('field_accuracy'))} "
            f"numeric={_pct(m.get('numeric_accuracy'))} "
            f"silent={_pct(m.get('silent_wrong_rate'))} "
            f"FULLY_CORRECT={m.get('_fully_correct')}"
        )
    print("Recommendation:", comparison["recommendation"])
    print(f"Wrote {OUT_DIR / 'comparison_results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
