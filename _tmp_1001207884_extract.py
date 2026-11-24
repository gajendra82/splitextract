from pathlib import Path
import logging

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
load_dotenv()

from services.sales_statement_extractor import extract_sales_statement

path = Path(r"C:\Users\adity\Downloads\1001207884.jpg")
result = extract_sales_statement(path.read_bytes(), path.name)
items = result.get("line_items") or []
extra = (result.get("totals") or {}).get("extra") or {}
print(
    "parser",
    result.get("parser"),
    "method",
    result.get("method"),
    "N",
    len(items),
)
print("stockist", result.get("stockist_name"), "company", result.get("company_name"))
print("period", result.get("period_from"), result.get("period_to"))
print(
    "early",
    extra.get("early_vision_reason"),
    "extraction_method",
    extra.get("extraction_method"),
    "layout",
    extra.get("layout"),
)
print(
    "valid_ratio",
    extra.get("valid_ratio"),
    "row_status",
    extra.get("row_status_counts"),
)
print(
    "totals sv",
    (result.get("totals") or {}).get("sales_value"),
    "cv",
    (result.get("totals") or {}).get("closing_value"),
)
for it in items:
    name = str(it.get("product_name") or "")[:42]
    flags = (it.get("extra") or {}).get("flags")
    print(
        f"{name:42} op={it.get('opening_qty')} rc={it.get('receipts_qty')} "
        f"is={it.get('sales_qty')} cq={it.get('closing_qty')} "
        f"ov={it.get('opening_value')} iv={it.get('sales_value')} "
        f"cv={it.get('closing_value')} pack={it.get('packing')!r} flags={flags}"
    )
print("... total", len(items))
