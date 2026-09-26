"""ITEM DESCRIPTION / PACK / OPENING / RECEIPT / ISSUE / CLOSING / M.EXP.

A bare pack stays packing. Quantity totals are the sum of product rows.
The printed TOTAL row is not used.
"""

import unittest

import fitz

from services.sales_statement_extractor import (
    _a2z_cell_qty,
    _a2z_items_from_ocr_words,
    extract_sales_statement,
)


_STATEMENT = """
SOME MEDICAL AGENCY
STOCK & SALES ANALYSIS 01/08/2026 - 31/08/2026
ITEM DESCRIPTION PACK OPENING RECEIPT ISSUE CLOSING M.EXP
ARJUNA CAP 30 - - - -
TENTEX ROYAL TAB 10 5 - - 5
OTHER PRODUCT 60 3095 262 1463 1894
TOTAL 497975 37368 225587 305892
"""


def _pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)
    y = 40
    for line in text.strip().splitlines():
        page.insert_text((36, y), line, fontsize=9)
        y += 14
    data = doc.tobytes()
    doc.close()
    return data


class TestPackMexpQtyTotals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(_pdf_bytes(_STATEMENT), "pack-mexp.pdf")
        cls.by_name = {
            item["product_name"]: item for item in cls.result["line_items"]
        }

    def test_tentex_pack_is_not_opening(self):
        item = self.by_name["TENTEX ROYAL TAB"]
        self.assertEqual(item.get("product_name"), "TENTEX ROYAL TAB")
        self.assertEqual(item.get("packing"), "10")
        self.assertEqual(item.get("opening_qty"), 5)
        self.assertEqual(item.get("receipts_qty"), 0)
        self.assertEqual(item.get("sales_qty"), 0)
        self.assertEqual(item.get("closing_qty"), 5)

    def test_zero_row_is_kept(self):
        item = self.by_name["ARJUNA CAP"]
        self.assertEqual(item.get("packing"), "30")
        self.assertEqual(
            (
                item.get("opening_qty"),
                item.get("receipts_qty"),
                item.get("sales_qty"),
                item.get("closing_qty"),
            ),
            (0, 0, 0, 0),
        )

    def test_totals_are_product_sums_not_the_printed_total(self):
        totals = self.result["totals"]
        extra = totals["extra"]
        self.assertEqual(extra.get("extraction_method"), "pack_opening_receipt_issue_mexp")
        self.assertEqual(extra.get("total_row_source"), "product_row_sum")
        self.assertEqual(len(self.result["line_items"]), 3)
        self.assertEqual(
            (
                totals.get("opening_qty"),
                totals.get("receipts_qty"),
                totals.get("sales_qty"),
                totals.get("closing_qty"),
            ),
            (3100, 262, 1463, 1899),
        )
        opening = totals["opening_qty"]
        receipt = totals["receipts_qty"]
        issue = totals["sales_qty"]
        closing = totals["closing_qty"]
        self.assertEqual(opening + receipt - issue, closing)
        self.assertNotEqual(totals.get("opening_qty"), 497975)
        self.assertNotEqual(totals.get("receipts_qty"), 37368)
        self.assertNotEqual(totals.get("sales_qty"), 225587)
        self.assertNotEqual(totals.get("closing_qty"), 305892)


class TestTentexColumnPosition(unittest.TestCase):
    """Columns are separate words. Pack 10 must not become opening or sales."""

    def test_positioned_tentex_row(self):
        doc = fitz.open()
        page = doc.new_page(width=620, height=240)
        for x, text in (
            (20, "ITEM"),
            (55, "DESCRIPTION"),
            (175, "PACK"),
            (240, "OPENING"),
            (320, "RECEIPT"),
            (400, "ISSUE"),
            (470, "CLOSING"),
            (540, "M.EXP"),
        ):
            page.insert_text((x, 40), text, fontsize=8)
        for x, text in (
            (20, "TENTEX"),
            (60, "ROYAL"),
            (100, "TAB"),
            (180, "10"),
            (255, "5"),
            (335, "-"),
            (410, "-"),
            (490, "5"),
        ):
            page.insert_text((x, 70), text, fontsize=8)
        data = doc.tobytes()
        doc.close()
        result = extract_sales_statement(data, "tentex-positioned.pdf")
        self.assertEqual(
            result["totals"]["extra"].get("extraction_method"),
            "pack_opening_receipt_issue_mexp",
        )
        item = result["line_items"][0]
        self.assertEqual(item.get("product_name"), "TENTEX ROYAL TAB")
        self.assertEqual(item.get("packing"), "10")
        self.assertEqual(item.get("opening_qty"), 5)
        self.assertEqual(item.get("receipts_qty"), 0)
        self.assertEqual(item.get("sales_qty"), 0)
        self.assertEqual(item.get("closing_qty"), 5)

    def test_missing_dash_glyphs_keep_opening_out_of_sales(self):
        """Dashes are drawn lines, so the text layer is 10, 5, 5. Pack stays pack."""
        doc = fitz.open()
        page = doc.new_page(width=620, height=240)
        for x, text in (
            (20, "ITEM"),
            (55, "DESCRIPTION"),
            (175, "PACK"),
            (240, "OPENING"),
            (320, "RECEIPT"),
            (400, "ISSUE"),
            (470, "CLOSING"),
        ):
            page.insert_text((x, 40), text, fontsize=8)
        for x, text in (
            (20, "TENTEX"),
            (60, "ROYAL"),
            (100, "TAB"),
            (180, "10"),
            (255, "5"),
            (490, "5"),
        ):
            page.insert_text((x, 70), text, fontsize=8)
        data = doc.tobytes()
        doc.close()
        result = extract_sales_statement(data, "tentex-no-dash.pdf")
        item = result["line_items"][0]
        self.assertEqual(item.get("packing"), "10")
        self.assertEqual(item.get("opening_qty"), 5)
        self.assertEqual(item.get("receipts_qty"), 0)
        self.assertEqual(item.get("sales_qty"), 0)
        self.assertEqual(item.get("closing_qty"), 5)


