"""Labeled stock-value footer regex must compile and parse printed money.

Layout family: footer lines like Opening/Purchase/Sales/Closing stock value.
"""

from __future__ import annotations

import re
import unittest

from services.sales_statement_extractor import (
    _apply_labeled_stock_value_footer,
    _parse_labeled_stock_value_footer,
)


class TestLabeledStockValueFooter(unittest.TestCase):
    def test_sales_value_lookbehind_compiles(self):
        """Regression: variable-width (?<!last\\s+month\\s) crashed re.compile."""
        pat = (
            r"(?<!previous\s)(?<!last\smonth\s)sales\s+value"
            r"\s*[:\-–—]?\s*([0-9][0-9,]*(?:\.\d+)?)"
        )
        re.compile(pat, re.I)

    def test_parse_labeled_footer_values(self):
        blob = (
            "Opening stock value: 1,234.50\n"
            "Purchase value 500.00\n"
            "Sales value: 10033.23\n"
            "Closing stock value - 888.10\n"
        )
        found = _parse_labeled_stock_value_footer(blob)
        self.assertEqual(found["opening_value"], 1234.50)
        self.assertEqual(found["purchase_value"], 500.00)
        self.assertEqual(found["sales_value"], 10033.23)
        self.assertEqual(found["closing_value"], 888.10)

    def test_skips_previous_and_last_month_sales_value(self):
        blob = (
            "Previous sales value: 99.00\n"
            "Last month sales value: 88.00\n"
            "Sales value: 77.00\n"
        )
        found = _parse_labeled_stock_value_footer(blob)
        self.assertEqual(found.get("sales_value"), 77.00)

    def test_apply_fills_null_totals(self):
        result = {"totals": {"sales_value": None, "closing_value": None, "extra": {}}}
        out = _apply_labeled_stock_value_footer(
            result,
            "Sales value: 10033.23\nClosing stock value: 500.5",
        )
        self.assertEqual(out["totals"]["sales_value"], 10033.23)
        self.assertEqual(out["totals"]["closing_value"], 500.5)
        self.assertEqual(
            out["totals"]["extra"].get("total_row_source"),
            "labeled_stock_value_footer",
        )


if __name__ == "__main__":
    unittest.main()
