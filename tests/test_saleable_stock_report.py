"""Saleable Stock Report Particular | Opn | Rec | Issue | Bal PDF."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_saleable_stock_report_text,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000720977_2026_08_ZL_07_223_05092026080848.pdf"
)

HEADER = (
    "PARAS MEDICOS\n"
    "(From 01/08/2026 To 31/08/2026)\n"
    "Saleable Stock Report\n"
    "Particular | | Opn | Rec | Issue | Bal\n"
)

OTHER = (
    "GANESH AGENCY\n"
    "Stock Statment : HIMALAYA ZEAL\n"
    "Code  Item Description  Packing  Opening Purchase  Sales  Closing\n"
)


class TestSaleableStockReportDetection(unittest.TestCase):
    def test_detects_layout_from_headers_not_filename(self):
        self.assertTrue(_is_saleable_stock_report_text(HEADER))
        self.assertFalse(_is_saleable_stock_report_text(OTHER))
        self.assertFalse(_is_saleable_stock_report_text("Product Stock Report"))
        self.assertFalse(_is_saleable_stock_report_text(""))


@unittest.skipUnless(FIXTURE.is_file(), "missing Paras Medicos fixture")
class TestSaleableStockReportFixture(unittest.TestCase):
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

    def test_uses_saleable_parser(self):
        self.assertEqual(self.extra.get("extraction_method"), "saleable_stock_report")
        self.assertEqual(len(self.result["line_items"]), 15)
        self.assertEqual(self.result["stockist_name"], "PARAS MEDICOS")
        self.assertIn("HIMALAYA", str(self.result.get("company_name") or "").upper())
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertNotIn("Invoices", self.result)

    def test_date_header_and_totals_are_not_products(self):
        joined = " | ".join(self.names).upper()
        self.assertNotIn("(FROM", joined)
        self.assertNotIn("FROM 01", joined)
        self.assertNotIn("PARTICULAR", joined)
        self.assertNotIn("COMPANY TOTAL", joined)
        self.assertNotIn("FIRM TOTAL", joined)
        self.assertFalse(any(name.strip().startswith("(") for name in self.names))

    def test_sample_rows_qty_not_shifted(self):
        confido = self.by_name["CONFIDO TAB."]
        self.assertEqual(confido["packing"], "1X60 TAB")
        self.assertEqual(confido["opening_qty"], 25.0)
        self.assertEqual(confido["receipts_qty"], 0.0)
        self.assertEqual(confido["sales_qty"], 3.0)
        self.assertEqual(confido["closing_qty"], 22.0)

        gasex = self.by_name["GASEX TAB"]
        self.assertEqual(gasex["packing"], "60 TAB")
        self.assertEqual(gasex["opening_qty"], 54.0)
        self.assertEqual(gasex["sales_qty"], 1.0)
        self.assertEqual(gasex["closing_qty"], 53.0)

        hadjod = self.by_name["HADJOD TAB"]
        self.assertEqual(hadjod["opening_qty"], 23.0)
        self.assertEqual(hadjod["sales_qty"], 6.0)
        self.assertEqual(hadjod["closing_qty"], 17.0)

        liv_rows = [
            item
            for item in self.result["line_items"]
            if str(item.get("product_name") or "").upper() == "LIV.52 SYP"
        ]
        self.assertEqual(len(liv_rows), 2)
        liv200 = next(r for r in liv_rows if r.get("packing") == "200 ML")
        self.assertEqual(liv200["opening_qty"], 303.0)
        self.assertEqual(liv200["receipts_qty"], 1120.0)
        self.assertEqual(liv200["sales_qty"], 307.0)
        self.assertEqual(liv200["closing_qty"], 1116.0)

        bleminor = self.by_name["BLEMINOR ANTIBLCR"]
        self.assertEqual(bleminor["opening_qty"], 15.0)
        self.assertEqual(bleminor["sales_qty"], 0.0)
        self.assertEqual(bleminor["closing_qty"], 15.0)

        tentex = next(
            item
            for item in self.result["line_items"]
            if "TENTEX" in str(item.get("product_name") or "").upper()
        )
        self.assertEqual(tentex["opening_qty"], 54.0)
        self.assertEqual(tentex["sales_qty"], 4.0)
        self.assertEqual(tentex["closing_qty"], 50.0)

    def test_printed_company_totals_not_allocated_to_lines(self):
        extra = self.extra
        self.assertEqual(extra.get("opening_qty"), 942.0)
        self.assertEqual(extra.get("receipts_qty"), 1130.0)
        self.assertEqual(extra.get("sales_qty"), 401.0)
        self.assertEqual(extra.get("closing_qty"), 1671.0)
        self.assertEqual(extra.get("opening_value"), 153640.3)
        self.assertEqual(extra.get("receipts_value"), 202161.3)
        self.assertEqual(extra.get("printed_sales_value"), 68099.63)
        self.assertEqual(extra.get("printed_closing_value"), 285333.70)
        for item in self.result["line_items"]:
            self.assertFalse(item.get("sales_value"))
            self.assertFalse(item.get("closing_value"))


if __name__ == "__main__":
    unittest.main()
