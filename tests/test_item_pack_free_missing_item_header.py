"""FREE/P.RETURN qty-only: unlabeled ITEM under PACK header.

Vision omits ITEM chrome and labels col0 PACK while cells are still
name|pack|opening|…|sale|…|closing. Without remint, product_name is null →
zero_product_rows and SALE/CLOSING are lost.
"""

from __future__ import annotations

import unittest

from services.stock_vision_table import (
    _column_map_item_pack_free_return,
    _is_non_product_vision_row,
    _repair_missing_item_before_pack,
    map_vision_table,
)

_HDR = [
    "PACK",
    "OPENING",
    "PURCHASE",
    "FREE",
    "P.RETURN",
    "FREE",
    "SALE",
    "FREE",
    "S.RETURN",
    "FREE",
    "OTHERS",
    "CLOSING",
]
_CANONS_WRONG = [
    "pack",
    "opening_qty",
    "purchase_qty",
    "free_in_qty",
    "purchase_return_qty",
    "free_out_qty",
    "sales_qty",
    "ignore",
    "sales_return_qty",
    "ignore",
    "other_outward_qty",
    "closing_qty",
]


def _header_rows():
    return [
        [
            {
                "text": h,
                "col_index": i,
                "x_center": 0.05 + i * 0.07,
            }
            for i, h in enumerate(_HDR)
        ]
    ]


def _proposed_mapping():
    return [
        {"col_index": i, "canonical": c, "confidence": 0.9}
        for i, c in enumerate(_CANONS_WRONG)
    ]


def _row(name: str, pack: str, opening: float, sales: float, closing: float, idx: int):
    # name|pack|opening|purchase|free|pret|sale|free|sret|free|others|closing
    cells = [
        name,
        pack,
        str(int(opening)),
        "-",
        "-",
        "-",
        str(int(sales)),
        "-",
        "-",
        "-",
        "-",
        str(int(closing)),
    ]
    return {"row_index": idx, "cells": cells, "is_total_row": False}


class MissingItemBeforePackRepairTests(unittest.TestCase):
    def test_division_label_with_pack_and_sale_kept(self):
        """Phone Excel: first SKU mislabeled HIMALAYA ZEAL still carries SALE/CLOSING."""
        item = {
            "product_name": "HIMALAYA ZEAL",
            "packing": "75G.",
            "opening_qty": None,
            "sales_qty": 11.0,
            "closing_qty": 77.0,
            "extra": {
                "field_source": {
                    "sales_qty": "printed",
                    "closing_qty": "printed",
                    "pack": "printed",
                }
            },
        }
        self.assertFalse(_is_non_product_vision_row(item))
        bare = {
            "product_name": "HIMALAYA ZEAL",
            "packing": None,
            "opening_qty": 0.0,
            "sales_qty": 0.0,
            "closing_qty": None,
            "extra": {"field_source": {}},
        }
        self.assertTrue(_is_non_product_vision_row(bare))

    def test_votes_remap_leading_canons_keeps_sale_closing(self):
        columns = [
            {"col_index": i, "header_text": h, "canonical": c}
            for i, (h, c) in enumerate(zip(_HDR, _CANONS_WRONG))
        ]
        self.assertTrue(_column_map_item_pack_free_return(columns))
        rows = [
            _row("FIDO TAB", "60'S", 41, 16, 25, 0),
            _row("CONFIDO TAB", "60'S", 66, 11, 55, 1),
            _row("MERICOBAL PLUS", "60'S", 4, 4, 0, 2),
        ]
        errors: list = []
        out = _repair_missing_item_before_pack(columns, rows, errors)
        self.assertIn("MISSING_ITEM_BEFORE_PACK", errors)
        canons = [c["canonical"] for c in out]
        self.assertEqual(canons[0], "product_name")
        self.assertEqual(canons[1], "pack")
        self.assertEqual(canons[2], "opening_qty")
        self.assertEqual(canons[3], "purchase_qty")
        self.assertEqual(canons[6], "sales_qty")
        self.assertEqual(canons[11], "closing_qty")

    def test_map_vision_keeps_sales_and_closing(self):
        vision = {
            "column_count": 12,
            "header_rows": _header_rows(),
            "proposed_mapping": _proposed_mapping(),
            "rows": [
                # Header band leaked into body (seen on portrait Excel screenshots).
                {
                    "row_index": 0,
                    "cells": list(_HDR),
                    "is_total_row": False,
                },
                # Division label + pack + sale/closing = first SKU mis-OCR.
                {
                    "row_index": 1,
                    "cells": [
                        "HIMALAYA ZEAL",
                        "75G.",
                        "-",
                        "-",
                        "-",
                        "-",
                        "11",
                        "-",
                        "-",
                        "-",
                        "-",
                        "77",
                    ],
                    "is_total_row": False,
                },
                _row("FIDO TAB", "60'S", 41, 16, 25, 2),
                _row("CONFIDO TAB", "60'S", 66, 11, 55, 3),
                _row("ASHWAGANDHA", "60'S", 20, 5, 15, 4),
            ],
        }
        mapped = map_vision_table(vision, request_id="missing-item-pack")
        self.assertIn("MISSING_ITEM_BEFORE_PACK", mapped.get("errors") or [])
        items = mapped.get("line_items") or []
        self.assertGreaterEqual(len(items), 3)
        by_name = {str(i.get("product_name") or ""): i for i in items}
        self.assertNotIn("PACK", by_name)
        self.assertIn("HIMALAYA ZEAL", by_name)
        zeal = by_name["HIMALAYA ZEAL"]
        self.assertEqual(zeal.get("sales_qty"), 11.0)
        self.assertEqual(zeal.get("closing_qty"), 77.0)
        self.assertIn("FIDO TAB", by_name)
        fido = by_name["FIDO TAB"]
        self.assertEqual(fido.get("packing"), "60'S")
        self.assertEqual(fido.get("opening_qty"), 41.0)
        self.assertEqual(fido.get("sales_qty"), 16.0)
        self.assertEqual(fido.get("closing_qty"), 25.0)
        conf = by_name["CONFIDO TAB"]
        self.assertEqual(conf.get("opening_qty"), 66.0)
        self.assertEqual(conf.get("sales_qty"), 11.0)
        self.assertEqual(conf.get("closing_qty"), 55.0)


if __name__ == "__main__":
    unittest.main()
