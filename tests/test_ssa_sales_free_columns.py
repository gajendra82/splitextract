"""STOCK & SALES ANALYSIS with SALES QTY and SALES FREE in separate columns.

SALES FREE must not become sales_qty. A zero or dash stays in its column.
"""

import unittest
from pathlib import Path

import fitz

from services.sales_statement_extractor import (
    _apply_stock_identity_validation,
    empty_line_item,
    empty_result,
    extract_sales_statement,
)


_PDF = Path(
    r"C:\Users\Prerana Bhalerao\Downloads\ZA_2026_August"
    r"\0000700904_2026_08_ZA_30_578_05092026041035.PDF"
)
_SCREEN = Path(
    r"C:\Users\Prerana Bhalerao\Downloads\ZA_2026_August"
    r"\0000736667_2026_08_ZA_25_299_07092026182322.jpeg"
)

# SALES sits above SALES FREE, the same way the source form is printed.
_COLS = {
    "opening": 155,
    "purchase": 250,
    "purchase_free": 278,
    "sr": 350,
    "sr_free": 378,
    "other_in": 430,
    "total": 470,
    "sales": 515,
    "sales_free": 545,
    "sample": 600,
    "stock_tf": 650,
    "pr": 720,
    "other_out": 800,
    "closing": 860,
}


def _header_page(page):
    page.insert_text((20, 18), "STOCK & SALES ANALYSIS", fontsize=8)
    for x, text in (
        (20, "ITEM DESCRIPTION"),
        (145, "OPENING STOCK"),
        (235, "PURCHASE"),
        (330, "SALES RETURN"),
        (420, "OTHER"),
        (460, "TOTAL"),
        (530, "SALES"),
        (590, "SAMPLE"),
        (640, "STOCK T/F"),
        (700, "PURCHASE RETURN"),
        (790, "OTHER"),
        (845, "CLOSING STOCK"),
    ):
        page.insert_text((x, 40), text, fontsize=7)
    for x, text in (
        (155, "QTY."),
        (190, "VALUE"),
        (250, "QTY."),
        (278, "FREE"),
        (305, "VALUE"),
        (350, "QTY."),
        (378, "FREE"),
        (400, "VALUE"),
        (430, "QTY."),
        (470, "QTY."),
        (515, "QTY."),
        (545, "FREE"),
        (570, "VALUE"),
        (600, "QTY."),
        (650, "QTY."),
        (680, "VALUE"),
        (720, "QTY."),
        (755, "VALUE"),
        (800, "QTY."),
        (860, "QTY."),
        (895, "VALUE"),
    ):
        page.insert_text((x, 54), text, fontsize=7)


def _qty_row(page, y, name, values):
    page.insert_text((20, y), name, fontsize=6)
    page.insert_text((92.8, y), "200ML", fontsize=6)
    page.insert_text((119.8, y), "PCS", fontsize=6)
    for key, x in _COLS.items():
        page.insert_text((x, y), values[key], fontsize=6)


