"""Excel mobile screenshot: Opening / Receipt Qty / Total / Sale."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _apply_excel_mobile_receipt_sale_validation,
    _excel_mobile_receipt_sale_headers_ok,
    _excel_mobile_receipt_sale_items,
    _excel_mobile_receipt_sale_rows_ok,
    _ensure_stock_qty_value_fields,
    _is_excel_mobile_receipt_sale_screenshot,
    empty_result,
)


SCREENSHOT = Path(
    r"C:\Users\anuja\.cursor\projects"
    r"\c-Users-anuja-OneDrive-Desktop-split-extract-new-splitextract"
    r"\assets"
    r"\c__Users_anuja_AppData_Roaming_Cursor_User_workspaceStorage_"
    r"4e671aa00c4c9af2279e0419ad279d5c_images_"
    r"0000700155_2026_08_ZA_06_333_04092026080817-"
    r"b35d0dcf-076f-4365-b9e6-766ceb705b8a.png"
)
OTHER = Path("tests/fixtures/0000700077_2026_08_ZL_06_304_06092026141652.jpeg")


class TestExcelMobileReceiptSale(unittest.TestCase):
    def test_headers_accept_this_layout_only(self):
        self.assertTrue(
            _excel_mobile_receipt_sale_headers_ok(
                {
                    "layout_ok": True,
                    "detected_headers": [
                        "Product",
                        "Packing",
                        "Expiry Date",
                        "Rate",
                        "Opening",
                        "Receipt Qty",
                        "Receipt Free",
                        "Free Replace",
                        "Total",
                        "Sale",
                    ],
                }
            )
        )
        self.assertFalse(
            _excel_mobile_receipt_sale_headers_ok(
                {
                    "layout_ok": True,
                    "detected_headers": [
                        "Material_name",
                        "Opening_bal_qty",
                        "Primary_qty",
                        "Closing_bal_qty",
                    ],
                }
            )
        )
        self.assertFalse(
            _excel_mobile_receipt_sale_headers_ok(
                {
                    "layout_ok": True,
                    "detected_headers": ["Product", "Opening", "Purchase", "Sale", "Closing"],
                }
            )
        )

    def test_sale_stays_sale_and_total_is_not_closing(self):
        parsed = {
            "line_items": [
                {
                    "product_name": "ARJUNA CAP. 60'",
                    "packing": "1X60 C",
                    "opening_qty": 53,
                    "receipts_qty": "----",
                    "sales_qty": 7,
                    "closing_qty": 53,
                    "closing_value": 220.63,
                    "extra": {
                        "expiry": "02/28",
                        "unit_rate": 220.63,
                        "receipt_free": "----",
                        "free_replace": "----",
                        "total_stock": 53,
                    },
                },
                {
                    "product_name": "LIV 52 DS 100ML SYP",
                    "packing": "1X100ML",
                    "opening_qty": 19,
                    "receipts_qty": 70,
                    "sales_qty": 13,
                    "closing_qty": 89,
                    "extra": {
                        "unit_rate": 151.96,
                        "receipt_free": 0,
                        "free_replace": 0,
                        "total_stock": 89,
                    },
                },
                {
                    "product_name": "Last 6 Months NON MOVING PRODUCTS",
                    "opening_qty": 0,
                    "sales_qty": 0,
                    "extra": {"total_stock": 0},
                },
            ]
        }
        items = _excel_mobile_receipt_sale_items(parsed)
        self.assertEqual(len(items), 2)
        arjuna = items[0]
        self.assertEqual(arjuna["opening_qty"], 53.0)
        self.assertEqual(arjuna["receipts_qty"], 0.0)
        self.assertEqual(arjuna["sales_qty"], 7.0)
        self.assertIsNone(arjuna["closing_qty"])
        self.assertIsNone(arjuna["closing_value"])
        self.assertEqual(arjuna["extra"]["unit_rate"], 220.63)
        self.assertEqual(arjuna["extra"]["total_stock"], 53.0)
        liv = items[1]
        self.assertEqual(liv["receipts_qty"], 70.0)
        self.assertEqual(liv["sales_qty"], 13.0)
        self.assertIsNone(liv["closing_qty"])
        self.assertEqual(liv["extra"]["total_stock"], 89.0)

    def test_rows_ok_requires_sale_column_and_total_identity(self):
        good = []
        for index in range(8):
            good.append(
                {
                    "product_name": f"ITEM {index}",
                    "opening_qty": 10.0,
                    "receipts_qty": 2.0,
                    "sales_qty": 1.0,
                    "closing_qty": None,
                    "extra": {
                        "receipt_free": 0.0,
                        "free_replace": 0.0,
                        "total_stock": 12.0,
                    },
                }
            )
        self.assertTrue(_excel_mobile_receipt_sale_rows_ok(good))
        shifted = [dict(item, sales_qty=0.0, closing_qty=12.0) for item in good]
        self.assertFalse(_excel_mobile_receipt_sale_rows_ok(shifted))

    def test_missing_closing_stays_null(self):
        result = empty_result("sheet.png", "png")
        result["line_items"] = [
            {
                "product_name": "ARJUNA CAP. 60'",
                "opening_qty": 53.0,
                "receipts_qty": 0.0,
                "sales_qty": 7.0,
                "sales_value": None,
                "closing_qty": None,
                "closing_value": None,
                "extra": {"total_stock": 53.0, "receipt_free": 0.0, "free_replace": 0.0},
            }
        ]
        result["totals"]["extra"]["extraction_method"] = (
            "excel_mobile_receipt_sale_screenshot"
        )
        result["totals"]["extra"]["qty_only"] = True
        result = _apply_excel_mobile_receipt_sale_validation(result)
        result = _ensure_stock_qty_value_fields(result)
        item = result["line_items"][0]
        self.assertIsNone(item["closing_qty"])
        self.assertIsNone(item["closing_value"])
        self.assertEqual(item["sales_qty"], 7.0)
        self.assertTrue(item["extra"]["stock_identity_ok"])
        self.assertEqual(
            result["totals"]["extra"]["stock_identity_kind"],
            "excel_opening_receipt_total_sale",
        )

    def test_merge_keeps_the_row_that_has_the_receipt(self):
        from services.sales_statement_extractor import _excel_mobile_merge_items

        weak = {
            "product_name": "LIV 52 DS TAB. 60'",
            "opening_qty": 20.0,
            "receipts_qty": 0.0,
            "sales_qty": 40.0,
            "extra": {"receipt_free": 0.0, "free_replace": 0.0, "total_stock": 20.0},
        }
        strong = {
            "product_name": "LIV 52 DS TAB. 60'",
            "opening_qty": 20.0,
            "receipts_qty": 100.0,
            "sales_qty": 40.0,
            "extra": {"receipt_free": 0.0, "free_replace": 0.0, "total_stock": 120.0},
        }
        merged = _excel_mobile_merge_items([[weak], [strong]])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["receipts_qty"], 100.0)

    def test_pixel_gate_matches_this_screenshot_only(self):
        if not SCREENSHOT.exists():
            self.skipTest("screenshot fixture not on disk")
        self.assertTrue(
            _is_excel_mobile_receipt_sale_screenshot(SCREENSHOT.read_bytes())
        )
        if OTHER.exists():
            self.assertFalse(
                _is_excel_mobile_receipt_sale_screenshot(OTHER.read_bytes())
            )


if __name__ == "__main__":
    unittest.main()
