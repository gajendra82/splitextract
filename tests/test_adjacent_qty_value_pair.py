"""Adjacent Op/Receipt/Issue/Closing value columns must survive cell-layout repair."""

from __future__ import annotations

import unittest

from services.stock_header_resolver import resolve_columns
from services.stock_vision_table import (
    _apply_cell_layout_qty_value_repair,
    _infer_qty_value_canons_from_rows,
    _restore_closing_qty_from_header,
)


def _headers():
    names = [
        "PRODUCT NAME",
        "PACKING",
        "Op.",
        "Opening Bal",
        "Receipt",
        "Receipt/Pur",
        "Total",
        "Issue",
        "Issue/Sales",
        "Closing",
        "Closing Bala",
    ]
    return [
        {"col_index": i, "text": name, "header_text": name, "x_center": float(i)}
        for i, name in enumerate(names)
    ]


class TestAdjacentQtyValuePair(unittest.TestCase):
    def test_value_headers_promote_and_qty_header_does_not(self):
        resolved = resolve_columns(_headers())
        by = {
            c["header_text"]: c["canonical"]
            for c in resolved["columns"]
        }
        self.assertEqual(by["Op."], "opening_qty")
        self.assertEqual(by["Opening Bal"], "opening_value")
        self.assertEqual(by["Receipt"], "purchase_qty")
        self.assertEqual(by["Receipt/Pur"], "purchase_value")
        self.assertEqual(by["Issue"], "sales_qty")
        self.assertEqual(by["Issue/Sales"], "sales_value")
        self.assertEqual(by["Closing"], "closing_qty")
        self.assertEqual(by["Closing Bala"], "closing_value")
        self.assertNotEqual(by["Total"], "sales_qty")

        qty_headers = _headers()
        qty_headers[8]["text"] = "Issue/Sales Qty."
        qty_headers[8]["header_text"] = "Issue/Sales Qty."
        qty_resolved = resolve_columns(qty_headers)
        issue_sales = next(
            c for c in qty_resolved["columns"] if c["header_text"] == "Issue/Sales Qty."
        )
        self.assertNotEqual(issue_sales["canonical"], "sales_value")

    def test_cell_layout_does_not_overwrite_paired_columns(self):
        resolved = resolve_columns(_headers())
        columns = resolved["columns"]
        kept = _apply_cell_layout_qty_value_repair(
            columns, [], len(columns), []
        )
        by = {c["header_text"]: c["canonical"] for c in kept}
        self.assertEqual(by["Issue"], "sales_qty")
        self.assertEqual(by["Issue/Sales"], "sales_value")
        self.assertEqual(by["Closing"], "closing_qty")
        self.assertNotEqual(by.get("Total"), "sales_qty")

    def test_ocr_clacing_is_closing_qty(self):
        headers = _headers()
        headers[9]["text"] = "Clacing"
        headers[9]["header_text"] = "Clacing"
        resolved = resolve_columns(headers)
        by = {c["header_text"]: c["canonical"] for c in resolved["columns"]}
        self.assertEqual(by["Clacing"], "closing_qty")

    def test_issue_value_in_closing_restores_printed_closing(self):
        columns = resolve_columns(_headers())["columns"]
        # ASHVAGANDHA: Total 72 was taken as sales, Issue value 2232.99 as closing.
        item = {
            "product_name": "ASHVAGANDHA TABL",
            "opening_qty": 12.0,
            "receipts_qty": 60.0,
            "sales_qty": 72.0,
            "sales_value": None,
            "closing_qty": 2232.99,
            "closing_value": 10590.60,
            "extra": {
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "sales_qty": "printed",
                    "closing_qty": "printed",
                },
                "physical_cells": {
                    "2": "12",
                    "4": "60",
                    "6": "72",
                    "7": "12",
                    "8": "2232.99",
                    "9": "60",
                    "10": "10590.60",
                },
            },
        }
        self.assertTrue(_restore_closing_qty_from_header(item, columns))
        self.assertEqual(item["closing_qty"], 60.0)
        self.assertEqual(item["sales_qty"], 12.0)
        self.assertEqual(item["sales_value"], 2232.99)

    def test_zero_closing_restores_printed_closing_when_identity_matches(self):
        columns = resolve_columns(_headers())["columns"]
        # GASEX TAB: Issue 73 is already sales; Closing 13 was dropped.
        item = {
            "product_name": "GASEX TAB",
            "opening_qty": 86.0,
            "receipts_qty": 0.0,
            "sales_qty": 73.0,
            "sales_value": 10404.57,
            "closing_qty": 0.0,
            "closing_value": 1808.82,
            "extra": {
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "sales_qty": "printed",
                    "closing_qty": "printed",
                    "sales_value": "printed",
                },
                "physical_cells": {
                    "2": "86",
                    "6": "86",
                    "7": "73",
                    "8": "10404.57",
                    "9": "13",
                    "10": "1808.82",
                },
            },
        }
        self.assertTrue(_restore_closing_qty_from_header(item, columns))
        self.assertEqual(item["closing_qty"], 13.0)
        self.assertEqual(item["sales_qty"], 73.0)
        self.assertEqual(item["sales_value"], 10404.57)

    def test_printed_closing_is_left_alone(self):
        columns = resolve_columns(_headers())["columns"]
        item = {
            "product_name": "KOFLET SF SYP",
            "opening_qty": 44.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "closing_qty": 44.0,
            "extra": {
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "sales_qty": "printed",
                    "closing_qty": "printed",
                },
                "physical_cells": {"2": "44", "6": "44", "7": "0", "9": "44"},
            },
        }
        self.assertFalse(_restore_closing_qty_from_header(item, columns))
        self.assertEqual(item["closing_qty"], 44.0)

    def test_sales_qty_in_value_slot_still_moves_issue_money(self):
        columns = resolve_columns(_headers())["columns"]
        item = {
            "product_name": "ASHVAGANDHA TABL",
            "opening_qty": 12.0,
            "receipts_qty": 60.0,
            "sales_qty": 72.0,
            "sales_value": 12.0,
            "closing_qty": 2232.99,
            "closing_value": 60.0,
            "extra": {
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "sales_qty": "printed",
                    "closing_qty": "printed",
                    "sales_value": "printed",
                    "closing_value": "printed",
                },
                "physical_cells": {
                    "2": "12",
                    "4": "60",
                    "6": "72",
                    "7": "12",
                    "8": "2232.99",
                    "9": "60",
                    "10": "10590.60",
                },
            },
        }
        self.assertTrue(_restore_closing_qty_from_header(item, columns))
        self.assertEqual(item["closing_qty"], 60.0)
        self.assertEqual(item["sales_qty"], 12.0)
        self.assertEqual(item["sales_value"], 2232.99)
        self.assertEqual(item["closing_value"], 10590.60)

    def test_total_column_is_not_assigned_to_closing_qty(self):
        rows = [
            {
                "cells": [
                    "ASHVAGANDHA TABL",
                    "1*60",
                    "12",
                    "2118.12",
                    "60",
                    "10590.60",
                    "72",
                    "12",
                    "2232.99",
                    "60",
                    "10590.60",
                ]
            },
            {
                "cells": [
                    "GASEX TAB",
                    "1*100",
                    "86",
                    "11966.04",
                    "0",
                    "0.00",
                    "86",
                    "73",
                    "10404.57",
                    "13",
                    "1808.82",
                ]
            },
            {
                "cells": [
                    "RUMALAYA LINIMENT",
                    "1*60ML",
                    "55",
                    "6201.15",
                    "50",
                    "5390.00",
                    "105",
                    "23",
                    "2554.12",
                    "82",
                    "9005.16",
                ]
            },
        ]
        canons = _infer_qty_value_canons_from_rows(rows, 11)
        self.assertIsNotNone(canons)
        self.assertEqual(canons[6], "total_qty")
        self.assertEqual(canons[7], "sales_qty")
        self.assertEqual(canons[8], "sales_value")
        self.assertEqual(canons[9], "closing_qty")
        self.assertEqual(canons[10], "closing_value")
        self.assertNotEqual(canons[8], "closing_qty")


if __name__ == "__main__":
    unittest.main()