class TestSsaSalesFreeColumns(unittest.TestCase):
    def test_sales_free_is_not_sales_qty(self):
        doc = fitz.open()
        page = doc.new_page(width=980, height=220)
        _header_page(page)
        _qty_row(
            page,
            80,
            "BRESOL-SYP",
            {
                "opening": "36",
                "purchase": "28",
                "purchase_free": "0",
                "sr": "0",
                "sr_free": "0",
                "other_in": "0",
                "total": "64",
                "sales": "28",
                "sales_free": "1",
                "sample": "0",
                "stock_tf": "0",
                "pr": "0",
                "other_out": "0",
                "closing": "35",
            },
        )
        _qty_row(
            page,
            100,
            "CYSTONE-TAB",
            {
                "opening": "10",
                "purchase": "0",
                "purchase_free": "0",
                "sr": "0",
                "sr_free": "0",
                "other_in": "0",
                "total": "10",
                "sales": "-",
                "sales_free": "4",
                "sample": "0",
                "stock_tf": "0",
                "pr": "0",
                "other_out": "0",
                "closing": "6",
            },
        )
        _qty_row(
            page,
            120,
            "SEPTILIN-TAB",
            {
                "opening": "8",
                "purchase": "0",
                "purchase_free": "-",
                "sr": "0",
                "sr_free": "0",
                "other_in": "0",
                "total": "8",
                "sales": "5",
                "sales_free": "0",
                "sample": "-",
                "stock_tf": "0",
                "pr": "0",
                "other_out": "0",
                "closing": "3",
            },
        )
        data = doc.tobytes()
        doc.close()
        result = extract_sales_statement(data, "ssa-sales-free.pdf")
        self.assertEqual(
            result["totals"]["extra"].get("extraction_method"),
            "ssa_sales_free_columns",
        )
        by_name = {item["product_name"]: item for item in result["line_items"]}

        bresol = by_name["BRESOL-SYP"]
        self.assertEqual(bresol["opening_qty"], 36)
        self.assertEqual(bresol["receipts_qty"], 28)
        self.assertEqual(bresol["sales_qty"], 28)
        self.assertEqual(bresol["closing_qty"], 35)
        self.assertEqual(bresol["extra"]["sales_free"], 1)
        self.assertEqual(bresol["extra"]["purchase_free"], 0)
        self.assertEqual(bresol["extra"]["sample_qty"], 0)
        self.assertEqual(bresol["extra"]["total_stock"], 64)
        self.assertNotEqual(bresol["sales_qty"], bresol["extra"]["sales_free"])

        cystone = by_name["CYSTONE-TAB"]
        self.assertEqual(cystone["sales_qty"], 0)
        self.assertEqual(cystone["extra"]["sales_free"], 4)
        self.assertEqual(cystone["receipts_qty"], 0)
        self.assertEqual(cystone["closing_qty"], 6)
        self.assertNotEqual(cystone["sales_qty"], cystone["extra"]["sales_free"])

        septilin = by_name["SEPTILIN-TAB"]
        self.assertEqual(septilin["opening_qty"], 8)
        self.assertEqual(septilin["receipts_qty"], 0)
        self.assertEqual(septilin["sales_qty"], 5)
        self.assertEqual(septilin["extra"]["sales_free"], 0)
        self.assertEqual(septilin["extra"]["sample_qty"], 0)
        self.assertEqual(septilin["closing_qty"], 3)


