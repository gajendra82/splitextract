from pathlib import Path
import logging

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
load_dotenv()

from services.sales_statement_extractor import extract_sales_statement

path = Path(
    r"C:\Users\adity\Downloads\c5545a70-17eb-4250-9554-b2c30ece2d5d7882101328845136430.jpg"
)
result = extract_sales_statement(path.read_bytes(), path.name)
items = result.get("line_items") or []
extra = (result.get("totals") or {}).get("extra") or {}
print(
    "method",
    result.get("method"),
    "parser",
    result.get("parser"),
    "N",
    len(items),
    "gemini",
    extra.get("gemini_calls"),
)
print("stockist", result.get("stockist_name"), "company", result.get("company_name"))
print("valid_ratio", extra.get("valid_ratio"), "row_status", extra.get("row_status_counts"))
for it in items[:20]:
    name = str(it.get("product_name") or "")[:42]
    flags = (it.get("extra") or {}).get("flags")
    print(
        f"{name:42} op={it.get('opening_qty')} rc={it.get('receipts_qty')} "
        f"is={it.get('sales_qty')} cq={it.get('closing_qty')} "
        f"ov={it.get('opening_value')} iv={it.get('sales_value')} "
        f"cv={it.get('closing_value')} flags={flags}"
    )
print("... total", len(items))
