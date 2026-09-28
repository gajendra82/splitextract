"""ProductName / Pack / Op.stk / Pur / sales / Free / Repl / TotalStock.

Blank quantity cells stay in their own column. Age is not purchase.
The ITEM/PACK/S.RETURN/OTHERS reader is unchanged.
"""

import io
import unittest

import fitz
from openpyxl import Workbook

from services.sales_statement_extractor import extract_sales_statement


def _xlsx(rows):
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _by_name(result):
    return {item["product_name"]: item for item in result["line_items"]}


_HEADER = [
    "ProductName",
    "Pack",
    "Op.stk",
    "Pur",
    "sales",
    "Free",
    "Repl",
    "TotalStock",
    "SaleValue",
    "StockValueatPurchasePrice",
    "Age",
]


class TestZenithOpstkTotalStock(unittest.TestCase):
    def test_blank_cells_do_not_shift_and_age_is_not_purchase(self):
        payload = _xlsx(
            [
                ["HIMALAYA-ZENITH"],
                ["Stock And Sales Report (Month)-08/2026"],
                _HEADER,
                ["CYSTONE SYP 200ML", "200ML", None, None, None, None, None, None, None, None, 10],
                ["MESHASHRINGI TAB", "60'S", 28, None, None, None, None, 28, None, None, None],
                ["SEPTILIN TAB", "60's", 128, None, 3, None, None, 125, None, None, 10],
                ["SHUDDHA GUGGULU", "60'S", None, None, None, None, None, None, None, None, 10],
            ]
        )
        result = extract_sales_statement(payload, "zenith.xlsx")
        extra = result["totals"]["extra"]
        self.assertEqual(extra["extraction_method"], "zenith_opstk_totalstock")
        self.assertEqual(result["company_name"], "HIMALAYA-ZENITH")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        rows = _by_name(result)
        self.assertEqual(list(rows), [
            "CYSTONE SYP 200ML",
            "MESHASHRINGI TAB",
            "SEPTILIN TAB",
            "SHUDDHA GUGGULU",
        ])
        self.assertEqual(
            (
                rows["CYSTONE SYP 200ML"]["opening_qty"],
                rows["CYSTONE SYP 200ML"]["purchase_qty"],
                rows["CYSTONE SYP 200ML"]["sales_qty"],
                rows["CYSTONE SYP 200ML"]["free_qty"],
                rows["CYSTONE SYP 200ML"]["replacement_qty"],
                rows["CYSTONE SYP 200ML"]["closing_qty"],
                rows["CYSTONE SYP 200ML"]["receipts_qty"],
            ),
            (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        )
        self.assertEqual(rows["CYSTONE SYP 200ML"]["extra"]["age"], 10.0)
        mesh = rows["MESHASHRINGI TAB"]
        self.assertEqual(mesh["opening_qty"], 28.0)
        self.assertEqual(mesh["purchase_qty"], 0.0)
        self.assertEqual(mesh["sales_qty"], 0.0)
        self.assertEqual(mesh["closing_qty"], 28.0)
        self.assertEqual(mesh["receipts_qty"], 0.0)
        sept = rows["SEPTILIN TAB"]
        self.assertEqual(sept["opening_qty"], 128.0)
        self.assertEqual(sept["purchase_qty"], 0.0)
        self.assertEqual(sept["sales_qty"], 3.0)
        self.assertEqual(sept["closing_qty"], 125.0)
        self.assertEqual(sept["extra"]["age"], 10.0)
        shud = rows["SHUDDHA GUGGULU"]
        self.assertEqual(shud["opening_qty"], 0.0)
        self.assertEqual(shud["purchase_qty"], 0.0)
        self.assertEqual(shud["sales_qty"], 0.0)
        self.assertEqual(shud["closing_qty"], 0.0)
        self.assertEqual(shud["source_product_name"], "SHUDDHA GUGGULU")
        self.assertEqual(shud["source_packing"], "60'S")
        self.assertEqual(extra["opening_qty"], 156.0)
        self.assertEqual(extra["purchase_qty"], 0.0)
        self.assertEqual(extra["sales_qty"], 3.0)
        self.assertEqual(extra["closing_qty"], 153.0)
        self.assertNotIn("expected_closing", mesh["extra"])

    def test_positioned_pdf_keeps_blank_purchase_column(self):
        doc = fitz.open()
        page = doc.new_page(width=900, height=400)
        columns = [40, 180, 260, 320, 380, 440, 500, 580, 680, 780, 860]
        headers = _HEADER
        for x_pos, label in zip(columns, headers):
            page.insert_text((x_pos, 40), label, fontsize=8)
        page.insert_text((40, 20), "HIMALAYA-ZENITH Stock And Sales Report (Month)-08/2026", fontsize=9)

        def put(y_pos, values):
            for x_pos, value in zip(columns, values):
                if value in (None, ""):
                    continue
                page.insert_text((x_pos, y_pos), str(value), fontsize=8)

        put(80, ["CYSTONE SYP 200ML", "200ML", None, None, None, None, None, None, None, None, 10])
        put(110, ["MESHASHRINGI TAB", "60'S", 28, None, None, None, None, 28, None, None, None])
        put(140, ["SEPTILIN TAB", "60's", 128, None, 3, None, None, 125, None, None, 10])
        put(170, ["SHUDDHA GUGGULU", "60'S", None, None, None, None, None, None, None, None, 10])
        payload = doc.tobytes()
        doc.close()
        result = extract_sales_statement(payload, "zenith.pdf")
        rows = _by_name(result)
        self.assertEqual(result["totals"]["extra"]["extraction_method"], "zenith_opstk_totalstock")
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows["MESHASHRINGI TAB"]["opening_qty"], 28.0)
        self.assertEqual(rows["MESHASHRINGI TAB"]["purchase_qty"], 0.0)
        self.assertEqual(rows["MESHASHRINGI TAB"]["sales_qty"], 0.0)
        self.assertEqual(rows["MESHASHRINGI TAB"]["closing_qty"], 28.0)
        self.assertEqual(rows["SEPTILIN TAB"]["opening_qty"], 128.0)
        self.assertEqual(rows["SEPTILIN TAB"]["purchase_qty"], 0.0)
        self.assertEqual(rows["SEPTILIN TAB"]["sales_qty"], 3.0)
        self.assertEqual(rows["SEPTILIN TAB"]["closing_qty"], 125.0)
        self.assertEqual(rows["CYSTONE SYP 200ML"]["purchase_qty"], 0.0)
        self.assertEqual(rows["SHUDDHA GUGGULU"]["purchase_qty"], 0.0)
        self.assertEqual(rows["CYSTONE SYP 200ML"]["closing_qty"], 0.0)


if __name__ == "__main__":
    unittest.main()