class TestRajPharmaSalesFreeSource(unittest.TestCase):
    """Every qty below is the number printed in that column of the source PDF."""

    @classmethod
    def setUpClass(cls):
        if not _PDF.is_file():
            raise unittest.SkipTest(f"missing {_PDF.name}")
        cls.result = extract_sales_statement(_PDF.read_bytes(), _PDF.name)

    def test_method_and_representative_rows(self):
        result = self.result
        self.assertEqual(
            result["totals"]["extra"].get("extraction_method"),
            "ssa_sales_free_columns",
        )
        self.assertEqual(len(result["line_items"]), 36)
        by_name = {item["product_name"]: item for item in result["line_items"]}
        expected = {
            "BRESOL TAB.": (14, 50, 32, 32, 0),
            "CYSTONE FORTE TAB": (0, 56, 22, 34, 0),
            "CYSTONE SYP 200ML.": (0, 56, 31, 25, 0),
            "CYSTONE TAB": (392, 0, 276, 116, 0),
            "HIORA-K TOOTHPASTE 100G": (12, 0, 12, 0, 0),
            "HIORA-K TOOTHPASTE 50GM": (5, 50, 5, 50, 0),
            "LIV 52 DS SYP": (0, 70, 10, 60, 0),
            "LIV 52 DS SYP 200ML": (445, 0, 156, 289, 0),
            "LIV 52 DS TAB.": (0, 1000, 690, 310, 0),
            "LUKOL SYP": (0, 28, 18, 10, 0),
            "MENTAT SYP.": (0, 168, 91, 82, 0),
            "SEPTILIN SYP": (24, 28, 40, 12, 0),
            "SEPTILIN TAB.": (130, 0, 130, 0, 0),
            "EVECARE FORTE LIQ": (0, 28, 0, 28, 0),
        }
        for name, (opening, purchase, sales, closing, sales_free) in expected.items():
            item = by_name[name]
            self.assertEqual(item["opening_qty"], opening, name)
            self.assertEqual(item["receipts_qty"], purchase, name)
            self.assertEqual(item["sales_qty"], sales, name)
            self.assertEqual(item["closing_qty"], closing, name)
            self.assertEqual(item["extra"]["sales_free"], sales_free, name)
        drops = next(
            item
            for item in result["line_items"]
            if item["product_name"] == "LIV 52 DROPS" and str(item["packing"]).startswith("100ML")
        )
        self.assertEqual(drops["opening_qty"], 0)
        self.assertEqual(drops["receipts_qty"], 56)
        self.assertEqual(drops["sales_qty"], 3)
        self.assertEqual(drops["closing_qty"], 53)
        self.assertEqual(drops["extra"]["sales_free"], 0)
        mentat = by_name["MENTAT SYP."]
        self.assertEqual(mentat["extra"]["sr_qty"], 5)
        self.assertNotEqual(mentat["sales_qty"], mentat["extra"]["sr_qty"])
        self.assertNotEqual(mentat["sales_qty"], mentat["extra"]["total_stock"])
        forte = by_name["EVECARE FORTE LIQ"]
        self.assertEqual(forte["sales_qty"], 0)
        self.assertNotEqual(forte["sales_qty"], forte["closing_qty"])
        self.assertEqual(
            sum(item["sales_qty"] for item in result["line_items"]),
            result["totals"]["sales_qty"],
        )


class TestZandraDivisionSalesFreeScreenshot(unittest.TestCase):
    """Page 135 screenshot. SALES QTY is not the SALES FREE cell."""

    @classmethod
    def setUpClass(cls):
        if not _SCREEN.is_file():
            raise unittest.SkipTest(f"missing {_SCREEN.name}")
        cls.result = extract_sales_statement(_SCREEN.read_bytes(), _SCREEN.name)

    def test_sales_qty_comes_from_the_qty_column(self):
        result = self.result
        self.assertEqual(
            result["totals"]["extra"].get("extraction_method"),
            "ssa_sales_free_columns",
        )
        self.assertNotEqual(
            result["totals"]["extra"].get("extraction_method"),
            "ssa_qty_value_vision",
        )
        by_name = {item["product_name"]: item for item in result["line_items"]}
        expected = {
            "BRESOL-SYP 200ML": (36, 28, 28, 1, 35),
            "CYSTONE FORTE TAB": (113, 0, 6, 0, 107),
            "CYSTONE-SYP 200ML": (50, 0, 18, 0, 32),
            "CYSTONE-TAB": (147, 0, 9, 0, 138),
            "EVECARE-SYP 200ML": (35, 0, 6, 0, 29),
            "HIORA-K MOUTH WASH": (21, 0, 2, 0, 19),
            "HIORA-K TOOTHPASTE": (39, 50, 22, 0, 67),
            "LIV.52-DROPS": (81, 0, 42, 0, 39),
            "LIV.52DS-SYP": (253, 0, 28, 0, 225),
            "LIV.52DS-TAB": (48, 100, 61, 0, 87),
            "LUKOL TABLET": (83, 0, 7, 0, 76),
            "MENTAT SYP": (12, 0, 5, 0, 7),
            "SEPTILIN-SYP 200ML": (81, 0, 2, 0, 79),
            "SEPTILIN-TAB": (65, 0, 8, 0, 57),
        }
        for name, (opening, purchase, sales, sales_free, closing) in expected.items():
            item = by_name[name]
            self.assertEqual(item["opening_qty"], opening, name)
            self.assertEqual(item["receipts_qty"], purchase, name)
            self.assertEqual(item["sales_qty"], sales, name)
            self.assertEqual(item["extra"]["sales_free"], sales_free, name)
            self.assertEqual(item["closing_qty"], closing, name)
        bresol = by_name["BRESOL-SYP 200ML"]
        self.assertEqual(bresol["extra"]["total_stock"], 64)
        self.assertNotEqual(bresol["sales_qty"], bresol["extra"]["sales_free"])
        validation = result["totals"]["extra"]["stock_validation"]
        self.assertEqual(validation["total_stock"], 1620)
        self.assertEqual(validation["sales_qty"], 259)
        self.assertEqual(validation["sales_free"], 1)
        self.assertEqual(validation["calculated_closing"], 1360)
        self.assertEqual(validation["extracted_closing"], 1360)
        self.assertEqual(result["totals"]["extra"]["stock_identity_fail_count"], 0)
        self.assertTrue(validation["is_valid"])
        self.assertEqual(bresol["sales_qty"], 28)
        self.assertEqual(bresol["extra"]["sales_free"], 1)
        self.assertEqual(bresol["closing_qty"], 35)


