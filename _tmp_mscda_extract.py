import json
import os
import sys

sys.path.insert(0, r"d:\gst\splitextract")
os.chdir(r"d:\gst\splitextract")

from services.sales_statement_extractor import extract_sales_statement

path = r"C:\Users\adity\Downloads\MSCDA OCT 26.pdf"
raw = open(path, "rb").read()
r = extract_sales_statement(raw, "MSCDA OCT 26.pdf")
items = r.get("line_items") or []
tot = r.get("totals") or {}
extra = tot.get("extra") if isinstance(tot.get("extra"), dict) else {}
print("N", len(items))
print(
    "totals",
    json.dumps(
        {
            "sales_value": tot.get("sales_value"),
            "closing_value": tot.get("closing_value"),
            "method": extra.get("extraction_method"),
            "kind": extra.get("stock_identity_kind"),
            "layout": extra.get("layout"),
            "vision_final": extra.get("vision_table_final"),
        },
        default=str,
    ),
)
for i, it in enumerate(items):
    ex = it.get("extra") if isinstance(it.get("extra"), dict) else {}
    print(
        f"{i:02d} name={it.get('product_name')!r} pack={it.get('packing')!r} "
        f"op={it.get('opening_qty')} pq={it.get('purchase_qty') or it.get('receipts_qty')} "
        f"sq={it.get('sales_qty')} sv={it.get('sales_value')} "
        f"cq={it.get('closing_qty')} cv={it.get('closing_value')} "
        f"ov={it.get('opening_value') or ex.get('opening_value')}"
    )
slim = {
    "totals": {
        "sales_value": tot.get("sales_value"),
        "closing_value": tot.get("closing_value"),
        "extra": {
            k: extra.get(k)
            for k in ("extraction_method", "stock_identity_kind", "layout", "vision_table_final")
        },
    },
    "line_items": [
        {
            "product_name": it.get("product_name"),
            "packing": it.get("packing"),
            "opening_qty": it.get("opening_qty"),
            "receipts_qty": it.get("receipts_qty"),
            "purchase_qty": it.get("purchase_qty"),
            "sales_qty": it.get("sales_qty"),
            "sales_value": it.get("sales_value"),
            "closing_qty": it.get("closing_qty"),
            "closing_value": it.get("closing_value"),
        }
        for it in items
    ],
}
from pathlib import Path

Path("_tmp_mscda_slim.json").write_text(json.dumps(slim, indent=2), encoding="utf-8")
print("wrote slim")
