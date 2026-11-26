"""Sale and Stock Analysis: Pack|Opening|Purchase|Purchase Free|…|Sale|Approx Sale Free|…

Phone Excel truncates Purchase Free → Purchase Fre (was duplicate purchase_qty)
and large SALE/CLOSING qtys looked like money so PUR_FREE case_d parked Sale Free
into closing. Keep printed sale/closing when identity already balances.
"""

from __future__ import annotations

import unittest

from services.stock_header_resolver import resolve_columns
from services.stock_vision_table import _repair_pur_free_sale_closing_left_shift


class PurchaseFreHeaderAliasTests(unittest.TestCase):
    def test_truncated_purchase_fre_is_free_in(self):
        headers = [
            "Item Name",
            "Pack",
            "Opening",
            "Purchase",
            "Purchase Fre",
            "Purchase Re",
            "Sale",
            "Appro",
            "Sale Free",
            "Sale Return",
            "Others",
            "Closing",
        ]
        cells = [
            {"text": h, "col_index": i, "x_center": 0.05 + i * 0.07}
            for i, h in enumerate(headers)
        ]
        resolved = resolve_columns(cells)
        by_idx = {
            int(c["col_index"]): c.get("canonical")
            for c in resolved.get("columns") or []
        }
        self.assertEqual(by_idx[3], "purchase_qty")
        self.assertEqual(by_idx[4], "free_in_qty")
        self.assertEqual(by_idx[6], "sales_qty")
        self.assertEqual(by_idx[8], "free_out_qty")
        self.assertEqual(by_idx[11], "closing_qty")


class PurFreeSkipWhenQtyIdentityHoldsTests(unittest.TestCase):
    def test_large_sale_closing_with_sale_free_not_case_d(self):
        # V-52 SYP 200ML-style: open − sale − free ≈ close on qty-only grid.
        item = {
            "product_name": "V-52 SYP 200ML",
            "opening_qty": 1876.0,
            "receipts_qty": 0.0,
            "sales_qty": 1255.0,
            "sales_value": None,
            "closing_qty": 623.0,
            "closing_value": None,
            "extra": {
                "free_out_qty": 1.0,
                "field_source": {
                    "opening_qty": "printed",
                    "sales_qty": "printed",
                    "free_out_qty": "printed",
                    "closing_qty": "printed",
                    "closing_value": "missing",
                    "sales_value": "missing",
                },
                "flags": [],
                "vision_column_headers": [
                    "Item Name",
                    "Pack",
                    "Opening",
                    "Purchase",
                    "Purchase Fre",
                    "Purchase Re",
                    "Sale",
                    "Appro",
                    "Sale Free",
                    "Sale Return",
                    "Others",
                    "Closing",
                ],
            },
        }
        self.assertFalse(_repair_pur_free_sale_closing_left_shift(item))
        self.assertEqual(item["sales_qty"], 1255.0)
        self.assertEqual(item["closing_qty"], 623.0)
        self.assertEqual(item["extra"]["free_out_qty"], 1.0)

    def test_purchase_plus_sale_free_identity_skips(self):
        item = {
            "product_name": "V-52 SYP 100ML",
            "opening_qty": 0.0,
            "receipts_qty": 1050.0,
            "purchase_qty": 1050.0,
            "sales_qty": 517.0,
            "closing_qty": 535.0,
            "extra": {
                "free_out_qty": 2.0,
                "field_source": {
                    "opening_qty": "missing",
                    "purchase_qty": "printed",
                    "receipts_qty": "printed",
                    "sales_qty": "printed",
                    "free_out_qty": "printed",
                    "closing_qty": "printed",
                    "closing_value": "missing",
                    "sales_value": "missing",
                },
                "flags": [],
                "vision_column_headers": [
                    "Item Name",
                    "Pack",
                    "Opening",
                    "Purchase",
                    "Purchase Fre",
                    "Sale",
                    "Sale Free",
                    "Closing",
                ],
            },
        }
        self.assertFalse(_repair_pur_free_sale_closing_left_shift(item))
        self.assertEqual(item["sales_qty"], 517.0)
        self.assertEqual(item["closing_qty"], 535.0)


if __name__ == "__main__":
    unittest.main()
