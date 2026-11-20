"""StockStatement LMS|OP.Stock|Rcpts with Ho Sale / Transit must not shift qtys."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _opstock_rcpts_clstk_colmap,
    _parse_text_lms_opstock_rcpts,
)


FATIMA_LIKE_TEXT = """
Page 1 of 3
FATIMA DRUG LINES
StockStatement Report As On October
Name Code Product Packing LMS OP.Stock Rcpts Sales SL.Value Ho Sale(Incl) Ho Sale Val Free CL.Stk CL.Value Transit Transit.Val
60300 ABANA TAB # 60s 7 57 0 1 167.62 0.00 0.00 0 56 9386.72 0.00
60319 CLARINA ANTI ACNE 30gr 3 54 0 3 438.84 0.00 0.00 0 51 7401.48 0.00
60301 DIABECON DS TAB # 60'S 6 3 50 4 827.12 0.00 0.00 0 49 10639.86 0.00
60305 LIV 52 SYP 100ml 95 65 210 97 10346.02 1.00 106.66 0 178 18919.27 0.00
60308 PILEX TAB # 60s 325 7 500 288 53749.70 0.00 0.00 0 219 40878.54 0.00
"""


class TextLmsOpstockHoSaleTests(unittest.TestCase):
    def test_colmap_includes_ho_sale_and_transit(self):
        headers = [
            "Name",
            "Code",
            "Product",
            "Packing",
            "LMS",
            "OP.Stock",
            "Rcpts",
            "Sales",
            "SL.Value",
            "Ho Sale(Incl)",
            "Ho Sale Val",
            "Free",
            "CL.Stk",
            "CL.Value",
            "Transit",
            "Transit.Val",
        ]
        m = _opstock_rcpts_clstk_colmap(headers)
        self.assertEqual(m["lms"], 4)
        self.assertEqual(m["op"], 5)
        self.assertEqual(m["pur"], 6)
        self.assertEqual(m["sale"], 7)
        self.assertEqual(m["sval"], 8)
        self.assertEqual(m["ho_sale"], 9)
        self.assertEqual(m["ho_val"], 10)
        self.assertEqual(m["free"], 11)
        self.assertEqual(m["bal"], 12)
        self.assertEqual(m["bval"], 13)
        self.assertEqual(m["transit"], 14)
        self.assertEqual(m["transit_val"], 15)

    def test_abana_not_shifted_packing_or_closing_value(self):
        parsed = _parse_text_lms_opstock_rcpts(FATIMA_LIKE_TEXT, "fatima.pdf", "pdf")
        self.assertIsNotNone(parsed)
        items = parsed["line_items"]
        ab = next(i for i in items if "ABANA" in str(i.get("product_name") or "").upper())
        self.assertEqual(ab.get("packing"), "60s")
        self.assertEqual(ab.get("opening_qty"), 57.0)
        self.assertEqual(ab.get("receipts_qty"), 0.0)
        self.assertEqual(ab.get("sales_qty"), 1.0)
        self.assertEqual(ab.get("sales_value"), 167.62)
        self.assertEqual(ab.get("closing_qty"), 56.0)
        self.assertEqual(ab.get("closing_value"), 9386.72)
        self.assertEqual((ab.get("extra") or {}).get("lms"), 7.0)
        # Must NOT put SL.Value / CL.Value into qty fields.
        self.assertNotEqual(ab.get("opening_qty"), 167.62)
        self.assertNotEqual(ab.get("closing_qty"), 9386.72)

    def test_liv52_ho_sale_does_not_shift_closing(self):
        parsed = _parse_text_lms_opstock_rcpts(FATIMA_LIKE_TEXT, "fatima.pdf", "pdf")
        liv = next(
            i
            for i in parsed["line_items"]
            if "LIV 52 SYP" in str(i.get("product_name") or "").upper()
            and str(i.get("packing") or "").upper() == "100ML"
        )
        self.assertEqual(liv.get("opening_qty"), 65.0)
        self.assertEqual(liv.get("receipts_qty"), 210.0)
        self.assertEqual(liv.get("sales_qty"), 97.0)
        self.assertEqual(liv.get("closing_qty"), 178.0)
        self.assertEqual(liv.get("closing_value"), 18919.27)
        self.assertEqual((liv.get("extra") or {}).get("lms"), 95.0)

    def test_multiline_pdf_text_does_not_use_packing_as_opening(self):
        """PDF text extract often prints one cell per line."""
        multiline = """
FATIMA DRUG LINES
StockStatement Report As On October
Name
Code
Product
Packing
LMS
OP.Stock
Rcpts
Sales
SL.Value
Ho Sale(Incl)
Ho Sale Val
Free
CL.Stk
CL.Value
Transit
Transit.Val
60300
5
ABANA TAB #
60s
7
57
0
1
167.62
0.00
0.00
0
56
9386.72
0.00
60305
3
LIV 52 SYP
100ml
95
65
210
97
10346.02
1.00
106.66
0
178
18919.27
0.00
60308
0
PILEX TAB #
60s
325
7
500
288
53749.70
0.00
0.00
0
219
40878.54
0.00
"""
        parsed = _parse_text_lms_opstock_rcpts(multiline, "fatima.pdf", "pdf")
        self.assertIsNotNone(parsed)
        ab = next(i for i in parsed["line_items"] if "ABANA" in str(i.get("product_name") or "").upper())
        self.assertEqual(ab.get("packing"), "60s")
        self.assertEqual(ab.get("opening_qty"), 57.0)
        self.assertEqual(ab.get("receipts_qty"), 0.0)
        self.assertEqual(ab.get("closing_qty"), 56.0)
        self.assertEqual(ab.get("closing_value"), 9386.72)
        self.assertNotEqual(ab.get("opening_qty"), 60.0)
        self.assertNotEqual(ab.get("closing_qty"), 9386.72)


if __name__ == "__main__":
    unittest.main()
