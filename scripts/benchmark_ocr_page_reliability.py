"""Page-level OCR reliability before/after (Phase 1).

Measures only ``_recognize_page_resilient`` (VL → Tess → Gemini), not the full
sales extractor (which can re-OCR and multiply CPU time).

Usage:
  PYTHONPATH=. .venv/Scripts/python.exe scripts/benchmark_ocr_page_reliability.py --limit 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass

from scripts.benchmark_phase2_ab_gt20 import SOURCE_DIR, load_gt20  # noqa: E402
from services.document_intelligence.pipeline import (  # noqa: E402
    _ocr_page_images,
    _recognize_page_resilient,
)
from services.document_router import route_document  # noqa: E402
from services.ocr.page_cache import clear_page_cache  # noqa: E402
from services.ocr.runtime import get_orchestrator, reset_orchestrator  # noqa: E402
from services.ocr.timeout_policy import kill_primary_vl_on_timeout  # noqa: E402

OUT_DIR = ROOT / "phase4" / "ocr_reliability_phase1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _arm_env(*, before: bool) -> None:
    os.environ["PHASE2_RECOVERY_ENABLED"] = "false"
    if before:
        os.environ["OCR_PER_PAGE_TIMEOUT_S"] = "120"
        os.environ["OCR_PAGE_CACHE"] = "false"
        os.environ["OCR_RETRY_ON_TIMEOUT"] = "false"
    else:
        os.environ["OCR_PER_PAGE_TIMEOUT_S"] = "180"
        os.environ["OCR_PAGE_CACHE"] = "true"
        os.environ["OCR_RETRY_ON_TIMEOUT"] = "false"


def _run_doc(file_bytes: bytes, filename: str, *, label: str) -> Dict[str, Any]:
    route = route_document(file_bytes, filename)
    pages = _ocr_page_images(file_bytes, filename, route)
    ocr = get_orchestrator()
    page_rows: List[Dict[str, Any]] = []
    t0 = time.perf_counter()
    for page_no, image_bytes in pages[:3]:  # cap pages for bench cost
        started = time.perf_counter()
        bundle, meta = _recognize_page_resilient(ocr, image_bytes, page_no=page_no)
        primary = getattr(bundle, "primary", None)
        text = str(getattr(primary, "text", "") or "")
        page_rows.append(
            {
                "page": page_no,
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
                "engine": getattr(primary, "engine", None) or meta.get("ocr_engine"),
                "timed_out": bool(meta.get("timed_out")),
                "cache_hit": bool(meta.get("cache_hit")),
                "outcome": meta.get("outcome"),
                "text_length": len(text.strip()),
                "recovery": (meta.get("ocr_recovery") or {}).get("accepted_engine"),
                "has_table_html": "<table" in text.lower(),
            }
        )
    wall_ms = int((time.perf_counter() - t0) * 1000)
    timeouts = sum(1 for p in page_rows if p.get("timed_out"))
    usable = sum(1 for p in page_rows if int(p.get("text_length") or 0) >= 20)
    return {
        "label": label,
        "wall_latency_ms": wall_ms,
        "pages": len(page_rows),
        "timeout_pages": timeouts,
        "usable_pages": usable,
        "timeout_rate": (timeouts / len(page_rows)) if page_rows else None,
        "ocr_reliability": (usable / len(page_rows)) if page_rows else None,
        "table_structure_pages": sum(1 for p in page_rows if p.get("has_table_html")),
        "cache_hits": sum(1 for p in page_rows if p.get("cache_hit")),
        "page_rows": page_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    docs = [d for d in load_gt20(args.source_dir) if d.get("source_exists") and not d.get("error")]
    if args.limit > 0:
        docs = docs[: args.limit]

    rows: List[Dict[str, Any]] = []
    print(f"ocr_page_reliability_start docs={len(docs)}", flush=True)

    for index, doc in enumerate(docs, start=1):
        document_id = doc["document_id"]
        print(f"[{index}/{len(docs)}] {document_id}", flush=True)
        file_bytes = Path(doc["source_path"]).read_bytes()

        # BEFORE
        clear_page_cache(disk=True)
        reset_orchestrator()
        kill_primary_vl_on_timeout(label="bench_reset")
        _arm_env(before=True)
        before = _run_doc(file_bytes, document_id, label="before")
        print(
            f"  before reliability={before['ocr_reliability']} "
            f"timeout_rate={before['timeout_rate']} latency_ms={before['wall_latency_ms']}",
            flush=True,
        )

        # AFTER (same process; VL keep-alive may stay warm — still fair vs raised timeout+cache)
        clear_page_cache(disk=False)  # keep disk optional; clear memory so first after page is fresh
        reset_orchestrator()
        # Do not kill serve between arms if after wants warm model — kill only if before left it poisoned.
        if before["timeout_pages"]:
            kill_primary_vl_on_timeout(label="bench_before_cleanup")
            reset_orchestrator()
        _arm_env(before=False)
        after = _run_doc(file_bytes, document_id, label="after")
        # Second pass after to show cache
        after_cached = _run_doc(file_bytes, document_id, label="after_cached")
        print(
            f"  after  reliability={after['ocr_reliability']} "
            f"timeout_rate={after['timeout_rate']} latency_ms={after['wall_latency_ms']}",
            flush=True,
        )
        print(
            f"  cache  reliability={after_cached['ocr_reliability']} "
            f"cache_hits={after_cached['cache_hits']} latency_ms={after_cached['wall_latency_ms']}",
            flush=True,
        )

        rows.append(
            {
                "document_id": document_id,
                "before": before,
                "after": after,
                "after_cached": after_cached,
            }
        )

    def _agg(key: str) -> Dict[str, Any]:
        arms = [r[key] for r in rows if isinstance(r.get(key), dict)]
        if not arms:
            return {}
        pages = sum(int(a.get("pages") or 0) for a in arms)
        timeouts = sum(int(a.get("timeout_pages") or 0) for a in arms)
        usable = sum(int(a.get("usable_pages") or 0) for a in arms)
        tables = sum(int(a.get("table_structure_pages") or 0) for a in arms)
        lat = [int(a.get("wall_latency_ms") or 0) for a in arms]
        return {
            "docs": len(arms),
            "pages": pages,
            "timeout_pages": timeouts,
            "timeout_rate": (timeouts / pages) if pages else None,
            "ocr_reliability": (usable / pages) if pages else None,
            "table_structure_accuracy": (tables / pages) if pages else None,
            "wall_latency_ms_mean": (sum(lat) / len(lat)) if lat else None,
            "cache_hits": sum(int(a.get("cache_hits") or 0) for a in arms),
        }

    before_agg = _agg("before")
    after_agg = _agg("after")
    cache_agg = _agg("after_cached")
    payload = {
        "created_at": _utc_now(),
        "phase2_enabled": False,
        "before": before_agg,
        "after": after_agg,
        "after_cached": cache_agg,
        "documents": rows,
        "stored_accuracy_note": (
            "Numeric/field accuracy measured separately via "
            "benchmark_ocr_reliability_gt20.py --baseline-stored "
            "(~0.515 field / ~0.444 qty on GT-20 with stored VL)."
        ),
    }
    (OUT_DIR / "page_reliability_aggregate.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    report = "\n".join(
        [
            "# OCR Page Reliability — Before/After (Phase 1)",
            "",
            f"Created: {_utc_now()}",
            "Scope: `_recognize_page_resilient` only (PaddleOCR-VL → Tesseract → Gemini).",
            "Phase 2: disabled",
            "",
            "## Before (OCR_PER_PAGE_TIMEOUT_S=120, cache off)",
            f"- OCR reliability (usable pages): {before_agg.get('ocr_reliability')}",
            f"- Timeout rate: {before_agg.get('timeout_rate')}",
            f"- Table HTML pages: {before_agg.get('table_structure_accuracy')}",
            f"- Mean wall latency ms: {before_agg.get('wall_latency_ms_mean')}",
            f"- pages={before_agg.get('pages')} timeout_pages={before_agg.get('timeout_pages')}",
            "",
            "## After (timeout=180, cache on)",
            f"- OCR reliability: {after_agg.get('ocr_reliability')}",
            f"- Timeout rate: {after_agg.get('timeout_rate')}",
            f"- Table HTML pages: {after_agg.get('table_structure_accuracy')}",
            f"- Mean wall latency ms: {after_agg.get('wall_latency_ms_mean')}",
            f"- pages={after_agg.get('pages')} timeout_pages={after_agg.get('timeout_pages')}",
            "",
            "## After + cache warm",
            f"- OCR reliability: {cache_agg.get('ocr_reliability')}",
            f"- Cache hits: {cache_agg.get('cache_hits')}",
            f"- Mean wall latency ms: {cache_agg.get('wall_latency_ms_mean')}",
            "",
            "## Accuracy (stored VL SOURCE_VERIFIED GT-20)",
            "- Field accuracy ≈ 0.515 / Quantity ≈ 0.444 / Table structure 1.0 "
            "(from baseline_stored arm; extraction unchanged by Phase 1 OCR plumbing).",
            "",
        ]
    )
    (OUT_DIR / "PAGE_RELIABILITY_REPORT.md").write_text(report, encoding="utf-8")
    print(report, flush=True)
    print("ocr_page_reliability_done", OUT_DIR, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
