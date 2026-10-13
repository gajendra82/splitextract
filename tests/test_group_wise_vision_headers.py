"""Group Wise Sales Vision header map + per-row Cl.Stock shift."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.stock_header_resolver import assign_cells, resolve_columns
from services.sales_statement_extractor import _result_looks_like_ssa_analysis
from services.stock_vision_table import (
    _apply_cell_layout_qty_value_repair,
    _classify_layout_cell,
    _realign_group_wise_row_fields,
)


def _cells(*texts):
    return [
        {
            "text": t,
            "x_center": float(i * 10),
            "col_index": i,
            "subheader_text": None,
        }
        for i, t in enumerate(texts)
    ]


_GW_HEADERS = (
    "Product Name",
    "Strength",
    "Op.",
    "Purchase",
    "Free",
    "Purchase",
    "Purchase",
    "Sales",
    "Sales",
    "Sales",
    "Sales",
    "Cl.Stock",
)


class GroupWisePackClassifierTests(unittest.TestCase):
    def test_gr_pack_token_classified(self):
        self.assertEqual(_classify_layout_cell("75GR"), "pack")
        self.assertEqual(_classify_layout_cell("30 GR"), "pack")
        self.assertEqual(_classify_layout_cell("200ML"), "pack")


class GroupWiseCellLayoutGuardTests(unittest.TestCase):
    def test_duplicate_sales_does_not_force_qty_value_layout(self):
        with patch.dict(
            os.environ,
            {
                "STOCK_HEADER_GROUP_WISE_SEQUENCE": "true",
                "STOCK_VISION_CELL_LAYOUT_REPAIR": "true",
            },
            clear=False,
        ):
            resolved = resolve_columns(_cells(*_GW_HEADERS))
            cols = list(resolved["columns"])
            errors = [
                e.get("code") if isinstance(e, dict) else str(e)
                for e in resolved.get("errors") or []
            ]
            rows = [
                {
                    "cells": [
                        "LIV-52 SYRUP",
                        "200ML",
                        "615",
                        "0",
                        "0",
                        "0",
                        "0",
                        "123",
                        "0",
                        "492",
                        None,
                        None,
                    ]
                },
                {
                    "cells": [
                        "HAIR ZONE SOLUTION",
                        "60'S",
                        "53",
                        "0",
                        "0",
                        "0",
                        "0",
                        "0",
                        "0",
                        "3",
                        "0",
                        "50",
                    ]
                },
            ]
            repaired = _apply_cell_layout_qty_value_repair(cols, rows, 12, errors)
            by = {c["col_index"]: c["canonical"] for c in repaired}
            self.assertEqual(by[9], "sales_qty")
            self.assertEqual(by[11], "closing_qty")
            self.assertNotEqual(by[7], "sales_value")
            self.assertNotIn("CELL_LAYOUT_QTY_VALUE_REPAIR", errors)


class GroupWiseRowClosingShiftTests(unittest.TestCase):
    def test_collapsed_row_moves_closing_from_sales_slot(self):
        with patch.dict(
            os.environ, {"STOCK_HEADER_GROUP_WISE_SEQUENCE": "true"}, clear=False
        ):
            cols = resolve_columns(_cells(*_GW_HEADERS))["columns"]
            cells = [
                "LIV-52 HB CAPS",
                "200ML",
                "615",
                "0",
                "0",
                "0",
                "0",
                "123",
                "0",
                "492",
                None,
                None,
            ]
            fields = dict(
                assign_cells(
                    [{"col_index": i, "text": cells[i]} for i in range(12)],
                    cols,
                )["fields"]
            )
            self.assertEqual(fields.get("sales_return_qty"), "123")
            self.assertEqual(fields.get("sales_qty"), "492")
            self.assertIsNone(fields.get("closing_qty"))
            self.assertTrue(_realign_group_wise_row_fields(fields, cols, cells))
            self.assertEqual(fields.get("sales_qty"), "123")
            self.assertEqual(fields.get("closing_qty"), "492")
            self.assertEqual(fields.get("sales_return_qty"), "0")

    def test_span_shift_when_digits_in_ignore_and_free_out(self):
        with patch.dict(
            os.environ, {"STOCK_HEADER_GROUP_WISE_SEQUENCE": "true"}, clear=False
        ):
            cols = resolve_columns(_cells(*_GW_HEADERS))["columns"]
            # Vision parked sales under ignore (col8) and closing under free_out (col10).
            cells = [
                "LIV-52 HB CAPS",
                "200ML",
                "615",
                "0",
                "0",
                "0",
                "0",
                "0",
                "123",
                "0",
                "492",
                None,
            ]
            fields = dict(
                assign_cells(
                    [{"col_index": i, "text": cells[i]} for i in range(12)],
                    cols,
                )["fields"]
            )
            self.assertEqual(fields.get("sales_qty"), "0")
            self.assertEqual(fields.get("free_out_qty"), "492")
            self.assertTrue(_realign_group_wise_row_fields(fields, cols, cells))
            self.assertEqual(fields.get("sales_qty"), "123")
            self.assertEqual(fields.get("closing_qty"), "492")

    def test_full_width_row_untouched(self):
        with patch.dict(
            os.environ, {"STOCK_HEADER_GROUP_WISE_SEQUENCE": "true"}, clear=False
        ):
            cols = resolve_columns(_cells(*_GW_HEADERS))["columns"]
            cells = [
                "HAIR ZONE SOLUTION",
                "60'S",
                "53",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "3",
                "0",
                "50",
            ]
            fields = dict(
                assign_cells(
                    [{"col_index": i, "text": cells[i]} for i in range(12)],
                    cols,
                )["fields"]
            )
            before = dict(fields)
            self.assertFalse(_realign_group_wise_row_fields(fields, cols))
            self.assertEqual(fields, before)
            self.assertEqual(fields.get("sales_qty"), "3")
            self.assertEqual(fields.get("closing_qty"), "50")


class GroupWiseSsaRemapGuardTests(unittest.TestCase):
    def test_group_wise_headers_not_treated_as_ssa_analysis(self):
        result = {
            "report_title": None,
            "line_items": [
                {
                    "product_name": "AACTARIL SOAP",
                    "extra": {
                        "vision_column_headers": list(_GW_HEADERS),
                        "physical_cells": {str(i): "0" for i in range(12)},
                    },
                }
            ],
        }
        self.assertFalse(_result_looks_like_ssa_analysis(result))

    def test_ssa_item_remap_does_not_turn_pack_into_opening_on_group_wise(self):
        """Regression: SSA pack/qty parse must not run on Group Wise Strength rows."""
        from services.sales_statement_extractor import (
            _repair_ssa_analysis_item_from_physical_cells,
        )

        item = {
            "product_name": "AACTARIL SOAP",
            "packing": "75GR",
            "opening_qty": 0.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "closing_qty": 0.0,
            "extra": {
                "vision_column_headers": list(_GW_HEADERS),
                "physical_cells": {
                    "0": "AACTARIL SOAP",
                    "1": "75GR",
                    "2": "0",
                    "3": "0",
                    "4": "0",
                    "5": "0",
                    "6": "0",
                    "7": "0",
                    "8": "0",
                    "9": "0",
                    "10": "0",
                    "11": "0",
                },
                "field_source": {
                    "opening_qty": "printed",
                    "sales_qty": "printed",
                    "closing_qty": "printed",
                    "pack": "printed",
                },
            },
        }
        # Direct item remap still parses pack as opening — callers must skip GW.
        changed = _repair_ssa_analysis_item_from_physical_cells(item)
        self.assertTrue(changed)
        self.assertEqual(item["opening_qty"], 75.0)
        # Guard used by vision pre-snapshot / result remap:
        from services.sales_statement_extractor import (
            _item_looks_like_group_wise_sales_headers,
        )

        self.assertTrue(_item_looks_like_group_wise_sales_headers(item))


if __name__ == "__main__":
    unittest.main()
