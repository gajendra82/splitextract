"""STVR: Sale|…|Others|Closing TOTAL money must not park Others as sales_value.

Sale-and-stock analysis grids print qty chrome (Sale * Ap / Sale Free / Sale
Retu / Others / Closing) and put rupees only on Grand Total under those
columns. Recovery used to take money[-2] (Others) as sales_value.
Also: space-flattened Purchase Free / Sale Return headers must not shift
sale_idx onto Sale Return money.
"""

from __future__ import annotations

import unittest

from services.stock_header_resolver import assign_cells, resolve_columns
from services.stock_native_resolver import (
    _footer_totals_from_page_results,
    _stvr_coalesce_header_tokens,
    _stvr_header_sale_close_indices,
    _stvr_pick_sales_closing_from_amounts,
    extract_sales_total_value_from_text,
)
from services.stock_native_tables import _coalesce_sale_star_free_return_headers


_GRAND_AMOUNTS = [
    1047060.5,
    980198.22,
    0.0,
    0.0,
    877394.70,
    881.63801,
    12633.696,
    -27124.36,
    1159698.6,
]

_PLUMBER_HEADERS = [
    "Item Name",
    "Pack",
    "Opening",
    "Purchase",
    "Purchase",
    "Purchase",
    "Sale * Ap",
    "p Sale Free",
    "Sale Retu",
    "Others",
    "Closing",
]

_SAMPLE_TEXT = """
SALE AND STOCK ANALYSIS
Item Name Pack Opening Purchase Purchase Purchase Sale * App Sale Free Sale Retu Others Closing
ARJUNA TABS 60`S 71 180 - - 109 - 3 -62 145
Grand Total 1047060.5 980198.22 0 0 877394.70 881.63801 12633.696 -27124.36 1159698.6
"""


