"""J R SHAH Stock and Sale Statement: Op/Pur/SP/Pur Val/Sale/SS/Sp Qty/Sale Val/Bal/Bal Val.

Must not steal ZANDRA Item Cd / Op Stk or STOCK & SALES ANALYSIS parsers.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from services.sales_statement_extractor import (
    _finalize_op_pur_sp_sale_bal_val,
    _is_op_pur_sp_sale_bal_val_text,
    _looks_like_op_pur_sp_sale_bal_val_misread,
    _looks_like_zandra_stock_sale_text,
    empty_result,
    extract_sales_statement,
)

FIXTURE = Path(__file__).resolve().parents[1] / (
    "0000700077_2026_08_ZL_06_304_06092026141652.jpeg"
)

HEADER = """
J R SHAH AND COMPANY
Stock and Sale Statement From 28-Jul-26 to 28-Aug-26
Item Name pack Op Pur SP Pur Val Sale SS Sp Qty Sale Val Bal. Bal Val
THE HIMALAYA DRUG (ZEAL)
"""

ZANDRA = """
Stock and Sale Statement
Item Cd Item Name Op Stk P Qty P S Qty P Val S Qty S S Qty S Val Cl Stk Cl Val
"""


class TestOpPurSpSaleBalVal(unittest.TestCase):
    def test_detector_accepts_jr_shah_not_zandra(self):
        self.assertTrue(_is_op_pur_sp_sale_bal_val_text(HEADER))
        self.assertFalse(_is_op_pur_sp_sale_bal_val_text(ZANDRA))
        self.assertFalse(_looks_like_zandra_stock_sale_text(HEADER))
        self.assertTrue(_looks_like_zandra_stock_sale_text(ZANDRA))

    def test_finalize_period_and_layout(self):
        result = empty_result("x.jpg", "jpg")
        result["period_from"] = "2023-07-28"
        result["period_to"] = "2023-08-28"
        result["line_items"] = [
            {
                "product_name": "H AACTARIL SOAP 75GM",
                "packing": "75GM",
                "opening_qty": 53,
                "receipts_qty": 0,
                "sales_qty": 22,
                "sales_value": 1919,
                "closing_qty": 31,
                "closing_value": 2167,
                "extra": {"purchase_value": 0},
            }
        ]
        finished = _finalize_op_pur_sp_sale_bal_val(result)
        self.assertEqual(finished.get("period_from"), "2026-07-28")
        self.assertEqual(finished.get("period_to"), "2026-08-28")
        extra = ((finished.get("totals") or {}).get("extra") or {})
        self.assertEqual(extra.get("extraction_method"), "op_pur_sp_sale_bal_val_photo")
        self.assertIsNone(finished["line_items"][0].get("opening_value"))
        self.assertIsNone((finished.get("totals") or {}).get("opening_value"))

    def test_finalize_prefers_printed_totals_near_line_sums(self):
        result = empty_result("x.jpg", "jpg")
        result["line_items"] = [
            {
                "product_name": "H A",
                "opening_qty": 10,
                "receipts_qty": 5,
                "sales_qty": 3,
                "sales_value": 100,
                "closing_qty": 12,
                "closing_value": 200,
                "extra": {"purchase_value": 50},
            },
            {
                "product_name": "H B",
                "opening_qty": 20,
                "receipts_qty": 0,
                "sales_qty": 7,
                "sales_value": 150,
                "closing_qty": 13,
                "closing_value": 300,
                "extra": {"purchase_value": 0},
            },
        ]
        # Printed TOTAL near product-row money — prefer the printed footer.
        result["totals"]["sales_value"] = 250.0
        result["totals"]["closing_value"] = 500.0
        result["totals"]["extra"]["purchase_value"] = 50.0
        result["totals"]["extra"]["closing_qty"] = 25.0
        finished = _finalize_op_pur_sp_sale_bal_val(result)
        totals = finished.get("totals") or {}
        extra = totals.get("extra") or {}
        self.assertEqual(totals.get("sales_value"), 250.0)
        self.assertEqual(totals.get("closing_value"), 500.0)
        self.assertEqual(extra.get("purchase_value"), 50.0)
        # Bal qty misfiled as Bal Val must not win over product-row money.
        result2 = empty_result("x.jpg", "jpg")
        result2["line_items"] = result["line_items"]
        result2["totals"]["sales_value"] = 250.0
        result2["totals"]["closing_value"] = 25.0  # equals closing qty sum
        result2["totals"]["extra"] = {"purchase_value": 50.0, "closing_qty": 25.0}
        finished2 = _finalize_op_pur_sp_sale_bal_val(result2)
        # Footer closing_value below money threshold falls back to line sum.
        self.assertEqual((finished2.get("totals") or {}).get("closing_value"), 500.0)

    def test_misread_detector(self):
        bad = empty_result("x.jpg", "jpg")
        bad["stockist_name"] = "J R SHAH AND COMPANY"
        bad["report_title"] = "Stock and Sale Statement"
        bad["line_items"] = [
            {
                "product_name": f"H ITEM {i}",
                "packing": "60TAB",
                "opening_qty": 10.0,
                "receipts_qty": 0.0,
                "sales_qty": 2.0,
                "closing_qty": 8.0,
                "sales_value": 0.0,
                "closing_value": 0.0,
            }
            for i in range(10)
        ]
        self.assertTrue(_looks_like_op_pur_sp_sale_bal_val_misread(bad))
        good = empty_result("x.jpg", "jpg")
        good["report_title"] = "Stock and Sale Statement"
        good["totals"]["extra"]["extraction_method"] = "pack_op_pur_bal_stock_sale_vision"
        good["line_items"] = [
            {
                "product_name": f"H ITEM {i}",
                "packing": "60TAB",
                "opening_qty": 10.0,
                "receipts_qty": 0.0,
                "sales_qty": 2.0,
                "closing_qty": 8.0,
                "sales_value": 100.0,
                "closing_value": 400.0,
                "extra": {"purchase_value": 0.0},
            }
            for i in range(10)
        ]
        self.assertFalse(_looks_like_op_pur_sp_sale_bal_val_misread(good))

    @unittest.skipUnless(FIXTURE.exists(), "J R SHAH fixture not on this machine")
    def test_extract_uses_dedicated_vision(self):
        vision_payload = empty_result(FIXTURE.name, "jpg")
        vision_payload["stockist_name"] = "J R SHAH AND COMPANY"
        vision_payload["company_name"] = "THE HIMALAYA DRUG (ZEAL)"
        vision_payload["period_from"] = "2026-07-28"
        vision_payload["period_to"] = "2026-08-28"
        vision_payload["report_title"] = "Stock and Sale Statement"
        vision_payload["line_items"] = [
            {
                "product_name": "H AACTARIL SOAP 75GM",
                "packing": "75GM",
                "opening_qty": 53.0,
                "receipts_qty": 0.0,
                "sales_qty": 22.0,
                "sales_value": 1919.0,
                "closing_qty": 31.0,
                "closing_value": 2167.0,
                "extra": {"purchase_value": 0.0},
            },
            {
                "product_name": "H ABANA 60TABLETS",
                "packing": "60TAB",
                "opening_qty": 15.0,
                "receipts_qty": 100.0,
                "sales_qty": 27.0,
                "sales_value": 4311.0,
                "closing_qty": 88.0,
                "closing_value": 12423.0,
                "extra": {"purchase_value": 14117.0},
            },
        ] + [
            {
                "product_name": f"H PRODUCT {i}",
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
            "pack_op_pur_bal_stock_sale_vision"
        )
        vision_payload["totals"]["extra"]["layout"] = "pack_op_pur_bal_stock_sale"

        with mock.patch(
            "services.sales_statement_extractor._maybe_early_vision_for_image",
            return_value=(vision_payload, True),
        ), mock.patch(
            "services.sales_statement_extractor._extract_pack_op_pur_bal_stock_sale_vision",
            return_value=vision_payload,
        ), mock.patch(
            "services.sales_statement_extractor._ocr_image_to_text",
            return_value="noise\n" + HEADER,
        ), mock.patch(
            "services.gemini_extraction_fallback.maybe_apply_gemini_fallback",
            side_effect=lambda result, *a, **k: result,
        ):
            result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)

        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertIn(
            extra.get("extraction_method"),
            {
                "op_pur_sp_sale_bal_val_photo",
                "pack_op_pur_bal_stock_sale_vision",
            },
        )
        abana = next(
            i
            for i in (result.get("line_items") or [])
            if "ABANA" in str(i.get("product_name") or "").upper()
        )
        self.assertEqual(abana.get("opening_qty"), 15.0)
        self.assertEqual(abana.get("receipts_qty"), 100.0)
        self.assertEqual(abana.get("sales_qty"), 27.0)
        self.assertEqual(abana.get("closing_qty"), 88.0)
        self.assertEqual(abana.get("sales_value"), 4311.0)
        self.assertEqual(abana.get("closing_value"), 12423.0)


if __name__ == "__main__":
    unittest.main()
