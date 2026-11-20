"""STOCK & SALES ANALYSIS photo: SALE qty/value then CLOSING qty/value.

Packing is 75G / 60T / 100ML, not the word Pcs. Sale qty must not land in receipts.
The Word reader that requires Pcs stays on its own rows.
"""

import unittest

from services.sales_statement_extractor import (
    _is_sale_closing_only_header,
    _parse_sale_closing_analysis_lines,
    _parse_sale_closing_pack_lines,
    _refile_sale_qty_held_as_receipt,
    empty_line_item,
    empty_result,
)


PHOTO = """
MAX PLUS
E-10-12, TRIVENI COMPLEX, LAXMI NAGAR, DELHI-110092
Phone : 011-4345 1001
GST NO. : 07AANPJ6710F1ZS
STOCK & SALES ANALYSIS 01-08-2026 - 31-08-2026
HIMALAYA ZEAL
Store : Purchase
ITEM DESCRIPTION          <===SALE===>   <==CLOSING==>
                          QTY. VALUE     QTY. VALUE
AACTARIL SOAP 75G 0 0 0 0
CLARINA FACE WASH 60ML 1 126 6 682
DIAREX TAB 30T 3 322 0 0
LIV 52 SYP 100ML 21 2134 49 4865
LIV 52 SYP 200ML 130 22048 108 17385
LIV 52 SYP.(SUGAR FREE) 100ML 0 0 1 189
PILEX FORTE TAB 30'S 0 0 0 0
RENALKA SYP 100ML 7 475 0 0
TOTAL 291 45351 560 73727
"""

PCS = """
LACHMI DRUG AGENCIES
AMINABAD
STOCK & SALES ANALYSIS (HIMALAYA (ZANDRA)) 01-08-2026 - 31-08-2026
ITEM DESCRIPTION
<===SALE===>
<==CLOSING==>
ARJUNA TAB 1*60TAB Pcs 15 3496 1 208
BONNISAN DROP 30ML Pcs 9 627 87 5744
LIV 52 TAB Pcs 2 100 3 150
TOTAL 26 4223 91 6102
"""

OPENING = """
STOCK & SALES ANALYSIS
ITEM DESCRIPTION
OPENING RECEIPT ISSUE CLOSING
<===SALE===>
<==CLOSING==>
"""


