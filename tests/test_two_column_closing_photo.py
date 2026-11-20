"""Two-column closing-stock photos are not treated as other statement layouts."""

import unittest

from services.sales_statement_extractor import _looks_like_two_column_closing_photo


class TwoColumnClosingPhotoTests(unittest.TestCase):
    def test_closing_stock_sheet_is_detected(self):
        items = [
            {"product_name": f"ROW {index}", "closing_qty": 1 if index < 5 else 0, "sales_qty": 0}
            for index in range(20)
        ]
        result = {"report_title": "CLOSING STOCK", "line_items": items}
        self.assertTrue(_looks_like_two_column_closing_photo(result))

    def test_datewise_and_sales_sheets_are_not_this_photo(self):
        items = [
            {"product_name": f"ROW {index}", "closing_qty": 4, "sales_qty": 0}
            for index in range(20)
        ]
        self.assertFalse(
            _looks_like_two_column_closing_photo(
                {"report_title": "Stock Statement (Datewise)", "line_items": items}
            )
        )
        items[0]["sales_qty"] = 3
        self.assertFalse(
            _looks_like_two_column_closing_photo(
                {"report_title": "CLOSING STOCK", "line_items": items}
            )
        )


if __name__ == "__main__":
    unittest.main()
