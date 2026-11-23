"""Cl.Stock + split 'Stock Value' header must keep printed closing qty."""

from __future__ import annotations

import unittest

from services.stock_header_resolver import assign_cells
from services.stock_native_tables import (
    _FitzWordPage,
    from_pdf_words,
    pdf_built_has_mashed_headers,
    resolver_ready,
)


def _word(x0, top, x1, bottom, text):
    return {"x0": x0, "top": top, "x1": x1, "bottom": bottom, "text": text}


class StockValueHeaderGlueTests(unittest.TestCase):
    def test_glues_stock_value_and_reads_cl_stock(self):
        words = [
            _word(30.0, 120.5, 52.8, 132.0, "SlNo"),
            _word(72.0, 120.5, 109.8, 132.0, "Product"),
            _word(112.6, 120.5, 139.8, 132.0, "Name"),
            _word(243.0, 120.5, 266.4, 132.0, "Pack"),
            _word(293.0, 120.5, 309.7, 132.0, "Op."),
            _word(312.4, 120.5, 339.7, 132.0, "Stock"),
            _word(363.0, 120.5, 382.4, 132.0, "Pur."),
            _word(385.2, 120.5, 401.9, 132.0, "Qty"),
            _word(433.0, 120.5, 454.7, 132.0, "BLM"),
            _word(457.4, 120.5, 474.1, 132.0, "Qty"),
            _word(503.0, 120.5, 517.4, 132.0, "LM"),
            _word(520.2, 120.5, 536.9, 132.0, "Qty"),
            _word(573.0, 120.5, 599.1, 132.0, "Sales"),
            _word(601.9, 120.5, 618.6, 132.0, "Qty"),
            _word(643.0, 120.5, 660.8, 132.0, "Sal."),
            _word(663.6, 120.5, 683.0, 132.0, "Amt"),
            _word(743.0, 120.5, 783.0, 132.0, "Cl.Stock"),
            _word(813.0, 120.5, 840.2, 132.0, "Stock"),
            _word(843.0, 120.5, 869.7, 132.0, "Value"),
            _word(30.0, 165.5, 35.6, 177.0, "2"),
            _word(72.0, 165.5, 105.9, 177.0, "ABANA"),
            _word(108.7, 165.5, 128.1, 177.0, "TAB"),
            _word(243.0, 165.5, 254.1, 177.0, "60"),
            _word(256.9, 165.5, 263.6, 177.0, "S"),
            _word(336.3, 165.5, 353.0, 177.0, "119"),
            _word(406.3, 165.5, 423.0, 177.0, "100"),
            _word(481.9, 165.5, 493.0, 177.0, "99"),
            _word(546.3, 165.5, 563.0, 177.0, "101"),
            _word(616.3, 165.5, 633.0, 177.0, "162"),
            _word(691.3, 165.5, 733.0, 177.0, "23962.50"),
            _word(791.9, 165.5, 803.0, 177.0, "57"),
            _word(866.9, 165.5, 903.0, 177.0, "8470.20"),
        ]
        built = from_pdf_words(_FitzWordPage(words, 1169.0, 827.0))
        headers = built.get("header_cells") or []
        texts = [str(h.get("text") or "") for h in headers]
        self.assertTrue(any("Cl.Stock" in t for t in texts))
        self.assertTrue(any(t.replace(" ", "").lower() == "stockvalue" for t in texts))
        ready = resolver_ready(headers)
        by = {c.get("canonical"): c.get("header_text") or c.get("text") for c in ready.get("columns") or []}
        self.assertEqual(by.get("closing_qty"), "Cl.Stock")
        self.assertEqual(normalize_join(by.get("closing_value")), "stockvalue")
        rows = built.get("data_rows") or []
        self.assertTrue(rows)
        fields = assign_cells(rows[0], ready.get("columns") or []).get("fields") or {}
        self.assertEqual(fields.get("opening_qty"), "119")
        self.assertEqual(fields.get("purchase_qty"), "100")
        self.assertEqual(fields.get("sales_qty"), "162")
        self.assertEqual(fields.get("closing_qty"), "57")
        self.assertEqual(fields.get("closing_value"), "8470.20")

    def test_unglues_qtylm_freesales_qtyclstock(self):
        words = [
            _word(30.0, 124.3, 67.7, 135.5, "Product"),
            _word(70.5, 124.3, 97.7, 135.5, "Name"),
            _word(192.0, 124.3, 215.3, 135.5, "Pack"),
            _word(228.0, 124.3, 249.7, 135.5, "BLM"),
            _word(252.4, 124.3, 280.4, 135.5, "QtyLM"),
            _word(283.2, 124.3, 299.9, 135.5, "Qty"),
            _word(304.0, 124.3, 320.6, 135.5, "Op."),
            _word(323.4, 124.3, 350.6, 135.5, "Stock"),
            _word(341.0, 124.3, 360.4, 135.5, "Pur."),
            _word(363.2, 124.3, 399.4, 135.5, "QtyPur."),
            _word(402.2, 124.3, 443.1, 135.5, "FreeSales"),
            _word(445.9, 124.3, 485.1, 135.5, "QtySales"),
            _word(487.9, 124.3, 514.9, 135.5, "FreeSR"),
            _word(517.7, 124.3, 547.9, 135.5, "QtySR"),
            _word(550.6, 124.3, 581.1, 135.5, "FreeAdj"),
            _word(583.9, 124.3, 637.0, 135.5, "QtyCl.Stock"),
            _word(641.0, 124.3, 668.2, 135.5, "Stock"),
            _word(671.0, 124.3, 697.6, 135.5, "Value"),
            _word(706.0, 124.3, 723.8, 135.5, "Sal."),
            _word(726.5, 124.3, 741.5, 135.5, "Val"),
            _word(30.0, 149.3, 82.2, 160.5, "BONNISAN"),
            _word(85.0, 149.3, 120.5, 160.5, "DROPS"),
            _word(192.0, 149.3, 203.1, 160.5, "30"),
            _word(205.9, 149.3, 219.8, 160.5, "ML"),
            _word(244.9, 149.3, 256.0, 160.5, "19"),
            _word(282.9, 149.3, 294.0, 160.5, "12"),
            _word(319.9, 149.3, 331.0, 160.5, "33"),
            _word(437.9, 149.3, 449.0, 160.5, "10"),
            _word(619.9, 149.3, 631.0, 160.5, "23"),
            _word(659.9, 149.3, 696.0, 160.5, "1522.37"),
            _word(741.4, 149.3, 772.0, 160.5, "746.60"),
            _word(30.0, 169.3, 82.2, 180.5, "BONNISAN"),
            _word(85.0, 169.3, 101.0, 180.5, "LIQ"),
            _word(103.8, 169.3, 134.4, 180.5, "100ML"),
            _word(192.0, 169.3, 208.7, 180.5, "120"),
            _word(211.4, 169.3, 225.3, 180.5, "ML"),
            _word(244.9, 169.3, 256.0, 180.5, "95"),
            _word(277.3, 169.3, 294.0, 180.5, "104"),
            _word(319.9, 169.3, 331.0, 180.5, "81"),
            _word(358.9, 169.3, 370.0, 180.5, "56"),
            _word(437.9, 169.3, 449.0, 180.5, "76"),
            _word(619.9, 169.3, 631.0, 180.5, "61"),
            _word(659.9, 169.3, 696.0, 180.5, "3666.10"),
            _word(735.9, 169.3, 772.0, 180.5, "5152.80"),
            _word(30.0, 189.3, 82.2, 200.5, "BONNISAN"),
            _word(85.0, 189.3, 98.3, 200.5, "SY"),
            _word(101.0, 189.3, 131.6, 200.5, "200ML"),
            _word(192.0, 189.3, 222.6, 200.5, "200ML"),
            _word(244.9, 189.3, 256.0, 200.5, "30"),
            _word(282.9, 189.3, 294.0, 200.5, "41"),
            _word(319.9, 189.3, 331.0, 200.5, "13"),
            _word(358.9, 189.3, 370.0, 200.5, "56"),
            _word(437.9, 189.3, 449.0, 200.5, "36"),
            _word(619.9, 189.3, 631.0, 200.5, "33"),
            _word(659.9, 189.3, 696.0, 200.5, "3343.23"),
            _word(735.9, 189.3, 772.0, 200.5, "4114.08"),
        ]
        built = from_pdf_words(_FitzWordPage(words, 827.0, 1174.0))
        texts = [str(h.get("text") or "") for h in (built.get("header_cells") or [])]
        blob = " ".join(texts).lower()
        self.assertIn("blm", blob)
        self.assertIn("lm", blob)
        self.assertTrue(any("op" in t.lower() and "stock" in t.lower() for t in texts))
        self.assertTrue(any("cl.stock" in t.lower() for t in texts))
        ready = resolver_ready(built.get("header_cells") or [])
        by = {
            c.get("canonical"): c.get("header_text") or c.get("text")
            for c in (ready.get("columns") or [])
            if c.get("canonical") not in (None, "ignore")
        }
        self.assertEqual(by.get("opening_qty"), "Op. Stock")
        self.assertIn("cl.stock", str(by.get("closing_qty") or "").lower())
        self.assertEqual(by.get("closing_value"), "Value")
        from services.stock_native_resolver import _items_from_resolved_word_columns

        parsed = _items_from_resolved_word_columns(built.get("page_results") or [])
        items = (parsed or {}).get("line_items") or []
        drops = next(i for i in items if "BONNISAN DROPS" in str(i.get("product_name") or ""))
        self.assertEqual(drops.get("opening_qty"), 33.0)
        self.assertEqual(drops.get("sales_qty"), 10.0)
        self.assertEqual(drops.get("closing_qty"), 23.0)
        self.assertEqual(drops.get("closing_value"), 1522.37)
        self.assertEqual(drops.get("sales_value"), 746.60)
        liq = next(i for i in items if "BONNISAN LIQ" in str(i.get("product_name") or ""))
        self.assertEqual(liq.get("opening_qty"), 81.0)
        self.assertEqual(liq.get("receipts_qty"), 56.0)
        self.assertEqual(liq.get("sales_qty"), 76.0)
        self.assertEqual(liq.get("closing_qty"), 61.0)

    def test_mashed_plumber_headers_trigger_fitz_fallback(self):
        mashed = {
            "page_results": [
                {
                    "header_cells": [
                        {"text": "Product Name"},
                        {"text": "Op."},
                        {"text": "StocPkur. Qty"},
                        {"text": "FreSealesQty"},
                        {"text": "Cl.Stock"},
                    ]
                }
            ]
        }
        clean = {
            "page_results": [
                {
                    "header_cells": [
                        {"text": "Product Name"},
                        {"text": "Op. Stock"},
                        {"text": "Sales Qty"},
                        {"text": "Cl.Stock"},
                    ]
                }
            ]
        }
        self.assertTrue(pdf_built_has_mashed_headers(mashed))
        self.assertFalse(pdf_built_has_mashed_headers(clean))


