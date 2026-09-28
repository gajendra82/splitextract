"""ITEM/PACK STOCK & SALES with FREE after PURCHASE/P.RETURN/SALE/S.RETURN.

Zero-quantity source rows stay. Distinct from item_pack_sreturn_others
(no SUBTOTAL; FREE columns present). Generic Excel still skips all-dash rows.
"""

import io
import unittest

from openpyxl import Workbook

from services.sales_statement_extractor import extract_sales_statement


def _xlsx(rows):
    book = Workbook()
    sheet = book.active
    sheet.title = "Stock_And_Sales_Report"
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


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


class TestItemPackFreePreturnSreturn(unittest.TestCase):
    def test_keeps_zero_rows_and_maps_free_columns(self):
        payload = _xlsx(
            [
                ["ROSHNI PHARMACEUTICALS"],
                ["STOCK & SALES"],
                ["Company : HIMALAYA DRUGS-ZEAL"],
                ["From: 01-Aug-26  To: 31-Aug-26"],
                _HEADER,
                ["Company : HIMALAYA DRUGS-ZEAL"],
                [
                    "ABANA TAB",
                    "X60",
                    80,
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    80,
                    1730,
                    "-",
                ],
                [
                    "AMALAKI CAP",
                    "X60",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    2461,
                    "-",
                ],
                [
                    "LIV.52 TAB",
                    "X100TAB",
                    "-",
                    200,
                    "-",
                    "-",
                    "-",
                    200,
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    1751,
                    "-",
                ],
                [
                    "PILEX FORTE OINT",
                    "X30GM",
                    "-",
                    100,
                    "-",
                    "-",
                    "-",
                    10,
                    "-",
                    "-",
                    "-",
                    "-",
                    90,
                    3832,
                    "-",
                ],
                ["Total Value (HIMALAYA DRUGS-ZEAL)", "", 1, 2, "", 0, "", 3, "", 0, "", 0, 5],
            ]
        )
        result = extract_sales_statement(payload, "roshni.xlsx")
        extra = result["totals"]["extra"]
        self.assertEqual(extra["extraction_method"], "item_pack_free_preturn_sreturn")
        names = [item["product_name"] for item in result["line_items"]]
        self.assertEqual(
            names,
            ["ABANA TAB", "AMALAKI CAP", "LIV.52 TAB", "PILEX FORTE OINT"],
        )
        self.assertNotIn("Total Value (HIMALAYA DRUGS-ZEAL)", names)
        self.assertTrue(
            all(not str(name).lower().startswith("company :") for name in names)
        )
        self.assertEqual(result.get("company_name"), "HIMALAYA DRUGS-ZEAL")
        self.assertEqual(extra.get("zero_qty_rows"), 1)

        by_name = {item["product_name"]: item for item in result["line_items"]}
        abana = by_name["ABANA TAB"]
        self.assertEqual(abana["opening_qty"], 80)
        self.assertEqual(abana["closing_qty"], 80)
        self.assertEqual(abana["sales_qty"], 0)
        self.assertEqual(abana["receipts_qty"], 0)
        self.assertTrue(abana["extra"]["qty_reconcile_ok"])

        zero = by_name["AMALAKI CAP"]
        for field in (
            "opening_qty",
            "purchase_qty",
            "sales_qty",
            "closing_qty",
            "purchase_return_qty",
            "sales_return_qty",
            "others_out_qty",
            "free_qty",
        ):
            self.assertEqual(zero[field], 0, field)

        liv = by_name["LIV.52 TAB"]
        self.assertEqual(liv["opening_qty"], 0)
        self.assertEqual(liv["purchase_qty"], 200)
        self.assertEqual(liv["receipts_qty"], 200)
        self.assertEqual(liv["sales_qty"], 200)
        self.assertEqual(liv["closing_qty"], 0)
        self.assertTrue(liv["extra"]["qty_reconcile_ok"])

        pilex = by_name["PILEX FORTE OINT"]
        self.assertEqual(pilex["purchase_qty"], 100)
        self.assertEqual(pilex["sales_qty"], 10)
        self.assertEqual(pilex["closing_qty"], 90)
        self.assertTrue(pilex["extra"]["qty_reconcile_ok"])

    def test_does_not_steal_item_pack_sreturn_others(self):
        payload = _xlsx(
            [
                [
                    "ITEM",
                    "PACK",
                    "OPENING",
                    "PURCHASE",
                    "S.RETURN",
                    "OTHERS",
                    "SUBTOTAL",
                    "SALE",
                    "P.RETURN",
                    "OTHERS",
                    "CLOSING",
                    "ITEMCODE",
                ],
                ["ARJUNA TAB", "60TAB", 40, "-", "-", "-", 40, 10, "-", "-", 30, 12878],
                ["BONNISAN DROPS", "30ML", "-", "-", "-", "-", "-", "-", "-", "-", "-", 12879],
            ]
        )
        result = extract_sales_statement(payload, "perfect.xlsx")
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "item_pack_sreturn_others",
        )
        names = [item["product_name"] for item in result["line_items"]]
        self.assertEqual(names, ["ARJUNA TAB", "BONNISAN DROPS"])


if __name__ == "__main__":
    unittest.main()
