"""Stacked Opening Stock / Sale Return / Total Receipt / Sale Quantity words."""

from __future__ import annotations

import os
import unittest

from services.stock_header_resolver import _pick_group
from services.stock_native_tables import _is_qualifier_text


class TestStackedOpeningSaleHeaders(unittest.TestCase):
    def test_second_line_qualifiers(self):
        for word in ("Stock", "Quantity", "Return", "Receipt", "Cost", "Value"):
            self.assertTrue(_is_qualifier_text(word), word)

    def test_total_receipt_is_total_not_purchase(self):
        group, _conf, reason = _pick_group("total receipt")
        self.assertEqual(group, "total")
        self.assertIn("total receipt", reason)

    def test_purchase_cost_is_rate(self):
        group, _conf, _reason = _pick_group("purchase cost")
        self.assertEqual(group, "rate")

    def test_sale_return_stays_return(self):
        group, _conf, _reason = _pick_group("sale return")
        self.assertEqual(group, "sales_return")


class TestJsPharmaceuticalPdf(unittest.TestCase):
    PDF = os.path.join(
        os.path.dirname(__file__),
        "..",
        "debug_uploads",
        "69b1d67d1de2e5e4_j_s_pharmaceutical_stat_saptember.PDF",
    )

    @unittest.skipUnless(os.path.isfile(PDF), "fixture PDF not present")
    def test_sale_is_not_closing(self):
        from services.stock_native_resolver import parse_pdf_free_column_statement

        with open(self.PDF, "rb") as handle:
            data = handle.read()
        result = parse_pdf_free_column_statement(
            data, "j s pharmaceutical stat saptember.PDF"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["stockist_name"], "J S PHARMACUTICALS")
        self.assertEqual(result["company_name"], "ZEAL HIMALAYA")
        self.assertEqual(result["period_from"], "2026-09-01")
        self.assertEqual(result["period_to"], "2026-09-30")
        rows = {item["product_name"]: item for item in result["line_items"]}
        aact = rows["AACTARIL SOAP"]
        self.assertEqual(aact["opening_qty"], 52)
        self.assertEqual(aact["sales_qty"], 3)
        self.assertEqual(aact["closing_qty"], 49)
        self.assertEqual(aact["closing_value"], 3311.42)
        gasex = rows["GASEX TABLETS"]
        self.assertEqual(gasex["opening_qty"], 2)
        self.assertEqual(gasex["receipts_qty"], 100)
        self.assertEqual(gasex["sales_qty"], 16)
        self.assertEqual(gasex["closing_qty"], 86)
        self.assertNotIn("[ Software by: PKS Data,", rows)


if __name__ == "__main__":
    unittest.main()
