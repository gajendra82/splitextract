"""Western Healthcare Monthly Sales And Stock PDF — format-specific checks."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_monthly_sales_and_stock_text,
    _parse_monthly_ss_report,
    extract_sales_statement,
)

SAMPLE = Path(
    r"C:\Users\adity\Downloads\testing\0000734351_2026_08_ZL_11_495_02092026115041.pdf"
)


class TestMonthlySalesAndStockDetect(unittest.TestCase):
    def test_monthly_ss_report_is_not_this_layout(self):
        text = (
            "Monthly SS Report\nOpening\nPur. Qty\nSale Qty\n"
            "Item Name\nClosing\nP Rate\nPTR"
        )
        self.assertFalse(_is_monthly_sales_and_stock_text(text))

    def test_title_and_rate_columns_detect(self):
        text = "Monthly Sales And Stock\nP Rate\nPTR\nOpening\nClosing"
        self.assertTrue(_is_monthly_sales_and_stock_text(text))


@unittest.skipUnless(SAMPLE.exists(), "sample Monthly Sales And Stock pdf not on this machine")
class TestMonthlySalesAndStockSample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(SAMPLE.read_bytes(), SAMPLE.name)

    def test_metadata_and_totals(self):
        self.assertEqual(
            self.result["totals"]["extra"]["extraction_method"],
            "monthly_sales_and_stock",
        )
        self.assertEqual(self.result["stockist_name"], "WESTERN HEALTHCARE SOLUTIONS PVT. LTD.")
        self.assertIn("KOLLAM", self.result["stockist_address"] or "")
        self.assertEqual(self.result["company_name"], "HIMALAYA WELLMESS COMPANY")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertEqual(len(self.result["line_items"]), 23)
        self.assertEqual(self.result["totals"]["sales_value"], 68589.30)
        self.assertEqual(self.result["totals"]["closing_value"], 134256.12)
        self.assertEqual(self.result["totals"]["opening_qty"], 734.0)
        self.assertEqual(self.result["totals"]["receipts_qty"], 585.0)
        self.assertEqual(self.result["totals"]["sales_qty"], 470.0)
        self.assertEqual(self.result["totals"]["closing_qty"], 853.0)
        self.assertEqual(self.result["totals"]["extra"]["opening_value"], 94904.05)
        self.assertEqual(self.result["totals"]["extra"]["purchase_value"], 86616.75)

    def test_wrapped_names_and_printed_columns(self):
        items = {item["product_name"]: item for item in self.result["line_items"]}
        bleminor = items["BLEMINOR ANTI BLEMISH CREAM"]
        self.assertEqual(bleminor["opening_qty"], 1.0)
        self.assertEqual(bleminor["receipts_qty"], 50.0)
        self.assertEqual(bleminor["sales_qty"], 1.0)
        self.assertEqual(bleminor["sales_value"], 171.44)
        self.assertEqual(bleminor["closing_qty"], 50.0)
        self.assertEqual(bleminor["closing_value"], 9790.0)
        confido = items["CONFIDO TABLET"]
        self.assertEqual(confido["opening_qty"], 17.0)
        self.assertEqual(confido["opening_value"], 2529.77)
        self.assertEqual(confido["sales_qty"], 7.0)
        self.assertEqual(confido["sales_value"], 1128.04)
        self.assertEqual(confido["closing_qty"], 10.0)
        self.assertEqual(confido["closing_value"], 1678.60)
        self.assertIn("CLARINA ANTI ACNE FACEWASH GEL", items)
        syrups = [
            item for item in self.result["line_items"] if item["product_name"] == "LIV 52 SYRUP"
        ]
        self.assertEqual(sorted(item["packing"] for item in syrups), ["100 ML", "200 ML"])
        liv = items["LIV 52 TABLET"]
        self.assertEqual(liv["packing"], "100 S")
        self.assertEqual(liv["opening_qty"], 88.0)
        self.assertEqual(liv["receipts_qty"], 100.0)
        self.assertEqual(liv["sales_qty"], 72.0)
        self.assertEqual(liv["sales_value"], 11622.88)
        self.assertEqual(liv["closing_qty"], 116.0)
        ophth = items["OPHTHACARE EYE DROPS"]
        self.assertEqual(ophth["sales_qty"], 23.0)
        self.assertEqual(ophth["extra"]["sales_free"], 1.0)
        self.assertEqual(ophth["closing_qty"], 105.0)
        pilex = items["PILEX FORTE TABLET"]
        self.assertEqual(pilex["sales_qty"], 7.0)
        self.assertEqual(pilex["extra"]["sale_return_qty"], 5.0)
        self.assertEqual(pilex["extra"]["sale_return_value"], 586.70)
        self.assertEqual(pilex["closing_qty"], 42.0)

    def test_does_not_use_monthly_ss_parser(self):
        import fitz

        doc = fitz.open(stream=SAMPLE.read_bytes(), filetype="pdf")
        try:
            self.assertIsNone(_parse_monthly_ss_report(doc, SAMPLE.name))
        finally:
            doc.close()


if __name__ == "__main__":
    unittest.main()
