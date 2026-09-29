"""Marg ERP landscape PRODUCT DESCRIPTION / RECEIVE + ISSUE/CLOSING split PDF."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _looks_like_stockist_header,
    extract_sales_statement,
)

SAMPLE = Path(__file__).resolve().parents[1] / (
    "0000701833_2026_08_ZL_20_8232_02092026134945.pdf"
)


class TestMargNanoSplitPdf(unittest.TestCase):
    def test_column_labels_are_not_stockists(self):
        for label in ("RECEIVE", "EXPIRY", "QUANTITY", "ISSUE", "VALUE", "STOCK"):
            self.assertFalse(_looks_like_stockist_header(label), label)

    @unittest.skipUnless(SAMPLE.is_file(), "sample PDF missing")
    def test_gayatri_single_statement_not_receive_expiry(self):
        result = extract_sales_statement(SAMPLE.read_bytes(), SAMPLE.name)
        self.assertFalse(result.get("multi_statement"))
        self.assertEqual(result.get("stockist_name"), "GAYATRI DRUG DISTRIBUTORS")
        self.assertEqual(result.get("period_from"), "2026-08-01")
        self.assertEqual(result.get("period_to"), "2026-08-31")
        items = result.get("line_items") or []
        self.assertEqual(len(items), 21)
        method = ((result.get("totals") or {}).get("extra") or {}).get(
            "extraction_method"
        )
        self.assertEqual(method, "marg_nano_split_columns")
        by_name = {item["product_name"]: item for item in items}
        cream = by_name["CLARINA CREAM 30gms"]
        self.assertEqual(cream["opening_qty"], 19.0)
        self.assertEqual(cream["sales_qty"], 5.0)
        self.assertEqual(cream["sales_value"], 585.7)
        self.assertEqual(cream["closing_qty"], 14.0)
        confido = by_name["CONFIDO TAB. 60 s"]
        self.assertEqual(confido["sales_qty"], 20.0)
        self.assertEqual(confido["sales_value"], 2976.2)
        liv = by_name["LIV.52 SYRUP 200ML"]
        self.assertEqual(liv["receipts_qty"], 140.0)
        self.assertEqual(liv["receipts_value"], 22127.0)
        self.assertAlmostEqual(result["totals"]["sales_value"], 63853.34, places=2)


if __name__ == "__main__":
    unittest.main()
