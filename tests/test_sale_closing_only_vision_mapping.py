"""SALE+CLOSING-only stock statement: header-driven field mapping regression.

Format: ITEM DESCRIPTION | SALE QTY/VALUE | CLOSING QTY/VALUE | RE-ORDER | M.EXP
No Opening / Receipt columns. Sale must not land in opening_qty.
"""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import _ensure_stock_qty_value_fields
from services.stock_reconciliation import reconcile_row
from services.stock_row_classifier import RowStatus, classify_result, classify_row, read_row_fields
from services.stock_vision_table import (
    _column_map_sale_closing_only,
    map_vision_table,
)


def _n(v: float) -> str:
    return str(int(v)) if float(v) == int(v) else str(v)


def _sale_closing_table(rows):
    """Vision JSON shaped like STOCK & SALES ANALYSIS (SALE + CLOSING only)."""
    return {
        "header_rows": [
            {"text": "ITEM DESCRIPTION", "col_index": 0, "x_center": 0.08},
            {
                "text": "<==SALE==>",
                "col_index": 1,
                "x_center": 0.35,
                "subheader_text": "QTY.",
            },
            {
                "text": "",
                "col_index": 2,
                "x_center": 0.42,
                "subheader_text": "VALUE",
            },
            {
                "text": "<==CLOSING==>",
                "col_index": 3,
                "x_center": 0.58,
                "subheader_text": "QTY.",
            },
            {
                "text": "",
                "col_index": 4,
                "x_center": 0.68,
                "subheader_text": "VALUE",
            },
            {"text": "RE-ORDER", "col_index": 5, "x_center": 0.82},
            {"text": "M.EXP", "col_index": 6, "x_center": 0.92},
        ],
        "column_count": 7,
        "rows": [
            {"row_index": i, "cells": list(cells), "is_total_row": False}
            for i, cells in enumerate(rows)
        ],
        "proposed_mapping": [
            {"col_index": 0, "field": "product_name", "confidence": 1.0},
            {"col_index": 1, "field": "sales_qty", "confidence": 1.0},
            {"col_index": 2, "field": "sales_value", "confidence": 1.0},
            {"col_index": 3, "field": "closing_qty", "confidence": 1.0},
            {"col_index": 4, "field": "closing_value", "confidence": 1.0},
            {"col_index": 5, "field": "order_qty", "confidence": 0.9},
            {"col_index": 6, "field": "ignore", "confidence": 0.9},
        ],
        "unreadable_cells": [],
    }


