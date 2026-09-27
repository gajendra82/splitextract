"""Photographed PROMPT Datewise statements keep OpStk on the product row."""

import unittest

from services.sales_statement_extractor import (
    _datewise_photo_openings_missing,
    _repair_datewise_photo_qty,
)


class DatewisePhotoDetectorTests(unittest.TestCase):
    def test_missing_opening_on_stocked_rows_is_reread(self):
        items = [
            {"product_name": "ARJUNA TABLETS", "opening_qty": 27, "closing_qty": 22},
        ]
        items.extend(
            {
                "product_name": f"ROW {index}",
                "opening_qty": 0,
                "closing_qty": 5,
                "closing_value": 100,
            }
            for index in range(8)
        )
        result = {
            "report_title": "Stock Statement (Datewise)",
            "line_items": items,
        }
        self.assertTrue(_datewise_photo_openings_missing(result))

    def test_filled_openings_are_left_alone(self):
        items = [
            {
                "product_name": f"ROW {index}",
                "opening_qty": 10,
                "closing_qty": 4,
            }
            for index in range(12)
        ]
        result = {
            "report_title": "Stock Statement (Datewise)",
            "line_items": items,
        }
        self.assertFalse(_datewise_photo_openings_missing(result))

    def test_other_titles_are_not_this_photo(self):
        items = [
            {"product_name": f"ROW {index}", "opening_qty": 0, "sales_qty": 3}
            for index in range(10)
        ]
        self.assertFalse(
            _datewise_photo_openings_missing(
                {"report_title": "STOCK & SALES ANALYSIS", "line_items": items}
            )
        )

    def test_blank_pur_is_not_copied_from_sales(self):
        item = {
            "opening_qty": 12,
            "receipts_qty": 3,
            "sales_qty": 3,
            "closing_qty": 9,
        }
        _repair_datewise_photo_qty(item)
        self.assertEqual(item["receipts_qty"], 0)
        self.assertEqual(item["opening_qty"], 12)

    def test_dropped_opening_is_restored_from_the_row(self):
        item = {
            "opening_qty": 0,
            "receipts_qty": 0,
            "sales_qty": 1,
            "closing_qty": 8,
        }
        _repair_datewise_photo_qty(item)
        self.assertEqual(item["opening_qty"], 9)

    def test_real_zero_opening_stays_zero(self):
        item = {
            "opening_qty": 0,
            "receipts_qty": 12,
            "sales_qty": 0,
            "closing_qty": 12,
        }
        _repair_datewise_photo_qty(item)
        self.assertEqual(item["opening_qty"], 0)
        self.assertEqual(item["receipts_qty"], 12)


if __name__ == "__main__":
    unittest.main()
