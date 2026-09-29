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

    def test_right_shifted_opstk_maps_opening_not_receipts(self):
        """OpStk Qty near x=256 must not land in the fixed receipts band."""
        words = [
            (195.6, 91.3, 216.2, 100, "Pack"),
            (245.9, 91.3, 272.9, 100, "OpStk"),
            (300.6, 91.3, 316.4, 100, "Pur"),
            (326.2, 91.3, 347.4, 100, "Sales"),
            (386.5, 91.3, 410.3, 100, "ClStk"),
            (256.1, 103.3, 271.9, 110, "Qty"),
            (298.1, 103.3, 313.9, 110, "Qty"),
            (340.1, 103.3, 355.9, 110, "Qty"),
            (376.7, 103.3, 392.4, 110, "Qty"),
            (414.0, 103.3, 440.0, 110, "Amount"),
        ]
        buckets = _prompt_datewise_buckets_for_words(words)
        self.assertEqual(_prompt_datewise_bucket(262.1, buckets), "opening_qty")
        self.assertEqual(_prompt_datewise_bucket(312.6, buckets), "receipts_qty")
        self.assertEqual(_prompt_datewise_bucket(352.6, buckets), "sales_qty")
        self.assertEqual(_prompt_datewise_bucket(380.6, buckets), "closing_qty")
        # Compressed layout: no Sales Amount between Sales Qty and ClStk.
        names = [n for n, _lo, _hi in buckets]
        self.assertNotIn("sales_value", names)
        self.assertEqual(_prompt_datewise_bucket(418.1, buckets), "closing_value")

    def test_free_column_gets_sales_scheme_bucket(self):
        """Gayatri-style Datewise: Free between Sales Qty and Amount."""
        words = [
            (159.8, 108.9, 178.8, 118, "Pack"),
            (205.0, 108.9, 230.0, 118, "OpStk"),
            (245.6, 108.9, 260.1, 118, "Pur"),
            (304.1, 108.9, 323.6, 118, "Sales"),
            (396.9, 108.9, 418.9, 118, "ClStk"),
            (215.5, 123.3, 230.0, 132, "Qty"),
            (245.6, 123.3, 260.1, 132, "Qty"),
            (275.5, 123.3, 290.0, 132, "Qty"),
            (302.4, 123.3, 319.9, 132, "Free"),
            (328.6, 122.7, 363.6, 132, "Amount"),
            (386.0, 123.3, 400.5, 132, "Qty"),
            (416.6, 123.3, 448.1, 132, "Amount"),
        ]
        buckets = _prompt_datewise_buckets_for_words(words)
        names = [n for n, _lo, _hi in buckets]
        self.assertIn("sales_scheme_qty", names)
        self.assertEqual(_prompt_datewise_bucket(277.9, buckets), "sales_qty")
        self.assertEqual(_prompt_datewise_bucket(316.1, buckets), "sales_scheme_qty")
        self.assertEqual(_prompt_datewise_bucket(343.7, buckets), "sales_value")
        self.assertEqual(_prompt_datewise_bucket(388.5, buckets), "closing_qty")

    def test_standard_wide_gap_keeps_sales_value(self):
        """Standard OpStk (left) keeps Sales Amount between Sales Qty and ClStk."""
        words = [
            (195.6, 91.3, 216.2, 100, "Pack"),
            (197.2, 91.3, 224.0, 100, "OpStk"),
            (250.7, 91.3, 266.0, 100, "Pur"),
            (301.4, 91.3, 330.0, 100, "Sales"),
            (386.5, 91.3, 410.3, 100, "ClStk"),
            (208.7, 103.3, 224.0, 110, "Qty"),
            (250.7, 103.3, 266.0, 110, "Qty"),
            (292.7, 103.3, 308.0, 110, "Qty"),
            (376.7, 103.3, 392.4, 110, "Qty"),
            (403.2, 103.3, 437.0, 110, "Amount"),
        ]
        buckets = _prompt_datewise_buckets_for_words(words)
        names = [n for n, _lo, _hi in buckets]
        self.assertIn("sales_value", names)
        self.assertNotIn("sales_scheme_qty", names)
        self.assertEqual(_prompt_datewise_bucket(338.1, buckets), "sales_value")
        self.assertEqual(_prompt_datewise_bucket(388.6, buckets), "closing_qty")
        self.assertEqual(_prompt_datewise_bucket(422.1, buckets), "closing_value")


if __name__ == "__main__":
    unittest.main()