class SaleClosingOnlyMappingTests(unittest.TestCase):
    CASES = [
        ("AACTARIL SOAP", "75GM", 16.0, 1181.0, 100.0, 7380.0, "-", "10/28"),
        ("ABANA TAB.", "50'S", 30.0, 4235.0, 160.0, 22587.0, "-", "4/28"),
        ("ALTHEA LOTION", "100ML.", 0.0, 0.0, 14.0, 2298.0, "-", "5/28"),
    ]

    def test_sale_closing_maps_to_sales_and_closing_not_opening(self):
        table = _sale_closing_table(
            [
                [name, pack, _n(sq), _n(sv), _n(cq), _n(cv), reorder, mexp]
                for name, pack, sq, sv, cq, cv, reorder, mexp in self.CASES
            ]
        )
        mapped = map_vision_table(table)
        self.assertTrue(_column_map_sale_closing_only(mapped["column_map"]))
        canons = [c["canonical"] for c in mapped["column_map"]]
        self.assertIn("sales_qty", canons)
        self.assertIn("closing_qty", canons)
        self.assertNotIn("opening_qty", canons)
        self.assertNotIn("purchase_qty", canons)

        self.assertEqual(len(mapped["line_items"]), 3)
        for item, (name, pack, sq, sv, cq, cv, _ro, _mx) in zip(
            mapped["line_items"], self.CASES
        ):
            with self.subTest(product=name):
                self.assertIn(name.split()[0], str(item.get("product_name") or ""))
                self.assertEqual(item.get("sales_qty"), sq)
                self.assertEqual(item.get("sales_value"), sv)
                self.assertEqual(item.get("closing_qty"), cq)
                self.assertEqual(item.get("closing_value"), cv)
                self.assertIsNone(item.get("opening_qty"))
                self.assertIsNone(item.get("receipts_qty"))
                fs = (item.get("extra") or {}).get("field_source") or {}
                self.assertEqual(fs.get("opening_qty"), "missing")
                self.assertEqual(fs.get("sales_qty"), "printed")
                self.assertEqual(fs.get("closing_qty"), "printed")
                self.assertEqual(
                    str(item.get("packing") or "").rstrip("."),
                    pack.rstrip("."),
                )

    def test_sale_closing_classifier_valid_and_recon_skips(self):
        table = _sale_closing_table(
            [["AACTARIL SOAP", "75GM", "16", "1181", "100", "7380", "-", "10/28"]]
        )
        mapped = map_vision_table(table)
        item = mapped["line_items"][0]
        status, info = classify_row(read_row_fields(item))
        self.assertEqual(status, RowStatus.VALID)
        self.assertEqual(info.get("reason"), "sale_closing_only")

        classified = classify_result({"line_items": mapped["line_items"]})
        self.assertGreaterEqual(classified["valid_ratio"], 0.99)

        recon = reconcile_row(item)
        self.assertTrue(recon.get("valid"))
        self.assertEqual(recon.get("skipped"), "sale_closing_only")
        self.assertEqual(recon.get("sales_qty"), 16.0)
        self.assertEqual(recon.get("extracted_closing_qty"), 100.0)

    def test_ensure_fields_keeps_opening_null(self):
        table = _sale_closing_table(
            [["AACTARIL SOAP", "75GM", "16", "1181", "100", "7380", "-", "10/28"]]
        )
        mapped = map_vision_table(table)
        result = {
            "line_items": mapped["line_items"],
            "totals": {
                "sales_value": None,
                "closing_value": None,
                "extra": {
                    "extraction_method": "vision_table",
                    "stock_identity_kind": "sale_closing_only",
                    "column_map": mapped["column_map"],
                },
            },
        }
        out = _ensure_stock_qty_value_fields(result)
        item = out["line_items"][0]
        self.assertEqual(item["sales_qty"], 16.0)
        self.assertEqual(item["sales_value"], 1181.0)
        self.assertEqual(item["closing_qty"], 100.0)
        self.assertEqual(item["closing_value"], 7380.0)
        self.assertIsNone(item.get("opening_qty"))
        self.assertIsNone(item.get("receipts_qty"))


