"""Vision path: LMS|Op.Stk|Receipt must not left-compact blank cells.

Regression for production bug where LMS=1 / Op.Stk=14 became
opening_qty=1 / receipts_qty=14.
"""

from __future__ import annotations

import unittest

from services.stock_vision_table import map_vision_table, _normalize_row_cells


OPSTK_LMS_HEADERS = [
    "Product",
    "Packg",
    "LMS",
    "Op.Stk",
    "Receipt",
    "Pu.Ret",
    "Sales",
    "S.Ret",
    "Brk",
    "Repl",
    "Cl.Stk",
    "Cl.Value",
    "el free",
    "^XY",
]


def _vision_table(cells, *, headers=None, proposed=None):
    headers = headers or OPSTK_LMS_HEADERS
    n = len(headers)
    return {
        "tables": [
            {
                "table_index": 0,
                "column_count": n,
                "header_rows": [
                    [
                        {
                            "text": h,
                            "col_index": i,
                            "x_center": (i + 0.5) / n,
                        }
                        for i, h in enumerate(headers)
                    ]
                ],
                "rows": [
                    {
                        "row_index": 0,
                        "cells": cells,
                        "is_total_row": False,
                    }
                ],
                "proposed_mapping": proposed
                or [
                    {"col_index": i, "canonical": "ignore", "confidence": 0.1}
                    for i in range(n)
                ],
                "unreadable_cells": [],
            }
        ]
    }


def _map(cells, **kwargs):
    return map_vision_table(_vision_table(cells, **kwargs), request_id="lms-shift")


