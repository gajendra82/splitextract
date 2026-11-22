"""Equal-width SALE/CLOSING titles on VALUE still map QTY columns.

STOCK & SALES ANALYSIS prints <===SALE===> / <==CLOSING==> on the VALUE cell
while QTY sits in the empty parent column. Index-join then drops both qty
columns and every line item extracts as 0.
"""

from __future__ import annotations

import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from services.sales_statement_extractor import (
    _xls_best_header,
    _xls_fanout_movement_qty_value,
    _xls_header_colmap,
    extract_sales_statement,
)


PARENT = ["", "", "<===SALE===>", "", "<==CLOSING==>", " RE-"]
CHILD = ["ITEM DESCRIPTION", "QTY.", "VALUE", "QTY.", "VALUE", "ORDER"]

LIVE_XLS = Path(r"C:\Users\Prerana Bhalerao\Downloads\HIM GANDHI-1.XLS")


def _xlsx_bytes(rows):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sale_closing_rows():
    return [
        ["ESS KAY MEDICOSE"],
        ["NEAR PROF.S.S.MEMORIAL PUBLIC SCHOOL OUTSIDE CHANDANA GATE, KAITHAL"],
        ["Phone : 9812729999 E-Mail : esskaymedicose@gmail.com"],
        [
            "STOCK & SALES ANALYSIS  (HIMALAYA ZANDRA) "
            "01-09-2026 - 30-09-2026  Reorder : Sale X 1.50"
        ],
        PARENT,
        CHILD,
        [],
        ["HIMALAYA ZANDRA"],
        [
            "ARJUNA TABLETS                60'S",
            19.0,
            4192.0,
            47.0,
            11638.0,
            "   -",
        ],
        [
            "BONNISAN LIQUID               100 ML",
            28.0,
            1683.0,
            47.0,
            3187.0,
            "   -",
        ],
        [
            "CYSTONE TABLETS               60",
            9.0,
            1433.0,
            91.0,
            16848.0,
            "   -",
        ],
        [" Total", 1457.0, 238502.0, 1895.0, 321397.0, 1363.0],
    ]


