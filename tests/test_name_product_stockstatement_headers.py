"""Name + Product headers: Product is SKU, Name is company (StockStatement)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.stock_header_resolver import assign_cells, resolve_columns


def _cells(*texts):
    return [
        {
            "text": t,
            "x_center": None,
            "col_index": i,
            "subheader_text": None,
        }
        for i, t in enumerate(texts)
    ]


class PreferProductOverNameTests(unittest.TestCase):
    def test_product_wins_over_bare_name_when_flagged(self):
        headers = (
            "Name",
            "Code",
            "Product",
            "Packing",
            "LMS",
            "OP.Stock",
            "Rcpts",
            "Sales",
            "SL.Value",
            "Free",
            "CL.Stk",
            "CL.Value",
            "Transit",
            "Transit.Val",
        )
        with patch.dict(
            "os.environ",
            {"STOCK_HEADER_PREFER_PRODUCT_OVER_NAME": "false"},
            clear=False,
        ):
            off = resolve_columns(_cells(*headers))
            off_by = {c["header_text"]: c["canonical"] for c in off["columns"]}
            self.assertEqual(off_by["Name"], "product_name")
            self.assertEqual(off_by["Product"], "ignore")

        with patch.dict(
            "os.environ",
            {"STOCK_HEADER_PREFER_PRODUCT_OVER_NAME": "true"},
            clear=False,
        ):
            on = resolve_columns(_cells(*headers))
            on_by = {c["header_text"]: c for c in on["columns"]}
            self.assertEqual(on_by["Name"]["canonical"], "ignore")
            self.assertEqual(on_by["Name"]["reason"], "name_before_product")
            self.assertEqual(on_by["Product"]["canonical"], "product_name")
            self.assertEqual(on_by["Code"]["canonical"], "product_code")
            self.assertEqual(on_by["OP.Stock"]["canonical"], "opening_qty")
            self.assertEqual(on_by["Rcpts"]["canonical"], "purchase_qty")
            self.assertEqual(on_by["Sales"]["canonical"], "sales_qty")
            self.assertEqual(on_by["SL.Value"]["canonical"], "sales_value")
            self.assertEqual(on_by["CL.Stk"]["canonical"], "closing_qty")
            self.assertEqual(on_by["CL.Value"]["canonical"], "closing_value")

    def test_peel_leading_code_from_product_name(self):
        from services.stock_native_resolver import fields_to_line_item

        with patch.dict(
            "os.environ", {"STOCK_PDF_WORDS_PEEL_PRODUCT_CODE": "true"}, clear=False
        ):
            item = fields_to_line_item(
                {
                    "product_name": "60300 ABANA TAB #",
                    "pack": "60s",
                    "opening_qty": "57",
                    "purchase_qty": "0",
                    "sales_qty": "1",
                    "sales_value": "167.62",
                    "closing_qty": "56",
                    "closing_value": "9386.72",
                    "lms": "7",
                }
            )
            self.assertEqual(item["product_code"], "60300")
            self.assertEqual(item["product_name"], "ABANA TAB #")
            self.assertEqual(item["opening_qty"], 57.0)
            self.assertEqual(item["extra"].get("lms"), 7.0)

            glued = fields_to_line_item(
                {"product_name": "HIMALAYA- 60306 AACTARIL SOAP 75gm", "pack": "75gm"}
            )
            self.assertEqual(glued["product_code"], "60306")
            self.assertEqual(glued["product_name"], "AACTARIL SOAP 75gm")

    def test_trailing_transit_val_does_not_null_closing_value(self):
        """Transit.Val 0.00 must not CELL_COLLISION with CL.Value amount."""
        width = 1000.0

        def _h(text, x0, x1, idx):
            return {
                "text": text,
                "col_index": idx,
                "x0": x0,
                "x1": x1,
                "x_center": ((x0 + x1) / 2.0) / width,
                "subheader_text": None,
            }

        headers = [
            _h("Name", 50, 80, 0),
            _h("Code", 110, 140, 1),
            _h("Product", 150, 200, 2),
            _h("Packing", 280, 320, 3),
            _h("LMS", 350, 380, 4),
            _h("OP.Stock", 400, 430, 5),
            _h("Rcpts", 450, 480, 6),
            _h("Sales", 490, 520, 7),
            _h("SL.Value", 540, 580, 8),
            _h("Free", 720, 745, 9),
            _h("CL.Stk", 760, 790, 10),
            _h("CL.Value", 810, 850, 11),
            _h("Transit", 860, 890, 12),
            _h("Transit.Val", 910, 950, 13),
        ]
        env = {
            "STOCK_HEADER_PREFER_PRODUCT_OVER_NAME": "true",
            "STOCK_HEADER_ABSORB_INTERIOR_IGNORE": "true",
            "STOCK_HEADER_ABSORB_IGNORE_GEOMETRY": "false",
            "STOCK_HEADER_PREFER_NON_IGNORE_DEAD_ZONE": "true",
        }
        with patch.dict("os.environ", env, clear=False):
            cols = resolve_columns(headers)["columns"]
            by_idx = {int(h["col_index"]): h for h in headers}
            for c in cols:
                h = by_idx.get(int(c["col_index"]))
                if h:
                    c["x_center"] = h["x_center"]
            tokens = [
                {"text": "60300 ABANA TAB #", "x": 0.175},
                {"text": "60s", "x": 0.30},
                {"text": "7", "x": 0.365},
                {"text": "57", "x": 0.415},
                {"text": "0", "x": 0.465},
                {"text": "1", "x": 0.505},
                {"text": "167.62", "x": 0.56},
                {"text": "0", "x": 0.732},
                {"text": "56", "x": 0.775},
                {"text": "9386.72", "x": 0.830},
                {"text": "0.00", "x": 0.930},
            ]
            fields = assign_cells(tokens, cols)["fields"]
            self.assertEqual(fields.get("product_name"), "60300 ABANA TAB #")
            self.assertEqual(fields.get("opening_qty"), "57")
            self.assertEqual(fields.get("sales_qty"), "1")
            self.assertEqual(fields.get("closing_qty"), "56")
            self.assertEqual(fields.get("closing_value"), "9386.72")
            self.assertEqual(fields.get("sales_value"), "167.62")


if __name__ == "__main__":
    unittest.main()
