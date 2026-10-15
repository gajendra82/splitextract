"""A delimited-text export saved with a .xls name must not crash (was HTTP 500)."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _xls_delimited_text_rows,
    extract_sales_statement,
)

CSV_TEXT = (
    "Some Distributors Pvt.Ltd\n"
    "HIMALAYA WELLNESS\n"
    "Stock And Sales for the period 01-09-2026 to 30-09-2026\n"
    "\n"
    "Product Name,Pack,Rate,Previous Last Sale,Previous Sale,Opening,Receipt,"
    "Sales,Free,Sales RTN,Sale Value,Purchase RTN,Others,Closing,Cl Value,"
    "expQty,nonMoveQty,Flag\n"
    "ABC TAB,60''S,198,0,0,0,0,0,0,0,0,0,0,0,0,0,0,\n"
    "XYZ DROPS 30ML,30ML,74.66,117,74,284,0,74,0,1,5525,0,0,210,15671,0,0,\n"
)

REAL_FILE = Path(__file__).resolve().parent.parent / "Trimurti Distributors.xls"


class XlsDelimitedTextTests(unittest.TestCase):
    def test_rows_helper_returns_none_for_biff_like_bytes(self):
        self.assertIsNone(_xls_delimited_text_rows(b"\xd0\xcf\x11\xe0" + b"\x00" * 64))
        self.assertIsNone(_xls_delimited_text_rows(b"just one line"))

    def test_csv_with_xls_extension_extracts_without_raising(self):
        result = extract_sales_statement(CSV_TEXT.encode("utf-8"), "export.xls")
        items = result["line_items"]
        self.assertEqual(len(items), 2)
        xyz = next(i for i in items if i["product_name"].startswith("XYZ"))
        # Printed values are kept as-is, columns come from headers.
        self.assertEqual(xyz["opening_qty"], 284.0)
        self.assertEqual(xyz["sales_qty"], 74.0)
        self.assertEqual(xyz["sales_value"], 5525.0)
        self.assertEqual(xyz["closing_qty"], 210.0)
        self.assertEqual(xyz["closing_value"], 15671.0)
        self.assertEqual(result["period_from"], "2026-09-01")
        self.assertEqual(result["period_to"], "2026-09-30")

    @unittest.skipUnless(REAL_FILE.exists(), "sample file not present")
    def test_real_sample_file(self):
        result = extract_sales_statement(REAL_FILE.read_bytes(), REAL_FILE.name)
        self.assertGreater(len(result["line_items"]), 0)
        self.assertEqual(result["period_from"], "2026-09-01")
        self.assertEqual(result["period_to"], "2026-09-30")


if __name__ == "__main__":
    unittest.main()
