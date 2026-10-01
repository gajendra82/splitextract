"""Sideways STOCK & SALES ANALYSIS photo with DUMP and M.EXP."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.sales_statement_extractor import (
    _ssa_mexp_finish_values,
    _ssa_mexp_photo_header,
    _ssa_mexp_photo_loose_json,
    _ssa_mexp_values_dropped,
    empty_result,
)


class TestSsaMexpPhotoDetector(unittest.TestCase):
    def test_header_requires_dump_and_mexp(self):
        text = (
            "M PHARMA\nSTOCK & SALES ANALYSIS\n"
            "OPENING RECEIPT ISSUE CLOSING DUMP\nQTY VALUE M.EXP\n"
        )
        self.assertTrue(_ssa_mexp_photo_header(text))

    def test_value_pairs_match_when_dump_heading_is_missed(self):
        text = (
            "M PHARMA\nSTOCK & SALES ANALYSIS\n"
            "OPENING RECEIPT\nQTY VALUE QTY VALUE QTY VALUE M.EXP\n"
        )
        self.assertTrue(_ssa_mexp_photo_header(text))

    def test_other_stock_sheets_do_not_match(self):
        self.assertFalse(
            _ssa_mexp_photo_header(
                "PROMPT Stock Statement Datewise\nOpStk Pur Sales ClStk\n"
            )
        )
        self.assertFalse(
            _ssa_mexp_photo_header("CLOSING STOCK\nPRODUCT NAME CLOSING STOCK\n")
        )
        self.assertFalse(
            _ssa_mexp_photo_header("STOCK & SALES ANALYSIS\nOPENING RECEIPT ISSUE\n")
        )
        self.assertFalse(
            _ssa_mexp_photo_header(
                "STOCK & SALES ANALYSIS\nITEM DESCRIPTION PACK OPENING RECEIPT M.EXP\n"
            )
        )

    def test_truncated_band_json_still_keeps_finished_rows(self):
        text = """```json
{"report_title":"STOCK & SALES ANALYSIS","line_items":[
{"product_name":"BACTARIL SOAP","packing":"75GM","opening_qty":91,"opening_value":5836.69},
{"product_name":"ABANA TAB","packing":"1X60","opening_qty":45
"""
        parsed = _ssa_mexp_photo_loose_json(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["line_items"][0]["product_name"], "BACTARIL SOAP")
        self.assertEqual(len(parsed["line_items"]), 1)

    def test_dropped_opening_and_receipt_values_need_reread(self):
        items = []
        for index in range(8):
            items.append(
                {
                    "product_name": f"ITEM {index}",
                    "opening_qty": 1.0,
                    "opening_value": None,
                    "receipts_qty": 2.0,
                    "receipts_value": None,
                    "sales_value": 10.0,
                    "closing_value": 20.0,
                    "extra": {"dump_m_exp": "5/27"},
                }
            )
        result = {
            "report_title": "STOCK & SALES ANALYSIS",
            "line_items": items,
        }
        self.assertTrue(_ssa_mexp_values_dropped(result))
        result["line_items"][0]["opening_value"] = 100.0
        result["line_items"][0]["receipts_value"] = 50.0
        result["line_items"][1]["opening_value"] = 10.0
        result["line_items"][1]["receipts_value"] = 5.0
        result["line_items"][2]["opening_value"] = 8.0
        result["line_items"][2]["receipts_value"] = 4.0
        result["line_items"][3]["opening_value"] = 6.0
        result["line_items"][3]["receipts_value"] = 3.0
        self.assertFalse(_ssa_mexp_values_dropped(result))

    def test_finish_copies_values_and_sums_totals(self):
        result = empty_result("sheet.jpeg", "jpeg")
        result["line_items"] = [
            {
                "product_name": "BONNISAN 100ML",
                "opening_value": None,
                "receipts_value": None,
                "sales_value": 511.37,
                "closing_value": 2933.13,
                "extra": {"opening_value": 64.0, "receipts_value": 3380.5},
            },
            {
                "product_name": "CYSTONE TAB",
                "opening_value": 12.5,
                "receipts_value": 40.0,
                "sales_value": 100.0,
                "closing_value": 200.0,
                "extra": {},
            },
        ]
        result["totals"]["sales_value"] = 99999.0
        finished = _ssa_mexp_finish_values(result)
        self.assertEqual(finished["line_items"][0]["opening_value"], 64.0)
        self.assertEqual(finished["line_items"][0]["receipts_value"], 3380.5)
        self.assertEqual(finished["line_items"][1]["opening_value"], 12.5)
        self.assertEqual(finished["totals"]["sales_value"], 611.37)
        self.assertEqual(finished["totals"]["closing_value"], 3133.13)
        self.assertEqual(
            finished["totals"]["extra"]["total_row_source"], "product_row_sum"
        )

    def test_generic_fallback_does_not_replace_this_reader(self):
        from services.gemini_extraction_fallback import maybe_apply_gemini_fallback

        result = empty_result("prakash.jpeg", "jpeg")
        result["line_items"] = [
            {
                "product_name": "BONNISAN 100ML",
                "opening_value": 64.0,
                "receipts_value": 3380.5,
                "sales_value": 511.37,
                "closing_value": 2933.13,
                "extra": {},
            }
        ]
        result["totals"]["extra"]["extraction_method"] = "ssa_mexp_photo"
        result["totals"]["extra"]["layout"] = "ssa_opening_receipt_issue_value_mexp"
        with patch(
            "services.gemini_extraction_fallback.evaluate_extraction_quality",
            return_value={"should_fallback": True, "reasons": ["stock_identity_failure"], "score": 75, "quality": "poor"},
        ):
            kept = maybe_apply_gemini_fallback(result, b"\xff\xd8", "prakash.jpeg", ".jpeg")
        self.assertEqual(kept["line_items"][0]["opening_value"], 64.0)
        self.assertEqual(kept["line_items"][0]["receipts_value"], 3380.5)
        self.assertEqual(kept["totals"]["extra"]["gemini_fallback"], "not_called")


if __name__ == "__main__":
    unittest.main()
