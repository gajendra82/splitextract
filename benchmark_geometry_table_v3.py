#!/usr/bin/env python3
"""CLI: geometry table v3 benchmark (Excel Op.Stk layout, no production changes)."""

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
    p.add_argument("--file", required=True)
    p.add_argument("--no-gemini", action="store_true")
    p.add_argument("--max-gemini-cells", type=int, default=24)
    p.add_argument("--json-out", default="/tmp/stock_geometry_table_v3.json")
    p.add_argument("--txt-out", default="/tmp/stock_geometry_table_v3.txt")
    p.add_argument("--debug-png", default="/tmp/stock_geometry_table_v3_debug.png")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    path = Path(args.file)
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
    print(json.dumps(report.get("metrics"), indent=2, default=str))
    print("perf", json.dumps(report.get("performance"), indent=2, default=str))
    print("gemini", json.dumps(report.get("gemini"), indent=2, default=str))
    print("geometry", json.dumps({
        "n_row_bands": (report.get("geometry") or {}).get("n_row_bands"),
        "n_col_lines": (report.get("geometry") or {}).get("n_col_lines"),
        "col_xs": (report.get("geometry") or {}).get("col_xs"),
        "header_ri": (report.get("geometry") or {}).get("header_ri"),
        "header_source": (report.get("geometry") or {}).get("header_source"),
    }, indent=2))
    print("total_detected_rows", report.get("total_detected_rows"))
    for f in report.get("focus") or []:
        print(json.dumps(f, default=str))
    print(f"wrote {jp}\nwrote {tp}\nwrote {args.debug_png}")
    print("production_switched=", report.get("production_switched"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
