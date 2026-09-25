"""VED Medisales Particulars / Open Qty / Purch Qty / Sales & DC PDF."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_ssa_opening_receipt_issue_value_text,
    _is_ved_stock_sales_statement_text,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000735580_2026_08_ZL_13_272_02092026120100.pdf"
)

HEADER = (
    "VED MEDISALES PRIVATE LIMITED\n"
    "Stock and sales Statement from : 01/08/2026 to 31/08/2026\n"
    "Particulars  Pkg.  Open. Qty.  Purch. Qty.  Sales Ret.  Sales & DC  "
    "Misc Out  Close Stock  Closing Value  Sales Value\n"
)

ZANDRA = (
    "Stock and Sale Statement\n"
    "Item Cd  Item Name  Op Stk  P Qty  P Val  S Qty  S Val  Cl Stk  Cl Val\n"
)

SSA = (
    "STOCK & SALES ANALYSIS 01/08/2026 - 31/08/2026\n"
    "ITEM DESCRIPTION  OPENING  RECEIPT  ISSUE  CLOSING  DUMP\n"
    "QTY. VALUE QTY. VALUE QTY. VALUE QTY. VALUE QTY.\n"
)


class TestVedDetection(unittest.TestCase):
    def test_detects_layout_from_headers_not_filename(self):
        self.assertTrue(_is_ved_stock_sales_statement_text(HEADER))
        self.assertFalse(_is_ved_stock_sales_statement_text(ZANDRA))
        self.assertFalse(_is_ved_stock_sales_statement_text(SSA))
        self.assertFalse(_is_ved_stock_sales_statement_text(""))
        self.assertFalse(_is_ssa_opening_receipt_issue_value_text(HEADER))


@unittest.skipUnless(FIXTURE.is_file(), "missing VED Medisales fixture")
class TestVedFixture(unittest.TestCase):
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

    def test_uses_ved_parser(self):
        self.assertEqual(self.extra.get("extraction_method"), "ved_stock_sales_statement")
        self.assertIn("VED MEDISALES", str(self.result.get("stockist_name") or "").upper())
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertIn("HIMALAYA", str(self.result.get("company_name") or "").upper())
        self.assertGreaterEqual(len(self.result["line_items"]), 35)
        self.assertNotIn("Invoices", self.result)

    def test_headers_totals_and_expiry_list_are_not_products(self):
        joined = " | ".join(self.names).upper()
        self.assertNotIn("GRAND TOTAL", joined)
        self.assertNotIn("PARTICULARS", joined)
        self.assertNotIn("BATCHNO", joined)
        self.assertNotIn("LIST OF ITEMS", joined)
        self.assertFalse(any(name.upper() == "GEN" for name in self.names))

    def test_columns_are_not_shifted_into_receipts_value(self):
        soap = self.by_name["AACTARIL SOAP-75GM"]
        self.assertEqual(soap["packing"], "75GMS")
        self.assertEqual(soap["opening_qty"], 64.0)
        self.assertEqual(soap["receipts_qty"], 0.0)
        self.assertEqual(soap["sales_qty"], 0.0)
        self.assertEqual(soap["closing_qty"], 64.0)
        self.assertEqual(soap["closing_value"], 5607.68)
        self.assertFalse(soap.get("sales_value"))

        abana = self.by_name["ABANA TAB-60"]
        self.assertEqual(abana["opening_qty"], 63.0)
        self.assertEqual(abana["sales_qty"], 11.0)
        self.assertEqual(abana["sales_value"], 1843.82)
        self.assertEqual(abana["closing_qty"], 52.0)
        self.assertEqual(abana["closing_value"], 8716.24)
        self.assertNotEqual(abana["receipts_qty"], 8716.24)

        hadjod = next(
            item
            for item in self.result["line_items"]
            if "HADJOD" in str(item.get("product_name") or "").upper()
        )
        self.assertEqual(hadjod["opening_qty"], 13.0)
        self.assertEqual(hadjod["receipts_qty"], 120.0)
        self.assertEqual(hadjod["sales_qty"], 8.0)
        self.assertEqual(hadjod["closing_qty"], 125.0)
        self.assertEqual(hadjod["sales_value"], 1980.96)
        self.assertEqual(hadjod["closing_value"], 30952.50)

        bleminor = next(
            item
            for item in self.result["line_items"]
            if "BLEMINOR" in str(item.get("product_name") or "").upper()
        )
        self.assertEqual(bleminor["product_name"].upper(), "BLEMINOR ANTI.BLEM.CR-30")
        self.assertEqual(bleminor["opening_qty"], 21.0)
        self.assertEqual(bleminor["sales_qty"], 11.0)
        self.assertEqual(bleminor["closing_qty"], 10.0)

        clarina_acne = next(
            item
            for item in self.result["line_items"]
            if "CLARINA" in str(item.get("product_name") or "").upper()
            and "F.W.GEL" in str(item.get("product_name") or "").upper()
        )
        self.assertEqual(clarina_acne["product_name"].upper(), "CLARINA ANTI ACNE F.W.GEL-60")
        self.assertNotIn("ANTI.BLEM", clarina_acne["product_name"].upper())
        self.assertEqual(clarina_acne["opening_qty"], 42.0)
        self.assertEqual(clarina_acne["sales_qty"], 2.0)

        clarina_cream = next(
            item
            for item in self.result["line_items"]
            if "CREAM(ANT.ACNE)" in str(item.get("product_name") or "").upper()
        )
        self.assertEqual(clarina_cream["product_name"].upper(), "CLARINA CREAM(ANT.ACNE)-30")
        self.assertNotIn("F.W.GEL", clarina_cream["product_name"].upper())
        self.assertEqual(clarina_cream["opening_qty"], 24.0)
        self.assertEqual(clarina_cream["sales_qty"], 8.0)

        confido = next(
            item
            for item in self.result["line_items"]
            if "CONFIDO" in str(item.get("product_name") or "").upper()
        )
        self.assertEqual(confido["product_name"].upper(), "CONFIDO TAB-60")
        self.assertNotIn("CREAM", confido["product_name"].upper())
        self.assertEqual(confido["opening_qty"], 60.0)
        self.assertEqual(confido["sales_qty"], 37.0)

    def test_printed_grand_totals(self):
        self.assertEqual(self.result["totals"].get("closing_value"), 304880.64)
        self.assertEqual(self.result["totals"].get("sales_value"), 124866.76)


if __name__ == "__main__":
    unittest.main()
