"""COMPANY WISE STOCK / STOCK REPORT — SNO ITEM DESCRIPTION PACK OB PUR SAL CB."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_company_wise_stock_ob_pur_text,
    _parse_company_wise_stock_ob_pur,
    extract_sales_statement,
)

SAMPLE = """
NILAKANTHA AUSADHALAYA
AT/PO-NISCHINTAKOILI,CUTTACK ODISHA GST:21AQEPS9821K1ZK
STOCK REPORTFROM DATE:01-08-2026 TO 31-08-2026  Pg:1
--------------------------------------------------------------------------------
SNO ITEM DESCRIPTION PACK        OB    PUR     FR    SAL    FR     CB  EXPR      RATE    VALUE
--------------------------------------------------------------------------------
COMPANY NAME:HIMALAYA WELLNEESS COMPANY  CODE: HIA
---------------------------------------
1   BONISAN DROP      30 ML       0      0     0      0     0      0     42.66
2   BONNISAN SYP      200ML     280      0     0    144     4    132   12-28    101.31  13372.92
3   BONNISAN-SYP      120 ML      0      0     0      0     0      0     33.52
4   BRESOL SYP        200ML      16      0     0      0     0     16   09-28    145.21   2323.36
6   CONFIDO TAB       60S        98      0     0      2     0     96   05-27    164.14  15757.44
14  HIORA-K TOOTHPASTE100GM      50      0     0      4     0     46   04-29    108.06   4970.76
         TOTAL VAL.   118970.19       0.00   19984.10                98580.85
COMPANY NAME:HIMALAYA  CODE: HIM
1   AACTARIL-SOAP     75GM        0      0     0      0     0      0     46.28
2   ASHVAGANDHA-TAB   60S        30      0     0      1     0     29   11-27    162.93   4724.97
         TOTAL VAL.   224959.17       0.00   60057.24               164901.93
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


class TestCompanyWiseStockObPur(unittest.TestCase):
    def test_detector(self):
        self.assertTrue(_is_company_wise_stock_ob_pur_text(SAMPLE))
        self.assertFalse(
            _is_company_wise_stock_ob_pur_text(
                "ITEM DESCRIPTION PACK OPENING RECEIPT ISSUE CLOSING M.EXP\n"
            )
        )

    def test_skips_address_and_strips_serial(self):
        doc = _FakeDoc([_FakePage(SAMPLE)])
        result = _parse_company_wise_stock_ob_pur(doc, "nila.pdf")
        self.assertIsNotNone(result)
        self.assertTrue(result.get("multi_statement"))
        self.assertEqual(result["statement_count"], 2)
        stmts = result["statements"]
        self.assertEqual(stmts[0]["stockist_name"], "NILAKANTHA AUSADHALAYA")
        self.assertIn("NISCHINTAKOILI", stmts[0]["stockist_address"] or "")
        self.assertEqual(stmts[0]["period_from"], "2026-08-01")
        self.assertEqual(stmts[0]["period_to"], "2026-08-31")
        self.assertEqual(stmts[0]["company_name"], "HIMALAYA WELLNEESS COMPANY")
        names = [i["product_name"] for i in stmts[0]["line_items"]]
        self.assertNotIn(
            "AT/PO-NISCHINTAKOILI,CUTTACK ODISHA GST:21AQEPS9821K1ZK", names
        )
        self.assertFalse(any("STOCK REPORT" in n for n in names))
        self.assertFalse(any(re_match_sno(n) for n in names))
        by = {i["product_name"]: i for i in stmts[0]["line_items"]}
        bonn = by["BONNISAN SYP"]
        self.assertEqual(bonn["packing"], "200ML")
        self.assertEqual(bonn["opening_qty"], 280.0)
        self.assertEqual(bonn["receipts_qty"], 0.0)
        self.assertEqual(bonn["sales_qty"], 144.0)
        self.assertEqual(bonn["closing_qty"], 132.0)
        self.assertEqual(bonn["closing_value"], 13372.92)
        conf = by["CONFIDO TAB"]
        self.assertEqual(conf["opening_qty"], 98.0)
        self.assertEqual(conf["sales_qty"], 2.0)
        self.assertEqual(conf["closing_qty"], 96.0)
        tooth = by["HIORA-K TOOTHPASTE"]
        self.assertEqual(tooth["packing"], "100GM")
        self.assertEqual(tooth["opening_qty"], 50.0)
        self.assertEqual(tooth["sales_qty"], 4.0)
        self.assertEqual(tooth["closing_qty"], 46.0)
        self.assertEqual(stmts[1]["company_name"], "HIMALAYA")
        self.assertEqual(len(stmts[1]["line_items"]), 2)
        ash = stmts[1]["line_items"][1]
        self.assertEqual(ash["product_name"], "ASHVAGANDHA-TAB")
        self.assertEqual(ash["opening_qty"], 30.0)
        self.assertEqual(ash["sales_qty"], 1.0)
        self.assertEqual(ash["closing_qty"], 29.0)

    def test_live_pdf(self):
        path = Path(
            "/var/www/html/splitextract/"
            "0000717098_2026_08_ZL_18_375_04092026174806.pdf"
        )
        if not path.exists():
            self.skipTest("fixture PDF not present")
        result = extract_sales_statement(path.read_bytes(), path.name)
        # Same stockist+month sections are merged by the existing grouper.
        method = ((result.get("totals") or {}).get("extra") or {}).get(
            "extraction_method"
        )
        self.assertEqual(method, "company_wise_stock_ob_pur")
        self.assertEqual(result["stockist_name"], "NILAKANTHA AUSADHALAYA")
        items = result.get("line_items") or []
        if result.get("multi_statement"):
            items = []
            for stmt in result["statements"]:
                items.extend(stmt.get("line_items") or [])
        self.assertGreaterEqual(len(items), 100)
        all_names = [it.get("product_name") or "" for it in items]
        self.assertFalse(any("STOCK REPORT" in n.upper() for n in all_names))
        self.assertFalse(any("AT/PO" in n.upper() for n in all_names))
        self.assertFalse(any("GST:" in n.upper() for n in all_names))
        self.assertFalse(any(re_match_sno(n) for n in all_names))
        by = {n: it for n, it in zip(all_names, items)}
        self.assertIn("BONNISAN SYP", by)
        self.assertEqual(by["BONNISAN SYP"]["opening_qty"], 280.0)
        self.assertEqual(by["BONNISAN SYP"]["sales_qty"], 144.0)
        self.assertEqual(by["BONNISAN SYP"]["closing_qty"], 132.0)
        self.assertIn("CONFIDO TAB", by)
        self.assertEqual(by["CONFIDO TAB"]["opening_qty"], 98.0)
        self.assertEqual(by["CONFIDO TAB"]["sales_qty"], 2.0)
        self.assertEqual(by["CONFIDO TAB"]["closing_qty"], 96.0)

def re_match_sno(name: str) -> bool:
    import re

    return bool(re.match(r"^\d+\s+\S", name or ""))


if __name__ == "__main__":
    unittest.main()
