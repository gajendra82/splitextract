"""STOCK VALUATION AS ON photo: Stock/Value map to closing, never sales.

Other stock-statement layouts must not match this header gate.
"""
import unittest

from services.sales_statement_extractor import (
    _is_stock_valuation_header_text,
    _looks_like_stock_valuation_misfiled,
    _remap_stock_valuation_sales_to_closing,
)


class TestStockValuationAsOnGate(unittest.TestCase):
    def test_matches_noisy_photo_header_without_value_column(self):
        # Photo OCR often truncates the rightmost Value column.
        self.assertTrue(
            _is_stock_valuation_header_text(
                "GORBI MEDICAL STORE\n"
                "STOCK VALUATION AS ON 31/7/2026\n"
                "S.No. Description . Stock Rate.\n"
                "HIMALYA ZEAL"
            )
        )

    def test_rejects_ssa_opening_receipt_issue(self):
        self.assertFalse(
            _is_stock_valuation_header_text(
                "STOCK & SALES ANALYSIS ITEM DESCRIPTION "
                "OPENING RECEIPT ISSUE QTY VALUE"
            )
        )
        self.assertFalse(
            _is_stock_valuation_header_text(
                "STOCK VALUATION AS ON 01/01/2026 OPENING RECEIPT CLOSING"
            )
        )

    def test_rejects_plain_sales_statement(self):
        self.assertFalse(
            _is_stock_valuation_header_text(
                "SECONDARY SALES STATEMENT PRODUCT QTY VALUE"
            )
        )

    def test_remaps_misfiled_stock_off_sales_columns(self):
        result = {
            "report_title": "STOCK VALUATION",
            "line_items": [
                {
                    "product_name": "HIMALAYA AACTARIL SOAP",
                    "sales_qty": 8,
                    "sales_value": 652.51,
                    "closing_qty": 0,
                    "closing_value": 0,
                },
                {
                    "product_name": "HIMALAYA CANFIDO TAB",
                    "sales_qty": 23,
                    "sales_value": 3164.36,
                    "closing_qty": 0,
                    "closing_value": 0,
                },
                {
                    "product_name": "HIMALAYA HIMCOLIN GEL",
                    "sales_qty": 4,
                    "sales_value": 678.34,
                    "closing_qty": 0,
                    "closing_value": 0,
                },
            ],
            "totals": {"extra": {}},
        }
        self.assertTrue(_looks_like_stock_valuation_misfiled(result))
        fixed = _remap_stock_valuation_sales_to_closing(result)
        soap = fixed["line_items"][0]
        self.assertEqual(soap["closing_qty"], 8.0)
        self.assertEqual(soap["closing_value"], 652.51)
        self.assertEqual(soap["sales_qty"], 0.0)
        self.assertEqual(soap["sales_value"], 0.0)
        self.assertEqual(
            fixed["totals"]["extra"]["extraction_method"], "stock_valuation_as_on"
        )


if __name__ == "__main__":
    unittest.main()
