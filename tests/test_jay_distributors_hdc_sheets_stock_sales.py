"""Google Sheets screenshot of HDC STOCK & SALES STATEMENT (Jay Distributors).

Must not regress printed Marg opening/receive/issue photos (Katruwar).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from services.sales_statement_extractor import (
    _is_excel_sheets_stock_sales_screenshot,
    _main_stock_enrich_spreadsheet_meta,
    _main_stock_ocr_contaminated_by_ui,
    _main_stock_repair_clipped_himalaya_names,
    empty_result,
    extract_sales_statement,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "test_pdfs"
    / "jay_distributors_hdc_sheets_stock_sales.png"
)

SHEETS_OCR_SAMPLE = """
x HDC_HIMAL.. g o &
Not saved yet
A B C D E F G H I J
1 JAY DISTRIBUTORS.
2 1974/0, NEHRU STREET NEAR JAIN TEMPLE, NEXT TO SHANTINATH APT., VAPI (W)
3 Phone : 0260-2463112 E-Mail : jaydistributorsvapi@gmail.com
4 D.L.No. : 21B G/VL476 20/B G/VL487
6 HDC HIMALAYA(ZANDRA) STOCK & SALES STATEMENT 01-08-2026 - 31-08-2026
PRODUCT DESCRIPTION OPENING STOCK OPENING VALUE RECEIVE QUANTITY RECEIVE VALUE ISSUE QUANTITY ISSUE VALUE CLOSING STOCK CLOSING VALUE
8 NA TAB 60CAP 59 12105.96 0 0 3 729 56 11490.4
9 NISAN DROPS 30ML 36 2382.84 0 0 6 446.47 30 1985.7
Stock & Sales Statement v +
"""


class TestJayDistributorsSheetsStockSales(unittest.TestCase):
    def test_detects_excel_sheets_screenshot(self):
        self.assertTrue(_is_excel_sheets_stock_sales_screenshot(SHEETS_OCR_SAMPLE))
        # Printed Marg title alone is not a Sheets screenshot.
        self.assertFalse(
            _is_excel_sheets_stock_sales_screenshot(
                "MAIN STOCK & SALES STATEMENT\nPRODUCT DESCRIPTION OPENING STOCK"
            )
        )

    def test_ocr_ui_contamination(self):
        bad = empty_result("x.png", "png")
        bad["stockist_name"] = "x HDC_HIMAL.. g o &"
        bad["line_items"] = [
            {"product_name": f"{i} NISAN DROPS 30ML", "opening_qty": 1}
            for i in range(10)
        ]
        self.assertTrue(_main_stock_ocr_contaminated_by_ui(bad, SHEETS_OCR_SAMPLE))

    def test_enrich_meta_and_clipped_names(self):
        result = empty_result("x.png", "png")
        result["company_name"] = "HDC HIMALAYA(ZANDRA)"
        result["line_items"] = [
            {
                "product_name": "NISAN DROPS 30ML",
                "opening_qty": 36.0,
                "receipts_qty": 0.0,
                "sales_qty": 6.0,
                "closing_qty": 30.0,
                "sales_value": 446.47,
                "closing_value": 1985.7,
                "extra": {},
            },
            {
                "product_name": "ONE FORTE TAB 30TAB",
                "opening_qty": 5.0,
                "receipts_qty": 56.0,
                "sales_qty": 6.0,
                "closing_qty": 55.0,
                "sales_value": 721.39,
                "closing_value": 5906.45,
                "extra": {},
            },
            {
                "product_name": "2 DS 100ML SY 100ML",
                "opening_qty": 70.0,
                "receipts_qty": 0.0,
                "sales_qty": 38.0,
                "closing_qty": 32.0,
                "sales_value": 6427.01,
                "closing_value": 4862.72,
                "extra": {},
            },
        ]
        _main_stock_enrich_spreadsheet_meta(result, SHEETS_OCR_SAMPLE)
        _main_stock_repair_clipped_himalaya_names(result, SHEETS_OCR_SAMPLE)
        self.assertEqual(result.get("stockist_name"), "JAY DISTRIBUTORS")
        self.assertEqual(result.get("period_from"), "2026-08-01")
        self.assertEqual(result.get("period_to"), "2026-08-31")
        self.assertIn("HIMALAYA", str(result.get("company_name") or "").upper())
        names = [i["product_name"] for i in result["line_items"]]
        self.assertTrue(names[0].upper().startswith("BONNISAN DROPS"))
        self.assertTrue(names[1].upper().startswith("GERIFORTE TAB"))
        self.assertTrue(names[2].upper().startswith("LIV.52 DS"))

    @unittest.skipUnless(FIXTURE.exists(), "Sheets fixture PNG not on this machine")
    def test_extract_prefers_vision_not_ui_ocr(self):
        vision_payload = empty_result(FIXTURE.name, "png")
        vision_payload["stockist_name"] = "JAY DISTRIBUTORS"
        vision_payload["company_name"] = "HDC HIMALAYA(ZANDRA)"
        vision_payload["period_from"] = "2026-08-01"
        vision_payload["period_to"] = "2026-08-31"
        vision_payload["report_title"] = "STOCK & SALES STATEMENT"
        vision_payload["line_items"] = [
            {
                "product_name": "BONNISAN DROPS 30ML",
                "opening_qty": 36.0,
                "receipts_qty": 0.0,
                "sales_qty": 6.0,
                "sales_value": 446.47,
                "closing_qty": 30.0,
                "closing_value": 1985.7,
                "extra": {"opening_value": 2382.84, "receipts_value": 0.0},
            },
            {
                "product_name": "GERIFORTE TAB 60TAB",
                "opening_qty": 50.0,
                "receipts_qty": 100.0,
                "sales_qty": 57.0,
                "sales_value": 10370.61,
                "closing_qty": 93.0,
                "closing_value": 15264.09,
                "extra": {"opening_value": 8206.5, "receipts_value": 16413.0},
            },
        ] + [
            {
                "product_name": f"PRODUCT {i}",
                "opening_qty": float(i),
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": float(i),
                "closing_value": 0.0,
                "extra": {},
            }
            for i in range(3, 12)
        ]
        vision_payload["totals"]["extra"]["extraction_method"] = (
            "main_stock_sales_statement"
        )

        with mock.patch(
            "services.sales_statement_extractor._extract_main_stock_sales_vision",
            return_value=vision_payload,
        ) as vision_mock:
            result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)

        self.assertTrue(vision_mock.called)
        self.assertEqual(
            (result.get("totals") or {}).get("extra", {}).get("extraction_method"),
            "main_stock_sales_statement_sheets",
        )
        self.assertEqual(result.get("stockist_name"), "JAY DISTRIBUTORS")
        self.assertEqual(result.get("period_from"), "2026-08-01")
        self.assertEqual(result.get("period_to"), "2026-08-31")
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 8)
        # Must not keep the old UI-chrome stockist.
        self.assertNotRegex(str(result.get("stockist_name") or ""), r"HDC_HIMAL")
        bonni = next(
            (i for i in items if "BONNISAN" in str(i.get("product_name") or "").upper()),
            None,
        )
        self.assertIsNotNone(bonni)
        self.assertEqual(bonni.get("opening_qty"), 36.0)
        self.assertEqual(bonni.get("sales_qty"), 6.0)
        self.assertEqual(bonni.get("closing_qty"), 30.0)


if __name__ == "__main__":
    unittest.main()
