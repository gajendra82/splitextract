"""ITEM/PACK/OPENING/P.RETURN/SALE/S.RETURN/CLOSING/ITEMCODE qty-only STOCK & SALES.

Digits encoded as Greek τ..ύ must decode to printed ASCII. ITEMCODE is not value.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from services.sales_statement_extractor import (
    _xls_header_colmap,
    extract_sales_statement,
)
from services.stock_header_resolver import decode_pdf_pseudo_digits
from services.stock_native_resolver import parse_pdf_free_column_statement


_PDF = Path(r"C:\Users\Prerana Bhalerao\Downloads\STOCKANDSALES.pdf")


class TestPreturnSreturnAliases(unittest.TestCase):
    def test_xls_maps_preturn_sreturn_itemcode(self):
        colmap = _xls_header_colmap(
            [
                "ITEM",
                "PACK",
                "OPENING",
                "PURCHASE",
                "P.RETURN",
                "SALE",
                "S.RETURN",
                "OTHERS",
                "CLOSING",
                "REORDER - QTY",
                "ITEMCODE",
            ]
        )
        self.assertEqual(colmap["item"], 0)
        self.assertEqual(colmap["op"], 2)
        self.assertEqual(colmap["pret"], 4)
        self.assertEqual(colmap["sale"], 5)
        self.assertEqual(colmap["sret"], 6)
        self.assertEqual(colmap["bal"], 8)
        self.assertEqual(colmap["product_code"], 10)
        self.assertNotIn("bval", colmap)
        self.assertNotIn("sval", colmap)


class TestStockSalesPdfPseudoDigits(unittest.TestCase):
    def test_decode_from_to_dates(self):
        self.assertEqual(
            decode_pdf_pseudo_digits("From: τυ-Sep-φϊ  To: χτ-Sep-φϊ"),
            "From: 01-Sep-26  To: 30-Sep-26",
        )

    @unittest.skipUnless(_PDF.is_file(), "source PDF not on this machine")
    def test_kashi_opening_closing_title_not_itemcode_value(self):
        payload = _PDF.read_bytes()
        with patch.dict(
            "os.environ",
            {
                "ENABLE_GEMINI_EXTRACTION_FALLBACK": "false",
                "ENABLE_VISION_FALLBACK": "false",
                "STOCK_VISION_TABLE": "false",
                "STOCK_PDF_WORDS_FREE_COLUMNS": "true",
            },
        ):
            parsed = parse_pdf_free_column_statement(payload, "STOCKANDSALES.pdf")
            if parsed and parsed.get("line_items"):
                result = parsed
            else:
                result = extract_sales_statement(payload, "STOCKANDSALES.pdf")
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(result.get("report_title"), "STOCK & SALES")
        by_name = {item["product_name"]: item for item in result["line_items"]}
        aact = next(item for key, item in by_name.items() if "AACTARIL" in key)
        self.assertEqual(aact["opening_qty"], 20.0)
        self.assertEqual(aact["closing_qty"], 20.0)
        self.assertIn(aact.get("closing_value"), (None, 0.0))
        self.assertNotEqual(aact.get("closing_value"), 57.27)
        self.assertNotEqual(aact.get("sales_value"), 57.27)
        bleminor = next(item for key, item in by_name.items() if "BLEMINOR" in key)
        self.assertEqual(bleminor["opening_qty"], 42.0)
        self.assertEqual(bleminor["sales_qty"], 2.0)
        self.assertEqual(bleminor["closing_qty"], 40.0)
        self.assertTrue(
            extra.get("qty_only")
            or extra.get("extraction_method") in {
                "pdf_words_free_columns",
                "semantic_text_table",
            }
        )
        self.assertTrue(
            all("ERP" not in str(name or "").upper() for name in by_name)
        )
