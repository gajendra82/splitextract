"""THE PHARMA HUB phone photo of Code/Item Opening/Purchase/Sales Stock Statement.

Must not regress the Ganesh Agency PDF code_item parser.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from services.sales_statement_extractor import (
    _code_item_enrich_meta_from_ocr,
    _is_code_item_stock_statement_photo_text,
    _is_code_item_stock_statement_text,
    _looks_like_code_item_photo_misread,
    _merge_code_item_photo_row,
    _repair_code_item_photo_qty_columns,
    empty_result,
    extract_sales_statement,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "test_pdfs"
    / "pharma_hub_code_item_stock_statement.jpg"
)

PHOTO_OCR = """
THE PHARMA HUB
Stock Statment : HIMALAYA DRUGS (ZEAL)
Page 1 Of 1
Aug. 2026
01/09/26
TOTALS
"""

PDF_HEADER = (
    "GANESH AGENCY\n"
    "Stock Statment : HIMALAYA ZEAL\n"
    "31/08/26\n"
    "Code  Item Description  Packing  Opening Purchase  Sales  Closing  "
    "Stock-Value  Sales-Value\n"
)


class TestPharmaHubCodeItemPhoto(unittest.TestCase):
    def test_photo_detector_accepts_title_without_full_headers(self):
        self.assertTrue(_is_code_item_stock_statement_photo_text(PHOTO_OCR))
        self.assertTrue(_is_code_item_stock_statement_text(PDF_HEADER))
        self.assertTrue(_is_code_item_stock_statement_photo_text(PDF_HEADER))
        self.assertFalse(
            _is_code_item_stock_statement_photo_text(
                "STOCK & SALES ANALYSIS\nOpening Receipt Issue Closing"
            )
        )

    def test_enrich_meta_from_ocr(self):
        result = empty_result("x.jpg", "jpg")
        result["stockist_name"] = "HIMALAYA DRUGS (ZEAL)"
        result["company_name"] = "THE PHARMA HUB"
        _code_item_enrich_meta_from_ocr(result, PHOTO_OCR)
        self.assertEqual(result.get("stockist_name"), "THE PHARMA HUB")
        self.assertIn("HIMALAYA", str(result.get("company_name") or "").upper())
        self.assertEqual(result.get("period_from"), "2026-08-01")
        self.assertEqual(result.get("period_to"), "2026-08-31")

    def test_misread_detector(self):
        bad = empty_result("x.jpg", "jpg")
        bad["stockist_name"] = "HIMALAYA DRUGS (ZEAL)"
        bad["company_name"] = "THE PHARMA HUB"
        bad["line_items"] = [
            {
                "product_code": str(1400 + i),
                "product_name": f"ITEM {i}",
                "packing": "60TAB",
                "opening_qty": 10.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 10.0,
                "sales_value": 0.0,
                "closing_value": 100.0 + i,
            }
            for i in range(10)
        ]
        self.assertTrue(_looks_like_code_item_photo_misread(bad))
        good = empty_result("x.jpg", "jpg")
        good["totals"]["extra"]["extraction_method"] = (
            "code_item_stock_statement_photo"
        )
        good["line_items"] = bad["line_items"]
        self.assertFalse(_looks_like_code_item_photo_misread(good))

    def test_merge_preserves_first_seen_order(self):
        merged = []
        seen = {}
        _merge_code_item_photo_row(
            merged,
            seen,
            {
                "product_code": "1848",
                "product_name": "HIMCOCID",
                "opening_qty": 0,
                "receipts_qty": 0,
                "sales_qty": 0,
                "closing_qty": 0,
                "sales_value": 0,
                "closing_value": 0,
            },
        )
        _merge_code_item_photo_row(
            merged,
            seen,
            {
                "product_code": "17720",
                "product_name": "AACTARIL SOAP",
                "opening_qty": 0,
                "receipts_qty": 0,
                "sales_qty": 0,
                "closing_qty": 0,
                "sales_value": 0,
                "closing_value": 0,
            },
        )
        _merge_code_item_photo_row(
            merged,
            seen,
            {
                "product_code": "1397",
                "product_name": "ABANA TAB",
                "opening_qty": 55,
                "receipts_qty": 0,
                "sales_qty": 12,
                "closing_qty": 43,
                "sales_value": 2011.44,
                "closing_value": 6709.29,
            },
        )
        # Better AACTARIL read must upgrade in place, not move to end.
        _merge_code_item_photo_row(
            merged,
            seen,
            {
                "product_code": "17720",
                "product_name": "AACTARIL SOAP",
                "opening_qty": 2,
                "receipts_qty": 0,
                "sales_qty": 2,
                "closing_qty": 0,
                "sales_value": 175.24,
                "closing_value": 0,
            },
        )
        self.assertEqual(
            [i["product_code"] for i in merged], ["1848", "17720", "1397"]
        )
        self.assertEqual(merged[1]["opening_qty"], 2)
        self.assertEqual(merged[1]["sales_qty"], 2)
        self.assertEqual(merged[1]["sales_value"], 175.24)

    def test_repair_opening_purchase_and_missing_sales(self):
        result = empty_result("x.jpg", "jpg")
        result["line_items"] = [
            {
                "product_code": "1397",
                "product_name": "ABANA TAB",
                "opening_qty": 0.0,
                "receipts_qty": 55.0,
                "sales_qty": 12.0,
                "closing_qty": 43.0,
            },
            {
                "product_code": "1855",
                "product_name": "LIV 52 TAB",
                "opening_qty": 132.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 14.0,
            },
            {
                "product_code": "1925",
                "product_name": "HIMCOLIN GEL",
                "opening_qty": 0.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 66.0,
            },
            {
                "product_code": "1852",
                "product_name": "LIV 52 HB CAP",
                "opening_qty": 0.0,
                "receipts_qty": 72.0,
                "sales_qty": 0.0,
                "closing_qty": 0.0,
                "closing_value": 12123.04,
            },
        ]
        _repair_code_item_photo_qty_columns(result)
        abana, liv, him, hb = result["line_items"]
        self.assertEqual(abana["opening_qty"], 55.0)
        self.assertEqual(abana["receipts_qty"], 0.0)
        self.assertEqual(liv["sales_qty"], 118.0)
        self.assertEqual(him["opening_qty"], 66.0)
        self.assertEqual(hb["opening_qty"], 72.0)
        self.assertEqual(hb["closing_qty"], 72.0)
        self.assertEqual(hb["receipts_qty"], 0.0)

    @unittest.skipUnless(FIXTURE.exists(), "Pharma Hub fixture not on this machine")
    def test_extract_uses_code_item_photo_vision(self):
        vision_payload = empty_result(FIXTURE.name, "jpg")
        vision_payload["stockist_name"] = "THE PHARMA HUB"
        vision_payload["company_name"] = "HIMALAYA DRUGS (ZEAL)"
        vision_payload["period_from"] = "2026-08-01"
        vision_payload["period_to"] = "2026-08-31"
        vision_payload["report_title"] = "Stock Statement"
        vision_payload["line_items"] = [
            {
                "product_code": "1397",
                "product_name": "ABANA TAB",
                "packing": "60's",
                "opening_qty": 55.0,
                "receipts_qty": 0.0,
                "sales_qty": 12.0,
                "sales_value": 1316.52,
                "closing_qty": 43.0,
                "closing_value": 6709.29,
                "extra": {},
            },
            {
                "product_code": "1836",
                "product_name": "CLARINA CREAM",
                "packing": "30GM",
                "opening_qty": 40.0,
                "receipts_qty": 0.0,
                "sales_qty": 9.0,
                "sales_value": 2221.68,
                "closing_qty": 31.0,
                "closing_value": 4221.08,
                "extra": {},
            },
            {
                "product_code": "1854",
                "product_name": "LIV 52 SYP(SMALL)",
                "packing": "100ML",
                "opening_qty": 52.0,
                "receipts_qty": 0.0,
                "sales_qty": 49.0,
                "sales_value": 5226.34,
                "closing_qty": 3.0,
                "closing_value": 297.83,
                "extra": {},
            },
        ] + [
            {
                "product_code": str(1900 + i),
                "product_name": f"PRODUCT {i}",
                "packing": "60TAB",
                "opening_qty": float(i),
                "receipts_qty": 0.0,
                "sales_qty": 1.0,
                "sales_value": 10.0,
                "closing_qty": float(max(0, i - 1)),
                "closing_value": 5.0,
                "extra": {},
            }
            for i in range(3, 10)
        ]
        vision_payload["totals"]["extra"]["extraction_method"] = (
            "code_item_stock_statement_photo"
        )
        vision_payload["totals"]["extra"]["layout"] = "code_item_stock_statement"

        with mock.patch(
            "services.sales_statement_extractor._extract_code_item_stock_statement_vision",
            return_value=vision_payload,
        ) as vision_mock:
            result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)

        self.assertTrue(vision_mock.called)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(
            extra.get("extraction_method"), "code_item_stock_statement_photo"
        )
        self.assertEqual(result.get("stockist_name"), "THE PHARMA HUB")
        self.assertIn("HIMALAYA", str(result.get("company_name") or "").upper())
        self.assertEqual(result.get("period_from"), "2026-08-01")
        self.assertEqual(result.get("period_to"), "2026-08-31")
        abana = next(
            i
            for i in (result.get("line_items") or [])
            if str(i.get("product_code")) == "1397"
        )
        self.assertEqual(abana.get("opening_qty"), 55.0)
        self.assertEqual(abana.get("sales_qty"), 12.0)
        self.assertEqual(abana.get("closing_qty"), 43.0)
        self.assertEqual(abana.get("sales_value"), 1316.52)


if __name__ == "__main__":
    unittest.main()
