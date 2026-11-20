"""PRODUCT / UNIT / OP QNTY / PUR-QNTY / SALE-QNTY / BAL-QNTY photos.

Line columns are quantities. The GRAND TOTAL line is rupees.
SALES & STOCK OF COMPANY sheets stay on their own reader.
"""

import unittest
from pathlib import Path

from PIL import Image, ImageOps

from services.sales_statement_extractor import (
    _blank_op_pur_sale_bal_column_bars,
    _finalize_op_pur_sale_bal_qnty,
    _is_opening_purchase_sale_balance_qty_text,
    _is_op_pur_sale_bal_qnty_text,
    empty_line_item,
    empty_result,
)

HIMALAYA_STOCK = Path(
    r"C:\Users\adity\Downloads\ZL_2026_August"
    r"\0000735032_2026_08_ZL_24_232_03092026035341.jpg"
)


HEADER = (
    "COMPANY STOCK STATEMENT ( SR )\n"
    "FROM 01/08/2026 TO 30/08/2026\n"
    "Company : HIMALAYA WELLNESS COMPANY\n"
    "PRODUCT UNIT OP QNTY PUR-QNTY SALE-QNTY BAL-QNTY\n"
)

OTHER = (
    "SALES & STOCK OF COMPANY HIMALAYA\n"
    "PRODUCT NAME UNIT OPENING PURCHASE SALE BALANCE\n"
)


def _row(name, opening, purchase, sale, closing, sales_value=None):
    item = empty_line_item()
    item["product_name"] = name
    item["opening_qty"] = opening
    item["receipts_qty"] = purchase
    item["sales_qty"] = sale
    item["closing_qty"] = closing
    if sales_value is not None:
        item["sales_value"] = sales_value
        item["extra"] = {"purchase_value": sales_value}
    return item


class TestOpPurSaleBalQnty(unittest.TestCase):
    def test_detector_is_this_header_only(self):
        self.assertTrue(_is_op_pur_sale_bal_qnty_text(HEADER))
        self.assertFalse(_is_op_pur_sale_bal_qnty_text(OTHER))
        self.assertFalse(_is_op_pur_sale_bal_qnty_text(""))
        self.assertFalse(_is_opening_purchase_sale_balance_qty_text(HEADER))

    def test_sale_qty_is_not_money_and_grand_total_is_kept(self):
        result = empty_result("sample.jpg", "jpg")
        result["company_name"] = "HIMALAYA WELLNESS COMPANY"
        result["period_from"] = "2026-08-01"
        result["period_to"] = "2026-08-30"
        result["line_items"] = [
            _row("ABANA TAB", 0, 0, 0, 0, 0),
            _row("CONFIDO TAB", 83, 100, 3, 180, 3),
            _row("EVECARE CAP", 15, 0, 10, 5, 10),
            _row("EVECARE FORTE LIQUID", 135, 0, 3, 132),
            _row("GASEX SYP 200ML", 0, 0, 0, 0),
            _row("LIV 52 100ML SYP", 37, 0, 0, 37),
            _row("LIV.52 DS TAB", 670, 1100, 1604, 166, 1604),
            _row("TENTEX FORTE TAB", 9, 0, 9, 0, 9),
            _row("GRAND TOTAL", 208059.06, 311820.13, 305277.25, 214601.93),
        ]
        result["totals"]["sales_value"] = 305277.25
        result["totals"]["closing_value"] = 214601.93
        result["totals"]["extra"]["opening_value"] = 208059.06
        result["totals"]["extra"]["purchase_value"] = 311820.13
        finished = _finalize_op_pur_sale_bal_qnty(result)
        self.assertIsNotNone(finished)
        names = [item["product_name"] for item in finished["line_items"]]
        self.assertNotIn("GRAND TOTAL", names)
        confido = finished["line_items"][1]
        self.assertEqual(confido["sales_qty"], 3)
        self.assertEqual(confido["opening_qty"], 83)
        self.assertEqual(confido["closing_qty"], 180)
        self.assertIsNone(confido["sales_value"])
        self.assertNotIn("purchase_value", confido.get("extra") or {})
        eve = next(i for i in finished["line_items"] if i["product_name"] == "EVECARE CAP")
        self.assertEqual(eve["opening_qty"], 15)
        self.assertEqual(eve["sales_qty"], 10)
        self.assertEqual(eve["closing_qty"], 5)
        tentex = next(
            i for i in finished["line_items"] if i["product_name"] == "TENTEX FORTE TAB"
        )
        self.assertEqual(tentex["opening_qty"], 9)
        self.assertEqual(tentex["sales_qty"], 9)
        self.assertEqual(tentex["closing_qty"], 0)
        self.assertEqual(finished["totals"]["sales_value"], 305277.25)
        self.assertEqual(finished["totals"]["closing_value"], 214601.93)
        self.assertEqual(finished["totals"]["extra"]["opening_value"], 208059.06)
        self.assertEqual(finished["totals"]["extra"]["purchase_value"], 311820.13)
        self.assertEqual(
            finished["totals"]["extra"]["extraction_method"],
            "op_pur_sale_bal_qnty",
        )

    def test_column_bar_read_as_digit_one_is_removed(self):
        result = empty_result("sample.jpg", "jpg")
        result["line_items"] = [
            _row("ABANA TAB", 1, 1, 1, 0),
            _row("CONFIDO TAB", 831, 1001, 31, 180),
            _row("CYSTONE SYP", 0, 0, 0, 0),
            _row("EVECARE CAP", 151, 0, 101, 5),
            _row("EVECARE FORTE LIQUID", 135, 1, 31, 132),
            _row("GASEX TAB", 37, 0, 0, 37),
            _row("LIV.52 DS TAB", 670, 1100, 1604, 166),
            _row("SEPTILIN TAB", 11, 0, 0, 11),
            _row("LIV 52 SOLD", 11, 0, 11, 0),
            _row("TENTEX FORTE TAB", 91, 1, 91, 0),
        ]
        result["line_items"][1]["packing"] = "160TAB"
        result["line_items"][3]["packing"] = "30CAP1"
        result["line_items"][5]["packing"] = "100ML"
        result["line_items"][6]["packing"] = "1100ML"
        result["line_items"][7]["packing"] = "10CAP"
        finished = _finalize_op_pur_sale_bal_qnty(result)
        by_name = {item["product_name"]: item for item in finished["line_items"]}
        self.assertEqual(
            (
                by_name["CONFIDO TAB"]["opening_qty"],
                by_name["CONFIDO TAB"]["receipts_qty"],
                by_name["CONFIDO TAB"]["sales_qty"],
                by_name["CONFIDO TAB"]["closing_qty"],
            ),
            (83, 100, 3, 180),
        )
        self.assertEqual(by_name["CONFIDO TAB"]["packing"], "60TAB")
        self.assertEqual(
            (
                by_name["EVECARE CAP"]["opening_qty"],
                by_name["EVECARE CAP"]["sales_qty"],
                by_name["EVECARE CAP"]["closing_qty"],
            ),
            (15, 10, 5),
        )
        self.assertEqual(by_name["EVECARE CAP"]["packing"], "30CAP")
        self.assertEqual(by_name["EVECARE FORTE LIQUID"]["opening_qty"], 135)
        self.assertEqual(by_name["EVECARE FORTE LIQUID"]["receipts_qty"], 0)
        self.assertEqual(by_name["EVECARE FORTE LIQUID"]["sales_qty"], 3)
        self.assertEqual(by_name["ABANA TAB"]["opening_qty"], 0)
        self.assertEqual(by_name["ABANA TAB"]["sales_qty"], 0)
        self.assertEqual(by_name["ABANA TAB"]["closing_qty"], 0)
        self.assertEqual(by_name["SEPTILIN TAB"]["opening_qty"], 11)
        self.assertEqual(by_name["SEPTILIN TAB"]["closing_qty"], 11)
        self.assertEqual(by_name["SEPTILIN TAB"]["packing"], "10CAP")
        self.assertEqual(by_name["GASEX TAB"]["packing"], "100ML")
        self.assertEqual(by_name["LIV.52 DS TAB"]["packing"], "100ML")
        self.assertEqual(by_name["LIV.52 DS TAB"]["opening_qty"], 670)
        self.assertEqual(by_name["TENTEX FORTE TAB"]["opening_qty"], 9)
        self.assertEqual(by_name["TENTEX FORTE TAB"]["receipts_qty"], 0)
        self.assertEqual(by_name["TENTEX FORTE TAB"]["sales_qty"], 9)
        self.assertEqual(by_name["LIV 52 SOLD"]["opening_qty"], 11)
        self.assertEqual(by_name["LIV 52 SOLD"]["sales_qty"], 11)

    def test_other_company_stock_sheet_is_not_rewritten(self):
        result = empty_result("other.jpg", "jpg")
        result["line_items"] = [
            _row("LIV 52 DS SYP", 59, 0, 3, 56),
            _row("LIV-52 DS TAB", 20, 100, 48, 72),
        ]
        self.assertIsNone(_finalize_op_pur_sale_bal_qnty(result))


