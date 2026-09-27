"""Zandra ORDER FORM photos with a product table on each side."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _zandra_two_column_order_photo,
    _zandra_two_column_order_text,
)


ATUL = Path("0000735937_2026_08_ZA_24_258_01092026065830.jpeg")
SINGH = Path("0000735936_2026_08_ZA_24_256_02092026173543.jpg")
OLDER_SAP = Path("0000732417_2026_08_ZA_07_322_06092026011854.jpeg")


class TestZandraTwoColumnOrderText(unittest.TestCase):
    def test_requires_both_words(self):
        self.assertTrue(
            _zandra_two_column_order_text("Zandra\nORDER FORM\nFrom:")
        )
        self.assertTrue(
            _zandra_two_column_order_text("Zandra at\nORDER FORNIN")
        )
        self.assertFalse(_zandra_two_column_order_text("ORDER FORM\nSAP Code"))
        self.assertFalse(_zandra_two_column_order_text("Zandra\nStock and Sale Statement"))

    @unittest.skipUnless(ATUL.is_file(), "missing Atul order form")
    def test_this_photo_matches(self):
        self.assertTrue(_zandra_two_column_order_photo(ATUL.read_bytes()))

    @unittest.skipUnless(SINGH.is_file(), "missing Dr Singh order form")
    def test_title_below_the_top_of_the_photo_matches(self):
        self.assertTrue(_zandra_two_column_order_photo(SINGH.read_bytes()))

    @unittest.skipUnless(OLDER_SAP.is_file(), "missing older SAP photo")
    def test_older_right_qty_photo_does_not_match(self):
        self.assertFalse(_zandra_two_column_order_photo(OLDER_SAP.read_bytes()))


if __name__ == "__main__":
    unittest.main()
