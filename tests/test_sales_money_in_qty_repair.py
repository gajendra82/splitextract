"""LMS1 SL.VALUE parked in SALES qty / hundred-x decimal scale repairs."""

from __future__ import annotations

import unittest

from services.stock_vision_table import (
    _repair_closing_value_hundredx_scale,
    _repair_pur_free_sale_closing_left_shift,
    _repair_sales_money_parked_in_qty,
    _repair_sales_value_hundredx_scale,
)


class SalesMoneyParkedInQtyTests(unittest.TestCase):
    def test_paisa_sales_value_moved_from_sales_qty(self):
        item = {
            "product_name": "BONNISPAZ DROPS",
            "opening_qty": 10.0,
            "receipts_qty": 18.0,
            "sales_qty": 1139.57,
            "sales_value": 0.0,
            "closing_qty": 0.0,
            "closing_value": 512.0,
            "extra": {
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "sales_qty": "printed",
                    "sales_value": "printed",
                    "closing_qty": "missing",
                    "closing_value": "printed",
                    "free_out_qty": "missing",
                },
                "flags": ["CLOSING_VALUE_PARKED_IN_QTY"],
            },
        }
        self.assertTrue(_repair_sales_money_parked_in_qty(item))
        self.assertIsNone(item["sales_qty"])
        self.assertEqual(item["sales_value"], 1139.57)
        self.assertEqual(item["extra"]["field_source"]["sales_qty"], "missing")
        self.assertIn("SALES_VALUE_PARKED_IN_QTY", item["extra"]["flags"])

    def test_balanced_small_sales_qty_not_moved(self):
        item = {
            "product_name": "OK ROW",
            "opening_qty": 10.0,
            "receipts_qty": 5.0,
            "sales_qty": 3.0,
            "sales_value": 0.0,
            "closing_qty": 12.0,
            "closing_value": 100.0,
            "extra": {
                "field_source": {
                    "sales_qty": "printed",
                    "sales_value": "printed",
                    "closing_qty": "printed",
                    "closing_value": "printed",
                }
            },
        }
        self.assertFalse(_repair_sales_money_parked_in_qty(item))
        self.assertEqual(item["sales_qty"], 3.0)


class SalesValueHundredxScaleTests(unittest.TestCase):
    def test_geriforte_style_decimal_drop(self):
        item = {
            "product_name": "GERIFORTE SYP",
            "opening_qty": 34.0,
            "receipts_qty": 28.0,
            "sales_qty": 15.0,
            "sales_value": 245700.0,
            "closing_qty": 47.0,
            "closing_value": 7698.6,
            "extra": {
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "sales_qty": "printed",
                    "sales_value": "printed",
                    "closing_qty": "printed",
                    "closing_value": "printed",
                }
            },
        }
        self.assertTrue(_repair_sales_value_hundredx_scale(item))
        self.assertEqual(item["sales_value"], 2457.0)
        self.assertIn("SALES_VALUE_HUNDREDX_SCALE", item["extra"]["flags"])

    def test_does_not_scale_when_rate_already_matches(self):
        item = {
            "product_name": "NORMAL",
            "opening_qty": 34.0,
            "receipts_qty": 28.0,
            "sales_qty": 15.0,
            "sales_value": 2457.0,
            "closing_qty": 47.0,
            "closing_value": 7698.6,
            "extra": {"field_source": {"sales_value": "printed"}},
        }
        self.assertFalse(_repair_sales_value_hundredx_scale(item))
        self.assertEqual(item["sales_value"], 2457.0)

    def test_bresol_style_closing_value_decimal_drop(self):
        item = {
            "product_name": "BRESOL TAB",
            "opening_qty": 7.0,
            "receipts_qty": 112.0,
            "sales_qty": 78.0,
            "sales_value": 9448.92,
            "closing_qty": 41.0,
            "closing_value": 496674.0,
            "extra": {
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "sales_qty": "printed",
                    "sales_value": "printed",
                    "closing_qty": "printed",
                    "closing_value": "printed",
                }
            },
        }
        self.assertTrue(_repair_closing_value_hundredx_scale(item))
        self.assertAlmostEqual(item["closing_value"], 4966.74, places=2)
        self.assertIn("CLOSING_VALUE_HUNDREDX_SCALE", item["extra"]["flags"])


class PurFreeCaseDSalesValueInSalesQtyTests(unittest.TestCase):
    def test_free_out_closing_and_sales_money_remapped(self):
        item = {
            "product_name": "HIORA K PASTE",
            "opening_qty": 51.0,
            "receipts_qty": 50.0,
            "sales_qty": 4876.0,
            "sales_value": 0.0,
            "closing_qty": 7435.9,
            "closing_value": None,
            "extra": {
                "free_out_qty": 61.0,
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "sales_qty": "printed",
                    "sales_value": "printed",
                    "free_out_qty": "printed",
                    "closing_qty": "printed",
                    "closing_value": "missing",
                },
            },
        }
        self.assertTrue(_repair_pur_free_sale_closing_left_shift(item))
        self.assertIsNone(item["sales_qty"])
        self.assertEqual(item["sales_value"], 4876.0)
        self.assertEqual(item["closing_qty"], 61.0)
        self.assertEqual(item["closing_value"], 7435.9)
        self.assertIsNone(item["extra"]["free_out_qty"])
        self.assertEqual(item["extra"]["field_source"]["sales_qty"], "missing")
        self.assertIn("PUR_FREE_SALE_CLOSING_SHIFT", item["extra"]["flags"])

    def test_case_d_after_closing_money_already_parked(self):
        item = {
            "product_name": "HIORA K PASTE",
            "opening_qty": 51.0,
            "receipts_qty": 50.0,
            "sales_qty": 4876.0,
            "sales_value": 0.0,
            "closing_qty": 0.0,
            "closing_value": 7435.9,
            "extra": {
                "free_out_qty": 61.0,
                "field_source": {
                    "sales_qty": "printed",
                    "sales_value": "printed",
                    "free_out_qty": "printed",
                    "closing_qty": "missing",
                    "closing_value": "printed",
                },
                "flags": ["CLOSING_VALUE_PARKED_IN_QTY"],
            },
        }
        self.assertTrue(_repair_pur_free_sale_closing_left_shift(item))
        self.assertIsNone(item["sales_qty"])
        self.assertEqual(item["sales_value"], 4876.0)
        self.assertEqual(item["closing_qty"], 61.0)
        self.assertEqual(item["closing_value"], 7435.9)


if __name__ == "__main__":
    unittest.main()
