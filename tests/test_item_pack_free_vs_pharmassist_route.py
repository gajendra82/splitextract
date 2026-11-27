"""PRODUCT|PACK|…|FREE|P.RETURN|SALE|CLOSING must not lock PharmAssist/SaleRet.

Layout family: STOCK AND SALES ANALYSIS qty-only FREE/P.RETURN grid (e.g. 80.jpg).
Blank-OCR PharmAssist divert used to stamp default title 'Stock and Sale Report'
and zero sales_qty.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from services.sales_statement_extractor import (
    _pack_op_pur_bal_looks_like_pharmassist_misfile,
    _parse_vikash_ocr_text,
    _ssa_dump_result_looks_like_item_pack_free_misfile,
    empty_line_item,
    empty_result,
)
from services.stock_direct_vision import (
    _has_item_pack_free_return_headers,
    _has_pharmassist_stock_sale_headers,
    _pharmassist_result_has_native_chrome,
    classify_stock_direct_vision,
)

FREE_PEEK = (
    "AASTHA TRADERS\n"
    "STOCK AND SALES ANALYSIS\n"
    "HIMALAYA DRUG COMPANY - ZAND\n"
    "From : 01-Oct-26 To : 31-Oct-26\n"
    "PRODUCT PACK OPENING PURCHASE FREE P.RETURN FREE SALE FREE "
    "S.RETURN FREE OTHERS CLOSING\n"
)

OCR_ROWS = """
AASTHA TRADERS
KH NO-262/228/173 GALI NO-8, NEAR RAM MANDIR, WAZIRABAD VILLAGE, DELHI-84
STOCK AND SALES ANALYSIS
HIMALAYA DRUG COMPANY - ZAND
From : 01-Oct-26 To : 31-Oct-26
PRODUCT PACK OPENING PURCHASE FREE P.RETURN FREE SALE FREE S.RETURN FREE OTHERS CLOSING
ARJUNA TABLET 60 49 60 - - - 69 - - - - 40
BONNISAN DROPS 30ML 76 - - - - 38 - - - - 38
BONNISAN SYP 200ML 36 28 - - - 14 - - - - 50
DIABECON TAB 60 249 160 - - - 149 - - - - 260
"""

LIVE = Path(r"C:\Users\adity\Downloads\80.jpg")


class ItemPackFreeHeaderRouteTests(unittest.TestCase):
    def test_detects_free_preturn_family(self):
        self.assertTrue(_has_item_pack_free_return_headers(FREE_PEEK))
        self.assertFalse(_has_pharmassist_stock_sale_headers(FREE_PEEK))
        self.assertIsNone(classify_stock_direct_vision(FREE_PEEK, "80.jpg"))

    def test_pharmassist_chrome_still_routes(self):
        peek = (
            "FOUR FENCE PHARMA PVT. LTD.\n"
            "Stock and Sale Report ----- From date 01-Aug-26 to 31-Aug-26\n"
            "Opening Val.: 70260.98 Closing Val.: 61133.16\n"
            "Sales : 38607.82 Sales (Jul): 41978.32 Sales (Jun): 49027.42\n"
        )
        self.assertTrue(_has_pharmassist_stock_sale_headers(peek))
        decision = classify_stock_direct_vision(peek, "x.jpg")
        self.assertIsNotNone(decision)
        self.assertEqual(decision["layout"], "pharmassist_stock_sale")

    def test_default_title_is_not_native_chrome(self):
        result = empty_result("80.jpg", "jpg")
        result["report_title"] = "Stock and Sale Report"
        item = empty_line_item()
        item["product_name"] = "ARJUNA TABLET"
        item["opening_qty"] = 49.0
        item["sales_qty"] = 0.0
        item["closing_qty"] = 60.0
        result["line_items"] = [item] * 10
        self.assertFalse(_pharmassist_result_has_native_chrome(result))
        self.assertFalse(
            _pack_op_pur_bal_looks_like_pharmassist_misfile(result, peek_text="")
        )

    def test_analysis_title_never_pharmassist_misfile(self):
        result = empty_result("80.jpg", "jpg")
        result["report_title"] = "STOCK AND SALES ANALYSIS"
        item = empty_line_item()
        item["product_name"] = "ARJUNA TABLET"
        item["opening_qty"] = 49.0
        item["sales_qty"] = 0.0
        item["closing_qty"] = 60.0
        item["extra"] = {"flags": ["SSA_DUMP_RECEIPT_AS_OPENING"]}
        result["line_items"] = [item] * 12
        self.assertFalse(
            _pack_op_pur_bal_looks_like_pharmassist_misfile(result, peek_text=FREE_PEEK)
        )

    def test_ssa_dump_item_pack_free_misfile(self):
        result = empty_result("80.jpg", "jpg")
        result["report_title"] = "STOCK AND SALES ANALYSIS"
        items = []
        for name, op in (
            ("ARJUNA TABLET", 49),
            ("BONNISAN DROPS", 76),
            ("BRESOL SYP", 30),
            ("CYSTONE FORTE", 76),
            ("DIABECON TAB", 249),
            ("EVECARE SYP", 61),
            ("GASEX TAB", 109),
            ("GERIFORTE TAB", 40),
            ("HIMPLASIA TAB", 30),
            ("HIORA GA GEL", 41),
        ):
            item = empty_line_item()
            item["product_name"] = name
            item["packing"] = "60"
            item["opening_qty"] = float(op)
            item["receipts_qty"] = 0.0
            item["sales_qty"] = 0.0
            item["closing_qty"] = float(op)
            item["extra"] = {"flags": ["SSA_DUMP_RECEIPT_AS_OPENING"], "dump_qty": 0.0}
            items.append(item)
        result["line_items"] = items
        self.assertTrue(_ssa_dump_result_looks_like_item_pack_free_misfile(result))

    def test_ocr_header_align_keeps_sales(self):
        parsed = _parse_vikash_ocr_text(OCR_ROWS, "80.jpg", ".jpg")
        by = {i["product_name"]: i for i in parsed["line_items"]}
        self.assertEqual(by["ARJUNA TABLET"]["sales_qty"], 69.0)
        self.assertEqual(by["BONNISAN DROPS"]["sales_qty"], 38.0)
        self.assertEqual(by["DIABECON TAB"]["sales_qty"], 149.0)
        self.assertEqual(parsed.get("stockist_name"), "AASTHA TRADERS")


@unittest.skipUnless(LIVE.is_file(), "80.jpg not on this machine")
class ItemPackFreeLivePhotoTests(unittest.TestCase):
    def test_80_jpg_does_not_lock_pharmassist_zero_sales(self):
        from services.sales_statement_extractor import extract_sales_statement

        vt_result = empty_result(LIVE.name, "jpg")
        vt_result["stockist_name"] = "AASTHA TRADERS"
        vt_result["report_title"] = "STOCK AND SALES ANALYSIS"
        vt_result["period_from"] = "2026-10-01"
        vt_result["period_to"] = "2026-10-31"
        row = empty_line_item()
        row["product_name"] = "ARJUNA TABLET"
        row["packing"] = "60"
        row["opening_qty"] = 49.0
        row["receipts_qty"] = 60.0
        row["sales_qty"] = 69.0
        row["closing_qty"] = 40.0
        row["extra"] = {
            "vision_column_headers": [
                "PRODUCT",
                "PACK",
                "OPENING",
                "PURCHASE",
                "FREE",
                "P.RETURN",
                "FREE",
                "SALE",
                "FREE",
                "S.RETURN",
                "FREE",
                "OTHERS",
                "CLOSING",
            ],
            "field_source": {
                "opening_qty": "printed",
                "sales_qty": "printed",
                "closing_qty": "printed",
            },
        }
        vt_result["line_items"] = [row]
        vt_result["totals"]["extra"].update(
            {
                "extraction_method": "vision_table",
                "vision_table_final": True,
                "layout": "item_pack_free_return",
                "valid_ratio": 1.0,
            }
        )

        with patch(
            "services.stock_vision_table.vision_table_active_for", return_value=True
        ), patch(
            "services.stock_vision_table.run_vision_table_path",
            return_value={
                "status": "ok",
                "result": vt_result,
                "gemini_calls": 1,
                "gemini_budget": 2,
            },
        ), patch(
            "services.sales_statement_extractor._extract_ssa_opening_receipt_issue_dump_image",
            return_value=None,
        ), patch(
            "services.sales_statement_extractor._ocr_image_to_text", return_value=""
        ), patch(
            "services.sales_statement_extractor._ssa_dump_portrait_noise_candidate",
            return_value=True,
        ), patch(
            "services.sales_statement_extractor._ssa_before_lstsl_empty_ocr_enabled",
            return_value=True,
        ), patch(
            "services.stock_vision_table._needs_vertical_split", return_value=False
        ), patch(
            "services.sales_statement_extractor._extract_pack_op_pur_bal_stock_sale_vision",
            return_value=None,
        ), patch(
            "services.stock_direct_vision.extract_pharmassist_direct_vision",
            return_value=None,
        ):
            result = extract_sales_statement(LIVE.read_bytes(), LIVE.name)

        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertNotEqual(extra.get("extraction_method"), "pharmassist_stock_sale_vision")
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 1)
        arjuna = next(
            i for i in items if str(i.get("product_name") or "").startswith("ARJUNA")
        )
        self.assertEqual(arjuna.get("sales_qty"), 69.0)


if __name__ == "__main__":
    unittest.main()
