"""
Compare FINAL /test-extract JSON for the same PDF under two OCR modes.

Does NOT modify application OCR/Gemini logic — only toggles PADDLE_OCR_ENABLED
and calls the existing FastAPI /test-extract endpoint via TestClient.

Usage:
  python scripts/compare_test_extract_pipelines.py path\\to\\invoice.pdf
  python scripts/compare_test_extract_pipelines.py path\\to\\invoice.pdf --out-dir comparison_results
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _set_paddle_flag(enabled: bool) -> None:
    os.environ["PADDLE_OCR_ENABLED"] = "true" if enabled else "false"
    # Keep OneDNN off on Windows when Paddle may load
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    os.environ.setdefault("FLAGS_onednn", "0")
    try:
        from services.paddle_ocr import reset_paddle_ocr_engine_for_tests

        reset_paddle_ocr_engine_for_tests()
    except Exception:
        pass


def _run_test_extract(pdf_path: Path, paddle_enabled: bool) -> Tuple[Dict[str, Any], float]:
    """Invoke POST /test-extract with the given PDF; return (json, elapsed_seconds)."""
    _set_paddle_flag(paddle_enabled)

    # Windows consoles (cp1252) crash on emoji prints inside /test-extract.
    # Fix encoding in this test process only — do not change app.py.
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    # Import app after env flag is set so any module-level reads see it
    root = str(_project_root())
    if root not in sys.path:
        sys.path.insert(0, root)

    from fastapi.testclient import TestClient
    import app as app_mod

    label = "paddleocr" if paddle_enabled else "tesseract"
    print(f"\n{'=' * 70}")
    print(f"Running /test-extract  mode={label}  PADDLE_OCR_ENABLED={os.environ.get('PADDLE_OCR_ENABLED')}")
    print(f"PDF: {pdf_path}")
    print(f"{'=' * 70}")

    t0 = time.perf_counter()
    with TestClient(app_mod.app, raise_server_exceptions=False) as client:
        with pdf_path.open("rb") as fh:
            response = client.post(
                "/test-extract",
                files={"file": (pdf_path.name, fh, "application/pdf")},
            )
    elapsed = time.perf_counter() - t0

    if response.status_code != 200:
        raise RuntimeError(
            f"/test-extract failed mode={label} status={response.status_code} body={response.text[:2000]}"
        )

    data = response.json()
    data["_comparison_meta"] = {
        "mode": label,
        "paddle_ocr_enabled": paddle_enabled,
        "pdf": str(pdf_path),
        "elapsed_seconds": round(elapsed, 3),
        "http_status": response.status_code,
        "ran_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    print(f"Done mode={label} in {elapsed:.1f}s  invoices={len(data.get('invoices') or [])}")
    return data, elapsed


def _first_invoice(payload: Dict[str, Any]) -> Dict[str, Any]:
    invoices = payload.get("invoices") or []
    return invoices[0] if invoices else {}


def _extracted_data(inv: Dict[str, Any]) -> Dict[str, Any]:
    ed = inv.get("extracted_data") or {}
    if isinstance(ed, dict) and "data" in ed:
        return ed
    return ed if isinstance(ed, dict) else {}


def _summary(ed: Dict[str, Any]) -> Dict[str, Any]:
    data = ed.get("data") if isinstance(ed.get("data"), dict) else ed
    if not isinstance(data, dict):
        return {}
    summary = data.get("invoice_summary")
    return summary if isinstance(summary, dict) else {}


def _line_items(ed: Dict[str, Any]) -> Any:
    data = ed.get("data") if isinstance(ed.get("data"), dict) else ed
    if not isinstance(data, dict):
        return None
    return data.get("line_items")


def _ocr_text_from_invoice(inv: Dict[str, Any], ed: Dict[str, Any]) -> str:
    """Prefer pipeline raw_ocr_text; fall back to nested ocr_text."""
    raw = inv.get("raw_ocr_text")
    if isinstance(raw, str) and raw.strip():
        return raw
    data = ed.get("data") if isinstance(ed.get("data"), dict) else ed
    if isinstance(data, dict):
        return str(data.get("ocr_text") or "")
    return ""


def _collect_page_ocr_meta(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort OCR method/confidence from invoice payloads / stats."""
    methods: List[str] = []
    confidences: List[float] = []
    ocr_chars = 0

    for inv in payload.get("invoices") or []:
        ed = _extracted_data(inv) if inv.get("extracted_data") else {}
        ocr_chars += len(_ocr_text_from_invoice(inv, ed))
        for key in ("ocr_method", "extraction_method"):
            if inv.get(key):
                methods.append(str(inv.get(key)))
        if isinstance(ed, dict):
            for key in ("ocr_method", "extraction_method", "ocr_confidence"):
                if key in ed and ed[key] is not None:
                    if "conf" in key:
                        try:
                            confidences.append(float(ed[key]))
                        except (TypeError, ValueError):
                            pass
                    else:
                        methods.append(str(ed[key]))

    stats = payload.get("ocr_statistics") or {}
    return {
        "ocr_statistics": stats,
        "ocr_methods_seen": sorted(set(methods)),
        "ocr_confidence_values": confidences,
        "ocr_confidence_avg": round(sum(confidences) / len(confidences), 2) if confidences else None,
        "ocr_character_count": ocr_chars,
    }


