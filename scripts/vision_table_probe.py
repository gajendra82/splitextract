#!/usr/bin/env python3
"""Manual probe for the schema-agnostic Vision table reader (Phase 2b).

Never imported by the app. Does not change production behaviour.

Usage
-----
  python scripts/vision_table_probe.py <file.jpg|png|pdf>
  python scripts/vision_table_probe.py sheet.jpg --live
  python scripts/vision_table_probe.py sheet.jpg --live --save op_pur_demo
  python scripts/vision_table_probe.py sheet.jpg --live --compare
  python scripts/vision_table_probe.py --replay rollout_5_….jpg
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _load_images(path: Path) -> List[bytes]:
    data = path.read_bytes()
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}:
        return [data]
    if ext == ".pdf":
        from services.stock_vision_table import render_pdf_pages

        images = render_pdf_pages(data, zoom=2.5)
        if not images:
            _die(f"Could not render PDF pages from {path}")
        return images
    _die(f"Unsupported file type: {ext}")


def _print_map(mapped: Dict[str, Any]) -> None:
    print("\n=== column_map ===")
    for col in mapped.get("column_map") or []:
        print(
            f"  col={col.get('col_index')} {col.get('header_text')!r} -> "
            f"{col.get('canonical')} conf={col.get('confidence')} "
            f"source={col.get('source')}"
        )
    if mapped.get("errors"):
        print("errors:", ", ".join(str(e) for e in mapped["errors"]))
    print(
        f"tables_found={mapped.get('tables_found')} "
        f"rows_per_table={mapped.get('rows_per_table')} "
        f"coercions={json.dumps(mapped.get('coercions') or {})} "
        f"split_events={mapped.get('split_events')}"
    )
    print("\n=== rows ===")
    for item in mapped.get("line_items") or []:
        extra = item.get("extra") or {}
        flags = extra.get("flags") or []
        print(
            f"  [{extra.get('vision_row_index')}] t={extra.get('table_index')} "
            f"{item.get('product_name')}: "
            f"op={item.get('opening_qty')} pur={item.get('receipts_qty')} "
            f"sale={item.get('sales_qty')} cls={item.get('closing_qty')}"
            + (f" flags={flags}" if flags else "")
        )


def _replay(name: str) -> int:
    """Re-run mapping on a saved response with zero Gemini calls."""
    from services.stock_row_classifier import classify_result, classify_row, read_row_fields
    from services.stock_vision_table import map_vision_table

    resp_dir = (
        Path(__file__).resolve().parents[1] / "tests" / "data" / "vision_responses"
    )
    path = resp_dir / f"{name}.json"
    if not path.is_file():
        # Allow bare filename or stem already including .json
        alt = resp_dir / name
        if alt.is_file():
            path = alt
        else:
            matches = sorted(resp_dir.glob(f"*{name}*.json"))
            matches = [m for m in matches if "handcheck" not in str(m)]
            if len(matches) == 1:
                path = matches[0]
            else:
                _die(f"Saved response not found for --replay {name!r} under {resp_dir}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    mapped = map_vision_table(raw, request_id="replay")
    _print_map(mapped)
    summary = classify_result({"line_items": mapped.get("line_items") or []})
    print("\n=== classify_result ===")
    print("counts:", json.dumps(summary.get("row_status_counts") or {}))
    print("valid_ratio:", summary.get("valid_ratio"))
    print("gemini_calls=0 (replay)")
    for item in mapped.get("line_items") or []:
        status, _info = classify_row(read_row_fields(item))
        print(
            f"  status={status.value} {item.get('product_name')}: "
            f"op={item.get('opening_qty')} pur={item.get('receipts_qty')} "
            f"sale={item.get('sales_qty')} cls={item.get('closing_qty')}"
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", help="Image or PDF path")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually call Gemini (otherwise refuse).",
    )
    parser.add_argument(
        "--save",
        metavar="NAME",
        help="Write raw Gemini JSON to tests/data/vision_responses/NAME.json",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Also run extract_sales_statement (may make more Gemini calls).",
    )
    parser.add_argument(
        "--replay",
        metavar="NAME",
        help="Re-map a saved vision_responses JSON with no Gemini call.",
    )
    args = parser.parse_args(argv)

    if args.replay:
        return _replay(args.replay)

    if not args.file:
        parser.print_help()
        return 2

    path = Path(args.file)
    if not path.is_file():
        _die(f"File not found: {path}")

    if not args.live:
        print(
            "Refusing to call Gemini without --live.\n"
            f"  python scripts/vision_table_probe.py {path} --live\n"
            f"  python scripts/vision_table_probe.py {path} --live --save NAME\n"
            f"  python scripts/vision_table_probe.py {path} --live --compare\n"
            f"  python scripts/vision_table_probe.py --replay NAME"
        )
        return 2

    from services.stock_row_classifier import (
        classify_result,
        classify_row,
        read_row_fields,
    )
    from services.stock_vision_table import (
        extract_stock_table_vision,
        map_vision_table,
    )

    images = _load_images(path)
    call_count = 0
    hard_stop = 3
    pages_per = 4
    try:
        from services.stock_vision_table import (
            _needs_vertical_split,
            _pages_per_call,
            _vertical_halves,
        )

        pages_per = _pages_per_call()
        if len(images) == 1 and _needs_vertical_split(images[0]):
            halves = _vertical_halves(images[0])
            if len(halves) == 2:
                images = halves
                pages_per = 1
                print(
                    f"Tall image: vertical split into {len(images)} crops "
                    f"(pages_per={pages_per})"
                )
    except Exception:
        pass

    all_items: List[Dict[str, Any]] = []
    carry = None
    last_raw: Optional[Dict[str, Any]] = None
    last_mapped: Optional[Dict[str, Any]] = None
    for start in range(0, len(images), pages_per):
        if call_count >= hard_stop:
            print(f"Hard stop: Gemini calls reached {hard_stop}")
            break
        batch = images[start : start + pages_per]
        raw = extract_stock_table_vision(batch, {"label": "vision_table_probe"})
        call_count += 1
        last_raw = raw
        if raw.get("error"):
            print("Vision error:", raw.get("error"))
            if raw.get("truncated"):
                print("truncated=true finish_reason=", raw.get("finish_reason"))
            print("raw:", (raw.get("raw") or "")[:500])
            break
        mapped = map_vision_table(raw, carry_header=carry, request_id="probe")
        last_mapped = mapped
        if mapped.get("header_used"):
            carry = mapped["header_used"]
        _print_map(mapped)
        all_items.extend(mapped.get("line_items") or [])

    result_for_classify = {"line_items": all_items, "totals": {"extra": {}}}
    summary = classify_result(result_for_classify)
    print("\n=== classify_result ===")
    print("counts:", json.dumps(summary.get("row_status_counts") or {}))
    print("valid_ratio:", summary.get("valid_ratio"))
    print(f"gemini_calls={call_count} (hard stop {hard_stop})")
    if last_mapped is not None:
        print(
            f"tables_found={last_mapped.get('tables_found')} "
            f"coercions={json.dumps(last_mapped.get('coercions') or {})} "
            f"split_events={last_mapped.get('split_events')}"
        )

    if args.save and last_raw is not None and not last_raw.get("error"):
        out_dir = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "data"
            / "vision_responses"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{args.save}.json"
        out_path.write_text(json.dumps(last_raw, indent=2), encoding="utf-8")
        print(f"Saved raw Gemini JSON -> {out_path}")

    if args.compare:
        print(
            "\nWARNING: --compare runs extract_sales_statement and may make "
            "additional Gemini calls through the current pipeline."
        )
        from services.sales_statement_extractor import extract_sales_statement

        current = extract_sales_statement(path.read_bytes(), path.name)
        cur_items = current.get("line_items") or []
        print("\n=== compare (current vs vision) ===")
        if last_mapped is not None:
            print(
                f"vision tables_found={last_mapped.get('tables_found')} "
                f"rows_per_table={last_mapped.get('rows_per_table')} "
                f"coercions={json.dumps(last_mapped.get('coercions') or {})} "
                f"split_events={last_mapped.get('split_events')}"
            )
        print(
            f"{'product':40} {'cur_op':>7} {'vis_op':>7} "
            f"{'cur_pur':>7} {'vis_pur':>7} {'cur_sl':>7} {'vis_sl':>7} "
            f"{'cur_cl':>7} {'vis_cl':>7} {'status':>12}"
        )
        n = max(len(cur_items), len(all_items))
        for i in range(n):
            c = cur_items[i] if i < len(cur_items) else {}
            v = all_items[i] if i < len(all_items) else {}
            name = str(c.get("product_name") or v.get("product_name") or "")[:40]

            def _fmt(val: Any) -> str:
                if val is None:
                    return f"{'-':>7}"
                try:
                    return f"{float(val):>7.1f}"
                except (TypeError, ValueError):
                    return f"{str(val)[:7]:>7}"

            status = "-"
            if v:
                st, _ = classify_row(read_row_fields(v))
                status = st.value

            print(
                f"{name:40} "
                f"{_fmt(c.get('opening_qty'))} {_fmt(v.get('opening_qty'))} "
                f"{_fmt(c.get('receipts_qty'))} {_fmt(v.get('receipts_qty'))} "
                f"{_fmt(c.get('sales_qty'))} {_fmt(v.get('sales_qty'))} "
                f"{_fmt(c.get('closing_qty'))} {_fmt(v.get('closing_qty'))} "
                f"{status:>12}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
