"""Day 4 performance and reliability audit.

Controlled workloads on this host. Does not claim production JSONB integration.
Set DAY4_RUN_OCR=1 to include one cold/warm VL image timing (expensive).
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "audits" / "day4_performance"
ZA = Path(r"C:\Users\amnsa\Downloads\ZA_2026_August")
ZL = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")


@dataclass
class DocResult:
    path: str
    name: str
    ext: str
    bytes: int
    seconds: float
    ok: bool
    rows: int = 0
    method: str = ""
    verified: Optional[bool] = None
    decision: str = ""
    schema_class: str = ""
    error: str = ""
    peak_rss_mb: float = 0.0


@dataclass
class BatchResult:
    label: str
    mode: str
    workers: int
    docs: List[DocResult] = field(default_factory=list)
    wall_seconds: float = 0.0
    throughput_docs_per_min: float = 0.0
    peak_rss_mb: float = 0.0
    peak_vram_mb: Optional[float] = None
    cpu_percent_sample: Optional[float] = None


def _unset_perf_env() -> None:
    for key in (
        "OCR_EXECUTION_TIMEOUT_S",
        "OCR_ENGINE",
        "OCR_SECONDARY_REPAIR",
        "PHASE2_RECOVERY_ENABLED",
        "RUN_LIVE_EXTRACTION",
    ):
        os.environ.pop(key, None)
    # Keep recovery off so digital timings isolate parsers, not Gemini.
    os.environ["PHASE2_RECOVERY_ENABLED"] = "false"
    os.environ["OCR_SECONDARY_REPAIR"] = "false"


def _rss_mb() -> float:
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        return 0.0


def _cpu_percent() -> Optional[float]:
    try:
        import psutil

        return float(psutil.cpu_percent(interval=0.2))
    except Exception:
        return None


def _vram_mb() -> Optional[float]:
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=5,
        )
        return float(out.strip().splitlines()[0])
    except Exception:
        return None


def _hardware() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "cwd": str(ROOT),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rss_mb_start": round(_rss_mb(), 1),
        "vram_mb_start": _vram_mb(),
        "cpu_count": os.cpu_count(),
    }
    try:
        import psutil

        info["ram_gb"] = round(psutil.virtual_memory().total / (1024**3), 2)
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader",
            ],
            text=True,
            timeout=5,
        ).strip()
        info["gpu"] = out
    except Exception:
        info["gpu"] = None
    return info


def _pick_docs() -> List[Path]:
    candidates = [
        ZA / "0000700096_2026_08_ZA_06_9236_03092026042726.xlsx",
        ZA / "0000700273_2026_08_ZA_18_538_07092026170433.xlsx",
        ZA / "0000700025_2026_08_ZA_06_337_08092026060608.xls",
        ZA / "0000700697_2026_08_ZA_25_379_07092026151037.txt",
        ZA / "0000700827_2026_08_ZA_25_294_07092026065213.txt",
        ZA / "0000700481_2026_08_ZA_10_355_07092026120455.htm",
        ZA / "0000702138_2026_08_ZA_24_291_07092026035226.html",
        ZA / "0000713511_2026_08_ZA_24_255_07092026171904.docx",
        ZA / "0000700015_2026_08_ZA_06_337_05092026070554.pdf",
        ZA / "0000700017_2026_08_ZA_06_337_05092026071148.pdf",
    ]
    zl_xlsx = sorted(ZL.glob("*.xlsx"))[:1] if ZL.is_dir() else []
    candidates.extend(zl_xlsx)
    return [path for path in candidates if path.is_file()]


def _extract_one(path: Path) -> DocResult:
    from services.sales_statement_extractor import extract_sales_statement

    data = path.read_bytes()
    started = time.perf_counter()
    peak = _rss_mb()
    try:
        result = extract_sales_statement(data, path.name)
        seconds = time.perf_counter() - started
        peak = max(peak, _rss_mb())
        totals = result.get("totals") if isinstance(result, dict) else {}
        extra = totals.get("extra") if isinstance(totals, dict) else {}
        decision = result.get("decision") if isinstance(result, dict) else {}
        if not isinstance(decision, dict):
            decision = {}
        di = result.get("document_intelligence") if isinstance(result, dict) else {}
        if not isinstance(di, dict):
            di = {}
        final = di.get("final") if isinstance(di.get("final"), dict) else decision
        return DocResult(
            path=str(path),
            name=path.name,
            ext=path.suffix.lower(),
            bytes=len(data),
            seconds=round(seconds, 3),
            ok=True,
            rows=len(result.get("line_items") or []) if isinstance(result, dict) else 0,
            method=str((extra or {}).get("extraction_method") or ""),
            verified=final.get("verified"),
            decision=str(final.get("status") or final.get("decision") or ""),
            schema_class=str((extra or {}).get("schema_class") or ""),
            peak_rss_mb=round(peak, 1),
        )
    except Exception as exc:
        return DocResult(
            path=str(path),
            name=path.name,
            ext=path.suffix.lower(),
            bytes=len(data),
            seconds=round(time.perf_counter() - started, 3),
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            peak_rss_mb=round(max(peak, _rss_mb()), 1),
        )


def run_sequential(paths: List[Path], label: str) -> BatchResult:
    docs: List[DocResult] = []
    wall0 = time.perf_counter()
    peak_rss = _rss_mb()
    peak_vram = _vram_mb()
    for path in paths:
        doc = _extract_one(path)
        docs.append(doc)
        peak_rss = max(peak_rss, doc.peak_rss_mb, _rss_mb())
        vram = _vram_mb()
        if vram is not None:
            peak_vram = max(peak_vram or 0.0, vram)
    wall = time.perf_counter() - wall0
    return BatchResult(
        label=label,
        mode="sequential",
        workers=1,
        docs=docs,
        wall_seconds=round(wall, 3),
        throughput_docs_per_min=round((len(docs) / wall) * 60.0, 2) if wall else 0.0,
        peak_rss_mb=round(peak_rss, 1),
        peak_vram_mb=peak_vram,
        cpu_percent_sample=_cpu_percent(),
    )


def run_concurrent(paths: List[Path], workers: int, label: str) -> BatchResult:
    docs: List[DocResult] = []
    lock = threading.Lock()
    peak_rss = _rss_mb()
    peak_vram = _vram_mb()
    wall0 = time.perf_counter()

    def _job(path: Path) -> DocResult:
        nonlocal peak_rss, peak_vram
        doc = _extract_one(path)
        with lock:
            peak_rss = max(peak_rss, doc.peak_rss_mb, _rss_mb())
            vram = _vram_mb()
            if vram is not None:
                peak_vram = max(peak_vram or 0.0, vram)
        return doc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_job, path) for path in paths]
        for future in as_completed(futures):
            docs.append(future.result())
    wall = time.perf_counter() - wall0
    docs.sort(key=lambda item: item.name)
    return BatchResult(
        label=label,
        mode="concurrent",
        workers=workers,
        docs=docs,
        wall_seconds=round(wall, 3),
        throughput_docs_per_min=round((len(docs) / wall) * 60.0, 2) if wall else 0.0,
        peak_rss_mb=round(peak_rss, 1),
        peak_vram_mb=peak_vram,
        cpu_percent_sample=_cpu_percent(),
    )


def run_schema_batch() -> Dict[str, Any]:
    from services.hybrid_schema import accept_document, result_from_document
    from app import build_split_extract_response_from_sales_statement

    payloads = {
        "predefined": {"product_name": "SPEMAN", "packing": "60s", "sales_qty": 0, "closing_qty": 4},
        "hybrid": {
            "product_name": "SPEMAN",
            "closing_qty": 4,
            "note": "हिंदी",
            "flags": [None, False],
        },
        "fully_dynamic": {"rows": [{"sku": "α"}], "empty": [], "note": None},
    }
    started = time.perf_counter()
    out = {}
    for name, payload in payloads.items():
        envelope = accept_document(payload)
        sales = result_from_document(payload)
        response = build_split_extract_response_from_sales_statement(
            sales_result=sales,
            source_filename=f"{name}.json",
            use_blob_storage=False,
        )
        encoded = json.dumps(response, ensure_ascii=False)
        out[name] = {
            "schema_class": envelope["schema_class"],
            "verified": envelope["verified"],
            "response_bytes": len(encoded.encode("utf-8")),
            "has_invoices": bool(response.get("invoices")),
            "dynamic_preserved": (
                response.get("dynamic") == payload
                if name == "fully_dynamic"
                else "note" in json.dumps(response, ensure_ascii=False)
                if name == "hybrid"
                else True
            ),
        }
    return {
        "seconds": round(time.perf_counter() - started, 4),
        "cases": out,
    }


def run_cache_benchmark() -> Dict[str, Any]:
    """Measure primary-image OCR cache: 9 distinct keys then re-hit key 0."""
    from services import sales_statement_extractor as sse

    calls: List[str] = []

    def fake_ocr(image, use_fallback=False):
        calls.append(image.tobytes()[:8].hex() if hasattr(image, "tobytes") else "x")
        time.sleep(0.01)
        return "HIORA 5 50 1 54", {"engine": "paddleocr-vl", "status": "ok", "unverified_fallback": False}

    sse._PRIMARY_IMAGE_OCR_CACHE.clear()
    from PIL import Image

    images = []
    for index in range(9):
        img = Image.new("RGB", (8, 8), color=(index, 0, 0))
        buf = __import__("io").BytesIO()
        img.save(buf, format="PNG")
        images.append(buf.getvalue())

    import unittest.mock as mock

    with mock.patch("services.ocr.runtime.primary_is_paddle_vl", return_value=True), mock.patch(
        "services.ocr.runtime.ocr_pil_image_text", side_effect=fake_ocr
    ):
        cold = []
        for data in images:
            t0 = time.perf_counter()
            sse._ocr_image_to_text(data)
            cold.append(time.perf_counter() - t0)
        # After 9 inserts with max 8, key0 was evicted under LRU. Touch key1..8 then key0.
        warm_hits = []
        for data in images[1:]:
            t0 = time.perf_counter()
            sse._ocr_image_to_text(data)
            warm_hits.append(time.perf_counter() - t0)
        # Re-read image 1 (still in LRU after hits) — must not call OCR again.
        before = len(calls)
        t0 = time.perf_counter()
        sse._ocr_image_to_text(images[1])
        hit_seconds = time.perf_counter() - t0
        after = len(calls)
    return {
        "cold_calls": len(images),
        "ocr_invocations": len(calls),
        "median_cold_s": round(statistics.median(cold), 4),
        "median_warm_hit_s": round(statistics.median(warm_hits), 4),
        "repeat_hit_s": round(hit_seconds, 4),
        "repeat_hit_reused": after == before,
        "cache_size": len(sse._PRIMARY_IMAGE_OCR_CACHE),
        "cache_max": sse._PRIMARY_IMAGE_OCR_CACHE_MAX,
    }


def run_ocr_image_optional() -> Optional[Dict[str, Any]]:
    if os.getenv("DAY4_RUN_OCR", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return {
            "skipped": True,
            "reason": "Set DAY4_RUN_OCR=1 to time one cold/warm VL image (often >5 min).",
        }
    path = ROOT / "test_images" / "0000701478_2026_08_ZA_01_224_08092026054726.jpg"
    if not path.is_file():
        path = ZA / "0000701478_2026_08_ZA_01_224_08092026054726.jpg"
    if not path.is_file():
        return {"skipped": True, "reason": "JPG fixture missing"}
    from services import sales_statement_extractor as sse

    sse._PRIMARY_IMAGE_OCR_CACHE.clear()
    data = path.read_bytes()
    v0 = _vram_mb()
    t0 = time.perf_counter()
    text1 = sse._ocr_image_to_text(data)
    cold = time.perf_counter() - t0
    v1 = _vram_mb()
    t1 = time.perf_counter()
    text2 = sse._ocr_image_to_text(data)
    warm = time.perf_counter() - t1
    v2 = _vram_mb()
    return {
        "skipped": False,
        "file": path.name,
        "cold_s": round(cold, 3),
        "warm_cache_s": round(warm, 3),
        "text_len": len(text1 or ""),
        "texts_equal": text1 == text2,
        "vram_mb": {"before": v0, "after_cold": v1, "after_warm": v2},
        "rss_mb": round(_rss_mb(), 1),
    }


def run_partial_batch_failure(paths: List[Path]) -> Dict[str, Any]:
    """One bad payload must not stop siblings."""
    from services.sales_statement_extractor import extract_sales_statement

    results = []
    wall0 = time.perf_counter()

    def work(item: Tuple[str, bytes, str]) -> Dict[str, Any]:
        name, data, kind = item
        try:
            if kind == "bad":
                extract_sales_statement(b"not-a-real-document", name)
                return {"name": name, "ok": True, "unexpected": True}
            result = extract_sales_statement(data, name)
            return {
                "name": name,
                "ok": True,
                "rows": len(result.get("line_items") or []),
            }
        except Exception as exc:
            return {"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    jobs: List[Tuple[str, bytes, str]] = []
    for path in paths[:3]:
        jobs.append((path.name, path.read_bytes(), "good"))
    jobs.append(("corrupt.bin", b"\x00\x01\xffnot-a-file", "bad"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, jobs))
    return {
        "wall_s": round(time.perf_counter() - wall0, 3),
        "results": results,
        "good_ok": sum(1 for item in results if item.get("ok") and item["name"] != "corrupt.bin"),
        "bad_failed": any(
            (not item.get("ok")) and item["name"] == "corrupt.bin" for item in results
        ),
    }


def main() -> int:
    _unset_perf_env()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    paths = _pick_docs()
    report: Dict[str, Any] = {
        "hardware": _hardware(),
        "env": {
            "PHASE2_RECOVERY_ENABLED": os.environ.get("PHASE2_RECOVERY_ENABLED"),
            "OCR_SECONDARY_REPAIR": os.environ.get("OCR_SECONDARY_REPAIR"),
            "MAX_CONCURRENT_EXTRACTIONS": os.environ.get("MAX_CONCURRENT_EXTRACTIONS", "2"),
            "MAX_CONCURRENT_REQUESTS": os.environ.get("MAX_CONCURRENT_REQUESTS", "1"),
            "DAY4_RUN_OCR": os.environ.get("DAY4_RUN_OCR", ""),
        },
        "documents": [{"name": p.name, "bytes": p.stat().st_size, "ext": p.suffix} for p in paths],
    }
    if not paths:
        report["error"] = "No fixtures found under Downloads ZA/ZL"
        (OUT_DIR / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 1

    print(f"Day4 audit: {len(paths)} digital docs", flush=True)
    # Cold sequential (first process use of parsers/libs).
    cold = run_sequential(paths, "digital_cold_sequential")
    report["digital_cold_sequential"] = asdict(cold)
    print(
        f"cold sequential wall={cold.wall_seconds}s throughput={cold.throughput_docs_per_min}/min",
        flush=True,
    )
    # Warm sequential (modules imported, caches warm for digital).
    warm = run_sequential(paths, "digital_warm_sequential")
    report["digital_warm_sequential"] = asdict(warm)
    print(
        f"warm sequential wall={warm.wall_seconds}s throughput={warm.throughput_docs_per_min}/min",
        flush=True,
    )

    for workers in (1, 2, 4):
        batch = run_concurrent(paths, workers, f"digital_concurrent_w{workers}")
        report[f"digital_concurrent_w{workers}"] = asdict(batch)
        ok = sum(1 for doc in batch.docs if doc.ok)
        print(
            f"concurrent workers={workers} wall={batch.wall_seconds}s "
            f"ok={ok}/{len(batch.docs)} throughput={batch.throughput_docs_per_min}/min "
            f"peak_rss={batch.peak_rss_mb}MB",
            flush=True,
        )

    report["schema_batch"] = run_schema_batch()
    report["image_ocr_cache"] = run_cache_benchmark()
    report["partial_batch_failure"] = run_partial_batch_failure(paths)
    report["ocr_image"] = run_ocr_image_optional()
    report["hardware_end"] = {
        "rss_mb": round(_rss_mb(), 1),
        "vram_mb": _vram_mb(),
        "cpu_percent": _cpu_percent(),
    }

    out_path = OUT_DIR / "report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
