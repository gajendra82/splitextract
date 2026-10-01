"""Marg OPENING/RECEIPT/ISSUE/CLOSING M.EXP photos must not use Op Stk vision."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_marg_closing_mexp_photo_text,
    _looks_like_zandra_stock_sale_result,
    _marg_mexp_set_total_stock,
    _repair_marg_mexp_missing_issue,
    empty_result,
    extract_sales_statement,
)


FIXTURE = Path("0000736024_2026_08_ZA_30_313_04092026142630.jpg")


class TestMargClosingMexpPhoto(unittest.TestCase):
    def test_header_gate_tolerates_ocr_typos(self):
        self.assertTrue(
            _is_marg_closing_mexp_photo_text(
                "ITEM DESCRIPTION OPENING RECELPT TSSUE CLOSING M.EXP\n"
                "HIMALAYA ZANDRA"
            )
        )
        self.assertFalse(
            _is_marg_closing_mexp_photo_text(
                "Item Cd Item Name Op Stk P Qty S Qty Cl Stk\nHIMALAYA ZANDRA"
            )
        )

    def test_company_zandra_alone_does_not_select_opstk_grid(self):
        result = empty_result("x.jpg", "jpg")
        result["company_name"] = "HIMALAYA ZANDRA"
        result["report_title"] = "STOCK & SALES"
        self.assertFalse(_looks_like_zandra_stock_sale_result(result))
        result["report_title"] = "Stock and Sale Statement"
        self.assertTrue(_looks_like_zandra_stock_sale_result(result))

    def test_infers_issue_when_vision_drops_it(self):
        items = [
            {
                "product_name": "BONNISAN SYP",
                "opening_qty": 50,
                "receipts_qty": 0,
                "sales_qty": 0,
                "closing_qty": 46,
            },
            {
                "product_name": "BRESOL SYP",
                "opening_qty": 19,
                "receipts_qty": 0,
                "sales_qty": 0,
                "closing_qty": 19,
            },
        ]
        _repair_marg_mexp_missing_issue(items)
        self.assertEqual(items[0]["sales_qty"], 4.0)
        # Opening equals closing with blank ISSUE — leave sales at 0.
        self.assertEqual(items[1]["sales_qty"], 0)

    def test_restores_opening_when_only_issue_was_kept(self):
        items = [
            {
                "product_name": "BONNISPAZ DROPS",
                "opening_qty": 0,
                "receipts_qty": 0,
                "sales_qty": 2,
                "closing_qty": 0,
            }
        ]
        _repair_marg_mexp_missing_issue(items)
        self.assertEqual(items[0]["opening_qty"], 2.0)
        self.assertEqual(items[0]["sales_qty"], 2.0)
        self.assertEqual(items[0]["closing_qty"], 0.0)

    @unittest.skipUnless(FIXTURE.is_file(), "missing Marg CLOSING M.EXP photo")
    def test_fixture_maps_issue_to_sales_qty(self):
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        extra = ((result.get("totals") or {}).get("extra") or {})
        self.assertEqual(extra.get("extraction_method"), "marg_closing_mexp_photo")
        by_key = {
            (
                str(i.get("product_name") or "").upper(),
                str(i.get("packing") or "").upper(),
            ): i
            for i in (result.get("line_items") or [])
        }
        drops = next(
            (
                i
                for i in result["line_items"]
                if re.search(r"BONNISAN\s+DROP", str(i.get("product_name") or ""), re.I)
            ),
            None,
        )
        self.assertIsNotNone(drops, msg=list(by_key)[:5])
        self.assertEqual(drops["sales_qty"], 26.0)
        syp = next(
            (
                i
                for i in result["line_items"]
                if re.search(r"BONNISAN\s+SYP", str(i.get("product_name") or ""), re.I)
                and "100" in f"{i.get('product_name')} {i.get('packing')}"
            ),
            None,
        )
        self.assertIsNotNone(syp, msg=list(by_key)[:8])
        self.assertEqual(syp["opening_qty"], 50.0)
        self.assertEqual(syp["sales_qty"], 4.0)
        self.assertEqual(syp["closing_qty"], 46.0)
        paz = next(
            (
                i
                for i in result["line_items"]
                if re.search(r"BONNISPAZ", str(i.get("product_name") or ""), re.I)
            ),
            None,
        )
        self.assertIsNotNone(paz)
        self.assertEqual(paz["opening_qty"], 2.0)
        self.assertEqual(paz["sales_qty"], 2.0)
        self.assertEqual(paz["closing_qty"], 0.0)

    def test_total_stock_is_opening_plus_receipt(self):
        item = {
            "product_name": "BONNISAN DROP",
            "packing": "1*30ML",
            "opening_qty": 15.0,
            "receipts_qty": 40.0,
            "sales_qty": 26.0,
            "closing_qty": 29.0,
            "extra": {"layout": "marg_closing_mexp"},
        }
        _marg_mexp_set_total_stock([item])
        self.assertEqual(item["extra"]["total_stock"], 55.0)
        self.assertEqual(item["sales_qty"], 26.0)
        self.assertEqual(item["closing_qty"], 29.0)


if __name__ == "__main__":
    unittest.main()
