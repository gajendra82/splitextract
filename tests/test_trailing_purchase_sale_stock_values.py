"""Stock and Sale: trailing Purchase|Sale|Stock after Closing are money columns."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.stock_header_resolver import assign_cells, resolve_columns


def _cells(*texts):
    return [
        {
            "text": t,
            "x_center": float(i),
            "col_index": i,
            "subheader_text": None,
        }
        for i, t in enumerate(texts)
    ]


class TrailingPurchaseSaleStockValuesTests(unittest.TestCase):
    HEADERS = (
        "Product",
        "Pack",
        "Op",
        "Pur",
        "Gd.In",
        "Tot",
        "In",
        "Sale",
        "Gd. Out.",
        "Closing",
        "Purchase",
        "Sale",
        "Stock",
        "NearExp",
    )

    def test_promotes_trailing_money_columns(self):
        resolved = resolve_columns(_cells(*self.HEADERS))
        by = {c["col_index"]: c for c in resolved["columns"]}
        self.assertEqual(by[3]["canonical"], "purchase_qty")
        self.assertEqual(by[7]["canonical"], "sales_qty")
        self.assertEqual(by[9]["canonical"], "closing_qty")
        self.assertEqual(by[10]["canonical"], "purchase_value")
        self.assertEqual(by[11]["canonical"], "sales_value")
        self.assertEqual(by[12]["canonical"], "closing_value")
        self.assertEqual(by[10]["reason"], "trailing_purchase_sale_stock_values")

    def test_flag_off_keeps_legacy_duplicate_behaviour(self):
        with patch.dict(
            "os.environ",
            {"STOCK_HEADER_TRAILING_PURCHASE_SALE_STOCK_VALUES": "false"},
            clear=False,
        ):
            resolved = resolve_columns(_cells(*self.HEADERS))
        # Without promotion, trailing Sale/Stock are ignored duplicates and
        # Purchase steals purchase_qty from Pur.
        canons = {c["canonical"] for c in resolved["columns"]}
        self.assertNotIn("purchase_value", canons)
        self.assertNotIn("sales_value", canons)
        self.assertNotIn("closing_value", canons)

    def test_assign_maps_printed_values(self):
        resolved = resolve_columns(_cells(*self.HEADERS))
        tokens = [
            {"text": t, "col_index": i, "x": float(i)}
            for i, t in enumerate(
                [
                    "BONNISAN DROPS",
                    "30ML",
                    "65",
                    "50",
                    "0",
                    "115",
                    "27",
                    "0",
                    "88",
                    "3309.50",
                    "2015.82",
                    "5824.72",
                ]
            )
        ]
        # Match geometry to header x_centers (col_index==x in this fixture).
        # Row has no separate Tot/In token pair — use index assign via no-x mode.
        for t in tokens:
            t.pop("x", None)
        # Headers keep x_center; clear them so assign uses col_index like stitch.
        cols = []
        for c in resolved["columns"]:
            cc = dict(c)
            cc.pop("x_center", None)
            cols.append(cc)
        # Rebuild tokens to 14 slots: Product..NearExp with empty NearExp.
        # Printed row skipped empty Tot/In split — map by known positions:
        # 0 name,1 pack,2 op,3 pur,4 gdin,5 totin,6 sale,7 gdout,8 closing,
        # 9 purchase_value,10 sales_value,11 closing_value
        # Our headers have Tot and In separate (5,6) so insert placeholder.
        mapped = [
            {"text": "BONNISAN DROPS", "col_index": 0},
            {"text": "30ML", "col_index": 1},
            {"text": "65", "col_index": 2},
            {"text": "50", "col_index": 3},
            {"text": "0", "col_index": 4},
            {"text": "115", "col_index": 5},
            {"text": "", "col_index": 6},
            {"text": "27", "col_index": 7},
            {"text": "0", "col_index": 8},
            {"text": "88", "col_index": 9},
            {"text": "3309.50", "col_index": 10},
            {"text": "2015.82", "col_index": 11},
            {"text": "5824.72", "col_index": 12},
        ]
        fields = assign_cells(mapped, cols)["fields"]
        self.assertEqual(fields["opening_qty"], "65")
        self.assertEqual(fields["purchase_qty"], "50")
        self.assertEqual(fields["sales_qty"], "27")
        self.assertEqual(fields["closing_qty"], "88")
        self.assertEqual(fields["purchase_value"], "3309.50")
        self.assertEqual(fields["sales_value"], "2015.82")
        self.assertEqual(fields["closing_value"], "5824.72")


if __name__ == "__main__":
    unittest.main()
