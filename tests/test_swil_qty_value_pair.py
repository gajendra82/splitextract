"""Portrait Sales & Stock Qty/Value pairs (Opening Bal / Issue/Sales / Closing Bala)."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_swil_landscape_qty_value_text,
    _is_swil_opening_receipt_value_statement,
    _is_swil_qty_value_pair_text,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000729398_2026_08_ZL_04_8208_01092026090620.pdf"
)
LANDSCAPE = (
    Path(__file__).resolve().parents[1]
    / "0000737669_2026_08_ZA_25_299_07092026182012.PDF"
)

PAIR_HEADER = (
    "MILAN AGENCY\n"
    "Sales & Stock Statement(From 01/08/2026 Upto 31/08/2026)\n"
    "PRODUCT NAME PACKING Op. Opening Bal Receipt Receipt/Pur Total "
    "Issue Issue/Sales Closing Closing Bala Dump Near\n"
    "Qty. Value Qty. Value Qty. Qty. Value Qty. Value Stock Expiry\n"
)

NARROW_SWIL = (
    "Sales & Stock Statement(From 01/08/2026 Upto 31/08/2026)\n"
    "PRODUCT Opening Receipt/Pur Issue Closing\n"
)

LANDSCAPE_HEADER = (
    "Sales & Stock Statement(From 01/08/2026 Upto 31/08/2026)\n"
    "Code PRODUCT NAME PACKING Op. Opening Bal Receipt Receipt/Pur "
    "Total Issue Issue/Sales Shortage Expiry Closing Closing Bala Dump Ne\n"
    "Qty. Value Qty. Value Qty. Qty. Value\n"
)


class TestQtyValuePairDetection(unittest.TestCase):
    def test_detects_opening_bal_pair_not_narrow_swil(self):
        self.assertTrue(_is_swil_qty_value_pair_text(PAIR_HEADER))
        self.assertTrue(_is_swil_opening_receipt_value_statement(PAIR_HEADER))
        self.assertFalse(_is_swil_landscape_qty_value_text(PAIR_HEADER))
        self.assertFalse(_is_swil_qty_value_pair_text(NARROW_SWIL))
        self.assertTrue(_is_swil_opening_receipt_value_statement(NARROW_SWIL))
        self.assertTrue(_is_swil_qty_value_pair_text(LANDSCAPE_HEADER))
        self.assertFalse(_is_swil_qty_value_pair_text(""))


@unittest.skipUnless(FIXTURE.is_file(), "missing Milan Qty/Value pair fixture")
class TestQtyValuePairFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            str(item.get("product_name") or "").upper(): item
            for item in cls.result["line_items"]
        }
        cls.names = [
            str(item.get("product_name") or "") for item in cls.result["line_items"]
        ]

    def test_uses_pair_parser_not_shifted_portrait_swil(self):
        self.assertEqual(self.extra.get("extraction_method"), "swil_qty_value_pair")
        self.assertFalse(self.extra.get("vertex_ai_used"))
        self.assertEqual(self.result["stockist_name"], "MILAN AGENCY")
        self.assertIn("HIMALAYA", str(self.result.get("company_name") or "").upper())
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertGreaterEqual(len(self.result["line_items"]), 28)
        self.assertNotIn("Invoices", self.result)

    def test_opening_value_is_not_used_as_opening_qty(self):
        soap = self.by_name["AACTARIL SOAP"]
        self.assertEqual(soap["packing"], "1*75GM")
        self.assertEqual(soap["opening_qty"], 49.0)
        self.assertEqual(soap["extra"]["opening_value"], 3806.32)
        self.assertEqual(soap["receipts_qty"], 0.0)
        self.assertEqual(soap["sales_qty"], 5.0)
        self.assertEqual(soap["sales_value"], 438.10)
        self.assertEqual(soap["closing_qty"], 44.0)
        self.assertEqual(soap["closing_value"], 3417.92)
        self.assertNotEqual(soap["opening_qty"], 3806.32)
        self.assertNotEqual(soap["sales_value"], 44.0)

        abana = self.by_name["ABANA TAB"]
        self.assertEqual(abana["packing"], "1x60TAB")
        self.assertEqual(abana["opening_qty"], 62.0)
        self.assertEqual(abana["extra"]["opening_value"], 7368.70)
        self.assertEqual(abana["sales_qty"], 62.0)
        self.assertEqual(abana["closing_qty"], 0.0)
        self.assertEqual(abana["closing_value"], 0.0)

        bleminor = next(
            item
            for item in self.result["line_items"]
            if "BLEMINOR" in str(item.get("product_name") or "").upper()
        )
        self.assertEqual(bleminor["opening_qty"], 50.0)
        self.assertEqual(bleminor["extra"]["opening_value"], 8245.00)
        self.assertEqual(bleminor["sales_qty"], 0.0)
        self.assertEqual(bleminor["closing_qty"], 50.0)
        self.assertEqual(bleminor["closing_value"], 8245.00)

        hadjod = self.by_name["HADJOD TAB"]
        self.assertEqual(hadjod["opening_qty"], 169.0)
        self.assertEqual(hadjod["receipts_qty"], 360.0)
        self.assertEqual(hadjod["extra"]["receipts_value"], 75456.00)
        self.assertEqual(hadjod["sales_qty"], 0.0)
        self.assertEqual(hadjod["closing_qty"], 529.0)
        self.assertEqual(hadjod["closing_value"], 112742.47)

    def test_total_row_is_not_a_product(self):
        joined = " | ".join(self.names).upper()
        self.assertNotIn("TOTAL", joined)
        self.assertEqual(self.result["totals"].get("sales_value"), 661842.00)
        self.assertEqual(self.result["totals"].get("closing_value"), 1928041.45)


@unittest.skipUnless(LANDSCAPE.is_file(), "missing landscape Swil fixture")
class TestLandscapeSwilNotStolen(unittest.TestCase):
    def test_landscape_still_uses_existing_parser(self):
        result = extract_sales_statement(LANDSCAPE.read_bytes(), LANDSCAPE.name)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "swil_landscape_qty_value")


if __name__ == "__main__":
    unittest.main()
