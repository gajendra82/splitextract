"""SwilERP 7-column Sales & Stock Statement (Op.Bal/Receipt/Retrn/Total/Issue/Retrn/Closing).

The two Retrn columns distinguish this from the 5-column Biswas/Mahajan stacked
layout handled by _parse_opbal_receipt_issue_statement, which must stay unchanged.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_swilerp_retrn_sales_stock_text,
    _parse_swilerp_retrn_sales_stock_pdf,
    extract_sales_statement,
)

# Stacked one-value-per-line layout, two Himalaya company sections.
SAMPLE = """                              B.P.PHARMACEUTICALS
                            Maitry Lane, Link Road
Page No.1
Sales & Stock Statement(From 01/08/2026 Upto 31/08/2026)
 Sep  1,2026
Himalaya (Hospital)
PRODUCT NAME        PACKING    Op.Bal.  Receipt  Retrn
   Total
   Issue  Retrn  Closing
    Qty.
    Qty.
   Qty
    Qty.
    Qty.
   Qty  Balance
OXITARD CAP          10'S
       9
       0
     0
       9
       9
     0
       0
CYSTONE SYP*         200ML
       5
       0
     0
       5
       3
     0
       2
TOTAL
     766
       0
     0
     766
     712
     0
       0
***
Himalaya (Zandra)
PRODUCT NAME        PACKING    Op.Bal.  Receipt  Retrn
   Total
   Issue  Retrn  Closing
    Qty.
    Qty.
   Qty
    Qty.
    Qty.
   Qty  Balance
BONNISAN DROP       30ML
      96
       0
     0
      96
      74
     0
      22
BRESOL SYP
100ML
       0
       0
     0
       0
       0
     0
       0
LIV52 DS TAB
60'S
       1
     200
     0
     201
     110
     0
      91
TOTAL
  162322
   59413
     0
  221735
  110523
     0
  103215
***
GRAND TOTAL
  399567
   96469
     0
  496036
  236886
     0
  242291
Powered By SwilERP for Retail, Distribution & Chain Stores
"""

# 5-column Biswas stacked layout — must NOT be claimed by the 7-column detector.
BISWAS_5COL = """M/S BISWAS MEDICINE AGENCY
HIMALAYA (ZANDRA)
Sales & Stock Statement(From 01/08/2026 Upto 31/08/2026)
PRODUCT NAME
PACKING
Op.Bal.
Receipt
Total
Issue
Closing
Qty.
Balance
BONNISAN DROPS
30ML
94
0
94
26
68
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


class TestSwilERPRetrnSalesStock(unittest.TestCase):
    def test_detector_requires_two_retrn_columns(self):
        self.assertTrue(_is_swilerp_retrn_sales_stock_text(SAMPLE))
        # 5-column Biswas layout has no Retrn columns -> not this format.
        self.assertFalse(_is_swilerp_retrn_sales_stock_text(BISWAS_5COL))

    def test_parses_all_products_and_maps_columns(self):
        doc = _FakeDoc([_FakePage(SAMPLE)])
        result = _parse_swilerp_retrn_sales_stock_pdf(doc, "bp.pdf")
        self.assertIsNotNone(result)

        # Gather items whether single or multi_statement.
        if result.get("multi_statement"):
            items = []
            for stmt in result["statements"]:
                items.extend(stmt.get("line_items") or [])
        else:
            items = result.get("line_items") or []

        by = {i["product_name"]: i for i in items}
        self.assertIn("OXITARD CAP", by)
        self.assertIn("CYSTONE SYP*", by)
        self.assertIn("BONNISAN DROP", by)
        self.assertIn("LIV52 DS TAB", by)
        # No TOTAL / GRAND TOTAL / header fragments leaked in as products.
        self.assertFalse(any("TOTAL" in n.upper() for n in by))

        oxi = by["OXITARD CAP"]
        self.assertEqual(oxi["packing"], "10'S")
        self.assertEqual(oxi["opening_qty"], 9.0)
        self.assertEqual(oxi["receipts_qty"], 0.0)
        self.assertEqual(oxi["sales_qty"], 9.0)   # Issue column
        self.assertEqual(oxi["closing_qty"], 0.0)  # Closing/Balance column

        cys = by["CYSTONE SYP*"]  # trailing * must not swallow the packing
        self.assertEqual(cys["packing"], "200ML")
        self.assertEqual(cys["opening_qty"], 5.0)
        self.assertEqual(cys["sales_qty"], 3.0)
        self.assertEqual(cys["closing_qty"], 2.0)

        liv = by["LIV52 DS TAB"]  # packing printed on its own line
        self.assertEqual(liv["packing"], "60'S")
        self.assertEqual(liv["opening_qty"], 1.0)
        self.assertEqual(liv["receipts_qty"], 200.0)
        self.assertEqual(liv["sales_qty"], 110.0)
        self.assertEqual(liv["closing_qty"], 91.0)

    def test_live_pdf(self):
        path = Path(
            r"C:\Users\anuja\Downloads\ZA_2026_August"
            r"\0000700229_2026_08_ZA_18_227_06092026070306.PDF"
        )
        if not path.exists():
            self.skipTest("fixture PDF not present")
        result = extract_sales_statement(path.read_bytes(), path.name)
        method = ((result.get("totals") or {}).get("extra") or {}).get(
            "extraction_method"
        )
        self.assertEqual(method, "swilerp_retrn_sales_stock")
        items = result.get("line_items") or []
        if result.get("multi_statement"):
            items = []
            for stmt in result["statements"]:
                items.extend(stmt.get("line_items") or [])
        self.assertGreaterEqual(len(items), 150)
        by = {}
        for it in items:
            by.setdefault(it["product_name"], []).append(it)
        self.assertIn("OXITARD CAP", by)
        oxi = by["OXITARD CAP"][0]
        self.assertEqual(oxi["opening_qty"], 9.0)
        self.assertEqual(oxi["sales_qty"], 9.0)
        self.assertEqual(oxi["closing_qty"], 0.0)


if __name__ == "__main__":
    unittest.main()