class TestOpPurSaleBalQntySanitize(unittest.TestCase):
    def test_footer_rupees_survive_qty_only_check(self):
        payload_items = []
        for name, opening, purchase, sale, closing in (
            ("ABANA TAB", 0, 0, 0, 0),
            ("CONFIDO TAB", 83, 100, 3, 180),
            ("CYSTONE SYP", 0, 0, 0, 0),
            ("EVECARE CAP", 15, 0, 10, 5),
            ("EVECARE FORTE LIQUID", 135, 0, 3, 132),
            ("GASEX TAB", 37, 0, 0, 37),
            ("LIV.52 DS TAB", 670, 1100, 1604, 166),
            ("TENTEX FORTE TAB", 9, 0, 9, 0),
        ):
            payload_items.append(_row(name, opening, purchase, sale, closing))
        result = empty_result("sample.jpg", "jpg")
        result["line_items"] = payload_items
        result["totals"]["sales_value"] = 305277.25
        result["totals"]["closing_value"] = 214601.93
        result["totals"]["extra"]["opening_value"] = 208059.06
        result["totals"]["extra"]["purchase_value"] = 311820.13
        finished = _finalize_op_pur_sale_bal_qnty(result)
        from services.sales_statement_extractor import _sanitize_statement_financials

        kept = _sanitize_statement_financials(finished)
        self.assertEqual(kept["totals"]["sales_value"], 305277.25)
        self.assertEqual(kept["totals"]["closing_value"], 214601.93)


class TestOpPurSaleBalColumnBars(unittest.TestCase):
    @unittest.skipUnless(HIMALAYA_STOCK.is_file(), "missing Himalaya stock photo")
    def test_column_bar_is_removed_and_digit_one_stays(self):
        image = ImageOps.exif_transpose(Image.open(HIMALAYA_STOCK)).convert("RGB")
        before = image.convert("L").load()
        self.assertLess(before[1275, 760], 80)
        self.assertLess(before[1221, 760], 80)
        self.assertLess(before[1256, 780], 80)
        after = _blank_op_pur_sale_bal_column_bars(image).convert("L").load()
        self.assertEqual(after[1275, 760], 255)
        self.assertLess(after[1221, 760], 80)
        self.assertLess(after[1256, 780], 80)
