"""Word table: Item Description, Opening, Receipt, Issue, Closing."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import extract_sales_statement

FIXTURE = Path("0000729702_2026_08_ZL_20_303_05092026131419.docx")


class TestItemOpeningReceiptIssueDocx(unittest.TestCase):
    def test_madhur_medicose_qty_columns(self):
        if not FIXTURE.is_file():
            self.skipTest(f"missing {FIXTURE.name}")
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        items = result["line_items"]
        self.assertEqual(len(items), 41)
        self.assertNotIn("statements", result)
        self.assertEqual(result["stockist_name"], "MADHUR MEDICOSE")
        self.assertIn("SRI GANGANAGAR", result["stockist_address"])
        self.assertEqual(result["company_name"], "HIMALAYA ZEAL")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        self.assertFalse(any(item["product_name"].upper() == "TOTAL" for item in items))
        self.assertFalse(any(item["product_name"].upper() == "HIMALAYA ZEAL" for item in items))

        abana = next(item for item in items if item["product_name"] == "ABANA")
        self.assertEqual(abana["packing"], "60TAB")
        self.assertEqual(abana["opening_qty"], 7)
        self.assertEqual(abana["receipts_qty"], 0)
        self.assertEqual(abana["sales_qty"], 5)
        self.assertEqual(abana["closing_qty"], 2)

        face = next(item for item in items if item["product_name"] == "CLARINA FACE WASH GEL")
        self.assertEqual(face["packing"], "60 ML")
        self.assertEqual(face["opening_qty"], 50)
        self.assertEqual(face["closing_qty"], 50)

        syrup = next(item for item in items if item["product_name"] == "LIV 52 SYRP.")
        self.assertEqual(syrup["opening_qty"], 116)
        self.assertEqual(syrup["receipts_qty"], 5005)
        self.assertEqual(syrup["sales_qty"], 2957)
        self.assertEqual(syrup["closing_qty"], 2164)

        self.assertEqual(sum(item["opening_qty"] for item in items), 1370)
        self.assertEqual(sum(item["receipts_qty"] for item in items), 5105)
        self.assertEqual(sum(item["sales_qty"] for item in items), 3306)
        self.assertEqual(sum(item["closing_qty"] for item in items), 3169)
        self.assertIsNone(result["totals"]["sales_value"])
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "item_opening_receipt_issue_docx",
        )


if __name__ == "__main__":
    unittest.main()