class TestSsaSalesFreeIdentity(unittest.TestCase):
    def test_sales_free_is_deducted_from_total_stock(self):
        result = empty_result("sales-free.pdf", "pdf")
        result["totals"]["extra"]["extraction_method"] = "ssa_sales_free_columns"
        result["totals"]["extra"]["layout"] = "ssa_sales_free_columns"

        def row(name, opening, purchase, total, sales, sales_free, closing, sample=0, transfer=0, pr=0, repl_out=0):
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = opening
            item["receipts_qty"] = purchase
            item["sales_qty"] = sales
            item["closing_qty"] = closing
            item["extra"] = {
                "layout": "ssa_sales_free_columns",
                "total_stock": total,
                "sales_free": sales_free,
                "sample_qty": sample,
                "stock_tf_qty": transfer,
                "pr_qty": pr,
                "repl_other_out": repl_out,
            }
            return item

        bresol = row("BRESOL-SYP 200ML", 36, 28, 64, 28, 1, 35)
        other = row("OTHER", 1260, 296, 1556, 231, 0, 1325)
        sample_row = row("SAMPLE ROW", 0, 0, 10, 3, 1, 4, sample=2)
        result["line_items"] = [bresol, other, sample_row]
        validated = _apply_stock_identity_validation(result)
        extra = validated["totals"]["extra"]
        validation = extra["stock_validation"]
        self.assertEqual(bresol["sales_qty"], 28)
        self.assertEqual(bresol["extra"]["sales_free"], 1)
        self.assertEqual(bresol["closing_qty"], 35)
        self.assertEqual(validation["total_stock"], 1630)
        self.assertEqual(validation["sales_qty"], 262)
        self.assertEqual(validation["sales_free"], 2)
        self.assertEqual(validation["sample_qty"], 2)
        self.assertEqual(validation["calculated_closing"], 1364)
        self.assertEqual(validation["extracted_closing"], 1364)
        self.assertEqual(extra["stock_identity_fail_count"], 0)
        self.assertTrue(validation["is_valid"])
        self.assertTrue(bresol["extra"]["stock_identity_ok"])
        page = empty_result("page.pdf", "pdf")
        page["totals"]["extra"]["extraction_method"] = "ssa_sales_free_columns"
        page["line_items"] = [
            row("PAGE", 1296, 324, 1620, 259, 1, 1360),
        ]
        page_extra = _apply_stock_identity_validation(page)["totals"]["extra"]
        self.assertEqual(page_extra["stock_validation"]["calculated_closing"], 1360)
        self.assertEqual(page_extra["stock_identity_fail_count"], 0)
        self.assertTrue(page_extra["stock_validation"]["is_valid"])
        self.assertEqual(page["line_items"][0]["sales_qty"], 259)
        self.assertEqual(page["line_items"][0]["closing_qty"], 1360)


if __name__ == "__main__":
    unittest.main()
