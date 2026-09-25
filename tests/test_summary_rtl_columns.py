"""Company-wise SUMMARY RTL: OP AMT is opening value, SALE AMT is not sales qty."""

import unittest

import fitz

from services.sales_statement_extractor import (
    _apply_parsed_sales_json,
    _clear_summary_rtl_fabricated_qty,
    _is_summary_rtl_statement,
    _summary_rtl_items_from_text,
    _summary_rtl_items_from_words,
    empty_result,
    extract_sales_statement,
)

_HEADER = """\
SHUBHAM AGENCY
SALES & STOCK STATEMENT ( COMPANY WISE => SUMMARY RTL )
HIMALAYA DRUG
From: 01/08/2026 To: 31/08/2026
PRODUCT NAME OP QTY OP AMT SALE AMT CLOSING CLOSING AMT
"""

_ROWS = """\
BONNISAN 100 SYP 64 4038.72 0 64 4038.72
CYSTONE FORT TAB 27 3044.51 725.04 21 2367.95
CYSTONE TAB 262 45152.20 4769.06 236 40671.41
EVECARE FORTE 200ML 1 182.26 394.73 0 0
EVECARE SYRUP 400ML 1 232.61 251.90 0 0
EVECARE SYP (L) 9 1372.23 0 9 1372.23
HIORA K PASTE 100G 33 3744.28 122.88 32 3630.82
HIORA-D TOOTHPAST 100G 39 3678.95 102.18 38 3584.62
LIV 52 100ML SYP 1173 112954 29388.50 883 85028.66
LIV 52 200ML SYP 125 20134 21805 700 112750.10
LIV-52 100ML DS SYP 60 9573.48 1036.81 54 8616.13
LIV-52 DROP 413 42471.70 5690.01 363 37329.83
LIV-52 DS TAB 753 142562 7866.28 716 135557.10
LIV-52 TAB 17 2652.51 1965.92 5 780.15
PILEX FORTE GEL 30GM 11 1053.12 518.35 6 574.43
PLATENZA SYP 100ML 25 4645.20 804.88 21 3901.97
RUMALAYA OIL 60ML 769 82459.20 14615 639 68519.39
TENTEX ROYAL CAP 85 16939.70 0 85 16939.65
"""


