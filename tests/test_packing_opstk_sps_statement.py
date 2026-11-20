"""Packing / Op Stk / Sp S Qty / D. Qty stock-and-sale PDF."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_packing_opstk_sps_statement_text,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000700176_2026_08_ZL_06_306_04092026132813.pdf"
)

HEADER = (
    "THACKER PHARMA DISTRIBUTORS\n"
    "Stock and Sale Statement  From  01-Aug-26 to 31-Aug-26\n"
    "Item Cd Item Name Packing Op Stk P Qty P Val S Qty Sp S Qty "
    "S Val C Qty D. Qty A Qty Cl Stk Cl Val\n"
)

ZANDRA = (
    "Stock and Sale Statement\n"
    "Item Cd  Item Name  Op Stk  P Qty  P Val  S Qty  S Val  Cl Stk  Cl Val\n"
)


class TestPackingOpstkDetector(unittest.TestCase):
    def test_requires_packing_scheme_and_d_qty(self):
        self.assertTrue(_is_packing_opstk_sps_statement_text(HEADER))
        self.assertFalse(_is_packing_opstk_sps_statement_text(ZANDRA))
        self.assertFalse(_is_packing_opstk_sps_statement_text(""))


@unittest.skipUnless(FIXTURE.is_file(), "missing packing opstk fixture")
class TestPackingOpstkFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            str(item.get("product_name") or "").upper(): item
            for item in cls.result["line_items"]
        }

    def test_header_period_and_method(self):
        self.assertEqual(self.extra.get("extraction_method"), "packing_opstk_sps_statement")
        self.assertIn("THACKER PHARMA", str(self.result.get("stockist_name") or "").upper())
        self.assertIn("KATIRA", str(self.result.get("stockist_address") or "").upper())
        self.assertIn("ZEAL", str(self.result.get("company_name") or "").upper())
        self.assertIn("HIMALAYA", str(self.result.get("company_name") or "").upper())
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertGreaterEqual(len(self.result["line_items"]), 40)
        self.assertEqual(self.result["totals"]["sales_value"], 126498.0)
        self.assertEqual(self.result["totals"]["closing_value"], 190314.0)

    def test_columns_follow_printed_right_edges(self):
        soap = self.by_name["AACTARIL SOAP"]
        self.assertEqual(soap["opening_qty"], 18)
        self.assertEqual(soap["sales_qty"], 2)
        self.assertEqual(soap["sales_value"], 168)
        self.assertEqual(soap["closing_qty"], 16)
        self.assertEqual(soap["closing_value"], 1119)
        self.assertEqual(soap["receipts_qty"], 0)

        ds = self.by_name["DIABECON DS TAB"]
        self.assertEqual(ds["opening_qty"], 28)
        self.assertEqual(ds["receipts_qty"], 50)
        self.assertEqual(ds["extra"]["purchase_value"], 9625)
        self.assertEqual(ds["sales_qty"], 28)
        self.assertEqual(ds["closing_qty"], 50)

        syrup = self.by_name["LIV-52 SYP"]
        self.assertEqual(syrup["opening_qty"], 70)
        self.assertEqual(syrup["receipts_qty"], 140)
        self.assertEqual(syrup["sales_qty"], 127)
        self.assertEqual(syrup["closing_qty"], 83)

        tab = self.by_name["LIV-52 TAB"]
        self.assertEqual(tab["sales_qty"], 89)
        self.assertEqual(tab["closing_qty"], 112)
        self.assertEqual(tab["extra"]["c_qty"], 5)

        oxitard = self.by_name["OXITARD CAP"]
        self.assertEqual(oxitard["opening_qty"], 0)
        self.assertEqual(oxitard["receipts_qty"], 84)
        self.assertEqual(oxitard["sales_qty"], 21)
        self.assertEqual(oxitard["closing_qty"], 63)

        shallaki = self.by_name["SHALLAKI CAP."]
        self.assertEqual(shallaki["opening_qty"], 7)
        self.assertEqual(shallaki["sales_qty"], 7)
        self.assertEqual(shallaki["closing_qty"], 0)
