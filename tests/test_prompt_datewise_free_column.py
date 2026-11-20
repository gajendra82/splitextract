"""Regression: PROMPT Datewise with Free column (Gayatri Distributors)."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import extract_sales_statement

FIX = Path(
    r"C:\Users\amnsa\Downloads\ZL_2026_August"
    r"\0000700060_2026_08_ZL_06_748_04092026113445.pdf"
)


@unittest.skipUnless(FIX.is_file(), "Gayatri Datewise PDF fixture missing")
class TestPromptDatewiseFreeColumn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIX.read_bytes(), FIX.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}

    def test_method_and_period(self):
        self.assertEqual(self.extra.get("extraction_method"), "prompt_datewise_layout")
        self.assertEqual(self.result.get("period_from"), "2026-08-01")
        self.assertEqual(self.result.get("period_to"), "2026-08-31")
        self.assertGreaterEqual(len(self.result.get("line_items") or []), 50)

    def test_free_maps_to_sales_scheme_and_identity(self):
        liv = next(
            i
            for i in self.result["line_items"]
            if i.get("product_name") == "LIV.52 TABLETS"
        )
        self.assertEqual(liv.get("opening_qty"), 320.0)
        self.assertEqual(liv.get("receipts_qty"), 200.0)
        self.assertEqual(liv.get("sales_qty"), 223.0)
        self.assertEqual(liv.get("closing_qty"), 292.0)
        self.assertEqual((liv.get("extra") or {}).get("sales_scheme_qty"), 5.0)
        self.assertTrue((liv.get("extra") or {}).get("stock_identity_ok"))
        self.assertEqual(self.extra.get("stock_identity_fail_count"), 0)

    def test_footer_amounts(self):
        self.assertEqual(self.result["totals"].get("sales_value"), 283497.0)
        self.assertEqual(self.result["totals"].get("closing_value"), 448202.0)
        self.assertEqual(self.extra.get("total_row_source"), "prompt_datewise_footer")
        self.assertEqual(self.extra.get("closing_qty"), 3227.0)
        self.assertEqual(self.extra.get("sales_scheme_qty"), 5.0)


if __name__ == "__main__":
    unittest.main()
