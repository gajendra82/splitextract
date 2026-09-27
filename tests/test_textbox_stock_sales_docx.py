"""Text-box STOCK & SALES ANALYSIS. Repeated drawings are one statement."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import extract_sales_statement

FIXTURE = Path("0000729801_2026_08_ZL_20_303_06092026105836.docx")


class TestTextboxStockSalesDocx(unittest.TestCase):
    def test_sahuwala_text_boxes_are_one_statement(self):
        if not FIXTURE.is_file():
            self.skipTest(f"missing {FIXTURE.name}")
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        items = result["line_items"]
        names = [item["product_name"] for item in items]
        self.assertEqual(len(items), 22)
        self.assertNotIn("statements", result)
        self.assertNotIn("TOTAL", names)
        self.assertEqual(result["stockist_name"], "SAHUWALA MEDICAL AGENCIES")
        self.assertIn("SRIGANGANAGAR", result["stockist_address"])
        self.assertEqual(result["company_name"], "HIMALYA ZEAL")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        self.assertNotEqual(result["period_from"][:7], "2026-09")

        first = items[0]
        self.assertEqual(first["product_name"], "CLARIA ANTI ACNE CREAM")
        self.assertEqual(first["packing"], "1*30GM")
        self.assertEqual(first["opening_qty"], 0)
        self.assertEqual(first["sales_qty"], 0)
        self.assertEqual(first["closing_qty"], 0)

        confido = next(item for item in items if item["product_name"] == "CONFIDO TAB")
        self.assertEqual(confido["packing"], "1*60AB")
        self.assertEqual(confido["opening_qty"], 50)
        self.assertEqual(confido["receipts_qty"], 0)
        self.assertEqual(confido["sales_qty"], 0)
        self.assertEqual(confido["closing_qty"], 50)
        self.assertEqual(confido["sales_value"], 0)

        syrup = next(item for item in items if item["product_name"] == "LIV 52 SYP")
        self.assertEqual(syrup["packing"], "1*200ML")
        self.assertEqual(syrup["opening_qty"], 185)
        self.assertEqual(syrup["receipts_qty"], 1750)
        self.assertEqual(syrup["sales_qty"], 1550)
        self.assertEqual(syrup["closing_qty"], 385)

        self.assertEqual(sum(item["opening_qty"] for item in items), 1185)
        self.assertEqual(sum(item["receipts_qty"] for item in items), 1750)
        self.assertEqual(sum(item["sales_qty"] for item in items), 2175)
        self.assertEqual(sum(item["closing_qty"] for item in items), 760)
        self.assertIsNone(result["totals"]["sales_value"])
        self.assertIsNone(result["totals"]["closing_value"])
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "textbox_stock_sales_analysis_docx",
        )


if __name__ == "__main__":
    unittest.main()