class StvrSaleOthersClosingMoneyTests(unittest.TestCase):
    def test_pick_skips_signed_others_without_header_idx(self):
        sales, closing = _stvr_pick_sales_closing_from_amounts(_GRAND_AMOUNTS)
        self.assertAlmostEqual(sales, 877394.70, places=2)
        self.assertAlmostEqual(closing, 1159698.6, places=2)
        self.assertNotAlmostEqual(sales, -27124.36, places=2)

    def test_plumber_headers_map_sale_and_closing_slots(self):
        sale_i, close_i = _stvr_header_sale_close_indices(_PLUMBER_HEADERS)
        self.assertEqual(sale_i, 4)
        self.assertEqual(close_i, 8)
        sales, closing = _stvr_pick_sales_closing_from_amounts(
            _GRAND_AMOUNTS, sale_idx=sale_i, close_idx=close_i
        )
        self.assertAlmostEqual(sales, 877394.70, places=2)
        self.assertAlmostEqual(closing, 1159698.6, places=2)

    def test_classic_last_two_money_unchanged(self):
        sales, closing = _stvr_pick_sales_closing_from_amounts(
            [1000.5, 500.25, 800.25, 900.5]
        )
        self.assertAlmostEqual(sales, 800.25, places=2)
        self.assertAlmostEqual(closing, 900.5, places=2)

    def test_extract_from_grand_total_text(self):
        out = extract_sales_total_value_from_text(_SAMPLE_TEXT)
        self.assertAlmostEqual(out["sales_value"], 877394.70, places=2)
        self.assertAlmostEqual(out["closing_value"], 1159698.6, places=2)
        self.assertNotAlmostEqual(out["sales_value"], -27124.36, places=2)
        self.assertEqual(out["label"], "Grand Total")

    def test_coalesce_space_split_sale_star_free_return(self):
        flat = (
            "Item Name Pack Opening Purchase Purchase Free Purchase Return "
            "Sale * Sale Free Sale Return Others Closing"
        ).split()
        joined = _stvr_coalesce_header_tokens(flat)
        self.assertIn("Sale *", joined)
        self.assertIn("Sale Free", joined)
        self.assertIn("Sale Return", joined)
        self.assertIn("Purchase Free", joined)
        sale_i, close_i = _stvr_header_sale_close_indices(flat)
        self.assertEqual(sale_i, 4)
        self.assertEqual(close_i, 8)

    def test_sale_star_grand_total_not_sale_return(self):
        text = """
SALE AND STOCK ANALYSIS
Item Name Pack Opening Purchase Purchase Free Purchase Return Sale * Sale Free Sale Return Others Closing
DIANA TAB 60S 100 50 - - 97 - 6 - 147
Grand Total 1525348.73 425709.11 0 0 793445.193 408.407 5996.378 -63136.31 1162789.74
"""
        out = extract_sales_total_value_from_text(text)
        self.assertAlmostEqual(out["sales_value"], 793445.193, places=2)
        self.assertAlmostEqual(out["closing_value"], 1162789.74, places=2)
        self.assertNotAlmostEqual(out["sales_value"], 5996.378, places=2)

    def test_blank_sale_star_does_not_rehome_return_to_sales_value(self):
        headers = [
            "Item Name",
            "Pack",
            "Opening",
            "Purchase",
            "Purchase Free",
            "Purchase Return",
            "Sale *",
            "Sale Free",
            "Sale Return",
            "Others",
            "Closing",
        ]
        cols = resolve_columns(
            [
                {"text": h, "col_index": i, "x_center": float(i * 40)}
                for i, h in enumerate(headers)
            ]
        )["columns"]
        tokens = [
            {"text": v, "col_index": i}
            for i, v in enumerate(
                [
                    "Total",
                    "",
                    "1525348.73",
                    "425709.11",
                    "0",
                    "0",
                    "",
                    "408.407",
                    "5996.378",
                    "-63136.31",
                    "1162789.74",
                ]
            )
        ]
        fields = assign_cells(tokens, cols)["fields"]
        self.assertNotEqual(fields.get("sales_value"), "5996.378")
        self.assertEqual(fields.get("sales_return_qty"), "5996.378")
        self.assertEqual(fields.get("free_out_qty"), "408.407")
        self.assertTrue(fields.get("sales_qty") in (None, ""))

    def test_pdf_word_headers_split_app_sale_and_retu_others(self):
        """Tight gaps glue App→Sale and Retu→Others; coalesce must repair."""
        raw = [
            ("Item Name", 87.1, 142.7),
            ("Pack", 244.6, 270.3),
            ("Opening", 286.6, 331.2),
            ("Purchase", 339.1, 388.6),
            ("Purchase", 391.6, 441.1),
            ("Purchase", 444.1, 493.6),
            ("Sale", 496.6, 519.2),
            ("*", 522.3, 526.6),
            ("App Sale", 529.6, 571.7),
            ("Free", 574.8, 598.0),
            ("Sale", 601.6, 624.2),
            ("Retu Others", 627.3, 689.5),
            ("Closing", 706.6, 746.9),
        ]
        header = [
            {
                "text": text,
                "col_index": i,
                "x0": x0,
                "x1": x1,
                "x_center": ((x0 + x1) / 2.0) / 841.0,
            }
            for i, (text, x0, x1) in enumerate(raw)
        ]
        fixed = _coalesce_sale_star_free_return_headers(header, 841.0)
        texts = [str(c.get("text") or "") for c in fixed]
        self.assertEqual(
            texts[-5:],
            ["Sale * App", "Sale Free", "Sale Retu", "Others", "Closing"],
        )
        cols = resolve_columns(fixed)["columns"]
        canons = [c.get("canonical") for c in cols]
        self.assertIn("sales_qty", canons)
        self.assertIn("free_out_qty", canons)
        self.assertIn("sales_return_qty", canons)
        self.assertIn("other_outward_qty", canons)
        # Sale* money must not collide; Return/Others stay distinct.
        self.assertEqual(canons.count("sales_qty"), 1)
        self.assertEqual(canons.count("sales_return_qty"), 1)
        self.assertEqual(canons.count("other_outward_qty"), 1)

    def test_footer_sale_star_money_is_sales_value_not_return_or_others(self):
        headers = [
            "Item Name",
            "Pack",
            "Opening",
            "Purchase",
            "Purchase",
            "Purchase",
            "Sale * App",
            "Sale Free",
            "Sale Retu",
            "Others",
            "Closing",
        ]
        header_cells = [
            {
                "text": h,
                "col_index": i,
                "x0": float(i * 40),
                "x1": float(i * 40 + 30),
                "x_center": float(i * 40 + 15),
            }
            for i, h in enumerate(headers)
        ]
        cols = resolve_columns(header_cells)["columns"]
        amounts = [
            "Grand Total",
            "",
            "1047060.5",
            "980198.22",
            "0",
            "0",
            "877394.70",
            "881.63801",
            "12633.696",
            "-27124.36",
            "1159698.6",
        ]
        row = [
            {
                "text": v,
                "col_index": i,
                "x": float(i * 40 + 15),
                "x0": float(i * 40),
                "x1": float(i * 40 + 30),
            }
            for i, v in enumerate(amounts)
        ]
        fields = assign_cells(row, cols)["fields"]
        self.assertEqual(fields.get("sales_qty"), "877394.70")
        self.assertEqual(fields.get("sales_return_qty"), "12633.696")
        self.assertEqual(fields.get("other_outward_qty"), "-27124.36")
        footer = _footer_totals_from_page_results(
            [{"header_cells": header_cells, "data_rows": [row]}]
        )
        self.assertAlmostEqual(footer.get("sales_value"), 877394.70, places=2)
        self.assertAlmostEqual(footer.get("closing_value"), 1159698.6, places=2)
        self.assertNotAlmostEqual(footer.get("sales_value"), 12633.696, places=2)
        self.assertNotAlmostEqual(footer.get("sales_value"), -27124.36, places=2)

    def test_months_ss_sale_fqty_ret_still_rehomes(self):
        """Sale F.Qty|Sale Ret placeholders (no analysis chrome) still shift."""
        headers = ["Item", "Op", "Pur", "Sale F.Qty", "Sale Ret", "Cl Qty"]
        cols = resolve_columns(
            [
                {"text": h, "col_index": i, "x_center": float(i * 40)}
                for i, h in enumerate(headers)
            ]
        )["columns"]
        tokens = [
            {"text": v, "col_index": i}
            for i, v in enumerate(["PROD", "10", "5", "120", "45678.50", "8"])
        ]
        fields = assign_cells(tokens, cols)["fields"]
        self.assertEqual(fields.get("sales_qty"), "120")
        self.assertEqual(fields.get("sales_value"), "45678.50")
        self.assertTrue(fields.get("free_out_qty") in (None, ""))
        self.assertTrue(fields.get("sales_return_qty") in (None, ""))

    def test_split_grand_total_lines_skip_sale_return_and_others(self):
        """PyMuPDF splits Grand Total label vs money across lines.

        Truncated bands used to park Sale Return (or signed Others) as
        sales_value when Opening/Purchase sat on earlier lines.
        """
        text = """
SALE AND STOCK ANALYSIS
Opening Purchase Purchase Purchase Sale * App Sale Free Sale Retu Others
Closing
ARJUNA TABS
60`S
71
180
109
145
Total (HM3)
1047060.5 980198.22
0
0 877394.70 881.63801 12633.696 -27124.36 1159698.6
Grand Total
1047060.5 980198.22
0
0 877394.70 881.63801 12633.696 -27124.36 1159698.6
"""
        out = extract_sales_total_value_from_text(text)
        self.assertAlmostEqual(out["sales_value"], 877394.70, places=2)
        self.assertAlmostEqual(out["closing_value"], 1159698.6, places=2)
        self.assertNotAlmostEqual(out["sales_value"], 12633.696, places=2)
        self.assertNotAlmostEqual(out["sales_value"], -27124.36, places=2)
        self.assertEqual(out["label"], "Grand Total")

    def test_truncated_sale_free_return_band_not_sale_return(self):
        sales, closing = _stvr_pick_sales_closing_from_amounts(
            [0.0, 877394.70, 881.63801, 12633.696, -27124.36, 1159698.6]
        )
        self.assertAlmostEqual(sales, 877394.70, places=2)
        self.assertAlmostEqual(closing, 1159698.6, places=2)


if __name__ == "__main__":
    unittest.main()
