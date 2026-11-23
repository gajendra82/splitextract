"""ITEM/PACK/OPENING/P.RETURN/SALE/S.RETURN/CLOSING/ITEMCODE qty-only STOCK & SALES.

Digits encoded as Greek τ..ύ must decode to printed ASCII. ITEMCODE is not value.
Excel family: mislabeled ITEMCODE holds unit_rate; unlabeled cols hold
reorder_value / opening_value; last GRAND TOTAL (else TOTAL VALUE) wins.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from services.sales_statement_extractor import (
    _xls_fill_item_pack_preturn_sale_rows,
    _xls_header_colmap,
    _xls_preturn_sale_discover_value_cols,
    _xls_item_pack_preturn_sale_indexes,
    empty_result,
    extract_sales_statement,
)
from services.stock_header_resolver import decode_pdf_pseudo_digits
from services.stock_native_resolver import parse_pdf_free_column_statement


_PDF = Path(r"C:\Users\Prerana Bhalerao\Downloads\STOCKANDSALES.pdf")
_XLS = Path(r"C:\Users\Prerana Bhalerao\Downloads\STOCKANDSALES (4).xls")


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


class TestItemPackPreturnSaleExcelFamily(unittest.TestCase):
    """Layout-family Excel: ITEMCODE→rate + unlabeled opening_value + last GRAND TOTAL."""

    def _rows(self):
        return [
            ["MEDILINK"],
            ["STOCK & SALES"],
            ["Company : HIMALAYA DRUG COMPNAY"],
            ["From: 01-Oct-26  To: 31-Oct-26"],
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
                None,
                None,
            ],
            ["Division : ZEAL"],
            # open 10 @ 12.5 → opening_value 125; sale 4; closing 6; reorder_value blank
            ["ALPHA TAB", "10S", 10.0, "-", "-", 4.0, "-", "-", 6.0, "-", 12.5, "-", 125.0],
            # printed opening differs from qty×rate (source quirk)
            [
                "BETA SYP 200ML",
                "200ML",
                244.0,
                "-",
                "-",
                244.0,
                "-",
                "-",
                "-",
                244.0,
                140.17,
                34201.24,
                43500.32,
            ],
            ["Total Value (ZEAL)", "", 1000.0, 0.0, 0.0, 100.0, 0.0, 0.0, 900.0],
            ["TOTAL VALUE", "", 2000.0, 0.0, 0.0, 200.0, 0.0, 0.0, 1800.0, 50.0],
            ["GRAND TOTAL", "", 303936.0, 0.0, 0.0, 54804.11, 0.0, 0.0, 241331.04, 38274.32],
        ]

    def test_itemcode_discovered_as_rate_and_opening_value(self):
        rows = self._rows()
        indexes = _xls_item_pack_preturn_sale_indexes(rows[4])
        self.assertIsNotNone(indexes)
        discovered = _xls_preturn_sale_discover_value_cols(rows, 4, indexes)
        self.assertEqual(discovered.get("rate"), 10)
        self.assertNotIn("itemcode", discovered)
        self.assertEqual(discovered.get("opening_value"), 12)
        self.assertEqual(discovered.get("reorder_value"), 11)

    def test_prefers_last_grand_total_keeps_printed_opening(self):
        rows = self._rows()
        result = empty_result("synthetic_preturn.xls", "xls")
        out = _xls_fill_item_pack_preturn_sale_rows(result, rows, 4, "Sheet1", 12)
        extra = (out.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "item_pack_preturn_sale")
        self.assertEqual(extra.get("total_row_label"), "GRAND TOTAL")
        self.assertEqual(extra.get("total_row_source"), "preturn_sale_grand_total")
        self.assertEqual(out["totals"].get("sales_value"), 54804.11)
        self.assertEqual(out["totals"].get("closing_value"), 241331.04)
        self.assertEqual(extra.get("opening_value"), 303936.0)
        self.assertTrue(extra.get("no_sales_value"))
        self.assertTrue(extra.get("no_closing_value"))
        self.assertEqual(extra.get("sales_closing_value_scope"), "footer_only")
        by_name = {i["product_name"]: i for i in out["line_items"]}
        alpha = by_name["ALPHA TAB"]
        self.assertEqual(alpha["opening_qty"], 10.0)
        self.assertEqual(alpha["sales_qty"], 4.0)
        self.assertEqual(alpha["closing_qty"], 6.0)
        self.assertEqual(alpha["opening_value"], 125.0)
        self.assertIsNone(alpha["sales_value"])
        self.assertIsNone(alpha["closing_value"])
        self.assertEqual((alpha.get("extra") or {}).get("unit_rate"), 12.5)
        beta = by_name["BETA SYP 200ML"]
        self.assertEqual(beta["opening_value"], 43500.32)
        self.assertEqual((beta.get("extra") or {}).get("printed_opening_value"), 43500.32)
        cands = (beta.get("extra") or {}).get("candidates") or []
        self.assertTrue(
            any(
                c.get("field") == "opening_value" and c.get("value") == 34201.48
                for c in cands
            )
        )
        self.assertIsNone(beta.get("product_code"))

    @unittest.skipUnless(_XLS.is_file(), "source XLS not on this machine")
    def test_live_stockandsales_xls_footer_and_liv52(self):
        result = extract_sales_statement(_XLS.read_bytes(), _XLS.name)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "item_pack_preturn_sale")
        self.assertEqual(extra.get("total_row_label"), "TOTAL VALUE")
        self.assertEqual(result["totals"].get("sales_value"), 54804.11)
        self.assertEqual(result["totals"].get("closing_value"), 241331.04)
        self.assertEqual(extra.get("opening_value"), 303936.0)
        self.assertEqual(extra.get("line_opening_value_sum"), 303936.0)
        liv = next(
            i for i in result["line_items"] if "LIV.52 SYP 200ML" in i["product_name"]
        )
        self.assertEqual(liv.get("opening_value"), 43500.32)
        self.assertIsNone(liv.get("sales_value"))
        self.assertIsNone(liv.get("closing_value"))
        self.assertEqual((liv.get("extra") or {}).get("unit_rate"), 140.17)


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
