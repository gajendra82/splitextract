"""SwilERP stacked Op.Bal + Expiry/Breakage + Near (7 qty columns)."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_swil_stacked_opbal_expiry_near_format,
    _is_swil_stacked_opbal_rcpt_oth_format,
    _parse_opbal_receipt_issue_statement,
    _parse_swil_stacked_opbal_expiry_near_statement,
    extract_sales_statement,
)


FIXTURE = Path(
    "/var/www/html/splitextract/"
    "0000737772_2026_08_ZL_03_9262_07092026034326.PDF"
)

SAMPLE = """
MEDIKING
HOSPITAL ROAD
Page No.1
Sales & Stock Statement(From 01/08/2026 Upto 31/08/2026)
Sep  4,2026
HIMALAYA ZEAL
PRODUCT NAME         PACKING
 Op.Bal.
 Receipt
   Total
   Issue
   Expiry
 Closing
     Near
    Qty.
    Qty.
    Qty.
    Qty.
 Breakage  Balance
   Expiry
CONFIDO TAB
1X60
       0
       3
       3
       2
        0
       1
        0
LIV 52 100ML SYP
100ML
      34
     280
     314
      50
        0
     264
        0
PILEX TAB
60S
       4
       0
       4
       3
        0
       1
        0
TOTAL
   16573
   71483
   88056
   16424
        0
   71632
        0
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


class TestSwilStackedOpbalExpiryNear(unittest.TestCase):
    def test_gate_accepts_expiry_near_not_biswas_or_rcpt_oth(self):
        self.assertTrue(_is_swil_stacked_opbal_expiry_near_format(SAMPLE))
        self.assertFalse(_is_swil_stacked_opbal_rcpt_oth_format(SAMPLE))
        self.assertFalse(_is_swil_stacked_opbal_expiry_near_format(BISWAS_5COL))

    def test_sample_maps_issue_and_closing_not_expiry(self):
        result = _parse_swil_stacked_opbal_expiry_near_statement(
            SAMPLE, "mediking.pdf", "pdf"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["stockist_name"], "MEDIKING")
        self.assertEqual(result["company_name"], "HIMALAYA ZEAL")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        by_name = {i["product_name"]: i for i in result["line_items"]}
        conf = by_name["CONFIDO TAB"]
        self.assertEqual(conf["packing"], "1X60")
        self.assertEqual(conf["opening_qty"], 0.0)
        self.assertEqual(conf["receipts_qty"], 3.0)
        self.assertEqual(conf["sales_qty"], 2.0)
        self.assertEqual(conf["closing_qty"], 1.0)
        self.assertEqual(conf["extra"]["expiry_breakage_qty"], 0.0)
        liv = by_name["LIV 52 100ML SYP"]
        self.assertEqual(liv["opening_qty"], 34.0)
        self.assertEqual(liv["receipts_qty"], 280.0)
        self.assertEqual(liv["sales_qty"], 50.0)
        self.assertEqual(liv["closing_qty"], 264.0)

    def test_biswas_5col_still_uses_opbal_parser(self):
        self.assertIsNone(
            _parse_swil_stacked_opbal_expiry_near_statement(
                BISWAS_5COL, "biswas.pdf", "pdf"
            )
        )
        opbal = _parse_opbal_receipt_issue_statement(BISWAS_5COL, "biswas.pdf", "pdf")
        self.assertIsNotNone(opbal)
        self.assertEqual(
            opbal["totals"]["extra"]["extraction_method"],
            "opbal_issue_closing_parser",
        )
        self.assertEqual(len(opbal["line_items"]), 3)

    def test_live_mediking_pdf_all_products(self):
        if not FIXTURE.exists():
            self.skipTest("fixture PDF not present")
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        method = ((result.get("totals") or {}).get("extra") or {}).get(
            "extraction_method"
        )
        self.assertEqual(method, "swil_stacked_opbal_expiry_near")
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 24)
        by_name = {i["product_name"]: i for i in items}
        self.assertIn("CONFIDO TAB", by_name)
        self.assertIn("LIV 52 TAB", by_name)
        self.assertEqual(by_name["LIV 52 100ML SYP"]["closing_qty"], 264.0)
        self.assertEqual(by_name["CONFIDO TAB"]["closing_qty"], 1.0)
        # Gemini fallback must not replace this native parse.
        self.assertEqual(
            ((result.get("totals") or {}).get("extra") or {}).get("gemini_fallback"),
            "not_called",
        )


if __name__ == "__main__":
    unittest.main()
