"""Sideways STOCK & SALES ANALYSIS photo with DUMP and M.EXP."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _ssa_mexp_photo_header,
    _ssa_mexp_photo_loose_json,
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


if __name__ == "__main__":
    unittest.main()