class BareQtyValueSaleClosingTests(unittest.TestCase):
    """Vision often drops <SALE>/<CLOSING> and only returns bare QTY/VALUE pairs."""

    def test_two_bare_qty_value_pairs_map_to_sales_and_closing(self):
        import os

        os.environ["STOCK_HEADER_QTY_VALUE_SEQUENCE"] = "true"
        table = {
            "header_rows": [
                {"text": "ITEM DESCRIPTION", "col_index": 0},
                {"text": "QTY", "col_index": 1},
                {"text": "VALUE", "col_index": 2},
                {"text": "QTY", "col_index": 3},
                {"text": "VALUE", "col_index": 4},
                {"text": "RE-ORDER", "col_index": 5},
                {"text": "M.EXP", "col_index": 6},
            ],
            "column_count": 7,
            "rows": [
                {
                    "row_index": 0,
                    "cells": [
                        "AACTARIL SOAP",
                        "75GM",
                        "16",
                        "1181",
                        "100",
                        "7380",
                        "-",
                        "10/28",
                    ],
                    "is_total_row": False,
                },
                {
                    "row_index": 1,
                    "cells": [
                        "ABANA TAB.",
                        "50'S",
                        "30",
                        "4235",
                        "160",
                        "22587",
                        "-",
                        "4/28",
                    ],
                    "is_total_row": False,
                },
            ],
            "proposed_mapping": [],
            "unreadable_cells": [],
        }
        mapped = map_vision_table(table)
        canons = [c["canonical"] for c in mapped["column_map"]]
        self.assertIn("sales_qty", canons)
        self.assertIn("closing_qty", canons)
        self.assertNotIn("opening_qty", canons)
        self.assertNotIn("purchase_qty", canons)
        item = mapped["line_items"][0]
        self.assertEqual(item["sales_qty"], 16.0)
        self.assertEqual(item["sales_value"], 1181.0)
        self.assertEqual(item["closing_qty"], 100.0)
        self.assertEqual(item["closing_value"], 7380.0)
        self.assertIsNone(item.get("opening_qty"))
        self.assertIsNone(item.get("receipts_qty"))

    def test_reread_cannot_invent_opening_over_sale_closing(self):
        from services.stock_vision_table import merge_reread

        first = {
            "product_name": "AACTARIL SOAP",
            "opening_qty": None,
            "receipts_qty": None,
            "sales_qty": 16.0,
            "sales_value": 1181.0,
            "closing_qty": 100.0,
            "closing_value": 7380.0,
            "extra": {
                "vision_row_index": 0,
                "field_source": {
                    "opening_qty": "missing",
                    "purchase_qty": "missing",
                    "receipts_qty": "missing",
                    "sales_qty": "printed",
                    "closing_qty": "printed",
                },
            },
        }
        bad = {
            "product_name": "AACTARIL SOAP",
            "opening_qty": 16.0,
            "receipts_qty": 100.0,
            "sales_qty": 0.0,
            "closing_qty": 116.0,
            "extra": {
                "vision_row_index": 0,
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "sales_qty": "missing",
                    "closing_qty": "printed",
                },
            },
        }
        merged = merge_reread([first], [bad], prefer_reconciled=True)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["sales_qty"], 16.0)
        self.assertEqual(merged[0]["closing_qty"], 100.0)
        self.assertIsNone(merged[0].get("opening_qty"))


class OpeningReceiptSaleClosingUnchangedTests(unittest.TestCase):
    """Full Opening+Receipt+Sale+Closing layout must not shift columns."""

    def test_op_pur_sale_cls_still_maps_correctly(self):
        table = {
            "header_rows": [
                {"text": "Product", "col_index": 0},
                {"text": "Opening", "col_index": 1, "subheader_text": "Qty"},
                {"text": "", "col_index": 2, "subheader_text": "Value"},
                {"text": "Purchase", "col_index": 3, "subheader_text": "Qty"},
                {"text": "", "col_index": 4, "subheader_text": "Value"},
                {"text": "Sale", "col_index": 5, "subheader_text": "Qty"},
                {"text": "", "col_index": 6, "subheader_text": "Value"},
                {"text": "Closing", "col_index": 7, "subheader_text": "Qty"},
                {"text": "", "col_index": 8, "subheader_text": "Value"},
            ],
            "column_count": 9,
            "rows": [
                {
                    "row_index": 0,
                    "cells": [
                        "LIV 52 DS",
                        "10",
                        "100",
                        "5",
                        "50",
                        "3",
                        "30",
                        "12",
                        "120",
                    ],
                    "is_total_row": False,
                }
            ],
            "proposed_mapping": [],
            "unreadable_cells": [],
        }
        mapped = map_vision_table(table)
        self.assertFalse(_column_map_sale_closing_only(mapped["column_map"]))
        item = mapped["line_items"][0]
        self.assertEqual(item["opening_qty"], 10.0)
        self.assertEqual(item["receipts_qty"], 5.0)
        self.assertEqual(item["sales_qty"], 3.0)
        self.assertEqual(item["closing_qty"], 12.0)
        self.assertEqual(item["extra"]["field_source"]["opening_qty"], "printed")
        status, _info = classify_row(read_row_fields(item))
        self.assertEqual(status, RowStatus.VALID)
        recon = reconcile_row(item)
        self.assertTrue(recon.get("valid"))
        self.assertNotEqual(recon.get("skipped"), "sale_closing_only")


if __name__ == "__main__":
    unittest.main()
