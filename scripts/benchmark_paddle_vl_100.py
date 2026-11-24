"""PaddleOCR-VL-only 100-document OCR evidence benchmark.

Uses the exact Phase 3 selection.json manifest. Resumable via per-document
checkpoints. Does not run Tesseract, classic PaddleOCR, Gemini, parser, or Phase 2.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ocr.paddle_vl_engine import (
    ENGINE_NAME,
    MODEL_NAME,
    PIPELINE_VERSION,
    PaddleVLEngine,
    evidence_from_result,
)
from services.ocr.paddle_vl_io import (
    iter_document_pages,
    load_page_image,
    load_phase3_manifest,
    page_needs_ocr,
    resolve_dataset_path,
)

DEFAULT_DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")
KNOWN_DIFFICULT_IDS = [
    "0000700259_2026_08_ZL_18_379_03092026174312.jpg",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(document_id: str) -> str:
    return (
        document_id.replace("/", "_")
        .replace("\\", "_")
        .replace(" ", "_")
    )


def _resource_snapshot() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "cpu_percent": "not_available",
        "ram_used_gb": "not_available",
        "ram_total_gb": "not_available",
        "gpu": "not_available",
        "vram_used_gb": "not_available",
        "vram_total_gb": "not_available",
    }
    try:
        import psutil

        out["cpu_percent"] = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        out["ram_used_gb"] = round(mem.used / (1024**3), 2)
        out["ram_total_gb"] = round(mem.total / (1024**3), 2)
    except Exception:
        pass
    try:
        import paddle

        if paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0:
            out["gpu"] = str(paddle.device.cuda.get_device_properties(0).name)
            try:
                # reserved memory when available
                reserved = paddle.device.cuda.memory_reserved(0)
                out["vram_used_gb"] = round(float(reserved) / (1024**3), 3)
            except Exception:
                out["vram_used_gb"] = "not_available"
            try:
                total = paddle.device.cuda.get_device_properties(0).total_memory
                out["vram_total_gb"] = round(float(total) / (1024**3), 2)
            except Exception:
                pass
    except Exception:
        pass
    return out


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _process_document(
    engine: PaddleVLEngine,
    item: Dict[str, Any],
    *,
    dataset: Path,
    max_pages: Optional[int],
    timeout_s: Optional[float],
) -> Dict[str, Any]:
    doc_id = item["document_id"]
    path = resolve_dataset_path(dataset, doc_id, explicit_path=item.get("path"))
    started = time.perf_counter()
    doc_payload: Dict[str, Any] = {
        "document_id": doc_id,
        "relative_path": item.get("relative_path") or doc_id,
        "source_path": str(path) if path else None,
        "file_type": item.get("file_type"),
        "pdf_kind": item.get("pdf_kind"),
        "manifest_ocr_required": item.get("ocr_required"),
        "engine": ENGINE_NAME,
        "model": MODEL_NAME,
        "model_version": PIPELINE_VERSION,
        "started_at": _now(),
        "pages": [],
        "status": "success",
        "errors": [],
    }
    if path is None or not path.is_file():
        doc_payload["status"] = "error"
        doc_payload["errors"].append(
            {"error_type": "file_not_found", "error_message": doc_id}
        )
        doc_payload["latency_ms"] = (time.perf_counter() - started) * 1000.0
        doc_payload["finished_at"] = _now()
        return doc_payload

    pages = iter_document_pages(path, max_pages=max_pages)
    if pages and pages[0].get("skip_reason"):
        # Spreadsheets / non-visual / unreadable files — not OCR targets.
        reason = str(pages[0].get("skip_reason") or "")
        is_error = reason.startswith("pdf_open_failed") or reason == "empty_file"
        status = "error" if is_error else "skipped_native_text"
        page_rec = {
            "page": 1,
            "status": status,
            "paddle_vl_status": status,
            "native_text": False,
            "ocr_required": False,
            "reason": reason,
            "error": pages[0].get("error"),
            "engine": ENGINE_NAME,
        }
        doc_payload["pages"].append(page_rec)
        doc_payload["status"] = status
        if is_error:
            doc_payload["errors"].append(
                {
                    "error_type": reason,
                    "error_message": pages[0].get("error") or reason,
                }
            )
        doc_payload["latency_ms"] = (time.perf_counter() - started) * 1000.0
        doc_payload["finished_at"] = _now()
        return doc_payload

    for page_info in pages:
        page = int(page_info["page"])
        page_index = int(page_info["page_index"])
        page_started = time.perf_counter()
        needs, native_meta = page_needs_ocr(path, page_index=page_index)
        if not needs:
            doc_payload["pages"].append(
                {
                    "page": page,
                    "status": "skipped_native_text",
                    "paddle_vl_status": "skipped_native_text",
                    "native_text": True,
                    "ocr_required": False,
                    "engine": ENGINE_NAME,
                    "latency_ms": (time.perf_counter() - page_started) * 1000.0,
                    **native_meta,
                }
            )
            continue

        try:
            image_bytes, image_meta = load_page_image(path, page_index=page_index)
            if not image_bytes:
                raise RuntimeError(image_meta.get("error") or "image_load_failed")
            if timeout_s is not None:
                # Soft timeout marker only; inference itself is synchronous.
                pass
            result = engine.recognize(
                image_bytes,
                page=page,
                metadata={
                    "document_id": doc_id,
                    "source_path": str(path),
                    **image_meta,
                },
            )
            evidence = evidence_from_result(result)
            page_rec = {
                **evidence,
                "page": page,
                "native_text": False,
                "ocr_required": True,
                "paddle_vl_status": evidence.get("status"),
                "image_meta": image_meta,
                "resources": _resource_snapshot(),
            }
            if evidence.get("status") != "success":
                doc_payload["errors"].append(
                    {
                        "page": page,
                        "error_type": evidence.get("error_type") or "ocr_error",
                        "error_message": evidence.get("error"),
                    }
                )
                if doc_payload["status"] == "success":
                    doc_payload["status"] = evidence.get("status") or "error"
            doc_payload["pages"].append(page_rec)
        except Exception as exc:
            err = {
                "page": page,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "traceback": traceback.format_exc(limit=5),
            }
            doc_payload["errors"].append(err)
            doc_payload["status"] = "error"
            doc_payload["pages"].append(
                {
                    "page": page,
                    "engine": ENGINE_NAME,
                    "status": "error",
                    "paddle_vl_status": "error",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "latency_ms": (time.perf_counter() - page_started) * 1000.0,
                    "text": "",
                    "numeric_tokens": [],
                    "words": [],
                    "boxes": [],
                }
            )

    doc_payload["latency_ms"] = (time.perf_counter() - started) * 1000.0
    doc_payload["finished_at"] = _now()
    doc_payload["resources"] = _resource_snapshot()
    return doc_payload


def _aggregate(docs: List[Dict[str, Any]], *, init_ms: float, cold_ms: Optional[float], warm_latencies: List[float], total_ms: float, env: Dict[str, Any], manifest_path: str) -> Dict[str, Any]:
    pages = [p for d in docs for p in (d.get("pages") or [])]
    skipped = [p for p in pages if p.get("status") == "skipped_native_text"]
    ocr_pages = [p for p in pages if p.get("status") != "skipped_native_text"]
    success = [p for p in ocr_pages if p.get("status") == "success"]
    errors = [p for p in ocr_pages if p.get("status") == "error"]
    timeouts = [p for p in ocr_pages if p.get("status") == "timeout"]
    latencies = [
        float(p["latency_ms"])
        for p in success
        if isinstance(p.get("latency_ms"), (int, float))
    ]
    text_lens = [len(p.get("text") or "") for p in success]
    word_counts = [int(p.get("word_count") or 0) for p in success]
    numeric_counts = [len(p.get("numeric_tokens") or []) for p in success]
    confs = [
        float((p.get("confidence") or {}).get("mean"))
        for p in success
        if isinstance((p.get("confidence") or {}).get("mean"), (int, float))
    ]
    valid_boxes = 0
    invalid_boxes = 0
    layout_pages = 0
    table_pages = 0
    for p in success:
        boxes = p.get("boxes") or []
        for box in boxes:
            if isinstance(box, (list, tuple)) and len(box) == 4:
                valid_boxes += 1
            else:
                invalid_boxes += 1
        if (p.get("layout") or {}).get("available"):
            layout_pages += 1
        if (p.get("tables") or {}).get("available") or (
            (p.get("tables") or {}).get("count") or 0
        ) > 0:
            table_pages += 1

    difficult = []
    for doc in docs:
        if any(k in (doc.get("document_id") or "") for k in KNOWN_DIFFICULT_IDS):
            difficult.append(
                {
                    "document_id": doc.get("document_id"),
                    "status": doc.get("status"),
                    "pages": [
                        {
                            "page": p.get("page"),
                            "status": p.get("status"),
                            "text_preview": (p.get("text") or "")[:500],
                            "numeric_tokens": (p.get("numeric_tokens") or [])[:80],
                            "latency_ms": p.get("latency_ms"),
                        }
                        for p in (doc.get("pages") or [])
                    ],
                }
            )

    def _avg(vals):
        return float(statistics.mean(vals)) if vals else None

    def _med(vals):
        return float(statistics.median(vals)) if vals else None

    return {
        "engine": ENGINE_NAME,
        "model": MODEL_NAME,
        "model_version": PIPELINE_VERSION,
        "manifest": manifest_path,
        "environment": env,
        "documents": len(docs),
        "pages": len(pages),
        "native_text_pages_skipped": len(skipped),
        "ocr_required_pages": len(ocr_pages),
        "successful_pages": len(success),
        "failed_pages": len(errors),
        "timeout_pages": len(timeouts),
        "average_latency_ms": _avg(latencies),
        "median_latency_ms": _med(latencies),
        "cold_start_latency_ms": cold_ms,
        "warm_latency_ms": _avg(warm_latencies),
        "init_latency_ms": init_ms,
        "total_benchmark_ms": total_ms,
        "average_text_length": _avg(text_lens),
        "average_word_count": _avg(word_counts),
        "numeric_token_statistics": {
            "pages_with_numeric_tokens": sum(1 for n in numeric_counts if n > 0),
            "total_numeric_tokens": sum(numeric_counts),
            "average_per_success_page": _avg(numeric_counts),
            "median_per_success_page": _med(numeric_counts),
        },
        "confidence_statistics": {
            "pages_with_confidence": len(confs),
            "mean": _avg(confs),
            "median": _med(confs),
            "note": "PaddleOCR-VL block confidence is often unavailable; do not invent.",
        },
        "bounding_box_statistics": {
            "valid": valid_boxes,
            "invalid": invalid_boxes,
            "total": valid_boxes + invalid_boxes,
        },
        "table_layout_evidence": {
            "pages_with_layout_regions": layout_pages,
            "pages_with_tables": table_pages,
            "available": layout_pages > 0 or table_pages > 0,
        },
        "known_difficult_documents": difficult,
        "document_status_counts": _count_by(docs, "status"),
        "generated_at": _now(),
    }


def _count_by(items: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for item in items:
        k = str(item.get(key) or "unknown")
        out[k] = out.get(k, 0) + 1
    return out


def _render_report(summary: Dict[str, Any], out_path: Path) -> None:
    env = summary.get("environment") or {}
    num = summary.get("numeric_token_statistics") or {}
    conf = summary.get("confidence_statistics") or {}
    box = summary.get("bounding_box_statistics") or {}
    layout = summary.get("table_layout_evidence") or {}
    lines = [
        "# PaddleOCR-VL 1.6 — 100-Document OCR Benchmark",
        "",
        "## 1. Executive summary",
        "",
        f"PaddleOCR-VL 1.6 was run as an **opt-in, isolated** OCR evidence producer on the exact Phase 3 representative 100-document set (`{summary.get('manifest')}`).",
        "Production OCR priority was not changed. Tesseract, classic PaddleOCR, Gemini, Phase 2, and parsers were not run.",
        "No OCR accuracy percentage is claimed; no manually verified OCR ground-truth set was used for full-corpus scoring.",
        "",
        f"- Documents: **{summary.get('documents')}**",
        f"- Pages seen: **{summary.get('pages')}**",
        f"- Native-text pages skipped: **{summary.get('native_text_pages_skipped')}**",
        f"- OCR-required pages: **{summary.get('ocr_required_pages')}**",
        f"- Successful OCR pages: **{summary.get('successful_pages')}**",
        f"- Failed OCR pages: **{summary.get('failed_pages')}**",
        f"- Timeouts: **{summary.get('timeout_pages')}**",
        "",
        "## 2. Environment",
        "",
        f"- OS: {env.get('os')}",
        f"- Python: {env.get('python')}",
        f"- Isolated venv: {env.get('venv')}",
        "",
        "## 3. PaddleOCR-VL version",
        "",
        f"- PaddleOCR package: {env.get('paddleocr')}",
        f"- PaddleX: {env.get('paddlex')}",
        f"- Pipeline: `{PIPELINE_VERSION}` → `{MODEL_NAME}`",
        "",
        "## 4. Model identifier",
        "",
        f"- Engine: `{ENGINE_NAME}`",
        f"- Pipeline name: `{MODEL_NAME}`",
        f"- VL recognition model: `{env.get('vl_rec_model')}`",
        f"- Layout model: `{env.get('layout_model')}`",
        "",
        "## 5. Hardware",
        "",
        f"- CPU: {env.get('cpu')}",
        f"- RAM: {env.get('ram_gb')} GiB",
        f"- GPU: {env.get('gpu')}",
        f"- VRAM: {env.get('vram_gb')}",
        f"- CUDA compiled (Paddle): {env.get('cuda_compiled')}",
        f"- Device used: {env.get('device')}",
        "",
        "## 6. Dataset",
        "",
        f"- Dataset root: `{env.get('dataset')}`",
        f"- Manifest: `{summary.get('manifest')}` (Phase 3 `selection.json`, not a new sample)",
        "",
        "## 7. Number of documents",
        "",
        f"{summary.get('documents')}",
        "",
        "## 8. Number of pages",
        "",
        f"{summary.get('pages')}",
        "",
        "## 9. Native-text pages skipped",
        "",
        f"{summary.get('native_text_pages_skipped')}",
        "",
        "## 10. OCR-required pages",
        "",
        f"{summary.get('ocr_required_pages')}",
        "",
        "## 11. Successful pages",
        "",
        f"{summary.get('successful_pages')}",
        "",
        "## 12. Failed pages",
        "",
        f"{summary.get('failed_pages')}",
        "",
        "## 13. Timeout pages",
        "",
        f"{summary.get('timeout_pages')}",
        "",
        "## 14. Average latency",
        "",
        f"{summary.get('average_latency_ms')} ms (successful OCR pages; excludes model init)",
        "",
        "## 15. Median latency",
        "",
        f"{summary.get('median_latency_ms')} ms",
        "",
        "## 16. Cold-start latency",
        "",
        f"{summary.get('cold_start_latency_ms')} ms (first OCR page after model load)",
        "",
        "## 17. Warm latency",
        "",
        f"{summary.get('warm_latency_ms')} ms (mean of subsequent successful OCR pages)",
        "",
        f"- Model init latency: {summary.get('init_latency_ms')} ms",
        f"- Total benchmark wall time: {summary.get('total_benchmark_ms')} ms",
        "",
        "## 18. Average text length",
        "",
        f"{summary.get('average_text_length')}",
        "",
        "## 19. Average word count",
        "",
        f"{summary.get('average_word_count')}",
        "",
        "## 20. Numeric token statistics",
        "",
        f"- Pages with numeric tokens: {num.get('pages_with_numeric_tokens')}",
        f"- Total numeric tokens: {num.get('total_numeric_tokens')}",
        f"- Average per success page: {num.get('average_per_success_page')}",
        f"- Median per success page: {num.get('median_per_success_page')}",
        "",
        "Tokens are reported exactly as produced (no automatic correction).",
        "",
        "## 21. Confidence statistics",
        "",
        f"- Pages with mean confidence: {conf.get('pages_with_confidence')}",
        f"- Mean: {conf.get('mean')}",
        f"- Median: {conf.get('median')}",
        f"- Note: {conf.get('note')}",
        "",
        "## 22. Bounding-box statistics",
        "",
        f"- Valid: {box.get('valid')}",
        f"- Invalid: {box.get('invalid')}",
        f"- Total: {box.get('total')}",
        "",
        "## 23. Table / layout evidence",
        "",
        f"- Pages with layout regions: {layout.get('pages_with_layout_regions')}",
        f"- Pages with tables: {layout.get('pages_with_tables')}",
        f"- Table/layout evidence available: {layout.get('available')}",
        "",
        "## 24. Known difficult-document results",
        "",
    ]
    difficult = summary.get("known_difficult_documents") or []
    if not difficult:
        lines.append("No known difficult IDs from the Phase 3 set were present or completed yet.")
    for item in difficult:
        lines.append(f"### `{item.get('document_id')}`")
        lines.append("")
        lines.append(f"- Document status: {item.get('status')}")
        for page in item.get("pages") or []:
            lines.append(
                f"- Page {page.get('page')}: status={page.get('status')}, "
                f"latency_ms={page.get('latency_ms')}"
            )
            lines.append(f"- Numeric tokens: `{page.get('numeric_tokens')}`")
            lines.append("")
            lines.append("```")
            lines.append(str(page.get("text_preview") or ""))
            lines.append("```")
            lines.append("")
    lines.extend(
        [
            "## 25. Failure analysis",
            "",
            f"Document status counts: `{summary.get('document_status_counts')}`",
            "",
            "Per-document error detail is in `phase4/paddle_vl_benchmark/per_document/`.",
            "",
            "## 26. Resource usage",
            "",
            f"- Snapshot at summary time: `{env.get('resource_snapshot')}`",
            "",
            "## 27. Limitations",
            "",
            "- Isolated `.venvs/paddleocr_vl` required; production Python has classic Paddle/torch DLL issues.",
            "- CUDNN host/runtime mismatch warning observed (Paddle built with 9.9, host 9.5).",
            "- VL page latency is high on RTX 3050 6GB; unsuitable as a silent production swap.",
            "- Block-level confidence is frequently unavailable from the VL pipeline.",
            "- No full OCR character/word ground truth for the 100-doc set → no fake accuracy %.",
            "- Digital PDFs and non-visual office files are skipped via native-text / non-visual rules.",
            "",
            "## 28. Recommendation",
            "",
            "Keep PaddleOCR-VL 1.6 **opt-in / benchmark-only**. Do not change production OCR priority.",
            "Use this report as one engine's evidence profile for a later controlled multi-engine bakeoff on the same Phase 3 manifest.",
            "",
        ]
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def _collect_env(dataset: Path, device: str) -> Dict[str, Any]:
    import platform

    env: Dict[str, Any] = {
        "os": f"{platform.system()} {platform.release()}",
        "python": sys.version.split()[0],
        "venv": sys.prefix,
        "dataset": str(dataset),
        "device": device,
        "paddle": "not_available",
        "paddleocr": "not_available",
        "paddlex": "not_available",
        "cuda_compiled": "not_available",
        "gpu": "not_available",
        "vram_gb": "not_available",
        "cpu": "not_available",
        "ram_gb": "not_available",
        "vl_rec_model": "PaddleOCR-VL-1.6-0.9B",
        "layout_model": "PP-DocLayoutV3",
        "resource_snapshot": _resource_snapshot(),
    }
    try:
        import paddle

        env["paddle"] = paddle.__version__
        env["cuda_compiled"] = paddle.is_compiled_with_cuda()
        if paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0:
            props = paddle.device.cuda.get_device_properties(0)
            env["gpu"] = getattr(props, "name", str(props))
            total = getattr(props, "total_memory", None)
            if total:
                env["vram_gb"] = round(float(total) / (1024**3), 2)
    except Exception as exc:
        env["paddle"] = f"error: {exc}"
    try:
        import paddleocr

        env["paddleocr"] = getattr(paddleocr, "__version__", "?")
    except Exception as exc:
        env["paddleocr"] = f"error: {exc}"
    try:
        import paddlex

        env["paddlex"] = getattr(paddlex, "__version__", "?")
    except Exception as exc:
        env["paddlex"] = f"error: {exc}"
    try:
        import psutil

        env["ram_gb"] = round(psutil.virtual_memory().total / (1024**3), 2)
    except Exception:
        pass
    try:
        import platform as pl

        env["cpu"] = pl.processor() or "not_available"
    except Exception:
        pass
    return env


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PaddleOCR-VL 100-doc benchmark")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--phase3", type=Path, default=ROOT / "phase3_eval")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "phase4" / "paddle_vl_benchmark",
    )
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None, help="Debug: first N docs")
    parser.add_argument("--device", type=str, default=None)
    parser.add_argument("--force", action="store_true", help="Re-run completed docs")
    args = parser.parse_args(argv)

    output_dir = args.output_dir
    per_dir = output_dir / "per_document"
    per_dir.mkdir(parents=True, exist_ok=True)
    selection_path = args.phase3 / "selection.json"
    manifest = load_phase3_manifest(args.phase3)
    if args.limit is not None:
        manifest = manifest[: args.limit]

    _write_json(
        output_dir / "manifest.json",
        {
            "source": str(selection_path),
            "count": len(manifest),
            "document_ids": [m["document_id"] for m in manifest],
            "engine": ENGINE_NAME,
            "model": MODEL_NAME,
            "pipeline_version": PIPELINE_VERSION,
        },
    )

    engine = PaddleVLEngine(device=args.device)
    if not engine.available():
        print("PADDLEOCR-VL unavailable — BLOCKED", engine.model_info())
        return 2

    wall0 = time.perf_counter()
    init0 = time.perf_counter()
    # Prefer subprocess warm-up. Host-site paddleocr can hard-crash Windows;
    # never force in-process ``_client()`` when the isolated venv is selected.
    info = engine.model_info()
    if info.get("backend") == "subprocess":
        init_ms = (time.perf_counter() - init0) * 1000.0
    else:
        engine._client()
        init_ms = (time.perf_counter() - init0) * 1000.0
    env = _collect_env(
        args.dataset,
        engine._resolved_device or args.device or info.get("device") or "auto",
    )
    env["init_latency_ms"] = init_ms
    env["backend"] = info.get("backend")

    cold_ms: Optional[float] = None
    warm_latencies: List[float] = []
    completed_docs: List[Dict[str, Any]] = []

    print("=" * 40)
    print("PADDLEOCR-VL 100-DOCUMENT BENCHMARK")
    print("=" * 40)
    print(f"Manifest: {selection_path} ({len(manifest)} docs)")
    print(f"Init ms: {init_ms:.1f}")

    for index, item in enumerate(manifest, start=1):
        doc_id = item["document_id"]
        out_path = per_dir / f"{_safe_name(doc_id)}.json"
        if out_path.is_file() and not args.force:
            existing = _load_json(out_path)
            if existing and existing.get("pages") is not None:
                completed_docs.append(existing)
                for page in existing.get("pages") or []:
                    if page.get("status") == "success" and isinstance(
                        page.get("latency_ms"), (int, float)
                    ):
                        if cold_ms is None:
                            cold_ms = float(page["latency_ms"])
                        else:
                            warm_latencies.append(float(page["latency_ms"]))
                print(f"[{index}/{len(manifest)}] resume-skip {doc_id}")
                continue

        print(f"[{index}/{len(manifest)}] run {doc_id}", flush=True)
        try:
            doc_payload = _process_document(
                engine,
                item,
                dataset=args.dataset,
                max_pages=args.max_pages,
                timeout_s=None,
            )
        except Exception as exc:
            # One document must never abort the 100-doc run.
            print(
                f"[{index}/{len(manifest)}] FATAL-DOC {doc_id}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            doc_payload = {
                "document_id": doc_id,
                "relative_path": item.get("relative_path") or doc_id,
                "source_path": item.get("path"),
                "engine": ENGINE_NAME,
                "model": MODEL_NAME,
                "model_version": PIPELINE_VERSION,
                "status": "error",
                "pages": [
                    {
                        "page": 1,
                        "engine": ENGINE_NAME,
                        "status": "error",
                        "paddle_vl_status": "error",
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "text": "",
                        "numeric_tokens": [],
                        "words": [],
                        "boxes": [],
                    }
                ],
                "errors": [
                    {
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "traceback": traceback.format_exc(limit=8),
                    }
                ],
                "started_at": _now(),
                "finished_at": _now(),
            }
        _write_json(out_path, doc_payload)
        completed_docs.append(doc_payload)
        for page in doc_payload.get("pages") or []:
            if page.get("status") == "success" and isinstance(
                page.get("latency_ms"), (int, float)
            ):
                if cold_ms is None:
                    cold_ms = float(page["latency_ms"])
                else:
                    warm_latencies.append(float(page["latency_ms"]))

        # Rolling summary for crash safety.
        partial = _aggregate(
            completed_docs,
            init_ms=init_ms,
            cold_ms=cold_ms,
            warm_latencies=warm_latencies,
            total_ms=(time.perf_counter() - wall0) * 1000.0,
            env=env,
            manifest_path=str(selection_path),
        )
        _write_json(output_dir / "summary.json", partial)
        _write_json(ROOT / "phase4" / "paddle_vl_100.json", partial)

    total_ms = (time.perf_counter() - wall0) * 1000.0
    summary = _aggregate(
        completed_docs,
        init_ms=init_ms,
        cold_ms=cold_ms,
        warm_latencies=warm_latencies,
        total_ms=total_ms,
        env=env,
        manifest_path=str(selection_path),
    )
    _write_json(output_dir / "summary.json", summary)
    _write_json(ROOT / "phase4" / "paddle_vl_100.json", summary)
    report_path = ROOT / "phase4" / "PADDLE_VL_100_REPORT.md"
    _render_report(summary, report_path)

    print()
    print("=" * 40)
    print("PADDLEOCR-VL 100-DOCUMENT BENCHMARK")
    print("=" * 40)
    print(f"Documents: {summary['documents']}")
    print(f"Pages: {summary['pages']}")
    print(f"Native-text pages skipped: {summary['native_text_pages_skipped']}")
    print(f"OCR-required pages: {summary['ocr_required_pages']}")
    print(f"Successful: {summary['successful_pages']}")
    print(f"Errors: {summary['failed_pages']}")
    print(f"Timeouts: {summary['timeout_pages']}")
    print(f"Average latency: {summary['average_latency_ms']}")
    print(f"Median latency: {summary['median_latency_ms']}")
    print(f"Cold-start latency: {summary['cold_start_latency_ms']}")
    print(f"Warm latency: {summary['warm_latency_ms']}")
    print(f"Average text length: {summary['average_text_length']}")
    print(f"Average word count: {summary['average_word_count']}")
    print(
        "Numeric token count:",
        (summary.get("numeric_token_statistics") or {}).get("total_numeric_tokens"),
    )
    box = summary.get("bounding_box_statistics") or {}
    print("Bounding boxes:")
    print(f"Valid: {box.get('valid')}")
    print(f"Invalid: {box.get('invalid')}")
    print(
        "Table/layout evidence available:",
        (summary.get("table_layout_evidence") or {}).get("available"),
    )
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