class TestXlsSaleClosingQtyValueOffset(unittest.TestCase):
    def test_offset_fanout_pairs_sale_and_closing_qty(self):
        with patch.dict(
            os.environ, {"STOCK_XLS_MOVEMENT_QTY_VALUE_FANOUT_OFFSET": "true"}
        ):
            combined = _xls_fanout_movement_qty_value(PARENT, CHILD)
        self.assertIsNotNone(combined)
        colmap = _xls_header_colmap(combined)
        self.assertEqual(colmap["item"], 0)
        self.assertEqual(colmap["sale"], 1)
        self.assertEqual(colmap["sval"], 2)
        self.assertEqual(colmap["bal"], 3)
        self.assertEqual(colmap["bval"], 4)

    def test_flag_off_skips_equal_width_fanout(self):
        with patch.dict(
            os.environ, {"STOCK_XLS_MOVEMENT_QTY_VALUE_FANOUT_OFFSET": "false"}
        ):
            self.assertIsNone(_xls_fanout_movement_qty_value(PARENT, CHILD))
            idx, colmap, score = _xls_best_header(_sale_closing_rows())
        self.assertIsNotNone(idx)
        self.assertGreater(score, 0)
        self.assertNotIn("sale", colmap)
        self.assertNotIn("bal", colmap)

    def test_aligned_qty_value_pair_is_unchanged(self):
        """Index-join already works when OPENING/SALE sit on QTY, not VALUE."""
        parents = ["ITEM", "OPENING", "OPENING", "SALE", "SALE"]
        children = ["", "QTY", "VALUE", "QTY", "VALUE"]
        with patch.dict(
            os.environ, {"STOCK_XLS_MOVEMENT_QTY_VALUE_FANOUT_OFFSET": "true"}
        ):
            self.assertIsNone(_xls_fanout_movement_qty_value(parents, children))

    def test_extracts_printed_sale_and_closing_qty(self):
        data = _xlsx_bytes(_sale_closing_rows())
        with patch.dict(
            os.environ, {"STOCK_XLS_MOVEMENT_QTY_VALUE_FANOUT_OFFSET": "true"}
        ):
            result = extract_sales_statement(data, "sale-closing.xlsx")
        items = result["line_items"]
        names = [item["product_name"] for item in items]
        self.assertNotIn("HIMALAYA ZANDRA", names)
        self.assertFalse(any("TOTAL" in name.upper() for name in names))
        by_name = {item["product_name"]: item for item in items}
        arjuna = next(item for item in items if "ARJUNA" in item["product_name"])
        self.assertEqual(arjuna["sales_qty"], 19.0)
        self.assertEqual(arjuna["sales_value"], 4192.0)
        self.assertEqual(arjuna["closing_qty"], 47.0)
        self.assertEqual(arjuna["closing_value"], 11638.0)
        self.assertEqual(arjuna["opening_qty"], 0.0)
        self.assertEqual(arjuna["receipts_qty"], 0.0)
        bonni = next(item for item in items if "BONNISAN LIQUID" in item["product_name"])
        self.assertEqual(bonni["sales_qty"], 28.0)
        self.assertEqual(bonni["closing_qty"], 47.0)
        cystone = next(
            item for item in items if "CYSTONE TABLETS" in item["product_name"]
        )
        self.assertEqual(cystone["sales_qty"], 9.0)
        self.assertEqual(cystone["closing_qty"], 91.0)
        self.assertEqual(result["totals"]["sales_value"], 238502.0)
        self.assertEqual(result["totals"]["closing_value"], 321397.0)
        self.assertGreaterEqual(len(by_name), 3)

    def test_flag_off_extract_keeps_zero_qty(self):
        data = _xlsx_bytes(_sale_closing_rows())
        with patch.dict(
            os.environ, {"STOCK_XLS_MOVEMENT_QTY_VALUE_FANOUT_OFFSET": "false"}
        ):
            result = extract_sales_statement(data, "sale-closing.xlsx")
        arjuna = next(
            item
            for item in result["line_items"]
            if "ARJUNA" in item["product_name"]
        )
        self.assertEqual(arjuna["sales_qty"], 0.0)
        self.assertEqual(arjuna["closing_qty"], 0.0)
        self.assertEqual(arjuna["sales_value"], 4192.0)
        self.assertEqual(arjuna["closing_value"], 11638.0)

    def test_item_op_sale_bal_layout_unchanged(self):
        data = _xlsx_bytes(
            [
                ["Item", "Op.", "Sale", "Bal."],
                ["LIV 52 TAB", 10, 2, 8],
            ]
        )
        with patch.dict(
            os.environ, {"STOCK_XLS_MOVEMENT_QTY_VALUE_FANOUT_OFFSET": "true"}
        ):
            result = extract_sales_statement(data, "legacy.xlsx")
        liv = result["line_items"][0]
        self.assertEqual(liv["product_name"], "LIV 52 TAB")
        self.assertEqual(liv["opening_qty"], 10.0)
        self.assertEqual(liv["sales_qty"], 2.0)
        self.assertEqual(liv["closing_qty"], 8.0)

    @unittest.skipUnless(LIVE_XLS.is_file(), "live HIM GANDHI-1.XLS is missing")
    def test_live_gandhi_xls_printed_qty(self):
        with patch.dict(
            os.environ, {"STOCK_XLS_MOVEMENT_QTY_VALUE_FANOUT_OFFSET": "true"}
        ):
            result = extract_sales_statement(
                LIVE_XLS.read_bytes(), LIVE_XLS.name
            )
        items = result["line_items"]
        self.assertGreaterEqual(len(items), 40)
        names = [item["product_name"] for item in items]
        self.assertNotIn("HIMALAYA ZANDRA", names)
        arjuna = next(item for item in items if "ARJUNA" in item["product_name"].upper())
        self.assertEqual(arjuna["sales_qty"], 19.0)
        self.assertEqual(arjuna["sales_value"], 4192.0)
        self.assertEqual(arjuna["closing_qty"], 47.0)
        self.assertEqual(arjuna["closing_value"], 11638.0)
        self.assertEqual(result["totals"]["sales_value"], 238502.0)
        self.assertEqual(result["totals"]["closing_value"], 321397.0)
        nonzero = sum(
            1
            for item in items
            if (item.get("sales_qty") or 0) or (item.get("closing_qty") or 0)
        )
        self.assertGreater(nonzero, 30)


if __name__ == "__main__":
    unittest.main()
