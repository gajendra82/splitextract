"""SOURCE_VERIFIED GT-20 OCR reliability benchmark (Phase 1).

Runs Phase2-OFF extraction and records OCR reliability + accuracy metrics.

Modes:
  --baseline-stored   Score with stored VL OCR (accuracy baseline; no live OCR)
  --live-ocr          Live PaddleOCR-VL → Tess → Gemini path (measures timeouts)
  --before-after      Two live arms: legacy 120s/no-cache vs Phase1 policy

Usage:
  PYTHONPATH=. .venv/Scripts/python.exe scripts/benchmark_ocr_reliability_gt20.py --baseline-stored
  PYTHONPATH=. .venv/Scripts/python.exe scripts/benchmark_ocr_reliability_gt20.py --live-ocr --limit 3
  PYTHONPATH=. .venv/Scripts/python.exe scripts/benchmark_ocr_reliability_gt20.py --before-after --limit 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass

from scripts.benchmark_phase2_ab_gt20 import (  # noqa: E402
    SOURCE_DIR,
    load_gt20,
    load_stored_vl_text,
    score_extraction,
    slim_result,
)
from services.ocr.ocr_text_override import ocr_text_override  # noqa: E402
from services.sales_statement_extractor import (  # noqa: E402
    attach_phase1_architecture,
    extract_sales_statement,
    extract_sales_statement_core,
)

OUT_DIR = ROOT / "phase4" / "ocr_reliability_phase1"
PER_DIR = OUT_DIR / "per_document"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_phase2_off() -> None:
    os.environ["PHASE2_RECOVERY_ENABLED"] = "false"


def _ocr_metrics_from_result(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "pages": 0,
            "timeout_pages": 0,
            "empty_pages": 0,
            "cache_hits": 0,
            "engines": [],
            "recovery_engines": [],
            "page_timing": [],
            "has_table_html": False,
            "di_ocr_override": False,
        }
    extra = ((result.get("totals") or {}).get("extra") or {})
    ocr = result.get("ocr") if isinstance(result.get("ocr"), dict) else {}
    if not ocr:
        di = result.get("document_intelligence") or result.get("phase4") or {}
        if isinstance(di, dict):
            ocr = di.get("ocr") if isinstance(di.get("ocr"), dict) else {}
    if not ocr:
        arch = result.get("architecture") if isinstance(result.get("architecture"), dict) else {}
        ocr = arch.get("ocr") if isinstance(arch.get("ocr"), dict) else {}
    timing = list(ocr.get("page_timing") or [])
    recovery = list(ocr.get("ocr_recovery") or [])
    evidence = ocr.get("evidence") if isinstance(ocr.get("evidence"), dict) else {}
    pages = evidence.get("pages") if isinstance(evidence.get("pages"), list) else []
    timeout_pages = sum(1 for t in timing if t.get("timed_out"))
    if not timeout_pages and pages:
        timeout_pages = sum(
            1
            for p in pages
            if isinstance(p, dict)
            and (
                p.get("status") == "timeout"
                or p.get("timed_out")
                or "timeout" in str(p.get("error") or "").lower()
            )
        )
    empty_pages = sum(
        1
        for p in pages
        if isinstance(p, dict) and int(p.get("text_length") or 0) < 20
    )
    cache_hits = sum(1 for t in timing if t.get("cache_hit"))
    engines = list(ocr.get("engines_used") or [])
    recovery_engines = [
        r.get("accepted_engine")
        for r in recovery
        if isinstance(r, dict) and r.get("accepted_engine")
    ]
    html = str(extra.get("ocr_table_html") or "")
    return {
        "pages": len(timing) or len(pages) or int(ocr.get("page_count") or 0),
        "timeout_pages": timeout_pages,
        "empty_pages": empty_pages,
        "cache_hits": cache_hits,
        "engines": engines,
        "recovery_engines": recovery_engines,
        "page_timing": timing,
        "ocr_timed_out": bool(ocr.get("timed_out")),
        "has_table_html": "<table" in html.lower(),
        "di_ocr_override": bool(extra.get("di_ocr_override")),
        "ocr_source": extra.get("ocr_source") or ocr.get("ocr_source"),
        "text_length": len(str(extra.get("ocr_text") or html or "")),
    }


def _table_structure_ok(result: Optional[Dict[str, Any]], ocr_m: Dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    items = result.get("line_items") or []
    if items:
        return True
    return bool(ocr_m.get("has_table_html"))


def run_stored(file_bytes: bytes, filename: str, ocr_text: str) -> Dict[str, Any]:
    _set_phase2_off()
    started = time.perf_counter()
    error = None
    result = None
    try:
        with ocr_text_override(ocr_text):
            result = extract_sales_statement_core(file_bytes, filename)
        if isinstance(result, dict):
            extra = result.setdefault("totals", {}).setdefault("extra", {})
            if isinstance(extra, dict):
                extra["di_ocr_override"] = True
                if "<table" in ocr_text.lower():
                    extra.setdefault("ocr_table_html", ocr_text[:20000])
                else:
                    extra.setdefault("ocr_text", ocr_text[:20000])
                extra.setdefault("ocr_source", "paddleocr-vl-stored")
            result = attach_phase1_architecture(result, file_bytes, filename)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {"result": result, "error": error, "wall_latency_ms": elapsed_ms, "mode": "stored_vl"}


def run_live(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    _set_phase2_off()
    started = time.perf_counter()
    error = None
    result = None
    try:
        result = extract_sales_statement(file_bytes, filename)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {"result": result, "error": error, "wall_latency_ms": elapsed_ms, "mode": "live_ocr"}


def _score_row(
    document_id: str,
    fields: List[Dict[str, Any]],
    arm: Dict[str, Any],
    *,
    label: str,
) -> Dict[str, Any]:
    result = arm.get("result")
    score = score_extraction(result, fields)
    ocr_m = _ocr_metrics_from_result(result)
    metrics = score.get("metrics") or {}
    return {
        "label": label,
        "error": arm.get("error"),
        "wall_latency_ms": arm.get("wall_latency_ms"),
        "mode": arm.get("mode"),
        "ocr": ocr_m,
        "field_accuracy": metrics.get("field_accuracy"),
        "quantity_accuracy": metrics.get("quantity_accuracy"),
        "missing_values": metrics.get("missing_values"),
        "table_structure_ok": _table_structure_ok(result, ocr_m),
        "score": {k: v for k, v in score.items() if k != "matches"},
        "result": slim_result(result),
    }


def _aggregate_arm(rows: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    arms = [r[key] for r in rows if isinstance(r.get(key), dict) and not r.get("error")]
    if not arms:
        return {"docs": 0}
    field_acc = [a["field_accuracy"] for a in arms if a.get("field_accuracy") is not None]
    qty_acc = [a["quantity_accuracy"] for a in arms if a.get("quantity_accuracy") is not None]
    timeouts = sum(int((a.get("ocr") or {}).get("timeout_pages") or 0) for a in arms)
    pages = sum(int((a.get("ocr") or {}).get("pages") or 0) for a in arms)
    empty = sum(int((a.get("ocr") or {}).get("empty_pages") or 0) for a in arms)
    recovered = sum(len((a.get("ocr") or {}).get("recovery_engines") or []) for a in arms)
    cache_hits = sum(int((a.get("ocr") or {}).get("cache_hits") or 0) for a in arms)
    table_ok = sum(1 for a in arms if a.get("table_structure_ok"))
    lat = [int(a.get("wall_latency_ms") or 0) for a in arms]
    ocr_fail_docs = sum(
        1
        for a in arms
        if (a.get("ocr") or {}).get("ocr_timed_out")
        or int((a.get("ocr") or {}).get("timeout_pages") or 0) > 0
        or (
            int((a.get("ocr") or {}).get("empty_pages") or 0) > 0
            and not (a.get("ocr") or {}).get("recovery_engines")
        )
    )
    return {
        "docs": len(arms),
        "field_accuracy_mean": (sum(field_acc) / len(field_acc)) if field_acc else None,
        "quantity_accuracy_mean": (sum(qty_acc) / len(qty_acc)) if qty_acc else None,
        "pages": pages,
        "timeout_pages": timeouts,
        "timeout_rate": (timeouts / pages) if pages else None,
        "empty_pages": empty,
        "recovery_accepts": recovered,
        "cache_hits": cache_hits,
        "ocr_unreliable_docs": ocr_fail_docs,
        "ocr_reliability": 1.0 - (ocr_fail_docs / len(arms)) if arms else None,
        "table_structure_accuracy": (table_ok / len(arms)) if arms else None,
        "wall_latency_ms_mean": (sum(lat) / len(lat)) if lat else None,
        "wall_latency_ms_sum": sum(lat),
    }


def render_report(
    *,
    before: Optional[Dict[str, Any]],
    after: Dict[str, Any],
    mode: str,
    rows: List[Dict[str, Any]],
) -> str:
    lines = [
        "# OCR Reliability Phase 1 — SOURCE_VERIFIED Benchmark",
        "",
        f"Created: {_utc_now()}",
        f"Mode: `{mode}`",
        f"Phase 2: disabled (`PHASE2_RECOVERY_ENABLED=false`)",
        "",
        "## Baseline (prior / before)",
    ]
    if before:
        lines.extend(
            [
                f"- OCR reliability: {before.get('ocr_reliability')}",
                f"- Timeout rate: {before.get('timeout_rate')}",
                f"- Field accuracy: {before.get('field_accuracy_mean')}",
                f"- Quantity accuracy: {before.get('quantity_accuracy_mean')}",
                f"- Table structure accuracy: {before.get('table_structure_accuracy')}",
                f"- Mean wall latency ms: {before.get('wall_latency_ms_mean')}",
                f"- Docs: {before.get('docs')} pages={before.get('pages')} "
                f"timeout_pages={before.get('timeout_pages')}",
            ]
        )
    else:
        lines.extend(
            [
                "- Prior live smoke: `ocr page=1 timed out after 120.0s` (effective page deadline).",
                "- Stored-VL Phase2 A/B field accuracy OFF: **0.518** / qty **0.465** "
                "(COMPARISON_REPORT; OCR mode stored, not live).",
                "- Live OCR was previously unreliable enough that GT-20 A/B used stored VL text.",
            ]
        )
    lines.extend(
        [
            "",
            "## After (Phase 1 policy)",
            f"- OCR reliability: {after.get('ocr_reliability')}",
            f"- Timeout rate: {after.get('timeout_rate')}",
            f"- Field accuracy: {after.get('field_accuracy_mean')}",
            f"- Quantity accuracy: {after.get('quantity_accuracy_mean')}",
            f"- Table structure accuracy: {after.get('table_structure_accuracy')}",
            f"- Mean wall latency ms: {after.get('wall_latency_ms_mean')}",
            f"- Cache hits: {after.get('cache_hits')}",
            f"- Recovery accepts: {after.get('recovery_accepts')}",
            f"- Docs: {after.get('docs')} pages={after.get('pages')} "
            f"timeout_pages={after.get('timeout_pages')}",
            "",
            "## Notes",
            "- Recovery order preserved: PaddleOCR-VL → Tesseract → Gemini transcription.",
            "- Empty/timeout OCR is never cached or stamped as success.",
            "- Page-level timeouts kill the VL serve process before recovery.",
            "",
            "## Per-document",
            "",
            "| document_id | arm | field_acc | qty_acc | timeout_pages | latency_ms | table_ok |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for row in rows:
        for arm_key in ("before", "after", "stored", "live"):
            arm = row.get(arm_key)
            if not isinstance(arm, dict):
                continue
            ocr = arm.get("ocr") or {}
            lines.append(
                "| {doc} | {arm} | {fa} | {qa} | {to} | {lat} | {tbl} |".format(
                    doc=row.get("document_id"),
                    arm=arm_key,
                    fa=arm.get("field_accuracy"),
                    qa=arm.get("quantity_accuracy"),
                    to=ocr.get("timeout_pages"),
                    lat=arm.get("wall_latency_ms"),
                    tbl=arm.get("table_structure_ok"),
                )
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--baseline-stored", action="store_true")
    parser.add_argument("--live-ocr", action="store_true")
    parser.add_argument("--before-after", action="store_true")
    args = parser.parse_args()

    if not (args.baseline_stored or args.live_ocr or args.before_after):
        args.baseline_stored = True

    _set_phase2_off()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PER_DIR.mkdir(parents=True, exist_ok=True)

    docs = load_gt20(args.source_dir)
    if args.limit and args.limit > 0:
        docs = docs[: args.limit]

    rows: List[Dict[str, Any]] = []
    mode = (
        "before_after_live"
        if args.before_after
        else ("live_ocr" if args.live_ocr else "baseline_stored")
    )
    print(f"ocr_reliability_start mode={mode} docs={len(docs)}", flush=True)

    for index, doc in enumerate(docs, start=1):
        document_id = doc["document_id"]
        print(f"[{index}/{len(docs)}] {document_id}", flush=True)
        if doc.get("error") or not doc.get("source_exists"):
            row = {
                "document_id": document_id,
                "error": doc.get("error") or f"missing_source:{doc.get('source_path')}",
            }
            rows.append(row)
            continue

        file_bytes = Path(doc["source_path"]).read_bytes()
        fields = doc["fields"]
        row: Dict[str, Any] = {"document_id": document_id, "verified_fields": len(fields)}

        if args.baseline_stored and not args.before_after and not args.live_ocr:
            stored = load_stored_vl_text(document_id)
            if not stored:
                row["error"] = "missing_stored_vl_ocr"
                rows.append(row)
                continue
            arm = run_stored(file_bytes, document_id, stored)
            row["stored"] = _score_row(document_id, fields, arm, label="stored")
        elif args.before_after:
            # Before: legacy aggressive page timeout + cache off.
            os.environ["OCR_PER_PAGE_TIMEOUT_S"] = "120"
            os.environ["OCR_PAGE_CACHE"] = "false"
            os.environ["OCR_RETRY_ON_TIMEOUT"] = "false"
            before_arm = run_live(file_bytes, document_id)
            row["before"] = _score_row(document_id, fields, before_arm, label="before")

            # After: Phase 1 defaults.
            os.environ["OCR_PER_PAGE_TIMEOUT_S"] = "180"
            os.environ["OCR_PAGE_CACHE"] = "true"
            os.environ["OCR_RETRY_ON_TIMEOUT"] = "false"
            after_arm = run_live(file_bytes, document_id)
            row["after"] = _score_row(document_id, fields, after_arm, label="after")
        else:
            os.environ.setdefault("OCR_PER_PAGE_TIMEOUT_S", "180")
            os.environ.setdefault("OCR_PAGE_CACHE", "true")
            live_arm = run_live(file_bytes, document_id)
            row["live"] = _score_row(document_id, fields, live_arm, label="live")

        rows.append(row)
        (PER_DIR / f"{document_id.replace('/', '_')}.json").write_text(
            json.dumps(row, indent=2, default=str), encoding="utf-8"
        )
        summary_arm = row.get("after") or row.get("live") or row.get("stored") or {}
        print(
            "  field_acc={fa} qty_acc={qa} timeout_pages={to} latency_ms={lat}".format(
                fa=summary_arm.get("field_accuracy"),
                qa=summary_arm.get("quantity_accuracy"),
                to=(summary_arm.get("ocr") or {}).get("timeout_pages"),
                lat=summary_arm.get("wall_latency_ms"),
            ),
            flush=True,
        )

    before_agg = _aggregate_arm(rows, "before") if args.before_after else None
    after_key = "after" if args.before_after else ("live" if args.live_ocr else "stored")
    after_agg = _aggregate_arm(rows, after_key)

    payload = {
        "created_at": _utc_now(),
        "mode": mode,
        "phase2_enabled": False,
        "before": before_agg,
        "after": after_agg,
        "documents": rows,
    }
    (OUT_DIR / "aggregate.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    report = render_report(before=before_agg, after=after_agg, mode=mode, rows=rows)
    (OUT_DIR / "BENCHMARK_REPORT.md").write_text(report, encoding="utf-8")
    _set_phase2_off()
    print("ocr_reliability_done", OUT_DIR, flush=True)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
