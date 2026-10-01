"""Landscape Stock & Sale (OpStk/P.Qty/S.Qty/ClStk/ClVal/Order) photos."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _stock_sale_opstk_headers_ok,
    _stock_sale_opstk_order_result_ok,
    empty_result,
    extract_sales_statement,
)


LAXMI_PDF = Path(
    r"C:\Users\anuja\Downloads\ZA_2026_August"
    r"\0000700168_2026_08_ZA_06_330_05092026132522.pdf"
)


class TestStockSaleOpstkOrder(unittest.TestCase):
    def test_headers_gate_accepts_opstk_layout(self):
        parsed = {
            "detected_headers": [
                "PRODUCT NAME",
                "Jun 26",
                "Jul 26",
                "OpStk",
                "P.Qty",
                "P.Val",
                "S.Qty",
                "S.Val",
                "CrQty",
                "DbQty",
                "ClStk",
                "ClVal",
                "Order",
            ]
        }
        self.assertTrue(_stock_sale_opstk_headers_ok(parsed))

    def test_headers_gate_rejects_receipt_pur_format(self):
        # Format A (Deshbandhu Sales & Stock Receipt/Pur) must NOT be claimed.
        parsed = {
            "detected_headers": [
                "PRODUCT NAME",
                "PACKING",
                "Op.Bal",
                "Receipt/Pur Value",
                "Issue/Sales Value",
                "Closing",
            ]
        }
        self.assertFalse(_stock_sale_opstk_headers_ok(parsed))
        self.assertFalse(_stock_sale_opstk_headers_ok({}))
        self.assertFalse(_stock_sale_opstk_headers_ok({"detected_headers": []}))

    def test_result_ok_requires_rows_and_money(self):
        good = empty_result("x.pdf", "pdf")
        good["period_from"] = "2026-08-01"
        good["period_to"] = "2026-08-31"
        good["line_items"] = [
            {
                "product_name": f"PROD {i}",
                "opening_qty": 1.0,
                "receipts_qty": 1.0,
                "sales_qty": 1.0,
                "closing_qty": 1.0,
                "sales_value": 10.0,
                "closing_value": 20.0,
                "extra": {},
            }
            for i in range(6)
        ]
        self.assertTrue(_stock_sale_opstk_order_result_ok(good))

        bad_year = empty_result("x.pdf", "pdf")
        bad_year["period_from"] = "2001-08-01"
        bad_year["line_items"] = good["line_items"]
        self.assertFalse(_stock_sale_opstk_order_result_ok(bad_year))

        too_few = empty_result("x.pdf", "pdf")
        too_few["line_items"] = good["line_items"][:2]
        self.assertFalse(_stock_sale_opstk_order_result_ok(too_few))

    def test_live_laxmi_pdf_uses_dedicated_reader(self):
        if not LAXMI_PDF.exists():
            self.skipTest("E Laxmi fixture PDF not present")
        result = extract_sales_statement(LAXMI_PDF.read_bytes(), LAXMI_PDF.name)
        extra = ((result.get("totals") or {}).get("extra") or {})
        method = extra.get("extraction_method")
        # Must NOT be mis-claimed by the Receipt/Pur reader or generic fallback.
        self.assertEqual(method, "stock_sale_opstk_order_vision")
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 20)
        # Company banner is not a product row.
        self.assertFalse(
            any(
                str(i.get("product_name") or "").strip().upper()
                == "HIMALAYA DRUG COM"
                for i in items
            )
        )
        # Printed totals row read correctly.
        self.assertEqual(result["totals"].get("sales_value"), 71414.0)
        self.assertEqual(result["totals"].get("closing_value"), 149761.0)


if __name__ == "__main__":
    unittest.main()
