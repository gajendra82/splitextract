"""STOCK & SALES ANALYSIS Quantity / Value in Rs. footers on pdf_words path."""

from __future__ import annotations

import unittest

from services.stock_native_resolver import (
    _footer_totals_from_page_results,
    build_line_items_from_tokens,
)
from services.stock_native_tables import (
    _sheet_total_row_kind,
    resolver_ready,
)
from services.stock_header_resolver import assign_cells
from services.stock_native_resolver import fields_to_line_item


def _ssa_columns():
    headers = [
        ("ITEM DESCRIPTION", "product_name"),
        ("OPENING STOCK", "opening_qty"),
        ("PURCHASES", "purchase_qty"),
        ("SALE RETURN", "sales_return_qty"),
        ("REPL./ OTHERS", "other_outward_qty"),
        ("TOTAL STOCK", "total_qty"),
        ("SALES", "sales_qty"),
        ("PURCHASE RETURN", "purchase_return_qty"),
        ("REPL./ OTHERS", "ignore"),
        ("CLOSING STOCK", "closing_qty"),
        ("RATE", "rate"),
    ]
    header_cells = []
    columns = []
    for i, (text, canon) in enumerate(headers):
        xc = float(i)
        header_cells.append(
            {
                "col_index": i,
                "text": text,
                "x_center": xc,
                "x0": xc - 0.4,
                "x1": xc + 0.4,
            }
        )
        columns.append(
            {
                "col_index": i,
                "header_text": text,
                "canonical": canon,
                "confidence": 1.0,
            }
        )
    return header_cells, columns


def _row_tokens(label: str, nums: list) -> list:
    toks = [{"text": label, "col_index": 0, "x": 0.0}]
    for i, n in enumerate(nums, start=1):
        toks.append({"text": str(n), "col_index": i, "x": float(i)})
    return toks


class TestSsaQtyValueFooterKind(unittest.TestCase):
    def test_sheet_total_row_kind_detects_busy_footers(self):
        qty = _row_tokens("Total Quantity", [409, 250, 10, 0, 669, 190, 0, 0, 479])
        self.assertEqual(_sheet_total_row_kind(qty), "quantity")
        val = _row_tokens("Value in Rs.", [53570, 32383, 1253, 0, 87206, 23135, 0, 0, 64606])
        self.assertEqual(_sheet_total_row_kind(val), "value")
        self.assertIsNone(_sheet_total_row_kind(_row_tokens("GASEX TAB.", [0, 200, 0, 0, 200, 86, 0, 0, 114])))


class TestSsaQtyValueFooterBuild(unittest.TestCase):
    def test_footer_rows_not_ingested_as_products(self):
        _header_cells, columns = _ssa_columns()
        rows = [
            _row_tokens("GASEX TAB.", [0, 200, 0, 0, 200, 86, 0, 0, 114, 134.97]),
            _row_tokens("Total Quantity", [409, 250, 10, 0, 669, 190, 0, 0, 479]),
            _row_tokens(
                "Value in Rs.",
                [53570, 32383, 1253, 0, 87206, 23135, 0, 0, 64606],
            ),
        ]
        items = build_line_items_from_tokens(columns, rows)
        names = [it.get("product_name") for it in items]
        self.assertEqual(names, ["GASEX TAB."])
        self.assertEqual(items[0].get("sales_qty"), 86.0)

    def test_footer_totals_promote_value_in_rs_sales_money(self):
        header_cells, columns = _ssa_columns()
        val_row = _row_tokens(
            "Value in Rs.",
            [53570, 32383, 1253, 0, 87206, 23135, 0, 0, 64606],
        )
        page_results = [
            {
                "header_cells": header_cells,
                "data_rows": [val_row],
            }
        ]
        footer = _footer_totals_from_page_results(page_results)
        self.assertEqual(footer.get("sales_value"), 23135.0)
        self.assertEqual(footer.get("opening_value"), 53570.0)
        self.assertEqual(footer.get("closing_value"), 64606.0)

    def test_footer_totals_merge_qty_and_value_grand_rows(self):
        header_cells, _columns = _ssa_columns()
        qty_row = _row_tokens(
            "Total Quantity",
            [4070, 32360, 10, 0, 36440, 32733, 0, 0, 3707],
        )
        val_row = _row_tokens(
            "Value in Rs.",
            [505166, 5283059, 1253, 0, 5789477, 5392302, 0, 0, 459977],
        )
        page_results = [
            {
                "header_cells": header_cells,
                "data_rows": [qty_row, val_row],
            }
        ]
        footer = _footer_totals_from_page_results(page_results)
        self.assertEqual(footer.get("sales_qty"), 32733.0)
        self.assertEqual(footer.get("sales_value"), 5392302.0)
        self.assertEqual(footer.get("closing_value"), 459977.0)

    def test_assign_cells_maps_product_row_sales(self):
        _, columns = _ssa_columns()
        row = _row_tokens("GASEX SYP. ELAICHI", [48, 0, 0, 0, 48, 3, 0, 0, 45, 96.24])
        item = fields_to_line_item(assign_cells(row, columns).get("fields") or {})
        self.assertEqual(item.get("sales_qty"), 3.0)
        self.assertEqual(item.get("closing_qty"), 45.0)


if __name__ == "__main__":
    unittest.main()