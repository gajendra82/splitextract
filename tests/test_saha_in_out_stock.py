"""Sales && Stock Company [Summary]. OP AMT is opening, OUT AMT is sales."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_saha_in_out_stock_text,
    _is_summary_rtl_statement,
    extract_sales_statement,
)


SAHA = Path(
    r"C:\Users\adity\Downloads\ZL_2026_August\0000734330_2026_08_ZL_04_347_04092026042402.pdf"
)

HEADER = """
NEW SAHA MEDICAL AGENCY
Sales && Stock Statement Company [Summary] From : 01/08/26 To 31/08/26
NO PRODUCT / COMPANY OP QTY OP AMT IN QTY IN AMT OUT QTY
"""

SUMMARY_RTL = """
COMPANY WISE SUMMARY RTL
SALES & STOCK STATEMENT
OP QTY OP AMT SALE AMT CLOSING CLOSING AMT
"""


class TestSahaInOutStock(unittest.TestCase):
    def test_header_is_not_summary_rtl(self):
        self.assertTrue(_is_saha_in_out_stock_text(HEADER))
        self.assertFalse(_is_summary_rtl_statement(HEADER))
        self.assertFalse(_is_saha_in_out_stock_text(SUMMARY_RTL))

    @unittest.skipUnless(SAHA.is_file(), "missing Saha stock statement")
    def test_opening_amount_is_not_sales_value(self):
        result = extract_sales_statement(SAHA.read_bytes(), SAHA.name)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "saha_in_out_stock")
        items = result.get("line_items") or []
        self.assertEqual(len(items), 59)
        soap = next(item for item in items if item["product_code"] == "5463")
        self.assertEqual(soap["opening_qty"], 50)
        self.assertEqual(soap["extra"]["opening_value"], 3309.50)
        self.assertEqual(soap["sales_qty"], 5)
        self.assertEqual(soap["sales_value"], 372.32)
        self.assertEqual(soap["closing_qty"], 45)
        self.assertEqual(soap["closing_value"], 2978.55)
        self.assertNotEqual(soap["sales_value"], soap["extra"]["opening_value"])
        failed = 0
        for item in items:
            expected = item["opening_qty"] + item["receipts_qty"] - item["sales_qty"]
            if abs(expected - item["closing_qty"]) > 0.05:
                failed += 1
        self.assertEqual(failed, 0)
        self.assertEqual(result["totals"]["sales_value"], 158810.67)
        self.assertEqual(extra.get("opening_value"), 334264.30)
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")


if __name__ == "__main__":
    unittest.main()