def _pipeline_notes(payload: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    inv = _first_invoice(payload)
    if inv.get("extracted_data") in (None, {}, ""):
        notes.append(
            "extracted_data is null/empty — structured invoice fields were not produced "
            "(often Gemini/Vertex credentials failure)."
        )
    stats = payload.get("ocr_statistics") or {}
    if int(stats.get("gemini_text_api") or 0) or int(stats.get("gemini_vision") or 0):
        if inv.get("extracted_data") in (None, {}, ""):
            notes.append(
                "Gemini was called but did not return usable structured JSON for this run."
            )
    return notes


def _field_compare(name: str, tess_val: Any, paddle_val: Any) -> Dict[str, Any]:
    identical = tess_val == paddle_val
    # Normalize empty-ish
    if tess_val is None and paddle_val in ("", None):
        identical = True
    if paddle_val is None and tess_val in ("", None):
        identical = True
    row = {
        "field": name,
        "tesseract": tess_val,
        "paddleocr": paddle_val,
        "identical": identical,
    }
    if not identical:
        row["difference"] = {
            "tesseract": tess_val,
            "paddleocr": paddle_val,
        }
    return row


def _build_comparison(
    tess: Dict[str, Any],
    paddle: Dict[str, Any],
    tess_elapsed: float,
    paddle_elapsed: float,
    pdf_path: Path,
) -> Dict[str, Any]:
    t_inv = _first_invoice(tess)
    p_inv = _first_invoice(paddle)
    t_ed = _extracted_data(t_inv) if t_inv.get("extracted_data") else {}
    p_ed = _extracted_data(p_inv) if p_inv.get("extracted_data") else {}
    t_sum = _summary(t_ed)
    p_sum = _summary(p_ed)
    t_ocr_meta = _collect_page_ocr_meta(tess)
    p_ocr_meta = _collect_page_ocr_meta(paddle)
    t_raw = _ocr_text_from_invoice(t_inv, t_ed)
    p_raw = _ocr_text_from_invoice(p_inv, p_ed)

    t_method = (
        "paddleocr" if int((tess.get("ocr_statistics") or {}).get("paddleocr") or 0) > 0
        else "tesseract"
        if int((tess.get("ocr_statistics") or {}).get("tesseract") or 0) > 0
        else (t_ocr_meta["ocr_methods_seen"][0] if t_ocr_meta["ocr_methods_seen"] else "unknown")
    )
    p_method = (
        "paddleocr" if int((paddle.get("ocr_statistics") or {}).get("paddleocr") or 0) > 0
        else "tesseract"
        if int((paddle.get("ocr_statistics") or {}).get("tesseract") or 0) > 0
        else (p_ocr_meta["ocr_methods_seen"][0] if p_ocr_meta["ocr_methods_seen"] else "unknown")
    )

    summary_keys = [
        "invoice_no",
        "invoice_date",
        "invoice_date_raw",
        "vendor",
        "vendor_gstin",
        "customer",
        "customer_gstin",
        "customer_address",
        "irn",
        "tax",
        "total",
        "calculated_total",
        "subtotal",
        "cgst",
        "sgst",
        "igst",
    ]
    extra_keys = sorted(set(t_sum.keys()) | set(p_sum.keys()))
    for k in extra_keys:
        if k not in summary_keys:
            summary_keys.append(k)

    field_rows: List[Dict[str, Any]] = [
        _field_compare("ocr_method", t_method, p_method),
        _field_compare(
            "ocr_character_count",
            t_ocr_meta["ocr_character_count"],
            p_ocr_meta["ocr_character_count"],
        ),
        _field_compare(
            "ocr_confidence_avg",
            t_ocr_meta["ocr_confidence_avg"],
            p_ocr_meta["ocr_confidence_avg"],
        ),
        _field_compare("extraction_time_seconds", round(tess_elapsed, 3), round(paddle_elapsed, 3)),
        _field_compare(
            "ocr_statistics",
            tess.get("ocr_statistics"),
            paddle.get("ocr_statistics"),
        ),
        _field_compare("invoice.invoice_no", t_inv.get("invoice_no"), p_inv.get("invoice_no")),
        _field_compare(
            "invoice.extracted_data_present",
            t_inv.get("extracted_data") not in (None, {}, ""),
            p_inv.get("extracted_data") not in (None, {}, ""),
        ),
        _field_compare("raw_ocr_text", t_raw, p_raw),
    ]

    for key in summary_keys:
        field_rows.append(
            _field_compare(
                f"invoice_summary.{key}",
                t_sum.get(key),
                p_sum.get(key),
            )
        )

    t_items = _line_items(t_ed)
    p_items = _line_items(p_ed)
    field_rows.append(_field_compare("line_items", t_items, p_items))

    def _item_count(li: Any) -> Optional[int]:
        if isinstance(li, dict):
            if isinstance(li.get("count"), int):
                return li["count"]
            items = li.get("items")
            if isinstance(items, list):
                return len(items)
        if isinstance(li, list):
            return len(li)
        return None

    field_rows.append(
        _field_compare(
            "line_items.count",
            _item_count(t_items),
            _item_count(p_items),
        )
    )

    diffs = [r for r in field_rows if not r.get("identical")]
    same = [r for r in field_rows if r.get("identical")]

    return {
        "pdf": str(pdf_path),
        "compared_at_utc": datetime.now(timezone.utc).isoformat(),
        "pipeline": "POST /test-extract (full existing pipeline)",
        "notes": {
            "tesseract_run": _pipeline_notes(tess),
            "paddleocr_run": _pipeline_notes(paddle),
            "important": (
                "Structured invoice_summary/line_items require a working Vertex/Gemini credential. "
                "If extracted_data is null in both result files, fix GOOGLE_APPLICATION_CREDENTIALS "
                "and re-run this script — OCR-layer differences are still visible via "
                "ocr_statistics / raw_ocr_text."
            ),
        },
        "runs": {
            "tesseract": {
                "paddle_ocr_enabled": False,
                "elapsed_seconds": round(tess_elapsed, 3),
                "ocr_method": t_method,
                "ocr_character_count": t_ocr_meta["ocr_character_count"],
                "ocr_confidence_avg": t_ocr_meta["ocr_confidence_avg"],
                "ocr_statistics": tess.get("ocr_statistics"),
                "summary": tess.get("summary"),
            },
            "paddleocr": {
                "paddle_ocr_enabled": True,
                "elapsed_seconds": round(paddle_elapsed, 3),
                "ocr_method": p_method,
                "ocr_character_count": p_ocr_meta["ocr_character_count"],
                "ocr_confidence_avg": p_ocr_meta["ocr_confidence_avg"],
                "ocr_statistics": paddle.get("ocr_statistics"),
                "summary": paddle.get("summary"),
            },
        },
        "fields": field_rows,
        "summary": {
            "fields_compared": len(field_rows),
            "identical_count": len(same),
            "different_count": len(diffs),
            "different_fields": [r["field"] for r in diffs],
        },
        "differences_only": diffs,
    }


def _rebuild_comparison_from_saved(out_dir: Path, pdf_path: Path) -> Dict[str, Any]:
    tess = json.loads((out_dir / "tesseract_result.json").read_text(encoding="utf-8"))
    paddle = json.loads((out_dir / "paddleocr_result.json").read_text(encoding="utf-8"))
    tess_elapsed = float((tess.get("_comparison_meta") or {}).get("elapsed_seconds") or 0)
    paddle_elapsed = float((paddle.get("_comparison_meta") or {}).get("elapsed_seconds") or 0)
    return _build_comparison(tess, paddle, tess_elapsed, paddle_elapsed, pdf_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare /test-extract Tesseract vs PaddleOCR JSON")
    parser.add_argument(
        "pdf",
        nargs="?",
        default=r"C:\Users\amnsa\Downloads\invoice_KAPL-26-35087.pdf",
        help="Path to invoice PDF",
    )
    parser.add_argument(
        "--out-dir",
        default="comparison_results",
        help="Output directory (relative to project root or absolute)",
    )
    parser.add_argument(
        "--order",
        choices=["tesseract-first", "paddle-first"],
        default="tesseract-first",
        help="Which OCR mode to run first",
    )
    parser.add_argument(
        "--rebuild-only",
        action="store_true",
        help="Rebuild comparison.json from existing result JSON files (no re-extract)",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.is_file() and not args.rebuild_only:
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = _project_root() / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.rebuild_only:
        comparison = _rebuild_comparison_from_saved(out_dir, pdf_path)
        cmp_path = out_dir / "comparison.json"
        cmp_path.write_text(
            json.dumps(comparison, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"Rebuilt {cmp_path}")
        print(
            f"Fields: {comparison['summary']['identical_count']} identical, "
            f"{comparison['summary']['different_count']} different"
        )
        for name in comparison["summary"]["different_fields"]:
            print(f"  - {name}")
        return 0

    # Preserve caller's flag after runs
    previous_flag = os.environ.get("PADDLE_OCR_ENABLED")

    try:
        if args.order == "tesseract-first":
            tess, tess_elapsed = _run_test_extract(pdf_path, paddle_enabled=False)
            paddle, paddle_elapsed = _run_test_extract(pdf_path, paddle_enabled=True)
        else:
            paddle, paddle_elapsed = _run_test_extract(pdf_path, paddle_enabled=True)
            tess, tess_elapsed = _run_test_extract(pdf_path, paddle_enabled=False)

        tess_path = out_dir / "tesseract_result.json"
        paddle_path = out_dir / "paddleocr_result.json"
        cmp_path = out_dir / "comparison.json"

        tess_path.write_text(json.dumps(tess, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        paddle_path.write_text(json.dumps(paddle, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

        comparison = _build_comparison(tess, paddle, tess_elapsed, paddle_elapsed, pdf_path)
        cmp_path.write_text(
            json.dumps(comparison, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

        print(f"\nSaved:\n  {tess_path}\n  {paddle_path}\n  {cmp_path}")
        print(
            f"Fields: {comparison['summary']['identical_count']} identical, "
            f"{comparison['summary']['different_count']} different"
        )
        if comparison["summary"]["different_fields"]:
            print("Different fields:")
            for name in comparison["summary"]["different_fields"]:
                print(f"  - {name}")
        for note in (comparison.get("notes") or {}).get("tesseract_run") or []:
            print(f"NOTE (tesseract): {note}")
        for note in (comparison.get("notes") or {}).get("paddleocr_run") or []:
            print(f"NOTE (paddleocr): {note}")
        if (comparison.get("notes") or {}).get("important"):
            print(f"IMPORTANT: {comparison['notes']['important']}")
        return 0
    finally:
        if previous_flag is None:
            os.environ.pop("PADDLE_OCR_ENABLED", None)
        else:
            os.environ["PADDLE_OCR_ENABLED"] = previous_flag
        try:
            from services.paddle_ocr import reset_paddle_ocr_engine_for_tests

            reset_paddle_ocr_engine_for_tests()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
