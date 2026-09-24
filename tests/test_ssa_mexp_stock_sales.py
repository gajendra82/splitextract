"""Landscape STOCK & SALES ANALYSIS with DUMP + M.EXP (Arvind)."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_daxinsoft_stock_sales_text,
    _is_ssa_mexp_stock_sales_text,
    _is_ssa_opening_receipt_issue_value_text,
    _is_ved_stock_sales_statement_text,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000736096_2026_08_ZL_24_237_06092026062517.PDF"
)
PRAKASH = (
    Path(__file__).resolve().parents[1]
    / "0000736167_2026_08_ZA_24_8137_03092026173805.pdf"
)

HEADER = (
    "ARVIND MEDICAL AGENCY\n"
    "STOCK & SALES ANALYSIS 01-08-2026 - 31-08-2026\n"
    "ITEM DESCRIPTION  OPENING  RECEIPT  ISSUE  CLOSING  DUMP\n"
    "QTY. VALUE QTY. VALUE QTY. VALUE QTY. VALUE QTY. M.EXP\n"
)

PRAKASH_HEADER = (
    "PRAKASH MEDICAL STORE\n"
    "STOCK & SALES ANALYSIS 01/08/2026 - 31/08/2026\n"
    "ITEM DESCRIPTION  OPENING  RECEIPT  ISSUE  CLOSING  DUMP\n"
    "QTY. VALUE QTY. VALUE QTY. VALUE QTY. VALUE QTY.\n"
)

VED = (
    "Stock and sales Statement from : 01/08/2026 to 31/08/2026\n"
    "Particulars  Pkg.  Open. Qty.  Purch. Qty.  Sales & DC  Close Stock\n"
)


class TestSsaMexpDetection(unittest.TestCase):
    def test_detects_mexp_not_prakash_or_other_layouts(self):
        self.assertTrue(_is_ssa_mexp_stock_sales_text(HEADER))
        self.assertTrue(_is_ssa_opening_receipt_issue_value_text(HEADER))
        self.assertFalse(_is_ssa_mexp_stock_sales_text(PRAKASH_HEADER))
        self.assertTrue(_is_ssa_opening_receipt_issue_value_text(PRAKASH_HEADER))
        self.assertFalse(_is_ssa_mexp_stock_sales_text(VED))
        self.assertFalse(_is_ved_stock_sales_statement_text(HEADER))
        self.assertFalse(_is_daxinsoft_stock_sales_text(HEADER))
        self.assertFalse(_is_ssa_mexp_stock_sales_text(""))


@unittest.skipUnless(FIXTURE.is_file(), "missing Arvind SSA M.EXP fixture")
class TestSsaMexpFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {}
        for item in cls.result["line_items"]:
            key = str(item.get("product_name") or "").upper()
            pack = str(item.get("packing") or "").upper()
            cls.by_name.setdefault(key, item)
            cls.by_name[f"{key}|{pack}"] = item
        cls.names = [
            str(item.get("product_name") or "") for item in cls.result["line_items"]
        ]

    def test_uses_mexp_parser_not_prakash_ssa(self):
        self.assertEqual(self.extra.get("extraction_method"), "ssa_mexp_stock_sales")
        self.assertEqual(self.result["stockist_name"], "ARVIND MEDICAL AGENCY")
        self.assertIn("HIMALAYA", str(self.result.get("company_name") or "").upper())
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertGreaterEqual(len(self.result["line_items"]), 80)
        self.assertNotIn("Invoices", self.result)

    def test_headers_totals_and_company_are_not_products(self):
        joined = " | ".join(self.names).upper()
        self.assertNotIn("TOTAL", joined)
        self.assertNotIn("ITEM DESCRIPTION", joined)
        self.assertFalse(any("HIMALAYA" in name.upper() for name in self.names))

    def test_mexp_rows_are_not_dropped_and_columns_are_not_shifted(self):
        soap = self.by_name["AACTARIL SOAP"]
        self.assertEqual(soap["packing"], "75GM")
        self.assertEqual(soap["opening_qty"], 0.0)
        self.assertEqual(soap["receipts_qty"], 72.0)
        self.assertEqual(soap["sales_qty"], 0.0)
        self.assertEqual(soap["closing_qty"], 72.0)
        self.assertEqual(soap["closing_value"], 5872.60)
        self.assertEqual(soap["sales_value"], 0.0)
        self.assertEqual(soap["extra"]["receipts_value"], 5592.96)
        self.assertEqual(soap["extra"].get("m_exp"), "6/29")

        abana = self.by_name["ABANA TAB"]
        self.assertEqual(abana["packing"], "1X60")
        self.assertEqual(abana["opening_qty"], 137.0)
        self.assertEqual(abana["sales_qty"], 16.0)
        self.assertEqual(abana["sales_value"], 2471.16)
        self.assertEqual(abana["closing_qty"], 121.0)
        self.assertEqual(abana["closing_value"], 18879.63)

        diarex = self.by_name.get("DIAREX TAB") or self.by_name.get("DIAREX TAB|1*30TAB")
        self.assertIsNotNone(diarex)
        self.assertEqual(diarex["opening_qty"], 35.0)
        self.assertEqual(diarex["receipts_qty"], 100.0)
        self.assertEqual(diarex["sales_qty"], 12.0)
        self.assertEqual(diarex["closing_qty"], 123.0)
        self.assertEqual(diarex["closing_value"], 10854.61)

        himcocid = next(
            item
            for item in self.result["line_items"]
            if "HIMCOCID SF" in str(item.get("product_name") or "").upper()
        )
        self.assertEqual(himcocid["opening_qty"], 33.6)
        self.assertEqual(himcocid["closing_qty"], 33.6)

    def test_printed_grand_totals(self):
        self.assertEqual(self.result["totals"].get("sales_value"), 434457.57)
        self.assertEqual(self.result["totals"].get("closing_value"), 1084435.94)
        self.assertNotEqual(self.result["totals"].get("sales_value"), 350347.04)
        self.assertNotEqual(self.result["totals"].get("closing_value"), 782058.01)


@unittest.skipUnless(PRAKASH.is_file(), "missing Prakash SSA fixture")
class TestPrakashSsaNotStolen(unittest.TestCase):
    def test_prakash_still_uses_original_ssa_parser(self):
        result = extract_sales_statement(PRAKASH.read_bytes(), PRAKASH.name)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "ssa_opening_receipt_issue_value")
        self.assertEqual(result["stockist_name"], "PRAKASH MEDICAL STORE")
        self.assertEqual(result["totals"].get("sales_value"), 94830.56)
        self.assertEqual(result["totals"].get("closing_value"), 430534.70)


if __name__ == "__main__":
    unittest.main()
