#!/usr/bin/env python3
"""CLI: geometry hybrid v3 benchmark (blue-mark robust, no production changes)."""

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
    p.add_argument("--json-out", default="/tmp/stock_geometry_hybrid_v3.json")
    p.add_argument("--txt-out", default="/tmp/stock_geometry_hybrid_v3.txt")
    p.add_argument("--debug-png", default="/tmp/stock_grid_debug_v3.png")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    path = Path(args.file)
    if not path.is_file():
        print(f"ERROR: {path}", file=sys.stderr)
        return 2

    from services.stock_geometry_hybrid_v3 import run_hybrid_v3, write_v3_reports

    report = run_hybrid_v3(
        path, debug_png=args.debug_png, enable_gemini=not args.no_gemini
    )
    jp, tp = write_v3_reports(report, json_path=args.json_out, txt_path=args.txt_out)
    print(json.dumps(report.get("metrics"), indent=2, default=str))
    print("perf", json.dumps(report.get("performance"), indent=2, default=str))
    print(
        "gemini",
        json.dumps(
            {
                k: report.get("gemini", {}).get(k)
                for k in ("api_calls", "latency_ms", "cells_applied", "error")
            }
        ),
    )
    for f in report.get("focus") or []:
        print(
            json.dumps(
                {k: f.get(k) for k in f if k != "cells"},
                default=str,
            )
        )
    print(f"wrote {jp}\nwrote {tp}\nwrote {args.debug_png}")
    print("production_switched=", report.get("production_switched"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
