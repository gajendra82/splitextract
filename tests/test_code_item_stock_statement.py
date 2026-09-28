"""Code / Item Description / Opening / Purchase stock-statement PDF."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_code_item_stock_statement_text,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000721161_2026_08_ZL_13_287_04092026124132.PDF"
)

HEADER = (
    "GANESH AGENCY\n"
    "Stock Statment : HIMALAYA ZEAL\n"
    "31/08/26\n"
    "Code  Item Description  Packing  Opening Purchase  Sales  Closing  "
    "Stock-Value  Sales-Value\n"
)

OTHER = (
    "Sales & Stock Statement(From 01/08/2026 Upto 31/08/2026)\n"
    "Code PRODUCT NAME PACKING Op. Opening Bal Receipt Receipt/Pur\n"
)


class TestCodeItemDetection(unittest.TestCase):
    def test_detects_layout_from_headers_not_filename(self):
        self.assertTrue(_is_code_item_stock_statement_text(HEADER))
        self.assertFalse(_is_code_item_stock_statement_text(OTHER))
        self.assertFalse(_is_code_item_stock_statement_text("Product Stock Report"))
        self.assertFalse(_is_code_item_stock_statement_text(""))


@unittest.skipUnless(FIXTURE.is_file(), "missing Ganesh Agency fixture")
class TestCodeItemFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_code = {
            item.get("product_code"): item for item in cls.result["line_items"]
        }
        cls.names = [
            str(item.get("product_name") or "") for item in cls.result["line_items"]
        ]

    def test_uses_code_item_parser(self):
        self.assertEqual(self.extra.get("extraction_method"), "code_item_stock_statement")
        self.assertGreaterEqual(len(self.result["line_items"]), 50)
        self.assertEqual(self.result["stockist_name"], "GANESH AGENCY")
        self.assertIn("HIMALAYA", str(self.result.get("company_name") or "").upper())
        self.assertEqual(self.result["period_to"], "2026-08-31")

    def test_headers_dates_and_batch_rows_are_not_products(self):
        joined = " | ".join(self.names).upper()
        self.assertNotIn("31/08/26", joined)
        self.assertNotIn("ITEM DESCRIPTION", joined)
        self.assertNotIn("TOTALS", joined)
        self.assertFalse(any("BATCH" in name.upper() for name in self.names))
        self.assertNotIn("Invoices", self.result)

    def test_codes_are_not_used_as_opening_qty_or_name_prefix(self):
        for item in self.result["line_items"]:
            code = str(item.get("product_code") or "")
            name = str(item.get("product_name") or "")
            self.assertTrue(code.isdigit(), name)
            self.assertFalse(name.startswith(code), name)
            self.assertNotEqual(item.get("opening_qty"), float(code))

    def test_sample_rows(self):
        aactaril = self.by_code["10463"]
        self.assertEqual(aactaril["product_name"], "AACTARIL SOAP")
        self.assertEqual(aactaril["packing"], "75GM")
        self.assertEqual(aactaril["opening_qty"], 0.0)
        self.assertEqual(aactaril["receipts_qty"], 0.0)

        abana = self.by_code["10373"]
        self.assertEqual(abana["product_name"], "ABANA TAB")
        self.assertEqual(abana["opening_qty"], 61.0)
        self.assertEqual(abana["receipts_qty"], 0.0)
        self.assertEqual(abana["sales_qty"], 4.0)
        self.assertEqual(abana["closing_qty"], 57.0)
        self.assertEqual(abana["closing_value"], 8893.71)
        self.assertEqual(abana["sales_value"], 670.48)

        tentex = self.by_code["10462"]
        self.assertEqual(tentex["product_name"], "TENTEX FORTE TAB")
        self.assertEqual(tentex["opening_qty"], 79.0)
        self.assertEqual(tentex["receipts_qty"], 300.0)
        self.assertEqual(tentex["sales_qty"], 44.0)
        self.assertEqual(tentex["closing_qty"], 335.0)
        self.assertEqual(tentex["closing_value"], 33257.96)
        self.assertEqual(tentex["sales_value"], 4693.04)

        talekt = self.by_code["10485"]
        self.assertEqual(talekt["product_name"], "TALEKT TAB")
        self.assertEqual(talekt["opening_qty"], 0.0)
        self.assertNotEqual(talekt["opening_qty"], 10485.0)

    def test_printed_totals_and_no_duplicates(self):
        self.assertEqual(self.result["totals"].get("closing_value"), 254822.26)
        self.assertEqual(self.result["totals"].get("sales_value"), 131171.34)
        codes = [i.get("product_code") for i in self.result["line_items"]]
        self.assertEqual(len(codes), len(set(codes)))


if __name__ == "__main__":
    unittest.main()
