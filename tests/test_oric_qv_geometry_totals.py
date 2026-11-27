"""ORIC qty|value header-x grid + dual LMS totals scrub (layout family).

Printed TOTAL example (STOCK & SALES STATEMENT footer bar):
opening 1472 / receive 413 / issue 683 / closing 1202 (+ VALUE cells).
"""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _main_stock_apply_balanced_line_qty_totals,
    _main_stock_assign_oric_qv_row_by_centres,
    _main_stock_extract_oric_totals_vals,
    _main_stock_finalize_oric_grid_totals,
    _main_stock_oric_qv_header_centres,
    _main_stock_oric_qv_phrase_role,
    _main_stock_scrub_misaligned_oric_rows,
    _main_stock_totals_match_product_row,
    empty_line_item,
    empty_result,
)


class OricQvGeometryAlignmentTests(unittest.TestCase):
    def test_header_phrases_map_to_qty_value_roles(self):
        self.assertEqual(_main_stock_oric_qv_phrase_role("OPENING STOCK"), "opening_qty")
        self.assertEqual(_main_stock_oric_qv_phrase_role("OPENING VALUE"), "opening_value")
        self.assertEqual(
            _main_stock_oric_qv_phrase_role("RECEIVE QUANTITY"), "receipts_qty"
        )
        self.assertEqual(_main_stock_oric_qv_phrase_role("ISSUE VALUE"), "sales_value")
        self.assertEqual(
            _main_stock_oric_qv_phrase_role("LAST MONTH SALE"), "lms"
        )

    def test_dual_lms_header_centres_keep_two_trail_slots(self):
        words = []
        # Synthetic header band y=40.
        labels = [
            (40, "PRODUCT"),
            (120, "OPENING"),
            (150, "STOCK"),
            (200, "OPENING"),
            (240, "VALUE"),
            (300, "RECEIVE"),
            (350, "QUANTITY"),
            (400, "RECEIVE"),
            (440, "VALUE"),
            (500, "ISSUE"),
            (540, "QUANTITY"),
            (600, "ISSUE"),
            (640, "VALUE"),
            (700, "CLOSING"),
            (740, "STOCK"),
            (800, "CLOSING"),
            (840, "VALUE"),
            (900, "LAST"),
            (930, "MONTH"),
            (970, "SALE"),
            (1020, "LAST"),
            (1050, "MONTH"),
            (1090, "SALE"),
        ]
        for cx, text in labels:
            words.append({"text": text, "cx": float(cx), "cy": 40.0, "left": float(cx)})
        centres = _main_stock_oric_qv_header_centres(words)
        self.assertIsNotNone(centres)
        assert centres is not None
        for key in (
            "opening_qty",
            "opening_value",
            "receipts_qty",
            "receipts_value",
            "sales_qty",
            "sales_value",
            "closing_qty",
            "closing_value",
            "lms",
            "lms_value",
        ):
            self.assertIn(key, centres)
        self.assertLess(centres["opening_qty"], centres["opening_value"])
        self.assertLess(centres["lms"], centres["lms_value"])

    def test_row_assign_keeps_qty_value_pairs_and_dual_lms(self):
        centres = {
            "opening_qty": 100.0,
            "opening_value": 160.0,
            "receipts_qty": 220.0,
            "receipts_value": 280.0,
            "sales_qty": 340.0,
            "sales_value": 400.0,
            "closing_qty": 460.0,
            "closing_value": 520.0,
            "lms": 580.0,
            "lms_value": 640.0,
        }
        nums = [
            {"text": "66", "cx": 100.0},
            {"text": "4150", "cx": 160.0},
            {"text": "0", "cx": 220.0},
            {"text": "0", "cx": 280.0},
            {"text": "5", "cx": 340.0},
            {"text": "314", "cx": 400.0},
            {"text": "61", "cx": 460.0},
            {"text": "3836", "cx": 520.0},
            {"text": "0", "cx": 580.0},
            {"text": "0", "cx": 640.0},
        ]
        item = _main_stock_assign_oric_qv_row_by_centres(
            "BONNISAN DROPS 30ML", nums, centres
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item["opening_qty"], 66.0)
        self.assertEqual(item["opening_value"], 4150.0)
        self.assertEqual(item["sales_qty"], 5.0)
        self.assertEqual(item["sales_value"], 314.0)
        self.assertEqual(item["closing_qty"], 61.0)
        self.assertEqual(item["closing_value"], 3836.0)
        self.assertEqual(item["extra"]["lms"], 0.0)
        self.assertEqual(item["extra"]["lms_value"], 0.0)


