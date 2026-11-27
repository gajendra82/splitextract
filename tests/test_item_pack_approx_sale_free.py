"""Sale and Stock Analysis: Pack|Opening|Purchase|Purchase Free|…|Sale|Approx Sale Free|…

Phone Excel truncates Purchase Free → Purchase Fre (was duplicate purchase_qty)
and large SALE/CLOSING qtys looked like money so PUR_FREE case_d parked Sale Free
into closing. Keep printed sale/closing when identity already balances.

Vision stamps layout=item_pack_free_return with extraction_method=vision_table;
identity must not reclassify as SaleRet, and TOTAL money must land in
totals.extra.sales_value (same path as opening_value) so sales+value show.
"""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _apply_stock_identity_validation,
    _stock_identity_kind,
)
from services.stock_header_resolver import resolve_columns
from services.stock_native_resolver import apply_universal_stock_footer_financials
from services.stock_vision_table import (
    _promote_printed_total_row_money,
    _repair_pur_free_sale_closing_left_shift,
)


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


class VisionItemPackFreeSalesValueSurfaceTests(unittest.TestCase):
    def test_identity_kind_keeps_item_pack_free_despite_sale_return(self):
        result = {
            "line_items": [
                {
                    "product_name": "ACTARIL SOAP",
                    "opening_qty": 47.0,
                    "receipts_qty": 144.0,
                    "purchase_qty": 144.0,
                    "sales_qty": 27.0,
                    "closing_qty": 164.0,
                    "sales_value": None,
                    "closing_value": None,
                    "extra": {"sale_return": None, "free_out_qty": None},
                },
                {
                    "product_name": "ABECON TAB",
                    "opening_qty": 118.0,
                    "receipts_qty": 100.0,
                    "purchase_qty": 100.0,
                    "sales_qty": 30.0,
                    "closing_qty": 189.0,
                    "sales_value": None,
                    "closing_value": None,
                    "extra": {"sale_return": 1.0, "free_out_qty": None},
                },
            ],
            "totals": {
                "sales_qty": None,
                "sales_value": 793445.193,
                "closing_qty": None,
                "closing_value": 1162789.74,
                "extra": {
                    "extraction_method": "vision_table",
                    "layout": "item_pack_free_return",
                    "stock_identity_kind": "item_pack_free_return",
                    "qty_only": True,
                    "no_sales_value": True,
                    "opening_value": 1525348.73,
                    "purchase_value": 425709.11,
                },
            },
        }
        self.assertEqual(_stock_identity_kind(result), "item_pack_free_return")
        out = _apply_stock_identity_validation(result)
        extra = out["totals"]["extra"]
        self.assertEqual(extra.get("stock_identity_kind"), "item_pack_free_return")
        self.assertEqual(extra.get("layout"), "item_pack_free_return")
        self.assertEqual(out["totals"].get("sales_qty"), 57.0)
        self.assertEqual(extra.get("sales_qty"), 57.0)
        self.assertEqual(out["line_items"][0]["sales_qty"], 27.0)
        self.assertEqual(out["line_items"][1]["sales_qty"], 30.0)

    def test_promote_mirrors_sales_closing_into_extra(self):
        totals = {
            "sales_value": None,
            "closing_value": None,
            "extra": {},
        }
        rows = [
            {
                "product_name": "Grand Total",
                "opening_qty": None,
                "sales_qty": None,
                "closing_qty": None,
                "opening_value": 1525348.73,
                "sales_value": 793445.193,
                "closing_value": 1162789.74,
                "extra": {"purchase_value": 425709.11},
            }
        ]
        promoted = _promote_printed_total_row_money(totals, rows)
        self.assertIn("sales_value", promoted)
        self.assertIn("closing_value", promoted)
        self.assertEqual(totals["sales_value"], 793445.193)
        self.assertEqual(totals["closing_value"], 1162789.74)
        self.assertEqual(totals["extra"].get("sales_value"), 793445.193)
        self.assertEqual(totals["extra"].get("closing_value"), 1162789.74)
        self.assertTrue(totals["extra"].get("has_printed_sales_value"))

    def test_footer_financials_fill_extra_sales_when_totals_already_set(self):
        result = {
            "line_items": [
                {
                    "product_name": "ACTARIL SOAP",
                    "sales_qty": 27.0,
                    "sales_value": None,
                    "closing_qty": 164.0,
                    "closing_value": None,
                }
            ],
            "totals": {
                "sales_value": 793445.193,
                "closing_value": 1162789.74,
                "extra": {
                    "qty_only": True,
                    "no_sales_value": True,
                    "layout": "item_pack_free_return",
                    "vision_table_total_rows": [
                        {
                            "product_name": "Grand Total",
                            "sales_value": 793445.193,
                            "closing_value": 1162789.74,
                            "opening_value": 1525348.73,
                            "extra": {"purchase_value": 425709.11},
                        }
                    ],
                },
            },
        }
        out = apply_universal_stock_footer_financials(result)
        extra = out["totals"]["extra"]
        self.assertEqual(out["totals"]["sales_value"], 793445.193)
        self.assertEqual(extra.get("sales_value"), 793445.193)
        self.assertEqual(extra.get("closing_value"), 1162789.74)
        self.assertTrue(extra.get("has_printed_sales_value"))
        self.assertEqual(extra.get("line_sales_value_sum"), 793445.193)


if __name__ == "__main__":
    unittest.main()
