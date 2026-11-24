"""TOTAL / GRAND TOTAL footers must never remain as line_items."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _is_non_product_line_name,
    _is_statement_total_product_name,
    _peel_named_total_products,
)
from services.stock_vision_table import (
    _is_vision_total_row_name,
    _promote_printed_total_row_money,
)


class StatementTotalNameTests(unittest.TestCase):
    def test_bare_and_dashed_total_detected(self):
        for name in (
            "TOTAL",
            "Total",
            "TOTAL:",
            "GRAND TOTAL",
            "Total (Value In Rs.)",
            "TOTAL VALUE",
            "----Total---->>",
            "---Total--->>",
        ):
            with self.subTest(name=name):
                self.assertTrue(_is_statement_total_product_name(name), name)
                self.assertTrue(_is_vision_total_row_name(name), name)
                self.assertTrue(_is_non_product_line_name(name), name)

    def test_sku_starting_with_total_kept(self):
        for name in ("TOTAL CARE", "TOTAL CARE BABY PANTS L", "TOTALINE SYRUP"):
            with self.subTest(name=name):
                self.assertFalse(_is_statement_total_product_name(name), name)
                self.assertFalse(_is_vision_total_row_name(name), name)
                self.assertFalse(_is_non_product_line_name(name), name)


class PeelNamedTotalProductsTests(unittest.TestCase):
    def test_mid_list_and_trailing_total_dropped(self):
        result = {
            "source_file": "78.jpg",
            "line_items": [
                {
                    "product_name": "ARJUNA CAP",
                    "opening_qty": 34.0,
                    "sales_qty": 5.0,
                    "closing_qty": 29.0,
                    "sales_value": 571.4,
                    "closing_value": 2937.99,
                },
                {
                    "product_name": "TOTAL",
                    "opening_qty": 1493.0,
                    "sales_qty": 251.0,
                    "closing_qty": 1242.0,
                    "sales_value": 41453.40,
                    "closing_value": 169302.72,
                },
                {
                    "product_name": "V GEL OINT",
                    "opening_qty": 28.0,
                    "sales_qty": 7.0,
                    "closing_qty": 21.0,
                    "sales_value": 773.36,
                    "closing_value": 2050.44,
                },
                {
                    "product_name": "----Total---->>",
                    "opening_qty": 1937.0,
                    "sales_qty": 394.0,
                    "closing_qty": 1543.0,
                    "sales_value": 66287.56,
                    "closing_value": 217772.49,
                },
            ],
            "totals": {"sales_value": None, "closing_value": None, "extra": {}},
        }
        out = _peel_named_total_products(result)
        names = [i["product_name"] for i in out["line_items"]]
        self.assertEqual(names, ["ARJUNA CAP", "V GEL OINT"])
        self.assertEqual(out["totals"]["sales_value"], 66287.56)
        self.assertEqual(out["totals"]["closing_value"], 217772.49)
        self.assertEqual(out["totals"]["extra"].get("named_total_products_dropped"), 2)

    def test_does_not_drop_total_care_sku(self):
        result = {
            "line_items": [
                {
                    "product_name": "TOTAL CARE BABY PANTS L",
                    "opening_qty": 1.0,
                    "sales_qty": 0.0,
                    "closing_qty": 1.0,
                    "closing_value": 96.24,
                }
            ],
            "totals": {"extra": {}},
        }
        out = _peel_named_total_products(result)
        self.assertEqual(len(out["line_items"]), 1)
        self.assertEqual(
            out["line_items"][0]["product_name"], "TOTAL CARE BABY PANTS L"
        )


class PromoteLastTotalRowTests(unittest.TestCase):
    def test_prefers_last_running_total(self):
        totals: dict = {"sales_value": None, "closing_value": None, "extra": {}}
        rows = [
            {
                "product_name": "TOTAL",
                "sales_value": 41453.40,
                "closing_value": 169302.72,
            },
            {
                "product_name": "TOTAL",
                "sales_value": 66287.56,
                "closing_value": 217772.49,
            },
        ]
        promoted = _promote_printed_total_row_money(totals, rows)
        self.assertEqual(sorted(promoted), ["closing_value", "sales_value"])
        self.assertEqual(totals["sales_value"], 66287.56)
        self.assertEqual(totals["closing_value"], 217772.49)


if __name__ == "__main__":
    unittest.main()
