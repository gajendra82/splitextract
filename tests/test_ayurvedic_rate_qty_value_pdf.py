"""RATE + OPENING/RECEIPT/ISSUE/CLOSING PDF must not use the no-RATE SSA parser.

That wrong path put unit rate into packing and skipped AYURVEDIC SOLUTION POINT.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_ssa_opening_receipt_issue_value_text,
    extract_sales_statement,
)


PDF = Path(
    "/var/www/html/splitextract/0000735013_2026_08_ZL_17_5046_04092026140537.PDF"
)

RATE_HEADER = (
    "AYURVEDIC SOLUTION POINT\n"
    "STOCK & SALES ANALYSIS  (HIMALAYA WELLNESS COMPANY) 01-08-2026 - 31-08-2026\n"
    "ITEM DESCRIPTION                        RATE         OPENING              RECEIPT\n"
    "QTY.       VALUE     QTY.       VALUE\n"
)

NO_RATE_HEADER = (
    "PRAKASH MEDICAL STORE\n"
    "STOCK & SALES ANALYSIS 01/08/2026 - 31/08/2026\n"
    "ITEM DESCRIPTION  OPENING  RECEIPT  ISSUE  CLOSING  DUMP\n"
    "QTY. VALUE QTY. VALUE QTY. VALUE QTY. VALUE QTY.\n"
)


class TestRateSheetNotClaimedBySsaOri(unittest.TestCase):
    def test_rate_header_stays_off_ssa_ori(self):
        self.assertFalse(_is_ssa_opening_receipt_issue_value_text(RATE_HEADER))
        self.assertTrue(_is_ssa_opening_receipt_issue_value_text(NO_RATE_HEADER))


@unittest.skipUnless(PDF.is_file(), "missing Ayurvedic Solution Point PDF")
class TestAyurvedicRatePdf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(PDF.read_bytes(), PDF.name)

    def test_stockist_and_packing_not_rate(self):
        self.assertEqual(self.result["stockist_name"], "AYURVEDIC SOLUTION POINT")
        self.assertIn("HIMALAYA", self.result["company_name"] or "")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertEqual(
            (self.result.get("totals") or {}).get("extra", {}).get("extraction_method"),
            "rate_qty_value_columns",
        )
        aactaril = next(
            item
            for item in self.result["line_items"]
            if "AACTARIL SOAP 75GM" in (item.get("product_name") or "").upper()
        )
        self.assertEqual(aactaril["packing"], "75GM")
        self.assertNotEqual(aactaril["packing"], "72.13")
        self.assertEqual(aactaril["extra"].get("unit_rate"), 72.13)
        self.assertEqual(aactaril["opening_qty"], 23.0)
        self.assertAlmostEqual(aactaril.get("opening_value"), 1658.92, places=2)
        self.assertEqual(aactaril["sales_qty"], 4.0)
        self.assertAlmostEqual(aactaril["sales_value"], 347.42, places=2)
        self.assertEqual(aactaril["closing_qty"], 19.0)
