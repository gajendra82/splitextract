"""Word Sales & Stock Statement with Dump. PDF dump reader is not used."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import extract_sales_statement

FIXTURE = Path("0000732713_2026_08_ZL_25_447_04092026063536.docx")


class TestWellnessDumpDocx(unittest.TestCase):
    def test_medicine_emporium_both_pages(self):
        if not FIXTURE.is_file():
            self.skipTest(f"missing {FIXTURE.name}")
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        items = result["line_items"]
        self.assertEqual(len(items), 53)
        self.assertNotIn("statements", result)
        self.assertEqual(result["stockist_name"], "MEDICINE EMPORIUM")
        self.assertIn("KUCHKUCHIA", result["stockist_address"])
        self.assertEqual(result["company_name"], "HIMALAYA WELLNESS COMPANY")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        self.assertNotEqual(result["period_from"][:7], "2026-09")
        self.assertFalse(
            any("TOTAL" in item["product_name"].upper() for item in items)
        )

        soap = items[0]
        self.assertEqual(soap["product_name"], "AACTARIL SOAP 75 G")
        self.assertEqual(soap["packing"], "75G")
        self.assertEqual(soap["opening_qty"], 38)
        self.assertEqual(soap["sales_qty"], 0)
        self.assertEqual(soap["closing_qty"], 38)
        self.assertEqual(soap["closing_value"], 2637.58)

        drops = next(item for item in items if item["product_name"].startswith("BONNISAN DROPS"))
        self.assertEqual(drops["packing"], "30ML")
        self.assertEqual(drops["opening_qty"], 113)
        self.assertEqual(drops["sales_qty"], 22)
        self.assertEqual(drops["sales_value"], 1456.18)
        self.assertEqual(drops["closing_qty"], 91)
        self.assertEqual(drops["closing_value"], 6023.29)

        self.assertEqual(result["totals"]["sales_value"], 122924.12)
        self.assertEqual(result["totals"]["closing_value"], 360295.45)
        self.assertAlmostEqual(sum(item["sales_value"] for item in items), 122924.12, places=2)
        self.assertAlmostEqual(sum(item["closing_value"] for item in items), 360295.45, places=2)
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "himalaya_wellness_dump_docx",
        )


if __name__ == "__main__":
    unittest.main()