class VisionLmsOpstkNoShiftTests(unittest.TestCase):
    def test_bonnisan_lms1_opstk14_blank_receipt(self):
        """TEST 1: LMS=1, Op.Stk=14, Receipt=null → no left shift."""
        cells = [
            "BONNISAN 100ML",
            "100ML",
            "1",
            "14",
            None,
            None,
            None,
            None,
            None,
            None,
            "14",
            "721.98",
            None,
            None,
        ]
        item = _map(cells)["line_items"][0]
        self.assertEqual(item["product_name"], "BONNISAN 100ML")
        self.assertEqual(item["extra"].get("lms"), 1.0)
        self.assertEqual(item["opening_qty"], 14.0)
        self.assertEqual(item["extra"]["field_source"].get("purchase_qty"), "missing")
        # Legacy top-level may be 0.0 for missing purchase; must NOT be 14.
        self.assertNotEqual(item["receipts_qty"], 14.0)
        self.assertNotEqual(item["opening_qty"], 1.0)
        self.assertEqual(item["closing_qty"], 14.0)
        self.assertEqual(item["closing_value"], 721.98)

    def test_blank_lms_keeps_opstk_as_opening(self):
        """TEST: LMS=null, Op.Stk=14, Receipt=null."""
        cells = [
            "BONNISAN 100ML",
            "100ML",
            None,
            "14",
            None,
            None,
            None,
            None,
            None,
            None,
            "14",
            "721.98",
            None,
            None,
        ]
        item = _map(cells)["line_items"][0]
        self.assertIsNone(item["extra"].get("lms"))
        self.assertEqual(item["extra"]["field_source"].get("lms"), "missing")
        self.assertEqual(item["opening_qty"], 14.0)
        self.assertNotEqual(item["receipts_qty"], 14.0)
        self.assertEqual(item["extra"]["field_source"].get("purchase_qty"), "missing")

    def test_lms1_blank_opstk_receipt14(self):
        """TEST: LMS=1, Op.Stk=null, Receipt=14 — blanks do not compact."""
        cells = [
            "PRODUCT X",
            "10ML",
            "1",
            None,
            "14",
            None,
            None,
            None,
            None,
            None,
            "0",
            "0",
            None,
            None,
        ]
        item = _map(cells)["line_items"][0]
        self.assertEqual(item["extra"].get("lms"), 1.0)
        self.assertEqual(item["extra"]["field_source"].get("opening_qty"), "missing")
        self.assertEqual(item["receipts_qty"], 14.0)
        self.assertNotEqual(item["opening_qty"], 1.0)
        self.assertNotEqual(item["opening_qty"], 14.0)

    def test_sparse_dict_cells_preserve_blank_receipt_slot(self):
        """Dict cells omitting blank Receipt must not left-shift Closing into Receipt."""
        # Physical: LMS=1, Op.Stk=14, Receipt blank, Cl.Stk=14, Cl.Value=721.98
        sparse = [
            {"col_index": 0, "text": "BONNISAN 100ML"},
            {"col_index": 1, "text": "100ML"},
            {"col_index": 2, "text": "1"},
            {"col_index": 3, "text": "14"},
            # col 4 Receipt intentionally omitted (blank)
            {"col_index": 10, "text": "14"},
            {"col_index": 11, "text": "721.98"},
        ]
        coercions: dict = {}
        dense = _normalize_row_cells(sparse, 14, coercions)
        self.assertEqual(dense[2], "1")
        self.assertEqual(dense[3], "14")
        self.assertIsNone(dense[4])  # Receipt stays blank
        self.assertEqual(dense[10], "14")
        self.assertEqual(dense[11], "721.98")
        self.assertGreater(coercions.get("dict_cells_by_col_index", 0), 0)

        item = _map(sparse)["line_items"][0]
        self.assertEqual(item["extra"].get("lms"), 1.0)
        self.assertEqual(item["opening_qty"], 14.0)
        self.assertNotEqual(item["receipts_qty"], 14.0)
        self.assertEqual(item["closing_qty"], 14.0)
        self.assertEqual(item["closing_value"], 721.98)

    def test_lms_never_maps_to_opening_via_proposed_mapping(self):
        """Model proposing LMS→opening_qty must be blocked."""
        cells = [
            "BONNISAN 100ML",
            "100ML",
            "1",
            "14",
            None,
            None,
            None,
            None,
            None,
            None,
            "14",
            "721.98",
            None,
            None,
        ]
        proposed = [
            {"col_index": 2, "canonical": "opening_qty", "confidence": 0.99},
            {"col_index": 3, "canonical": "purchase_qty", "confidence": 0.99},
            {"col_index": 4, "canonical": "purchase_qty", "confidence": 0.5},
        ]
        mapped = _map(cells, proposed=proposed)
        item = mapped["line_items"][0]
        col2 = next(c for c in mapped["column_map"] if c["col_index"] == 2)
        self.assertEqual(col2["canonical"], "lms")
        self.assertEqual(item["extra"].get("lms"), 1.0)
        self.assertEqual(item["opening_qty"], 14.0)
        self.assertNotEqual(item["opening_qty"], 1.0)
        self.assertNotEqual(item["receipts_qty"], 14.0)

    def test_bresol_and_septilin_style_rows(self):
        bresol = [
            "BRESOL S 200ML",
            "200ML",
            "1",
            "51",
            None,
            None,
            "3",
            None,
            None,
            None,
            "48",
            "7862.4",
            None,
            None,
        ]
        item = _map(bresol)["line_items"][0]
        self.assertEqual(item["extra"].get("lms"), 1.0)
        self.assertEqual(item["opening_qty"], 51.0)
        self.assertEqual(item["sales_qty"], 3.0)
        self.assertEqual(item["closing_qty"], 48.0)
        self.assertEqual(item["closing_value"], 7862.4)

        sept = [
            "SEPTILIN T",
            "60",
            "2",
            "16",
            None,
            None,
            "13",
            None,
            None,
            None,
            "3",
            "587.4",
            None,
            None,
        ]
        item2 = _map(sept)["line_items"][0]
        self.assertEqual(item2["extra"].get("lms"), 2.0)
        self.assertEqual(item2["opening_qty"], 16.0)
        self.assertEqual(item2["sales_qty"], 13.0)
        self.assertEqual(item2["closing_qty"], 3.0)


if __name__ == "__main__":
    unittest.main()
