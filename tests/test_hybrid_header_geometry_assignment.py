"""Hybrid 10-col: header OCR geometry assignment + stockist scoring.

Covers Op.Stk/Purch/Total/Sl·Iss/Cl.Stk/Rate/Cl.Val layouts without
hardcoding a stockist, product, statement id, or image-specific values.
"""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import _detect_stockist_from_page_text
from services.stock_geometry_hybrid_v2 import (
    HYBRID_NUMERIC_FIELDS,
    VALUE_FIELDS,
    _ocr_header_confident,
    _score_header_columns,
)
from services.stock_geometry_hybrid_v3 import _normalize_numeric
from services.stock_geometry_table_v3 import classify_grid_row, geometry_row_to_line_item
from services.stock_header_resolver import assign_cells, normalize_header, resolve_columns


def _portal_header_cells():
    """Generic Op.Stk … Cl.Val header band (OCR chrome prefixes allowed)."""
    texts = [
        "Sr.",
        "| Product Name",
        "| Pack",
        "| Op.Stk",
        "Purch",
        "Total",
        "— Sl/Iss",
        "| CI.Stk",
        "~ Rate",
        "—Ci.Val",
    ]
    # Synthetic x centres matching a typical 10-col grid.
    xs = [80, 190, 325, 390, 455, 515, 575, 640, 695, 755]
    return [
        {"text": t, "col_index": i, "x_center": float(xs[i])}
        for i, t in enumerate(texts)
    ]


class PortalHeaderResolverTests(unittest.TestCase):
    def test_sr_is_serial_not_sale_return(self):
        cols = resolve_columns(_portal_header_cells())["columns"]
        by_i = {c["col_index"]: c for c in cols}
        self.assertEqual(by_i[0]["canonical"], "ignore")
        self.assertNotEqual(by_i[0]["canonical"], "sales_return_qty")

    def test_ci_val_is_closing_value_not_closing_qty(self):
        cols = resolve_columns(_portal_header_cells())["columns"]
        by_i = {c["col_index"]: c for c in cols}
        self.assertEqual(by_i[7]["canonical"], "closing_qty")
        self.assertEqual(by_i[9]["canonical"], "closing_value")
        self.assertEqual(by_i[8]["canonical"], "rate")
        self.assertEqual(by_i[3]["canonical"], "opening_qty")
        self.assertEqual(by_i[6]["canonical"], "sales_qty")

    def test_ocr_chrome_stripped_in_normalize(self):
        self.assertEqual(normalize_header("—Ci.Val"), "ci val")
        self.assertEqual(normalize_header("| CI.Stk"), "ci stk")
        self.assertEqual(normalize_header("~ Rate"), "rate")

    def test_header_score_prefers_portal_layout(self):
        cols = resolve_columns(_portal_header_cells())["columns"]
        self.assertTrue(_ocr_header_confident(cols))
        self.assertGreater(_score_header_columns(cols), 20)


class GeometryTokenAssignmentTests(unittest.TestCase):
    def _columns_with_ranges(self):
        resolved = resolve_columns(_portal_header_cells())["columns"]
        # Attach x0/x1 from centres (± half gap) so assign_cells can use geometry.
        xs = [80, 190, 325, 390, 455, 515, 575, 640, 695, 755]
        for i, c in enumerate(resolved):
            c["x0"] = xs[i] - 25
            c["x1"] = xs[i] + 25
            c["x_center"] = float(xs[i])
        return resolved

    def test_token_x_center_assigns_closing_value_not_qty(self):
        columns = self._columns_with_ranges()
        # Tokens only for printed cells; purchase blank → no token near x=455.
        tokens = [
            {"text": "PROD A", "x": 190, "y": 10},
            {"text": "28", "x": 390, "y": 10},  # opening
            {"text": "28", "x": 515, "y": 10},  # total
            {"text": "26", "x": 575, "y": 10},  # sale
            {"text": "2", "x": 640, "y": 10},  # closing qty
            {"text": "4425", "x": 755, "y": 10},  # closing value
        ]
        out = assign_cells(tokens, columns)
        fields = out["fields"]
        self.assertEqual(fields.get("opening_qty"), "28")
        self.assertIsNone(fields.get("purchase_qty"))  # missing middle
        self.assertEqual(fields.get("total_qty"), "28")
        self.assertEqual(fields.get("sales_qty"), "26")
        self.assertEqual(fields.get("closing_qty"), "2")
        self.assertEqual(fields.get("closing_value"), "4425")
        self.assertIsNone(fields.get("sales_return_qty"))

    def test_missing_middle_column_does_not_shift(self):
        columns = self._columns_with_ranges()
        tokens = [
            {"text": "PROD B", "x": 190, "y": 10},
            {"text": "10", "x": 390, "y": 10},
            # purchase (455) absent
            {"text": "10", "x": 515, "y": 10},
            {"text": "1", "x": 575, "y": 10},
            {"text": "9", "x": 640, "y": 10},
            {"text": "1719", "x": 755, "y": 10},
        ]
        out = assign_cells(tokens, columns)
        f = out["fields"]
        self.assertEqual(f.get("opening_qty"), "10")
        self.assertIsNone(f.get("purchase_qty"))
        self.assertEqual(f.get("sales_qty"), "1")
        self.assertEqual(f.get("closing_qty"), "9")
        self.assertEqual(f.get("closing_value"), "1719")
        # Sale return must stay absent — not back-filled from sale/total.
        self.assertIsNone(f.get("sales_return_qty"))

    def test_line_item_keeps_closing_value_separate(self):
        row = {
            "product_name_ocr": "GENERIC SYP",
            "selected": {
                "product_name": "GENERIC SYP",
                "pack": "100ML",
                "opening_qty": 28.0,
                "purchase_qty": 0.0,
                "total_qty": 28.0,
                "sales_qty": 0.0,
                "sales_return_qty": None,
                "closing_qty": 28.0,
                "closing_value": 4425.0,
                "rate": 158.04,
            },
            "business_fields": {
                "opening_qty": 28.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "sales_return_qty": None,
                "closing_qty": 28.0,
                "closing_value": 4425.0,
                "total_qty": 28.0,
            },
        }
        item = geometry_row_to_line_item(row)
        self.assertEqual(item["closing_qty"], 28.0)
        self.assertEqual(item["closing_value"], 4425.0)
        self.assertNotEqual(item["closing_qty"], item["closing_value"])
        fs = (item.get("extra") or {}).get("field_source") or {}
        self.assertEqual(fs.get("closing_qty"), "printed")
        self.assertEqual(fs.get("closing_value"), "printed")
        self.assertEqual(fs.get("sales_return_qty"), "missing")


