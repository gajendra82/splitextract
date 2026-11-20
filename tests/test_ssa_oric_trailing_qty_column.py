"""Qty-only ORIC with unlabeled trailing qty after CLOSING.

Layout family: STOCK & SALES ANALYSIS OPENING|RECEIPT|ISSUE|CLOSING + optional
5th unlabeled digit (LMS/order). Must not shift ISSUE/CLOSING into the trail,
and must peel spaced packs like 60 'S / 3 *60.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.sales_statement_extractor import _parse_ps_pharma_statement


BATHEJA_TRAILING = """
BATHEJA AGENCIES
55-56, DAWA BAZAR, CIVIC CENTER
JABALPUR, M.P. - 482002
STOCK & SALES ANALYSIS (HIMALYA ZINDEL(PURE HERBS)) 01-09-2026 - 30-09-2026
ITEM DESCRIPTION OPENING RECEIPT ISSUE CLOSING
HIMALYA ZINDEL(PURE HERBS)
ASHWAGANDHA TAB 60TAB 125 - 26 99 26
AYURSLIM CAP 60 CAP 13 - 3 10 8
AYURSLIM CAP OFFER PAC 3 *60 38 - 1 37 0
BRAHMI 60 'S 7 - 4 3 11
BRAHMI SYP 200 ML - - - - 0
GASEX TAB 100's 412 4000 4020 392 521
GOKSHURA TAB 60TAB 43 10 3 50 1
TENTEX ROYAL CAP 10 's 231 - 9 222 5
TULSI TAB 60 's 19 - - 19 1
TOTAL 220394 553081 571023 206653 95K
"""


class TestSsaOricTrailingQtyColumn(unittest.TestCase):
    def test_trailing_column_does_not_shift_oric(self):
        result = _parse_ps_pharma_statement(BATHEJA_TRAILING, "batheja.jpg")
        self.assertIsNotNone(result)
        by_name = {item["product_name"]: item for item in result["line_items"]}

        ash = by_name["ASHWAGANDHA TAB"]
        self.assertEqual(ash["packing"], "60TAB")
        self.assertEqual(ash["opening_qty"], 125.0)
        self.assertEqual(ash["receipts_qty"], 0.0)
        self.assertEqual(ash["sales_qty"], 26.0)
        self.assertEqual(ash["closing_qty"], 99.0)
        self.assertEqual((ash.get("extra") or {}).get("trailing_qty"), 26.0)

        offer = by_name["AYURSLIM CAP OFFER PAC"]
        self.assertEqual(offer["packing"], "3*60")
        self.assertEqual(offer["opening_qty"], 38.0)
        self.assertEqual(offer["receipts_qty"], 0.0)
        self.assertEqual(offer["sales_qty"], 1.0)
        self.assertEqual(offer["closing_qty"], 37.0)

        brahmi = by_name["BRAHMI"]
        self.assertIsNotNone(brahmi.get("packing"))
        self.assertIn("60", str(brahmi.get("packing")))
        self.assertEqual(brahmi["opening_qty"], 7.0)
        self.assertEqual(brahmi["receipts_qty"], 0.0)
        self.assertEqual(brahmi["sales_qty"], 4.0)
        self.assertEqual(brahmi["closing_qty"], 3.0)
        self.assertEqual((brahmi.get("extra") or {}).get("trailing_qty"), 11.0)

        tulsi = by_name["TULSI TAB"]
        self.assertEqual(tulsi["opening_qty"], 19.0)
        self.assertEqual(tulsi["receipts_qty"], 0.0)
        self.assertEqual(tulsi["sales_qty"], 0.0)
        self.assertEqual(tulsi["closing_qty"], 19.0)
        self.assertEqual((tulsi.get("extra") or {}).get("trailing_qty"), 1.0)

        tentex = by_name["TENTEX ROYAL CAP"]
        self.assertEqual(tentex["opening_qty"], 231.0)
        self.assertEqual(tentex["sales_qty"], 9.0)
        self.assertEqual(tentex["closing_qty"], 222.0)
        self.assertNotIn("TENTEX FORTE", by_name)

        # Qty-only sheet: no sales_value column — do not invent money.
        self.assertEqual(ash.get("sales_value"), 0.0)
        totals = result.get("totals") or {}
        self.assertEqual(totals.get("opening_qty"), 220394.0)
        self.assertEqual(totals.get("receipts_qty"), 553081.0)
        self.assertEqual(totals.get("sales_qty"), 571023.0)
        self.assertEqual(totals.get("closing_qty"), 206653.0)

    def test_flag_off_keeps_prior_four_token_path(self):
        with patch.dict(os.environ, {"STOCK_SSA_ORIC_TRAILING_QTY_COLUMN": "false"}):
            # Without pack peel on 60 'S, prior path absorbed opening into name.
            # Flag off + no pack still requires pack for row5; BRAHMI regresses.
            result = _parse_ps_pharma_statement(
                "STOCK & SALES ANALYSIS\n"
                "ITEM DESCRIPTION OPENING RECEIPT ISSUE CLOSING\n"
                "ASHWAGANDHA TAB 60TAB 125 - 26 99 26\n"
                "TOTAL 1 2 3 4\n",
                "t.jpg",
            )
        self.assertIsNotNone(result)
        ash = result["line_items"][0]
        self.assertEqual(ash["product_name"], "ASHWAGANDHA TAB")
        self.assertEqual(ash["closing_qty"], 99.0)


if __name__ == "__main__":
    unittest.main()
