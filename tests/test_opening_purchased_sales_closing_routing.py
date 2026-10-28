"""Opening stock / Purchased Qty / Sales Qty / Closing Stock image routing.

Tall OCR-failed screenshots must not be forced into OPBAL/REC/EXCESS Vision,
and Vision-table must keep printed Op/Pur/Sales/Cl rows when identity fails.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.stock_header_resolver import resolve_columns
from services.stock_vision_table import (
    _repair_closing_money_parked_in_qty,
    _vision_headers_qty_only_oric,
    _vision_result_is_qty_only_op_pur_sales_cl,
    _vision_table_quality_fallback_reason,
)


def _cells(*texts):
    return [
        {"text": t, "x_center": None, "col_index": i, "subheader_text": None}
        for i, t in enumerate(texts)
    ]


class OpeningStocAliasTests(unittest.TestCase):
    def test_truncated_opening_stoc_resolves(self):
        result = resolve_columns(
            _cells("Sl no", "Product", "Opening stoc", "Purchased Qty", "Sales Qty", "Closing Stock")
        )
        canons = [c["canonical"] for c in result["columns"]]
        self.assertEqual(
            canons,
            [
                "ignore",
                "product_name",
                "opening_qty",
                "purchase_qty",
                "sales_qty",
                "closing_qty",
            ],
        )


class QtyOnlyOpPurSalesClGuardTests(unittest.TestCase):
    def test_result_detector_from_column_map(self):
        result = {
            "totals": {
                "extra": {
                    "column_map": [
                        {"header_text": "Opening stock", "canonical": "opening_qty"},
                        {"header_text": "Purchased Qty", "canonical": "purchase_qty"},
                        {"header_text": "Sales Qty", "canonical": "sales_qty"},
                        {"header_text": "Closing Stock", "canonical": "closing_qty"},
                    ]
                }
            },
            "line_items": [],
        }
        self.assertTrue(_vision_result_is_qty_only_op_pur_sales_cl(result))

    def test_headers_qty_only_includes_purchased(self):
        item = {
            "extra": {
                "vision_column_headers": [
                    "Opening stock",
                    "Purchased Qty",
                    "Sales Qty",
                    "Closing Stock",
                ]
            }
        }
        self.assertTrue(_vision_headers_qty_only_oric(item))

    def test_large_closing_qty_not_parked_as_money_on_qty_only(self):
        item = {
            "opening_qty": 1481.0,
            "receipts_qty": 200.0,
            "sales_qty": 776.0,
            "closing_qty": 975.0,
            "closing_value": None,
            "extra": {
                "field_source": {"closing_value": "missing", "closing_qty": "printed"},
                "vision_column_headers": [
                    "Opening stock",
                    "Purchased Qty",
                    "Sales Qty",
                    "Closing Stock",
                ],
            },
        }
        self.assertFalse(_repair_closing_money_parked_in_qty(item))
        self.assertEqual(item["closing_qty"], 975.0)


class VisionKeepPrintedOpPurSalesClTests(unittest.TestCase):
    def test_valid_ratio_low_skipped_for_op_pur_sales_cl(self):
        result = {
            "line_items": [
                {
                    "product_name": f"ITEM {i}",
                    "opening_qty": 10.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 3.0,
                    "closing_qty": 11.0,
                    "extra": {
                        "vision_column_headers": [
                            "Opening stock",
                            "Purchased Qty",
                            "Sales Qty",
                            "Closing Stock",
                        ]
                    },
                }
                for i in range(20)
            ],
            "totals": {
                "extra": {
                    "valid_ratio": 0.30,
                    "row_status_counts": {
                        "VALID": 6,
                        "MISSING_VALUE": 8,
                        "COLUMN_ASSIGNMENT_SUSPECTED": 6,
                        "STRUCTURAL_ERROR": 0,
                    },
                    "column_map": [
                        {"header_text": "Opening stock", "canonical": "opening_qty"},
                        {"header_text": "Purchased Qty", "canonical": "purchase_qty"},
                        {"header_text": "Sales Qty", "canonical": "sales_qty"},
                        {"header_text": "Closing Stock", "canonical": "closing_qty"},
                    ],
                }
            },
        }
        with patch.dict(
            "os.environ",
            {
                "STOCK_VISION_TABLE_QUALITY_FALLBACK": "true",
                "STOCK_VISION_KEEP_PRINTED_OP_PUR_SALES_CL": "true",
                "STOCK_VISION_TABLE_MIN_VALID_RATIO": "0.50",
            },
        ):
            self.assertIsNone(_vision_table_quality_fallback_reason(result))

    def test_valid_ratio_low_still_fires_without_keep_flag(self):
        result = {
            "line_items": [
                {
                    "product_name": f"ITEM {i}",
                    "opening_qty": 1.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 0.0,
                    "closing_qty": 1.0,
                }
                for i in range(20)
            ],
            "totals": {
                "extra": {
                    "valid_ratio": 0.30,
                    "row_status_counts": {
                        "VALID": 6,
                        "MISSING_VALUE": 8,
                        "COLUMN_ASSIGNMENT_SUSPECTED": 6,
                        "STRUCTURAL_ERROR": 0,
                    },
                    "column_map": [
                        {"header_text": "Opening stock", "canonical": "opening_qty"},
                        {"header_text": "Purchased Qty", "canonical": "purchase_qty"},
                        {"header_text": "Sales Qty", "canonical": "sales_qty"},
                        {"header_text": "Closing Stock", "canonical": "closing_qty"},
                    ],
                }
            },
        }
        with patch.dict(
            "os.environ",
            {
                "STOCK_VISION_TABLE_QUALITY_FALLBACK": "true",
                "STOCK_VISION_KEEP_PRINTED_OP_PUR_SALES_CL": "false",
                "STOCK_VISION_TABLE_MIN_VALID_RATIO": "0.50",
            },
        ):
            self.assertEqual(
                _vision_table_quality_fallback_reason(result), "valid_ratio_low"
            )


class TallOpbalHeuristicFlagTests(unittest.TestCase):
    def test_flag_defaults_off(self):
        import os

        self.assertEqual(
            os.getenv("STOCK_OPBAL_TALL_UNREADABLE_HEURISTIC", "false")
            .strip()
            .lower(),
            "false",
        )


class ManufacturerStockistSwapTests(unittest.TestCase):
    def test_swaps_distributor_company_and_manufacturer_stockist(self):
        from services.sales_statement_extractor import _clear_company_confused_stockist

        result = {
            "company_name": "EASTERN AGENCIES HEALTHCARE PVT. LTD",
            "stockist_name": "HIMALAYA WELLNESS COMPANY (ZEAL)",
            "line_items": [],
        }
        with patch.dict(
            "os.environ",
            {
                "STOCK_REJECT_COMPANY_AS_STOCKIST": "true",
                "STOCK_SWAP_MANUFACTURER_STOCKIST": "true",
            },
        ):
            self.assertTrue(_clear_company_confused_stockist(result))
        self.assertEqual(
            result["company_name"], "HIMALAYA WELLNESS COMPANY (ZEAL)"
        )
        self.assertEqual(
            result["stockist_name"], "EASTERN AGENCIES HEALTHCARE PVT. LTD"
        )


if __name__ == "__main__":
    unittest.main()
