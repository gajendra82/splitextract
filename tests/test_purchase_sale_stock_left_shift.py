"""Stock and Sale: Closing qty under Out + Purchase|Sale|Stock slid left."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.stock_header_resolver import resolve_columns
from services.stock_vision_table import (
    _apply_purchase_sale_stock_left_shift_pass,
    _repair_purchase_sale_stock_left_shift,
)


def _header_cells(*texts):
    return [
        {
            "text": t,
            "x_center": float(i),
            "col_index": i,
            "subheader_text": None,
        }
        for i, t in enumerate(texts)
    ]


HEADERS = (
    "Product",
    "Pack",
    "Op",
    "Pur",
    "Gd.In",
    "Tot In",
    "Sale",
    "Gd.",
    "Out.",
    "Closing",
    "Purchase",
    "Sale",
    "Stock",
    "Near",
    "Exp",
)


def _columns():
    return resolve_columns(_header_cells(*HEADERS))["columns"]


def _item(
    *,
    name: str,
    opening: float,
    purchase: float,
    sales: float,
    physical: dict,
    closing_qty=None,
    purchase_value=None,
    sales_value=None,
    closing_value=None,
):
    fs = {
        "opening_qty": "printed",
        "purchase_qty": "printed",
        "sales_qty": "printed",
        "closing_qty": "printed" if closing_qty is not None else "missing",
        "purchase_value": "printed" if purchase_value is not None else "missing",
        "sales_value": "printed" if sales_value is not None else "missing",
        "closing_value": "printed" if closing_value is not None else "missing",
    }
    return {
        "product_name": name,
        "opening_qty": opening,
        "receipts_qty": purchase,
        "sales_qty": sales,
        "closing_qty": closing_qty,
        "sales_value": sales_value,
        "closing_value": closing_value if closing_value is not None else 0.0,
        "extra": {
            "purchase_value": purchase_value,
            "field_source": fs,
            "physical_cells": {str(k): v for k, v in physical.items()},
            "flags": [],
        },
    }


class PurchaseSaleStockLeftShiftTests(unittest.TestCase):
    def test_right_shifts_full_triad_liv52(self):
        # Vision: Out=105, Closing=37960, Purchase=41749.50, Sale=19929, Stock=null
        # Truth: Closing=105, Purchase=37960, Sale=41749.50, Stock=19929
        item = _item(
            name="LIV 52 DS TABS",
            opening=100.0,
            purchase=200.0,
            sales=195.0,
            closing_qty=37960.0,
            purchase_value=41749.50,
            sales_value=19929.0,
            closing_value=None,
            physical={
                0: "LIV 52 DS TABS",
                1: "60S",
                2: "100",
                3: "200",
                4: "0",
                5: "300",
                6: "195",
                7: "0",
                8: "105",
                9: "37960.00",
                10: "41749.50",
                11: "19929.00",
                12: None,
            },
        )
        self.assertTrue(_repair_purchase_sale_stock_left_shift(item, _columns()))
        self.assertEqual(item["closing_qty"], 105.0)
        self.assertEqual(item["extra"]["purchase_value"], 37960.0)
        self.assertEqual(item["sales_value"], 41749.50)
        self.assertEqual(item["closing_value"], 19929.0)
        self.assertEqual(item["extra"]["field_source"]["closing_value"], "printed")
        self.assertIn("PURCHASE_SALE_STOCK_LEFT_SHIFT", item["extra"]["flags"])

    def test_blank_purchase_value_skips_purchase_slot(self):
        # Zero Pur qty, blank Purchase money: Closing held Sale, Sale held Stock.
        item = _item(
            name="ARJUNA TABS",
            opening=55.0,
            purchase=0.0,
            sales=6.0,
            closing_qty=1485.72,
            purchase_value=None,
            sales_value=10810.87,
            closing_value=None,
            physical={
                0: "ARJUNA TABS",
                1: "60S",
                2: "55",
                3: "0",
                4: "0",
                5: "55",
                6: "6",
                7: "0",
                8: "49",
                9: "1485.72",
                10: None,
                11: "10810.87",
                12: None,
            },
        )
        self.assertTrue(_repair_purchase_sale_stock_left_shift(item, _columns()))
        self.assertEqual(item["closing_qty"], 49.0)
        self.assertIsNone(item["extra"]["purchase_value"])
        self.assertEqual(item["sales_value"], 1485.72)
        self.assertEqual(item["closing_value"], 10810.87)

    def test_case_b_restores_qty_when_stock_already_mapped(self):
        item = _item(
            name="BONNISPAZ DROP",
            opening=110.0,
            purchase=72.0,
            sales=135.0,
            closing_qty=None,
            purchase_value=4085.28,
            sales_value=8640.0,
            closing_value=2666.78,
            physical={
                0: "BONNISPAZ DROP",
                1: "15ML",
                2: "110",
                3: "72",
                4: "0",
                5: "182",
                6: "135",
                7: "0",
                8: "47",
                9: None,
                10: "4085.28",
                11: "8640.00",
                12: "2666.78",
            },
        )
        self.assertTrue(_repair_purchase_sale_stock_left_shift(item, _columns()))
        self.assertEqual(item["closing_qty"], 47.0)
        self.assertEqual(item["closing_value"], 2666.78)
        self.assertEqual(item["extra"]["purchase_value"], 4085.28)

    def test_flag_off_is_noop(self):
        item = _item(
            name="LIV 52 DS TABS",
            opening=100.0,
            purchase=200.0,
            sales=195.0,
            closing_qty=37960.0,
            purchase_value=41749.50,
            sales_value=19929.0,
            physical={
                8: "105",
                9: "37960.00",
                10: "41749.50",
                11: "19929.00",
                12: None,
            },
        )
        with patch.dict(
            "os.environ",
            {"STOCK_VISION_PURCHASE_SALE_STOCK_LEFT_SHIFT": "false"},
            clear=False,
        ):
            self.assertFalse(_repair_purchase_sale_stock_left_shift(item, _columns()))
        self.assertEqual(item["closing_qty"], 37960.0)

    def test_pass_fixes_rows(self):
        item = _item(
            name="LIV 52 DS TABS",
            opening=100.0,
            purchase=200.0,
            sales=195.0,
            closing_qty=37960.0,
            purchase_value=41749.50,
            sales_value=19929.0,
            physical={
                8: "105",
                9: "37960.00",
                10: "41749.50",
                11: "19929.00",
                12: None,
            },
        )
        fixed = _apply_purchase_sale_stock_left_shift_pass([item], _columns())
        self.assertEqual(fixed, 1)
        self.assertEqual(item["closing_value"], 19929.0)


if __name__ == "__main__":
    unittest.main()