def normalize_join(text):
    return "".join(str(text or "").lower().split())


class PrintedZeroAndShortQtyTests(unittest.TestCase):
    def test_keeps_all_zero_row_and_one_digit_opening(self):
        from services.stock_native_resolver import _items_from_resolved_word_columns

        header = [
            {"text": "Product Name", "x_center": 0.10, "col_index": 0},
            {"text": "Op. Stock", "x_center": 0.271, "col_index": 1},
            {"text": "Pur. Qty", "x_center": 0.327, "col_index": 2},
            {"text": "Sales Qty", "x_center": 0.510, "col_index": 3},
            {"text": "Cl.Stock", "x_center": 0.653, "col_index": 4},
            {"text": "Stock Value", "x_center": 0.720, "col_index": 5},
        ]
        zero_row = [
            {"text": "AACTARIL SOAP", "x": 0.10},
            {"text": "0", "x": 0.300},
            {"text": "0", "x": 0.359},
            {"text": "0", "x": 0.539},
            {"text": "0", "x": 0.680},
            {"text": "0", "x": 0.770},
        ]
        short_open = [
            {"text": "DIABECON DS TAB", "x": 0.10},
            {"text": "1", "x": 0.300},
            {"text": "100", "x": 0.355},
            {"text": "100", "x": 0.534},
            {"text": "1", "x": 0.685},
            {"text": "179.02", "x": 0.759},
        ]
        third = [
            {"text": "ABANA TAB", "x": 0.10},
            {"text": "119", "x": 0.295},
            {"text": "100", "x": 0.355},
            {"text": "162", "x": 0.534},
            {"text": "57", "x": 0.680},
            {"text": "8470.20", "x": 0.770},
        ]
        parsed = _items_from_resolved_word_columns(
            [{"header_cells": header, "data_rows": [zero_row, short_open, third]}]
        )
        items = (parsed or {}).get("line_items") or []
        names = [str(i.get("product_name") or "") for i in items]
        self.assertTrue(any("AACTARIL" in n for n in names))
        self.assertTrue(any("DIABECON DS" in n for n in names))
        by = {n: i for n, i in zip(names, items)}
        zero = next(i for n, i in by.items() if "AACTARIL" in n)
        self.assertEqual(zero.get("opening_qty"), 0.0)
        self.assertEqual(zero.get("closing_qty"), 0.0)
        ds = next(i for n, i in by.items() if "DIABECON DS" in n)
        self.assertEqual(ds.get("opening_qty"), 1.0)
        self.assertEqual(ds.get("receipts_qty"), 100.0)
        self.assertEqual(ds.get("sales_qty"), 100.0)
        self.assertEqual(ds.get("closing_qty"), 1.0)


