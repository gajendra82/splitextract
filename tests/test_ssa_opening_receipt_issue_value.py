"""STOCK & SALES ANALYSIS Opening/Receipt/Issue/Closing qty+value + DUMP."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_order_form_stock_statement_text,
    _is_saleable_stock_report_text,
    _is_ssa_opening_receipt_issue_value_text,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000736167_2026_08_ZA_24_8137_03092026173805.pdf"
)

HEADER = (
    "PRAKASH MEDICAL STORE\n"
    "STOCK & SALES ANALYSIS 01/08/2026 - 31/08/2026\n"
    "ITEM DESCRIPTION  OPENING  RECEIPT  ISSUE  CLOSING  DUMP\n"
    "QTY. VALUE QTY. VALUE QTY. VALUE QTY. VALUE QTY.\n"
)

BUSY = (
    "STOCK & SALES ANALYSIS\n"
    "Item Description Opening Purchases Return Others Total Sales Closing Rate\n"
)

SALEABLE = (
    "Saleable Stock Report\n"
    "Particular | | Opn | Rec | Issue | Bal\n"
)


class TestSsaOriDetection(unittest.TestCase):
    def test_detects_receipt_issue_dump_not_other_layouts(self):
        self.assertTrue(_is_ssa_opening_receipt_issue_value_text(HEADER))
        self.assertFalse(_is_ssa_opening_receipt_issue_value_text(BUSY))
        self.assertFalse(_is_ssa_opening_receipt_issue_value_text(SALEABLE))
        self.assertFalse(_is_ssa_opening_receipt_issue_value_text(""))
        self.assertFalse(_is_saleable_stock_report_text(HEADER))
        self.assertFalse(_is_order_form_stock_statement_text(HEADER))


@unittest.skipUnless(FIXTURE.is_file(), "missing Prakash SSA fixture")
class TestSsaOriFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            str(item.get("product_name") or "").upper(): item
            for item in cls.result["line_items"]
        }
        cls.names = [
            str(item.get("product_name") or "") for item in cls.result["line_items"]
        ]

    def test_uses_ssa_ori_parser(self):
        self.assertEqual(
            self.extra.get("extraction_method"), "ssa_opening_receipt_issue_value"
        )
        self.assertEqual(self.result["stockist_name"], "PRAKASH MEDICAL STORE")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertGreaterEqual(len(self.result["line_items"]), 28)
        self.assertNotIn("Invoices", self.result)

    def test_totals_and_purchase_detail_are_not_products(self):
        joined = " | ".join(self.names).upper()
        self.assertNotIn("TOTAL", joined)
        self.assertNotIn("PURCHASE DETAIL", joined)
        self.assertNotIn("HIMALAYA WELLNESS COMPNY", joined)
        self.assertNotIn("HIMALAYS WELLNESS COMPANY", joined)
        self.assertFalse(any(name.upper() in {"HIMALYA", "HIMALAYA"} for name in self.names))

    def test_sample_rows_and_dash_zero(self):
        liv = next(
            item
            for item in self.result["line_items"]
            if str(item.get("product_name") or "").upper().startswith("LIV 52 DS SYP")
        )
        self.assertEqual(liv["opening_qty"], 140.0)
        self.assertEqual(liv["receipts_qty"], 0.0)
        self.assertEqual(liv["sales_qty"], 70.0)
        self.assertEqual(liv["sales_value"], 16160.46)
        self.assertEqual(liv["closing_qty"], 70.0)
        self.assertEqual(liv["closing_value"], 16901.85)
        self.assertEqual(liv["extra"]["opening_value"], 33803.70)

        lukol = self.by_name["LUKOL TAB"]
        self.assertEqual(lukol["opening_qty"], 0.0)
        self.assertEqual(lukol["receipts_qty"], 50.0)
        self.assertEqual(lukol["extra"]["receipts_value"], 8544.00)
        self.assertEqual(lukol["sales_qty"], 0.0)
        self.assertEqual(lukol["closing_qty"], 50.0)

        confido = next(
            item
            for item in self.result["line_items"]
            if "CONFIDO" in str(item.get("product_name") or "").upper()
        )
        self.assertEqual(confido["packing"], "1CC")
        self.assertEqual(confido["opening_qty"], 100.0)
        self.assertEqual(confido["receipts_qty"], 100.0)
        self.assertEqual(confido["closing_qty"], 200.0)

    def test_printed_grand_totals(self):
        self.assertEqual(self.result["totals"].get("sales_value"), 94830.56)
        self.assertEqual(self.result["totals"].get("closing_value"), 430534.70)
        self.assertEqual(self.extra.get("opening_value"), 263435.48)
        self.assertEqual(self.extra.get("receipts_value"), 253268.57)
        self.assertNotEqual(self.result["totals"].get("sales_value"), 385495.67)


if __name__ == "__main__":
    unittest.main()
