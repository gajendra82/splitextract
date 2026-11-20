"""Run JR SHAH JPEG alone and print the Class-A verification report."""
from __future__ import annotations

import json
from pathlib import Path

from services.sales_statement_extractor import extract_sales_statement

FIX = Path(__file__).resolve().parent / (
    "0000700077_2026_08_ZL_06_304_06092026141652.jpeg"
)


def main() -> None:
    assert FIX.is_file(), f"missing fixture {FIX}"
    result = extract_sales_statement(FIX.read_bytes(), FIX.name)
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    extra = (result.get("totals") or {}).get("extra") or {}

    print("=== META ===")
    print("method", extra.get("extraction_method"))
    print("period", result.get("period_from"), "->", result.get("period_to"))
    print("stockist", result.get("stockist_name"))
    print("company", result.get("company_name"))
    print("row_count", len(items))

    print("\n=== ROWS (opening/purchase/sales/balance + values) ===")
    identity_fails = 0
    for idx, item in enumerate(items, 1):
        opening = float(item.get("opening_qty") or 0)
        receipts = float(item.get("receipts_qty") or 0)
        sales = float(item.get("sales_qty") or 0)
        closing = float(item.get("closing_qty") or 0)
        ok = abs((opening + receipts - sales) - closing) < 0.01
        ss = float((item.get("extra") or {}).get("sales_scheme_qty") or 0)
        if not ok and ss:
            ok = abs((opening + receipts - sales - ss) - closing) < 0.01
        if not ok and ss == 0:
            delta = (opening + receipts - sales) - closing
            if 1 <= delta <= 5 and abs(delta - round(delta)) < 0.01:
                # SS often omitted; show residual for report clarity.
                pass
        if not ok:
            identity_fails += 1
        pv = (item.get("extra") or {}).get("purchase_value")
        print(
            f"{idx:02d} {item.get('product_name')!s:40s} "
            f"pack={item.get('packing')!s:8s} "
            f"op={opening:g} pur={receipts:g} sale={sales:g} bal={closing:g} "
            f"ss={ss:g} pv={pv} sv={item.get('sales_value')} bv={item.get('closing_value')} "
            f"id={'OK' if ok else 'FAIL'}"
        )

    print("\n=== TOTALS ===")
    print("purchase_value", extra.get("purchase_value"))
    print("sales_value", (result.get("totals") or {}).get("sales_value"))
    print("closing_qty", extra.get("closing_qty"))
    print("closing_value", (result.get("totals") or {}).get("closing_value"))
    print("line_sales_qty_sum", sum(float(i.get("sales_qty") or 0) for i in items))
    print("line_closing_qty_sum", sum(float(i.get("closing_qty") or 0) for i in items))

    print("\n=== STOCK IDENTITY ===")
    print("fail_count", identity_fails)
    print("pass_count", len(items) - identity_fails)

    out = Path(__file__).resolve().parent / "0000700077_extract_report.json"
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print("\nWrote", out)


if __name__ == "__main__":
    main()
