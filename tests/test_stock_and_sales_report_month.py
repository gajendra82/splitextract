"""Stock And Sales Report(Month) OpStk/Rcpt/Sales/Cl.Stk — Srinath-style PDF text."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_stock_and_sales_report_month_text,
    _is_zenith_opstk_text,
    _parse_stock_and_sales_report_month,
    extract_sales_statement,
)

SAMPLE = """
SRINATH MEDICAL AGENCIES
NAICKEN STREET,
Stock And Sales Report(Month)
of Aug2026
--------------------------------------------------------------------------------------------------------------------------------------
ProductName                   Pack OpStk  Rcpt  Sales Fre SalesValue Adj Cl.Stk NetStockVaStockValue ILas  IILa  III   PrvSalVal  Age 
--------------------------------------------------------------------------------------------------------------------------------------
ARJUNA TABS                    60C   206           49       12149.85        157   33560.75  36086.88   37    58    64    7676.22   87 
BONNISAN DROPS                30ml    44                                     44    1728.78   1859.25                             2765 
BONNISAN LIQUID              120ML   150   168                              318   15466.82  17707.81                               25 
HARIDRA tabs                    60   100    60     15        3504.13        145   27717.97  29938.58   27    16    12    5668.77   13 
V-GEL                        30gms    85   125    141       14500.50         69    7006.89   7006.89  103    24   137   10122.85      
                                    7392  1288   1513      295916.02  16   7183 1188429.581257339.96 1196  1072  1066  213969.47      
--------------------------------------------------------------------------------------------------------------------------------------
OpStk        PurVal        SalVal        Cl.Val        PM.Sal        IIPM.Sal                                                                               
1238713.71    211908.59     295916.02     1188429.58    224667.94    203987.36                                                                              
                                                                 End Of Report
"""


class _FakePage:
    def __init__(self, text: str):
        self._text = text

    def get_text(self, kind: str = "text"):
        if kind == "text":
            return self._text
        return []


class _FakeDoc(list):
    pass


class TestStockAndSalesReportMonth(unittest.TestCase):
    def test_detector_accepts_month_report_not_zenith(self):
        self.assertTrue(_is_stock_and_sales_report_month_text(SAMPLE))
        self.assertFalse(_is_zenith_opstk_text(SAMPLE))
        zenith = (
            "HIMALAYA-ZENITH Stock And Sales Report (Month)-08/2026\n"
            "ProductName Pack Op.stk Pur sales Free Repl TotalStock\n"
        )
        self.assertFalse(_is_stock_and_sales_report_month_text(zenith))
        self.assertTrue(_is_zenith_opstk_text(zenith))

    def test_parses_opstk_rcpt_sales_clstk(self):
        doc = _FakeDoc([_FakePage(SAMPLE)])
        result = _parse_stock_and_sales_report_month(doc, "srinath.pdf")
        self.assertIsNotNone(result)
        self.assertEqual(result["stockist_name"], "SRINATH MEDICAL AGENCIES")
        self.assertEqual(result["stockist_address"], "NAICKEN STREET,")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "stock_and_sales_report_month",
        )
        self.assertEqual(len(result["line_items"]), 5)
        by_name = {i["product_name"]: i for i in result["line_items"]}
        arjuna = by_name["ARJUNA TABS"]
        self.assertEqual(arjuna["packing"], "60C")
        self.assertEqual(arjuna["opening_qty"], 206.0)
        self.assertEqual(arjuna["receipts_qty"], 0.0)
        self.assertEqual(arjuna["sales_qty"], 49.0)
        self.assertEqual(arjuna["sales_value"], 12149.85)
        self.assertEqual(arjuna["closing_qty"], 157.0)
        self.assertEqual(arjuna["closing_value"], 33560.75)
        liquid = by_name["BONNISAN LIQUID"]
        self.assertEqual(liquid["packing"], "120ML")
        self.assertEqual(liquid["opening_qty"], 150.0)
        self.assertEqual(liquid["receipts_qty"], 168.0)
        self.assertEqual(liquid["sales_qty"], 0.0)
        self.assertEqual(liquid["closing_qty"], 318.0)
        haridra = by_name["HARIDRA tabs"]
        self.assertEqual(haridra["receipts_qty"], 60.0)
        self.assertEqual(haridra["sales_qty"], 15.0)
        self.assertEqual(haridra["closing_qty"], 145.0)
        vgel = by_name["V-GEL"]
        self.assertEqual(vgel["packing"], "30gms")
        self.assertEqual(vgel["opening_qty"], 85.0)
        self.assertEqual(vgel["receipts_qty"], 125.0)
        self.assertEqual(vgel["sales_qty"], 141.0)
        self.assertEqual(vgel["closing_qty"], 69.0)

    def test_live_pdf_fast_path(self):
        path = Path(
            "/var/www/html/splitextract/"
            "0000702745_2026_08_ZA_22_230_04092026061217.pdf"
        )
        if not path.exists():
            self.skipTest("fixture PDF not present")
        result = extract_sales_statement(path.read_bytes(), path.name)
        self.assertFalse(result.get("multi_statement"))
        self.assertEqual(
            ((result.get("totals") or {}).get("extra") or {}).get("extraction_method"),
            "stock_and_sales_report_month",
        )
        self.assertEqual(result["stockist_name"], "SRINATH MEDICAL AGENCIES")
        self.assertGreaterEqual(len(result.get("line_items") or []), 50)
        opening = sum(i.get("opening_qty") or 0 for i in result["line_items"])
        receipts = sum(i.get("receipts_qty") or 0 for i in result["line_items"])
        sales = sum(i.get("sales_qty") or 0 for i in result["line_items"])
        closing = sum(i.get("closing_qty") or 0 for i in result["line_items"])
        self.assertEqual(opening, 7392.0)
        self.assertEqual(receipts, 1288.0)
        self.assertEqual(sales, 1513.0)
        self.assertEqual(closing, 7183.0)


if __name__ == "__main__":
    unittest.main()