class ClStockBoxPackClusterTests(unittest.TestCase):
    def test_closing_qty_not_glued_to_box_pack(self):
        """Right-aligned Cl.Stock qty must not merge with the next Box Pack token."""
        words = [
            _word(30.0, 119.3, 52.8, 130.5, "SlNo"),
            _word(70.0, 119.3, 107.7, 130.5, "Product"),
            _word(110.5, 119.3, 137.7, 130.5, "Name"),
            _word(299.0, 119.3, 322.3, 130.5, "Pack"),
            _word(430.0, 119.3, 446.6, 130.5, "Op."),
            _word(449.4, 119.3, 476.6, 130.5, "Stock"),
            _word(489.0, 119.3, 508.4, 130.5, "Pur."),
            _word(511.2, 119.3, 546.4, 130.5, "Qty"),
            _word(549.2, 119.3, 570.3, 130.5, "Free"),
            _word(582.0, 119.3, 608.1, 130.5, "Sales"),
            _word(610.9, 119.3, 651.1, 130.5, "Qty"),
            _word(653.9, 119.3, 675.0, 130.5, "Free"),
            _word(700.0, 119.3, 740.0, 130.5, "Cl.Stock"),
            _word(759.0, 119.3, 777.9, 130.5, "Box"),
            _word(780.6, 119.3, 804.0, 130.5, "Pack"),
            _word(976.0, 119.3, 1003.2, 130.5, "Stock"),
            _word(1006.0, 119.3, 1032.6, 130.5, "Value"),
            _word(30.0, 144.3, 35.6, 155.5, "1"),
            _word(70.0, 144.3, 103.9, 155.5, "ABANA"),
            _word(106.6, 144.3, 126.1, 155.5, "TAB"),
            _word(299.0, 144.3, 318.7, 155.5, "60'S"),
            _word(462.3, 144.3, 479.0, 155.5, "108"),
            _word(511.4, 144.3, 517.0, 155.5, "0"),
            _word(566.4, 144.3, 572.0, 155.5, "0"),
            _word(603.9, 144.3, 615.0, 155.5, "19"),
            _word(684.4, 144.3, 690.0, 155.5, "0"),
            _word(737.9, 144.3, 749.0, 155.5, "89"),
            _word(759.0, 144.3, 800.4, 155.5, "100x60'S"),
            _word(992.3, 144.3, 1034.0, 155.5, "13225.40"),
            _word(30.0, 184.3, 35.6, 195.5, "3"),
            _word(70.0, 184.3, 122.0, 195.5, "BONNISAN"),
            _word(125.0, 184.3, 155.0, 195.5, "DROPS"),
            _word(299.0, 184.3, 310.1, 195.5, "30"),
            _word(312.9, 184.3, 326.8, 195.5, "ML"),
            _word(462.3, 184.3, 479.0, 195.5, "101"),
            _word(500.0, 184.3, 517.0, 195.5, "200"),
            _word(566.4, 184.3, 572.0, 195.5, "0"),
            _word(603.9, 184.3, 615.0, 195.5, "91"),
            _word(684.4, 184.3, 690.0, 195.5, "0"),
            _word(731.9, 184.3, 749.0, 195.5, "210"),
            _word(759.0, 184.3, 780.0, 195.5, "1x30"),
            _word(783.0, 184.3, 800.0, 195.5, "ML"),
            _word(992.3, 184.3, 1034.0, 195.5, "13899.90"),
            _word(30.0, 204.3, 35.6, 215.5, "2"),
            _word(70.0, 204.3, 200.0, 215.5, "BLEMINOR-CREAM"),
            _word(299.0, 204.3, 310.1, 215.5, "30"),
            _word(312.9, 204.3, 326.8, 215.5, "ML"),
            _word(473.4, 204.3, 479.0, 215.5, "0"),
            _word(511.4, 204.3, 517.0, 215.5, "0"),
            _word(566.4, 204.3, 572.0, 215.5, "0"),
            _word(609.4, 204.3, 615.0, 215.5, "0"),
            _word(684.4, 204.3, 690.0, 215.5, "0"),
            _word(743.4, 204.3, 749.0, 215.5, "0"),
            _word(1028.4, 204.3, 1034.0, 215.5, "0"),
        ]
        built = from_pdf_words(_FitzWordPage(words, 1169.0, 827.0))
        tokens0 = [
            str(t.get("text") or "")
            for t in ((built.get("data_rows") or [[]])[0] or [])
        ]
        self.assertIn("89", tokens0)
        self.assertTrue(any("100x60" in t for t in tokens0))
        self.assertFalse(any("89" in t and "100x60" in t for t in tokens0))
        ready = resolver_ready(built.get("header_cells") or [])
        by = {
            c.get("canonical"): c.get("header_text") or c.get("text")
            for c in (ready.get("columns") or [])
            if c.get("canonical") not in (None, "ignore")
        }
        self.assertIn("cl.stock", str(by.get("closing_qty") or "").lower())
        fields = assign_cells(
            (built.get("data_rows") or [[]])[0],
            ready.get("columns") or [],
        ).get("fields") or {}
        self.assertEqual(fields.get("opening_qty"), "108")
        self.assertEqual(fields.get("sales_qty"), "19")
        self.assertEqual(fields.get("closing_qty"), "89")
        from services.stock_native_resolver import _items_from_resolved_word_columns

        parsed = _items_from_resolved_word_columns(built.get("page_results") or [])
        items = (parsed or {}).get("line_items") or []
        abana = next(i for i in items if "ABANA" in str(i.get("product_name") or ""))
        self.assertEqual(abana.get("opening_qty"), 108.0)
        self.assertEqual(abana.get("sales_qty"), 19.0)
        self.assertEqual(abana.get("closing_qty"), 89.0)
        drops = next(i for i in items if "BONNISAN" in str(i.get("product_name") or ""))
        self.assertEqual(drops.get("opening_qty"), 101.0)
        self.assertEqual(drops.get("receipts_qty"), 200.0)
        self.assertEqual(drops.get("sales_qty"), 91.0)
        self.assertEqual(drops.get("closing_qty"), 210.0)


