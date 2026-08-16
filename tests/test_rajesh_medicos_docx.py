"""RAJESH MEDICOS Stock-and-Sales Detail DOCX extraction checks."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _map_stock_sales_detail_headers,
    _unglue_stock_sales_label,
    extract_sales_statement,
)

SAMPLE = Path(
    r"C:\Users\gajen\Downloads\RE_ Discussion on E‑Detailing Solution _ Microsoft Teams Meeting\RAJESH MEDICOSE.docx"
)

EXPECTED_EXTRA = {
    "sl_no",
    "opening_value",
    "purchase_scheme",
    "purchase_value",
    "sales_scheme",
    "break_sales_qty",
    "break_sales_value",
    "credit_qty",
    "credit_scheme_qty",
    "credit_value",
    "debit_qty",
    "debit_scheme_qty",
    "debit_value",
}


class TestStockSalesDetailHeaders(unittest.TestCase):
    def test_maps_all_twenty_columns(self):
        headers = [
            "Sl.No", "Item", "Op.Qty", "Op.Val", "P.Qty", "P.Sch", "P.Val",
            "S.Qty", "S.Sch", "S.Val", "Br.S.Qty", "Br.S.Val", "Cr.Qty",
            "Cr.Sch.Qty", "Cr.Val", "Db.Qty", "Db.Sch.Qty", "Db.Val",
            "Cl.Qty", "Cl.Val",
        ]
        colmap = _map_stock_sales_detail_headers(headers)
        self.assertEqual(colmap["product_name"], 1)
        self.assertEqual(colmap["credit_scheme_qty"], 13)
        self.assertEqual(colmap["debit_scheme_qty"], 16)
        self.assertEqual(colmap["closing_value"], 19)

    def test_unglue_labels(self):
        self.assertEqual(
            _unglue_stock_sales_label("AUROBINDOPHARMALTD"),
            "AUROBINDO PHARMA LTD",
        )
        self.assertEqual(
            _unglue_stock_sales_label("RAJESHMEDICOS-BALAGHAT"),
            "RAJESH MEDICOS - BALAGHAT",
        )


@unittest.skipUnless(SAMPLE.exists(), "RAJESH MEDICOSE.docx not on this machine")
class TestRajeshMedicosDocx(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(
            SAMPLE.read_bytes(), "RAJESH MEDICOSE.docx"
        )

    def test_metadata(self):
        self.assertEqual(self.result["report_title"], "Stock and Sales Detail Report")
        self.assertEqual(self.result["stockist_name"], "RAJESH MEDICOS - BALAGHAT")
        self.assertEqual(self.result["company_name"], "AUROBINDO PHARMA LTD")
        self.assertEqual(self.result["period_from"], "2026-06-01")
        self.assertEqual(self.result["period_to"], "2026-06-30")

    def test_all_items_and_closing_match(self):
        items = self.result["line_items"]
        self.assertEqual(len(items), 33)
        self.assertEqual(self.result["totals"]["closing_value"], 227901.78)
        self.assertEqual(self.result["totals"]["sales_value"], 130404.04)
        self.assertTrue(
            self.result["totals"]["extra"].get("closing_value_matches_lines")
        )
        self.assertEqual(
            self.result["totals"]["extra"].get("line_closing_value_sum"), 227901.78
        )

    def test_every_column_captured(self):
        item = next(i for i in self.result["line_items"] if i["product_name"].startswith("ANDIAL"))
        self.assertEqual(item["opening_qty"], 800.0)
        self.assertEqual(item["receipts_qty"], 500.0)
        self.assertEqual(item["sales_qty"], 681.0)
        self.assertEqual(item["sales_value"], 12571.26)
        self.assertEqual(item["closing_qty"], 594.0)
        self.assertEqual(item["closing_value"], 10965.24)
        for key in EXPECTED_EXTRA:
            self.assertIn(key, item["extra"], key)


if __name__ == "__main__":
    unittest.main()
