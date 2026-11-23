"""Pack|Rate|Opening|Receipt|Sales|Sale Value|Closing|Cl Value cell-layout."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.stock_vision_table import (
    _clear_neighbor_stolen_closing_fields,
    _compare_line_sums_to_printed_total,
    _infer_qty_value_canons_from_rows,
    _money_from_total_raw_cells,
    _promote_printed_total_row_money,
    _repair_closing_value_parked_in_sales_value,
    _repair_pack_rate_opening_receipt_sales_in_free,
    _repair_qty_copied_into_value_fields,
)


def _row(cells, *, total=False):
    return {"cells": list(cells), "is_total_row": bool(total)}


class PackRateSaleValueLayoutTest(unittest.TestCase):
    def test_infers_sale_value_before_closing_on_wide_grid(self):
        rows = [
            _row(
                [
                    "PUNARNAVA",
                    "60",
                    "176.42",
                    "0",
                    "0",
                    "2",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "2",
                    "353",
                    "0",
                ]
            ),
            _row(
                [
                    "SEPTILIN T",
                    "60",
                    "173.58",
                    "608",
                    "0",
                    "58",
                    "0",
                    "1",
                    "0",
                    "141",
                    "0",
                    "0",
                    "57",
                    "7642",
                    "0",
                ]
            ),
            _row(
                [
                    "SPEMAN T",
                    "60",
                    "167.51",
                    "0",
                    "0",
                    "7",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "7",
                    "845",
                    "0",
                ]
            ),
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_PACK_RATE_SALE_VALUE_LAYOUT": "true"}
        ):
            canons = _infer_qty_value_canons_from_rows(rows, 15)
        self.assertIsNotNone(canons)
        assert canons is not None
        self.assertEqual(canons[0], "product_name")
        # Bare pack tokens like "60" may classify as small-int; they must not
        # become opening_qty. Sale Value / Closing slots are what matter.
        self.assertNotEqual(canons[1], "opening_qty")
        self.assertEqual(canons[5], "opening_qty")
        self.assertEqual(canons[7], "sales_qty")
        self.assertEqual(canons[9], "sales_value")
        self.assertEqual(canons[12], "closing_qty")
        self.assertEqual(canons[13], "closing_value")

    def test_free_column_keeps_opening_and_sales_unshifted(self):
        """Printed Free between Sales and Sales RTN must not steal Opening."""
        rows = [
            _row(
                [
                    "ASHWAGANDHA TABLETS 60 S INDIA",
                    "60 S",
                    "185.1",
                    "0",
                    "0",
                    "0",
                    "63",
                    "60",
                    "0",
                    "0",
                    "11666",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    None,
                ]
            ),
            _row(
                [
                    "GASEX TABS 100S",
                    "100S",
                    "156.2",
                    "1",
                    "1314",
                    "3",
                    "35",
                    "1393",
                    "0",
                    "0",
                    "206245",
                    "0",
                    "0",
                    "21",
                    "3263",
                    "0",
                    "0",
                    None,
                ]
            ),
            _row(
                [
                    "HIMCOID SUSPAXXL 200 ML",
                    "200 ML",
                    "110.5",
                    "0",
                    "0",
                    "32",
                    "0",
                    "7",
                    "0",
                    "0",
                    "773",
                    "0",
                    "0",
                    "25",
                    "2762",
                    "0",
                    "0",
                    None,
                ]
            ),
            _row(
                [
                    "RUMALAYA LINIMENT 60ML",
                    "60ML",
                    "128",
                    "11",
                    "58",
                    "82",
                    "0",
                    "17",
                    "0",
                    "0",
                    "2176",
                    "0",
                    "0",
                    "65",
                    "8320",
                    "0",
                    "0",
                    None,
                ]
            ),
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_PACK_RATE_SALE_VALUE_LAYOUT": "true"}
        ):
            canons = _infer_qty_value_canons_from_rows(rows, 18)
        self.assertIsNotNone(canons)
        assert canons is not None
        self.assertEqual(canons[0], "product_name")
        self.assertEqual(canons[1], "pack")
        self.assertEqual(canons[2], "rate")
        self.assertEqual(canons[5], "opening_qty")
        self.assertEqual(canons[6], "purchase_qty")
        self.assertEqual(canons[7], "sales_qty")
        self.assertEqual(canons[8], "free_out_qty")
        self.assertEqual(canons[9], "sales_return_qty")
        self.assertEqual(canons[10], "sales_value")
        self.assertEqual(canons[13], "closing_qty")
        self.assertEqual(canons[14], "closing_value")

    def test_truncated_row_maps_sale_value_not_closing_qty(self):
        rows = [
            _row(
                ["LIV 52 DS S 100ML", "151.96", "283", "17", "43", "0", "1", "171", "0"]
            ),
            _row(["LUKOL TAB", "170.88", "0", "0", "26", "0", "0", "0", "0"]),
            _row(["LASUNA TA", "156.82", "0", "0", "40", "0", "0", "0", "0"]),
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_PACK_RATE_SALE_VALUE_LAYOUT": "true"}
        ):
            canons = _infer_qty_value_canons_from_rows(rows, 9)
        self.assertIsNotNone(canons)
        assert canons is not None
        self.assertEqual(canons[0], "product_name")
        self.assertEqual(canons[1], "rate")
        self.assertEqual(canons[4], "opening_qty")
        self.assertEqual(canons[6], "sales_qty")
        self.assertEqual(canons[7], "sales_value")
        self.assertNotEqual(canons[7], "closing_qty")

    def test_total_raw_cells_recover_sale_and_closing_value(self):
        printed = {
            "product_name": "----Total---->>",
            "sales_value": 11754.0,
            "closing_value": 0.0,
            "extra": {
                "vision_raw_cells": [
                    "--Total-->>",
                    "0",
                    "165727",
                    "7966",
                    "44833",
                    "0",
                    "1416",
                    "0",
                    "1416",
                    "11754",
                    "0",
                    "32243",
                    "32242.74",
                    "0",
                    "0",
                ]
            },
        }
        with patch.dict(
            os.environ, {"STOCK_VISION_PACK_RATE_SALE_VALUE_LAYOUT": "true"}
        ):
            money = _money_from_total_raw_cells(printed)
        self.assertEqual(money.get("sales_value"), 1416.0)
        self.assertEqual(money.get("closing_value"), 32242.74)

    def test_promote_printed_total_fills_null_totals(self):
        totals = {"sales_value": None, "closing_value": None, "extra": {}}
        rows = [
            {
                "product_name": "----Total---->>",
                "sales_value": 1416.0,
                "closing_value": 1416.0,
                "extra": {
                    "vision_raw_cells": [
                        "--Total-->>",
                        "0",
                        "165727",
                        "7966",
                        "44833",
                        "0",
                        "1416",
                        "0",
                        "1416",
                        "11754",
                        "0",
                        "32243",
                        "32242.74",
                        "0",
                        "0",
                    ]
                },
            }
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_PACK_RATE_SALE_VALUE_LAYOUT": "true"}
        ):
            promoted = _promote_printed_total_row_money(totals, rows)
        self.assertIn("sales_value", promoted)
        self.assertIn("closing_value", promoted)
        self.assertEqual(totals["sales_value"], 1416.0)
        self.assertEqual(totals["closing_value"], 32242.74)

    def test_pack_rate_opening_receipt_sales_in_free_shift(self):
        """KOFLET-style: Opening under Receipt, Sales under Free — reassign only."""
        item = {
            "product_name": "KOFLET KIDZ COUGH SYRUP 100ML",
            "opening_qty": 0.0,
            "receipts_qty": 56.0,
            "sales_qty": 0.0,
            "sales_value": 0.0,
            "closing_qty": 0.0,
            "closing_value": 0.0,
            "extra": {
                "free_out_qty": 3.0,
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "sales_qty": "printed",
                    "free_out_qty": "printed",
                    "sales_value": "printed",
                    "closing_qty": "printed",
                    "closing_value": "printed",
                },
                "vision_column_headers": [
                    "Product Name",
                    "Pack",
                    "Rate",
                    "Previous Last Sale",
                    "Previous Sale",
                    "Opening",
                    "Receipt",
                    "Sales",
                    "Free",
                    "Sales RTN",
                    "Sale Value",
                    "Closing",
                    "Cl Value",
                ],
            },
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_VISION_PACK_RATE_SALE_VALUE_LAYOUT": "true",
                "STOCK_VISION_PACK_RATE_OPEN_SALES_IN_FREE": "true",
            },
        ):
            self.assertTrue(_repair_pack_rate_opening_receipt_sales_in_free(item))
        self.assertEqual(item["opening_qty"], 56.0)
        self.assertEqual(item["receipts_qty"], 0.0)
        self.assertEqual(item["sales_qty"], 3.0)
        self.assertIsNone(item["extra"]["free_out_qty"])

    def test_closing_value_parked_in_sales_value_from_neighbor(self):
        """SHILAJIT: Cl Value under Sale Value; next row's Cl Value duplicated."""
        item = {
            "product_name": "SHILAJIT CAPSULE",
            "opening_qty": 28.0,
            "receipts_qty": 0.0,
            "sales_qty": 24.0,
            "sales_value": 7429.0,
            "closing_qty": 26.0,
            "closing_value": 9025.0,
            "extra": {"field_source": {}},
        }
        nxt = {
            "product_name": "SHILAJIT GOLD CAPSULES",
            "opening_qty": 23.0,
            "receipts_qty": 24.0,
            "sales_qty": 0.0,
            "sales_value": 9417.0,
            "closing_qty": 23.0,
            "closing_value": 9025.0,
            "extra": {"field_source": {}},
        }
        with patch.dict(
            os.environ, {"STOCK_VISION_CLOSING_VALUE_IN_SALES_VALUE": "true"}
        ):
            self.assertTrue(
                _repair_closing_value_parked_in_sales_value(item, nxt)
            )
        self.assertEqual(item["closing_value"], 7429.0)
        self.assertIsNone(item["sales_value"])

    def test_neighbor_closing_steal_cleared(self):
        """RUMALAYA GEL must not keep LINIMENT's Closing 65."""
        gel = {
            "product_name": "RUMALAYA GEL 30G",
            "opening_qty": 0.0,
            "receipts_qty": 0.0,
            "sales_qty": 1.0,
            "sales_value": 107.0,
            "closing_qty": 65.0,
            "closing_value": 20265.0,
            "extra": {"field_source": {}},
        }
        lin = {
            "product_name": "RUMALAYA LINIMENT 60ML",
            "opening_qty": 82.0,
            "receipts_qty": 0.0,
            "sales_qty": 17.0,
            "sales_value": 2176.0,
            "closing_qty": 65.0,
            "closing_value": 8320.0,
            "extra": {"field_source": {}},
        }
        with patch.dict(
            os.environ, {"STOCK_VISION_CLEAR_NEIGHBOR_CLOSING_STEAL": "true"}
        ):
            n = _clear_neighbor_stolen_closing_fields([gel, lin])
        self.assertEqual(n, 1)
        self.assertIsNone(gel["closing_qty"])
        self.assertEqual(gel["closing_value"], 20265.0)
        self.assertEqual(lin["closing_qty"], 65.0)

    def test_qty_copied_into_value_skips_both_zero(self):
        item = {
            "product_name": "ZERO ROW",
            "sales_qty": 0.0,
            "sales_value": 0.0,
            "closing_qty": 0.0,
            "closing_value": 0.0,
            "extra": {
                "field_source": {
                    "sales_qty": "printed",
                    "sales_value": "printed",
                    "closing_qty": "printed",
                    "closing_value": "printed",
                }
            },
        }
        self.assertFalse(_repair_qty_copied_into_value_fields(item))
        self.assertNotIn(
            "QTY_COPIED_INTO_VALUE", (item["extra"].get("flags") or [])
        )

    def test_printed_total_keeps_large_opening_qty_when_cl_value_has_paisa(self):
        printed = {
            "product_name": "----Total---->>",
            "opening_qty": 228851.0,
            "opening_value": None,
            "receipts_qty": 82872.0,
            "receipts_value": None,
            "sales_qty": 225351.0,
            "sales_value": 231332.0,
            "closing_qty": 76713.0,
            "closing_value": 76713.21,
            "extra": {},
        }
        items = [
            {
                "opening_qty": 35.0,
                "receipts_qty": 1300.0,
                "sales_qty": 1314.0,
                "sales_value": 206245.0,
                "closing_qty": 21.0,
                "closing_value": 3253.0,
            }
        ]
        out = _compare_line_sums_to_printed_total(items, [printed])
        self.assertTrue(out.get("compared"))
        self.assertEqual(out["printed_total"]["opening_qty"], 228851.0)
        self.assertEqual(out["printed_total"]["opening_value"], 0.0)
        self.assertEqual(out["printed_total"]["sales_value"], 231332.0)
        self.assertEqual(out["printed_total"]["closing_value"], 76713.21)


if __name__ == "__main__":
    unittest.main()
