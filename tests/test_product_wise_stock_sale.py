"""PRODUCT WISE STOCK & SALE photo — packing is not opening, SALE QTY is sales."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _finalize_product_wise_stock_sale,
    _looks_like_product_wise_stock_sale_result,
    _product_wise_drop_banner_items,
    _product_wise_fix_pack_used_as_opening,
    _product_wise_is_company_banner,
    _product_wise_is_footer_total,
    _product_wise_table_crops,
    _looks_like_product_wise_stock_sale_text,
    _looks_like_zandra_stock_sale_result,
    empty_result,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000704607_2026_08_ZL_08_215_01092026054203.jpg"
)


class TestProductWiseDetection(unittest.TestCase):
    def test_detects_sale_and_ocr_bale_not_zandra(self):
        self.assertTrue(
            _looks_like_product_wise_stock_sale_text(
                "PRODUCT WISE STOCK & SALE FROM 01/08/2026 TO 31/03/2027"
            )
        )
        self.assertTrue(
            _looks_like_product_wise_stock_sale_text("PRODUCT WISE STOCK & BALE")
        )
        zandra = empty_result("z.png", "png")
        zandra["report_title"] = "Stock and Sale Statement"
        self.assertFalse(_looks_like_product_wise_stock_sale_result(zandra))
        self.assertTrue(_looks_like_zandra_stock_sale_result(zandra))
        self.assertFalse(_looks_like_product_wise_stock_sale_text(""))

    def test_keeps_cross_year_period(self):
        result = empty_result("nagal.jpg", "jpg")
        result["period_from"] = "2026-08-01"
        result["period_to"] = "2026-03-31"
        result["report_title"] = "PRODUCT WISE STOCK & BALE"
        result["line_items"] = [
            {
                "product_name": "AMALKI TAB",
                "packing": "60",
                "opening_qty": 17.0,
                "sales_qty": 14.0,
                "sales_value": 2405.91,
                "closing_qty": 103.0,
                "closing_value": 15290.94,
                "receipts_qty": 0.0,
                "extra": {"opening_value": 2652.51},
            }
        ]
        parsed = {
            "report_title": "PRODUCT WISE STOCK & SALE FROM 01/08/2026 TO 31/03/2027",
            "period_from": "2026-08-01",
            "period_to": "2027-03-31",
            "line_items": [{"receipts_value": 0}],
        }
        finished = _finalize_product_wise_stock_sale(result, parsed)
        extra = (finished.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "product_wise_stock_sale_vision")
        self.assertEqual(finished["period_from"], "2026-08-01")
        self.assertEqual(finished["period_to"], "2027-03-31")
        self.assertEqual(finished["line_items"][0]["opening_qty"], 17.0)
        self.assertNotEqual(finished["line_items"][0]["opening_qty"], 60.0)
        self.assertEqual(finished["line_items"][0]["sales_qty"], 14.0)

    def test_moves_packing_60_out_of_opening_qty(self):
        item = _product_wise_fix_pack_used_as_opening(
            {
                "product_name": "ANANA TAB",
                "packing": None,
                "opening_qty": 60.0,
                "receipts_qty": 0.0,
                "sales_qty": 14.0,
                "sales_value": 2405.91,
                "closing_qty": 103.0,
                "closing_value": 15290.94,
                "opening_value": 2652.51,
                "extra": {"opening_value": 2652.51},
            }
        )
        self.assertEqual(item["packing"], "60")
        self.assertNotEqual(item["opening_qty"], 60.0)
        self.assertAlmostEqual(item["opening_qty"], 18.0)
        self.assertEqual(item["sales_qty"], 14.0)
        self.assertEqual(item["closing_qty"], 103.0)

    def test_drops_grand_total_attached_to_last_name(self):
        self.assertTrue(
            _product_wise_is_footer_total(
                {
                    "product_name": "VRIKSHAMLA TAB",
                    "opening_qty": 2671.0,
                    "sales_value": 117465.19,
                    "closing_value": 372462.78,
                }
            )
        )
        self.assertTrue(
            _product_wise_is_footer_total(
                {
                    "product_name": "VRIKSHAMLA TAB",
                    "opening_qty": 37.0,
                    "sales_qty": 753.0,
                    "closing_qty": 2776.0,
                    "sales_value": 117465.19,
                }
            )
        )
        self.assertFalse(
            _product_wise_is_footer_total(
                {
                    "product_name": "AMALKI TAB",
                    "opening_qty": 17.0,
                    "sales_qty": 14.0,
                    "closing_qty": 103.0,
                    "sales_value": 2405.91,
                    "closing_value": 15290.94,
                }
            )
        )

    def test_drops_himalaya_drug_zeal_inzmam_banner(self):
        self.assertTrue(
            _product_wise_is_company_banner("HIMALAYA DRUG(ZEAL)--(INZMAM)")
        )
        self.assertTrue(_product_wise_is_company_banner("HIMALAYA DRUG(ZEAL)"))
        self.assertTrue(_product_wise_is_company_banner("HIMALAYA ZEAL"))
        self.assertFalse(_product_wise_is_company_banner("AMALKI TAB"))
        self.assertFalse(_product_wise_is_company_banner("AACTARIL SOAP 75GM"))
        result = empty_result("nagal.jpg", "jpg")
        result["report_title"] = "PRODUCT WISE STOCK & SALE"
        result["line_items"] = [
            {
                "product_name": "HIMALAYA DRUG(ZEAL)--(INZMAM)",
                "opening_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 0.0,
            },
            {
                "product_name": "AMALKI TAB",
                "packing": "60",
                "opening_qty": 17.0,
                "sales_qty": 14.0,
                "closing_qty": 103.0,
            },
        ]
        finished = _finalize_product_wise_stock_sale(result, {})
        names = [item.get("product_name") for item in finished["line_items"]]
        self.assertNotIn("HIMALAYA DRUG(ZEAL)--(INZMAM)", names)
        self.assertEqual(names, ["AMALKI TAB"])
        titled = empty_result("nagal.jpg", "jpg")
        titled["line_items"] = [
            {"product_name": "PRODUCT WISE STOCK & SALE FROM 01/08/2026"}
        ]
        self.assertTrue(_looks_like_product_wise_stock_sale_result(titled))
        banner_only = empty_result("other.jpg", "jpg")
        banner_only["line_items"] = [
            {"product_name": "HIMALAYA DRUG(ZEAL)--(INZMAM)"},
            {
                "product_name": "AACTRARIL SAOP 75GM",
                "opening_qty": 61.0,
                "sales_qty": 25.0,
            },
        ]
        self.assertFalse(_looks_like_product_wise_stock_sale_result(banner_only))
        stripped = _product_wise_drop_banner_items(banner_only)
        names = [item.get("product_name") for item in stripped["line_items"]]
        self.assertNotIn("HIMALAYA DRUG(ZEAL)--(INZMAM)", names)
        self.assertEqual(names[0], "AACTRARIL SAOP 75GM")


@unittest.skipUnless(FIXTURE.is_file(), "missing PRODUCT WISE STOCK & SALE fixture")
class TestProductWiseFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.items = cls.result.get("line_items") or []

    def test_uses_format_vision_not_generic_zero_sales(self):
        self.assertEqual(
            self.extra.get("extraction_method"), "product_wise_stock_sale_vision"
        )
        self.assertEqual(self.result["stockist_name"], "NAGAL ENTERPRISE")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2027-03-31")
        self.assertGreaterEqual(len(_product_wise_table_crops(FIXTURE.read_bytes())), 2)
        self.assertGreaterEqual(len(self.items), 41)
        self.assertNotIn("Invoices", self.result)
        sold = sum(1 for item in self.items if float(item.get("sales_qty") or 0) > 0)
        self.assertGreaterEqual(sold, 8)
        first = next(
            item
            for item in self.items
            if re.search(r"AMALKI|ANANA|ANNADA|AMLA", str(item.get("product_name") or ""), re.I)
        )
        self.assertNotEqual(first["opening_qty"], 60.0)
        self.assertGreater(float(first.get("opening_qty") or 0), 0)
        self.assertGreater(float(first.get("sales_qty") or 0), 0)


if __name__ == "__main__":
    unittest.main()
