"""Dual LAST MONTH SALE columns after CLOSING VALUE stay off ORIC qty slots.

Layout family (not stockist-specific):
PRODUCT DESCRIPTION | OPENING STOCK|VALUE | RECEIVE QTY|VALUE | ISSUE QTY|VALUE |
CLOSING STOCK|VALUE | LAST MONTH SALE | LAST MONTH SALE
"""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _header_driven_oric_money_parked_in_qty,
    _main_stock_parse_ocr_line,
    _main_stock_result_has_printed_opening_value,
    empty_line_item,
    empty_result,
)


class OricDualLmsTrailTests(unittest.TestCase):
    def test_dual_lms_zeros_keep_opening_stock_not_value(self):
        item = _main_stock_parse_ocr_line(
            "BONNISAN DROPS 30ML 30 ML 66 4150 0 0 5 314 61 3836 0 0"
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item["opening_qty"], 66.0)
        self.assertEqual(item["opening_value"], 4150.0)
        self.assertEqual(item["receipts_qty"], 0.0)
        self.assertEqual(item["receipts_value"], 0.0)
        self.assertEqual(item["sales_qty"], 5.0)
        self.assertEqual(item["sales_value"], 314.0)
        self.assertEqual(item["closing_qty"], 61.0)
        self.assertEqual(item["closing_value"], 3836.0)
        self.assertEqual(item["extra"]["lms"], 0.0)
        self.assertNotIn("66", str(item["product_name"]))

    def test_dual_lms_with_receive_keeps_qty_value_pairs(self):
        item = _main_stock_parse_ocr_line(
            "LIV-52 DS SYRUP 100ML 100 ML 14 2064 70 10318 33 4864 51 7517 0 0"
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item["opening_qty"], 14.0)
        self.assertEqual(item["opening_value"], 2064.0)
        self.assertEqual(item["receipts_qty"], 70.0)
        self.assertEqual(item["receipts_value"], 10318.0)
        self.assertEqual(item["sales_qty"], 33.0)
        self.assertEqual(item["sales_value"], 4864.0)
        self.assertEqual(item["closing_qty"], 51.0)
        self.assertEqual(item["closing_value"], 7517.0)

    def test_cystone_forte_dual_lms(self):
        item = _main_stock_parse_ocr_line(
            "CYSTONE FORTE TAB(FC) 1X30 S 62 6658 0 0 20 2148 42 4510 0 0"
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item["opening_qty"], 62.0)
        self.assertEqual(item["opening_value"], 6658.0)
        self.assertEqual(item["sales_qty"], 20.0)
        self.assertEqual(item["sales_value"], 2148.0)
        self.assertEqual(item["closing_qty"], 42.0)
        self.assertEqual(item["closing_value"], 4510.0)

    def test_single_lms_trail_still_peels_one_cell(self):
        item = _main_stock_parse_ocr_line(
            "ARJUNA TAB 0 0 180 39056.82 90 19284.64 90 19859.4 0"
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item["product_name"], "ARJUNA TAB")
        self.assertEqual(item["opening_qty"], 0.0)
        self.assertEqual(item["receipts_qty"], 180.0)
        self.assertEqual(item["sales_qty"], 90.0)
        self.assertEqual(item["closing_qty"], 90.0)
        self.assertEqual(item["extra"]["lms"], 0.0)

    def test_eight_col_katruwar_line_unchanged(self):
        item = _main_stock_parse_ocr_line(
            "BONNISAN DROP 30ML 77 5096.63 0 0 32 2389.12 45 2978.55 01-May-29"
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item["opening_qty"], 77.0)
        self.assertEqual(item["extra"]["opening_value"], 5096.63)
        self.assertEqual(item["sales_qty"], 32.0)
        self.assertEqual(item["closing_qty"], 45.0)


class HeaderDrivenOricMoneyInQtyTests(unittest.TestCase):
    def test_detects_value_parked_in_qty(self):
        result = empty_result("prince.jpg", "jpg")
        result["totals"]["extra"]["extraction_method"] = "header_driven_stock_photo"
        result["totals"]["extra"]["column_roles"] = [
            "opening_qty",
            "opening_value",
            "receipts_qty",
            "receipts_value",
            "sales_qty",
            "sales_value",
            "closing_qty",
            "closing_value",
            "lms",
        ]
        samples = [
            (4150, 0, 314, 3836),
            (5138, 0, 1142, 3996),
            (2229, 0, 405, 1824),
            (4066, 0, 1307, 3630),
            (7873, 0, 503, 7370),
            (2935, 2406, 2887, 2454),
            (6658, 0, 2148, 4510),
            (4869, 0, 2087, 2782),
        ]
        for oq, rq, sq, cq in samples:
            item = empty_line_item()
            item["product_name"] = f"SKU-{oq}"
            item["opening_qty"] = float(oq)
            item["receipts_qty"] = float(rq)
            item["sales_qty"] = float(sq)
            item["closing_qty"] = float(cq)
            item["opening_value"] = None
            item["sales_value"] = None
            item["closing_value"] = None
            result["line_items"].append(item)
        self.assertTrue(_header_driven_oric_money_parked_in_qty(result))

    def test_correct_qty_value_pairs_not_flagged(self):
        result = empty_result("prince.jpg", "jpg")
        result["totals"]["extra"]["extraction_method"] = "header_driven_stock_photo"
        result["totals"]["extra"]["column_roles"] = [
            "opening_qty",
            "opening_value",
            "sales_qty",
            "sales_value",
            "closing_qty",
            "closing_value",
        ]
        samples = [
            (66, 4150, 5, 314, 61, 3836),
            (82, 5138, 20, 1142, 62, 3996),
            (24, 2229, 4, 405, 20, 1824),
            (36, 4066, 3, 1307, 33, 3630),
            (53, 7873, 3, 503, 50, 7370),
            (61, 2935, 60, 2887, 51, 2454),
            (62, 6658, 20, 2148, 42, 4510),
            (28, 4869, 12, 2087, 16, 2782),
        ]
        for oq, ov, sq, sv, cq, cv in samples:
            item = empty_line_item()
            item["product_name"] = f"OK-{oq}"
            item["opening_qty"] = float(oq)
            item["opening_value"] = float(ov)
            item["receipts_qty"] = 0.0
            item["sales_qty"] = float(sq)
            item["sales_value"] = float(sv)
            item["closing_qty"] = float(cq)
            item["closing_value"] = float(cv)
            result["line_items"].append(item)
        self.assertFalse(_header_driven_oric_money_parked_in_qty(result))
        self.assertTrue(_main_stock_result_has_printed_opening_value(result))


if __name__ == "__main__":
    unittest.main()
