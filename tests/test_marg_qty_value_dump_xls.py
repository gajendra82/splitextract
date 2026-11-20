"""Marg ITEM DESCRIPTION sheet with QTY and VALUE columns plus DUMP."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _parse_marg_closing_mexp_xls,
    _parse_marg_opening_receipt_issue,
    _parse_marg_qty_value_dump_xls,
    extract_sales_statement,
)

PLAIN = [
    ["SOME MEDICAL"],
    ["ITEM DESCRIPTION", "OPENING", "RECEIPT", "ISSUE", "CLOSING"],
    ["ABANA TAB              50'S", 10, 0, 2, 8],
]

MEXP_ROWS = [
    ["BHARAT MEDICALS"],
    ["STOCK & SALES ANALYSIS  (HIMALAYA ZEAL) 01-08-2026 - 31-08-2026"],
    ["ITEM DESCRIPTION", "OPENING", "RECEIPT", "ISSUE", "CLOSING M.EXP"],
    ["ABANA TAB              50'S.", 57, 0, 2, "    55  6/27"],
]

FIXTURE = Path("0000725772_2026_08_ZA_24_8137_03092026174341.xlsx")


class TestMargQtyValueDump(unittest.TestCase):
    def test_older_marg_headers_are_not_this_layout(self):
        self.assertIsNone(
            _parse_marg_qty_value_dump_xls(PLAIN, "plain.xls", ".xls", "Sheet1")
        )
        self.assertIsNone(
            _parse_marg_qty_value_dump_xls(MEXP_ROWS, "mexp.xls", ".xls", "Sheet1")
        )
        self.assertTrue(
            _parse_marg_opening_receipt_issue(PLAIN, "plain.xls", ".xls", "Sheet1")
        )
        self.assertTrue(
            _parse_marg_closing_mexp_xls(MEXP_ROWS, "mexp.xls", ".xls", "Sheet1")
        )

    def test_bombay_medical_store_workbook(self):
        if not FIXTURE.is_file():
            self.skipTest("workbook is not in the workspace")
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        items = result["line_items"]
        self.assertEqual(
            (result.get("totals") or {}).get("extra", {}).get("extraction_method"),
            "marg_qty_value_dump_xls",
        )
        self.assertEqual(result["stockist_name"], "BOMBAY MEDICAL STORE")
        self.assertIn("HARDOI", result["stockist_address"])
        self.assertEqual(result["company_name"], "HIMALAYA HEALTHCARE (ZANDRA AMIT)")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-29")
        self.assertEqual(len(items), 35)
        self.assertNotIn("HIMALAYA HEALTHCARE", [item["product_name"] for item in items])
        first = items[0]
        self.assertEqual(first["product_name"], "BONNISAN DROP")
        self.assertEqual(first["packing"], "30ML")
        self.assertEqual(first["opening_qty"], 27)
        self.assertEqual(first["sales_qty"], 3)
        self.assertEqual(first["sales_value"], 197.1)
        self.assertEqual(first["closing_qty"], 24)
        self.assertEqual(first["closing_value"], 1791.84)
        self.assertEqual(first["extra"]["dump_qty"], 7)
        hiora = next(item for item in items if item["product_name"] == "HIORA PASTE")
        self.assertEqual(hiora["packing"], "100 GM")
        cystone = next(item for item in items if item["product_name"] == "CYSTONE FORTE TAB")
        self.assertEqual(cystone["packing"], "1X60 1X60")
        septillin = next(item for item in items if item["product_name"] == "SEPTILLIN SYP")
        self.assertEqual(septillin["sales_qty"], -4)
        self.assertEqual(septillin["closing_qty"], 21)
        self.assertEqual(result["totals"]["sales_value"], 224760.21)
        self.assertEqual(result["totals"]["closing_value"], 109150.03)
        self.assertEqual(result["totals"]["extra"]["opening_qty"], 695)
        self.assertEqual(result["totals"]["extra"]["sales_qty"], 1302)
        self.assertEqual(result["totals"]["extra"]["closing_qty"], 703)
        self.assertAlmostEqual(
            sum(item["sales_value"] for item in items), 224760.21, places=2
        )
        self.assertAlmostEqual(
            sum(item["opening_qty"] + item["receipts_qty"] - item["sales_qty"] for item in items),
            sum(item["closing_qty"] for item in items),
            places=2,
        )


if __name__ == "__main__":
    unittest.main()
