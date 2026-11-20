#!/usr/bin/env python3
"""Benchmark: geometry-first cell OCR for dense Excel stock screenshots.

Does NOT switch production. Feature flag STOCK_GEOMETRY_CELL_OCR_ENABLED
defaults to false and is not read by /extract-sales-statement yet.

Usage:
  python benchmark_stock_geometry_v3.py \
    --image /path/to/0000725384_2026_08_ZA_11_210_07092026105840.png
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--image", "--file", dest="image", required=True)
    p.add_argument("--no-gemini", action="store_true")
    p.add_argument("--max-gemini-cells", type=int, default=24)
    p.add_argument("--json-out", default="/tmp/stock_geometry_v3.json")
    p.add_argument("--txt-out", default="/tmp/stock_geometry_v3.txt")
    p.add_argument("--debug-png", default="/tmp/stock_grid_debug_v3.png")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    path = Path(args.image)
    if not path.is_file():
        print(f"ERROR: {path}", file=sys.stderr)
        return 2

    from services.stock_geometry_table_v3 import run_geometry_table_v3, write_reports

    report = run_geometry_table_v3(
        path,
        debug_png=args.debug_png,
        enable_gemini=not args.no_gemini,
        max_gemini_cells=args.max_gemini_cells,
    )
    if report.get("error"):
        print(json.dumps(report, indent=2, default=str))
        return 1

    jp, tp = write_reports(report, json_path=args.json_out, txt_path=args.txt_out)

    m = report.get("metrics") or {}
    blue = report.get("blue_suppression") or m.get("blue_suppression") or {}
    print("=== METRICS ===")
    for k in (
        "product_row_recall",
        "physical_cell_accuracy",
        "semantic_field_accuracy",
        "row_accuracy",
        "column_alignment_accuracy",
        "blank_cell_precision",
        "row_alignment_errors",
        "column_alignment_errors",
        "lms_opening_shift_errors",
        "opstk_receipt_shift_errors",
        "cross_row_contamination_count",
        "missing_closing_cell_count",
        "metadata_rejection_count",
        "physical_slot_mismatches",
        "benchmark_failed_semantic_shift",
        "product_rows_detected",
        "non_product_rows_rejected",
        "reconciliation_pass_rate",
        "gemini_fallback_cell_count",
        "gemini_fallback_latency_ms",
        "total_latency_ms",
        "lms_accuracy",
        "opening_accuracy",
        "purchase_receipt_accuracy",
        "sales_accuracy",
        "closing_accuracy",
        "value_accuracy",
        # hybrid 10-col metrics
        "cell_accuracy",
        "exact_row_accuracy",
        "product_match_accuracy",
    ):
        if k in m:
            print(f"{k}: {m.get(k)}")

    print("\n=== BLUE SUPPRESSION ===")
    print(json.dumps(blue, indent=2, default=str))
    print("blue_pixels_detected:", blue.get("blue_pixels_detected"))
    print("blue_cells_detected:", blue.get("blue_cells_detected"))
    print("cells_recovered:", blue.get("cells_recovered"))
    gem = report.get("gemini") or {}
    print("gemini_fallback_cells:", gem.get("cells_applied") or gem.get("api_calls"))

    print("\n=== NON_PRODUCT REJECTED ===")
    for dr in report.get("debug_rows") or []:
        if dr.get("row_class") == "NON_PRODUCT":
            print(json.dumps(dr, default=str))

    print("\n=== FOCUS ROWS (physical + business) ===")
    for f in report.get("focus") or []:
        print(json.dumps(f, default=str))
        sel = f.get("selected") or {}
        if f.get("product") == "ARJUNA TAB" or "ARJUNA" in str(f.get("product") or "").upper():
            print(
                "ARJUNA_QTY:",
                sel.get("opening_qty"),
                sel.get("purchase_qty") or sel.get("receipts_qty"),
                sel.get("sales_return_qty") or sel.get("goods_return_qty"),
                sel.get("total_qty"),
                sel.get("sales_qty"),
                sel.get("closing_qty"),
            )
    if m.get("benchmark_failed_semantic_shift"):
        print("\nBENCHMARK FAILED: physical cells OK but semantic fields shifted")
        return 1

    print("\n=== FLAG / SAFETY ===")
    print(json.dumps(report.get("feature_flag"), default=str))
    print("production_switched=", report.get("production_switched"))
    print("flag_env_enabled=", report.get("feature_flag", {}).get("enabled"))
    print("detection_mode=", (report.get("geometry") or {}).get("detection_mode"))
    print(f"wrote {jp}\nwrote {tp}\nwrote {args.debug_png}")
    print("blue_debug=/tmp/stock_blue_suppressed_debug.png /tmp/stock_blue_mask_debug.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
