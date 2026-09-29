"""SwilERP stacked Op.Bal + Rcpt Oth / Issue Oth / Shortage (9 qty columns)."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_swil_stacked_opbal_rcpt_oth_format,
    _parse_opbal_receipt_issue_statement,
    _parse_swil_stacked_opbal_rcpt_oth_statement,
    extract_sales_statement,
)


FIXTURE = Path(
    "/var/www/html/splitextract/"
    "0000713452_2026_08_ZL_17_5046_04092026142317.pdf"
)

SAMPLE = """
MEDICO
NYAMO LOTHA ROAD
Page No.1
Sales & Stock Statement(From 01/08/2026 Upto 27/08/2026)
Aug 27,2026
HIMALAYA ZEAL DIVISION
PRODUCT NAME         PACKING
   Op.Bal.
   Receipt
  Rcpt Oth
    Surplus
     Total
     Issue
 Issue Oth
   Shortage
   Closing
      Qty.
      Qty.
       Qty
       Qty.
      Qty.
      Qty.
       Qty
       Qty.
   Balance
CONFIDO TABLET       60'S
        89
         0
         0
          0
        89
         2
         0
          0
        87
LIV-52 LIQUID
200ML
       124
         0
         0
          0
       124
       119
         0
          0
         5
LIV-52 SYRUP
100ML
       203
         0
         0
          0
       203
       198
         0
          0
         5
GASEX TABLET
100'S
         0
         0
         0
          0
         0
         0
         0
          0
         0
TOTAL
    107043
         0
         0
          0
    107043
     66531
         0
          0
     46084
"""


BISWAS_5COL = """
M/S BISWAS MEDICINE AGENCY
Sales & Stock Statement(From 01/08/2026 Upto 31/08/2026)
HIMALAYA (ZANDRA)
PRODUCT NAME
PACKING
Op.Bal.
Receipt
Total
Issue
Closing
Qty.
Qty.
Qty.
Qty.
Balance
BONNISAN DROPS
30ML
94
0
94
26
68
BRESOL TAB
60'S
13
0
13
7
6
EVECARE FORTE LIQ 20
200 ML
17
0
17
4
13
"""


class TestSwilStackedOpbalRcptOth(unittest.TestCase):
    def test_gate_accepts_rcpt_oth_header(self):
        self.assertTrue(_is_swil_stacked_opbal_rcpt_oth_format(SAMPLE))
        self.assertFalse(_is_swil_stacked_opbal_rcpt_oth_format(BISWAS_5COL))

    def test_sample_rows_map_opening_issue_closing(self):
        result = _parse_swil_stacked_opbal_rcpt_oth_statement(
            SAMPLE, "medico.pdf", "pdf"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["stockist_name"], "MEDICO")
        self.assertEqual(result["company_name"], "HIMALAYA ZEAL DIVISION")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-27")
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "swil_stacked_opbal_rcpt_oth",
        )
        by_name = {i["product_name"]: i for i in result["line_items"]}
        confido = by_name["CONFIDO TABLET"]
        self.assertEqual(confido["packing"], "60'S")
        self.assertEqual(confido["opening_qty"], 89.0)
        self.assertEqual(confido["receipts_qty"], 0.0)
        self.assertEqual(confido["sales_qty"], 2.0)
        self.assertEqual(confido["closing_qty"], 87.0)
        liv = by_name["LIV-52 LIQUID"]
        self.assertEqual(liv["packing"], "200ML")
        self.assertEqual(liv["opening_qty"], 124.0)
        self.assertEqual(liv["sales_qty"], 119.0)
        self.assertEqual(liv["closing_qty"], 5.0)
        syrup = by_name["LIV-52 SYRUP"]
        self.assertEqual(syrup["opening_qty"], 203.0)
        self.assertEqual(syrup["sales_qty"], 198.0)
        self.assertEqual(syrup["closing_qty"], 5.0)

    def test_biswas_5col_not_stolen_by_swil_parser(self):
        self.assertIsNone(
            _parse_swil_stacked_opbal_rcpt_oth_statement(
                BISWAS_5COL, "biswas.pdf", "pdf"
            )
        )
        # Existing 5-col stacked OpBal path still works.
        opbal = _parse_opbal_receipt_issue_statement(
            BISWAS_5COL, "biswas.pdf", "pdf"
        )
        self.assertIsNotNone(opbal)
        self.assertTrue(opbal["totals"]["extra"].get("opbal_stacked_lines"))
        self.assertEqual(opbal["totals"]["extra"]["extraction_method"],
                         "opbal_issue_closing_parser")
        self.assertEqual(len(opbal["line_items"]), 3)

    def test_live_medico_pdf(self):
        if not FIXTURE.exists():
            self.skipTest("fixture PDF not present")
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        method = ((result.get("totals") or {}).get("extra") or {}).get(
            "extraction_method"
        )
        self.assertEqual(method, "swil_stacked_opbal_rcpt_oth")
        self.assertEqual(result.get("stockist_name"), "MEDICO")
        self.assertIn("HIMALAYA", str(result.get("company_name") or "").upper())
        self.assertEqual(result.get("period_from"), "2026-08-01")
        self.assertEqual(result.get("period_to"), "2026-08-27")
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 15)
        by_name = {i["product_name"]: i for i in items}
        self.assertEqual(by_name["CONFIDO TABLET"]["opening_qty"], 89.0)
        self.assertEqual(by_name["CONFIDO TABLET"]["sales_qty"], 2.0)
        self.assertEqual(by_name["CONFIDO TABLET"]["closing_qty"], 87.0)
        self.assertEqual(by_name["LIV-52 LIQUID"]["opening_qty"], 124.0)
        self.assertEqual(by_name["LIV-52 LIQUID"]["sales_qty"], 119.0)
        self.assertEqual(by_name["LIV-52 LIQUID"]["closing_qty"], 5.0)
        self.assertEqual(by_name["LIV-52 TABLETS"]["opening_qty"], 102.0)
        self.assertEqual(by_name["LIV-52 TABLETS"]["sales_qty"], 48.0)
        self.assertEqual(by_name["LIV-52 TABLETS"]["closing_qty"], 54.0)


if __name__ == "__main__":
    unittest.main()