class TestSummaryRtlColumns(unittest.TestCase):
    def test_detector_ignores_other_stock_statements(self):
        self.assertTrue(_is_summary_rtl_statement(_HEADER))
        self.assertTrue(
            _is_summary_rtl_statement(
                "SHUBHAM AGENCY\n"
                "SALES & STOCK STATEMENT ( COMPANY WISE => SUMMARY RTL )\n"
                "PRODUCT NAME\n"
                "OP OP SALE CLOSING CLOSING\n"
                "QTY AMT AMT AMT\n"
            )
        )
        self.assertFalse(
            _is_summary_rtl_statement(
                "Sales & Stock Statement\nReceipt/Pur Opening Issue Closing\nCode PACKING"
            )
        )
        self.assertFalse(
            _is_summary_rtl_statement(
                "STOCK STATEMENT\nPRODUCT DESCRIPTION PACKING OPSTK PURCH SALE IN/OT STOCK"
            )
        )

    def test_op_amt_is_opening_value_and_sales_qty_is_absent(self):
        items = {
            item["product_name"]: item
            for item in _summary_rtl_items_from_text(_HEADER + _ROWS)
        }
        self.assertEqual(len(items), 18)

        fort = items["CYSTONE FORT TAB"]
        self.assertEqual(fort["opening_qty"], 27)
        self.assertEqual(fort["opening_value"], 3044.51)
        self.assertIsNone(fort["sales_qty"])
        self.assertEqual(fort["sales_value"], 725.04)
        self.assertEqual(fort["closing_qty"], 21)
        self.assertEqual(fort["closing_value"], 2367.95)

        cystone = items["CYSTONE TAB"]
        self.assertEqual(cystone["opening_qty"], 262)
        self.assertEqual(cystone["opening_value"], 45152.20)
        self.assertEqual(cystone["extra"]["opening_value"], 45152.20)
        self.assertEqual(cystone["sales_value"], 4769.06)
        self.assertEqual(cystone["closing_qty"], 236)
        self.assertEqual(cystone["closing_value"], 40671.41)
        self.assertIsNone(cystone["sales_qty"])
        self.assertNotEqual(cystone["sales_qty"], cystone["closing_qty"])
        self.assertNotIn("sales_qty", cystone["extra"])
        self.assertIsNone(cystone["receipts_qty"])
        self.assertIsNone(cystone["extra"]["receipts_value"])
        self.assertNotEqual(cystone["opening_value"], cystone["sales_value"])

        syrup = items["LIV 52 100ML SYP"]
        self.assertEqual(syrup["opening_qty"], 1173)
        self.assertEqual(syrup["opening_value"], 112954)
        self.assertEqual(syrup["sales_value"], 29388.50)
        self.assertEqual(syrup["closing_qty"], 883)
        self.assertEqual(syrup["closing_value"], 85028.66)
        self.assertIsNone(syrup["sales_qty"])
        self.assertNotEqual(syrup["sales_qty"], syrup["closing_qty"])
        self.assertNotIn("sales_qty", syrup["extra"])

        liv = items["LIV-52 DS TAB"]
        self.assertEqual(liv["opening_qty"], 753)
        self.assertEqual(liv["opening_value"], 142562)
        self.assertEqual(liv["sales_value"], 7866.28)
        self.assertEqual(liv["closing_qty"], 716)
        self.assertEqual(liv["closing_value"], 135557.10)
        self.assertIsNone(liv["sales_qty"])
        self.assertNotEqual(liv["sales_qty"], liv["closing_qty"])

        oil = items["RUMALAYA OIL 60ML"]
        self.assertEqual(oil["opening_qty"], 769)
        self.assertEqual(oil["opening_value"], 82459.20)
        self.assertEqual(oil["sales_value"], 14615)
        self.assertEqual(oil["closing_qty"], 639)
        self.assertEqual(oil["closing_value"], 68519.39)
        self.assertIsNone(oil["sales_qty"])
        self.assertNotEqual(oil["sales_qty"], oil["closing_qty"])

        self.assertEqual(items["BONNISAN 100 SYP"]["opening_qty"], 64)
        self.assertEqual(items["BONNISAN 100 SYP"]["sales_value"], 0)
        self.assertEqual(items["BONNISAN 100 SYP"]["closing_qty"], 64)
        self.assertEqual(items["EVECARE FORTE 200ML"]["opening_qty"], 1)
        self.assertEqual(items["EVECARE FORTE 200ML"]["opening_value"], 182.26)
        self.assertEqual(items["EVECARE FORTE 200ML"]["sales_value"], 394.73)
        self.assertEqual(items["EVECARE FORTE 200ML"]["closing_qty"], 0)
        self.assertEqual(items["EVECARE FORTE 200ML"]["closing_value"], 0)
        self.assertEqual(items["EVECARE SYP (L)"]["opening_qty"], 9)
        self.assertEqual(items["EVECARE SYP (L)"]["opening_value"], 1372.23)
        self.assertEqual(items["EVECARE SYP (L)"]["sales_value"], 0)
        self.assertEqual(items["EVECARE SYP (L)"]["closing_qty"], 9)
        self.assertEqual(items["EVECARE SYP (L)"]["closing_value"], 1372.23)
        self.assertEqual(items["HIORA K PASTE 100G"]["opening_value"], 3744.28)
        self.assertEqual(items["HIORA K PASTE 100G"]["sales_value"], 122.88)
        self.assertEqual(items["HIORA K PASTE 100G"]["closing_value"], 3630.82)
        self.assertEqual(items["HIORA-D TOOTHPAST 100G"]["opening_value"], 3678.95)
        self.assertEqual(items["HIORA-D TOOTHPAST 100G"]["sales_value"], 102.18)
        self.assertEqual(items["LIV-52 100ML DS SYP"]["opening_value"], 9573.48)
        self.assertEqual(items["LIV-52 100ML DS SYP"]["sales_value"], 1036.81)
        self.assertEqual(items["LIV-52 DROP"]["opening_value"], 42471.70)
        self.assertEqual(items["LIV-52 DROP"]["sales_value"], 5690.01)
        self.assertEqual(items["LIV-52 TAB"]["opening_value"], 2652.51)
        self.assertEqual(items["LIV-52 TAB"]["sales_value"], 1965.92)
        self.assertEqual(items["PILEX FORTE GEL 30GM"]["sales_value"], 518.35)
        self.assertEqual(items["PLATENZA SYP 100ML"]["sales_value"], 804.88)
        for item in items.values():
            self.assertIsNone(item["sales_qty"])
            self.assertNotEqual(item["sales_qty"], item["closing_qty"])
            self.assertIn("opening_value", item)
        self.assertEqual(items["LIV 52 100ML SYP"]["opening_qty"], 1173)
        self.assertEqual(items["LIV 52 100ML SYP"]["closing_qty"], 883)
        self.assertEqual(items["LIV 52 200ML SYP"]["opening_qty"], 125)
        self.assertEqual(items["LIV 52 200ML SYP"]["closing_qty"], 700)
        self.assertEqual(items["TENTEX ROYAL CAP"]["opening_qty"], 85)
        self.assertEqual(items["TENTEX ROYAL CAP"]["closing_value"], 16939.65)

    def test_pdf_route_keeps_sales_qty_unavailable(self):
        doc = fitz.open()
        page = doc.new_page()
        y = 72
        for line in (_HEADER + _ROWS).splitlines():
            if line.strip():
                page.insert_text((40, y), line.strip(), fontsize=9)
                y += 14
        result = extract_sales_statement(doc.tobytes(), "shubham_summary_rtl.pdf")
        doc.close()
        items = result["line_items"]
        self.assertEqual(len(items), 18)
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"], "summary_rtl_op_amt"
        )
        self.assertNotIn("sales_qty", result["totals"]["extra"])
        self.assertNotIn("receipts_qty", result["totals"]["extra"])
        cystone = next(item for item in items if item["product_name"] == "CYSTONE TAB")
        self.assertIsNone(cystone["sales_qty"])
        self.assertNotEqual(cystone["sales_qty"], cystone["closing_qty"])
        self.assertNotIn("sales_qty", cystone["extra"])
        self.assertEqual(cystone["opening_value"], 45152.20)
        self.assertEqual(cystone["sales_value"], 4769.06)
        self.assertEqual(cystone["closing_qty"], 236)
        self.assertEqual(cystone["closing_value"], 40671.41)
        syrup = next(item for item in items if item["product_name"] == "LIV 52 100ML SYP")
        self.assertEqual(syrup["opening_qty"], 1173)
        self.assertEqual(syrup["opening_value"], 112954)
        self.assertEqual(syrup["sales_value"], 29388.50)
        self.assertEqual(syrup["closing_qty"], 883)
        self.assertEqual(syrup["closing_value"], 85028.66)
        self.assertIsNone(syrup["sales_qty"])
        self.assertNotEqual(syrup["sales_qty"], syrup["closing_qty"])
        sale_sum = round(sum(item["sales_value"] for item in items), 2)
        self.assertGreater(sale_sum, 0)
        self.assertEqual(result["totals"]["sales_value"], sale_sum)
        self.assertEqual(result["totals"]["extra"]["line_sales_value_sum"], sale_sum)
        self.assertNotEqual(result["totals"]["extra"]["line_sales_value_sum"], 0)
        self.assertEqual(cystone["sales_value"], 4769.06)
        self.assertNotEqual(cystone["sales_value"], 0)
        self.assertEqual(result["totals"]["opening_qty"], sum(
            item["opening_qty"] for item in items
        ))
        self.assertEqual(result["totals"]["closing_qty"], sum(
            item["closing_qty"] for item in items
        ))
        self.assertEqual(
            result["totals"]["extra"]["stock_validation"]["extracted_closing"],
            result["totals"]["closing_qty"],
        )
        self.assertEqual(
            result["totals"]["extra"]["stock_validation"]["calculated_closing"],
            result["totals"]["closing_qty"],
        )

    def test_stacked_header_keeps_sale_amt_out_of_sales_qty(self):
        """OP over QTY must not drop OP AMT or store SALE AMT in extra.sales_qty."""
        words = []

        def add(x0, y0, x1, text):
            words.append((x0, y0, x1, y0 + 8, text))

        add(40, 40, 90, "PRODUCT")
        add(100, 40, 140, "NAME")
        add(180, 40, 200, "OP")
        add(250, 40, 270, "OP")
        add(330, 40, 360, "SALE")
        add(420, 40, 470, "CLOSING")
        add(520, 40, 570, "CLOSING")
        add(180, 52, 210, "QTY")
        add(250, 52, 280, "AMT")
        add(330, 52, 370, "AMT")
        add(540, 52, 580, "AMT")
        add(40, 80, 120, "CYSTONE")
        add(125, 80, 155, "TAB")
        add(180, 80, 210, "262")
        add(230, 80, 280, "45152.20")
        add(320, 80, 370, "4769.06")
        add(430, 80, 470, "236")
        add(520, 80, 580, "40671.41")
        items = _summary_rtl_items_from_words(words)
        self.assertEqual(len(items), 1)
        cystone = items[0]
        self.assertEqual(cystone["product_name"], "CYSTONE TAB")
        self.assertEqual(cystone["opening_qty"], 262)
        self.assertEqual(cystone["opening_value"], 45152.20)
        self.assertEqual(cystone["sales_value"], 4769.06)
        self.assertEqual(cystone["closing_qty"], 236)
        self.assertEqual(cystone["closing_value"], 40671.41)
        self.assertIsNone(cystone["sales_qty"])
        self.assertNotEqual(cystone["sales_qty"], cystone["closing_qty"])
        self.assertNotIn("sales_qty", cystone["extra"])

    def test_vision_json_keeps_op_amt_and_drops_copied_sales_qty(self):
        """The image reader was copying OP QTY into sales_qty and dropping OP AMT."""
        parsed = {
            "stockist_name": "SHUBHAM AGENCY",
            "company_name": "HIMALAYA DRUG",
            "report_title": "SALES & STOCK STATEMENT ( COMPANY WISE => SUMMARY RTL )",
            "line_items": [
                {
                    "product_name": "CYSTONE TAB",
                    "opening_qty": 262,
                    "opening_value": 45152.20,
                    "sales_qty": 262,
                    "sales_value": 4769.06,
                    "closing_qty": 236,
                    "closing_value": 40671.41,
                },
                {
                    "product_name": "CYSTONE FORT TAB",
                    "opening_qty": 27,
                    "opening_value": 3044.51,
                    "sales_qty": 0,
                    "sales_value": 725.04,
                    "closing_qty": 21,
                    "closing_value": 2367.95,
                },
            ],
        }
        result = _apply_parsed_sales_json(empty_result("shubham.pdf", "pdf"), parsed)
        items = {item["product_name"]: item for item in result["line_items"]}
        cystone = items["CYSTONE TAB"]
        self.assertEqual(cystone["opening_value"], 45152.20)
        self.assertEqual(cystone["sales_value"], 4769.06)
        self.assertIsNone(cystone["sales_qty"])
        self.assertNotEqual(cystone["sales_qty"], cystone["opening_qty"])
        fort = items["CYSTONE FORT TAB"]
        self.assertEqual(fort["opening_value"], 3044.51)
        self.assertEqual(fort["sales_value"], 725.04)
        self.assertIsNone(fort["sales_qty"])
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"], "summary_rtl_op_amt"
        )

        other = empty_result("zandra.pdf", "pdf")
        other["report_title"] = "Stock and Sale Statement"
        other["line_items"] = [
            {
                "product_name": "LIV 52",
                "opening_qty": 10,
                "sales_qty": 4,
                "sales_value": 100,
                "closing_qty": 6,
                "closing_value": 200,
                "extra": {},
            }
        ]
        kept = _clear_summary_rtl_fabricated_qty(other)
        self.assertEqual(kept["line_items"][0]["sales_qty"], 4)


if __name__ == "__main__":
    unittest.main()
