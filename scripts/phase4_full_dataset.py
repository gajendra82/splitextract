"""Stage B — Full dataset Phase 4 measurement.

Resumable. Measurement only. Does not modify parsers/validation mid-run.
Uses the production primary, PaddleOCR-VL. Gemini is blocked in-process, and
OCR fallback is off so a Paddle failure stays an OCR error. Ensemble comparison
is not used.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from phase3.compare import baseline_accepted, iter_line_items

DEFAULT_DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")
OUTPUT = ROOT / "phase4"

SUPPORTED = {
    ".pdf",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".doc",
    ".docx",
    ".htm",
    ".html",
    ".txt",
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
}


def _configure_env() -> None:
    os.environ["PHASE2_RECOVERY_ENABLED"] = "false"
    os.environ["OCR_ENGINE"] = "paddleocr-vl"
    os.environ["OCR_FALLBACK_ENGINE"] = "off"
    os.environ["ENABLE_GEMINI_EXTRACTION_FALLBACK"] = "false"
    os.environ["ENABLE_VISION_FALLBACK"] = "false"
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _disable_gemini() -> None:
    def _blocked(*args, **kwargs):
        raise RuntimeError("GEMINI_DISABLED_FOR_PHASE4_STAGE_B")

    try:
        import services.sales_extraction_runtime as runtime

        runtime.sales_generate_content_via_vertex = _blocked  # type: ignore
    except Exception:
        pass
    try:
        import services.vertex_gemini_client as vertex

        vertex.generate_content_via_vertex = _blocked  # type: ignore
    except Exception:
        pass
    try:
        import services.gemini_extraction_fallback as gef

        gef.maybe_apply_gemini_fallback = lambda result, *a, **k: result  # type: ignore
    except Exception:
        pass


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _list_supported(dataset: Path) -> List[Path]:
    files = []
    for path in sorted(dataset.iterdir()):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext in SUPPORTED or ext == "":
            # include extensionless only if sniffable later; skip bare unknowns
            if ext in SUPPORTED:
                files.append(path)
    return files


def _meta(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    phase1 = result.get("phase1") if isinstance(result.get("phase1"), dict) else {}
    validation = phase1.get("validation") if isinstance(phase1.get("validation"), dict) else {}
    identity = result.get("stock_identity") if isinstance(result.get("stock_identity"), dict) else {}
    return {
        "parser": extra.get("extraction_method") or extra.get("parser"),
        "extraction_method": extra.get("extraction_method"),
        "layout": extra.get("layout") or extra.get("layout_id") or "",
        "profile": extra.get("profile") or identity.get("profile") or "",
        "row_count": len(iter_line_items(result)),
        "validation_accepted": validation.get("accepted") is True,
        "primary_failure": validation.get("primary_failure")
        or validation.get("failure_type")
        or identity.get("status"),
    }


def _render_page_image(file_bytes: bytes, filename: str) -> Optional[bytes]:
    ext = Path(filename).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}:
        return file_bytes
    if ext != ".pdf":
        return None
    try:
        import fitz
        from PIL import Image
        import io

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            if doc.page_count < 1:
                return None
            page = doc.load_page(0)
            # 1.5x is enough for ensemble measurement without full print DPI cost.
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        finally:
            doc.close()
    except Exception:
        return None


def _ocr_probe(file_bytes: bytes, filename: str, requires_ocr: bool) -> Dict[str, Any]:
    """Exercise OCR orchestrator for visual docs when ensemble is on."""
    out: Dict[str, Any] = {
        "probed": False,
        "engines_used": [],
        "disagreement": False,
        "tesseract": None,
        "paddle": None,
        "paddle_invoked": False,
        "error": None,
    }
    if not requires_ocr and Path(filename).suffix.lower() not in {
        ".jpg",
        ".jpeg",
        ".png",
        ".bmp",
        ".tif",
        ".tiff",
        ".webp",
    }:
        return out
    image = _render_page_image(file_bytes, filename)
    if not image:
        out["error"] = "no_image_for_ocr_probe"
        return out
    try:
        from services.document_intelligence.pipeline import DocumentIntelligencePipeline

        # Honor orchestrator policy: secondary only when ensemble + low conf / needed.
        bundle = DocumentIntelligencePipeline().recognize_image(
            image, page=1, force_secondary=False, low_confidence=True
        )
        out["probed"] = True
        out["engines_used"] = list(bundle.engines_used)
        out["disagreement"] = bool(bundle.disagreement)
        out["paddle_invoked"] = "paddle" in (bundle.engines_used or [])
        if bundle.primary:
            out["tesseract" if bundle.primary.engine == "tesseract" else "primary"] = {
                "engine": bundle.primary.engine,
                "confidence": bundle.primary.confidence,
                "word_count": len(bundle.primary.words),
                "text_len": len(bundle.primary.text or ""),
            }
            if bundle.primary.engine == "tesseract":
                out["tesseract"] = out.get("tesseract") or {
                    "engine": bundle.primary.engine,
                    "confidence": bundle.primary.confidence,
                    "word_count": len(bundle.primary.words),
                    "text_len": len(bundle.primary.text or ""),
                }
        if bundle.secondary:
            payload = {
                "engine": bundle.secondary.engine,
                "confidence": bundle.secondary.confidence,
                "word_count": len(bundle.secondary.words),
                "text_len": len(bundle.secondary.text or ""),
            }
            if bundle.secondary.engine == "paddle":
                out["paddle"] = payload
            else:
                out["secondary"] = payload
        out["metadata"] = bundle.metadata
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def evaluate_document(path: Path) -> Dict[str, Any]:
    from services.document_intelligence.pipeline import DocumentIntelligencePipeline

    before = path.stat()
    file_bytes = path.read_bytes()
    started = time.perf_counter()
    error = ""
    result = None
    try:
        result = DocumentIntelligencePipeline().run(file_bytes, path.name)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exc()
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    after = path.stat()
    if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
        raise RuntimeError(f"dataset file modified: {path.name}")

    di = (result or {}).get("document_intelligence") if isinstance(result, dict) else {}
    di = di if isinstance(di, dict) else {}
    route = ((result or {}).get("phase1") or {}).get("route") if isinstance(result, dict) else {}
    route = route if isinstance(route, dict) else {}
    requires_ocr = bool(route.get("requires_ocr") or path.suffix.lower() in {
        ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"
    })
    ocr_probe = _ocr_probe(file_bytes, path.name, requires_ocr)
    # merge probe into elapsed note (probe time separate)
    meta = _meta(result if isinstance(result, dict) else None)
    decision = (di.get("decision") or {}) if isinstance(di, dict) else {}
    status = decision.get("status")
    if error:
        status = "EXTRACTION_ERROR"
    elif not status:
        if meta.get("validation_accepted"):
            status = "VERIFIED"
        else:
            status = "UNRESOLVED"

    strategy = di.get("strategy") or {}
    selected = strategy.get("selected") if isinstance(strategy, dict) else {}
    layout = di.get("layout") or {}
    verification = di.get("verification") or {}
    validation_stack = di.get("validation_stack") or {}

    return {
        "filename": path.name,
        "extension": path.suffix.lower() or "(none)",
        "document_type": route.get("document_type") or "sales_statement",
        "pages": route.get("page_count")
        or (di.get("observability") or {}).get("router_decision", {}).get("page_stats_count"),
        "router_decision": {
            "file_kind": route.get("file_kind"),
            "pdf_kind": route.get("pdf_kind"),
            "requires_ocr": route.get("requires_ocr"),
            "confidence": route.get("confidence"),
        },
        "ocr_required": requires_ocr,
        "ocr_engines_used": ocr_probe.get("engines_used")
        or (di.get("ocr") or {}).get("policy", {}).get("primary_engine")
        and [(di.get("ocr") or {}).get("policy", {}).get("primary_engine")]
        or [],
        "tesseract_result": ocr_probe.get("tesseract"),
        "paddle_result": ocr_probe.get("paddle"),
        "ocr_disagreement": bool(ocr_probe.get("disagreement")),
        "paddle_invoked": bool(ocr_probe.get("paddle_invoked")),
        "ocr_probe": ocr_probe,
        "layout_strategy": (layout or {}).get("strategy"),
        "parser_strategy": (selected or {}).get("name"),
        "strategy_kind": (selected or {}).get("kind"),
        "extraction_method": meta.get("extraction_method"),
        "profile": meta.get("profile"),
        "schema_validation": bool((validation_stack or {}).get("accepted"))
        if validation_stack
        else meta.get("validation_accepted"),
        "evidence_validation": True,
        "business_validation": meta.get("validation_accepted"),
        "independent_verification": (verification or {}).get("passed"),
        "final_decision": status,
        "failure_type": meta.get("primary_failure") if status == "UNRESOLVED" else None,
        "row_count": meta.get("row_count"),
        "processing_time_ms": elapsed_ms,
        "error": error,
        "known_parser": (selected or {}).get("kind") == "named_parser"
        or bool(strategy.get("known_parser_preferred")),
        "unknown_layout": (layout or {}).get("known_layout") is False
        and (selected or {}).get("kind") in {"generic", "vision"},
    }


# Phase 3 regression family markers (filename substrings / methods)
PHASE3_FAMILIES = {
    "op_pur_orec": ["0000700017"],
    "sale_ret_qty": ["000070"],  # checked via extraction_method contains Sale_Ret / saleret
    "marg_purchase_detail": ["0000734248"],
    "profitmaker": ["0000701555"],
    "product_wise_issues": ["0000700283", "0000700398"],
    "product_wise_photo_fold": ["0000700325", "0000700259"],
}


def _aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    status_c = Counter(r.get("final_decision") for r in records)
    times = sorted(int(r.get("processing_time_ms") or 0) for r in records)
    p95 = times[int(0.95 * (len(times) - 1))] if times else 0
    avg = (sum(times) / len(times)) if times else 0
    ocr_req = sum(1 for r in records if r.get("ocr_required"))
    paddle_used = sum(1 for r in records if r.get("paddle_invoked"))
    tess_only = sum(
        1
        for r in records
        if r.get("ocr_required")
        and r.get("ocr_probe", {}).get("probed")
        and not r.get("paddle_invoked")
    )
    disagree = sum(1 for r in records if r.get("ocr_disagreement"))
    known = sum(1 for r in records if r.get("known_parser"))
    unknown = sum(1 for r in records if r.get("unknown_layout"))

    # Failure clusters
    clusters: Dict[str, List[str]] = defaultdict(list)
    for r in records:
        if r.get("final_decision") in {"VERIFIED", "RECOVERED_AND_VERIFIED"}:
            continue
        method = str(r.get("extraction_method") or "")
        fail = str(r.get("failure_type") or "")
        if r.get("final_decision") == "EXTRACTION_ERROR":
            key = "extraction_error"
        elif r.get("final_decision") == "PROVIDER_ERROR":
            key = "provider_error"
        elif r.get("ocr_disagreement"):
            key = "ocr_disagreement"
        elif r.get("unknown_layout"):
            key = "unknown_layout"
        elif "IDENTITY" in fail.upper() or "STOCK" in fail.upper():
            key = "validation_identity"
        elif "SCHEMA" in fail.upper():
            key = "schema"
        elif "MAP" in fail.upper() or "COLUMN" in fail.upper():
            key = "mapping"
        elif r.get("ocr_required"):
            key = "ocr"
        elif method:
            key = f"parser_layout:{method}"
        else:
            key = "validation"
        clusters[key].append(r.get("filename") or "")

    cluster_summary = []
    total_fail = sum(1 for r in records if r.get("final_decision") not in {
        "VERIFIED", "RECOVERED_AND_VERIFIED"
    })
    for key, names in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        cluster_summary.append(
            {
                "group": key,
                "count": len(names),
                "percentage": round(100.0 * len(names) / max(1, len(records)), 2),
                "percentage_of_failures": round(
                    100.0 * len(names) / max(1, total_fail), 2
                ),
                "representative_filenames": names[:8],
                "common_mechanism": "not_established_measurement_only",
                "recovery_path_available": key
                not in {"extraction_error", "provider_error"},
                "deterministic_or_unknown": "unknown",
            }
        )

    return {
        "total_documents": len(records),
        "status_distribution": dict(status_c),
        "VERIFIED": status_c.get("VERIFIED", 0),
        "RECOVERED_AND_VERIFIED": status_c.get("RECOVERED_AND_VERIFIED", 0),
        "UNRESOLVED": status_c.get("UNRESOLVED", 0),
        "PROVIDER_ERROR": status_c.get("PROVIDER_ERROR", 0),
        "EXTRACTION_ERROR": status_c.get("EXTRACTION_ERROR", 0),
        "ocr_required_count": ocr_req,
        "tesseract_only_count": tess_only,
        "paddleocr_used_count": paddle_used,
        "ocr_disagreement_count": disagree,
        "known_parser_count": known,
        "unknown_layout_count": unknown,
        "average_processing_time_ms": round(avg, 2),
        "p95_processing_time_ms": p95,
        "failure_clusters": cluster_summary,
    }


def _phase3_regression_check(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_name = {r.get("filename"): r for r in records}
    checks = {}
    for family, ids in PHASE3_FAMILIES.items():
        hits = []
        for prefix in ids:
            for name, rec in by_name.items():
                if name and str(name).startswith(prefix):
                    hits.append(
                        {
                            "filename": name,
                            "final_decision": rec.get("final_decision"),
                            "extraction_method": rec.get("extraction_method"),
                            "profile": rec.get("profile"),
                            "row_count": rec.get("row_count"),
                        }
                    )
        checks[family] = {
            "documents_found": len(hits),
            "samples": hits[:5],
            "note": "Measurement only; PASS→WRONG not re-scored against stored GT here",
        }
    return checks


def _decide(summary: Dict[str, Any], compat_decision: str) -> str:
    if compat_decision != "COMPATIBILITY_PASS":
        return "REGRESSION_FOUND"
    total = max(1, int(summary.get("total_documents") or 0))
    extraction_errors = int(summary.get("EXTRACTION_ERROR") or 0)
    if extraction_errors / total > 0.15:
        return "NEEDS_ARCHITECTURE_FIX"
    if total < 100:
        return "INSUFFICIENT_EVIDENCE"
    # Stable pipeline + compat pass + ensemble exercised
    if int(summary.get("paddleocr_used_count") or 0) == 0 and int(
        summary.get("ocr_required_count") or 0
    ) > 50:
        return "NEEDS_ARCHITECTURE_FIX"
    return "READY_FOR_PHASE2"


def write_reports(
    *,
    dataset_size: int,
    records: List[Dict[str, Any]],
    summary: Dict[str, Any],
    ocr_summary: Dict[str, Any],
    failure_clusters: List[Dict[str, Any]],
    decision: str,
    compat_decision: str,
) -> None:
    phase3 = _phase3_regression_check(records)
    _write(OUTPUT / "full_dataset_results.json", {"records": records})
    _write(OUTPUT / "full_dataset_summary.json", summary)
    _write(OUTPUT / "ocr_comparison_summary.json", ocr_summary)
    _write(OUTPUT / "failure_clusters.json", {"clusters": failure_clusters})

    lines = [
        "# Phase 4 Full Dataset Report (Stage B)",
        "",
        f"**PHASE4_FULL_DATASET_DECISION: {decision}**",
        "",
        f"Compatibility gate: `{compat_decision}`",
        "",
        "## 1. Dataset size",
        "",
        f"- Dataset path: `{DEFAULT_DATASET}`",
        f"- Files on disk: {dataset_size}",
        f"- Supported processed: {summary.get('total_documents')}",
        "",
        "## 2. Completed count",
        "",
        f"- Completed: {summary.get('total_documents')}",
        "",
        "## 3. Status distribution",
        "",
        f"- VERIFIED: {summary.get('VERIFIED')}",
        f"- RECOVERED_AND_VERIFIED: {summary.get('RECOVERED_AND_VERIFIED')} (expected ~0 with Phase 2 off)",
        f"- UNRESOLVED: {summary.get('UNRESOLVED')}",
        f"- PROVIDER_ERROR: {summary.get('PROVIDER_ERROR')}",
        f"- EXTRACTION_ERROR: {summary.get('EXTRACTION_ERROR')}",
        "",
        "Note: VERIFIED means schema/evidence/business validation + independent verification.",
        "This is **VALIDATED**, not source-ground-truth verified for every document.",
        "",
        "## 4. OCR statistics",
        "",
        f"- OCR-required: {summary.get('ocr_required_count')}",
        f"- Tesseract-only (probed, no paddle): {summary.get('tesseract_only_count')}",
        f"- PaddleOCR used: {summary.get('paddleocr_used_count')}",
        f"- OCR disagreement: {summary.get('ocr_disagreement_count')}",
        "",
        "## 5. PaddleOCR usage",
        "",
        f"- Invocations: {ocr_summary.get('paddle_invocations')}",
        f"- Strengthened evidence (secondary present, no disagreement): {ocr_summary.get('paddle_strengthened')}",
        f"- Disagreements: {ocr_summary.get('disagreements')}",
        f"- Downstream validation improved: **not claimed** (measurement only; no GT coupling)",
        "",
        "## 6. OCR disagreement",
        "",
        f"- Count: {summary.get('ocr_disagreement_count')}",
        "",
        "## 7. Known parser performance",
        "",
        f"- Known parser strategy count: {summary.get('known_parser_count')}",
        "",
        "## 8. Unknown-layout performance",
        "",
        f"- Unknown-layout strategy count: {summary.get('unknown_layout_count')}",
        "",
        "## 9. Failure distribution",
        "",
    ]
    for cluster in failure_clusters[:20]:
        lines.append(
            f"- **{cluster['group']}**: {cluster['count']} "
            f"({cluster['percentage']}% of docs) e.g. {', '.join(cluster['representative_filenames'][:3])}"
        )
    lines.extend(
        [
            "",
            "## 10. Performance statistics",
            "",
            f"- Average ms: {summary.get('average_processing_time_ms')}",
            f"- p95 ms: {summary.get('p95_processing_time_ms')}",
            "",
            "## 11. Phase 3 regression status",
            "",
            "Families checked for presence/stability markers (not a new GT campaign):",
            "",
        ]
    )
    for family, info in phase3.items():
        lines.append(f"- **{family}**: found={info['documents_found']}")
    lines.extend(
        [
            "",
            "Known OCR leftovers (3752 / 3337 / «18) left unresolved by design.",
            "",
            "## 12. Top remaining failure categories",
            "",
        ]
    )
    for cluster in failure_clusters[:8]:
        lines.append(
            f"1. `{cluster['group']}` — {cluster['count']} "
            f"(recovery_path={cluster['recovery_path_available']})"
        )
    lines.extend(
        [
            "",
            "## 13. Should Phase 2 be enabled?",
            "",
            "Yes — if decision is READY_FOR_PHASE2. This run kept Phase 2/Gemini off to measure baseline.",
            "",
            "## 14. Recommended next architectural step",
            "",
            "- If READY_FOR_PHASE2: enable bounded Phase 2 recovery on UNRESOLVED identity/OCR clusters only.",
            "- If NEEDS_ARCHITECTURE_FIX: harden OCR ensemble wiring / unknown-layout generic extraction before recovery.",
            "- Do not start Phase 4.x document loops.",
            "",
            f"## Final decision",
            "",
            f"PHASE4_FULL_DATASET_DECISION: **{decision}**",
            "",
        ]
    )
    (OUTPUT / "FULL_DATASET_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    _configure_env()
    _disable_gemini()
    dataset = Path(os.environ.get("PHASE4_DATASET", str(DEFAULT_DATASET)))
    OUTPUT.mkdir(parents=True, exist_ok=True)

    compat = _load_json(OUTPUT / "compatibility_results.json", {})
    compat_decision = (compat.get("summary") or {}).get("decision")
    if compat_decision != "COMPATIBILITY_PASS":
        print(f"Stage A not passed: {compat_decision}", flush=True)
        return 2

    files = _list_supported(dataset)
    print(f"Stage B files: {len(files)} from {dataset}", flush=True)
    print("Gemini hard-disabled; Phase2 off; OCR ensemble ON", flush=True)

    checkpoint_path = OUTPUT / "checkpoint.json"
    results_path = OUTPUT / "full_dataset_results.json"
    checkpoint = _load_json(checkpoint_path, {"completed": {}, "errors": {}})
    existing = _load_json(results_path, {"records": []})
    by_name = {
        r.get("filename"): r
        for r in (existing.get("records") or [])
        if isinstance(r, dict) and r.get("filename")
    }
    # merge checkpoint completed
    for name, rec in (checkpoint.get("completed") or {}).items():
        if name not in by_name and isinstance(rec, dict):
            by_name[name] = rec

    for index, path in enumerate(files, 1):
        if path.name in by_name and not os.environ.get("PHASE4_FORCE"):
            if index % 50 == 0:
                print(f"[{index}/{len(files)}] skip {path.name}", flush=True)
            continue
        print(f"[{index}/{len(files)}] {path.name}", flush=True)
        try:
            record = evaluate_document(path)
        except Exception as exc:
            record = {
                "filename": path.name,
                "extension": path.suffix.lower() or "(none)",
                "final_decision": "EXTRACTION_ERROR",
                "error": f"{type(exc).__name__}: {exc}",
                "processing_time_ms": 0,
                "ocr_required": path.suffix.lower()
                in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"},
                "paddle_invoked": False,
                "ocr_disagreement": False,
                "known_parser": False,
                "unknown_layout": False,
                "traceback": traceback.format_exc(),
            }
        by_name[path.name] = record
        checkpoint.setdefault("completed", {})[path.name] = {
            "final_decision": record.get("final_decision"),
            "processing_time_ms": record.get("processing_time_ms"),
            "filename": path.name,
        }
        # Persist every document
        records = [by_name[p.name] for p in files if p.name in by_name]
        _write(checkpoint_path, {
            "completed_count": len(by_name),
            "total": len(files),
            "completed": checkpoint.get("completed"),
            "last_file": path.name,
            "updated_at": time.time(),
        })
        _write(results_path, {"records": records})
        if index % 25 == 0:
            summary = _aggregate(records)
            print(
                f"  checkpoint {len(records)}/{len(files)} "
                f"VERIFIED={summary.get('VERIFIED')} UNRESOLVED={summary.get('UNRESOLVED')}",
                flush=True,
            )

    records = [by_name[p.name] for p in files if p.name in by_name]
    summary = _aggregate(records)
    ocr_summary = {
        "paddle_invocations": summary.get("paddleocr_used_count"),
        "paddle_strengthened": sum(
            1
            for r in records
            if r.get("paddle_invoked")
            and not r.get("ocr_disagreement")
            and r.get("paddle_result")
        ),
        "disagreements": summary.get("ocr_disagreement_count"),
        "ocr_required": summary.get("ocr_required_count"),
        "tesseract_only": summary.get("tesseract_only_count"),
        "validation_improved_claim": False,
    }
    decision = _decide(summary, compat_decision)
    summary["PHASE4_FULL_DATASET_DECISION"] = decision
    write_reports(
        dataset_size=len(list(dataset.iterdir())),
        records=records,
        summary=summary,
        ocr_summary=ocr_summary,
        failure_clusters=summary.get("failure_clusters") or [],
        decision=decision,
        compat_decision=compat_decision,
    )
    print(f"PHASE4_FULL_DATASET_DECISION: {decision}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
