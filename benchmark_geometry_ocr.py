#!/usr/bin/env python3
"""CLI: geometry + per-cell OCR benchmark (does NOT change production)."""

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
    parser = argparse.ArgumentParser(description="Geometry + per-cell OCR benchmark")
    parser.add_argument("--file", required=True)
    parser.add_argument(
        "--no-gemini-fallback",
        action="store_true",
        help="Skip targeted Gemini cell-crop fallback",
    )
    parser.add_argument(
        "--json-out",
        default="/tmp/stock_statement_geometry_ocr.json",
    )
    parser.add_argument(
        "--txt-out",
        default="/tmp/stock_statement_geometry_ocr.txt",
    )
    parser.add_argument(
        "--debug-png",
        default="/tmp/stock_grid_debug.png",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    path = Path(args.file)
    if not path.is_file():
        print(f"ERROR: missing {path}", file=sys.stderr)
        return 2

    from services.stock_geometry_ocr import (
        run_geometry_ocr_benchmark,
        write_geometry_reports,
    )

    report = run_geometry_ocr_benchmark(
        path,
        debug_png=args.debug_png,
        run_gemini_fallback=not args.no_gemini_fallback,
    )
    jp, tp = write_geometry_reports(
        report, json_path=args.json_out, txt_path=args.txt_out
    )
    print(json.dumps(report.get("metrics"), indent=2))
    print("hybrid:", json.dumps(report.get("hybrid_metrics"), indent=2, default=str)[:800])
    print("four-way:", json.dumps(report.get("comparison_with_prior_approaches"), indent=2, default=str)[:1200])
    print(f"wrote {jp}")
    print(f"wrote {tp}")
    print(f"wrote {args.debug_png}")
    print("production_switched=", report.get("production_switched"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
