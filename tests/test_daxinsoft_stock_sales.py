"""Daxinsoft Profitmaker Stock & Sales Statement (O.Stk / Qoh / Age)."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_daxinsoft_stock_sales_text,
    _is_ssa_opening_receipt_issue_value_text,
    _is_ved_stock_sales_statement_text,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000735801_2026_08_ZL_37_394_04092026164546.Pdf"
)

HEADER = (
    "DICAL AGENICES\n"
    "Stock & Sales Statement\n"
    "From 01/04/2026 To 30/04/2026\n"
    "Product Name  Packing  O.Stk  Purc  Tot  Sale  Qoh  Value  Age\n"
    "Company :HIMALAYA\n"
    "Generated in PROFITMAKER software\n"
)

VED = (
    "VED MEDISALES PRIVATE LIMITED\n"
    "Stock and sales Statement from : 01/08/2026 to 31/08/2026\n"
    "Particulars  Pkg.  Open. Qty.  Purch. Qty.  Sales Ret.  Sales & DC  "
    "Close Stock  Closing Value  Sales Value\n"
)

SSA = (
    "STOCK & SALES ANALYSIS 01/08/2026 - 31/08/2026\n"
    "ITEM DESCRIPTION  OPENING  RECEIPT  ISSUE  CLOSING  DUMP\n"
    "QTY. VALUE QTY. VALUE QTY. VALUE QTY. VALUE QTY.\n"
)

SALEABLE = (
    "Saleable Stock Report\n"
    "Particular  Opn  Rec  Issue  Bal\n"
)


class TestDaxinsoftDetection(unittest.TestCase):
    def test_detects_layout_from_headers_not_filename(self):
        self.assertTrue(_is_daxinsoft_stock_sales_text(HEADER))
        self.assertFalse(_is_daxinsoft_stock_sales_text(VED))
        self.assertFalse(_is_daxinsoft_stock_sales_text(SSA))
        self.assertFalse(_is_daxinsoft_stock_sales_text(SALEABLE))
        self.assertFalse(_is_daxinsoft_stock_sales_text(""))
        self.assertFalse(_is_ved_stock_sales_statement_text(HEADER))
        self.assertFalse(_is_ssa_opening_receipt_issue_value_text(HEADER))


@unittest.skipUnless(FIXTURE.is_file(), "missing Daxinsoft Profitmaker fixture")
class TestDaxinsoftFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            str(item.get("product_name") or "").upper(): item
            for item in cls.result["line_items"]
        }
        cls.names = [
            str(item.get("product_name") or "") for item in cls.result["line_items"]
        ]

    def test_uses_daxinsoft_parser(self):
        self.assertEqual(self.extra.get("extraction_method"), "daxinsoft_stock_sales")
        self.assertIn("AGEN", str(self.result.get("stockist_name") or "").upper())
        self.assertNotIn("DAXINSOFT", str(self.result.get("stockist_name") or "").upper())
        self.assertIn("HIMALAYA", str(self.result.get("company_name") or "").upper())
        self.assertEqual(self.result["period_from"], "2026-04-01")
        self.assertEqual(self.result["period_to"], "2026-04-30")
        self.assertEqual(len(self.result["line_items"]), 40)
        self.assertNotIn("Invoices", self.result)

    def test_age_is_not_closing_value_and_names_are_whole(self):
        soap = self.by_name["AACTARIL SOAP"]
        self.assertEqual(soap["packing"], "75g")
        self.assertEqual(soap["opening_qty"], 0.0)
        self.assertEqual(soap["sales_qty"], 0.0)
        self.assertEqual(soap["closing_qty"], 0.0)
        self.assertEqual(soap["closing_value"], 0.0)
        self.assertNotEqual(soap["closing_value"], 1677.0)
        self.assertEqual((soap.get("extra") or {}).get("age_days"), 1677.0)

        evocare = self.by_name["EVECARE CAP"]
        self.assertEqual(evocare["packing"], "30`S")
        self.assertEqual(evocare["opening_qty"], 10.0)
        self.assertEqual(evocare["receipts_qty"], 0.0)
        self.assertEqual(evocare["sales_qty"], 0.0)
        self.assertEqual(evocare["closing_qty"], 10.0)
        self.assertEqual(evocare["closing_value"], 1316.40)
        self.assertNotEqual(evocare["closing_qty"], 1316.40)
        self.assertEqual((evocare.get("extra") or {}).get("age_days"), 304.0)

        gasex = self.by_name["GASEX TAB"]
        self.assertEqual(gasex["opening_qty"], 47.0)
        self.assertEqual(gasex["sales_qty"], 3.0)
        self.assertEqual(gasex["closing_qty"], 44.0)
        self.assertEqual(gasex["closing_value"], 5676.11)

        hair = self.by_name["HAIR ZONE SOLUTION"]
        self.assertEqual(hair["opening_qty"], 23.0)
        self.assertEqual(hair["sales_qty"], 6.0)
        self.assertEqual(hair["closing_qty"], 17.0)
        self.assertEqual(hair["closing_value"], 5304.67)

        liv = self.by_name["LIV 52 SYP SMALL"]
        self.assertEqual(liv["opening_qty"], 165.0)
        self.assertEqual(liv["sales_qty"], 83.0)
        self.assertEqual(liv["closing_qty"], 82.0)
        self.assertEqual(liv["closing_value"], 7778.39)

    def test_headers_and_footer_are_not_products(self):
        joined = " | ".join(self.names).upper()
        self.assertNotIn("OPENING VALUE", joined)
        self.assertNotIn("SALE VALUE", joined)
        self.assertNotIn("PRODUCT NAME", joined)
        self.assertFalse(any("DAXINSOFT" in name.upper() for name in self.names))

    def test_printed_footer_totals(self):
        self.assertEqual(self.result["totals"].get("sales_value"), 17602.96)
        self.assertEqual(self.result["totals"].get("closing_value"), 35473.23)
        self.assertEqual(self.extra.get("total_row_source"), "daxinsoft_footer")


if __name__ == "__main__":
    unittest.main()
