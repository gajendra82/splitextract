"""SwilERP Sales & Stock Statement (Op.Bal/Issue/Closing) format helpers."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from services.sales_statement_extractor import (
    _apply_total_row_to_result,
    _is_opbal_issue_closing_format,
    _is_swilerp_sales_stock_statement,
    _looks_like_packing_token,
    _opbal_layout,
    _parse_opbal_receipt_issue_row,
    _repair_swilerp_vision_item,
    _split_stacked_statement_image_pages,
    empty_line_item,
    empty_result,
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

    def test_near_expiry_header_stays_dump_layout(self):
        text = (
            "Sales & Stock Statement\n"
            "PRODUCT NAME PACKING Op.Bal. Qty. Receipt Qty. Total Qty. "
            "Issue Qty. Closing Balance Dump Stock Near Expiry Remain Day\n"
        )
        self.assertEqual(_opbal_layout(text), "issue_closing_dump")

    def test_rejects_implausible_footer_qty_totals(self):
        """Phone OCR GRAND TOTAL glue must not overwrite opening/closing totals."""
        result = empty_result("tulsiyan.jpg", "jpg")
        result["totals"]["extra"]["extraction_method"] = "swilerp_page_split_vision"
        for op, rec, sale, cl in (
            (20.0, 0.0, 0.0, 20.0),
            (110.0, 1.0, 46.0, 65.0),
            (50.0, 0.0, 34.0, 16.0),
            (69.0, 0.0, 25.0, 44.0),
        ):
            item = empty_line_item()
            item["product_name"] = "X"
            item["opening_qty"] = op
            item["receipts_qty"] = rec
            item["sales_qty"] = sale
            item["closing_qty"] = cl
            result["line_items"].append(item)
        text = (
            "PRODUCT NAME PACKING Op.Bal Receipt Total Issue Closing Dump Near Expiry\n"
            "GRAND TOTAL, | | s7isel 276461 0! + sas32i 2604! oo! ol 613321 29187! ol\n"
        )
        with mock.patch.dict(
            os.environ, {"STOCK_REJECT_IMPLAUSIBLE_FOOTER_QTY_TOTALS": "true"}
        ):
            out = _apply_total_row_to_result(result, text)
        totals = out["totals"]
        self.assertEqual(totals["opening_qty"], 249.0)
        self.assertEqual(totals["receipts_qty"], 1.0)
        self.assertEqual(totals["sales_qty"], 105.0)
        self.assertEqual(totals["closing_qty"], 145.0)
        self.assertEqual(totals["extra"].get("total_row_source"), "line_item_sum")
        self.assertEqual(
            totals["extra"].get("total_row_rejected"), "closing_qty_mismatch"
        )

    def test_plausible_footer_qty_totals_kept(self):
        result = empty_result("ok.jpg", "jpg")
        result["totals"]["extra"]["extraction_method"] = "swilerp_page_split_vision"
        item = empty_line_item()
        item["product_name"] = "X"
        item["opening_qty"] = 100.0
        item["receipts_qty"] = 20.0
        item["sales_qty"] = 30.0
        item["closing_qty"] = 90.0
        result["line_items"] = [item]
        text = (
            "Op.Bal Receipt Total Issue Closing\n"
            "GRAND TOTAL 100 20 120 30 90\n"
        )
        with mock.patch.dict(
            os.environ, {"STOCK_REJECT_IMPLAUSIBLE_FOOTER_QTY_TOTALS": "true"}
        ):
            out = _apply_total_row_to_result(result, text)
        self.assertEqual(out["totals"]["opening_qty"], 100.0)
        self.assertEqual(out["totals"]["closing_qty"], 90.0)
        self.assertEqual(out["totals"]["extra"].get("total_row_source"), "footer_total")

    def test_single_page_phone_ratio_not_split(self):
        """1000x1600 (~1.6) is one page — must not be halved (Issue/Closing scramble)."""
        from io import BytesIO

        from PIL import Image

        img = Image.new("RGB", (1000, 1600), color=(255, 255, 255))
        buf = BytesIO()
        img.save(buf, format="JPEG")
        with mock.patch.dict(
            os.environ,
            {
                "STOCK_SWILERP_AVOID_SINGLE_PAGE_SPLIT": "true",
                "STOCK_SWILERP_STACK_SPLIT_MIN_RATIO": "1.90",
            },
        ):
            pages = _split_stacked_statement_image_pages(buf.getvalue())
        self.assertEqual(len(pages), 1)

    def test_tall_stacked_scan_still_splits(self):
        from io import BytesIO

        from PIL import Image

        img = Image.new("RGB", (1000, 2200), color=(255, 255, 255))
        buf = BytesIO()
        img.save(buf, format="JPEG")
        with mock.patch.dict(
            os.environ,
            {
                "STOCK_SWILERP_AVOID_SINGLE_PAGE_SPLIT": "true",
                "STOCK_SWILERP_STACK_SPLIT_MIN_RATIO": "1.90",
            },
        ):
            pages = _split_stacked_statement_image_pages(buf.getvalue())
        self.assertEqual(len(pages), 2)

    def test_repair_does_not_swap_when_closing_already_matches_dump(self):
        item = empty_line_item()
        item["opening_qty"] = 14.0
        item["sales_qty"] = 4.0
        item["closing_qty"] = 10.0
        item["extra"] = {"dump_qty": 10, "remain_day_stock": 0}
        _repair_swilerp_vision_item(item)
        self.assertEqual(item["sales_qty"], 4.0)
        self.assertEqual(item["closing_qty"], 10.0)


@unittest.skipUnless(SAMPLE.exists(), "Bhawani Shankar SwilERP sample JPG missing")
class TestSwilERPPageSplit(unittest.TestCase):
    def test_splits_tall_two_page_scan(self):
        pages = _split_stacked_statement_image_pages(SAMPLE.read_bytes())
        self.assertEqual(len(pages), 2)
        self.assertGreater(len(pages[0]), 1000)
        self.assertGreater(len(pages[1]), 1000)


if __name__ == "__main__":
    unittest.main()
