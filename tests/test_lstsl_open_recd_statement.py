"""LstSL / Open / Recd / Sales / Close keeps last-month sales out of opening."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from services.sales_statement_extractor import (
    _lstsl_clean_packing,
    _parse_lstsl_open_recd_statement,
    _try_lstsl_open_recd_image,
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

# LstSL layout family with printed footer money (SANJIVAN MEDICO-style audit).
SAMPLE_LSTSL_FOOTER_AUDIT = """
SANJIVAN MEDICO TRADERS, CHANDRAPUR
Stock & Sales Report for the month OCTOBER-2026 for : THE HIMALAYA DRUG CO. (DIV. ZEAL)    Page :   1
Product Name              Pack   LstSL  Open Recd. Sales Close  Order  Pend  LstMove Stk.Value
BLEMINOR CREAM            30ML       5     1    50     7    44    -43     - 12/10/26   1200.00
HAIRZONE SOLUTION         60ML       2     7     -     0     7     -7     - 18/10/26    980.50
LIV-52 TABLET             100'S     40   134   100   207    27   -27     - 22/10/26   4500.00
RENALKA SYP.-200ML        200ML      1     8     -     0     8     -8     - 25/10/26    640.00
SHALLAKI TAB              60'S       3    12     -     4     8     -8     - 30/10/26   1100.00
Last Month Sales    2,19,224.60  Open. Value     2,11,888.53
Receipt Value       1,84,261.24  Sales Value     2,06,038.56
Closing Value       1,89,585.72
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

    def test_lstsl_footer_money_not_line_items(self):
        result = _parse_lstsl_open_recd_statement(
            SAMPLE_LSTSL_FOOTER_AUDIT, "51929289.PDF", "pdf"
        )
        self.assertIsNotNone(result)
        self.assertEqual(
            result["stockist_name"], "SANJIVAN MEDICO TRADERS, CHANDRAPUR"
        )
        self.assertIn("HIMALAYA", result["company_name"] or "")
        names = [i["product_name"] for i in result["line_items"]]
        self.assertNotIn("Last Month Sales", names)
        self.assertNotIn("Open. Value", names)
        self.assertNotIn("Receipt Value", names)
        self.assertNotIn("Sales Value", names)
        self.assertNotIn("Closing Value", names)
        rows = {item["product_name"]: item for item in result["line_items"]}
        ble = rows["BLEMINOR CREAM"]
        self.assertEqual(ble["opening_qty"], 1.0)
        self.assertEqual(ble["receipts_qty"], 50.0)
        self.assertEqual(ble["sales_qty"], 7.0)
        self.assertEqual(ble["closing_qty"], 44.0)
        liv = rows["LIV-52 TABLET"]
        self.assertEqual(liv["opening_qty"], 134.0)
        self.assertEqual(liv["receipts_qty"], 100.0)
        self.assertEqual(liv["sales_qty"], 207.0)
        self.assertEqual(liv["closing_qty"], 27.0)
        self.assertEqual(result["totals"]["sales_value"], 206038.56)
        self.assertEqual(result["totals"]["closing_value"], 189585.72)
        extra = result["totals"]["extra"]
        self.assertEqual(extra["opening_value"], 211888.53)
        self.assertEqual(extra["receipts_value"], 184261.24)
        self.assertEqual(extra["last_month_sales_value"], 219224.60)
        self.assertEqual(extra["total_row_source"], "lstsl_open_recd_footer")

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

    def test_ocr_junk_packing_and_comma_glued_lstsl_open(self):
        """Phone OCR: em-dash+1JAR pack, «22,78 LstSL/Open glue, missing dashes."""
        sample = """
SANJIVAN MEDICO TRADERS, CHANDRAPUR
Stock & Sales Report for the month OCTOBER-2026 for : THE HIMALAYA DRUG CO. (DIV.: HIMALAYA WELLNESS (OTX)) Page: 1
Product Name Pack LstSL Open Recd. Sales Close Order Pend LstMove Stk.Value
ASHVAGANDHA TABLET 1*60T 41 30 60 45 45 -23 - 29/10/26 8914.50
GASEX SYRUP 1*200M 2 29 - 20 9 1 28 30/10/26 1028.52
GASEX TAB. 1*100 82 109 - 18 91 -82 - 27/10/26 14283.36
GOKSHURA TAB 1*60'S 7 5 60 13 52 -46 - 26/10/26 10895.04
HIMCOCID SUSPENSION 1*200M 36 2 28 8 22 -18 - 21/10/26 2430.56
KOFLET LOZENGES 200+25 \u20141JAR 23 3 12 5 10 -8 - 15/10/26 3009.52
KOFLET SYP 1*100M \u00ab22,78 - 56 22 6 56 30/10/26 1961.08
NEEM FACE WASH 15ML*12QTY 12X15M 3 1 - - 1 -1 - 01/10/26 184.90
RUMALAYA FORTE 1*30T 63 102 - 102 0 51 200 24/10/26 0.00
RUMALAYA GEL-30GM 1*30GM 22 87 - 11 76 -71 - 30/10/26 8092.44
RUMALAYA LINIMENT 1*60ML 74 54 50 56 48 -20 - 31/10/26 6144.00
RUMALAYA TAB. 1*60T 2 44 - 2 42 -41 - 21/10/26 6272.28
TENTEX ROYAL CAP 1*10T 28 83 - 37 46 -28 - 31/10/26 9848.60
Last Month Sales 65,166.80 Open. Value 94,002.34
Receipt Value 37,607.76 Sales Value 58,507.22
Closing Value 73,064.80
"""
        self.assertEqual(_lstsl_clean_packing("\u20141JAR"), "1JAR")
        self.assertEqual(_lstsl_clean_packing("-1JAR"), "1JAR")
        result = _parse_lstsl_open_recd_statement(sample, "1000570765.jpg", "jpg")
        self.assertIsNotNone(result)
        self.assertEqual(len(result["line_items"]), 13)
        rows = {item["product_name"]: item for item in result["line_items"]}
        self.assertEqual(rows["KOFLET LOZENGES 200+25"]["packing"], "1JAR")
        self.assertEqual(rows["KOFLET LOZENGES 200+25"]["opening_qty"], 3.0)
        self.assertEqual(rows["KOFLET LOZENGES 200+25"]["closing_value"], 3009.52)
        kof = rows["KOFLET SYP"]
        self.assertEqual(kof["extra"]["last_sales_qty"], 22.0)
        self.assertEqual(kof["opening_qty"], 78.0)
        self.assertEqual(kof["sales_qty"], 56.0)
        self.assertEqual(kof["closing_qty"], 22.0)
        self.assertEqual(kof["closing_value"], 1961.08)
        # ASCII << OCR junk (same row as «22,78) must still unglue LstSL/Open.
        ascii_junk = sample.replace("\u00ab22,78", "<<22,78")
        ascii_result = _parse_lstsl_open_recd_statement(
            ascii_junk, "1000570765.jpg", "jpg"
        )
        self.assertIsNotNone(ascii_result)
        self.assertEqual(len(ascii_result["line_items"]), 13)
        ascii_kof = {
            item["product_name"]: item for item in ascii_result["line_items"]
        }["KOFLET SYP"]
        self.assertEqual(ascii_kof["opening_qty"], 78.0)
        self.assertEqual(ascii_kof["sales_qty"], 56.0)
        ash = rows["ASHVAGANDHA TABLET"]
        self.assertEqual(ash["opening_qty"], 30.0)
        self.assertEqual(ash["receipts_qty"], 60.0)
        self.assertEqual(ash["closing_value"], 8914.50)
        gel = rows["RUMALAYA GEL-30GM"]
        self.assertEqual(gel["opening_qty"], 87.0)
        self.assertEqual(gel["sales_qty"], 11.0)
        self.assertEqual(gel["closing_qty"], 76.0)
        self.assertEqual(result["totals"]["sales_value"], 58507.22)
        self.assertEqual(result["totals"]["closing_value"], 73064.80)
        # No per-line sales $ column on this layout — Stk.Value is closing.
        self.assertTrue(
            all((item.get("sales_value") in (0, 0.0, None)) for item in result["line_items"])
        )

    def test_lstsl_image_prefers_upscaled_ocr_when_native_drops_rows(self):
        native = """
SANJIVAN MEDICO TRADERS, CHANDRAPUR
Stock & Sales Report for the month OCTOBER-2026 for : HIMALAYA Page: 1
Product Name Pack LstSL Open Recd. Sales Close Order Pend LstMove Stk.Value
GASEX SYRUP 1*200M 2 29 - 20 9 1 28 30/10/26 1028.52
GASEX TAB. 1*100 82 109 - 18 91 -82 - 27/10/26 14283.36
GOKSHURA TAB 1*60'S 7 5 60 13 52 -46 - 26/10/26 10895.04
HIMCOCID SUSPENSION 1*200M 36 2 28 8 22 -18 - 21/10/26 2430.56
RUMALAYA FORTE 1*30T 63 102 - 102 0 51 200 24/10/26 0.00
Last Month Sales 1.00 Open. Value 1.00
Receipt Value 1.00 Sales Value 1.00
Closing Value 1.00
"""
        upscaled = """
SANJIVAN MEDICO TRADERS, CHANDRAPUR
Stock & Sales Report for the month OCTOBER-2026 for : HIMALAYA Page: 1
Product Name Pack LstSL Open Recd. Sales Close Order Pend LstMove Stk.Value
ASHVAGANDHA TABLET 1*60T 41 30 60 45 45 -23 - 29/10/26 8914.50
GASEX SYRUP 1*200M 2 29 - 20 9 1 28 30/10/26 1028.52
GASEX TAB. 1*100 82 109 - 18 91 -82 - 27/10/26 14283.36
GOKSHURA TAB 1*60'S 7 5 60 13 52 -46 - 26/10/26 10895.04
HIMCOCID SUSPENSION 1*200M 36 2 28 8 22 -18 - 21/10/26 2430.56
KOFLET LOZENGES 200+25 1JAR 23 3 12 5 10 -8 - 15/10/26 3009.52
KOFLET SYP 1*100M 22 78 - 56 22 6 56 30/10/26 1961.08
NEEM FACE WASH 15ML*12QTY 12X15M 3 1 - - 1 -1 - 01/10/26 184.90
RUMALAYA FORTE 1*30T 63 102 - 102 0 51 200 24/10/26 0.00
RUMALAYA GEL-30GM 1*30GM 22 87 - 11 76 -71 - 30/10/26 8092.44
RUMALAYA LINIMENT 1*60ML 74 54 50 56 48 -20 - 31/10/26 6144.00
RUMALAYA TAB. 1*60T 2 44 - 2 42 -41 - 21/10/26 6272.28
TENTEX ROYAL CAP 1*10T 28 83 - 37 46 -28 - 31/10/26 9848.60
Last Month Sales 65,166.80 Open. Value 94,002.34
Receipt Value 37,607.76 Sales Value 58,507.22
Closing Value 73,064.80
"""
        with patch.dict(os.environ, {"STOCK_LSTSL_IMAGE_UPSCALE": "true"}):
            with patch(
                "services.sales_statement_extractor._ocr_image_to_text",
                return_value=native,
            ), patch(
                "services.sales_statement_extractor._lstsl_upscaled_ocr_text",
                return_value=upscaled,
            ):
                result = _try_lstsl_open_recd_image(b"fake", "1000570765.jpg", "jpg")
        self.assertIsNotNone(result)
        self.assertEqual(len(result["line_items"]), 13)
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"], "lstsl_open_recd_image"
        )
        names = {i["product_name"] for i in result["line_items"]}
        self.assertIn("ASHVAGANDHA TABLET", names)
        self.assertIn("RUMALAYA GEL-30GM", names)
        self.assertIn("RUMALAYA LINIMENT", names)
        self.assertIn("KOFLET SYP", names)
        self.assertIn("NEEM FACE WASH 15ML*12QTY", names)
        self.assertIn("RUMALAYA TAB.", names)
        self.assertEqual(result["totals"]["closing_value"], 73064.80)
        self.assertAlmostEqual(
            sum(float(i.get("closing_value") or 0) for i in result["line_items"]),
            73064.80,
            places=2,
        )

    def test_lstsl_image_falls_back_to_psm4_when_peek_misses_header(self):
        """psm6 junk must not skip the older psm4 terminal-grid pass."""
        junk = "battery 87%\nPDF reader preview chrome only\n"
        psm4 = """
GUNWANT MEDICALS
Stock & Sales Report for the month SEPTEMBER-2026 for : HIMALAYA Page: 1
Product Name Pack LstSL Open Recd. Sales Close Order Pend LstMove Stk.Value
ASHVAGANDHA TAB 60'S 20 36 - 6 30 -27 - 17/09/26 5866.80
AYURSLIM CAP 60'S 19 - 24 2 22 -21 - 24/09/26 5330.16
BRAHMI 60'S - 61 - 1 60 -60 - 22/09/26 11863.90
GASEX SYP 200ML 68 48 - 11 37 -32 28 11/09/26 4228.36
GASEX TAB 100'S 8 151 - 1 150 -150 - 18/09/26 16286.94
Last Month Sales 1.00 Open. Value 1.00
Receipt Value 1.00 Sales Value 1.00
Closing Value 1.00
"""

        def _ocr(_bytes, psm=6, enhance=False):
            if psm == 4:
                return psm4
            return junk

        with patch(
            "services.sales_statement_extractor._ocr_image_to_text",
            side_effect=_ocr,
        ), patch(
            "services.sales_statement_extractor._lstsl_upscaled_ocr_text",
            return_value="",
        ):
            result = _try_lstsl_open_recd_image(b"fake", "gunwant.jpg", "jpg")
        self.assertIsNotNone(result)
        self.assertEqual(len(result["line_items"]), 5)
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"], "lstsl_open_recd_image"
        )


if __name__ == "__main__":
    unittest.main()
