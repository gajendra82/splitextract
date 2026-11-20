"""Op.Stock / Cl.Stock / Stock Value word PDFs: split headers + band absorb."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.stock_header_resolver import assign_cells, resolve_columns
from services.stock_native_tables import _merge_split_stock_header_cells


def _hdr(*texts_and_x):
    """Build header cells from (text, x0, x1) triples; page width 1000."""
    cells = []
    for i, (text, x0, x1) in enumerate(texts_and_x):
        cells.append(
            {
                "text": text,
                "col_index": i,
                "x0": float(x0),
                "x1": float(x1),
                "x_center": ((float(x0) + float(x1)) / 2.0) / 1000.0,
                "subheader_text": None,
            }
        )
    return cells


class MergeSplitStockHeaderTests(unittest.TestCase):
    def test_op_stock_and_stock_value_merge_when_flagged(self):
        cells = _hdr(
            ("SlNo", 30, 52),
            ("Product", 70, 107),
            ("Name", 110, 137),
            ("Op.", 430, 446),
            ("Stock", 449, 476),
            ("Pur.", 489, 508),
            ("Cl.Stock", 700, 740),
            ("Stock", 976, 1003),
            ("Value", 1006, 1032),
        )
        with patch.dict(
            "os.environ",
            {"STOCK_PDF_WORDS_MERGE_SPLIT_STOCK_HEADERS": "false"},
            clear=False,
        ):
            off = _merge_split_stock_header_cells(cells, 1000)
            self.assertEqual(len(off), 9)

        with patch.dict(
            "os.environ",
            {"STOCK_PDF_WORDS_MERGE_SPLIT_STOCK_HEADERS": "true"},
            clear=False,
        ):
            on = _merge_split_stock_header_cells(cells, 1000)
            texts = [c["text"] for c in on]
            self.assertIn("Product Name", texts)
            self.assertIn("Op. Stock", texts)
            self.assertIn("Stock Value", texts)
            self.assertNotIn("Op.", texts)
            resolved = resolve_columns(on)
            by = {c["header_text"]: c["canonical"] for c in resolved["columns"]}
            self.assertEqual(by["Op. Stock"], "opening_qty")
            self.assertEqual(by["Cl.Stock"], "closing_qty")
            self.assertEqual(by["Stock Value"], "closing_value")
            self.assertNotEqual(by.get("Op. Stock"), "closing_qty")


class FreeSkipIgnoreNeighbourTests(unittest.TestCase):
    def test_free_after_qtypur_is_free_in(self):
        cells = [
            {
                "text": t,
                "x_center": None,
                "col_index": i,
                "subheader_text": None,
            }
            for i, t in enumerate(
                ("Product", "Op. Stock", "Pur.", "QtyPur.", "Free", "Sales", "Free", "Cl.Stock")
            )
        ]
        with patch.dict(
            "os.environ",
            {"STOCK_HEADER_FREE_SKIP_IGNORE": "false"},
            clear=False,
        ):
            off = resolve_columns(cells)
            off_by = {c["col_index"]: c["canonical"] for c in off["columns"]}
            # Without skip, Free after ignore QtyPur. takes Sales → free_out.
            self.assertEqual(off_by[4], "free_out_qty")

        with patch.dict(
            "os.environ",
            {"STOCK_HEADER_FREE_SKIP_IGNORE": "true"},
            clear=False,
        ):
            on = resolve_columns(cells)
            on_by = {c["col_index"]: c["canonical"] for c in on["columns"]}
            self.assertEqual(on_by[4], "free_in_qty")
            self.assertEqual(on_by[6], "free_out_qty")


class InteriorIgnoreAndDeadZoneTests(unittest.TestCase):
    def test_closing_qty_not_nulled_by_box_dead_zone(self):
        """89 sits between Cl.Stock and Box — keep closing_qty."""
        # Geometry mirrors landscape Stock And Sales words (page width 1169).
        width = 1169.0

        def _cell(text, x0, x1, idx):
            return {
                "text": text,
                "col_index": idx,
                "x0": float(x0),
                "x1": float(x1),
                "x_center": ((float(x0) + float(x1)) / 2.0) / width,
                "subheader_text": None,
            }

        headers = [
            _cell("Product Name", 70, 137.8, 0),
            _cell("Pack", 299, 322.4, 1),
            _cell("BLM", 352, 373.7, 2),
            _cell("QtyLM", 376.4, 407.4, 3),
            _cell("Qty", 410.2, 426.9, 4),
            _cell("Op. Stock", 430, 476.7, 5),
            _cell("Pur.", 489, 508.4, 6),
            _cell("QtyPur.", 511.2, 546.4, 7),
            _cell("Free", 549.2, 570.3, 8),
            _cell("Sales", 582, 608.1, 9),
            _cell("QtySales", 610.9, 651.1, 10),
            _cell("Free", 653.9, 675.0, 11),
            _cell("Cl.Stock", 700, 740.0, 12),
            _cell("Box", 759, 777.9, 13),
            _cell("Pack", 780.7, 804.0, 14),
            _cell("Cus.Required", 838, 903.0, 15),
            _cell("QTtoytSRqty", 905.8, 952.4, 16),
            _cell("Stock Value", 976, 1032.7, 17),
        ]
        env = {
            "STOCK_HEADER_FREE_SKIP_IGNORE": "true",
            "STOCK_HEADER_ABSORB_INTERIOR_IGNORE": "true",
            "STOCK_HEADER_PREFER_NON_IGNORE_DEAD_ZONE": "true",
            "STOCK_HEADER_ABSORB_IGNORE_GEOMETRY": "false",
        }
        with patch.dict("os.environ", env, clear=False):
            cols = resolve_columns(headers)["columns"]
            by_idx = {int(h["col_index"]): h for h in headers}
            for c in cols:
                h = by_idx.get(int(c["col_index"]))
                if h:
                    c["x_center"] = h["x_center"]

            tokens = [
                {"text": "ABANA TAB", "x": 0.0839},
                {"text": "60'S", "x": 0.2642},
                {"text": "21", "x": 0.3229},
                {"text": "34", "x": 0.3545},
                {"text": "108", "x": 0.4026},
                {"text": "0", "x": 0.4399},
                {"text": "0", "x": 0.4869},
                {"text": "19", "x": 0.5213},
                {"text": "0", "x": 0.5879},
                {"text": "89", "x": 0.6360},
                {"text": "100x60'S", "x": 0.6670},
                {"text": "0", "x": 0.7658},
                {"text": "0", "x": 0.8240},
                {"text": "13225.40", "x": 0.8667},
            ]
            fields = assign_cells(tokens, cols)["fields"]
            self.assertEqual(fields.get("opening_qty"), "108")
            self.assertEqual(fields.get("purchase_qty"), "0")
            self.assertEqual(fields.get("sales_qty"), "19")
            self.assertEqual(fields.get("closing_qty"), "89")
            self.assertEqual(fields.get("closing_value"), "13225.40")
            self.assertEqual(fields.get("product_name"), "ABANA TAB")


if __name__ == "__main__":
    unittest.main()
