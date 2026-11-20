"""Printed OPENING/RECEIPT/ISSUE/CLOSING qty+value sheet.

The 100ML row was blank and its numbers sat on the next BIG row, with
opening qty left at 0. Other layouts do not match this header.
"""
import unittest

from services.sales_statement_extractor import (
    _is_ssa_opening_receipt_issue_dump_text,
    _ssa_dump_move_opening_row_up,
    _ssa_dump_repair_items,
)


class TestSsaDumpHeader(unittest.TestCase):
    def test_header_matches_this_print_only(self):
        self.assertTrue(
            _is_ssa_opening_receipt_issue_dump_text(
                "STOCK & SALES ANALYSIS ITEM DESCRIPTION OPENING RECEIPT ISSUE QTY VALUE"
            )
        )
        self.assertFalse(
            _is_ssa_opening_receipt_issue_dump_text(
                "STOCK & SALES ANALYSIS RATE OPENING RECEIPT ISSUE QTY VALUE"
            )
        )
        self.assertFalse(
            _is_ssa_opening_receipt_issue_dump_text(
                "STOCK & SALES ANALYSIS ITEM OPENING RECEIPT ISSUE CLOSING M.EXP QTY VALUE"
            )
        )

    def test_moves_balanced_100ml_row_off_the_big_line(self):
        items = _ssa_dump_move_opening_row_up(
            [
                {
                    "product_name": "LIV 52 100ML (NEW MRP)",
                    "opening_qty": 0,
                    "opening_value": 0,
                    "receipts_qty": 0,
                    "sales_qty": 0,
                    "closing_qty": 0,
                },
                {
                    "product_name": "LIV 52 200 BIG(NEW MRP)",
                    "opening_qty": 2,
                    "opening_value": 316.10,
                    "receipts_qty": 8400,
                    "receipts_value": 1287807.70,
                    "sales_qty": 6580,
                    "sales_value": 995773.18,
                    "closing_qty": 1822,
                    "closing_value": 287970.74,
                },
                {
                    "product_name": "LIV 52 200ML (OLD MRP)",
                    "opening_qty": 800,
                    "opening_value": 118878.24,
                    "sales_qty": 800,
                    "sales_value": 113976.57,
                    "closing_qty": 0,
                },
            ]
        )
        hundred = items[0]
        big = items[1]
        old = items[2]
        self.assertEqual(hundred["opening_qty"], 2)
        self.assertEqual(hundred["receipts_qty"], 8400)
        self.assertEqual(hundred["sales_qty"], 6580)
        self.assertEqual(hundred["closing_qty"], 1822)
        self.assertEqual(big["opening_qty"], 0)
        self.assertEqual(big["receipts_qty"], 0)
        self.assertEqual(old["opening_qty"], 800)
        self.assertEqual(old["sales_qty"], 800)

    def test_infers_opening_qty_when_vision_drops_it(self):
        items = _ssa_dump_repair_items(
            [
                {
                    "product_name": "LIV 52 100ML (NEW MRP)",
                    "opening_qty": 0,
                    "opening_value": 0,
                    "receipts_qty": 0,
                    "sales_qty": 0,
                    "closing_qty": 0,
                    "extra": {},
                },
                {
                    "product_name": "LIV 52 200 BIG(NEW MRP)",
                    "opening_qty": 0,
                    "opening_value": 316.10,
                    "receipts_qty": 8400,
                    "receipts_value": 1287807.70,
                    "sales_qty": 6580,
                    "sales_value": 995773.18,
                    "closing_qty": 1822,
                    "closing_value": 287970.74,
                    "extra": {"opening_value": 316.10},
                },
                {
                    "product_name": "LIV 52 200ML (OLD MRP)",
                    "opening_qty": 0,
                    "opening_value": 118878.24,
                    "receipts_qty": 0,
                    "sales_qty": 800,
                    "sales_value": 113976.57,
                    "closing_qty": 0,
                    "extra": {"opening_value": 118878.24},
                },
            ]
        )
        hundred = items[0]
        big = items[1]
        old = items[2]
        self.assertEqual(hundred["opening_qty"], 2.0)
        self.assertEqual(hundred["receipts_qty"], 8400)
        self.assertEqual(hundred["closing_qty"], 1822)
        self.assertEqual(big["opening_qty"], 0)
        self.assertEqual(big["receipts_qty"], 0)
        self.assertEqual(old["opening_qty"], 800.0)
