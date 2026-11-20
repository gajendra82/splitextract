"""STOCK_HEADER_OCR_ENRICH: stockist vs Company: from page OCR after vision."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


SAMPLE_OCR = """
10:42 Sat, 10 Oct
SRI_VENKATESHWARA MEDICAL DISTRIBUTORS
Stock & Sales Statement
From 01/09/2026 To 30/09/2026 Page : 1
Company :HIMALAYA WELLNESS CO
ARJUNA TAB 60's
"""


class StockHeaderOcrEnrichTests(unittest.TestCase):
    def test_flag_off_is_noop(self):
        from services.sales_statement_extractor import _enrich_stock_statement_header_from_ocr

        result = {
            "stockist_name": "HIMALAYA WELLNESS CO",
            "company_name": None,
        }
        with patch.dict(os.environ, {"STOCK_HEADER_OCR_ENRICH": "false"}, clear=False):
            _enrich_stock_statement_header_from_ocr(result, SAMPLE_OCR)
        self.assertEqual(result["stockist_name"], "HIMALAYA WELLNESS CO")
        self.assertIsNone(result["company_name"])

    def test_replaces_company_confused_stockist(self):
        from services.sales_statement_extractor import _enrich_stock_statement_header_from_ocr

        result = {
            "stockist_name": "HIMALAYA WELLNESS CO",
            "company_name": None,
        }
        with patch.dict(os.environ, {"STOCK_HEADER_OCR_ENRICH": "true"}, clear=False):
            _enrich_stock_statement_header_from_ocr(result, SAMPLE_OCR)
        self.assertEqual(
            result["stockist_name"], "SRI VENKATESHWARA MEDICAL DISTRIBUTORS"
        )
        self.assertEqual(result["company_name"], "HIMALAYA WELLNESS CO")
        self.assertEqual(result["report_title"], "Stock & Sales Statement")

    def test_title_band_party_overrides_wrong_vision_stockist(self):
        from services.sales_statement_extractor import _enrich_stock_statement_header_from_ocr

        result = {
            "stockist_name": "SOME OTHER AGENCY",
            "company_name": "WRONG CO",
        }
        with patch.dict(os.environ, {"STOCK_HEADER_OCR_ENRICH": "true"}, clear=False):
            _enrich_stock_statement_header_from_ocr(result, SAMPLE_OCR)
        self.assertEqual(
            result["stockist_name"], "SRI VENKATESHWARA MEDICAL DISTRIBUTORS"
        )
        self.assertEqual(result["company_name"], "HIMALAYA WELLNESS CO")

    def test_fills_missing_stockist_and_company(self):
        from services.sales_statement_extractor import _enrich_stock_statement_header_from_ocr

        result = {"stockist_name": None, "company_name": None}
        with patch.dict(os.environ, {"STOCK_HEADER_OCR_ENRICH": "true"}, clear=False):
            _enrich_stock_statement_header_from_ocr(result, SAMPLE_OCR)
        self.assertEqual(
            result["stockist_name"], "SRI VENKATESHWARA MEDICAL DISTRIBUTORS"
        )
        self.assertEqual(result["company_name"], "HIMALAYA WELLNESS CO")


class VisionCompanyNamePassthroughTests(unittest.TestCase):
    def test_map_vision_table_keeps_company_name(self):
        from services.stock_vision_table import map_vision_table

        table = {
            "stockist_name": "SRI VENKATESHWARA MEDICAL DISTRIBUTORS",
            "company_name": "HIMALAYA WELLNESS CO",
            "statement_period": "01/09/2026 to 30/09/2026",
            "tables": [
                {
                    "table_index": 0,
                    "x_range": [0.0, 1.0],
                    "column_count": 3,
                    "header_rows": [
                        [
                            {"text": "Product", "col_index": 0, "x_center": 0.1},
                            {"text": "Sale", "col_index": 1, "x_center": 0.5},
                            {"text": "Qoh", "col_index": 2, "x_center": 0.8},
                        ]
                    ],
                    "proposed_mapping": [
                        {"col_index": 0, "canonical": "product_name", "confidence": 1},
                        {"col_index": 1, "canonical": "sales_qty", "confidence": 1},
                        {"col_index": 2, "canonical": "closing_qty", "confidence": 1},
                    ],
                    "rows": [
                        {
                            "row_index": 0,
                            "y_center": 0.3,
                            "cells": ["SPEMAN TAB", "12", "25"],
                            "is_total_row": False,
                        }
                    ],
                    "unreadable_cells": [],
                }
            ],
        }
        mapped = map_vision_table(table)
        self.assertEqual(
            mapped.get("stockist_name"), "SRI VENKATESHWARA MEDICAL DISTRIBUTORS"
        )
        self.assertEqual(mapped.get("company_name"), "HIMALAYA WELLNESS CO")


if __name__ == "__main__":
    unittest.main()
