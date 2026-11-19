"""Force-skip LstSL rescue and prefer Vision Table for diagnosis."""
from pathlib import Path
import logging
from unittest.mock import patch

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
load_dotenv()

from services.sales_statement_extractor import extract_sales_statement

path = Path(r"C:\Users\adity\Downloads\1000343838.png")


def _no_lstsl(*_a, **_k):
    return None


with patch(
    "services.sales_statement_extractor._extract_lstsl_open_recd_vision",
    side_effect=_no_lstsl,
):
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
    "totals sv",
    (result.get("totals") or {}).get("sales_value"),
    "cv",
    (result.get("totals") or {}).get("closing_value"),
)
want = {
    "BLEMINOR": (90, 0, 5, 85),
    "DIABECON DS": (58, 50, 8, 99),
    "LIV 52 SYP 100": (28, 3150, 2442, 736),
    "PILEX TAB": (224, 0, 57, 167),
    "PILEK": (224, 0, 57, 167),
}
for it in items:
    name = str(it.get("product_name") or "")
    short = name[:42]
    print(
        f"{short:42} op={it.get('opening_qty')} rc={it.get('receipts_qty')} "
        f"is={it.get('sales_qty')} cq={it.get('closing_qty')} "
        f"ov={it.get('opening_value')} iv={it.get('sales_value')} "
        f"cv={it.get('closing_value')} pack={it.get('packing')!r}"
    )
print("--- key checks ---")
for it in items:
    name = str(it.get("product_name") or "").upper()
    for key, (o, r, s, c) in want.items():
        if key in name:
            print(
                f"MATCH {key}: got op={it.get('opening_qty')} rc={it.get('receipts_qty')} "
                f"is={it.get('sales_qty')} cq={it.get('closing_qty')} "
                f"iv={it.get('sales_value')} expect {o}/{r}/{s}/{c}"
            )
