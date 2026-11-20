"""Daily Stock Summary (Company-wise) TXT — Product & Pack + Op/Pur/Sales/Cl."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _looks_like_daily_stock_summary_txt,
    _parse_daily_stock_summary_txt,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000736947_2026_08_ZA_25_379_07092026142107.TXT"
)

SAMPLE = """
NARAYAN MEDICAL (NEW)                                                02/09/2026
BHANDERHATI, HOOGHLY                                                 17:26:01
** Daily Stock Summary (Company-wise) **          From 01/08/2026 to 31/08/2026
Company:HIMALAYA ZEUS [M177]                              Ordering Factor: 0.00
        Product & Pack        |  Op. | Pur. |Sales|Sales| Adj./| Cl.  | Order |
                              | Stock| Qty. |Retu.| Qty.| Dmg. |Stock | Qty.  |
AACTARIL SOAP,75GM                 62      0     0    10      0     52     -52
CLARINA ANTI ACNE FACEWAS,60ML     51      0     0     9     -1     41     -41
HIORA K TOOTH PASTE,100GM           0     50     0    10      0     40     -40
Valuation (Rs.) =>  9162.50     0.00       5801.66     30917.02      -30917.02
"""

KAVERI = """
KAVERI AGENCIES
MONTHLY STOCK & SALES
PRD CODE ITEM NAME PACK OP PUR TOT SALE SVAL CL CVAL
1234 LIV 52 TAB 60*TAB 10 0 10 2 100.00 8 80.00
"""


class TestDailyStockSummaryDetection(unittest.TestCase):
    def test_detects_company_wise_summary_not_kaveri(self):
        self.assertTrue(_looks_like_daily_stock_summary_txt(SAMPLE))
        self.assertFalse(_looks_like_daily_stock_summary_txt(KAVERI))
        self.assertFalse(_looks_like_daily_stock_summary_txt(""))

    def test_maps_sales_qty_not_order_or_return(self):
        parsed = _parse_daily_stock_summary_txt(SAMPLE, "narayan.txt")
        self.assertIsNotNone(parsed)
        items = {item["product_name"]: item for item in parsed["line_items"]}
        soap = items["AACTARIL SOAP"]
        self.assertEqual(soap["packing"], "75GM")
        self.assertEqual(
            (soap["opening_qty"], soap["receipts_qty"], soap["sales_qty"], soap["closing_qty"]),
            (62.0, 0.0, 10.0, 52.0),
        )
        self.assertEqual((soap.get("extra") or {}).get("sales_return_qty"), 0.0)
        self.assertEqual((soap.get("extra") or {}).get("order_qty"), -52.0)
        clarina = items["CLARINA ANTI ACNE FACEWAS"]
        self.assertEqual(clarina["sales_qty"], 9.0)
        self.assertEqual(clarina["closing_qty"], 41.0)
        self.assertEqual((clarina.get("extra") or {}).get("adj_dmg_qty"), -1.0)
        hiora = items["HIORA K TOOTH PASTE"]
        self.assertEqual(hiora["receipts_qty"], 50.0)
        self.assertEqual(hiora["sales_qty"], 10.0)
        self.assertEqual(hiora["closing_qty"], 40.0)
        self.assertEqual(parsed["stockist_name"], "NARAYAN MEDICAL (NEW)")
        self.assertEqual(parsed["period_from"], "2026-08-01")
        self.assertEqual(parsed["period_to"], "2026-08-31")
        self.assertNotIn("02/09/2026", parsed["stockist_name"])

    def test_changed_period_splits_by_month(self):
        text = SAMPLE + """
NARAYAN MEDICAL (NEW)                                                02/10/2026
** Daily Stock Summary (Company-wise) **          From 01/09/2026 to 30/09/2026
Company:HIMALAYA ZANDRA [M178]
AACTARIL SOAP,75GM                 10      0     0     2      0      8      -8
ABANA TAB,60'S                      4      0     0     1      0      3      -3
LIV 52 TAB,60'S                     6      0     0     1      0      5      -5
"""
        parsed = _parse_daily_stock_summary_txt(text, "narayan.txt")
        self.assertTrue(parsed.get("multi_statement"))
        self.assertEqual(len(parsed["statements"]), 2)
        self.assertEqual(parsed["statements"][0]["period_from"], "2026-08-01")
        self.assertEqual(parsed["statements"][1]["period_from"], "2026-09-01")
        self.assertEqual(parsed["statements"][1]["period_to"], "2026-09-30")
        august = [item["product_name"] for item in parsed["statements"][0]["line_items"]]
        september = [item["product_name"] for item in parsed["statements"][1]["line_items"]]
        self.assertIn("HIORA K TOOTH PASTE", august)
        self.assertIn("ABANA TAB", september)
        self.assertNotIn("ABANA TAB", august)


@unittest.skipUnless(FIXTURE.is_file(), "missing Daily Stock Summary fixture")
class TestDailyStockSummaryFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.items = cls.result.get("line_items") or []
        cls.by_name = {}
        for item in cls.items:
            cls.by_name.setdefault(item["product_name"], item)

    def test_extracts_all_company_sections(self):
        extra = (self.result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "daily_stock_summary_txt")
        self.assertEqual(self.result["stockist_name"], "NARAYAN MEDICAL (NEW)")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertGreaterEqual(len(self.items), 100)
        self.assertNotIn("statements", self.result)
        companies = {
            (item.get("extra") or {}).get("company_name") for item in self.items
        }
        self.assertGreaterEqual(len(companies), 5)
        self.assertIn("AACTARIL SOAP", self.by_name)
        self.assertIn("RUMALAYA FORTE TAB", self.by_name)
        self.assertIn("CYSTONE TABS", self.by_name)
        self.assertIn("LIV.52 TABS", self.by_name)
        self.assertIn("SPEMAN TAB", self.by_name)
        self.assertIn("TENTEX ROYAL CAP", self.by_name)
        self.assertIn("TENTEX FORTE TABS", self.by_name)
        self.assertIn("TULASI SYP", self.by_name)
        self.assertNotIn("Invoices", self.result)
        sold = sum(1 for item in self.items if float(item.get("sales_qty") or 0) > 0)
        self.assertGreaterEqual(sold, 20)

    def test_kaveri_code_first_rows_unchanged(self):
        result = extract_sales_statement(KAVERI.encode("utf-8"), "kaveri.txt")
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertNotEqual(extra.get("extraction_method"), "daily_stock_summary_txt")


if __name__ == "__main__":
    unittest.main()