class TestSaleClosingPackPhoto(unittest.TestCase):
    def test_header_is_sale_and_closing_only(self):
        self.assertTrue(_is_sale_closing_only_header(PHOTO))
        self.assertFalse(_is_sale_closing_only_header(OPENING))
        self.assertFalse(_is_sale_closing_only_header(""))

    def test_sale_qty_is_not_stored_as_a_receipt(self):
        result = _parse_sale_closing_pack_lines(PHOTO.splitlines(), "maxplus.jpg")
        self.assertIsNotNone(result)
        self.assertEqual(result["stockist_name"], "MAX PLUS")
        self.assertIn("TRIVENI", result["stockist_address"])
        self.assertEqual(result["company_name"], "HIMALAYA ZEAL")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        by_pack = {
            (item["product_name"], item["packing"]): item
            for item in result["line_items"]
        }
        liv = by_pack[("LIV 52 SYP", "100ML")]
        self.assertEqual(liv["sales_qty"], 21)
        self.assertEqual(liv["sales_value"], 2134)
        self.assertEqual(liv["closing_qty"], 49)
        self.assertEqual(liv["closing_value"], 4865)
        self.assertEqual(liv["receipts_qty"], 0)
        sugar = by_pack[("LIV 52 SYP.(SUGAR FREE)", "100ML")]
        self.assertEqual(sugar["sales_qty"], 0)
        self.assertEqual(sugar["closing_qty"], 1)
        self.assertEqual(by_pack[("LIV 52 SYP", "200ML")]["sales_qty"], 130)
        self.assertEqual(by_pack[("PILEX FORTE TAB", "30'S")]["sales_qty"], 0)
        self.assertEqual(result["totals"]["sales_value"], 45351)
        self.assertEqual(result["totals"]["closing_value"], 73727)
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "sale_closing_pack_photo",
        )
        self.assertIsNone(_parse_sale_closing_analysis_lines(PHOTO.splitlines(), "x.docx"))

    def test_word_pcs_rows_stay_on_the_word_reader(self):
        word = _parse_sale_closing_analysis_lines(PCS.splitlines(), "lachmi.docx")
        self.assertIsNotNone(word)
        self.assertEqual(word["line_items"][0]["sales_qty"], 15)
        self.assertEqual(word["line_items"][0]["receipts_qty"], 0)
        self.assertEqual(
            word["totals"]["extra"]["extraction_method"],
            "sale_closing_analysis_docx",
        )
        self.assertIsNone(_parse_sale_closing_pack_lines(PCS.splitlines(), "lachmi.jpg"))

    def test_vision_receipt_slot_is_moved_back_to_sales(self):
        result = empty_result("maxplus.jpg", "jpg")
        item = empty_line_item()
        item["product_name"] = "LIV 52 SYP"
        item["receipts_qty"] = 21
        item["sales_qty"] = 0
        item["closing_qty"] = 49
        item["extra"] = {"receipts_value": 2134}
        result["line_items"] = [item, item.copy(), item.copy()]
        moved = _refile_sale_qty_held_as_receipt(result)
        self.assertEqual(moved["line_items"][0]["sales_qty"], 21)
        self.assertEqual(moved["line_items"][0]["receipts_qty"], 0)
        self.assertEqual(moved["line_items"][0]["sales_value"], 2134)
        self.assertEqual(moved["line_items"][0]["closing_qty"], 49)
        self.assertEqual(
            moved["totals"]["extra"]["extraction_method"],
            "sale_closing_pack_photo",
        )

    def test_vision_mixup_is_recognized_without_header_ocr(self):
        from services.sales_statement_extractor import _vision_filed_sale_qty_as_receipt

        result = empty_result("maxplus.jpg", "jpg")
        result["report_title"] = "STOCK & SALES ANALYSIS"
        result["company_name"] = "HIMALAYA ZEAL"
        result["line_items"] = []
        for name, pack, receipt, closing in (
            ("AACTARIL SOAP", "75G", 0, 0),
            ("AMALKI TAB", "60T", 0, 0),
            ("DIAREX TAB", "30T", 3, 0),
            ("LIV 52 SYP", "100ML", 21, 49),
            ("OXITARD CAP", "10C", 0, 0),
            ("PILEX FORTE TAB", "30'S", 0, 0),
            ("PURIM TAB", "60T", 0, 8),
        ):
            item = empty_line_item()
            item["product_name"] = name
            item["packing"] = pack
            item["receipts_qty"] = receipt
            item["sales_qty"] = 0
            item["closing_qty"] = closing
            result["line_items"].append(item)
        result["totals"]["extra"]["receipts_qty"] = 291
        self.assertTrue(_vision_filed_sale_qty_as_receipt(result))
        moved = _refile_sale_qty_held_as_receipt(result)
        liv = next(i for i in moved["line_items"] if i["packing"] == "100ML")
        self.assertEqual(liv["sales_qty"], 21)
        self.assertEqual(liv["receipts_qty"], 0)
        self.assertEqual(liv["closing_qty"], 49)
        self.assertEqual(moved["totals"]["extra"]["sales_qty"], 291)
        self.assertEqual(moved["totals"]["extra"]["receipts_qty"], 0)

        already = empty_result("other.jpg", "jpg")
        already["report_title"] = "STOCK & SALES ANALYSIS"
        already["line_items"] = []
        for name, sales in (("ABANA TAB", 4), ("LIV 52", 10), ("CONFIDO", 2)):
            item = empty_line_item()
            item["product_name"] = name
            item["packing"] = "60T"
            item["opening_qty"] = 5
            item["receipts_qty"] = 1
            item["sales_qty"] = sales
            already["line_items"].append(item)
        self.assertFalse(_vision_filed_sale_qty_as_receipt(already))


if __name__ == "__main__":
    unittest.main()
