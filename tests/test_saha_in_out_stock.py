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

DEEPAK_HEADER = """
SHRI DEEPAK PHARMA
Sales & Stock Statement Company [Summary] From : 01/09/26 To 30/09/26
NO PRODUCT / COMPANY OP QTY OP AMT IN QTY IN AMT OUT QTY OUT AMT CL QTY CL AMT EXP DATE
"""

SUMMARY_RTL = """
COMPANY WISE SUMMARY RTL
SALES & STOCK STATEMENT
OP QTY OP AMT SALE AMT CLOSING CLOSING AMT
"""

DEEPAK = Path("/var/www/html/splitextract/Deepak Sept26.pdf")


class TestSahaInOutStock(unittest.TestCase):
    def test_header_is_not_summary_rtl(self):
        self.assertTrue(_is_saha_in_out_stock_text(HEADER))
        self.assertTrue(_is_saha_in_out_stock_text(DEEPAK_HEADER))
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


@unittest.skipUnless(DEEPAK.is_file(), "missing Deepak Company [Summary] PDF")
class TestDeepakCompanySummaryFullWidth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(DEEPAK.read_bytes(), DEEPAK.name)

    def test_extracts_all_printed_products(self):
        extra = (self.result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "saha_in_out_stock")
        self.assertEqual(extra.get("layout"), "company_summary_full_width")
        items = self.result.get("line_items") or []
        # Printed TOTAL / GRAND TOTAL : 109
        self.assertEqual(len(items), 109)
        self.assertEqual(self.result.get("stockist_name"), "SHRI DEEPAK PHARMA")
        self.assertEqual(self.result.get("period_from"), "2026-09-01")
        self.assertEqual(self.result.get("period_to"), "2026-09-30")

    def test_sample_rows_and_last_before_total(self):
        items = self.result.get("line_items") or []
        by_code = {str(i.get("product_code")): i for i in items}
        abana = by_code["2153"]
        self.assertEqual(abana["product_name"], "ABANA TABLETS")
        self.assertEqual(abana["opening_qty"], 165.0)
        self.assertEqual(abana["extra"]["opening_value"], 24457.72)
        self.assertEqual(abana["receipts_qty"], 100.0)
        self.assertEqual(abana["receipts_value"], 14860.00)
        self.assertEqual(abana["sales_qty"], 44.0)
        self.assertEqual(abana["sales_value"], 7375.28)
        self.assertEqual(abana["closing_qty"], 221.0)
        self.assertEqual(abana["closing_value"], 32758.52)
        last = items[-1]
        self.assertEqual(last["product_code"], "2353")
        self.assertIn("YASHTIMADHU", str(last.get("product_name") or "").upper())
        names = [str(i.get("product_name") or "").upper() for i in items]
        self.assertFalse(any(n.startswith("TOTAL") or "GRAND TOTAL" in n for n in names))


if __name__ == "__main__":
    unittest.main()