class OricGridTotalsScrubTests(unittest.TestCase):
    def test_footer_peels_printed_oric_totals(self):
        footer = (
            "TOTAL 1472 101575.00 413 44983.00 683 88182.00 1202 149248.00 0 0"
        )
        vals = _main_stock_extract_oric_totals_vals(footer)
        self.assertIsNotNone(vals)
        assert vals is not None
        self.assertEqual(vals[0], 1472.0)
        self.assertEqual(vals[2], 413.0)
        self.assertEqual(vals[4], 683.0)
        self.assertEqual(vals[6], 1202.0)

    def test_scrub_clears_absurd_issue_qty(self):
        result = empty_result("oric.jpg", "jpg")
        item = empty_line_item()
        item["product_name"] = "BRESOL TABLETS"
        item["opening_qty"] = 36.0
        item["receipts_qty"] = 0.0
        item["sales_qty"] = 71907.0
        item["sales_value"] = 1307.0
        item["closing_qty"] = 33.0
        item["closing_value"] = 3630.0
        item["opening_value"] = 4066.0
        result["line_items"].append(item)
        fixed = _main_stock_scrub_misaligned_oric_rows(result)
        self.assertEqual(fixed, 1)
        self.assertEqual(result["line_items"][0]["sales_qty"], 0.0)

    def test_finalize_rejects_product_row_totals_then_balanced_line(self):
        result = empty_result("oric.jpg", "jpg")
        # Vision glued EVECARE into totals.
        result["totals"]["opening_qty"] = 173.0
        result["totals"]["sales_qty"] = 120.0
        result["totals"]["closing_qty"] = 53.0
        result["totals"]["sales_value"] = 16554.0
        result["totals"]["extra"]["total_row_source"] = "main_stock_vision_totals"
        samples = [
            (66, 0, 5, 61, 4150, 314, 3836),
            (82, 0, 20, 62, 5138, 1142, 3996),
            (24, 0, 4, 20, 2229, 405, 1824),
            (36, 0, 3, 33, 4066, 1307, 3630),
            (53, 0, 3, 50, 7873, 503, 7370),
            (61, 50, 60, 51, 2935, 2887, 2454),
            (62, 0, 20, 42, 6658, 2148, 4510),
            (28, 0, 12, 16, 4869, 2087, 2782),
            (173, 0, 120, 53, 23865, 16554, 7311),
            (14, 70, 33, 51, 2064, 4864, 7517),
        ]
        for oq, rq, sq, cq, ov, sv, cv in samples:
            item = empty_line_item()
            item["product_name"] = f"SKU-{oq}-{sq}"
            item["opening_qty"] = float(oq)
            item["receipts_qty"] = float(rq)
            item["sales_qty"] = float(sq)
            item["closing_qty"] = float(cq)
            item["opening_value"] = float(ov)
            item["sales_value"] = float(sv)
            item["closing_value"] = float(cv)
            item["extra"]["opening_value"] = float(ov)
            result["line_items"].append(item)
        self.assertTrue(_main_stock_totals_match_product_row(result))
        _main_stock_finalize_oric_grid_totals(result, file_bytes=None)
        self.assertTrue(
            _main_stock_apply_balanced_line_qty_totals(result)
            or float(result["totals"].get("opening_qty") or 0) >= 400
        )
        # After finalize, totals must not remain the EVECARE product glue.
        self.assertNotEqual(float(result["totals"].get("opening_qty") or 0), 173.0)
        self.assertGreaterEqual(float(result["totals"].get("opening_qty") or 0), 400.0)


if __name__ == "__main__":
    unittest.main()
