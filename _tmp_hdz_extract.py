"""Reproduce StockHDZ extraction and dump definitive JSON for comparison."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
load_dotenv()

from services.sales_statement_extractor import extract_sales_statement
from services.stock_native_resolver import apply_lms_nearexp_pdf_fallback

path = Path(r"C:\Users\amnsa\Downloads\StockHDZ (2).Pdf")
file_bytes = path.read_bytes()
result = extract_sales_statement(file_bytes, path.name)
# Mirror API boundary repair.
result = apply_lms_nearexp_pdf_fallback(result, file_bytes=file_bytes)

items = result.get("line_items") or []
extra = ((result.get("totals") or {}).get("extra") or {})
totals = result.get("totals") or {}
near = (
    result.get("near_expiry_details")
    or totals.get("near_expiry_details")
    or extra.get("near_expiry_details")
    or []
)

print("n", len(items))
print("method", extra.get("extraction_method"), "fallback", extra.get("lms_nearexp_fallback"))
print(
    "totals sales_value",
    totals.get("sales_value"),
    "closing_value",
    totals.get("closing_value"),
    "lms_value",
    totals.get("lms_value") or extra.get("lms_value"),
)
print("near_expiry_details", len(near))
for it in items:
    name = str(it.get("product_name") or "")
    ex = it.get("extra") if isinstance(it.get("extra"), dict) else {}
    if "PILEX FORTE TAB" in name.upper():
        print(
            "PILEX",
            "ne",
            ex.get("near_expiry_qty"),
            "batches",
            len(ex.get("near_expiry_batches") or []),
            "sv",
            it.get("sales_value"),
            "cv",
            it.get("closing_value"),
        )
    if "DIABECON DS" in name.upper():
        print(
            "DIABECON",
            "ne",
            ex.get("near_expiry_qty"),
            "detail",
            ex.get("near_expiry_detail_qty"),
            "disc",
            ex.get("near_expiry_qty_discrepancy"),
            "batches",
            len(ex.get("near_expiry_batches") or []),
        )

out = Path("_tmp_hdz_result.json")
out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
print("wrote", out.resolve())
assert totals.get("sales_value") == 250325.29, totals.get("sales_value")
assert totals.get("closing_value") == 179233.42, totals.get("closing_value")
assert len(near) == 6, len(near)
pilex = next(
    i
    for i in items
    if "PILEX FORTE TAB" in str(i.get("product_name") or "").upper()
)
assert (pilex.get("extra") or {}).get("near_expiry_qty") == 3.0
diabecon = next(
    i
    for i in items
    if "DIABECON DS" in str(i.get("product_name") or "").upper()
)
dex = diabecon.get("extra") or {}
assert dex.get("near_expiry_qty") == 4.0
assert dex.get("near_expiry_detail_qty") == 11.0
assert dex.get("near_expiry_qty_discrepancy") is True
assert len(dex.get("near_expiry_batches") or []) == 2
print("ALL ASSERTS PASSED")
