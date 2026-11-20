"""OCR PRODUCT/PACK/OPENING/... rows assign from headers, not last-N numbers."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _extract_report_period_from_content,
    _parse_ocr_word_boxes_pack_opening,
    _parse_vikash_ocr_text,
)


OCR = """
: 01-Apr-26 To : 30-Apr-26
PRODUCT PACK OPENING PURCHASE FREE P.RETURN FREE SALE FREE S.RETURN FREE OTHERS CLOSING
ARJUNA TABLET 60 - - - - - 13 - - - - 47
BONNISAN DROPS 30ML 57 50 - - - 73 1 - - - 33
BRESOL SYP 200 13 28 - - - 15 - - - - 22
"""


class TestOcrPackOpeningHeaderAlign(unittest.TestCase):
    def test_pack_is_not_opening_or_sales(self):
        result = _parse_vikash_ocr_text(OCR, "517.jpg", ".jpg")
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertTrue(extra.get("ocr_header_aligned"))
        by_name = {str(i.get("product_name") or ""): i for i in result["line_items"]}

        arjuna = by_name["ARJUNA TABLET"]
        self.assertEqual(arjuna.get("packing"), "60")
        self.assertIn(arjuna.get("opening_qty"), (None, 0, 0.0))
        self.assertEqual(arjuna.get("sales_qty"), 13.0)
        self.assertEqual(arjuna.get("closing_qty"), 47.0)

        bonn = by_name["BONNISAN DROPS"]
        self.assertEqual(bonn.get("packing"), "30ML")
        self.assertEqual(bonn.get("opening_qty"), 57.0)
        self.assertEqual(bonn.get("receipts_qty"), 50.0)
        self.assertEqual(bonn.get("sales_qty"), 73.0)
        self.assertEqual(bonn.get("closing_qty"), 33.0)
        self.assertEqual((bonn.get("extra") or {}).get("free_out_qty"), 1.0)

        bresol = by_name["BRESOL SYP"]
        self.assertEqual(bresol.get("packing"), "200")
        self.assertEqual(bresol.get("opening_qty"), 13.0)
        self.assertEqual(bresol.get("receipts_qty"), 28.0)
        self.assertEqual(bresol.get("sales_qty"), 15.0)
        self.assertEqual(bresol.get("closing_qty"), 22.0)

    def test_period_from_printed_dd_mon_yy(self):
        result = _parse_vikash_ocr_text(OCR, "517.jpg", ".jpg")
        self.assertEqual(result.get("period_from"), "2026-04-01")
        self.assertEqual(result.get("period_to"), "2026-04-30")
        pf, pt, source = _extract_report_period_from_content(OCR)
        self.assertEqual(pf, "2026-04-01")
        self.assertEqual(pt, "2026-04-30")
        self.assertEqual(source, "report_mon_range")

    def test_period_line_is_not_stockist(self):
        result = _parse_vikash_ocr_text(OCR, "517.jpg", ".jpg")
        name = str(result.get("stockist_name") or "")
        self.assertFalse(re_search_period(name))
        self.assertNotIn("PRODUCT", name.upper())

    def test_word_boxes_assign_pack_not_opening(self):
        words = []

        def add(text, x, y, page_w=1000, page_h=400):
            words.append(
                {
                    "text": text,
                    "x": x,
                    "y": y,
                    "h": 12,
                    "conf": 80,
                    "page_w": page_w,
                    "page_h": page_h,
                }
            )

        add("PRODUCT", 40, 20)
        add("PACK", 200, 20)
        add("OPENING", 320, 20)
        add("PURCHASE", 440, 20)
        add("SALE", 560, 20)
        add("CLOSING", 900, 20)
        add("ARJUNA", 40, 60)
        add("TABLET", 90, 60)
        add("60", 200, 60)
        add("13", 560, 60)
        add("47", 900, 60)
        add("BONNISAN", 40, 90)
        add("DROPS", 100, 90)
        add("30ML", 200, 90)
        add("57", 320, 90)
        add("50", 440, 90)
        add("73", 560, 90)
        add("33", 900, 90)
        result = _parse_ocr_word_boxes_pack_opening(words, "517.jpg", ".jpg")
        self.assertIsNotNone(result)
        by_name = {str(i.get("product_name") or ""): i for i in result["line_items"]}
        arjuna = by_name["ARJUNA TABLET"]
        self.assertEqual(arjuna.get("packing"), "60")
        self.assertEqual(arjuna.get("sales_qty"), 13.0)
        self.assertEqual(arjuna.get("closing_qty"), 47.0)
        bonn = by_name["BONNISAN DROPS"]
        self.assertEqual(bonn.get("packing"), "30ML")
        self.assertEqual(bonn.get("opening_qty"), 57.0)
        self.assertEqual(bonn.get("receipts_qty"), 50.0)
        self.assertEqual(bonn.get("sales_qty"), 73.0)
        self.assertEqual(bonn.get("closing_qty"), 33.0)

    def test_word_boxes_split_header_y_still_assigns_pack(self):
        words = []

        def add(text, x, y, page_w=1000, page_h=400):
            words.append(
                {
                    "text": text,
                    "x": x,
                    "y": y,
                    "h": 12,
                    "conf": 80,
                    "page_w": page_w,
                    "page_h": page_h,
                }
            )

        add("PRODUCT", 40, 20)
        add("PACK", 200, 20)
        add("OPENING", 320, 20)
        add("PURCHASE", 440, 20)
        add("SALE", 560, 80)
        add("CLOSING", 900, 80)
        add("ARJUNA", 40, 120)
        add("TABLET", 90, 120)
        add("60", 200, 120)
        add("13", 560, 120)
        add("47", 900, 120)
        add("BONNISAN", 40, 150)
        add("DROPS", 100, 150)
        add("30ML", 200, 150)
        add("57", 320, 150)
        result = _parse_ocr_word_boxes_pack_opening(words, "517.jpg", ".jpg")
        self.assertIsNotNone(result)
        by_name = {str(i.get("product_name") or ""): i for i in result["line_items"]}
        arjuna = by_name["ARJUNA TABLET"]
        self.assertEqual(arjuna.get("packing"), "60")
        self.assertEqual(arjuna.get("sales_qty"), 13.0)
        self.assertEqual(arjuna.get("closing_qty"), 47.0)
        self.assertNotEqual(arjuna.get("opening_qty"), 60.0)
        bonn = by_name["BONNISAN DROPS"]
        self.assertEqual(bonn.get("packing"), "30ML")
        self.assertEqual(bonn.get("opening_qty"), 57.0)

    def test_garbled_pack_opening_does_not_shift_last_n(self):
        garbled = (
            "cinoma PACK OPENING PURCHASE FREE P.Ri yy\n"
            "ARJUNA TABLET 60 60 : = i 7 3 1 x .\n"
            "BONNISAN DROPS 30ML 57 SA : - 4 20 2 x =\n"
        )
        result = _parse_vikash_ocr_text(garbled, "517.jpg", ".jpg")
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertTrue(extra.get("ocr_pack_opening_unaligned"))
        self.assertTrue(extra.get("layout_unsafe"))
        self.assertEqual(result.get("line_items") or [], [])


def re_search_period(text: str) -> bool:
    import re

    return bool(re.search(r"\d{1,2}[- ][A-Za-z]{3,9}[- ]\d{2,4}", text or ""))
