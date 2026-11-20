"""MFR Wise Stock and Sales: header x-centres, not OCR last-N tokens."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import extract_sales_statement

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mfr_wise_stock_sales.pdf"


class MfrWiseStockSalesPdfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(_FIXTURE.read_bytes(), _FIXTURE.name)

    def test_company_title_and_period_come_from_the_printed_header(self):
        result = self.result
        self.assertEqual(result["company_name"], "HIMALAYA ZEAL DIVISION")
        self.assertEqual(result["stockist_name"], "PALANI AGENCIES")
        self.assertEqual(
            result["report_title"],
            "MFR Wise Stock and Sales From 01/09/26 To 30/09/26",
        )
        self.assertEqual(result["period_from"], "2026-09-01")
        self.assertEqual(result["period_to"], "2026-09-30")

    def test_header_text_is_not_a_product_row(self):
        names = [item["product_name"] for item in self.result["line_items"]]
        self.assertNotIn("100", names)
        self.assertFalse(any(name.upper().startswith("MFR ") for name in names))
        self.assertNotIn("HIMALAYA ZEAL DIVISION", [name.upper() for name in names])

    def test_opening_sales_and_closing_follow_the_printed_columns(self):
        by_name = {item["product_name"]: item for item in self.result["line_items"]}
        abana = by_name["ABANA TABS"]
        self.assertEqual(abana["packing"], "60'S")
        self.assertEqual(abana["opening_qty"], 100.0)
        self.assertEqual(abana["sales_qty"], 80.0)
        self.assertEqual(abana["closing_qty"], 20.0)

        confido = by_name["CONFIDO TAB"]
        self.assertEqual(confido["opening_qty"], 0.0)
        self.assertEqual(confido["sales_qty"], 20.0)
        self.assertEqual(confido["closing_qty"], 0.0)
        self.assertEqual((confido.get("extra") or {}).get("sale_return"), 20.0)

        # Printed Op 100, SQ 50, Cl 0. Do not rewrite the closing to force a balance.
        diabecon = by_name["DIABECON-DS TAB"]
        self.assertEqual(diabecon["opening_qty"], 100.0)
        self.assertEqual(diabecon["sales_qty"], 50.0)
        self.assertEqual(diabecon["closing_qty"], 0.0)

    def test_printed_footer_values_are_kept(self):
        totals = self.result["totals"]
        self.assertEqual(totals["sales_value"], 86897.6)
        self.assertEqual(totals["closing_value"], 39780.16)


if __name__ == "__main__":
    unittest.main()