class NumericChromeStripTests(unittest.TestCase):
    def test_pipe_equals_prefix_does_not_invent(self):
        self.assertEqual(_normalize_numeric("| = 36"), (36.0, False, False))
        self.assertEqual(_normalize_numeric("62.50"), (62.5, False, False))
        self.assertEqual(_normalize_numeric("2000"), (2000.0, False, False))


class IdentityOcrPickTests(unittest.TestCase):
    def test_opening_candidate_selected_when_total_balances(self):
        from services.stock_geometry_hybrid_v3 import reconcile_row_from_ocr_candidates

        row = {
            "selected": {
                "opening_qty": 356.0,
                "purchase_qty": 0.0,
                "total_qty": 36.0,
                "sales_qty": 4.0,
                "sales_return_qty": None,
                "purchase_return_qty": None,
                "closing_qty": None,
            },
            "cells": {
                "opening_qty": {
                    "normalized": 356.0,
                    "candidates": {
                        "A_original": {"normalized": 356.0},
                        "H_max_channel": {"normalized": 36.0},
                    },
                },
                "closing_qty": {
                    "normalized": None,
                    "ocr_uncertain": True,
                    "candidates": {
                        "C_threshold": {"normalized": 32.0, "uncertain": True},
                    },
                },
            },
        }
        reconcile_row_from_ocr_candidates(row)
        self.assertEqual(row["selected"]["opening_qty"], 36.0)
        self.assertEqual(row["selected"]["closing_qty"], 32.0)

    def test_purchase_candidate_selected_when_total_balances(self):
        from services.stock_geometry_hybrid_v3 import reconcile_row_from_ocr_candidates

        row = {
            "selected": {
                "opening_qty": 28.0,
                "purchase_qty": 60.0,
                "total_qty": 28.0,
                "sales_qty": 0.0,
                "closing_qty": 28.0,
            },
            "cells": {
                "opening_qty": {
                    "normalized": 28.0,
                    "candidates": {"A_original": {"normalized": 28.0}},
                },
                "purchase_qty": {
                    "normalized": 60.0,
                    "candidates": {
                        "A_original": {"normalized": 60.0},
                        "C_threshold": {"normalized": 0.0},
                        "E_contrast": {"normalized": 0.0},
                    },
                },
                "closing_qty": {
                    "normalized": 28.0,
                    "candidates": {"A_original": {"normalized": 28.0}},
                },
            },
        }
        reconcile_row_from_ocr_candidates(row)
        self.assertEqual(row["selected"]["purchase_qty"], 0.0)
        self.assertEqual(row["selected"]["opening_qty"], 28.0)


class StockistConsistencyTests(unittest.TestCase):
    def test_prefers_distributor_banner_over_nav_chrome(self):
        text = (
            "Home Profile Agency Retailer MR. Contact us\n"
            "ACME\n"
            "PHARMA DISTRIBUTORS\n"
            "Period From 01/01/2026 To 31/01/2026\n"
        )
        name = _detect_stockist_from_page_text(text)
        self.assertIsNotNone(name)
        self.assertIn("DISTRIBUTORS", name.upper())
        self.assertNotIn("Contact", name)
        self.assertNotIn("Home", name)
        self.assertNotIn("Profile", name)

    def test_low_confidence_nav_only_stays_unidentified(self):
        text = "Home Profile Agency Retailer MR. Contact us\nDashboard\nUploads\n"
        self.assertIsNone(_detect_stockist_from_page_text(text))


class ProductRowGateStillIntact(unittest.TestCase):
    def test_hybrid_numeric_fields_include_value(self):
        self.assertIn("closing_value", VALUE_FIELDS)
        self.assertIn("closing_value", HYBRID_NUMERIC_FIELDS)
        self.assertIn("closing_qty", HYBRID_NUMERIC_FIELDS)

    def test_banner_without_qty_still_rejected(self):
        selected = {
            "opening_qty": None,
            "purchase_qty": None,
            "sales_qty": None,
            "closing_qty": None,
            "total_qty": None,
        }
        cls, why = classify_grid_row(
            "ACME PHARMA DISTRIBUTORS", selected=selected
        )
        self.assertEqual(cls, "NON_PRODUCT")
        self.assertIn(
            why,
            {
                "metadata_distributor",
                "metadata_company_banner",
                "no_printed_qty_evidence",
            },
        )


if __name__ == "__main__":
    unittest.main()
