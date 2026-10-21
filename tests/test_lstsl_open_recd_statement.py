"""LstSL / Open / Recd / Sales / Close keeps last-month sales out of opening."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _parse_lstsl_open_recd_statement,
    extract_sales_statement,
)

PDF = Path("0000734693_2026_08_ZL_13_274_07092026153208.PDF")

SAMPLE = """
JAIN PHARMA,KHAMGAON
Stock & Sales from 01/08/2026 to 31/08/2026 for : HIMALAYA WELLNESS CO (DIV.: ZEAL GRP)    Page :   1
Product Name              Pack   LstSL  Open Recd. Sales Close  Order  Pend
ABANA TAB                 60'S      28    89   100    22   167   -167     -
ACTARIL SOAP-75G          75G       78    67     -    37    30    -30     -
OXITARD CAP               10 CAP    39    71     -    21    50    -50     -
VASAKA TAB                60'S       2    35     -     -    35    -35     -
VRIKSHAMLA TAB            60TAB      -     1    60     -    61    -61     -
Last Month Sales    2,28,014.92  Open. Value     4,36,418.60
Receipt Value       2,73,994.52  Sales Value     2,59,377.27
Closing Value       4,47,392.83
"""

SAMPLE_LSTMOVE_STKVAL = """
KIRAN AGENCIES,AKOLA
Stock & Sales Report for the month SEPTEMBER-2026 for : HIMALAYA WELLNESS COMPANY (DIV.: HIMALAYA OTX)    Page :   1
Product Name              Pack   LstSL  Open Recd. Sales Close  Order  Pend  LstMove Stk.Value
ASHVAGANDHA TAB           60'S      20    36     -     6    30    -27     - 17/09/26   5866.80
ASHWAGANDHA ORGANIC CAP   30'S      13    20     -     2    18    -17     - 12/09/26   6650.10
AYURSLIM CAP              60'S      19     -    24     2    22    -21     - 24/09/26   5330.16
BRAHMI                    60'S       -    61     -     1    60    -60     - 22/09/26  11863.90
GASEX SYP ELAICHI FLAVOUR 200ML     68    48     -    11    37    -32    28 11/09/26   4228.36
GASEX SYP GINGER LEMON    200ML      8   151     -     1   150   -150     - 18/09/26  16286.94
Last Month Sales    2,28,014.92  Open. Value     1,00,000.00
Receipt Value         50,000.00  Sales Value       80,000.00
Closing Value       1,20,000.00
"""

# Phone-screenshot OCR of a LstSL grid: glued qtys, replacement-char minuses.
SAMPLE_PHONE_OCR = """
7:09 Mon, 19 Oct
41150538.PDF
PDF reader
JAIN MEDICALS, AMRAVATI
Stock & Sales Report for the month SEPTEMBER-2026 for : HIMALAYA WELLNESS CO. (DIV.: HIMALAYA OTX) Page: 1
Product Name Pack LstSL Open Recd. Sales Close Order Pend Stk.Value
ASHVANGANDHA CAPS 60'S 30 42 - 37 5 33 60 1006.60
AYURSLIM CAPS 60'S 2 1 - - 1 \ufffd1 - 216.39
EXTRA MOIS. SOAP 2 IN1 125G - 4 - - 4 \ufffd4 - 440.96
GASEX SYP (ELICHI) 200ML 2 1 - - 1 \ufffd1 28 101.31
GASEX SYP GINGER LEMON \ufffd 200ML. 27,031 2819 40-20 - 4020.21
GASEX TAB 100'S 56 155 - 65 90 -23 200 12475.27
GENTLE BABY WASH 100ML. - 7 - - 7 7 - 592.57
KOFLET SYRUP 100ML 48147 - 30 117-86 - 9151.85
KOFLET LOZENGES 10'S 24145 - 6 139-133 - 2161.97
TENTEX ROYAL CAP 10'S 4157 - 31 26 5 - 4934.80
Last Month Sales 51,557.21 Open. Value 1,46,490.33
Receipt Value 33,799.28 Sales Value 49,175.14
Closing Value 1,30,501.85
"""


class TestLstslOpenRecdStatement(unittest.TestCase):
    def test_lstsl_is_not_opening_and_order_is_not_closing(self):
        result = _parse_lstsl_open_recd_statement(SAMPLE, "stmt.pdf", "pdf")
        self.assertEqual(result["stockist_name"], "JAIN PHARMA,KHAMGAON")
        self.assertEqual(result["company_name"], "HIMALAYA WELLNESS CO (DIV.: ZEAL GRP)")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        rows = {item["product_name"]: item for item in result["line_items"]}
        abana = rows["ABANA TAB"]
        self.assertEqual(abana["opening_qty"], 89)
        self.assertEqual(abana["receipts_qty"], 100)
        self.assertEqual(abana["sales_qty"], 22)
        self.assertEqual(abana["closing_qty"], 167)
        self.assertEqual(abana["extra"]["last_sales_qty"], 28)
        self.assertEqual(rows["ACTARIL SOAP-75G"]["receipts_qty"], 0)
        self.assertEqual(rows["OXITARD CAP"]["packing"], "10 CAP")
        self.assertEqual(rows["VASAKA TAB"]["sales_qty"], 0)
        self.assertEqual(result["totals"]["sales_value"], 259377.27)
        self.assertEqual(result["totals"]["closing_value"], 447392.83)
        self.assertIsNone(
            _parse_lstsl_open_recd_statement(
                "Product Name Open Recd Sales Close\nABANA TAB 1 2 3 4",
                "other.pdf",
                "pdf",
            )
        )

    def test_lstmove_stk_value_not_shifted_into_qty(self):
        result = _parse_lstsl_open_recd_statement(
            SAMPLE_LSTMOVE_STKVAL, "kiransept.pdf", "pdf"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["stockist_name"], "KIRAN AGENCIES,AKOLA")
        self.assertEqual(result["period_from"], "2026-09-01")
        self.assertEqual(result["period_to"], "2026-09-30")
        self.assertIn("HIMALAYA", result["company_name"] or "")
        rows = {item["product_name"]: item for item in result["line_items"]}
        ash = rows["ASHVAGANDHA TAB"]
        self.assertEqual(ash["packing"], "60'S")
        self.assertEqual(ash["opening_qty"], 36.0)
        self.assertEqual(ash["receipts_qty"], 0.0)
        self.assertEqual(ash["sales_qty"], 6.0)
        self.assertEqual(ash["closing_qty"], 30.0)
        self.assertEqual(ash["closing_value"], 5866.80)
        self.assertEqual(ash["extra"]["last_sales_qty"], 20.0)
        self.assertEqual(rows["BRAHMI"]["opening_qty"], 61.0)
        self.assertEqual(rows["BRAHMI"]["closing_qty"], 60.0)
        names = [i["product_name"] for i in result["line_items"]]
        self.assertNotIn("Last Month Sales", names)
        self.assertNotIn("Closing Value", names)
        self.assertEqual(result["totals"]["sales_value"], 80000.0)
        self.assertEqual(result["totals"]["closing_value"], 120000.0)

    @unittest.skipUnless(PDF.is_file(), "missing Jain Pharma PDF")
    def test_jain_pharma_pdf_keeps_open_and_close(self):
        result = extract_sales_statement(PDF.read_bytes(), PDF.name)
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"], "lstsl_open_recd_sales"
        )
        self.assertEqual(result["stockist_name"], "JAIN PHARMA,KHAMGAON")
        self.assertNotIn("HADJOD", result["company_name"])
        rows = {item["product_name"]: item for item in result["line_items"]}
        abana = rows["ABANA TAB"]
        self.assertEqual(abana["opening_qty"], 89.0)
        self.assertEqual(abana["sales_qty"], 22.0)
        self.assertEqual(abana["closing_qty"], 167.0)
        self.assertEqual(result["totals"]["sales_value"], 259377.27)

    def test_ocr_glue_and_missing_dash_rows_are_kept(self):
        result = _parse_lstsl_open_recd_statement(
            SAMPLE_PHONE_OCR, "95.jpg", "jpg"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["stockist_name"], "JAIN MEDICALS, AMRAVATI")
        self.assertNotIn("7:09", result["stockist_name"] or "")
        rows = {item["product_name"]: item for item in result["line_items"]}
        self.assertIn("GASEX TAB", rows)
        self.assertIn("TENTEX ROYAL CAP", rows)
        self.assertIn("KOFLET SYRUP", rows)
        self.assertIn("AYURSLIM CAPS", rows)
        ginger = rows["GASEX SYP GINGER LEMON"]
        self.assertEqual(ginger["opening_qty"], 31.0)
        self.assertEqual(ginger["receipts_qty"], 28.0)
        self.assertEqual(ginger["sales_qty"], 19.0)
        self.assertEqual(ginger["closing_qty"], 40.0)
        self.assertEqual(ginger["closing_value"], 4020.21)
        tentex = rows["TENTEX ROYAL CAP"]
        self.assertEqual(tentex["opening_qty"], 57.0)
        self.assertEqual(tentex["receipts_qty"], 0.0)
        self.assertEqual(tentex["sales_qty"], 31.0)
        self.assertEqual(tentex["closing_qty"], 26.0)
        syrup = rows["KOFLET SYRUP"]
        self.assertEqual(syrup["opening_qty"], 147.0)
        self.assertEqual(syrup["sales_qty"], 30.0)
        self.assertEqual(syrup["closing_qty"], 117.0)
        tab = rows["GASEX TAB"]
        self.assertEqual(tab["opening_qty"], 155.0)
        self.assertEqual(tab["sales_qty"], 65.0)
        self.assertEqual(tab["closing_qty"], 90.0)
        ash = rows["ASHVANGANDHA CAPS"]
        self.assertEqual(ash["opening_qty"], 42.0)
        self.assertEqual(ash["sales_qty"], 37.0)
        self.assertEqual(ash["closing_qty"], 5.0)
        self.assertEqual(result["totals"]["sales_value"], 49175.14)
        self.assertEqual(result["totals"]["closing_value"], 130501.85)


if __name__ == "__main__":
    unittest.main()
