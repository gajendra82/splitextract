"""Medica STOCK/STK VAL/JUL column-shift repair for phone screenshots."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.sales_statement_extractor import (
    _medica_nrv_shift_evidence,
    _repair_medica_nrv_column_shift,
    _repair_medica_vision_column_shift,
)


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

    def test_repairs_nrv_opening_stock_shift(self):
        """NRV | Opening qty | Opening value — no packing => restore opening_qty."""
        item = {
            "product_name": "AACTARIL SOAP 75G",
            "packing": None,
            "opening_qty": 69.0,  # NRV, not opening stock
            "receipts_qty": 12.0,  # Opening Stock qty
            "sales_qty": 0.0,
            "sales_value": 828.0,  # Opening value mis-filed
            "closing_qty": 10.0,
            "closing_value": 690.0,
            "extra": {},
        }
        with patch.dict("os.environ", {"STOCK_HEADER_IGNORE_NON_QTY": "true"}):
            self.assertTrue(_medica_nrv_shift_evidence(item))
            _repair_medica_nrv_column_shift(item)

        self.assertEqual(item["extra"]["nrv"], 69.0)
        self.assertEqual(item["opening_qty"], 12.0)
        self.assertEqual(item["receipts_qty"], 0.0)
        self.assertEqual(item["extra"]["opening_value"], 828.0)
        self.assertEqual(item["sales_value"], 0.0)
        self.assertEqual(item["closing_qty"], 10.0)
        self.assertEqual(item["closing_value"], 690.0)
        self.assertEqual(item["extra"]["field_source"]["opening_qty"], "printed")
        self.assertEqual(item["extra"]["medica_nrv_shift_kind"], "opening_after_nrv")
        self.assertTrue(item["extra"].get("medica_nrv_column_shift_repaired"))

    def test_repairs_nrv_sale_stock_shift_when_packing_present(self):
        """Classic SALE-after-NRV sheet keeps sale mapping when packing is printed."""
        item = {
            "product_name": "CONFIDO TAB",
            "packing": "60 TAB",
            "opening_qty": 145.8,  # NRV
            "receipts_qty": 28.0,  # SALE
            "sales_qty": 2.0,  # IN/OT
            "sales_value": 4082.4,
            "closing_qty": 11.0,
            "closing_value": 1603.8,
            "extra": {},
        }
        with patch.dict("os.environ", {"STOCK_HEADER_IGNORE_NON_QTY": "true"}):
            self.assertTrue(_medica_nrv_shift_evidence(item))
            _repair_medica_nrv_column_shift(item)

        self.assertEqual(item["extra"]["nrv"], 145.8)
        self.assertEqual(item["opening_qty"], 0.0)
        self.assertEqual(item["receipts_qty"], 0.0)
        self.assertEqual(item["sales_qty"], 28.0)
        self.assertEqual(item["extra"]["in_ot_qty"], 2.0)
        self.assertEqual(item["extra"]["medica_nrv_shift_kind"], "sale_after_nrv")

    def test_does_not_eat_real_opening_qty_as_nrv(self):
        """ABANA-style: opening=75, rate is 132 from closing_value/closing_qty."""
        item = {
            "product_name": "ABANA TAB 60'S",
            "packing": None,
            "opening_qty": 75.0,
            "receipts_qty": 0.0,
            "sales_qty": 5.0,
            "sales_value": 0.0,
            "closing_qty": 70.0,
            "closing_value": 9240.0,  # 70 * 132
            "extra": {},
        }
        with patch.dict("os.environ", {"STOCK_HEADER_IGNORE_NON_QTY": "true"}):
            self.assertFalse(_medica_nrv_shift_evidence(item))
            _repair_medica_nrv_column_shift(item, force=True)

        self.assertEqual(item["opening_qty"], 75.0)
        self.assertNotIn("nrv", item["extra"])
        self.assertFalse(item["extra"].get("medica_nrv_column_shift_repaired"))


if __name__ == "__main__":
    unittest.main()
