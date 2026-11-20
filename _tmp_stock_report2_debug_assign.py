import io
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

import pdfplumber
from services.stock_native_tables import from_pdf_words, resolver_ready
from services.stock_header_resolver import assign_cells
from services.stock_native_resolver import (
    build_line_items_from_tokens,
    fields_to_line_item,
)

path = Path(r"C:\Users\adity\Downloads\Stock_Report (2).PDF")
data = path.read_bytes()
with pdfplumber.open(io.BytesIO(data)) as pdf:
    built = from_pdf_words(pdf)

page_results = built.get("page_results") or []
print("n_pages_built", len(page_results))
page = page_results[0] if page_results else {
    "header_cells": built.get("header_cells"),
    "data_rows": built.get("data_rows"),
}
header = page.get("header_cells") or []
ready = resolver_ready(header)
cols = ready.get("columns") or []
print("resolved", ready.get("resolved"), "ncols", len(cols))
for c in cols:
    print(
        " ",
        c.get("canonical"),
        "|",
        (c.get("label") or c.get("text") or "")[:20],
        "x=",
        round(float(c.get("x_center") or c.get("x0") or 0), 1),
        c.get("reason"),
    )

rows = page.get("data_rows") or []
print("data_rows", len(rows))
items = build_line_items_from_tokens(cols, rows)
print("built items", len(items))
for it in items:
    print(
        f"{str(it.get('product_name'))[:40]:40} "
        f"op={it.get('opening_qty')} rc={it.get('receipts_qty')} "
        f"is={it.get('sales_qty')} cq={it.get('closing_qty')} "
        f"iv={it.get('sales_value')} cv={it.get('closing_value')} "
        f"pack={it.get('packing')!r}"
    )

print("\n--- raw assign sample ---")
for i, tokens in enumerate(rows[:8]):
    texts = [(t.get("text"), round(float(t.get("x_center") or 0), 1)) for t in tokens]
    assigned = assign_cells(tokens, cols)
    fields = assigned.get("fields") or {}
    item = fields_to_line_item(fields)
    print(i, texts)
    print(
        "  ->",
        item.get("product_name"),
        item.get("opening_qty"),
        item.get("receipts_qty"),
        item.get("sales_qty"),
        item.get("sales_value"),
        item.get("closing_qty"),
        item.get("closing_value"),
        "pack",
        item.get("packing"),
    )
    # field sources / assignment detail
    for k, v in (fields or {}).items():
        if v not in (None, "", [], {}):
            print("    field", k, "=", v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items() if kk in ('text','value','canonical')})
