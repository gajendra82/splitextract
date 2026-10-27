"""OPBAL|REC|SALES TOTAL|STOCK EXCESS words path (LST MN/FOR MN are history)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.sales_statement_extractor import (
    _assign_opbal_rec_sales_excess_row,
    _is_opbal_rec_sales_excess_text,
    _looks_like_zandra_stock_sale_text,
    _opbal_rec_sales_excess_header_centres,
    _parse_opbal_rec_sales_excess_words_pages,
)


def _w(x0: float, y0: float, text: str, width: float = 20.0):
    return (x0, y0, x0 + width, y0 + 8.0, text, 0, 0, 0)


SAU_TEXT = """
STOCK & SALES STATEMENT          SAU PHARMA PVT LTD          Page 1/2
For the month Sep 2026                                    30/09/2026
PRODUCT NAME     PACKING   OPBAL  REC.        S A L E S              S T O C K
                                  AVG.  LST MN  FOR MN  TOTAL  EXCESS  Nr. EXP.
HIMALAYA ZANDRA
ARJUNA TAB       60TAB       0     0     2      0       0       0
CYSTONE SYRUP    200ML       2     0     8      6       2       0
EVECARE SYP      200 ML     27     0     1      1       6      21      19
SEPTILIN TAB     60S        55     0    23     21      45      10
"""


class TestOpbalRecSalesExcessDetector(unittest.TestCase):
    def test_sau_layout_matches(self):
        self.assertTrue(_is_opbal_rec_sales_excess_text(SAU_TEXT))

    def test_zandra_does_not_steal_despite_zandra_banner(self):
        self.assertFalse(_looks_like_zandra_stock_sale_text(SAU_TEXT))

    def test_classic_opbal_issue_closing_not_claimed(self):
        classic = """
