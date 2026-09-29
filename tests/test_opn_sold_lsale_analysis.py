"""Stock and Sale Analysis: Opn, Rec, Sold, Stock, Lsale. Lsale is not Sold."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_opn_sold_lsale_analysis_text,
    _is_ssa_opening_receipt_issue_value_text,
    _parse_opn_sold_lsale_analysis,
    extract_sales_statement,
)


LOHIA = Path(
    r"C:\Users\adity\Downloads\ZL_2026_August\0000734319_2026_08_ZL_03_359_05092026134732.PDF"
)

HEADER = """
LOHIA TRADING CO.
Stock and Sale Analysis From 01/08/2026 TO 29/08/2026
Sr Product/Company Packing Opn Value Rec Value Sold Value Stock Lsale Value
HIMALAY JANDRA
1 AACTARIL SOAP 75'GM 75'GM 0 0.00 72 5592.96 0 0.00 72 40 5592.96
2 ARJUNA TAB. 60'S 60'S 60 12311.40 0 0.00 9 1846.71 51 1 10464.69
13 KOFLET H LOZENGE 1 BOX 1 BOX 16 3072.16 0 0.00 0 0.00 16 50 3072.16
38 LIV 52 DS TAB. 60S. 60S. 366 60543.72 2 330.84 320 52934.40 48 0 7940.16
4 GASEX SYRUP (GINGER LEMON) 200 200ML 27 2598.48 0 0.00 4 384.96 23 5 2213.52
Total 7771 1039980.02 212 24536.57 2265 307146.72 5718 757369.87
"""

SSA = """
STOCK & SALES ANALYSIS
ITEM DESCRIPTION OPENING RECEIPT ISSUE CLOSING DUMP VALUE
"""


class TestOpnSoldLsaleAnalysis(unittest.TestCase):
    def test_header_is_not_the_issue_dump_sheet(self):
        self.assertTrue(_is_opn_sold_lsale_analysis_text(HEADER))
        self.assertFalse(_is_ssa_opening_receipt_issue_value_text(HEADER))
        self.assertFalse(_is_opn_sold_lsale_analysis_text(SSA))

    def test_sold_is_sales_and_lsale_stays_separate(self):
        parsed = _parse_opn_sold_lsale_analysis(HEADER, "lohia.pdf", "pdf")
        self.assertIsNotNone(parsed)
        by_name = {
            item["product_name"]: item for item in parsed["line_items"]
        }
        soap = by_name["AACTARIL SOAP 75'GM"]
        self.assertEqual(soap["sales_qty"], 0)
        self.assertEqual(soap["receipts_qty"], 72)
        self.assertEqual(soap["closing_qty"], 72)
        self.assertEqual(soap["closing_value"], 5592.96)
        self.assertEqual(soap["extra"]["lsale_qty"], 40)
        arjuna = by_name["ARJUNA TAB. 60'S"]
        self.assertEqual(arjuna["opening_qty"], 60)
        self.assertEqual(arjuna["sales_qty"], 9)
        self.assertEqual(arjuna["sales_value"], 1846.71)
        self.assertEqual(arjuna["closing_qty"], 51)
        self.assertEqual(arjuna["extra"]["lsale_qty"], 1)
        lozenge = by_name["KOFLET H LOZENGE 1 BOX"]
        self.assertEqual(lozenge["packing"], "1 BOX")
        self.assertEqual(lozenge["opening_qty"], 16)
        tab = by_name["LIV 52 DS TAB. 60S."]
        self.assertEqual(tab["sales_qty"], 320)
        self.assertEqual(tab["closing_qty"], 48)
        self.assertEqual(parsed["totals"]["sales_value"], 307146.72)
        self.assertEqual(parsed["totals"]["closing_value"], 757369.87)
        self.assertEqual(parsed["company_name"], "HIMALAY JANDRA")

    @unittest.skipUnless(LOHIA.is_file(), "missing Lohia stock and sale analysis")
    def test_lohia_pdf_rows_balance(self):
        result = extract_sales_statement(LOHIA.read_bytes(), LOHIA.name)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "opn_sold_lsale_analysis")
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 100)
        failed = 0
        for item in items:
            opening = item["opening_qty"] + item["receipts_qty"]
            if abs(opening - item["sales_qty"] - item["closing_qty"]) > 0.01:
                failed += 1
        self.assertEqual(failed, 0)
        self.assertEqual(result["totals"]["sales_value"], 307146.72)
        self.assertEqual(result["totals"]["closing_value"], 757369.87)
        soap = next(
            item for item in items if item["product_name"].startswith("AACTARIL SOAP")
        )
        self.assertEqual(soap["sales_qty"], 0)
        self.assertEqual(soap["extra"]["lsale_qty"], 40)


if __name__ == "__main__":
    unittest.main()
