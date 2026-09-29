"""MediVision Stock and Sales photo: Op, Purc, Sale, WSale, Jul Sa, Jun Sa.

Sales qty is the Sale column. June, July, and wholesale quantities stay beside it.
The older Purc / Purc val sheet is unchanged.
"""

import unittest

from services.sales_statement_extractor import (
    _finalize_medivision_op_wsale,
    _is_medivision_op_wsale_header,
    _is_purc_sale_cl_header,
    _medivision_op_wsale_needs_reread,
    _vision_filed_sale_qty_as_receipt,
    empty_line_item,
    empty_result,
)


HEADER = """
PARAKH AGENCY
Stock and Sales
Company: HIMALAYA-ZEAL
01-08-26 to 31-08-26
Product name Unit Op Purc Sale WSale Jul Sa Jun Sa Sa val Cl qty Cl val NM60D qty NM90D qty NM val NE
"""

PURC_VAL = """
Stock and Sales
Product Unit Purc Purc val Sale Sa val Cl qty Cl val
"""


class TestMedivisionOpWsalePhoto(unittest.TestCase):
    def test_header_matches_only_this_column_set(self):
        self.assertTrue(_is_medivision_op_wsale_header(HEADER))
        self.assertFalse(_is_medivision_op_wsale_header(PURC_VAL))
        self.assertTrue(_is_purc_sale_cl_header(PURC_VAL))
        self.assertFalse(_is_medivision_op_wsale_header(""))
        self.assertFalse(
            _is_medivision_op_wsale_header(
                "STOCK & SALES ANALYSIS\nITEM DESCRIPTION\nOp Purc Sale WSale"
            )
        )

    def test_june_qty_is_not_kept_as_sales_when_stock_did_not_move(self):
        result = empty_result("parakh.jpg", "jpg")
        result["report_title"] = "Stock and Sales"
        result["line_items"] = []
        for name, opening, sales, closing, june in (
            ("AACTARIL SOAP 75GM", 7, 20, 7, 20),
            ("ABANA TAB 60'S", 28, 15, 13, 5),
            ("DIAREX TAB 30'S", 31, 12, 31, 0),
            ("HERBOLAX TAB", 45, 6, 45, 6),
            ("TALEKT SYP 120ML", 46, 0, 46, 0),
        ):
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = opening
            item["sales_qty"] = sales
            item["closing_qty"] = closing
            item["extra"]["june_sale_qty"] = june
            result["line_items"].append(item)
        fixed = _finalize_medivision_op_wsale(result)
        by_name = {item["product_name"]: item for item in fixed["line_items"]}
        self.assertEqual(by_name["AACTARIL SOAP 75GM"]["sales_qty"], 0)
        self.assertEqual(by_name["AACTARIL SOAP 75GM"]["opening_qty"], 7)
        self.assertEqual(by_name["AACTARIL SOAP 75GM"]["closing_qty"], 7)
        self.assertEqual(by_name["ABANA TAB 60'S"]["sales_qty"], 15)
        self.assertEqual(by_name["DIAREX TAB 30'S"]["sales_qty"], 0)
        self.assertEqual(by_name["HERBOLAX TAB"]["sales_qty"], 0)
        self.assertEqual(
            fixed["totals"]["extra"]["extraction_method"],
            "medivision_op_wsale_photo",
        )

    def test_wholesale_is_replaced_when_sale_is_the_number_that_closes(self):
        result = empty_result("parakh.jpg", "jpg")
        result["line_items"] = []
        for name, opening, receipts, sales, closing, wholesale in (
            ("LIV.52 SYP 100ML", 6, 70, 171, 1, 75),
            ("LIV.52 TAB 100'S", 113, 0, 91, 64, 49),
            ("CONFIDO TAB 60'S", 24, 0, 3, 21, 0),
            ("HADJOD TAB", 90, 0, 31, 59, 0),
            ("RENALKA SYP", 56, 0, 45, 11, 0),
        ):
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = opening
            item["receipts_qty"] = receipts
            item["sales_qty"] = sales
            item["closing_qty"] = closing
            item["extra"]["wholesale_qty"] = wholesale
            result["line_items"].append(item)
        fixed = _finalize_medivision_op_wsale(result)
        by_name = {item["product_name"]: item for item in fixed["line_items"]}
        self.assertEqual(by_name["LIV.52 SYP 100ML"]["sales_qty"], 75)
        self.assertEqual(by_name["LIV.52 SYP 100ML"]["receipts_qty"], 70)
        self.assertEqual(by_name["LIV.52 SYP 100ML"]["closing_qty"], 1)
        self.assertEqual(by_name["LIV.52 TAB 100'S"]["sales_qty"], 49)
        self.assertEqual(by_name["CONFIDO TAB 60'S"]["sales_qty"], 3)

    def test_reread_runs_only_when_this_sheet_does_not_balance(self):
        result = empty_result("parakh.jpg", "jpg")
        result["report_title"] = "Stock and Sales"
        result["company_name"] = "HIMALAYA-ZEAL"
        result["line_items"] = []
        for index in range(8):
            item = empty_line_item()
            item["product_name"] = f"ITEM {index}"
            item["opening_qty"] = 10
            item["sales_qty"] = 4
            item["closing_qty"] = 10
            result["line_items"].append(item)
        self.assertTrue(_medivision_op_wsale_needs_reread(result))

        balanced = empty_result("ok.jpg", "jpg")
        balanced["report_title"] = "Stock and Sales"
        balanced["company_name"] = "HIMALAYA-ZEAL"
        balanced["line_items"] = []
        for index in range(8):
            item = empty_line_item()
            item["product_name"] = f"ITEM {index}"
            item["opening_qty"] = 10
            item["sales_qty"] = 4
            item["closing_qty"] = 6
            balanced["line_items"].append(item)
        self.assertFalse(_medivision_op_wsale_needs_reread(balanced))

        analysis = empty_result("ssa.jpg", "jpg")
        analysis["report_title"] = "STOCK & SALES ANALYSIS"
        analysis["company_name"] = "HIMALAYA ZEAL"
        analysis["line_items"] = result["line_items"]
        self.assertFalse(_medivision_op_wsale_needs_reread(analysis))
        self.assertFalse(_vision_filed_sale_qty_as_receipt(result))


if __name__ == "__main__":
    unittest.main()
