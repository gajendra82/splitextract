"""SwilERP Sales & Stock Statement (Op.Bal/Issue/Closing) format helpers."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_opbal_issue_closing_format,
    _is_swilerp_sales_stock_statement,
    _looks_like_packing_token,
    _parse_opbal_receipt_issue_row,
    _repair_swilerp_vision_item,
    _split_stacked_statement_image_pages,
    empty_line_item,
)

SAMPLE = Path(
    r"C:\Users\anuja\Downloads\ZA_2026_August\0000700236_2026_08_ZA_18_539_07092026142715.jpg"
)


class TestSwilERPHelpers(unittest.TestCase):
    def test_detects_opbal_ocr_mangling(self):
        text = (
            "BHAWANI SHANKAR MEDICAL HALL\n"
            "Sales & Stock Statement(From 01/08/2026 Upto 31/08/2026)\n"
            "Sr. PRODUCTNAME PACKING Opal. Receipt Total Issue Closing Dump Near Remain Day\n"
        )
        self.assertTrue(_is_opbal_issue_closing_format(text))
        self.assertTrue(_is_swilerp_sales_stock_statement(text))

    def test_packing_tokens(self):
        self.assertTrue(_looks_like_packing_token("30ML"))
        self.assertTrue(_looks_like_packing_token("60'S"))
        self.assertTrue(_looks_like_packing_token("50GR"))
        self.assertTrue(_looks_like_packing_token("100GR"))
        self.assertFalse(_looks_like_packing_token("S"))

    def test_row_strips_serial_and_maps_issue(self):
        row = _parse_opbal_receipt_issue_row(
            "1 ARJUNA TAB 50S 9 0 9 6 3 3 0 16",
            layout="issue_closing_dump",
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["product_name"], "ARJUNA TAB")
        self.assertEqual(row["opening_qty"], 9.0)
        self.assertEqual(row["receipts_qty"], 0.0)
        self.assertEqual(row["sales_qty"], 6.0)  # Issue, not Total
        self.assertEqual(row["closing_qty"], 3.0)
        self.assertEqual(row["extra"].get("dump_qty"), 3.0)

    def test_negative_issue_allowed(self):
        row = _parse_opbal_receipt_issue_row(
            "63 SHATAVARI TAB 60S 56 0 56 -1 57 57 0 0",
            layout="issue_closing_dump",
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["sales_qty"], -1.0)
        self.assertEqual(row["closing_qty"], 57.0)

    def test_repair_issue_closing_swap_via_remain_day(self):
        item = empty_line_item()
        item["opening_qty"] = 96.0
        item["sales_qty"] = 2.0
        item["closing_qty"] = 94.0
        item["extra"] = {"dump_qty": 0, "remain_day_stock": 1}
        _repair_swilerp_vision_item(item)
        self.assertEqual(item["sales_qty"], 94.0)
        self.assertEqual(item["closing_qty"], 2.0)

    def test_repair_issue_closing_swap_via_dump(self):
        item = empty_line_item()
        item["opening_qty"] = 9.0
        item["sales_qty"] = 3.0
        item["closing_qty"] = 6.0
        item["extra"] = {"dump_qty": 3, "remain_day_stock": 16}
        _repair_swilerp_vision_item(item)
        self.assertEqual(item["sales_qty"], 6.0)
        self.assertEqual(item["closing_qty"], 3.0)

    def test_repair_does_not_swap_large_remain_correct_row(self):
        item = empty_line_item()
        item["opening_qty"] = 71.0
        item["sales_qty"] = 15.0
        item["closing_qty"] = 56.0
        item["extra"] = {"dump_qty": 0, "remain_day_stock": 116}
        _repair_swilerp_vision_item(item)
        self.assertEqual(item["sales_qty"], 15.0)
        self.assertEqual(item["closing_qty"], 56.0)

    def test_repair_lost_minus_on_return(self):
        item = empty_line_item()
        item["opening_qty"] = 56.0
        item["sales_qty"] = 1.0
        item["closing_qty"] = 57.0
        item["extra"] = {"dump_qty": 57, "remain_day_stock": 0}
        _repair_swilerp_vision_item(item)
        self.assertEqual(item["sales_qty"], -1.0)


@unittest.skipUnless(SAMPLE.exists(), "Bhawani Shankar SwilERP sample JPG missing")
class TestSwilERPPageSplit(unittest.TestCase):
    def test_splits_tall_two_page_scan(self):
        pages = _split_stacked_statement_image_pages(SAMPLE.read_bytes())
        self.assertEqual(len(pages), 2)
        self.assertGreater(len(pages[0]), 1000)
        self.assertGreater(len(pages[1]), 1000)


if __name__ == "__main__":
    unittest.main()
