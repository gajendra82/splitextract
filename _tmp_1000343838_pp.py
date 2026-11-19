"""Try particular-packing reader with broadened/no OCR gate."""
from pathlib import Path
import logging
from unittest.mock import patch

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
load_dotenv()

from services import sales_statement_extractor as s

path = Path(r"C:\Users\adity\Downloads\1000343838.png")
data = path.read_bytes()

fake_header = """
SURESH MEDICAL HALL
JHANJHARPUR BAZAR, MADHUBANI, JHANJHARPUR
STOCK REPORT
01/10/26 To :28/10/26
For HIMALAYA DRUG - LIV SYP
"""

# Patch text gate to accept LIV SYP + fake header OCR
with patch.object(
    s, "_particular_packing_header_text", return_value=fake_header
), patch.object(
    s,
    "_particular_packing_stock_report_text",
    side_effect=lambda text: bool(
        text
        and "STOCK REPORT" in text.upper()
        and ("ZEAL" in text.upper() or "HIMALAYA DRUG" in text.upper())
    ),
):
    result = s._extract_particular_packing_stock_report(data, path.name, ".png")

if not result:
    print("NO RESULT")
else:
    items = result.get("line_items") or []
    extra = (result.get("totals") or {}).get("extra") or {}
    print("N", len(items), "method", extra.get("extraction_method"))
    print("stockist", result.get("stockist_name"), "company", result.get("company_name"))
    print(
        "totals",
        (result.get("totals") or {}).get("sales_value"),
        (result.get("totals") or {}).get("closing_value"),
    )
    for it in items:
        name = str(it.get("product_name") or "")[:40]
        print(
            f"{name:40} op={it.get('opening_qty')} rc={it.get('receipts_qty')} "
            f"is={it.get('sales_qty')} cq={it.get('closing_qty')} "
            f"ov={it.get('opening_value')} iv={it.get('sales_value')} "
            f"cv={it.get('closing_value')} pack={it.get('packing')!r}"
        )
