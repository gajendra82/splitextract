"""OPENING STOCK|VALUE and RECEIVE QUANTITY|VALUE stay on separate roles.

Printed VALUE must not be classified as the quantity column. Other sheets
that only print Opening / Receipt / Sales / Closing keep those qty roles.
"""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _header_driven_headers,
    _header_driven_items,
    _header_driven_role,
    _header_driven_roles,
)


class HeaderDrivenStockValueRoleTests(unittest.TestCase):
    def test_stock_quantity_and_value_are_different_roles(self):
        self.assertEqual(_header_driven_role("OPENING STOCK"), "opening_qty")
        self.assertEqual(_header_driven_role("OPENING VALUE"), "opening_value")
        self.assertEqual(_header_driven_role("RECEIVE QUANTITY"), "receipts_qty")
        self.assertEqual(_header_driven_role("RECEIVE VALUE"), "receipts_value")
        self.assertEqual(_header_driven_role("ISSUE QUANTITY"), "sales_qty")
        self.assertEqual(_header_driven_role("ISSUE VALUE"), "sales_value")
        self.assertEqual(_header_driven_role("CLOSING STOCK"), "closing_qty")
        self.assertEqual(_header_driven_role("CLOSING VALUE"), "closing_value")
        self.assertEqual(_header_driven_role("LAST MONTH SALE"), "lms")

    def test_plain_qty_headers_stay_quantities(self):
        self.assertEqual(_header_driven_role("Opening"), "opening_qty")
        self.assertEqual(_header_driven_role("Receipt"), "receipts_qty")
        self.assertEqual(_header_driven_role("Sales"), "sales_qty")
        self.assertEqual(_header_driven_role("Closing"), "closing_qty")
        self.assertEqual(_header_driven_role("Closing Amt"), "closing_value")

    def test_printed_value_header_overrides_model_qty_role(self):
        parsed = {
            "detected_headers": [
                {"name": "PRODUCT DESCRIPTION", "role": "product"},
                {"name": "OPENING STOCK", "role": "opening_qty"},
                {"name": "OPENING VALUE", "role": "opening_qty"},
                {"name": "RECEIVE QUANTITY", "role": "receipts_qty"},
                {"name": "RECEIVE VALUE", "role": "receipts_qty"},
                {"name": "ISSUE QUANTITY", "role": "sales_qty"},
                {"name": "ISSUE VALUE", "role": "sales_value"},
                {"name": "CLOSING STOCK", "role": "closing_qty"},
                {"name": "CLOSING VALUE", "role": "closing_value"},
                {"name": "LAST MONTH SALE", "role": "sales_qty"},
            ]
        }
        headers = _header_driven_headers(parsed)
        roles = {header["name"]: header["role"] for header in headers}
        self.assertEqual(roles["OPENING VALUE"], "opening_value")
        self.assertEqual(roles["RECEIVE VALUE"], "receipts_value")
        self.assertEqual(roles["OPENING STOCK"], "opening_qty")
        self.assertEqual(roles["RECEIVE QUANTITY"], "receipts_qty")
        self.assertEqual(roles["LAST MONTH SALE"], "lms")
        self.assertIn("opening_value", _header_driven_roles(headers))
        self.assertIn("lms", _header_driven_roles(headers))

    def test_items_keep_stock_count_and_rupee_amount(self):
        headers = _header_driven_headers(
            {
                "detected_headers": [
                    "OPENING STOCK",
                    "OPENING VALUE",
                    "RECEIVE QUANTITY",
                    "RECEIVE VALUE",
                    "ISSUE QUANTITY",
                    "ISSUE VALUE",
                    "CLOSING STOCK",
                    "CLOSING VALUE",
                    "LAST MONTH SALE",
                ]
            }
        )
        roles = _header_driven_roles(headers)
        items = _header_driven_items(
            {
                "line_items": [
                    {
                        "product_name": "BONNISAN DROPS 30ML",
                        "opening_qty": 66,
                        "opening_value": 4150,
                        "receipts_qty": 0,
                        "receipts_value": 0,
                        "sales_qty": 5,
                        "sales_value": 314,
                        "closing_qty": 61,
                        "closing_value": 3836,
                        "extra": {"lms": 0},
                    }
                ]
            },
            roles,
        )
        self.assertEqual(len(items), 1)
        row = items[0]
        self.assertEqual(row["opening_qty"], 66.0)
        self.assertEqual(row["opening_value"], 4150.0)
        self.assertEqual(row["receipts_qty"], 0.0)
        self.assertEqual(row["receipts_value"], 0.0)
        self.assertEqual(row["sales_qty"], 5.0)
        self.assertEqual(row["sales_value"], 314.0)
        self.assertEqual(row["closing_qty"], 61.0)
        self.assertEqual(row["closing_value"], 3836.0)
        self.assertEqual(row["extra"]["lms"], 0.0)

    def test_qty_only_sheet_does_not_invent_opening_value(self):
        roles = _header_driven_roles(
            _header_driven_headers(
                {"detected_headers": ["Opening", "Receipt", "Sales", "Closing"]}
            )
        )
        items = _header_driven_items(
            {
                "line_items": [
                    {
                        "product_name": "ABANA",
                        "opening_qty": 10,
                        "opening_value": 9999,
                        "receipts_qty": 1,
                        "sales_qty": 2,
                        "closing_qty": 9,
                    }
                ]
            },
            roles,
        )
        self.assertEqual(items[0]["opening_qty"], 10.0)
        self.assertIsNone(items[0]["opening_value"])
        self.assertIsNone(items[0]["receipts_value"])
        self.assertNotIn("lms", items[0]["extra"])


if __name__ == "__main__":
    unittest.main()
