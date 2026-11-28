"""Barcode|Opening|Sale|Sale Value must not be remapped as SSA TOTAL STOCK."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _headers_look_like_barcode_op_sale_value,
    _repair_ssa_analysis_result_from_physical_cells,
    _result_looks_like_ssa_analysis,
)

_BARCODE_HEADERS = [
    "Barcode",
    "Name",
    "Pack",
    "Opening",
    "Purchase",
    "Sale",
    "Closing",
    "Pur Value",
    "Sale Value",
    "Closing Value",
]


class TestBarcodeSaleValueNotSsaRemap(unittest.TestCase):
    def test_headers_are_not_ssa_analysis(self):
        self.assertTrue(_headers_look_like_barcode_op_sale_value(_BARCODE_HEADERS))
        item = {
            "product_name": "CYSTONE SYP",
            "opening_qty": 94.0,
            "receipts_qty": 0.0,
            "sales_qty": 3.0,
            "sales_value": 594.67,
            "closing_qty": 91.0,
            "closing_value": 15772.55,
            "extra": {
                "vision_column_headers": list(_BARCODE_HEADERS),
                "physical_cells": {
                    "0": "03065",
                    "1": "CYSTONE SYP",
                    "2": "1X200",
                    "3": "94",
                    "4": "0",
                    "5": "3",
                    "6": "91",
                    "7": "0.00",
                    "8": "594.67",
                    "9": "15772.55",
                },
            },
        }
        result = {"report_title": "Stock And Sales", "line_items": [item]}
        self.assertFalse(_result_looks_like_ssa_analysis(result))
        _repair_ssa_analysis_result_from_physical_cells(result)
        self.assertEqual(item["sales_qty"], 3.0)
        self.assertEqual(item["sales_value"], 594.67)
        self.assertEqual(item["opening_qty"], 94.0)
        self.assertEqual(item["closing_qty"], 91.0)

    def test_ssa_total_stock_headers_still_match(self):
        self.assertFalse(
            _headers_look_like_barcode_op_sale_value(
                [
                    "ITEM DESCRIPTION",
                    "OPENING STOCK",
                    "PURCHASES",
                    "SALE RETURN",
                    "TOTAL STOCK",
                    "SALES",
                    "CLOSING STOCK",
                ]
            )
        )


if __name__ == "__main__":
    unittest.main()
