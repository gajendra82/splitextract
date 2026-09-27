"""Profitmaker Word table. Company rows are not products. Age is not sales value."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import extract_sales_statement

FIXTURE = Path("0000722839_2026_08_ZL_37_390_05092026064113.docx")


class TestProfitmakerOstkDocx(unittest.TestCase):
    def test_divya_pharma_columns_and_company_header(self):
        if not FIXTURE.is_file():
            self.skipTest(f"missing {FIXTURE.name}")
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        items = result["line_items"]
        names = [item["product_name"] for item in items]
        self.assertEqual(len(items), 52)
        self.assertNotIn("statements", result)
        self.assertEqual(result["stockist_name"], "DIVYA PHARMA HYDERABAD")
        self.assertEqual(result["company_name"], "HIMALAYA ZENITH")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-30")
        self.assertFalse(any(name.lower().startswith("company") for name in names))
        self.assertEqual(names[0], "BONI SPAZ")

        boni = items[0]
        self.assertEqual(boni["packing"], "15 ml")
        self.assertEqual(boni["opening_qty"], 0)
        self.assertEqual(boni["sales_qty"], 0)
        self.assertEqual(boni["sales_value"], 0)
        self.assertEqual(boni["closing_value"], 0)
        self.assertEqual(boni["extra"]["age_days"], 2567)

        liquid = next(item for item in items if item["product_name"] == "BONNISAN LIQUID")
        self.assertEqual(liquid["packing"], "100 ml")
        self.assertEqual(liquid["opening_qty"], 48)
        self.assertEqual(liquid["receipts_qty"], 0)
        self.assertEqual(liquid["sales_qty"], 0)
        self.assertEqual(liquid["sales_value"], 0)
        self.assertEqual(liquid["closing_qty"], 48)
        self.assertEqual(liquid["closing_value"], 2726.14)

        cystone = next(item for item in items if item["product_name"] == "CYSTONE TAB")
        self.assertEqual(cystone["opening_qty"], 200)
        self.assertEqual(cystone["sales_qty"], 27)
        self.assertEqual(cystone["closing_qty"], 173)
        self.assertEqual(cystone["closing_value"], 28385.27)

        diare = next(item for item in items if item["product_name"] == "DIAREX SYP")
        self.assertEqual(diare["opening_qty"], -1)
        self.assertEqual(diare["closing_qty"], -1)
        self.assertEqual(diare["closing_value"], -34.32)

        self.assertEqual(result["totals"]["sales_value"], 10152.64)
        self.assertEqual(result["totals"]["closing_value"], 88370.39)
        self.assertAlmostEqual(
            sum(item["closing_value"] for item in items), 88370.39, places=2
        )
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "profitmaker_ostk_docx",
        )


if __name__ == "__main__":
    unittest.main()