STOCK & SALES STATEMENT
PRODUCT NAME PACKING Op.Bal Receipt Total Issue Closing Dump
ITEM 60TAB 1 0 1 0 1 0
"""
        self.assertFalse(_is_opbal_rec_sales_excess_text(classic))


class TestOpbalRecSalesExcessAssign(unittest.TestCase):
    def setUp(self):
        header_rows = [
            [
                _w(12, 55, "PRODUCT"),
                _w(59, 55, "NAME"),
                _w(177, 55, "PACKING"),
                _w(235, 55, "OPBAL"),
                _w(286, 55, "REC."),
                _w(348, 55, "S"),
                _w(475, 55, "S"),
            ],
            [
                _w(328, 67, "AVG."),
                _w(359, 67, "LST"),
                _w(377, 67, "MN"),
                _w(401, 67, "FOR"),
                _w(423, 67, "MN"),
                _w(449, 67, "TOTAL"),
                _w(484, 67, "EXCESS"),
                _w(527, 67, "Nr."),
                _w(542, 67, "EXP."),
            ],
        ]
        self.centres = _opbal_rec_sales_excess_header_centres(header_rows)
        self.assertIsNotNone(self.centres)

    def test_total_is_sales_excess_is_closing(self):
        row = [
            _w(12, 100, "CYSTONE"),
            _w(58, 100, "SYRUP"),
            _w(177, 100, "200ML"),
            _w(260, 100, "2"),
            _w(302, 100, "0"),
            _w(344, 100, "8"),
            _w(386, 100, "6"),
            _w(431, 100, "2"),
            _w(473, 100, "0"),
        ]
        item = _assign_opbal_rec_sales_excess_row(row, self.centres)
        self.assertEqual(item["product_name"], "CYSTONE SYRUP")
        self.assertEqual(item["packing"], "200ML")
        self.assertEqual(item["opening_qty"], 2.0)
        self.assertEqual(item["receipts_qty"], 0.0)
        self.assertEqual(item["sales_qty"], 2.0)
        self.assertEqual(item["closing_qty"], 0.0)

    def test_near_expiry_seventh_slot(self):
        row = [
            _w(12, 100, "EVECARE"),
            _w(58, 100, "SYP"),
            _w(177, 100, "200"),
            _w(195, 100, "ML"),
            _w(255, 100, "27"),
            _w(302, 100, "0"),
            _w(344, 100, "1"),
            _w(386, 100, "1"),
            _w(431, 100, "6"),
            _w(468, 100, "21"),
            _w(510, 100, "19"),
        ]
        item = _assign_opbal_rec_sales_excess_row(row, self.centres)
        self.assertEqual(item["product_name"], "EVECARE SYP")
        self.assertEqual(item["packing"], "200 ML")
        self.assertEqual(item["opening_qty"], 27.0)
        self.assertEqual(item["sales_qty"], 6.0)
        self.assertEqual(item["closing_qty"], 21.0)
        self.assertEqual(item["expiry_damage_qty"], 19.0)

    def test_liv_52_keeps_digits_in_name(self):
        row = [
            _w(12, 100, "LIV"),
            _w(28, 100, "52"),
            _w(41, 100, "DS"),
            _w(56, 100, "SYP"),
            _w(177, 100, "200"),
            _w(195, 100, "ML"),
            _w(260, 100, "6"),
            _w(302, 100, "0"),
            _w(339, 100, "13"),
            _w(381, 100, "29"),
            _w(431, 100, "6"),
            _w(473, 100, "0"),
        ]
        item = _assign_opbal_rec_sales_excess_row(row, self.centres)
        self.assertEqual(item["product_name"], "LIV 52 DS SYP")
        self.assertEqual(item["opening_qty"], 6.0)
        self.assertEqual(item["sales_qty"], 6.0)
        self.assertEqual(item["closing_qty"], 0.0)


class TestOpbalRecSalesExcessPages(unittest.TestCase):
    def test_pages_parse_and_flag_off(self):
        words = [
            _w(12, 22, "STOCK"),
            _w(50, 22, "&"),
            _w(60, 22, "SALES"),
            _w(95, 22, "STATEMENT"),
            _w(280, 22, "SAU"),
            _w(303, 22, "PHARMA"),
            _w(12, 55, "PRODUCT"),
            _w(59, 55, "NAME"),
            _w(177, 55, "PACKING"),
            _w(235, 55, "OPBAL"),
            _w(286, 55, "REC."),
            _w(328, 67, "AVG."),
            _w(359, 67, "LST"),
            _w(377, 67, "MN"),
            _w(401, 67, "FOR"),
            _w(423, 67, "MN"),
            _w(449, 67, "TOTAL"),
            _w(484, 67, "EXCESS"),
            _w(527, 67, "Nr."),
            _w(542, 67, "EXP."),
            _w(12, 82, "HIMALAYA"),
            _w(67, 82, "ZANDRA"),
        ]
        # Three product rows
        y = 100
        for name, pack, nums in (
            ("ARJUNA TAB", "60TAB", (0, 0, 2, 0, 0, 0)),
            ("CYSTONE SYRUP", "200ML", (2, 0, 8, 6, 2, 0)),
            ("SEPTILIN TAB", "60S", (55, 0, 23, 21, 45, 10)),
        ):
            parts = name.split()
            x = 12.0
            for p in parts:
                words.append(_w(x, y, p))
                x += 40
            words.append(_w(177, y, pack))
            xs = (260, 302, 344, 386, 431, 473)
            for cx, n in zip(xs, nums):
                words.append(_w(cx, y, str(n)))
            y += 16

        pages = [{"text": SAU_TEXT, "words": words}]
        result = _parse_opbal_rec_sales_excess_words_pages(pages, "sau.pdf")
        self.assertIsNotNone(result)
        by_name = {i["product_name"]: i for i in result["line_items"]}
        self.assertEqual(by_name["CYSTONE SYRUP"]["sales_qty"], 2.0)
        self.assertEqual(by_name["SEPTILIN TAB"]["sales_qty"], 45.0)
        self.assertEqual(by_name["SEPTILIN TAB"]["closing_qty"], 10.0)
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "opbal_rec_sales_excess_words",
        )

        with patch.dict(os.environ, {"STOCK_OPBAL_REC_SALES_EXCESS_WORDS": "false"}):
            self.assertIsNone(
                _parse_opbal_rec_sales_excess_words_pages(pages, "sau.pdf")
            )


if __name__ == "__main__":
    unittest.main()
