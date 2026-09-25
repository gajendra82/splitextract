"""Landscape SwilERP Sales & Stock (Code / PACKING / Qty+Value) regression."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_swil_landscape_qty_value_text,
    _is_swil_opening_receipt_value_statement,
    extract_sales_statement,
)


FIXTURE_PDF = (
    Path(__file__).resolve().parents[1]
    / "0000737669_2026_08_ZA_25_299_07092026182012.PDF"
)

LANDSCAPE_HEADER = (
    "Sales & Stock Statement(From 01/08/2026 Upto 31/08/2026)\n"
    "Code PRODUCT NAME PACKING Op. Opening Bal Receipt Receipt/Pur "
    "Total Issue Issue/Sales Shortage Expiry Closing Closing Bala Dump Ne\n"
    "Qty. Value Qty. Value Qty. Qty. Value\n"
)

NARROW_SWIL_HEADER = (
    "Sales & Stock Statement(From 01/08/2026 Upto 31/08/2026)\n"
    "PRODUCT Opening Receipt/Pur Issue Closing\n"
)

PSR_TEXT = (
    "Product Stock Report\n"
    "ATUL MEDICO\n"
    "Product Name Opening Purchase Total Sale Closing Cls Amt\n"
    "BONNISAN DROPS 65.00 50.00 115.00 6.00 109.00 7214.71\n"
)


def _item_by_code(result, code: str):
    return next(
        item
        for item in result["line_items"]
        if str(item.get("product_code") or "") == code
    )


class TestSwilLandscapeDetection(unittest.TestCase):
    def test_detects_layout_from_headers_not_filename(self):
        self.assertTrue(_is_swil_landscape_qty_value_text(LANDSCAPE_HEADER))
        self.assertTrue(_is_swil_opening_receipt_value_statement(LANDSCAPE_HEADER))

    def test_rejects_other_statement_layouts(self):
        self.assertFalse(_is_swil_landscape_qty_value_text(NARROW_SWIL_HEADER))
        self.assertFalse(_is_swil_landscape_qty_value_text(PSR_TEXT))
        self.assertFalse(_is_swil_landscape_qty_value_text(""))
        self.assertTrue(_is_swil_opening_receipt_value_statement(NARROW_SWIL_HEADER))


class TestSwilLandscapeFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FIXTURE_PDF.is_file():
            raise unittest.SkipTest(f"missing fixture {FIXTURE_PDF.name}")
        cls.result = extract_sales_statement(FIXTURE_PDF.read_bytes(), FIXTURE_PDF.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.names = [
            str(item.get("product_name") or "") for item in cls.result["line_items"]
        ]

    def test_uses_landscape_fallback_not_shifted_buckets(self):
        self.assertEqual(self.extra.get("extraction_method"), "swil_landscape_qty_value")
        self.assertTrue(self.extra.get("fallback_used"))
        self.assertGreaterEqual(len(self.result["line_items"]), 20)
        self.assertEqual(self.extra.get("pages"), 3)

    def test_schema_and_period(self):
        for key in (
            "source_file",
            "source_format",
            "stockist_name",
            "period_from",
            "period_to",
            "line_items",
            "totals",
        ):
            self.assertIn(key, self.result)
        self.assertEqual(self.result["source_format"], "pdf")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertIn("MEDICINE HOUSE", str(self.result.get("stockist_name") or ""))
        item = self.result["line_items"][0]
        for key in (
            "product_name",
            "opening_qty",
            "receipts_qty",
            "sales_qty",
            "sales_value",
            "closing_qty",
            "closing_value",
        ):
            self.assertIn(key, item)

    def test_headers_and_footers_are_not_products(self):
        joined = " | ".join(self.names).upper()
        self.assertNotIn("PRODUCT NAME", joined)
        self.assertNotIn("GRAND TOTAL", joined)
        self.assertNotIn("POWERED BY", joined)
        self.assertFalse(any("PURCHASE INVOICE" in name.upper() for name in self.names))
        self.assertFalse(any(name.startswith("5336") for name in self.names))

    def test_no_duplicate_product_codes(self):
        codes = [
            item.get("product_code")
            for item in self.result["line_items"]
            if item.get("product_code")
        ]
        self.assertEqual(len(codes), len(set(codes)))

    def test_first_row_explicit_values(self):
        cystone = _item_by_code(self.result, "HIM125")
        self.assertIn("CYSTONE", str(cystone["product_name"]).upper())
        self.assertEqual(cystone["opening_qty"], 1.0)
        self.assertEqual(cystone["receipts_qty"], 28.0)
        self.assertEqual(cystone["sales_qty"], 28.0)
        self.assertEqual(cystone["sales_value"], 5261.11)
        self.assertEqual(cystone["closing_qty"], 1.0)
        self.assertEqual(cystone["closing_value"], 179.54)
        self.assertEqual(cystone["extra"]["opening_value"], 179.54)
        self.assertEqual(cystone["extra"]["total_stock"], 29.0)

    def test_liv52_ds_tablets_and_negative_closing_preserved(self):
        liv = _item_by_code(self.result, "HM38")
        self.assertEqual(liv["opening_qty"], 107.0)
        self.assertEqual(liv["receipts_qty"], 600.0)
        self.assertEqual(liv["sales_qty"], 131.0)
        self.assertEqual(liv["sales_value"], 25548.57)
        self.assertEqual(liv["closing_qty"], 576.0)
        self.assertEqual(liv["closing_value"], 111334.71)

        cystone_tab = _item_by_code(self.result, "HM03")
        self.assertEqual(cystone_tab["opening_qty"], 0.0)
        self.assertEqual(cystone_tab["receipts_qty"], 180.0)
        self.assertEqual(cystone_tab["sales_qty"], 200.0)
        self.assertEqual(cystone_tab["closing_qty"], -20.0)
        self.assertEqual(cystone_tab["closing_value"], -3887.94)

    def test_page_two_rows_continue_without_header_repeat(self):
        mentat = _item_by_code(self.result, "HM39")
        self.assertIn("MENTAT", str(mentat["product_name"]).upper())
        self.assertEqual(mentat["opening_qty"], 0.0)
        self.assertEqual(mentat["receipts_qty"], 57.0)
        self.assertEqual(mentat["sales_qty"], 8.0)
        self.assertEqual(mentat["sales_value"], 1452.84)
        self.assertEqual(mentat["closing_qty"], 49.0)
        self.assertEqual(mentat["closing_value"], 8484.11)
        evecare = _item_by_code(self.result, "HM42")
        self.assertEqual(evecare["receipts_qty"], 140.0)
        self.assertEqual(evecare["sales_qty"], 15.0)
        self.assertEqual(evecare["closing_qty"], 129.0)

    def test_stock_identity_reasonable_and_values_not_recalculated(self):
        fails = 0
        for item in self.result["line_items"]:
            opening = float(item.get("opening_qty") or 0)
            receipts = float(item.get("receipts_qty") or 0)
            sales = float(item.get("sales_qty") or 0)
            closing = float(item.get("closing_qty") or 0)
            if abs(round(opening + receipts - sales, 2) - round(closing, 2)) > 0.51:
                fails += 1
        self.assertLessEqual(fails, 1)
        self.assertEqual(self.extra.get("invalid_rows"), fails)
        liv = _item_by_code(self.result, "HM38")
        # Must keep printed Cl Val, not closing_qty * sales_value / sales_qty.
        rewritten = round(576.0 * (25548.57 / 131.0), 2)
        self.assertEqual(liv["closing_value"], 111334.71)
        self.assertNotEqual(liv["closing_value"], rewritten)

    def test_printed_totals_preserved(self):
        self.assertEqual(self.result["totals"].get("sales_value"), 143192.91)
        self.assertEqual(self.result["totals"].get("closing_value"), 279437.94)


if __name__ == "__main__":
    unittest.main()
