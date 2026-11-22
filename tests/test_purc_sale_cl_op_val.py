"""Purc/Sale/Cl sheets that also print Op / Op val.

STOCK_PURC_SALE_CL_OP_VAL (default ON) maps printed opening columns by header
x-centres. Kill switch false leaves Op/Op val in packing and slides Sa/Cl
values into qty columns.

STOCK_PURC_SALE_CL_VALUE_FOOTER peels printed Totals money under Op val / Purc
val / Sa val / Cl val so statement sales_value matches the sheet footer.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from services.sales_statement_extractor import (
    _is_purc_sale_cl_chrome_name,
    _purc_sale_cl_columns,
    _purc_sale_cl_field,
    _purc_sale_cl_op_val_enabled,
    _purc_sale_cl_value_footer_enabled,
    extract_sales_statement,
)


# Header x positions from UNIQUE DISTRIBUTORS Stock and Sales (HIMALAYA OTX / ZANDRA).
_HEADER = [
    (25.5, "Product"),
    (59.4, "Name"),
    (179.7, "Unit"),
    (244.2, "Op"),
    (283.4, "Op"),
    (296.4, "val"),
    (324.5, "Purc"),
    (362.7, "Purc"),
    (383.6, "val"),
    (411.8, "Sale"),
    (458.0, "Sa"),
    (471.0, "val"),
    (494.1, "Cl"),
    (504.1, "qty"),
    (548.3, "Cl"),
    (558.3, "val"),
]

# GASEX TAB printed numbers (left edge x).
_GASEX_TAB = [
    (247.2, "92"),
    (277.4, "11925.00"),
    (338.5, "0"),
    (380.7, "0.00"),
    (421.8, "27"),
    (456.0, "4238.00"),
    (509.1, "65"),
    (543.3, "8281.00"),
]

# Printed Totals under Op val / Purc val / Sa val / Cl val (UNIQUE ZANDRA Sept).
_TOTALS_MONEY = [
    (277.5, "77676.00"),
    (366.8, "61218.00"),
    (452.1, "62668.00"),
    (539.4, "85997.00"),
]

_UNIQUE_ZANDRA_PDF = Path(
    r"C:\Users\Prerana Bhalerao\Downloads\StockAndSalesReport-1.pdf"
)
_UNIQUE_ZANDRA_OCT_PDF = Path(
    r"C:\Users\Prerana Bhalerao\Downloads\DOC-20261103-WA0014.pdf"
)


class TestPurcSaleClOpVal(unittest.TestCase):
    def test_flag_defaults_on(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STOCK_PURC_SALE_CL_OP_VAL", None)
            self.assertTrue(_purc_sale_cl_op_val_enabled())
        with patch.dict(os.environ, {"STOCK_PURC_SALE_CL_OP_VAL": "false"}):
            self.assertFalse(_purc_sale_cl_op_val_enabled())

    def test_flag_off_ignores_op_headers(self):
        with patch.dict(os.environ, {"STOCK_PURC_SALE_CL_OP_VAL": "false"}):
            cols = _purc_sale_cl_columns(_HEADER)
        self.assertIsNotNone(cols)
        names = [n for n, _ in cols]
        self.assertNotIn("opening_qty", names)
        self.assertNotIn("opening_value", names)

    def test_flag_on_maps_op_and_midpoint_values(self):
        with patch.dict(os.environ, {"STOCK_PURC_SALE_CL_OP_VAL": "true"}):
            cols = _purc_sale_cl_columns(_HEADER)
            self.assertIsNotNone(cols)
            by_field = {n: x for n, x in cols}
            self.assertIn("opening_qty", by_field)
            self.assertIn("opening_value", by_field)
            assigned = {
                tok: _purc_sale_cl_field(x, cols) for x, tok in _GASEX_TAB
            }
        self.assertEqual(assigned["92"], "opening_qty")
        self.assertEqual(assigned["11925.00"], "opening_value")
        self.assertEqual(assigned["27"], "sales_qty")
        self.assertEqual(assigned["4238.00"], "sales_value")
        self.assertEqual(assigned["65"], "closing_qty")
        self.assertEqual(assigned["8281.00"], "closing_value")

    def test_totals_money_maps_to_value_columns(self):
        with patch.dict(os.environ, {"STOCK_PURC_SALE_CL_OP_VAL": "true"}):
            cols = _purc_sale_cl_columns(_HEADER)
            assigned = {
                tok: _purc_sale_cl_field(x, cols) for x, tok in _TOTALS_MONEY
            }
        self.assertEqual(assigned["77676.00"], "opening_value")
        self.assertEqual(assigned["61218.00"], "receipts_value")
        self.assertEqual(assigned["62668.00"], "sales_value")
        self.assertEqual(assigned["85997.00"], "closing_value")

    def test_value_footer_flag_defaults_on(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STOCK_PURC_SALE_CL_VALUE_FOOTER", None)
            self.assertTrue(_purc_sale_cl_value_footer_enabled())
        with patch.dict(os.environ, {"STOCK_PURC_SALE_CL_VALUE_FOOTER": "false"}):
            self.assertFalse(_purc_sale_cl_value_footer_enabled())

    def test_chrome_report_title_is_not_a_product(self):
        self.assertTrue(_is_purc_sale_cl_chrome_name("Stock and Sales Statement"))
        self.assertTrue(_is_purc_sale_cl_chrome_name("Stock and Sales"))
        self.assertFalse(_is_purc_sale_cl_chrome_name("LIV 52 DROP(60)"))

    def test_synthetic_op_val_row_keeps_printed_sales_and_negative_closing(self):
        """Printed Sa val / Cl val stay as-is (no identity rewrite)."""
        with patch.dict(os.environ, {"STOCK_PURC_SALE_CL_OP_VAL": "true"}):
            cols = _purc_sale_cl_columns(_HEADER)
            # Distinct tokens only — x positions from UNIQUE Op/Op val grid.
            forte_sale = _purc_sale_cl_field(421.8, cols)
            forte_sv = _purc_sale_cl_field(456.0, cols)
            forte_cl = _purc_sale_cl_field(509.1, cols)
            forte_clv = _purc_sale_cl_field(543.3, cols)
            syp_sv = _purc_sale_cl_field(456.0, cols)
            syp_cl = _purc_sale_cl_field(509.1, cols)
            syp_clv = _purc_sale_cl_field(543.3, cols)
        self.assertEqual(forte_sale, "sales_qty")
        self.assertEqual(forte_sv, "sales_value")
        self.assertEqual(forte_cl, "closing_qty")
        self.assertEqual(forte_clv, "closing_value")
        self.assertEqual(syp_sv, "sales_value")
        self.assertEqual(syp_cl, "closing_qty")
        self.assertEqual(syp_clv, "closing_value")


@unittest.skipUnless(_UNIQUE_ZANDRA_PDF.is_file(), "missing Unique Zandra StockAndSalesReport-1.pdf")
class TestPurcSaleClUniqueZandraLive(unittest.TestCase):
    def test_sales_value_matches_printed_totals_footer(self):
        env = {
            "STOCK_PURC_SALE_CL_OP_VAL": "true",
            "STOCK_PURC_SALE_CL_VALUE_FOOTER": "true",
        }
        with patch.dict(os.environ, env):
            result = extract_sales_statement(
                _UNIQUE_ZANDRA_PDF.read_bytes(), _UNIQUE_ZANDRA_PDF.name
            )
        totals = result.get("totals") or {}
        extra = totals.get("extra") or {}
        self.assertEqual(result.get("stockist_name"), "UNIQUE DISTRIBUTORS")
        self.assertEqual(result.get("company_name"), "HIMALAYA ZANDRA")
        self.assertEqual(totals.get("sales_value"), 62668.0)
        self.assertEqual(totals.get("closing_value"), 85997.0)
        self.assertEqual(extra.get("sales_value_source"), "purc_sale_cl_value_footer")
        self.assertEqual(extra.get("opening_value"), 77676.0)
        self.assertEqual(extra.get("purchase_value"), 61218.0)
        names = [it.get("product_name") for it in result.get("line_items") or []]
        self.assertNotIn("Stock and Sales Statement", names)
        liv = next(
            it
            for it in result.get("line_items") or []
            if it.get("product_name") == "LIV 52 DROP(60)"
        )
        self.assertEqual(liv.get("opening_qty"), 43.0)
        self.assertEqual(liv.get("receipts_qty"), 100.0)
        self.assertEqual(liv.get("sales_qty"), 63.0)
        self.assertEqual(liv.get("sales_value"), 6960.0)
        self.assertEqual(liv.get("closing_qty"), 85.0)
        self.assertEqual(liv.get("receipts_value"), 9794.0)


@unittest.skipUnless(
    _UNIQUE_ZANDRA_OCT_PDF.is_file(), "missing DOC-20261103-WA0014.pdf"
)
class TestPurcSaleClUniqueZandraOctLive(unittest.TestCase):
    def test_oct_sales_value_and_printed_anomaly_rows(self):
        env = {
            "STOCK_PURC_SALE_CL_OP_VAL": "true",
            "STOCK_PURC_SALE_CL_VALUE_FOOTER": "true",
        }
        with patch.dict(os.environ, env):
            result = extract_sales_statement(
                _UNIQUE_ZANDRA_OCT_PDF.read_bytes(), _UNIQUE_ZANDRA_OCT_PDF.name
            )
        totals = result.get("totals") or {}
        extra = totals.get("extra") or {}
        self.assertEqual(result.get("stockist_name"), "UNIQUE DISTRIBUTORS")
        self.assertEqual(result.get("company_name"), "HIMALAYA ZANDRA")
        self.assertEqual(result.get("period_from"), "2026-10-01")
        self.assertEqual(result.get("period_to"), "2026-10-31")
        self.assertEqual(totals.get("sales_value"), 54823.0)
        self.assertEqual(totals.get("closing_value"), 73536.0)
        self.assertEqual(extra.get("sales_value_source"), "purc_sale_cl_value_footer")
        self.assertEqual(extra.get("opening_value"), 85554.0)
        self.assertEqual(extra.get("purchase_value"), 35328.0)
        self.assertEqual(extra.get("extraction_method"), "purc_sale_cl_op_val_layout")
        items = result.get("line_items") or []
        by_name = {it.get("product_name"): it for it in items}
        forte = by_name["EVECARE FORTE TAB"]
        self.assertEqual(forte.get("opening_qty"), 0.0)
        self.assertEqual(forte.get("sales_qty"), 1.0)
        self.assertEqual(forte.get("sales_value"), 68.0)
        self.assertEqual(forte.get("closing_qty"), 2.0)
        self.assertEqual(forte.get("closing_value"), 132.0)
        syp = by_name["EVECARE SYP(200)"]
        self.assertEqual(syp.get("opening_qty"), 19.0)
        self.assertEqual(syp.get("sales_qty"), 19.0)
        self.assertEqual(syp.get("sales_value"), 3112.0)
        self.assertEqual(syp.get("closing_qty"), 0.0)
        self.assertEqual(syp.get("closing_value"), -297.0)
        ren = by_name["RENALKA SYP"]
        self.assertEqual(ren.get("opening_qty"), 4.0)
        self.assertEqual(ren.get("sales_qty"), 5.0)
        self.assertEqual(ren.get("sales_value"), 407.0)
        self.assertEqual(ren.get("closing_qty"), 0.0)
        self.assertEqual(ren.get("closing_value"), -124.0)
        paste = by_name["HIORA K TOOTH PASTE"]
        self.assertEqual(paste.get("opening_value"), -1.0)
        self.assertEqual(paste.get("closing_value"), -1.0)
        line_sales = sum(float(it.get("sales_value") or 0) for it in items)
        self.assertEqual(line_sales, 54823.0)
        names = [it.get("product_name") for it in items]
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("Stock and Sales Statement", names)


if __name__ == "__main__":
    unittest.main()
