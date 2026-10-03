#!/usr/bin/env python3
"""CLI: isolated stock OCR accuracy benchmark (does NOT change production).

Examples:
  python benchmark_stock_ocr.py --file 0000730099_....png --provider=mistral
  python benchmark_stock_ocr.py --file 0000730099_....png --provider=both
  python benchmark_stock_ocr.py --file 0000730099_....png --provider=gemini

Requires MISTRAL_API_KEY for --provider=mistral|both.
Gemini uses the existing Vertex/Gemini path (may hit 429 under load).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Ensure project root on path when run as script.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark stock OCR providers (isolated).")
    parser.add_argument(
        "--file",
        required=True,
        help="Path to stock statement image/PDF",
    )
    parser.add_argument(
        "--provider",
        default="mistral",
        choices=("mistral", "gemini", "both"),
        help="OCR provider(s) to run",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Optional JSON output path",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Less logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    path = Path(args.file)
    if not path.is_file():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 2

    if args.provider in {"mistral", "both"} and not (
        os.getenv("MISTRAL_API_KEY") or ""
    ).strip():
        print(
            "ERROR: MISTRAL_API_KEY is not set. Export it to run the Mistral benchmark.",
            file=sys.stderr,
        )
        if args.provider == "mistral":
            return 3

    providers = (
        ["mistral", "gemini"] if args.provider == "both" else [args.provider]
    )
    # Skip mistral if key missing when both
    if args.provider == "both" and not (os.getenv("MISTRAL_API_KEY") or "").strip():
        providers = ["gemini"]
        print("WARN: MISTRAL_API_KEY missing — running gemini only.", file=sys.stderr)

    from services.stock_ocr_benchmark import (
        format_comparison_table,
        run_benchmark,
        write_benchmark_reports,
    )

    report = run_benchmark(path, providers=providers)
    print(format_comparison_table(report))
    print()
    for result in report.get("results") or []:
        cmp_ = result.get("comparison") or {}
        print(
            f"=== {result.get('provider')} status={result.get('status')} "
            f"error={result.get('error')} strategy={result.get('strategy')} "
            f"latency_ms={result.get('provider_latency_ms')} ==="
        )
        print(
            f"cell_accuracy={cmp_.get('cell_accuracy_pct')}% "
            f"row_accuracy={cmp_.get('row_accuracy_pct')}% "
            f"recon_pass={result.get('reconciliation_summary')} "
            f"shifts={cmp_.get('column_shift_count')} "
            f"arith_mismatch={cmp_.get('arithmetic_mismatch_count')}"
        )
        print("per_field=", json.dumps(cmp_.get("per_field_accuracy_pct"), default=str))
        for row in result.get("rows") or result.get("rows_preview") or []:
            print(json.dumps(row, ensure_ascii=False, default=str))

    print("\ncomparison=", json.dumps(report.get("comparison"), indent=2, default=str))
    print("\nproduction_switched=", report.get("production_switched"))

    json_default = "/tmp/stock_statement_mistral_vs_gemini.json"
    txt_default = "/tmp/stock_statement_mistral_vs_gemini.txt"
    out_json = args.out or json_default
    json_p, txt_p = write_benchmark_reports(
        report, json_path=out_json, txt_path=txt_default
    )
    print(f"wrote {json_p}")
    print(f"wrote {txt_p}")

    # Non-zero if any provider hard-errored with no rows — still useful as report.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
