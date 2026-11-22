"""Wide STOCK & SALES PDFs: product/opening page + sales/closing page stitch."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.stock_native_resolver import (
    _footer_totals_from_page_results,
    build_items_from_pdf_page_results,
)
from services.stock_native_tables import _FitzWordPage, from_pdf_words, resolver_ready


def _word(x0, top, x1, bottom, text):
    return {"x0": x0, "top": top, "x1": x1, "bottom": bottom, "text": text}


def _left_page():
    words = [
        _word(103.0, 168.1, 148.0, 179.1, "PRODUCT"),
        _word(150.6, 168.1, 212.3, 179.1, "DESCRIPTION"),
        _word(264.2, 168.1, 287.1, 179.1, "UNIT"),
        _word(295.5, 168.1, 338.7, 179.1, "OPENING"),
        _word(348.1, 168.1, 397.5, 179.1, "PURCHASE"),
        _word(406.5, 168.1, 428.5, 179.1, "SALE"),
        _word(431.0, 168.1, 469.0, 179.1, "RETURN"),
        _word(477.7, 168.1, 523.4, 179.1, "REPLACE+"),
        _word(302.2, 182.7, 332.2, 193.7, "STOCK"),
        _word(348.5, 182.7, 397.0, 193.7, "QUANTITY"),
        _word(413.5, 182.7, 461.9, 193.7, "QUANTITY"),
        _word(482.3, 182.7, 519.1, 193.7, "OTHERS"),
        _word(56.4, 199.9, 100.2, 210.9, "AACTARIL"),
        _word(102.8, 199.9, 127.2, 210.9, "SOAP"),
        _word(129.8, 199.9, 157.5, 210.9, "75GM"),
        _word(261.9, 199.9, 278.5, 210.9, "PCS"),
        _word(330.5, 199.9, 341.8, 210.9, "51"),
        _word(394.1, 199.9, 399.7, 210.9, "0"),
        _word(465.9, 199.9, 471.5, 210.9, "0"),
        _word(519.9, 199.9, 525.5, 210.9, "0"),
        _word(56.4, 214.5, 88.6, 225.5, "ABANA"),
        _word(91.2, 214.5, 109.0, 225.5, "TAB"),
        _word(111.5, 214.5, 151.9, 225.5, "1X60TAB"),
        _word(261.9, 214.5, 278.5, 225.5, "PCS"),
        _word(330.5, 214.5, 341.8, 225.5, "26"),
        _word(394.1, 214.5, 399.7, 225.5, "0"),
        _word(465.9, 214.5, 471.5, 225.5, "0"),
        _word(519.9, 214.5, 525.5, 225.5, "0"),
        _word(56.4, 229.0, 73.0, 240.0, "LIV"),
        _word(73.0, 229.0, 86.0, 240.0, "52"),
        _word(86.0, 229.0, 107.0, 240.0, "TAB"),
        _word(107.0, 229.0, 160.0, 240.0, "1X100TAB"),
        _word(261.9, 229.0, 278.5, 240.0, "PCS"),
        _word(325.0, 229.0, 345.0, 240.0, "409"),
        _word(377.0, 229.0, 400.0, 240.0, "2500"),
        _word(465.9, 229.0, 471.5, 240.0, "0"),
        _word(519.9, 229.0, 525.5, 240.0, "0"),
        _word(56.4, 635.6, 89.0, 646.6, "TOTAL"),
        _word(89.0, 635.6, 140.0, 646.6, "QUANTITY"),
        _word(319.0, 635.6, 345.0, 646.6, "2798"),
        _word(377.0, 635.6, 400.0, 646.6, "2550"),
        _word(465.9, 635.6, 471.5, 646.6, "6"),
        _word(519.9, 635.6, 525.5, 646.6, "0"),
        _word(56.4, 650.0, 89.0, 661.0, "TOTAL"),
        _word(89.0, 650.0, 120.0, 661.0, "VALUE"),
        _word(319.0, 650.0, 345.0, 661.0, "374832"),
        _word(377.0, 650.0, 400.0, 661.0, "384767"),
        _word(465.9, 650.0, 471.5, 661.0, "709"),
        _word(519.9, 650.0, 525.5, 661.0, "0"),
    ]
    return _FitzWordPage(words, 612.0, 792.0)


def _right_page():
    words = [
        _word(62.7, 168.1, 92.5, 179.1, "TOTAL"),
        _word(118.6, 168.1, 140.5, 179.1, "SALE"),
        _word(179.1, 168.1, 195.9, 179.1, "P/R"),
        _word(220.6, 168.1, 266.3, 179.1, "REPLACE+"),
        _word(275.2, 168.1, 315.6, 179.1, "CLOSING"),
        _word(328.6, 168.1, 352.6, 179.1, "RATE"),
        _word(384.4, 168.1, 406.5, 179.1, "LAST"),
        _word(434.8, 168.1, 467.2, 179.1, "EXPIRY"),
        _word(58.7, 182.7, 96.5, 193.7, "RECEIVE"),
        _word(105.3, 182.7, 153.7, 193.7, "QUANTITY"),
        _word(163.2, 182.7, 211.7, 193.7, "QUANTITY"),
        _word(225.2, 182.7, 262.0, 193.7, "OTHERS"),
        _word(280.5, 182.7, 310.5, 193.7, "STOCK"),
        _word(364.8, 182.7, 401.5, 193.7, "MONTH"),
        _word(404.1, 182.7, 426.0, 193.7, "SALE"),
        _word(436.0, 182.7, 466.0, 193.7, "STOCK"),
        _word(87.3, 199.9, 98.5, 210.9, "51"),
        _word(145.2, 199.9, 156.4, 210.9, "10"),
        _word(208.9, 199.9, 214.4, 210.9, "0"),
        _word(262.9, 199.9, 268.4, 210.9, "0"),
        _word(307.1, 199.9, 318.4, 210.9, "41"),
        _word(332.6, 199.9, 357.8, 210.9, "63.63"),
        _word(423.2, 199.9, 428.8, 210.9, "0"),
        _word(463.5, 199.9, 469.1, 210.9, "0"),
        _word(87.3, 214.5, 98.5, 225.5, "26"),
        _word(145.2, 214.5, 156.4, 225.5, "19"),
        _word(208.9, 214.5, 214.4, 225.5, "0"),
        _word(262.9, 214.5, 268.4, 225.5, "0"),
        _word(312.8, 214.5, 318.4, 225.5, "7"),
        _word(327.0, 214.5, 357.8, 225.5, "141.17"),
        _word(406.3, 214.5, 428.8, 225.5, "1007"),
        _word(460.2, 214.5, 469.1, 225.5, "-1"),
        _word(76.0, 229.0, 100.0, 240.0, "2909"),
        _word(140.0, 229.0, 158.0, 240.0, "134"),
        _word(208.9, 229.0, 214.4, 240.0, "0"),
        _word(262.9, 229.0, 268.4, 240.0, "0"),
        _word(296.0, 229.0, 320.0, 240.0, "2775"),
        _word(333.0, 229.0, 365.0, 240.0, "148.6"),
        _word(406.0, 229.0, 430.0, 240.0, "2108"),
        _word(464.0, 229.0, 472.0, 240.0, "0"),
        _word(76.0, 635.6, 100.0, 646.6, "5354"),
        _word(140.0, 635.6, 158.0, 646.6, "795"),
        _word(208.9, 635.6, 214.4, 646.6, "0"),
        _word(262.9, 635.6, 268.4, 646.6, "0"),
        _word(296.0, 635.6, 320.0, 646.6, "4559"),
        _word(352.0, 635.6, 360.0, 646.6, "0"),
        _word(401.0, 635.6, 430.0, 646.6, "12024"),
        _word(455.0, 635.6, 475.0, 646.6, "-55"),
        _word(76.0, 650.0, 100.0, 661.0, "760308"),
        _word(140.0, 650.0, 158.0, 661.0, "103485"),
        _word(208.9, 650.0, 214.4, 661.0, "0"),
        _word(262.9, 650.0, 268.4, 661.0, "0"),
        _word(296.0, 650.0, 320.0, 661.0, "656826"),
        _word(352.0, 650.0, 360.0, 661.0, "0"),
        _word(401.0, 650.0, 430.0, 661.0, "1880832.59"),
        _word(455.0, 650.0, 475.0, 661.0, "-55"),
    ]
    return _FitzWordPage(words, 612.0, 792.0)


class SplitColumnPagesTests(unittest.TestCase):
    def test_stitches_left_names_with_right_closing(self):
        with patch.dict(os.environ, {"STOCK_PDF_SPLIT_COLUMN_PAGES": "true"}):
            built = from_pdf_words([_left_page(), _right_page()])
        self.assertEqual(len(built.get("page_results") or []), 1)
        page = built["page_results"][0]
        self.assertEqual(page.get("split_column_pages"), [0, 1])
        ready = resolver_ready(page.get("header_cells") or [])
        self.assertTrue(ready.get("resolved"))
        canons = {c.get("canonical") for c in ready.get("columns") or []}
        self.assertIn("product_name", canons)
        self.assertIn("opening_qty", canons)
        self.assertIn("sales_qty", canons)
        self.assertIn("closing_qty", canons)
        items = build_items_from_pdf_page_results(built["page_results"])
        names = [item["product_name"] for item in items]
        self.assertEqual(len(items), 3)
        self.assertTrue(any("AACTARIL" in n for n in names))
        self.assertTrue(any("ABANA" in n for n in names))
        self.assertTrue(any("LIV" in n for n in names))
        aact = next(item for item in items if "AACTARIL" in item["product_name"])
        self.assertEqual(aact["opening_qty"], 51.0)
        self.assertEqual(aact["receipts_qty"], 0.0)
        self.assertEqual(aact["sales_qty"], 10.0)
        self.assertEqual(aact["closing_qty"], 41.0)
        liv = next(item for item in items if "LIV" in item["product_name"])
        self.assertEqual(liv["opening_qty"], 409.0)
        self.assertEqual(liv["receipts_qty"], 2500.0)
        self.assertEqual(liv["sales_qty"], 134.0)
        self.assertEqual(liv["closing_qty"], 2775.0)

    def test_printed_quantity_footer_totals(self):
        with patch.dict(
            os.environ,
            {
                "STOCK_PDF_SPLIT_COLUMN_PAGES": "true",
                "STOCK_PDF_WORDS_FOOTER_TOTALS": "true",
            },
        ):
            built = from_pdf_words([_left_page(), _right_page()])
            footer = _footer_totals_from_page_results(built["page_results"])
        self.assertEqual(footer.get("opening_qty"), 2798.0)
        self.assertEqual(footer.get("receipts_qty"), 2550.0)
        self.assertEqual(footer.get("sales_qty"), 795.0)
        self.assertEqual(footer.get("closing_qty"), 4559.0)
        self.assertEqual(footer.get("opening_value"), 374832.0)
        self.assertEqual(footer.get("receipts_value"), 384767.0)
        self.assertEqual(footer.get("sales_value"), 103485.0)
        self.assertEqual(footer.get("closing_value"), 656826.0)

    def test_flag_off_does_not_stitch(self):
        with patch.dict(os.environ, {"STOCK_PDF_SPLIT_COLUMN_PAGES": "false"}):
            built = from_pdf_words([_left_page(), _right_page()])
        self.assertEqual(len(built.get("page_results") or []), 2)
