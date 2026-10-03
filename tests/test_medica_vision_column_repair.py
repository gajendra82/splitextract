"""Medica STOCK/STK VAL/JUL column-shift repair for phone screenshots."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import _repair_medica_vision_column_shift


class MedicaVisionColumnRepairTests(unittest.TestCase):
    def test_repairs_in_ot_stock_stk_val_shift(self):
        item = {
            "opening_qty": 37.0,
            "receipts_qty": 0.0,
            "sales_qty": 28.0,
            "closing_qty": 2.0,
            "closing_value": 11.0,
            "extra": {"jul_qty": 1805.0},
        }
        _repair_medica_vision_column_shift(item)
        self.assertEqual(item["closing_qty"], 11.0)
        self.assertEqual(item["closing_value"], 1805.0)
        self.assertEqual(item["extra"]["in_ot_qty"], 2.0)
        self.assertTrue(item["extra"].get("medica_column_shift_repaired"))

    def test_leaves_correct_liv_row_alone(self):
        item = {
            "opening_qty": 187.0,
            "receipts_qty": 0.0,
            "sales_qty": 137.0,
            "closing_qty": 50.0,
            "closing_value": 7903.0,
            "extra": {"jul_qty": 96.0},
        }
        _repair_medica_vision_column_shift(item)
        self.assertEqual(item["closing_qty"], 50.0)
        self.assertEqual(item["closing_value"], 7903.0)
        self.assertEqual(item["extra"]["jul_qty"], 96.0)
        self.assertFalse(item["extra"].get("medica_column_shift_repaired"))

    def test_leaves_abana_alone(self):
        item = {
            "opening_qty": 0.0,
            "receipts_qty": 3.0,
            "sales_qty": 0.0,
            "closing_qty": 3.0,
            "closing_value": 455.0,
            "extra": {"jul_qty": 3.0},
        }
        _repair_medica_vision_column_shift(item)
        self.assertEqual(item["closing_qty"], 3.0)
        self.assertEqual(item["closing_value"], 455.0)
        self.assertEqual(item["extra"]["jul_qty"], 3.0)


if __name__ == "__main__":
    unittest.main()
