"""STOCK & SALES ANALYSIS QTY/VALUE image — keep printed opening_value."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _apply_ssa_qty_value_fields,
    _looks_like_ssa_qty_value_result,
    _ssa_qty_value_opening_count,
    _ssa_qty_value_opening_missing,
    empty_result,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000703871_2026_08_ZL_04_342_03092026165141.jpg"
)


def _ssa_result_without_opening_value():
    result = empty_result("stmt.jpg", "jpg")
    result["report_title"] = "STOCK & SALES ANALYSIS"
    result["line_items"] = [
        {
            "product_name": "AACTARIL SOAP 75G",
            "opening_qty": 37.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "sales_value": 0.0,
            "closing_qty": 37.0,
            "closing_value": 2648.83,
            "extra": {},
        },
        {
            "product_name": "ABANA TABS 60",
            "opening_qty": 63.0,
            "receipts_qty": 200.0,
            "sales_qty": 30.0,
            "sales_value": 4700.06,
            "closing_qty": 233.0,
            "closing_value": 34623.80,
            "extra": {},
        },
        {
            "product_name": "ARJUNA TABS 60",
            "opening_qty": 76.0,
            "receipts_qty": 0.0,
            "sales_qty": 4.0,
            "sales_value": 1616.96,
            "closing_qty": 72.0,
            "closing_value": 15885.36,
            "extra": {},
        },
    ]
    return result


class TestSsaQtyValueDetection(unittest.TestCase):
    def test_detects_analysis_with_money_columns(self):
        result = _ssa_result_without_opening_value()
        self.assertTrue(_looks_like_ssa_qty_value_result(result))
        self.assertTrue(_ssa_qty_value_opening_missing(result))
        self.assertEqual(_ssa_qty_value_opening_count(result), 0)

    def test_does_not_trigger_qty_only_or_other_formats(self):
        qty_only = empty_result("busy.jpg", "jpg")
        qty_only["report_title"] = "STOCK & SALES ANALYSIS"
        qty_only["line_items"] = [
            {
                "product_name": "LIV 52 TAB",
                "opening_qty": 10.0,
                "sales_qty": 2.0,
                "closing_qty": 8.0,
                "sales_value": 0.0,
                "closing_value": 0.0,
                "extra": {},
            }
        ] * 4
        self.assertFalse(_looks_like_ssa_qty_value_result(qty_only))
        self.assertFalse(_ssa_qty_value_opening_missing(qty_only))

        psr = empty_result("psr.jpg", "jpg")
        psr["report_title"] = "Product Stock Report"
        psr["line_items"] = _ssa_result_without_opening_value()["line_items"]
        self.assertFalse(_looks_like_ssa_qty_value_result(psr))

    def test_apply_keeps_printed_opening_value(self):
        result = empty_result("stmt.jpg", "jpg")
        parsed = {
            "stockist_name": "RAHUL AGENCY",
            "company_name": "HIMALAYA ZEAL",
            "report_title": "STOCK & SALES ANALYSIS",
            "line_items": [
                {
                    "product_name": "AACTARIL SOAP 75G",
                    "opening_qty": 37,
                    "opening_value": 2648.83,
                    "receipts_qty": 0,
                    "sales_qty": 0,
                    "sales_value": 0,
                    "closing_qty": 37,
                    "closing_value": 2648.83,
                    "extra": {"dump_qty": 37},
                }
            ],
            "totals": {},
        }
        applied = _apply_ssa_qty_value_fields(result, parsed)
        item = applied["line_items"][0]
        self.assertEqual(item["opening_qty"], 37.0)
        self.assertEqual(item["extra"]["opening_value"], 2648.83)
        self.assertEqual(item["closing_value"], 2648.83)
        self.assertEqual(item["extra"]["dump_qty"], 37.0)
        self.assertEqual(
            applied["totals"]["extra"]["extraction_method"], "ssa_qty_value_vision"
        )


@unittest.skipUnless(FIXTURE.is_file(), "missing RAHUL AGENCY fixture")
class TestSsaQtyValueFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}

    def test_opening_value_populated(self):
        self.assertGreaterEqual(len(self.result.get("line_items") or []), 10)
        self.assertGreaterEqual(_ssa_qty_value_opening_count(self.result), 8)
        aactaril = next(
            item
            for item in self.result["line_items"]
            if "AACTARIL" in str(item.get("product_name") or "").upper()
        )
        self.assertEqual(aactaril["opening_qty"], 37.0)
        self.assertAlmostEqual(aactaril["extra"]["opening_value"], 2648.83, places=1)
        self.assertEqual(aactaril["closing_qty"], 37.0)
        self.assertAlmostEqual(aactaril["closing_value"], 2648.83, places=1)
        self.assertNotEqual(aactaril["extra"]["opening_value"], 0)

    def test_does_not_use_invoice_wrapper(self):
        self.assertNotIn("Invoices", self.result)
        self.assertIn("line_items", self.result)
        self.assertIn("RAHUL", str(self.result.get("stockist_name") or "").upper())


if __name__ == "__main__":
    unittest.main()
