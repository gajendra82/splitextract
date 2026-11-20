"""ITEM / PACK / OPENING / PURCHASE / FREE / P.RETURN / FREE / SALE / FREE / S.RETURN / FREE / OTHERS / CLOSING.

Product cells are quantities. Statement closing value is the CLOSING cell on
the exact TOTAL VALUE row. Division "Total Value (...)" rows are not used.
Other Excel layouts keep their existing totals.
"""

import io
import os
import unittest

from openpyxl import Workbook

from services.sales_statement_extractor import extract_sales_statement


_SOURCE_XLS = (
    r"C:\Users\adity\Downloads\ZL_2026_August"
    r"\0000734746_2026_08_ZL_30_750_03092026074252.xls"
)

_HEADER = [
    "ITEM",
    "PACK",
    "OPENING",
    "PURCHASE",
    "FREE",
    "P.RETURN",
    "FREE",
    "SALE",
    "FREE",
    "S.RETURN",
    "FREE",
    "OTHERS",
    "CLOSING",
    "ITEMCODE",
    "BRANCH TOTAL",
]


def _xlsx(rows):
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


class TestItemPackFreeReturnTotalValue(unittest.TestCase):
    def test_grand_total_value_closing_not_division_total(self):
        payload = _xlsx(
            [
                ["ZONNE VENTURES PRIVATE LIMITED"],
                ["STOCK & SALES"],
                ["From: 01-Aug-26  To: 25-Aug-26"],
                _HEADER,
                ["Division : 00"],
                [
                    "GASEX SYP GINGEER LEMON",
                    "200ML",
                    "-",
                    20,
                    "-",
                    "-",
                    "-",
                    2,
                    "-",
                    "-",
                    "-",
                    "-",
                    18,
                    16008,
                    "-",
                ],
                [
                    "ASHVAGANDHA TABLET",
                    "60 TAB",
                    58,
                    50,
                    "-",
                    "-",
                    "-",
                    93,
                    "-",
                    1,
                    "-",
                    "-",
                    16,
                    4689,
                    "-",
                ],
                [
                    "OPTHACARE EYE DROP",
                    "10ML",
                    15,
                    22,
                    2,
                    "-",
                    "-",
                    30.26,
                    2.74,
                    "-",
                    "-",
                    "-",
                    6,
                    999,
                    "-",
                ],
                ["BONNISAN DROPS", "30ML", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", 1, "-"],
                [
                    "Total Value (00)",
                    "",
                    1800.0,
                    8641.73,
                    "",
                    0,
                    "",
                    2050.39,
                    "",
                    0,
                    "",
                    0,
                    8391.34,
                ],
                [
                    "TOTAL VALUE",
                    "",
                    268306.71,
                    479442.06,
                    "",
                    0,
                    "",
                    409443.82,
                    "",
                    4819.59,
                    "",
                    0,
                    343124.63,
                ],
            ]
        )
        result = extract_sales_statement(payload, "zonne.xlsx")
        extra = result["totals"]["extra"]
        self.assertEqual(extra["extraction_method"], "item_pack_free_return")
        self.assertEqual(result["totals"]["closing_value"], 343124.63)
        self.assertEqual(result["totals"]["sales_value"], 409443.82)
        self.assertEqual(extra.get("opening_value"), 268306.71)
        self.assertEqual(extra.get("purchase_value"), 479442.06)
        self.assertEqual(extra.get("total_row_source"), "free_return_total_value")
        self.assertNotEqual(result["totals"]["closing_value"], 8391.34)
        self.assertTrue(extra["stock_validation"]["is_valid"])
        names = [item["product_name"] for item in result["line_items"]]
        self.assertEqual(
            names,
            [
                "GASEX SYP GINGEER LEMON",
                "ASHVAGANDHA TABLET",
                "OPTHACARE EYE DROP",
                "BONNISAN DROPS",
            ],
        )
        item = result["line_items"][0]
        self.assertEqual(item["opening_qty"], 0)
        self.assertEqual(item["receipts_qty"], 20)
        self.assertEqual(item["sales_qty"], 2)
        self.assertEqual(item["sales_return_qty"], 0)
        self.assertEqual(item["closing_qty"], 18)
        self.assertEqual(item["product_code"], "16008")
        ashva = result["line_items"][1]
        self.assertEqual(ashva["sales_return_qty"], 1)
        self.assertEqual(ashva["closing_qty"], 16)
        self.assertTrue(ashva["extra"]["stock_identity_ok"])
        eye = result["line_items"][2]
        self.assertEqual(eye["purchase_free_qty"], 2)
        self.assertEqual(eye["sale_free_qty"], 2.74)
        self.assertEqual(eye["sales_qty"], 30.26)
        self.assertEqual(eye["closing_qty"], 6)
        self.assertTrue(eye["extra"]["stock_identity_ok"])
        drops = result["line_items"][3]
        self.assertEqual(drops["opening_qty"], 0)
        self.assertEqual(drops["closing_qty"], 0)
        self.assertEqual(extra["zero_qty_rows"], 1)

    def test_qty_total_on_other_headers_is_not_closing_value(self):
        payload = _xlsx(
            [
                ["SOME MEDICAL AGENCY"],
                ["Item", "Pack", "Opening", "Purchase", "Sale", "Closing"],
                ["LIV 52 TAB", "100 TAB", 10, 0, 4, 6],
                ["TOTAL", "", 10, 0, 4, 6],
            ]
        )
        result = extract_sales_statement(payload, "generic.xlsx")
        self.assertIsNone(result["totals"]["closing_value"])
        self.assertEqual(result["line_items"][0]["closing_qty"], 6)
        self.assertEqual(result["line_items"][0]["closing_value"], 0.0)


@unittest.skipUnless(os.path.exists(_SOURCE_XLS), "source workbook is not on this machine")
class TestZonneVenturesSourceWorkbook(unittest.TestCase):
    def test_source_closing_value(self):
        with open(_SOURCE_XLS, "rb") as handle:
            result = extract_sales_statement(
                handle.read(), os.path.basename(_SOURCE_XLS)
            )
        extra = result["totals"]["extra"]
        self.assertEqual(extra["extraction_method"], "item_pack_free_return")
        self.assertEqual(result["totals"]["closing_value"], 343124.63)
        self.assertEqual(result["totals"]["sales_value"], 409443.82)
        self.assertEqual(extra.get("opening_value"), 268306.71)
        self.assertEqual(extra.get("total_row_source"), "free_return_total_value")
        self.assertEqual(result.get("company_name"), "HIMALAYA DRUG COMPANY")
        self.assertEqual(len(result["line_items"]), 134)
        self.assertEqual(extra["zero_qty_rows"], 60)
        self.assertTrue(extra["stock_validation"]["is_valid"])
        self.assertEqual(extra["stock_identity_fail_count"], 0)
        names = [item["product_name"] for item in result["line_items"]]
        self.assertNotIn("TOTAL VALUE", names)
        self.assertNotIn("Total Value (HIMALAYA ZEAL)", names)
        self.assertIn("GASEX SYRUP LEMON", names)
        gasex = next(
            item
            for item in result["line_items"]
            if item["product_name"] == "GASEX SYP GINGEER LEMON"
        )
        self.assertEqual(gasex["closing_qty"], 18)
        self.assertEqual(gasex["sales_qty"], 2)
        self.assertEqual(gasex["receipts_qty"], 20)
        ashva = next(
            item
            for item in result["line_items"]
            if item["product_name"] == "ASHVAGANDHA TABLET"
        )
        self.assertEqual(ashva["sales_return_qty"], 1)
        self.assertEqual(ashva["closing_qty"], 16)
        eye = next(
            item
            for item in result["line_items"]
            if item["product_name"] == "OPTHACARE EYE DROP"
        )
        self.assertEqual(eye["sales_qty"], 30.26)
        self.assertEqual(eye["purchase_free_qty"], 2)
        self.assertEqual(eye["sale_free_qty"], 2.74)
        self.assertEqual(eye["closing_qty"], 6)
        pilex = next(
            item
            for item in result["line_items"]
            if item["product_name"] == "PILEX TABLET"
        )
        self.assertEqual(pilex["sales_qty"], 87.95)
        self.assertEqual(pilex["sale_free_qty"], 0.05)
        self.assertEqual(pilex["sales_return_qty"], 1.95)
        self.assertEqual(pilex["sales_return_free_qty"], 0.05)
        self.assertEqual(pilex["closing_qty"], 0)
