"""Nu-Life Stock & Sales Report — Opstk/Sale/CurStk/StkVal/SalVal (no value swap)."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_stock_and_sales_report_month_text,
    _is_stock_sales_opstk_curstk_stkval_text,
    _parse_stock_sales_opstk_curstk_stkval_statement,
    extract_sales_statement,
)


FIXTURE = Path(
    "/var/www/html/splitextract/"
    "0000711168_2026_08_ZL_22_419_05092026045733.pdf"
)

SAMPLE = """
NU-LIFE AGENCY
THOOTHUKUDI - 628002
Date: 05-09-2026
Stock & Sales Report From 01/09/2026 To 05/09/2026
Company Name : HIMALAYA-ZEAL - .
===============================================================================
No   Product Name              Pack    Opstk       Pur PFree  PurRet       Jul   Aug      Sale CurStk    StkVal OrdQt    SalVal Exp Age Last PDate
===============================================================================
1   LIV.52 SYRUP  200ML       200ML       9         -     -       -        15    13         3      6    995.70    -2    524.14   -   3 07/07/2026
2   LIV.52 SYRUP 100ML        100ML      36         -     -       -        34    10         2     34   3375.52   -31    204.79   -   4 07/07/2026
3   LIV.52 TAB 100            5X100      58         -     -       -        22    23         4     54   8172.90   -48    663.78   -   1 07/07/2026
4   PILEX FORTE OIN           30GM       13         -     -       -        26    12         1     12   1161.60   -10    114.28   -   4          -
5   PILEX TAB 60              60S        34         -     -       -        16    26         6     28   4646.60   -19   1041.16   -   2 12/05/2026
-------------------------------------------------------------------------------
    Total                               150         -     -       -       113    84        16    134         -     -         -   -   -          -
-------------------------------------------------------------------------------
Opg.Stk Val:    20746.63 Purc.Value: :        0.00 Sales Value::     2548.14
Cl.Stk Val:     18352.32 Pur.RetVal ::        0.00 SalRet Val: :        0.00
"""

SRINATH_MONTH = """
SRINATH MEDICAL AGENCIES
Stock And Sales Report(Month)
of Aug2026
ProductName                   Pack OpStk  Rcpt  Sales Fre SalesValue Adj Cl.Stk NetStockVaStockValue
ARJUNA TABS                    60C   206           49       12149.85        157   33560.75  36086.88
"""


class TestStockSalesOpstkCurstkStkval(unittest.TestCase):
    def test_gate_accepts_nulife_not_srinath_month(self):
        self.assertTrue(_is_stock_sales_opstk_curstk_stkval_text(SAMPLE))
        self.assertFalse(_is_stock_and_sales_report_month_text(SAMPLE))
        self.assertFalse(_is_stock_sales_opstk_curstk_stkval_text(SRINATH_MONTH))

    def test_salval_is_sales_value_stkval_is_closing_value(self):
        result = _parse_stock_sales_opstk_curstk_stkval_statement(
            SAMPLE, "nulife.pdf", "pdf"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["stockist_name"], "NU-LIFE AGENCY")
        self.assertIn("HIMALAYA", str(result.get("company_name") or "").upper())
        self.assertEqual(result["period_from"], "2026-09-01")
        self.assertEqual(result["period_to"], "2026-09-05")
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "stock_sales_opstk_curstk_stkval",
        )
        self.assertEqual(result["totals"]["sales_value"], 2548.14)
        self.assertEqual(result["totals"]["closing_value"], 18352.32)
        by_name = {i["product_name"]: i for i in result["line_items"]}
        liv = by_name["LIV.52 SYRUP 200ML"]
        self.assertEqual(liv["opening_qty"], 9.0)
        self.assertEqual(liv["sales_qty"], 3.0)
        self.assertEqual(liv["closing_qty"], 6.0)
        # Critical: SalVal -> sales_value, StkVal -> closing_value (not swapped)
        self.assertEqual(liv["sales_value"], 524.14)
        self.assertEqual(liv["closing_value"], 995.70)
        liv100 = by_name["LIV.52 SYRUP 100ML"]
        self.assertEqual(liv100["sales_qty"], 2.0)
        self.assertEqual(liv100["closing_qty"], 34.0)
        self.assertEqual(liv100["sales_value"], 204.79)
        self.assertEqual(liv100["closing_value"], 3375.52)
        tab = by_name["LIV.52 TAB 100"]
        self.assertEqual(tab["sales_qty"], 4.0)
        self.assertEqual(tab["closing_qty"], 54.0)
        self.assertEqual(tab["sales_value"], 663.78)
        self.assertEqual(tab["closing_value"], 8172.90)

    def test_live_nulife_pdf(self):
        if not FIXTURE.exists():
            self.skipTest("fixture PDF not present")
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        method = ((result.get("totals") or {}).get("extra") or {}).get(
            "extraction_method"
        )
        self.assertEqual(method, "stock_sales_opstk_curstk_stkval")
        items = result.get("line_items") or []
        self.assertEqual(len(items), 5)
        by_name = {i["product_name"]: i for i in items}
        liv = by_name["LIV.52 SYRUP 200ML"]
        self.assertEqual(liv["sales_value"], 524.14)
        self.assertEqual(liv["closing_value"], 995.70)
        self.assertNotEqual(liv["sales_value"], liv["closing_value"])
        # Must not use Zandra vision path that swapped these columns.
        self.assertNotEqual(method, "zandra_stock_sale_vision")


if __name__ == "__main__":
    unittest.main()
