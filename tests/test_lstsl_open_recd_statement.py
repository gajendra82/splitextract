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


if __name__ == "__main__":
    unittest.main()