class PdfWordsLetterheadMetaTests(unittest.TestCase):
    def test_banner_company_and_printed_address(self):
        from services.stock_native_resolver import _apply_pdf_words_letterhead_meta

        result = {
            "stockist_name": "KASIVISWANATHAN MEDICAL AGENCIES PRIVATE LIMITED",
            "company_name": "KASIVISWANATHAN MEDICAL AGENCIES PRIVATE LIMITED",
            "stockist_address": None,
        }
        peek = (
            "KASIVISWANATHAN MEDICAL AGENCIES PRIVATE LIMITED\n"
            "Stock And Sales Statement - For the Period 01/09/2026 To 30/09/2026 Page No :1\n"
            "HIMALAYA\n"
            "SlNo Product Name Pack Op. Stock\n"
        )
        self.assertTrue(_apply_pdf_words_letterhead_meta(result, peek))
        self.assertEqual(result["company_name"], "HIMALAYA")
        self.assertIsNone(result["stockist_address"])

    def test_gstin_address_line_when_printed(self):
        from services.stock_native_resolver import _apply_pdf_words_letterhead_meta

        result = {
            "stockist_name": "KASIVISWANATHAN MEDICAL AGENCIES PRIVATE LIMITED",
            "company_name": None,
            "stockist_address": None,
        }
        peek = (
            "KASIVISWANATHAN MEDICAL AGENCIES PRIVATE LIMITED\n"
            "Main Road, Puttur GSTIN: 29AAAAA0000A1Z5\n"
            "Stock And Sales Statement\n"
            "HIMALAYA\n"
            "Product Name Pack\n"
        )
        _apply_pdf_words_letterhead_meta(result, peek)
        self.assertEqual(result["company_name"], "HIMALAYA")
        self.assertIn("Puttur", result["stockist_address"] or "")
        self.assertIn("GSTIN", result["stockist_address"] or "")
