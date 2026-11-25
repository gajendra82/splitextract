"""Empty pdfplumber word tables must fall back to fitz (OPENING|LMS grids)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services.stock_native_tables import pdf_built_table_empty


class PdfBuiltTableEmptyTests(unittest.TestCase):
    def test_empty_when_no_headers_or_rows(self):
        self.assertTrue(pdf_built_table_empty(None))
        self.assertTrue(pdf_built_table_empty({}))
        self.assertTrue(
            pdf_built_table_empty({"header_cells": [], "data_rows": []})
        )
        self.assertTrue(
            pdf_built_table_empty(
                {"header_cells": [{"text": "OPENING"}], "data_rows": []}
            )
        )

    def test_not_empty_when_headers_and_rows(self):
        self.assertFalse(
            pdf_built_table_empty(
                {
                    "header_cells": [{"text": "OPENING"}],
                    "data_rows": [[{"text": "Abana", "x": 10.0}]],
                }
            )
        )

    def test_page_results_rescue_non_empty(self):
        self.assertFalse(
            pdf_built_table_empty(
                {
                    "header_cells": [],
                    "data_rows": [],
                    "page_results": [
                        {
                            "header_cells": [{"text": "OPENING"}],
                            "data_rows": [[{"text": "1", "x": 1.0}]],
                        }
                    ],
                }
            )
        )


class PlumberEmptyFallsBackToFitzTests(unittest.TestCase):
    def test_parse_free_column_uses_fitz_when_plumber_empty(self):
        from services.stock_native_resolver import parse_pdf_free_column_statement

        fitz_built = {
            "header_cells": [
                {
                    "text": "PRODUCT",
                    "col_index": 0,
                    "x_center": 20.0,
                    "x0": 0.0,
                    "x1": 40.0,
                },
                {
                    "text": "OPENING",
                    "col_index": 1,
                    "x_center": 260.0,
                    "x0": 240.0,
                    "x1": 280.0,
                },
                {
                    "text": "PURCHASE",
                    "col_index": 2,
                    "x_center": 330.0,
                    "x0": 310.0,
                    "x1": 350.0,
                },
                {
                    "text": "LMS",
                    "col_index": 3,
                    "x_center": 400.0,
                    "x0": 390.0,
                    "x1": 410.0,
                },
                {
                    "text": "SALES",
                    "col_index": 4,
                    "x_center": 450.0,
                    "x0": 440.0,
                    "x1": 470.0,
                },
                {
                    "text": "CLOSING",
                    "col_index": 5,
                    "x_center": 510.0,
                    "x0": 490.0,
                    "x1": 530.0,
                },
            ],
            "data_rows": [
                [
                    {"text": "Bonnisan", "x": 10.0},
                    {"text": "Drops", "x": 50.0},
                    {"text": "30ml", "x": 180.0},
                    {"text": "40", "x": 275.0},
                    {"text": "-", "x": 350.0},
                    {"text": "60", "x": 400.0},
                    {"text": "40", "x": 455.0},
                    {"text": "-", "x": 520.0},
                ],
                [
                    {"text": "Bonnisan", "x": 10.0},
                    {"text": "Liq", "x": 50.0},
                    {"text": "100ml", "x": 180.0},
                    {"text": "108", "x": 275.0},
                    {"text": "-", "x": 350.0},
                    {"text": "60", "x": 400.0},
                    {"text": "96", "x": 455.0},
                    {"text": "12", "x": 520.0},
                ],
                [
                    {"text": "Bonnisan", "x": 10.0},
                    {"text": "Sy", "x": 50.0},
                    {"text": "200ml", "x": 180.0},
                    {"text": "248", "x": 275.0},
                    {"text": "252", "x": 345.0},
                    {"text": "60", "x": 400.0},
                    {"text": "282", "x": 455.0},
                    {"text": "218", "x": 520.0},
                ],
            ],
            "page_results": [],
            "pages_total": 1,
            "pages_resolved": 1,
        }
        empty_plumber = {
            "header_cells": [],
            "data_rows": [],
            "page_results": [],
            "pages_total": 1,
            "pages_resolved": 0,
        }
        mock_pdf = MagicMock()
        mock_pdf.pages = [MagicMock()]
        mock_pdf.pages[0].extract_text.return_value = (
            "PRODUCT OPENING PURCHASE LMS SALES CLOSING\n"
        )

        with patch("pdfplumber.open", return_value=mock_pdf), patch(
            "services.stock_native_tables.from_pdf_words",
            return_value=empty_plumber,
        ), patch(
            "services.stock_native_tables.from_fitz_pdf_bytes",
            return_value=fitz_built,
        ) as fitz_mock:
            result = parse_pdf_free_column_statement(b"%PDF-1.4", "StockHP.pdf")

        self.assertTrue(fitz_mock.called)
        self.assertIsNotNone(result)
        assert result is not None
        by = {i["product_name"]: i for i in result["line_items"]}
        self.assertEqual(by["Bonnisan Drops"]["opening_qty"], 40.0)
        self.assertEqual(by["Bonnisan Drops"]["sales_qty"], 40.0)
        self.assertEqual(by["Bonnisan Drops"]["extra"]["lms_qty"], 60.0)
        self.assertEqual(by["Bonnisan Sy"]["opening_qty"], 248.0)
        self.assertEqual(by["Bonnisan Sy"]["receipts_qty"], 252.0)
        self.assertEqual(by["Bonnisan Sy"]["sales_qty"], 282.0)
        self.assertEqual(by["Bonnisan Sy"]["closing_qty"], 218.0)


if __name__ == "__main__":
    unittest.main()
