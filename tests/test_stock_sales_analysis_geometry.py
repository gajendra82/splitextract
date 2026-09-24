"""STOCK & SALES ANALYSIS column geometry (Himalaya / Busy layout)."""

import unittest

import fitz

from services.sales_statement_extractor import extract_sales_statement


def _put(page, x, y, text):
    page.insert_text((x, y), text, fontsize=8, fontname="helv")


def _build_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=900, height=500)
    _put(page, 40, 30, "STOCK & SALES ANALYSIS (HIMALAYA WELLNESS)")
    headers = [
        (40, "ITEM"),
        (80, "DESCRIPTION"),
        (220, "OPENING"),
        (250, "STOCK"),
        (300, "PURCHASES"),
        (370, "RETURN"),
        (430, "OTHERS"),
        (490, "TOTAL"),
        (530, "STOCK"),
        (580, "SALES"),
        (640, "RETURN"),
        (700, "OTHERS"),
        (760, "CLOSING"),
        (810, "STOCK"),
        (860, "RATE"),
    ]
    for x, text in headers:
        _put(page, x, 70, text)
    for i, x in enumerate((220, 300, 370, 430, 490, 580, 640, 700, 760, 860), start=1):
        _put(page, x, 86, str(i))
    # ABANA: total stock 104 must not become closing; sales others stays 2.
    abana = [
        (40, "ABANA"),
        (80, "TAB"),
        (110, "60TAB"),
        (220, "4"),
        (300, "100"),
        (370, "0"),
        (430, "0"),
        (490, "104"),
        (580, "0"),
        (640, "0"),
        (700, "2"),
        (760, "102"),
        (860, "153.12"),
    ]
    for x, text in abana:
        _put(page, x, 120, text)
    # Wrapped name, blanks in the middle. Next row must not fill those blanks.
    for x, text in ((40, "ASHVAGANDHA"), (100, "TAB")):
        _put(page, x, 145, text)
    for x, text in ((40, "(60TAB)"), (220, "119"), (760, "67")):
        _put(page, x, 158, text)
    for x, text in (
        (40, "BONISAN"),
        (90, "SYP"),
        (130, "200ML"),
        (220, "908"),
        (300, "352"),
        (580, "11"),
        (760, "500"),
    ):
        _put(page, x, 190, text)
    _put(page, 40, 230, "Total Quantity")
    _put(page, 220, 230, "9999")
    _put(page, 40, 250, "Value in Rs.")
    _put(page, 220, 250, "8888")
    _put(page, 40, 270, "Continued ..2")
    _put(page, 40, 290, "ITEM DESCRIPTION")
    data = doc.tobytes()
    doc.close()
    return data


class TestStockSalesAnalysisGeometry(unittest.TestCase):
    def test_column_and_row_association(self):
        result = extract_sales_statement(self.pdf, "0000737290_2026_08_ZA_24_614.PDF")
        names = [i["product_name"] for i in result["line_items"]]
        self.assertNotIn("STOCK & SALES ANALYSIS (HIMALAYA WELLNESS)", names)
        self.assertFalse(any("HIMALAYA" in n for n in names))
        self.assertFalse(any(n.upper().startswith("TOTAL") for n in names))
        self.assertFalse(any("Value in Rs" in n for n in names))
        self.assertFalse(any("Continued" in n for n in names))
        self.assertFalse(any(n.upper().startswith("ITEM") for n in names))

        abana = next(i for i in result["line_items"] if i["product_name"].startswith("ABANA"))
        self.assertEqual(abana["opening_qty"], 4.0)
        self.assertEqual(abana["receipts_qty"], 100.0)
        self.assertEqual(abana["sales_qty"], 0.0)
        self.assertEqual(abana["closing_qty"], 102.0)
        self.assertEqual(abana["extra"]["total_stock"], 104.0)
        self.assertEqual(abana["extra"]["sales_others"], 2.0)
        self.assertEqual(abana["extra"]["unit_rate"], 153.12)
        self.assertNotEqual(abana["closing_qty"], 104.0)

        ash = next(i for i in result["line_items"] if "ASHVAGANDHA" in i["product_name"])
        self.assertIn("60TAB", ash["product_name"].replace(" ", ""))
        self.assertEqual(ash["opening_qty"], 119.0)
        self.assertEqual(ash["closing_qty"], 67.0)
        self.assertNotEqual(ash["opening_qty"], 908.0)
        self.assertNotEqual(ash["receipts_qty"], 352.0)
        self.assertNotEqual(ash["sales_qty"], 11.0)
        self.assertIsNone(ash["extra"]["total_stock"])
        self.assertIsNone(ash["extra"]["sales_others"])

        bon = next(i for i in result["line_items"] if "BONISAN" in i["product_name"])
        self.assertEqual(bon["opening_qty"], 908.0)
        self.assertEqual(bon["receipts_qty"], 352.0)
        self.assertEqual(bon["sales_qty"], 11.0)
        self.assertEqual(bon["closing_qty"], 500.0)

    @classmethod
    def setUpClass(cls):
        cls.pdf = _build_pdf()


if __name__ == "__main__":
    unittest.main()
