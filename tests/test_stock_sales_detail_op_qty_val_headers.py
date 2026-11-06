"""Stock and Sales Detail Report headers route to Op.Qty/Op.Val vision."""

from __future__ import annotations

import os
import unittest

from services.stock_direct_vision import (
    STOCK_SALES_OP_QTY_VAL_VISION_PROMPT,
    _has_stock_sales_op_qty_val_headers,
    _merge_detail_closing_repair,
    _stock_sales_detail_scan_per_page_enabled,
    _stock_sales_detail_widen_cl_enabled,
    _widen_detail_closing_columns,
    validate_stock_direct_vision,
)
from services.stock_header_resolver import normalize_header
from services.stock_native_tables import resolver_ready


class StockSalesDetailOpQtyValHeaderTests(unittest.TestCase):
    def test_detail_report_title_with_op_qty_cl_qty(self):
        text = (
            "Stock and Sales Detail Report\n"
            "Seller : RSM PHARMA PVT LTD\n"
            "Sl.No Item Op.Qty Op.Val P.Qty P.Sch P.Val S.Qty S.Sch S.Val Cl.Qty Cl.Val\n"
        )
        self.assertTrue(_has_stock_sales_op_qty_val_headers(text))

    def test_saleable_opn_rec_issue_not_matched(self):
        text = (
            "Saleable Stock Report\n"
            "Particular |Packing| Opn| Rec| Issue| Bal\n"
        )
        self.assertFalse(_has_stock_sales_op_qty_val_headers(text))

    def test_op_qty_normalizes_and_resolves_opening(self):
        self.assertEqual(normalize_header("Op.Qty"), "op qty")
        cells = [
            {"text": "Item", "col_index": 0, "x_center": 10},
            {"text": "Op.Qty", "col_index": 1, "x_center": 40},
            {"text": "Op.Val", "col_index": 2, "x_center": 55},
            {"text": "P.Qty", "col_index": 3, "x_center": 70},
            {"text": "S.Qty", "col_index": 4, "x_center": 85},
            {"text": "Cl.Qty", "col_index": 5, "x_center": 100},
            {"text": "Cl.Val", "col_index": 6, "x_center": 115},
        ]
        ready = resolver_ready(cells)
        self.assertTrue(ready.get("resolved"))
        canons = {
            str(c.get("canonical") or "") for c in (ready.get("columns") or [])
        }
        self.assertIn("opening_qty", canons)
        self.assertIn("opening_value", canons)
        self.assertIn("closing_qty", canons)

    def test_prompt_maps_cr_qty_to_sales_return_not_ignore(self):
        prompt = STOCK_SALES_OP_QTY_VAL_VISION_PROMPT
        self.assertIn("sales_return_qty", prompt)
        self.assertIn("Cr.Qty", prompt)
        self.assertIn("ASHWAGANDHA GUMMIES 60S", prompt)
        self.assertIn("186600.0", prompt)
        self.assertNotIn("~138600", prompt)

    def test_identity_includes_sales_return_for_detail_layout(self):
        """Op + P - S + Cr ≈ Cl (Cr is sales return on Detail reports)."""
        rows = [
            {
                "product_name": "ASHWAGANDHA GUMMIES 60S",
                "opening_qty": 316,
                "receipts_qty": 0,
                "sales_qty": 7,
                "sales_return_qty": 2,
                "closing_qty": 311,
                "closing_value": 186600.0,
                "extra": {"sale_return": 2},
            },
            {
                "product_name": "BONNISAN DROPS (30ML)",
                "opening_qty": 0,
                "receipts_qty": 300,
                "sales_qty": 167,
                "sales_return_qty": 5,
                "closing_qty": 138,
                "closing_value": 10303.08,
                "extra": {"sale_return": 5},
            },
        ] + [
            {
                "product_name": f"FILLER PRODUCT {i}",
                "opening_qty": 1,
                "receipts_qty": 0,
                "sales_qty": 0,
                "closing_qty": 1,
                "closing_value": 10,
            }
            for i in range(4)
        ]
        validation = validate_stock_direct_vision(
            {"line_items": rows}, layout="stock_sales_op_qty_val"
        )
        self.assertTrue(validation.get("ok"), validation)
        self.assertEqual(validation.get("identity_fail_count"), 0)

    def test_closing_repair_matches_same_key_by_order(self):
        base = {
            "line_items": [
                {
                    "product_name": "CYSTONE",
                    "opening_qty": 1,
                    "closing_qty": 1,
                    "closing_value": 1,
                },
                {
                    "product_name": "CYSTONE",
                    "opening_qty": 2,
                    "closing_qty": 2,
                    "closing_value": 2,
                },
            ]
        }
        repair = {
            "line_items": [
                {
                    "product_name": "CYSTONE",
                    "closing_qty": 10,
                    "closing_value": 100,
                },
                {
                    "product_name": "CYSTONE",
                    "closing_qty": 20,
                    "closing_value": 200,
                },
            ]
        }
        patched = _merge_detail_closing_repair(base, repair)
        self.assertEqual(patched, 2)
        self.assertEqual(base["line_items"][0]["closing_value"], 100)
        self.assertEqual(base["line_items"][1]["closing_value"], 200)

    def test_scan_per_page_flag_defaults_on(self):
        prev = os.environ.pop("STOCK_SALES_DETAIL_SCAN_PER_PAGE", None)
        try:
            self.assertTrue(_stock_sales_detail_scan_per_page_enabled())
            os.environ["STOCK_SALES_DETAIL_SCAN_PER_PAGE"] = "0"
            self.assertFalse(_stock_sales_detail_scan_per_page_enabled())
        finally:
            if prev is None:
                os.environ.pop("STOCK_SALES_DETAIL_SCAN_PER_PAGE", None)
            else:
                os.environ["STOCK_SALES_DETAIL_SCAN_PER_PAGE"] = prev

    def test_widen_cl_flag_defaults_on(self):
        prev = os.environ.pop("STOCK_SALES_DETAIL_WIDEN_CL", None)
        try:
            self.assertTrue(_stock_sales_detail_widen_cl_enabled())
            os.environ["STOCK_SALES_DETAIL_WIDEN_CL"] = "0"
            self.assertFalse(_stock_sales_detail_widen_cl_enabled())
        finally:
            if prev is None:
                os.environ.pop("STOCK_SALES_DETAIL_WIDEN_CL", None)
            else:
                os.environ["STOCK_SALES_DETAIL_WIDEN_CL"] = prev

    def test_widen_detail_closing_columns_makes_image_wider(self):
        from PIL import Image
        import io

        img = Image.new("RGB", (400, 200), (255, 255, 255))
        for x in range(300, 400):
            for y in range(200):
                img.putpixel((x, y), (240, 240, 240))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        out = _widen_detail_closing_columns(buf.getvalue())
        widened = Image.open(io.BytesIO(out))
        self.assertGreater(widened.width, 400)
        self.assertEqual(widened.height, 200)

    def test_prompt_forbids_op_val_as_closing_value(self):
        prompt = STOCK_SALES_OP_QTY_VAL_VISION_PROMPT
        self.assertIn("Never copy Op.Val", prompt)
        self.assertIn("red ZOOM panel", prompt)


if __name__ == "__main__":
    unittest.main()
