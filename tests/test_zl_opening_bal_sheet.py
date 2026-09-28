"""ZL Opening_bal_qty spreadsheet screenshots keep Secondaryrate and MRP."""

import unittest

from services.sales_statement_extractor import (
    _finalize_zl_opening_bal_sheet,
    _looks_like_zl_opening_bal_sheet,
    empty_line_item,
    empty_result,
)


def _sheet_result():
    result = empty_result("stmt.png", "png")
    result["report_title"] = "Sheet1"
    result["line_items"] = []
    for name, opening in (
        ("CONFIDO TABS (FC) 60s (AG)", 100),
        ("DIABECON TABS (DS) 60s", 20),
        ("HERBOLAX CAPSULES 10s", 50),
    ):
        item = empty_line_item()
        item["product_name"] = name
        item["opening_qty"] = opening
        item["closing_qty"] = opening
        result["line_items"].append(item)
    return result


class TestZlOpeningBalSheet(unittest.TestCase):
    def test_detector_accepts_sheet_screenshot_only(self):
        self.assertTrue(_looks_like_zl_opening_bal_sheet(_sheet_result()))
        other = _sheet_result()
        other["report_title"] = "Product Stock Report"
        self.assertFalse(_looks_like_zl_opening_bal_sheet(other))
        with_sales = _sheet_result()
        with_sales["line_items"][0]["sales_qty"] = 2
        self.assertFalse(_looks_like_zl_opening_bal_sheet(with_sales))

    def test_finalize_keeps_rate_and_mrp(self):
        result = empty_result("stmt.png", "png")
        item = empty_line_item()
        item["product_name"] = "CONFIDO TABS (FC) 60s (AG)"
        item["opening_qty"] = 100
        item["receipts_qty"] = 0
        item["sales_qty"] = 15
        item["closing_qty"] = 85
        item["extra"] = {"mrp": "255", "unit_rate": "172.23"}
        result["line_items"] = [item]
        done = _finalize_zl_opening_bal_sheet(result)
        row = done["line_items"][0]
        self.assertEqual(row["sales_qty"], 0.0)
        self.assertEqual(row["opening_qty"], 100)
        self.assertEqual(row["closing_qty"], 85)
        self.assertEqual(row["extra"]["mrp"], 255.0)
        self.assertEqual(row["extra"]["unit_rate"], 172.23)
        self.assertEqual(row["extra"]["layout"], "zl_opening_primary_closing")
        self.assertEqual(row["closing_value"], round(85 * 172.23, 2))
        self.assertEqual(
            done["totals"]["extra"]["extraction_method"],
            "zl_opening_bal_sheet_vision",
        )


if __name__ == "__main__":
    unittest.main()
