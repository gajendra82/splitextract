"""Word STOCK & SALES ANALYSIS with Sale and Closing qty/value only."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import extract_sales_statement

FIXTURE = Path("0000713511_2026_08_ZA_24_255_07092026171904.docx")


class TestSaleClosingAnalysisDocx(unittest.TestCase):
    def test_lachmi_drug_sale_and_closing(self):
        self.assertTrue(FIXTURE.is_file(), "fixture docx is missing")
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        items = result["line_items"]
        self.assertEqual(len(items), 55)
        self.assertNotIn("statements", result)
        self.assertEqual(result["stockist_name"], "LACHMI DRUG AGENCIES")
        self.assertIn("AMINABAD", result["stockist_address"])
        self.assertEqual(result["company_name"], "HIMALAYA (ZANDRA)")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        self.assertFalse(any(item["product_name"].upper() == "TOTAL" for item in items))

        first = items[0]
        self.assertEqual(first["product_name"], "ARJUNA TAB")
        self.assertEqual(first["packing"], "1*60TAB")
        self.assertEqual(first["sales_qty"], 15)
        self.assertEqual(first["sales_value"], 3496)
        self.assertEqual(first["closing_qty"], 1)
        self.assertEqual(first["closing_value"], 208)

        drop = next(item for item in items if item["product_name"] == "BONNISAN DROP")
        self.assertEqual(drop["packing"], "30ML")
        self.assertEqual(drop["sales_qty"], 9)
        self.assertEqual(drop["sales_value"], 627)
        self.assertEqual(drop["closing_qty"], 87)
        self.assertEqual(drop["closing_value"], 5744)

        self.assertEqual(sum(item["sales_qty"] for item in items), 570)
        self.assertEqual(sum(item["sales_value"] for item in items), 86604)
        self.assertEqual(sum(item["closing_qty"] for item in items), 1952)
        self.assertEqual(sum(item["closing_value"] for item in items), 295088)
        self.assertEqual(result["totals"]["sales_value"], 86604)
        self.assertEqual(result["totals"]["closing_value"], 295088)
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "sale_closing_analysis_docx",
        )


if __name__ == "__main__":
    unittest.main()
