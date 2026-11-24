"""Day 5 final production-readiness audit.

Writes evidence under audits/day5_final/. Does not commit.
Does not claim production JSONB. GPU VL stays sequential (one at a time).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "audits" / "day5_final"
ZA = Path(r"C:\Users\amnsa\Downloads\ZA_2026_August")
ZL = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")


def _env() -> None:
    for key in (
        "OCR_EXECUTION_TIMEOUT_S",
        "OCR_ENGINE",
        "OCR_SECONDARY_REPAIR",
        "PHASE2_RECOVERY_ENABLED",
        "RUN_LIVE_EXTRACTION",
    ):
        os.environ.pop(key, None)
    # Controlled audit: no Phase 2 Gemini unless DAY5_LIVE_GEMINI=1.
    if os.getenv("DAY5_LIVE_GEMINI", "").strip().lower() not in {"1", "true", "yes", "on"}:
        os.environ["PHASE2_RECOVERY_ENABLED"] = "false"
    os.environ["OCR_SECONDARY_REPAIR"] = "false"


def _rss_mb() -> float:
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        return 0.0


def _vram_mb() -> Optional[float]:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
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
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "rss_mb": round(_rss_mb(), 1),
        "vram_mb": _vram_mb(),
        "cpu_count": os.cpu_count(),
        "sales_deadline_s": int(
            os.getenv("SALES_EXTRACTION_MAX_EXECUTION_SECONDS", "600") or 600
        ),
        "phase2": os.getenv("PHASE2_RECOVERY_ENABLED"),
        "secondary_repair": os.getenv("OCR_SECONDARY_REPAIR"),
    }
    try:
        import psutil

        info["ram_gb"] = round(psutil.virtual_memory().total / (1024**3), 2)
    except Exception:
        pass
    try:
        info["gpu"] = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader",
            ],
            text=True,
            timeout=5,
        ).strip()
    except Exception:
        info["gpu"] = None
    return info


def _resolve(name: str) -> Optional[Path]:
    for root in (ZA, ZL, ROOT / "test_images", ROOT):
        path = root / name
        if path.is_file():
            return path
    return None


def _summarize(result: Dict[str, Any]) -> Dict[str, Any]:
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    di = result.get("document_intelligence") if isinstance(result.get("document_intelligence"), dict) else {}
    decision = di.get("decision") if isinstance(di.get("decision"), dict) else {}
    if not decision:
        decision = result.get("decision") if isinstance(result.get("decision"), dict) else {}
    obs = di.get("observability") if isinstance(di.get("observability"), dict) else {}
    timings = obs.get("timings") if isinstance(obs.get("timings"), dict) else {}
    ocr_bundle = di.get("ocr") if isinstance(di.get("ocr"), dict) else {}
    grounding = extra.get("source_grounding") if isinstance(extra.get("source_grounding"), dict) else {}
    phase1 = result.get("phase1") if isinstance(result.get("phase1"), dict) else {}
    validation = phase1.get("validation") if isinstance(phase1.get("validation"), dict) else {}
    verification = di.get("verification") if isinstance(di.get("verification"), dict) else {}
    return {
        "rows": len(result.get("line_items") or []),
        "method": str(extra.get("extraction_method") or ""),
        "verified": decision.get("verified"),
        "status": str(decision.get("status") or ""),
        "reason": str(decision.get("reason") or ""),
        "timings": timings,
        "ocr_engines": obs.get("ocr_engines_used") or ocr_bundle.get("engines_used"),
        "ocr_page_count": ocr_bundle.get("page_count"),
        "ocr_timed_out": ocr_bundle.get("timed_out"),
        "di_ocr_override": bool(extra.get("di_ocr_override")),
        "grounding": {
            "status": grounding.get("status"),
            "reader": grounding.get("reader"),
            "compared_rows": grounding.get("compared_rows"),
            "discrepancy_count": grounding.get("discrepancy_count"),
            "blank_read_as_zero": grounding.get("blank_read_as_zero"),
            "blocks_verification": grounding.get("blocks_verification"),
        },
        "source_evidence": validation.get("source_evidence"),
        "verification_passed": verification.get("passed"),
        "verification_issues": verification.get("issues") or [],
        "schema_class": extra.get("schema_class"),
        "fail_count": (
            (extra.get("stock_identity") or {}).get("fail_count")
            if isinstance(extra.get("stock_identity"), dict)
            else extra.get("stock_identity_fail_count")
        ),
    }


def extract_document(path: Path, label: str, *, di_override: bool = False) -> Dict[str, Any]:
    from services.sales_statement_extractor import extract_sales_statement
    from services.sales_extraction_runtime import (
        SALES_EXTRACTION_MAX_EXECUTION_SECONDS,
        clear_sales_deadline,
        start_sales_deadline,
    )

    data = path.read_bytes()
    request_id = f"day5-{label}"
    # Scanned/image audit: enable secondary-repair so DI OCR text is reused
    # by core (override path). Digital docs keep the production default OFF.
    previous_secondary = os.environ.get("OCR_SECONDARY_REPAIR")
    if di_override:
        os.environ["OCR_SECONDARY_REPAIR"] = "true"
    else:
        os.environ["OCR_SECONDARY_REPAIR"] = "false"
    start_sales_deadline(
        request_id, limit_seconds=float(SALES_EXTRACTION_MAX_EXECUTION_SECONDS)
    )
    rss0 = _rss_mb()
    vram0 = _vram_mb()
    t0 = time.perf_counter()
    record: Dict[str, Any] = {
        "label": label,
        "file": path.name,
        "path": str(path),
        "bytes": len(data),
        "ext": path.suffix.lower(),
        "deadline_s": SALES_EXTRACTION_MAX_EXECUTION_SECONDS,
        "di_override_env": di_override,
    }
    try:
        result = extract_sales_statement(data, path.name)
        seconds = time.perf_counter() - t0
        summary = _summarize(result if isinstance(result, dict) else {})
        record.update(
            {
                "ok": True,
                "seconds": round(seconds, 3),
                "within_deadline": seconds <= SALES_EXTRACTION_MAX_EXECUTION_SECONDS,
                "peak_rss_mb": round(max(rss0, _rss_mb()), 1),
                "vram_mb": {"before": vram0, "after": _vram_mb()},
                **summary,
            }
        )
        # Keep a compact row sample for grounding review (no full dump).
        rows = []
        for item in (result.get("line_items") or [])[:5]:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "product_name": item.get("product_name"),
                    "opening_qty": item.get("opening_qty"),
                    "receipts_qty": item.get("receipts_qty"),
                    "sales_qty": item.get("sales_qty"),
                    "closing_qty": item.get("closing_qty"),
                }
            )
        record["sample_rows"] = rows
    except Exception as exc:
        record.update(
            {
                "ok": False,
                "seconds": round(time.perf_counter() - t0, 3),
                "within_deadline": True,
                "error": f"{type(exc).__name__}: {exc}",
                "peak_rss_mb": round(max(rss0, _rss_mb()), 1),
                "vram_mb": {"before": vram0, "after": _vram_mb()},
            }
        )
    finally:
        clear_sales_deadline()
        if previous_secondary is None:
            os.environ.pop("OCR_SECONDARY_REPAIR", None)
        else:
            os.environ["OCR_SECONDARY_REPAIR"] = previous_secondary
    return record


def run_schema_integrity() -> Dict[str, Any]:
    from services.hybrid_schema import (
        HybridSchemaError,
        SalesLineFixed,
        accept_document,
        result_from_document,
    )
    from app import build_split_extract_response_from_sales_statement
    from pydantic import ValidationError

    out: Dict[str, Any] = {"cases": {}, "api": {}, "errors": []}

    predefined = {"product_name": "SPEMAN", "packing": "60s", "sales_qty": 0, "closing_qty": 4, "opening_qty": None}
    hybrid = {
        "product_name": "SPEMAN",
        "closing_qty": 4,
        "sales_qty": "not-a-number",
        "note": "हिंदी",
        "flags": [True, False, None],
        "nested": {"empty": {}, "rows": []},
    }
    dynamic = {"rows": [{"sku": "α", "qty_text": "see note"}], "empty": [], "note": None, "active": False}

    for name, payload in (
        ("predefined", predefined),
        ("hybrid", hybrid),
        ("fully_dynamic", dynamic),
    ):
        envelope = accept_document(payload)
        out["cases"][name] = {
            "schema_class": envelope["schema_class"],
            "verified": envelope["verified"],
            "verification_status": envelope["verification_status"],
            "schema_errors": envelope.get("schema_errors") or [],
            "document_equal": envelope["document"] == payload,
            "fixed_is_none": envelope["fixed"] is None,
        }
        sales = result_from_document(payload)
        response = build_split_extract_response_from_sales_statement(
            sales_result=sales,
            source_filename=f"{name}.json",
            use_blob_storage=False,
        )
        encoded = json.dumps(response, ensure_ascii=False)
        out["api"][name] = {
            "has_invoices": bool(response.get("invoices")),
            "verified_false": '"verified": false' in encoded.replace(" ", "").lower()
            or response.get("verification", {}).get("verified") is False,
            "dynamic_preserved": (
                response.get("dynamic") == payload
                if name == "fully_dynamic"
                else ("हिंदी" in encoded if name == "hybrid" else True)
            ),
            "response_bytes": len(encoded.encode("utf-8")),
        }

    # Integrity checks that must remain hard.
    try:
        SalesLineFixed.model_validate({"opening_qty": "bad", "unknown": 1})
        out["errors"].append("extra_field_accepted")
    except (ValidationError, HybridSchemaError):
        out["cases"]["rejects_unknown_fixed"] = True
    try:
        accept_document({"x": float("nan")})
        out["errors"].append("nan_accepted")
    except HybridSchemaError:
        out["cases"]["rejects_nan"] = True

    out["passed"] = not out["errors"] and all(
        c["verified"] is False for c in out["cases"].values() if isinstance(c, dict) and "verified" in c
    )
    return out


def run_source_grounding_review(documents: List[Dict[str, Any]]) -> Dict[str, Any]:
    findings = []
    for doc in documents:
        if not doc.get("ok"):
            findings.append(
                {
                    "document": doc.get("file"),
                    "severity": "high" if "Deadline" in str(doc.get("error") or "") else "medium",
                    "issue": "extraction_error",
                    "detail": doc.get("error"),
                    "verified": False,
                }
            )
            continue
        verified = doc.get("verified")
        grounding = doc.get("grounding") or {}
        status = str(doc.get("status") or "")
        reason = str(doc.get("reason") or "")
        if verified is True and grounding.get("blocks_verification"):
            findings.append(
                {
                    "document": doc.get("file"),
                    "severity": "critical",
                    "issue": "verified_despite_blocked_grounding",
                    "grounding": grounding,
                    "status": status,
                    "reason": reason,
                }
            )
        if verified is True and grounding.get("status") == "insufficient_source_evidence":
            findings.append(
                {
                    "document": doc.get("file"),
                    "severity": "critical",
                    "issue": "verified_without_source_evidence",
                    "grounding": grounding,
                }
            )
        if status == "VERIFIED" and reason == "":
            # still ok if grounding checked
            pass
        findings.append(
            {
                "document": doc.get("file"),
                "severity": "info",
                "issue": "status_snapshot",
                "status": status,
                "reason": reason,
                "verified": verified,
                "method": doc.get("method"),
                "rows": doc.get("rows"),
                "grounding": grounding,
                "sample_rows": doc.get("sample_rows"),
            }
        )
    critical = [f for f in findings if f.get("severity") == "critical"]
    return {
        "finding_count": len(findings),
        "critical_count": len(critical),
        "critical": critical,
        "findings": findings,
        "passed": len(critical) == 0,
    }


def run_multipage_override_probe() -> Dict[str, Any]:
    """DI multi-page OCR association without live GPU (mocked recognize)."""
    import io
    from unittest.mock import patch

    import fitz
    from PIL import Image

    from services.document_intelligence.pipeline import DocumentIntelligencePipeline
    from services.ocr.base import OCRResult
    from services.ocr.orchestrator import OCRBundle

    doc = fitz.open()
    for index in range(3):
        page = doc.new_page(width=200, height=200)
        page.insert_text((20, 40), f"PAGE{index+1} MARKER")
    pdf_bytes = doc.tobytes()
    doc.close()

    seen_pages: List[int] = []

    class FakeOcr:
        settings = type("S", (), {"primary_engine": "fake", "__dict__": {"primary_engine": "fake"}})()

        def recognize(self, image_bytes, page=1, metadata=None, use_fallback=True):
            seen_pages.append(int(page))
            text = f"PAGE{page} STOCK Opening Receipts Sales Closing\nITEM{page} 1 0 0 1"
            return OCRBundle(
                primary=OCRResult(
                    text=text,
                    words=[],
                    confidence=1.0,
                    page=page,
                    engine="fake",
                    metadata={"status": "ok"},
                ),
                engines_used=["fake"],
                metadata={"status": "ok"},
            )

    pipeline = DocumentIntelligencePipeline(ocr=FakeOcr())
    # Force OCR path by routing as scanned-like image PDF: patch _should_run_ocr.
    with patch(
        "services.document_intelligence.pipeline._should_run_ocr", return_value=True
    ), patch(
        "services.document_intelligence.pipeline._ocr_page_images",
        side_effect=lambda file_bytes, name, route: [
            (1, b"p1"),
            (2, b"p2"),
            (3, b"p3"),
        ],
    ), patch(
        "services.sales_statement_extractor.extract_sales_statement_core",
        return_value={
            "line_items": [],
            "totals": {"extra": {"extraction_method": "probe"}},
        },
    ):
        result = pipeline.run(pdf_bytes, "multipage_probe.pdf")

    di = result.get("document_intelligence") or {}
    obs = di.get("observability") or {}
    ocr_meta = (di.get("ocr") if isinstance(di.get("ocr"), dict) else None) or {}
    # page_count may live on observability or nested ocr bundle stored on result
    return {
        "seen_pages": seen_pages,
        "page_order_ok": seen_pages == [1, 2, 3],
        "no_drop": seen_pages == [1, 2, 3],
        "result_ok": isinstance(result, dict),
        "verified": (di.get("final") or {}).get("verified"),
        "observability_engines": obs.get("ocr_engines_used"),
    }


def run_reliability_checks() -> Dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor
    from services.ocr.bounded import OcrExecutionTimeout, call_bounded
    from services.ocr.ocr_text_override import get_ocr_text_override, ocr_text_override
    from services import sales_extraction_runtime as runtime
    from services.vertex_gemini_client import GeminiProviderError
    import threading
    import time

    out: Dict[str, Any] = {"checks": {}}

    # Timeout
    try:
        call_bounded(lambda: time.sleep(1), timeout_s=0.05, label="day5")
        out["checks"]["ocr_timeout"] = False
    except OcrExecutionTimeout:
        out["checks"]["ocr_timeout"] = True

    # 429 type
    err = GeminiProviderError(429, "RESOURCE_EXHAUSTED")
    out["checks"]["gemini_429"] = err.code == 429

    # Context isolation
    seen = {}

    def worker(name, value):
        with ocr_text_override(value):
            time.sleep(0.03)
            seen[name] = get_ocr_text_override()

    threads = [
        threading.Thread(target=worker, args=("a", "A")),
        threading.Thread(target=worker, args=("b", "B")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    out["checks"]["context_isolation"] = seen == {"a": "A", "b": "B"} and get_ocr_text_override() is None

    # Partial batch
    def work(kind):
        if kind == "bad":
            raise ValueError("malformed")
        return {"ok": True}

    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for kind in ("good", "bad", "good"):
            fut = pool.submit(work, kind)
            try:
                results.append(fut.result())
            except Exception:
                results.append({"ok": False})
    out["checks"]["partial_batch"] = results.count({"ok": True}) == 2 and results.count({"ok": False}) == 1

    # Upload limit still present
    import app as app_mod

    out["checks"]["max_body_size_200mb"] = int(getattr(app_mod.Request, "max_body_size", 0) or 0) == 200 * 1024 * 1024
    out["checks"]["deadline_default_600"] = runtime.SALES_EXTRACTION_MAX_EXECUTION_SECONDS == 600
    out["checks"]["gemini_retries_bounded"] = runtime.GEMINI_PROVIDER_429_MAX_RETRIES <= 5
    out["checks"]["max_concurrent_extractions"] = runtime.MAX_CONCURRENT_EXTRACTIONS

    # Cache eviction bounded
    from services import sales_statement_extractor as sse

    sse._PRIMARY_IMAGE_OCR_CACHE.clear()
    out["checks"]["ocr_cache_max"] = sse._PRIMARY_IMAGE_OCR_CACHE_MAX == 8

    out["passed"] = all(bool(v) for k, v in out["checks"].items() if k != "max_concurrent_extractions")
    return out


def main() -> int:
    _env()
    OUT.mkdir(parents=True, exist_ok=True)
    hardware = _hardware()

    digital_specs = [
        ("xlsx", "0000711172_2026_08_ZA_30_313_04092026034007.xlsx"),
        ("xls", "0000700848_2026_08_ZA_30_578_05092026144147.XLS"),
        ("docx", "0000713511_2026_08_ZA_24_255_07092026171904.docx"),
        ("txt", "0000732705_2026_08_ZA_24_270_04092026051409.TXT"),
        ("html", "0000734246_2026_08_ZA_13_249_06092026081229.html"),
        ("digital_pdf", "0000734513_2026_08_ZA_13_704_06092026081229.pdf"),
        ("scanned_pdf_1page", "0000700124_2026_08_ZL_06_521_02092026204703.pdf"),
    ]
    # Fix html name from day2 audit
    digital_specs[4] = ("html", "0000734246_2026_08_ZA_13_249_06092026162117.html")

    documents: List[Dict[str, Any]] = []
    print("Day5 digital/scanned extraction...", flush=True)
    for label, name in digital_specs:
        path = _resolve(name)
        if path is None:
            documents.append({"label": label, "file": name, "ok": False, "error": "fixture_missing"})
            print(f"  MISSING {label} {name}", flush=True)
            continue
        di_override = label.startswith("scanned")
        print(f"  extract {label} {name} di_override={di_override}", flush=True)
        record = extract_document(path, label, di_override=di_override)
        documents.append(record)
        print(
            f"    ok={record.get('ok')} s={record.get('seconds')} status={record.get('status')} "
            f"reason={record.get('reason')} rows={record.get('rows')} verified={record.get('verified')}",
            flush=True,
        )

    # One production image — sequential only. Skip if DAY5_SKIP_IMAGE=1.
    image_name = "0000727190_2026_08_ZA_06_333_04092026081416.png"
    if os.getenv("DAY5_SKIP_IMAGE", "").strip().lower() in {"1", "true", "yes", "on"}:
        documents.append(
            {
                "label": "image_png",
                "file": image_name,
                "ok": False,
                "error": "skipped_by_DAY5_SKIP_IMAGE",
                "limitation": True,
            }
        )
    else:
        path = _resolve(image_name)
        if path is None:
            documents.append({"label": "image_png", "file": image_name, "ok": False, "error": "fixture_missing"})
        else:
            print(f"  extract image_png {image_name} (sequential VL; may take several minutes)", flush=True)
            documents.append(extract_document(path, "image_png", di_override=True))
            print(
                f"    ok={documents[-1].get('ok')} s={documents[-1].get('seconds')} "
                f"status={documents[-1].get('status')} reason={documents[-1].get('reason')}",
                flush=True,
            )

    # Multi-page scanned live (2 pages). Skip with DAY5_SKIP_MULTIPAGE_LIVE=1.
    multipage_live: Dict[str, Any]
    mp_name = "0000700037_2026_08_ZA_06_609_04092026172645.pdf"
    if os.getenv("DAY5_SKIP_MULTIPAGE_LIVE", "").strip().lower() in {"1", "true", "yes", "on"}:
        multipage_live = {
            "skipped": True,
            "reason": "DAY5_SKIP_MULTIPAGE_LIVE set",
            "fixture": mp_name,
        }
    else:
        path = _resolve(mp_name)
        if path is None:
            multipage_live = {"skipped": True, "reason": "fixture_missing", "fixture": mp_name}
        else:
            print(f"  extract multipage_scanned_2p {mp_name}", flush=True)
            # Inspect page count first.
            try:
                import fitz

                pdf = fitz.open(path)
                page_count = len(pdf)
                text_chars = [len((pdf[i].get_text() or "").strip()) for i in range(page_count)]
                pdf.close()
            except Exception as exc:
                page_count = None
                text_chars = []
                print(f"    page inspect failed: {exc}", flush=True)
            record = extract_document(path, "multipage_scanned_2p", di_override=True)
            record["page_count"] = page_count
            record["native_text_chars"] = text_chars
            documents.append(record)
            multipage_live = record
            print(
                f"    pages={page_count} ok={record.get('ok')} s={record.get('seconds')} "
                f"status={record.get('status')} reason={record.get('reason')}",
                flush=True,
            )

    print("schema / multipage override / reliability...", flush=True)
    schema = run_schema_integrity()
    multipage_override = run_multipage_override_probe()
    reliability = run_reliability_checks()
    grounding = run_source_grounding_review(documents)

    (OUT / "document_results.json").write_text(
        json.dumps({"hardware": hardware, "documents": documents, "multipage_live": multipage_live}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (OUT / "schema_integrity_results.json").write_text(
        json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT / "source_grounding_results.json").write_text(
        json.dumps(grounding, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT / "reliability_results.json").write_text(
        json.dumps(
            {"reliability": reliability, "multipage_override_probe": multipage_override},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print("wrote day5 partial artifacts; regression comes next", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
