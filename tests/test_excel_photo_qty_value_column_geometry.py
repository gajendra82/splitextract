"""Regression: photographed Excel STOCK & SALES ANALYSIS qty/value columns.

Target image: 0000723513_2026_08_ZA_35_559_04092026072239.jpg

Vision previously returned correct left-to-right cells but mapped the Excel
serial into product_name and collapsed bare QTY/VALUE headers via
DUPLICATE_CANONICAL, shifting money into qty fields (e.g. 421.70 as receipts).

These tests lock the geometry/cell-layout repair without hardcoding stockist
or product-specific routes.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest import mock

from services.stock_header_resolver import resolve_columns
from services.stock_vision_table import (
    map_vision_table,
    _infer_qty_value_canons_from_rows,
)


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "0000723513_2026_08_ZA_35_559_04092026072239.jpg"
FIXTURE_LINK = (
    ROOT
    / "tests"
    / "fixtures"
    / "stock"
    / "excel_photo_ssa_qty_value"
    / "source.jpg"
)


def _cells(*texts):
    return [
        {"text": t, "col_index": i, "x_center": 0.05 * (i + 1), "subheader_text": None}
        for i, t in enumerate(texts)
    ]


def _broken_vision_table():
    """Headers as Gemini returned them for the target photo (shifted / bare)."""
    return {
        "tables": [
            {
                "table_index": 0,
                "column_count": 16,
                "header_rows": [
                    [
                        {"text": "ITEM DESCRIPTION", "col_index": 0, "x_center": 0.05},
                        {"text": "QTY.", "col_index": 1, "x_center": 0.12},
                        {"text": "VALUE", "col_index": 2, "x_center": 0.18},
                        {"text": "QTY.", "col_index": 3, "x_center": 0.28},
                        {"text": "VALUE", "col_index": 4, "x_center": 0.34},
                        {"text": "QTY.", "col_index": 5, "x_center": 0.44},
                        {"text": "VALUE", "col_index": 6, "x_center": 0.50},
                        {"text": "QTY.", "col_index": 7, "x_center": 0.60},
                        {"text": "VALUE", "col_index": 8, "x_center": 0.66},
                        {"text": "CLOSING", "col_index": 9, "x_center": 0.74},
                        {"text": "", "col_index": 10, "x_center": 0.78},
                        {"text": "CLOSING", "col_index": 11, "x_center": 0.82},
                        {"text": "", "col_index": 12, "x_center": 0.86},
                        {"text": "DUMP", "col_index": 13, "x_center": 0.90},
                        {"text": "", "col_index": 14, "x_center": 0.94},
                        {"text": "DUMP", "col_index": 15, "x_center": 0.98},
                    ]
                ],
                "rows": [
                    {
                        "row_index": 0,
                        "y_center": 0.40,
                        "is_total_row": False,
                        "cells": [
                            "9",
                            "CLARINA CREAM",
                            "30GM",
                            "4",
                            "421.70",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "4",
                            "421.70",
                            "4",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                        ],
                    },
                    {
                        "row_index": 1,
                        "y_center": 0.42,
                        "is_total_row": False,
                        "cells": [
                            "10",
                            "CYSTONE TAB",
                            "1X60",
                            "1",
                            "164.15",
                            "15",
                            "2462.18",
                            "16",
                            "2626.32",
                            "5",
                            "164.15",
                            "3",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                        ],
                    },
                    {
                        "row_index": 2,
                        "y_center": 0.44,
                        "is_total_row": False,
                        "cells": [
                            "11",
                            "DIAREX TAB",
                            "1X30",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            None,
                        ],
                    },
                    {
                        "row_index": 3,
                        "y_center": 0.46,
                        "is_total_row": False,
                        "cells": [
                            "12",
                            "EVECARE CAP",
                            "1X30",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            None,
                        ],
                    },
                    {
                        "row_index": 4,
                        "y_center": 0.50,
                        "is_total_row": False,
                        "cells": [
                            "24",
                            "LIV 52 DS TAB",
                            "1X60",
                            "0.00",
                            "0.00",
                            "100",
                            "18410.60",
                            "46",
                            "8468.88",
                            "54",
                            "9941.72",
                            "0.00",
                            "0.00",
                            "0.00",
                            "0.00",
                            None,
                        ],
                    },
                    {
                        "row_index": 5,
                        "y_center": 0.92,
                        "is_total_row": True,
                        "cells": [
                            "35",
                            "TOTAL",
                            "40",
                            "643.55",
                            "115",
                            "20872.78",
                            "86",
                            "15194.15",
                            "69",
                            "12116.18",
                            "0",
                            "0.00",
                            "0.00",
                            "0.00",
                            None,
                            None,
                        ],
                    },
                ],
                "proposed_mapping": [],
            }
        ],
        "unreadable_cells": [],
    }


class TestBareQtyValueSequence(unittest.TestCase):
    def test_sequence_assigns_opening_purchase_sales(self):
        env = {
            "STOCK_HEADER_QTY_VALUE_SEQUENCE": "true",
            "STOCK_VISION_CELL_LAYOUT_REPAIR": "false",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            result = resolve_columns(
                _cells(
                    "ITEM DESCRIPTION",
                    "QTY.",
                    "VALUE",
                    "QTY.",
                    "VALUE",
                    "QTY.",
                    "VALUE",
                    "CLOSING",
                    "",
                )
            )
        canons = [c["canonical"] for c in result["columns"]]
        self.assertEqual(canons[0], "product_name")
        self.assertEqual(canons[1], "opening_qty")
        self.assertEqual(canons[2], "opening_value")
        self.assertEqual(canons[3], "purchase_qty")
        self.assertEqual(canons[4], "purchase_value")
        self.assertEqual(canons[5], "sales_qty")
        self.assertEqual(canons[6], "sales_value")
        self.assertEqual(canons[7], "closing_qty")
        self.assertEqual(canons[8], "closing_value")


class TestCellLayoutRepair(unittest.TestCase):
    def test_clarina_421_70_is_opening_value_not_receipts_qty(self):
        env = {
            "STOCK_VISION_CELL_LAYOUT_REPAIR": "true",
            "STOCK_HEADER_QTY_VALUE_SEQUENCE": "true",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            mapped = map_vision_table(_broken_vision_table(), request_id="excel-photo")
        by_name = {
            str(i.get("product_name") or "").upper(): i
            for i in mapped.get("line_items") or []
        }
        clarina = by_name["CLARINA CREAM"]
        self.assertEqual(clarina.get("opening_qty"), 4.0)
        self.assertEqual(clarina.get("opening_value"), 421.7)
        self.assertEqual(clarina.get("receipts_qty"), 0.0)
        self.assertNotEqual(clarina.get("receipts_qty"), 421.7)
        self.assertEqual(clarina.get("closing_qty"), 4.0)
        self.assertEqual(clarina.get("closing_value"), 421.7)
        self.assertEqual(clarina.get("packing"), "30GM")
        # Must not treat Excel row number as the product.
        self.assertNotEqual(clarina.get("product_name"), "9")

    def test_cystone_opening_receipt_sales_closing_not_shifted(self):
        env = {"STOCK_VISION_CELL_LAYOUT_REPAIR": "true"}
        with mock.patch.dict(os.environ, env, clear=False):
            mapped = map_vision_table(_broken_vision_table(), request_id="excel-photo")
        cystone = next(
            i
            for i in mapped["line_items"]
            if "CYSTONE" in str(i.get("product_name") or "").upper()
        )
        self.assertEqual(cystone.get("opening_qty"), 1.0)
        self.assertEqual(cystone.get("opening_value"), 164.15)
        self.assertEqual(cystone.get("receipts_qty"), 15.0)
        self.assertEqual(cystone.get("sales_qty"), 16.0)
        self.assertEqual(cystone.get("closing_qty"), 5.0)

    def test_zero_rows_not_shifted(self):
        env = {"STOCK_VISION_CELL_LAYOUT_REPAIR": "true"}
        with mock.patch.dict(os.environ, env, clear=False):
            mapped = map_vision_table(_broken_vision_table(), request_id="excel-photo")
        for name in ("DIAREX TAB", "EVECARE CAP"):
            row = next(
                i
                for i in mapped["line_items"]
                if str(i.get("product_name") or "").upper() == name
            )
            self.assertEqual(row.get("opening_qty"), 0.0)
            self.assertEqual(row.get("receipts_qty"), 0.0)
            self.assertEqual(row.get("sales_qty"), 0.0)
            self.assertEqual(row.get("closing_qty"), 0.0)

    def test_liv52_receipts_and_sales_stay_in_columns(self):
        env = {"STOCK_VISION_CELL_LAYOUT_REPAIR": "true"}
        with mock.patch.dict(os.environ, env, clear=False):
            mapped = map_vision_table(_broken_vision_table(), request_id="excel-photo")
        liv = next(
            i
            for i in mapped["line_items"]
            if "LIV 52" in str(i.get("product_name") or "").upper()
        )
        self.assertEqual(liv.get("opening_qty"), 0.0)
        self.assertEqual(liv.get("receipts_qty"), 100.0)
        self.assertEqual((liv.get("extra") or {}).get("purchase_value"), 18410.6)
        self.assertEqual(liv.get("sales_qty"), 46.0)
        self.assertEqual(liv.get("sales_value"), 8468.88)
        self.assertEqual(liv.get("closing_qty"), 54.0)
        self.assertEqual(liv.get("closing_value"), 9941.72)

    def test_total_row_extracted_separately_and_aligned(self):
        env = {"STOCK_VISION_CELL_LAYOUT_REPAIR": "true"}
        with mock.patch.dict(os.environ, env, clear=False):
            mapped = map_vision_table(_broken_vision_table(), request_id="excel-photo")
        totals = mapped.get("totals_rows") or []
        self.assertTrue(totals)
        total = totals[0]
        self.assertRegex(str(total.get("product_name") or ""), r"TOTAL", re.I)
        self.assertEqual(total.get("opening_qty"), 40.0)
        self.assertEqual(total.get("opening_value"), 643.55)
        self.assertEqual(total.get("receipts_qty"), 115.0)
        self.assertEqual(total.get("sales_qty"), 86.0)
        self.assertEqual(total.get("closing_qty"), 69.0)
        # TOTAL must not appear as a product line.
        for item in mapped.get("line_items") or []:
            self.assertNotRegex(str(item.get("product_name") or ""), r"^\s*TOTAL\b", re.I)

    def test_infer_layout_from_cells(self):
        rows = _broken_vision_table()["tables"][0]["rows"]
        canons = _infer_qty_value_canons_from_rows(rows, 16)
        self.assertIsNotNone(canons)
        assert canons is not None
        self.assertEqual(canons[1], "product_name")
        self.assertEqual(canons[2], "pack")
        self.assertEqual(canons[3], "opening_qty")
        self.assertEqual(canons[4], "opening_value")
        self.assertEqual(canons[9], "closing_qty")
        self.assertEqual(canons[10], "closing_value")


    def test_pack_token_in_opening_shifts_qty_value(self):
        """Live Gemini shape: pack sits under OPENING header."""
        env = {"STOCK_VISION_CELL_LAYOUT_REPAIR": "true"}
        table = {
            "tables": [
                {
                    "table_index": 0,
                    "column_count": 10,
                    "header_rows": [
                        [
                            {"text": "ITEM DESCRIPTION", "col_index": 0, "x_center": 0.1},
                            {"text": "OPENING", "col_index": 1, "x_center": 0.2},
                            {"text": "VALUE", "col_index": 2, "x_center": 0.28},
                            {"text": "RECEIPT", "col_index": 3, "x_center": 0.36},
                            {"text": "VALUE", "col_index": 4, "x_center": 0.44},
                            {"text": "ISSUE", "col_index": 5, "x_center": 0.52},
                            {"text": "VALUE", "col_index": 6, "x_center": 0.60},
                            {"text": "CLOSING", "col_index": 7, "x_center": 0.68},
                            {"text": "VALUE", "col_index": 8, "x_center": 0.76},
                            {"text": "DUMP", "col_index": 9, "x_center": 0.88},
                        ]
                    ],
                    "rows": [
                        {
                            "row_index": 0,
                            "y_center": 0.4,
                            "is_total_row": False,
                            "cells": [
                                "CLARINA CREAM",
                                "30GM",
                                "4",
                                "421.70",
                                None,
                                "0.00",
                                None,
                                "4",
                                "421.70",
                                "4",
                            ],
                        },
                        {
                            "row_index": 1,
                            "y_center": 0.42,
                            "is_total_row": False,
                            "cells": [
                                "CYSTONE TAB",
                                "1X60",
                                "1",
                                "164.15",
                                "15",
                                "2462.18",
                                "16",
                                "2626.32",
                                None,
                                "0.00",
                            ],
                        },
                    ],
                    "proposed_mapping": [],
                }
            ],
            "unreadable_cells": [],
        }
        with mock.patch.dict(os.environ, env, clear=False):
            mapped = map_vision_table(table, request_id="pack-shift")
        clarina = next(
            i
            for i in mapped["line_items"]
            if "CLARINA" in str(i.get("product_name") or "").upper()
        )
        self.assertEqual(clarina.get("packing"), "30GM")
        self.assertEqual(clarina.get("opening_qty"), 4.0)
        self.assertEqual(clarina.get("opening_value"), 421.7)
        self.assertEqual(clarina.get("receipts_qty"), 0.0)
        self.assertNotEqual(clarina.get("receipts_qty"), 421.7)
        self.assertEqual(clarina.get("closing_qty"), 4.0)
        self.assertEqual(clarina.get("closing_value"), 421.7)
        cystone = next(
            i
            for i in mapped["line_items"]
            if "CYSTONE" in str(i.get("product_name") or "").upper()
        )
        self.assertEqual(cystone.get("opening_qty"), 1.0)
        self.assertEqual(cystone.get("opening_value"), 164.15)
        self.assertEqual(cystone.get("receipts_qty"), 15.0)
        self.assertEqual(cystone.get("sales_qty"), 16.0)


class TestTableCropAndGate(unittest.TestCase):
    @unittest.skipUnless(TARGET.is_file(), "target image missing")
    def test_bright_table_crop_detects_spreadsheet_region(self):
        from services.sales_statement_extractor import _crop_bright_spreadsheet_region
        from PIL import Image
        import io

        with mock.patch.dict(
            os.environ, {"STOCK_SSA_PHOTO_TABLE_CROP": "true"}, clear=False
        ):
            cropped = _crop_bright_spreadsheet_region(TARGET.read_bytes())
        self.assertIsNotNone(cropped)
        assert cropped is not None
        img = Image.open(io.BytesIO(cropped))
        # Crop should be wider than tall (spreadsheet), not the full phone frame.
        self.assertGreater(img.width, img.height)
        self.assertLess(img.height, 2000)
        debug = Path("/tmp/excel_photo_table_crop_debug.jpg")
        debug.write_bytes(cropped)

    def test_fuzzy_gate_accepts_title_plus_money_without_value_token(self):
        from services.sales_statement_extractor import (
            _is_ssa_opening_receipt_issue_dump_text_fuzzy,
        )

        preview = (
            "STOCK & SALES ANALYSIS (HIMALAYA ZANDRA) 01/08/2026 31/08/2026\n"
            "CLARINA CREAM 4 421.70 0.00 0.00 4 421.70 CLOSING DUMP"
        )
        self.assertTrue(_is_ssa_opening_receipt_issue_dump_text_fuzzy(preview))
        self.assertFalse(
            _is_ssa_opening_receipt_issue_dump_text_fuzzy(
                "STOCK & SALES ANALYSIS RATE OPENING 12.00"
            )
        )





if __name__ == "__main__":
    unittest.main()