class TestA2zPharmaZandiraTentex(unittest.TestCase):
    """A2Z PHARMA / ZANDIRA grid. Pack is the first number. M.EXP stays expiry."""

    def test_tentex_columns_and_expiry(self):
        statement = """
A2Z PHARMA
STOCK & SALES ANALYSIS
HIMALAYA WELLNESS COMPANY (ZANDIRA)
01-08-2026 to 31-08-2026
ITEM DESCRIPTION | PACK | OPENING | RECEIPT | ISSUE | CLOSING | M.EXP
TENTEX ROYAL TAB | 10 | 5 | - | - | 5 | 11/24
ARJUNA CAP | 30 | - | - | - | - |
"""
        result = extract_sales_statement(_pdf_bytes(statement), "a2z-zandira.pdf")
        item = next(
            row for row in result["line_items"] if row["product_name"] == "TENTEX ROYAL TAB"
        )
        self.assertEqual(item.get("product_name"), "TENTEX ROYAL TAB")
        self.assertEqual(item.get("packing"), "10")
        self.assertEqual(item.get("opening_qty"), 5)
        self.assertEqual(item.get("receipts_qty"), 0)
        self.assertEqual(item.get("sales_qty"), 0)
        self.assertEqual(item.get("closing_qty"), 5)
        extra = item.get("extra") or {}
        self.assertEqual(extra.get("expiry"), "11/24")
        self.assertEqual(extra.get("m_exp"), "11/24")
        self.assertNotEqual(item.get("opening_qty"), 10)
        self.assertNotEqual(item.get("sales_qty"), 5)


def _box(x, y, text, width=None):
    width = width if width is not None else max(12, 8 * len(text))
    return {"x0": x, "y0": y, "x1": x + width, "y1": y + 16, "t": text}


class TestA2zPhotoColumnBoxes(unittest.TestCase):
    """Word boxes measured on the A2Z monitor photo. Pack sits left of OPENING."""

    def test_measured_boxes_do_not_use_pack_as_opening(self):
        words = [
            _box(37, 440, "ITEM", 70),
            _box(127, 440, "DESCRIPTION", 200),
            _box(643, 440, "OPENING", 129),
            _box(829, 440, "RECEIPT", 129),
            _box(1055, 440, "TSSUE", 86),
            _box(1198, 440, "CLOSING", 128),
            _box(1382, 440, "EXP", 58),
            # TAB sits one pixel higher, so a y-order join would put it first.
            _box(80, 1573, "TENTEX", 90),
            _box(203, 1573, "ROYAL", 90),
            _box(324, 1572, "TAB", 50),
            _box(487, 1573, "10", 28),
            _box(761, 1572, "5", 16),
            _box(937, 1583, "-", 12),
            _box(1109, 1583, "-", 12),
            _box(1280, 1572, "5", 16),
            _box(1321, 1572, "11/24", 79),
        ]
        items = _a2z_items_from_ocr_words(words, None)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.get("product_name"), "TENTEX ROYAL TAB")
        self.assertEqual(item.get("packing"), "10")
        self.assertEqual(item.get("opening_qty"), 5)
        self.assertEqual(item.get("receipts_qty"), 0)
        self.assertEqual(item.get("sales_qty"), 0)
        self.assertEqual(item.get("closing_qty"), 5)
        self.assertEqual((item.get("extra") or {}).get("expiry"), "11/24")
        self.assertNotEqual(item.get("opening_qty"), 10)
        self.assertNotEqual(item.get("sales_qty"), 5)

    def test_short_glyph_reread_keeps_letters_at_zero(self):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (80, 40), "white")
        ImageDraw.Draw(image).text((8, 8), "5", fill="black")
        closing = _a2z_cell_qty([_box(4, 4, "S$", 24)], image)
        issue = _a2z_cell_qty([_box(4, 4, "t", 12)], image)
        self.assertEqual(closing, 5)
        self.assertEqual(issue, 0)


if __name__ == "__main__":
    unittest.main()
