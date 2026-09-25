"""PROMPT Datewise variant where Sales Qty sits inside the Pur x-range."""

import unittest

from services.sales_statement_extractor import (
    _PROMPT_DATEWISE_BUCKETS,
    _prompt_datewise_bucket,
    _prompt_datewise_buckets_for_words,
)


class TestPromptDatewiseSalesQty(unittest.TestCase):
    def test_standard_ranges_stay_when_sales_qty_is_right_of_285(self):
        words = [
            (299, 90, 330, 100, "Sales"),
            (245, 100, 260, 110, "Qty"),
            (290, 100, 310, 110, "Qty"),
        ]
        self.assertEqual(
            _prompt_datewise_buckets_for_words(words),
            _PROMPT_DATEWISE_BUCKETS,
        )

    def test_sales_qty_left_of_285_is_not_receipts(self):
        words = [
            (239, 90, 255, 100, "Pur"),
            (299, 90, 330, 100, "Sales"),
            (239, 100, 255, 110, "Qty"),
            (275, 100, 290, 110, "Qty"),
            (307, 100, 330, 110, "Free"),
        ]
        buckets = _prompt_datewise_buckets_for_words(words)
        self.assertEqual(_prompt_datewise_bucket(251, buckets), "receipts_qty")
        self.assertEqual(_prompt_datewise_bucket(273, buckets), "sales_qty")
        self.assertEqual(_prompt_datewise_bucket(283, buckets), "sales_qty")
        self.assertNotEqual(_prompt_datewise_bucket(323, buckets), "sales_qty")


if __name__ == "__main__":
    unittest.main()
