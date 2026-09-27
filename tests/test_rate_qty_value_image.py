"""Photographed STOCK & SALES ANALYSIS with RATE and QTY/VALUE columns."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_rate_qty_value_header_text,
    _parse_rate_qty_value_image,
)


PHOTO = Path("/var/www/html/splitextract/0000734021_2026_08_ZL_04_342_03092026164551.jpg")


class TestRateQtyValueHeader(unittest.TestCase):
    def test_requires_rate_and_qty_value(self):
        self.assertTrue(
            _is_rate_qty_value_header_text(
                "STOCK & SALES ANALYSIS\nRATE OPENING RECEIPT\nQTY VALUE QTY VALUE"
            )
        )
        self.assertFalse(
            _is_rate_qty_value_header_text(
                "STOCK & SALES ANALYSIS\nITEM DESCRIPTION OPENING RECEIPT ISSUE CLOSING M.EXP"
            )
        )
        self.assertFalse(
            _is_rate_qty_value_header_text("Zandra\nORDER FORM\nFrom:")
        )


@unittest.skipUnless(PHOTO.is_file(), "missing Dawaghar photo")
class TestDawagharQtyValuePhoto(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _parse_rate_qty_value_image(PHOTO.read_bytes(), PHOTO.name, ".jpg")

    def test_bonnisan_drops_keeps_rate_out_of_receipts(self):
        self.assertIsNotNone(self.result)
        drops = next(
            item
            for item in self.result["line_items"]
            if "BONNISAN DROPS" in item["product_name"].upper()
        )
        self.assertEqual(drops["opening_qty"], 50.0)
        self.assertEqual(drops["receipts_qty"], 0.0)
        self.assertEqual(drops["sales_qty"], 10.0)
        self.assertEqual(drops["closing_qty"], 40.0)
        self.assertAlmostEqual(drops["opening_value"], 3120.79, places=2)
        self.assertAlmostEqual(drops["sales_value"], 667.06, places=2)
        self.assertAlmostEqual(drops["closing_value"], 2496.63, places=2)
        self.assertNotEqual(drops["receipts_qty"], 62.42)
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertIn("DAWAGHAR", self.result["stockist_name"])
        names = " ".join(item["product_name"].upper() for item in self.result["line_items"])
        self.assertNotIn("STOCK & SALES", names)


if __name__ == "__main__":
    unittest.main()
