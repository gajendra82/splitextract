"""Marg STOCK & SALES ANALYSIS xlsx with RATE (no RE-ORDER) — sales qty/value."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _find_marg_erp_xls_header,
    _find_marg_sale_purchase_analysis_header,
    _parse_marg_sale_purchase_analysis_xls,
    extract_sales_statement,
)


def _book1_rows():
    return [
        ["SHIKHA ASSOCIATES"],
        ["OFFICE NO.108, FIRST FLOOR"],
        ["Phone : 9953339923"],
        ["STOCK & SALES ANALYSIS  (HIMALYA ZANDRA) 01-10-2026 - 31-10-2026"],
        [
            "ITEM DESCRIPTION",
            "OPENING",
            "",
            " SALE",
            "REPL./",
            "TOTAL",
            "",
            "PURCHASE",
            "REPL./",
            "CLOSING",
            "",
        ],
        [
            None,
            "STOCK",
            "PURCHASES",
            "RETURN",
            "OTHERS",
            "STOCK",
            "SALES",
            "RETURN",
            "OTHERS",
            "STOCK",
            "RATE",
        ],
        [],
        ["HIMALYA ZANDRA"],
        ["BONNISAN SYP 200ML             200ml", 24, 0, 0, 0, 24, 3, 0, 0, 21, 96.24],
        ["CYSTONE TAB                    60t", 32, 0, 1, 0, 33, 23, 0, 0, 10, 161.07],
        ["EVE CARE 200ml SYP.            200ml", 5, 28, 0, 0, 33, 9, 0, 0, 24, 137.95],
        ["LIV-52 DS TAB                  60t", 66, 100, 0, 0, 166, 127, 0, 0, 39, 189.8],
    ]


class MargSalePurchaseAnalysisRateXlsTests(unittest.TestCase):
    def test_header_accepts_rate_without_reorder(self):
        found = _find_marg_sale_purchase_analysis_header(_book1_rows())
        self.assertIsNotNone(found)
        _idx, roles = found
        self.assertEqual(roles["sales_qty"], 6)
        self.assertEqual(roles["purchase_qty"], 2)
        self.assertEqual(roles["rate"], 10)
        self.assertNotIn("reorder_qty", roles)
        # Generic Marg ERP must not steal this two-row RATE grid.
        self.assertIsNone(_find_marg_erp_xls_header(_book1_rows()))

    def test_sales_qty_and_value_from_rate(self):
        parsed = _parse_marg_sale_purchase_analysis_xls(
            _book1_rows(), "Book1.xlsx", ".xlsx", "MARG ERP 9+ Excel Report"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        by = {i["product_name"]: i for i in parsed["line_items"]}
        self.assertNotIn("HIMALYA ZANDRA", by)
        bon = by["BONNISAN SYP"]
        self.assertEqual(bon["packing"], "200ml")
        self.assertEqual(bon["opening_qty"], 24.0)
        self.assertEqual(bon["receipts_qty"], 0.0)
        self.assertEqual(bon["sales_qty"], 3.0)
        self.assertEqual(bon["closing_qty"], 21.0)
        self.assertAlmostEqual(bon["sales_value"], 288.72, places=2)
        self.assertAlmostEqual(bon["closing_value"], 2021.04, places=2)
        self.assertEqual(bon["extra"]["rate"], 96.24)

        cyst = by["CYSTONE TAB"]
        self.assertEqual(cyst["packing"], "60t")
        self.assertEqual(cyst["sales_qty"], 23.0)
        self.assertEqual(cyst["sales_return_qty"], 1.0)
        self.assertAlmostEqual(cyst["sales_value"], 23.0 * 161.07, places=2)

        eve = by["EVE CARE 200ml SYP."]
        self.assertEqual(eve["packing"], "200ml")
        self.assertEqual(eve["receipts_qty"], 28.0)
        self.assertEqual(eve["sales_qty"], 9.0)

        liv = by["LIV-52 DS TAB"]
        self.assertEqual(liv["packing"], "60t")
        self.assertEqual(liv["receipts_qty"], 100.0)
        self.assertEqual(liv["sales_qty"], 127.0)
        self.assertAlmostEqual(liv["sales_value"], 127.0 * 189.8, places=2)


class MargSalePurchaseAnalysisRateLiveFileTests(unittest.TestCase):
    def test_book1_xlsx_extract(self):
        from pathlib import Path

        path = Path(r"C:\Users\adity\Downloads\himalaya invoice\Book1 (1)-1.xlsx")
        if not path.is_file():
            self.skipTest("Book1 fixture absent")
        result = extract_sales_statement(path.read_bytes(), path.name)
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "marg_sale_purchase_analysis",
        )
        bon = next(
            i
            for i in result["line_items"]
            if str(i.get("product_name") or "").startswith("BONNISAN SYP")
        )
        self.assertEqual(bon["sales_qty"], 3.0)
        self.assertAlmostEqual(float(bon["sales_value"]), 288.72, places=2)


if __name__ == "__main__":
    unittest.main()
