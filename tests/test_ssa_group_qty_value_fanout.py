"""STOCK & SALES ANALYSIS: OPENING|RECEIPT|ISSUE|CLOSING × QTY|VALUE header fanout.

Vision often emits a flat dual header where parent bands restart the child
QTY/VALUE row at col_index 1 (not 0). Without split+fanout, CLOSING lands on
ignore and numbers shift to the wrong end of the row.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.stock_header_resolver import resolve_columns
from services.stock_vision_table import (
    _flatten_header_cells,
    _normalize_header_rows,
)


def _flat_truncated_parent_child_headers():
    return [
        {"text": "ITEM DESCRIPTION", "col_index": 0},
        {"text": "OPENIN", "col_index": 1},
        {"text": "RECEI", "col_index": 2},
        {"text": "ISSU", "col_index": 3},
        {"text": "CLOSI", "col_index": 4},
        {"text": "QTY.", "col_index": 1},
        {"text": "VALUE", "col_index": 2},
        {"text": "QTY.", "col_index": 3},
        {"text": "VALUE", "col_index": 4},
        {"text": "QTY.", "col_index": 5},
        {"text": "VALUE", "col_index": 6},
        {"text": "QTY.", "col_index": 7},
        {"text": "VALUE", "col_index": 8},
    ]


class SsaGroupQtyValueFanoutTests(unittest.TestCase):
    def test_flat_header_splits_when_child_row_starts_at_one(self):
        rows = _normalize_header_rows(_flat_truncated_parent_child_headers(), {})
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [c["text"] for c in rows[0]],
            ["ITEM DESCRIPTION", "OPENIN", "RECEI", "ISSU", "CLOSI"],
        )
        self.assertEqual(rows[1][0]["col_index"], 1)
        self.assertEqual(rows[1][0]["text"], "QTY.")

    def test_fanout_maps_closing_qty_and_value(self):
        with patch.dict(
            os.environ, {"STOCK_VISION_GROUP_QTY_VALUE_FANOUT": "true"}, clear=False
        ):
            rows = _normalize_header_rows(_flat_truncated_parent_child_headers(), {})
            cells = _flatten_header_cells(rows, 9)
        resolved = resolve_columns(cells)
        by = {c["col_index"]: c["canonical"] for c in resolved["columns"]}
        self.assertEqual(by[0], "product_name")
        self.assertEqual(by[1], "opening_qty")
        self.assertEqual(by[2], "opening_value")
        self.assertEqual(by[3], "purchase_qty")
        self.assertEqual(by[4], "purchase_value")
        self.assertEqual(by[5], "sales_qty")
        self.assertEqual(by[6], "sales_value")
        self.assertEqual(by[7], "closing_qty")
        self.assertEqual(by[8], "closing_value")

    def test_fanout_off_does_not_assign_full_oric_pair_grid(self):
        with patch.dict(
            os.environ, {"STOCK_VISION_GROUP_QTY_VALUE_FANOUT": "false"}, clear=False
        ):
            rows = _normalize_header_rows(_flat_truncated_parent_child_headers(), {})
            cells = _flatten_header_cells(rows, 9)
        resolved = resolve_columns(cells)
        canons = [c["canonical"] for c in resolved["columns"]]
        self.assertNotEqual(
            canons,
            [
                "product_name",
                "opening_qty",
                "opening_value",
                "purchase_qty",
                "purchase_value",
                "sales_qty",
                "sales_value",
                "closing_qty",
                "closing_value",
            ],
        )

    def test_bare_qty_value_headers_layout_assigns_closing(self):
        """Child-only QTY/VALUE row (parents dropped) still maps ORIC pairs."""
        from services.stock_vision_table import _apply_cell_layout_qty_value_repair

        columns = [
            {
                "col_index": i,
                "header_text": h,
                "canonical": "product_name" if i == 0 else "ignore",
                "confidence": 1.0 if i == 0 else 0.0,
                "is_value": False,
                "reason": "alias" if i == 0 else "unknown",
            }
            for i, h in enumerate(
                [
                    "ITEM DESCRIPTION",
                    "QTY.",
                    "VALUE",
                    "QTY.",
                    "VALUE",
                    "QTY.",
                    "VALUE",
                    "QTY.",
                    "VALUE",
                ]
            )
        ]
        rows = [
            {
                "cells": [
                    "AACTARIL SOAP",
                    "-11",
                    "-793.43",
                    "-",
                    "0.00",
                    "-",
                    "0.00",
                    "-11",
                    "-793.43",
                ]
            },
            {
                "cells": [
                    "CONFIDO TAB",
                    "45",
                    "6667.92",
                    "0",
                    "0.00",
                    "11",
                    "1778.11",
                    "34",
                    "5044.00",
                ]
            },
        ]
        errors: list = []
        with patch.dict(
            os.environ,
            {
                "STOCK_VISION_GROUP_QTY_VALUE_FANOUT": "true",
                "STOCK_VISION_CELL_LAYOUT_REPAIR": "false",
            },
            clear=False,
        ):
            repaired = _apply_cell_layout_qty_value_repair(
                columns, rows, 9, errors, force=False
            )
        by = {c["col_index"]: c["canonical"] for c in repaired}
        self.assertEqual(by[1], "opening_qty")
        self.assertEqual(by[7], "closing_qty")
        self.assertEqual(by[8], "closing_value")
        self.assertIn("CELL_LAYOUT_QTY_VALUE_REPAIR", errors)

    def test_exif_default_transposes_phone_portrait_landscape_buffer(self):
        """Landscape buffer + Orientation=6 must upright (CLOSING at right end)."""
        import io

        from PIL import Image

        from services.stock_vision_table import _exif_orient_image_bytes

        im = Image.new("RGB", (800, 600), color=(255, 255, 255))
        exif = im.getexif()
        exif[274] = 6
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90, exif=exif.tobytes())
        raw = buf.getvalue()
        with patch.dict(
            os.environ,
            {
                "STOCK_VISION_EXIF_TRANSPOSE": "true",
                "STOCK_VISION_EXIF_SKIP_LANDSCAPE_BUFFER": "false",
            },
            clear=False,
        ):
            out = _exif_orient_image_bytes(raw)
        with Image.open(io.BytesIO(out)) as oriented:
            self.assertEqual(oriented.size, (600, 800))

    def test_pack_peel_six_cells_pads_issue_so_closing_stays_closing(self):
        """open|mid|close (open=close) → pad blank ISSUE; no sales↔closing shuffle."""
        from services.stock_vision_table import _align_oric_qv_cells_after_pack_peel

        columns = [
            {"col_index": i, "canonical": c}
            for i, c in enumerate(
                [
                    "product_name",
                    "opening_qty",
                    "opening_value",
                    "purchase_qty",
                    "purchase_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                ]
            )
        ]
        # After pack peel: open, receipt 0/0, closing (issue dropped).
        cells = [
            "AACTARIL SOAP",
            "-11",
            "-793.43",
            "0.00",
            "0.00",
            "-11",
            "-793.43",
            None,
            None,
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_ORIC_QV_PACK_CELL_ALIGN": "true"}, clear=False
        ):
            out, width = _align_oric_qv_cells_after_pack_peel(
                cells, columns, 9, peeled_pack=True
            )
        self.assertEqual(width, 9)
        self.assertEqual(out[1], "-11")
        self.assertEqual(out[2], "-793.43")
        self.assertEqual(out[3], "0.00")
        self.assertEqual(out[4], "0.00")
        self.assertIsNone(out[5])
        self.assertIsNone(out[6])
        self.assertEqual(out[7], "-11")
        self.assertEqual(out[8], "-793.43")

    def test_pack_peel_seven_cells_pads_blank_receipt_value(self):
        """Blank receipt VALUE dropped → insert null so ISSUE/CLOSING stay put."""
        from services.stock_vision_table import _align_oric_qv_cells_after_pack_peel

        columns = [
            {"col_index": i, "canonical": c}
            for i, c in enumerate(
                [
                    "product_name",
                    "opening_qty",
                    "opening_value",
                    "purchase_qty",
                    "purchase_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                ]
            )
        ]
        cells = [
            "CONFIDO TAB",
            "45",
            "6667.92",
            "0.00",
            "11",
            "1778.11",
            "34",
            "5037.98",
            None,
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_ORIC_QV_PACK_CELL_ALIGN": "true"}, clear=False
        ):
            out, _width = _align_oric_qv_cells_after_pack_peel(
                cells, columns, 9, peeled_pack=True
            )
        self.assertEqual(out[3], "0.00")
        self.assertIsNone(out[4])
        self.assertEqual(out[5], "11")
        self.assertEqual(out[6], "1778.11")
        self.assertEqual(out[7], "34")
        self.assertEqual(out[8], "5037.98")

    def test_pack_peel_six_cells_sellthrough_pads_receipt_value(self):
        """ABANA: open|rq|iq|iv|cq with blank rv — ISSUE/CLOSING must not slide left."""
        from services.stock_vision_table import _align_oric_qv_cells_after_pack_peel

        columns = [
            {"col_index": i, "canonical": c}
            for i, c in enumerate(
                [
                    "product_name",
                    "opening_qty",
                    "opening_value",
                    "purchase_qty",
                    "purchase_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                ]
            )
        ]
        cells = [
            "ABANA TAB",
            "29",
            "4309.40",
            "0.00",
            "29",
            "4309.40",
            "0.00",
            None,
            None,
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_ORIC_QV_PACK_CELL_ALIGN": "true"}, clear=False
        ):
            out, width = _align_oric_qv_cells_after_pack_peel(
                cells, columns, 8, peeled_pack=True
            )
        self.assertEqual(width, 9)
        self.assertEqual(out[5], "29")
        self.assertEqual(out[6], "4309.40")
        self.assertEqual(out[7], "0.00")
        self.assertIsNone(out[4])

    def test_pack_peel_six_cells_negative_open_close_restores_closing(self):
        """Negative open=close: duplicate pair under ISSUE → closing, not sales."""
        from services.stock_vision_table import _align_oric_qv_cells_after_pack_peel

        columns = [
            {"col_index": i, "canonical": c}
            for i, c in enumerate(
                [
                    "product_name",
                    "opening_qty",
                    "opening_value",
                    "purchase_qty",
                    "purchase_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                ]
            )
        ]
        cells = [
            "CLARINA FACE",
            "-3",
            "-173.52",
            "0.00",
            "-3",
            "-173.52",
            "0.00",
            None,
            None,
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_ORIC_QV_PACK_CELL_ALIGN": "true"}, clear=False
        ):
            out, _width = _align_oric_qv_cells_after_pack_peel(
                cells, columns, 8, peeled_pack=True
            )
        self.assertIsNone(out[5])
        self.assertIsNone(out[6])
        self.assertEqual(out[7], "-3")
        self.assertEqual(out[8], "-173.52")


if __name__ == "__main__":
    unittest.main()
