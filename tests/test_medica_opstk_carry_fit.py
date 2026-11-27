"""Medica OPSTK|PURCH|SALE|SALE VAL|IN/OT|STOCK|STK VAL continuation pages.

Phone multipage Vision often reprints a product row as headers on page 2 and
reports fewer columns (drops SEP/AUG/…). Prefer the page-1 Medica carry map
instead of forcing cell_layout qty|value pairs (which park SALE under purchase).
"""

from __future__ import annotations

import unittest

from services.stock_vision_table import (
    _apply_cell_layout_qty_value_repair,
    _fit_carry_columns_to_count,
    _headers_look_like_medica_opstk_grid,
    _map_single_table,
)


def _medica_cols():
    headers = [
        ("PRODUCT DESCRIPTION", "product_name"),
        ("PACKING", "pack"),
        ("OPSTK", "opening_qty"),
        ("PURCH", "purchase_qty"),
        ("SALE", "sales_qty"),
        ("SALE VAL", "sales_value"),
        ("IN/OT", "other_outward_qty"),
        ("STOCK", "closing_qty"),
        ("STK VAL", "closing_value"),
        ("SEP", "lms"),
        ("AUG", "lms"),
        ("STK120", "ignore"),
        ("EXP3M", "ignore"),
    ]
    return [
        {
            "col_index": i,
            "header_text": h,
            "canonical": c,
            "confidence": 0.9,
            "is_value": c.endswith("_value"),
            "reason": "resolver",
            "source": "resolver",
        }
        for i, (h, c) in enumerate(headers)
    ]


class MedicaOpstkHeaderDetectTests(unittest.TestCase):
    def test_detects_medica_sale_val_grid(self):
        self.assertTrue(_headers_look_like_medica_opstk_grid(_medica_cols()))

    def test_rejects_opstk_opv_pair_grid(self):
        cols = [
            {"header_text": "OPSTK", "canonical": "opening_qty"},
            {"header_text": "OPV", "canonical": "opening_value"},
            {"header_text": "PURC", "canonical": "purchase_qty"},
            {"header_text": "PURCV", "canonical": "purchase_value"},
            {"header_text": "SALE", "canonical": "sales_qty"},
            {"header_text": "SALEV", "canonical": "sales_value"},
            {"header_text": "STOCK", "canonical": "closing_qty"},
            {"header_text": "STOCKV", "canonical": "closing_value"},
        ]
        self.assertFalse(_headers_look_like_medica_opstk_grid(cols))


class MedicaOpstkCellLayoutSkipTests(unittest.TestCase):
    def test_cell_layout_skips_medica_headers(self):
        cols = _medica_cols()
        errors: list = []
        out = _apply_cell_layout_qty_value_repair(
            cols,
            rows=[{"cells": ["X", "75GM", "39", "0", "30", "3447", "10", "19", "2040", "21"]}],
            column_count=13,
            errors=errors,
            force=True,
        )
        self.assertEqual(out[4]["canonical"], "sales_qty")
        self.assertEqual(out[5]["canonical"], "sales_value")
        self.assertEqual(out[7]["canonical"], "closing_qty")
        self.assertEqual(out[8]["canonical"], "closing_value")
        self.assertNotIn("CELL_LAYOUT_QTY_VALUE_REPAIR", errors)


class MedicaOpstkCarryFitTests(unittest.TestCase):
    def test_fit_truncates_trailing_month_cols(self):
        fitted = _fit_carry_columns_to_count(_medica_cols(), 10)
        self.assertEqual(len(fitted), 10)
        self.assertEqual(fitted[4]["canonical"], "sales_qty")
        self.assertEqual(fitted[5]["canonical"], "sales_value")
        self.assertEqual(fitted[6]["canonical"], "other_outward_qty")
        self.assertEqual(fitted[7]["canonical"], "closing_qty")
        self.assertEqual(fitted[8]["canonical"], "closing_value")

    def test_data_headers_prefer_medica_carry_despite_count_mismatch(self):
        carry = {
            "column_count": 13,
            "columns": _medica_cols(),
        }
        # Page-2 Vision: product row as headers, 10 numeric columns.
        table = {
            "column_count": 10,
            "header_rows": [
                [
                    {"col_index": 0, "text": "CYSTONE FORTE TAB"},
                    {"col_index": 1, "text": "30 s"},
                    {"col_index": 2, "text": "39"},
                    {"col_index": 3, "text": "0"},
                    {"col_index": 4, "text": "30"},
                    {"col_index": 5, "text": "3447"},
                    {"col_index": 6, "text": "10"},
                    {"col_index": 7, "text": "19"},
                    {"col_index": 8, "text": "2040"},
                    {"col_index": 9, "text": "21"},
                ]
            ],
            "rows": [
                {
                    "row_index": 0,
                    "cells": [
                        "CYSTONE FORTE TAB",
                        "30 s",
                        "39",
                        "0",
                        "30",
                        "3447",
                        "10",
                        "19",
                        "2040",
                        "21",
                    ],
                }
            ],
            "proposed_mapping": [],
        }
        mapped = _map_single_table(table, carry_header=carry, request_id="t")
        self.assertIn("PREFER_CARRY_ON_DATA_HEADERS", mapped.get("errors") or [])
        items = mapped.get("line_items") or []
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.get("sales_qty"), 30.0)
        self.assertEqual(item.get("sales_value"), 3447.0)
        self.assertEqual(item.get("closing_qty"), 19.0)
        self.assertEqual(item.get("closing_value"), 2040.0)
        self.assertEqual(item.get("opening_qty"), 39.0)
        self.assertEqual(
            float(item.get("purchase_qty") or item.get("receipts_qty") or 0),
            0.0,
        )
        # Must not look like qty|value remap (purchase=30, sales=10, close=2040).
        self.assertNotEqual(item.get("closing_qty"), 2040.0)


if __name__ == "__main__":
    unittest.main()
