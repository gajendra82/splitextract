"""Pharma Hub stock statement: Jun and Jul sit beside Sales.

Sales qty is the Sales column. June and July stay beside it.
Ganesh sheets without Stock-Out / Jun / Jul are unchanged.
"""

import io
import unittest
from pathlib import Path

from PIL import Image

from services.sales_statement_extractor import (
    _finalize_pharma_hub_jun_jul,
    _is_code_item_stock_statement_text,
    _is_pharma_hub_jun_jul_header,
    _jpeg_needs_quarter_turn,
    _pharma_hub_extract_is_usable,
    _pharma_hub_jun_jul_needs_reread,
    _pharma_hub_page_jpeg,
    _pharma_hub_party,
    empty_line_item,
    empty_result,
)


HEADER = """
PHARMA HUB
Stock Statment : HIMALAYA DRUGS (ZEAL)
Aug. 2026
Code Item Description Packing Opening Purchase Stock-Out Jun Jul Sales Stock-In Closing Stock-Value Sales-Value
"""

GANESH = (
    "GANESH AGENCY\n"
    "Stock Statment : HIMALAYA ZEAL\n"
    "Code  Item Description  Packing  Opening Purchase  Sales  Closing  "
    "Stock-Value  Sales-Value\n"
)


class TestPharmaHubJunJulPhoto(unittest.TestCase):
    def test_header_requires_the_extra_columns(self):
        self.assertTrue(_is_pharma_hub_jun_jul_header(HEADER))
        self.assertFalse(_is_pharma_hub_jun_jul_header(GANESH))
        self.assertTrue(_is_code_item_stock_statement_text(GANESH))
        self.assertFalse(_is_pharma_hub_jun_jul_header(""))
        self.assertFalse(
            _is_pharma_hub_jun_jul_header(
                "Stock Statement (Datewise)\nOpening Purchase Sales Closing"
            )
        )

    def test_july_qty_is_not_kept_as_august_sales(self):
        result = empty_result("hub.jpg", "jpg")
        result["report_title"] = "Stock Statement"
        result["line_items"] = []
        rows = (
            ("CONFIDO TAB", 53, 13, 41, 8, 13),
            ("CLARINA CREAM", 40, 10, 31, 12, 10),
            ("LIV 52 TAB", 132, 68, 14, 51, 68),
            ("TALEKT SYP", 192, 29, 169, 18, 29),
            ("ABANA TAB", 55, 11, 43, 6, 11),
        )
        for name, opening, sales, closing, june, july in rows:
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = opening
            item["sales_qty"] = sales
            item["closing_qty"] = closing
            item["extra"]["june_sale_qty"] = june
            item["extra"]["july_sale_qty"] = july
            # The August sale is the number that closes the row. A swap
            # stores it on the June field while July sits in sales_qty.
            if name == "CONFIDO TAB":
                item["extra"]["june_sale_qty"] = 12
            result["line_items"].append(item)
        fixed = _finalize_pharma_hub_jun_jul(result)
        by_name = {item["product_name"]: item for item in fixed["line_items"]}
        self.assertEqual(by_name["CONFIDO TAB"]["sales_qty"], 12)
        self.assertEqual(by_name["CONFIDO TAB"]["opening_qty"], 53)
        self.assertEqual(by_name["CONFIDO TAB"]["closing_qty"], 41)
        self.assertEqual(
            fixed["totals"]["extra"]["extraction_method"],
            "pharma_hub_jun_jul_photo",
        )

    def test_prior_month_qty_is_cleared_when_stock_did_not_move(self):
        result = empty_result("hub.jpg", "jpg")
        result["line_items"] = []
        for name, opening, june in (
            ("DIAREX TAB", 31, 12),
            ("HERBOLAX TAB", 45, 6),
            ("AMALAKI CAP", 20, 1),
            ("SHALLAKI CAP", 8, 4),
            ("TRIPHALA CAP", 28, 2),
        ):
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = opening
            item["sales_qty"] = june
            item["closing_qty"] = opening
            item["extra"]["june_sale_qty"] = june
            result["line_items"].append(item)
        fixed = _finalize_pharma_hub_jun_jul(result)
        for item in fixed["line_items"]:
            self.assertEqual(item["sales_qty"], 0, item["product_name"])
            self.assertEqual(item["closing_qty"], item["opening_qty"])

    def test_reread_runs_only_when_rows_do_not_balance(self):
        result = empty_result("hub.jpg", "jpg")
        result["report_title"] = "Stock Statement"
        result["company_name"] = "HIMALAYA DRUGS (ZEAL)"
        result["line_items"] = []
        for index in range(8):
            item = empty_line_item()
            item["product_name"] = f"ITEM {index}"
            item["opening_qty"] = 40
            item["sales_qty"] = 10
            item["closing_qty"] = 40
            result["line_items"].append(item)
        self.assertTrue(_pharma_hub_jun_jul_needs_reread(result))

        balanced = empty_result("ganesh.jpg", "jpg")
        balanced["report_title"] = "Stock Statement"
        balanced["company_name"] = "HIMALAYA ZEAL"
        balanced["line_items"] = []
        for index in range(8):
            item = empty_line_item()
            item["product_name"] = f"ITEM {index}"
            item["opening_qty"] = 61
            item["sales_qty"] = 4
            item["closing_qty"] = 57
            balanced["line_items"].append(item)
        self.assertFalse(_pharma_hub_jun_jul_needs_reread(balanced))

        datewise = empty_result("date.jpg", "jpg")
        datewise["report_title"] = "Stock Statement (Datewise)"
        datewise["line_items"] = result["line_items"]
        self.assertFalse(_pharma_hub_jun_jul_needs_reread(datewise))

    def test_party_is_this_stockist_only(self):
        hub = empty_result("hub.jpg", "jpg")
        hub["stockist_name"] = "THE PHARMA HUB"
        hub["company_name"] = "HIMALAYA DRUGS (ZEAL)"
        self.assertTrue(_pharma_hub_party(hub))
        ganesh = empty_result("ganesh.jpg", "jpg")
        ganesh["stockist_name"] = "GANESH AGENCY"
        ganesh["company_name"] = "HIMALAYA ZEAL"
        self.assertFalse(_pharma_hub_party(ganesh))

    def test_sideways_phone_photo_is_turned_upright(self):
        path = Path(
            r"C:\Users\adity\Downloads\ZL_2026_August"
            r"\0000734262_2026_08_ZL_13_281_01092026171025.jpg"
        )
        if not path.is_file():
            self.skipTest("missing Pharma Hub photo")
        raw = path.read_bytes()
        self.assertTrue(_jpeg_needs_quarter_turn(raw))
        page = Image.open(io.BytesIO(_pharma_hub_page_jpeg(raw)))
        self.assertGreater(page.size[1], page.size[0])

    def test_unbalanced_read_is_not_accepted(self):
        result = empty_result("hub.jpg", "jpg")
        result["line_items"] = []
        for index in range(8):
            item = empty_line_item()
            item["product_name"] = f"ITEM {index}"
            item["opening_qty"] = 10
            item["sales_qty"] = 4
            item["closing_qty"] = 10
            result["line_items"].append(item)
        self.assertFalse(_pharma_hub_extract_is_usable(result))


if __name__ == "__main__":
    unittest.main()
