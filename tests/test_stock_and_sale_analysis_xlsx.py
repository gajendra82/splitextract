"""Stock And Sales xlsx: Barcode | Name | Pack | Opening | Purchase | Sale."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _find_marg_erp_xls_header,
    _find_stock_and_sale_analysis_xls_header,
    _xls_best_header,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000723087_2026_08_ZL_30_750_04092026132500 (1).xlsx"
)

ANALYSIS_HEADER = [
    "Barcode",
    "Name",
    "Pack",
    "Opening",
    "Purchase",
    "Sale",
    "Closing",
    "Pur Value",
    "Sale Value",
    "Closing Value",
]


class TestStockAndSaleAnalysisDetection(unittest.TestCase):
    def test_detects_barcode_name_pack_header(self):
        found = _find_stock_and_sale_analysis_xls_header(
            [
                ["SOUTH DELHI DISTRIBUTORS"],
                ["Stock And Sales"],
                ANALYSIS_HEADER,
            ]
        )
        self.assertIsNotNone(found)
        _idx, roles = found
        self.assertEqual(_idx, 2)
        self.assertEqual(roles["product_name"], 1)
        self.assertEqual(roles["sales_qty"], 5)
        self.assertEqual(roles["sales_value"], 8)

    def test_does_not_match_generic_or_marg_headers(self):
        self.assertIsNone(
            _find_stock_and_sale_analysis_xls_header(
                [["Item", "Op.", "Sale", "Bal."], ["LIV 52 TAB", 10, 2, 8]]
            )
        )
        marg = [
            [
                "PRODUCT DESCRIPTION",
                "OPENING",
                "PURCHASE",
                "SALE",
                "CLOSING",
                "RATE",
            ]
        ]
        self.assertIsNone(_find_stock_and_sale_analysis_xls_header(marg))
        self.assertIsNotNone(_find_marg_erp_xls_header(marg))
        idx, colmap, _score = _xls_best_header(
            [["Item", "Op.", "Sale", "Bal."], ["LIV 52 TAB", 10, 2, 8]]
        )
        self.assertIsNotNone(idx)
        self.assertIn("item", colmap)


@unittest.skipUnless(FIXTURE.is_file(), "missing Stock And Sales xlsx fixture")
class TestStockAndSaleAnalysisFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.items = cls.result.get("line_items") or []
        cls.by_name = {item["product_name"]: item for item in cls.items}
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}

    def test_extracts_products_not_totals(self):
        self.assertEqual(
            self.extra.get("extraction_method"), "stock_and_sale_analysis_xlsx"
        )
        self.assertEqual(self.result["stockist_name"], "SOUTH DELHI DISTRIBUTORS")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertGreaterEqual(len(self.items), 50)
        self.assertNotIn("Invoices", self.result)
        names = [item.get("product_name") for item in self.items]
        self.assertFalse(any(re.search(r"^TOTAL", str(n or ""), re.I) for n in names))
        self.assertNotIn("00", names)
        self.assertEqual(names[0], "ABANA TAB.")

    def test_printed_qty_and_values(self):
        abana = self.by_name["ABANA TAB."]
        self.assertEqual(abana["product_code"], "03056")
        self.assertEqual(abana["packing"], "1X60`S")
        self.assertEqual(
            (
                abana["opening_qty"],
                abana["receipts_qty"],
                abana["sales_qty"],
                abana["closing_qty"],
            ),
            (23.0, 1400.0, 1422.0, 1.0),
        )
        self.assertAlmostEqual(abana["sales_value"], 203819.81, places=2)
        self.assertAlmostEqual(abana["closing_value"], 148.6, places=2)
        self.assertAlmostEqual((abana.get("extra") or {}).get("purchase_value"), 197638, places=2)

        amalaki = self.by_name["AMALAKI TAB"]
        self.assertIsNone(amalaki.get("product_code"))
        self.assertEqual(amalaki["opening_qty"], 40.0)
        self.assertEqual(amalaki["sales_qty"], 0.0)
        self.assertEqual(amalaki["closing_qty"], 40.0)

        liv = self.by_name["LIV-52 TAB."]
        self.assertEqual(liv["opening_qty"], 4636.0)
        self.assertEqual(liv["sales_qty"], 1395.0)
        self.assertEqual(liv["closing_qty"], 3241.0)

        bleminor = self.by_name["BLEMINOR ANTI BLEMISH CREAM"]
        self.assertEqual(bleminor["sales_qty"], -1.0)
        self.assertEqual(bleminor["closing_qty"], 40.0)


if __name__ == "__main__":
    unittest.main()
