import json
import os
import sys

sys.path.insert(0, r"d:\gst\splitextract")
os.chdir(r"d:\gst\splitextract")

from services.sales_statement_extractor import extract_sales_statement

path = r"C:\Users\adity\Downloads\43.jpg"
raw = open(path, "rb").read()
r = extract_sales_statement(raw, "43.jpg")
items = r.get("line_items") or []
print("N", len(items))
for i, it in enumerate(items):
    name = it.get("product_name") or ""
    print(
        f"{i:02d} name={name!r} pack={it.get('packing')!r} "
        f"op={it.get('opening_qty')} pq={it.get('purchase_qty') or it.get('receipts_qty')} "
        f"sq={it.get('sales_qty')} cq={it.get('closing_qty')} "
        f"sv={it.get('sales_value')} cv={it.get('closing_value')} "
        f"ov={it.get('opening_value')}"
    )
meta = {
    k: r.get(k)
    for k in (
        "company_name",
        "stockist_name",
        "address",
        "period_from",
        "period_to",
        "report_title",
    )
}
print("meta", json.dumps(meta, default=str))
tot = r.get("totals") or {}
extra = tot.get("extra") if isinstance(tot.get("extra"), dict) else {}
print(
    "totals",
    json.dumps(
        {
            "sales_value": tot.get("sales_value"),
            "closing_value": tot.get("closing_value"),
            "method": extra.get("extraction_method"),
            "kind": extra.get("stock_identity_kind"),
            "vision_final": extra.get("vision_table_final"),
        },
        default=str,
    ),
)
slim = {
    "meta": meta,
    "totals": {
        "sales_value": tot.get("sales_value"),
        "closing_value": tot.get("closing_value"),
        "extra": {
            k: extra.get(k)
            for k in (
                "extraction_method",
                "stock_identity_kind",
                "vision_table_final",
                "printed_total_row_source",
            )
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
            "closing_qty": it.get("closing_qty"),
            "opening_value": it.get("opening_value"),
            "sales_value": it.get("sales_value"),
            "closing_value": it.get("closing_value"),
        }
        for it in items
    ],
}
open(r"d:\gst\splitextract\_tmp_43_slim.json", "w", encoding="utf-8").write(
    json.dumps(slim, indent=2, default=str)
)
print("wrote slim")
