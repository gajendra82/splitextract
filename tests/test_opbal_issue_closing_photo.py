"""A blank header-driven Op.Bal sheet is reread. Sheets with quantities stay."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _header_driven_stock_qty_blank,
    _opbal_issue_closing_photo_usable,
    empty_result,
)


def _blank_sheet():
    result = empty_result("arup.jpg", "jpg")
    result["line_items"] = [
        {
            "product_name": f"ITEM {index}",
            "packing": "60'S",
            "opening_qty": 0,
            "receipts_qty": 0,
            "sales_qty": 0,
            "closing_qty": 0,
        }
        for index in range(20)
    ]
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "header_driven_stock_photo"
    extra["column_roles"] = ["opening_qty", "receipts_qty", "sales_qty", "closing_qty"]
    return result


class TestOpbalIssueClosingPhoto(unittest.TestCase):
    def test_all_zero_header_driven_rows_are_reread(self):
        self.assertTrue(_header_driven_stock_qty_blank(_blank_sheet()))

    def test_rows_with_quantities_stay_on_header_driven(self):
        result = _blank_sheet()
        for item in result["line_items"]:
            item["opening_qty"] = 12
            item["closing_qty"] = 11
            item["sales_qty"] = 1
        self.assertFalse(_header_driven_stock_qty_blank(result))

    def test_usable_reread_must_balance(self):
        result = empty_result("arup.jpg", "jpg")
        result["line_items"] = [
            {
                "opening_qty": 94,
                "receipts_qty": 0,
                "sales_qty": 19,
                "closing_qty": 75,
            }
            for _ in range(12)
        ]
        self.assertTrue(_opbal_issue_closing_photo_usable(result))
        for item in result["line_items"]:
            item["closing_qty"] = 0
        self.assertFalse(_opbal_issue_closing_photo_usable(result))


if __name__ == "__main__":
    unittest.main()
