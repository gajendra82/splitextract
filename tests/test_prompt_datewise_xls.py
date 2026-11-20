"""PROMPT Stock Statement (Datewise) workbook keeps every product row."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _prompt_datewise_xls_money,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000700025_2026_08_ZA_06_337_08092026060608.xls"
)


class TestPromptDatewiseMoney(unittest.TestCase):
    def test_near_rupee_amounts_round_half_up(self):
        self.assertEqual(_prompt_datewise_xls_money(1119.9), 1120.0)
        self.assertEqual(_prompt_datewise_xls_money(1661.1), 1661.0)
        self.assertEqual(_prompt_datewise_xls_money(1429.6799999999996), 1430.0)
        self.assertEqual(_prompt_datewise_xls_money(189.28), 189.0)
        self.assertEqual(_prompt_datewise_xls_money(-1.7e-13), 0.0)


@unittest.skipUnless(FIXTURE.is_file(), "missing datewise workbook")
class TestPromptDatewiseWorkbook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            str(item.get("product_name") or ""): item
            for item in cls.result["line_items"]
        }

    def test_keeps_all_numbered_products(self):
        self.assertEqual(self.extra.get("extraction_method"), "prompt_datewise_xls")
        self.assertEqual(self.result.get("stockist_name"), "ASHISH MARKETING")
        self.assertIn("HIMLALYA", str(self.result.get("company_name") or ""))
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertEqual(len(self.result["line_items"]), 59)
        self.assertIn("V GEL OINT", self.by_name)
        self.assertIn("ARJUNA CAP", self.by_name)

    def test_bonnisan_drops_amounts(self):
        drops = self.by_name["BONNISAN DROPS"]
        self.assertEqual(drops["opening_qty"], 39)
        self.assertEqual(drops["receipts_qty"], 0)
        self.assertEqual(drops["sales_qty"], 15)
        self.assertEqual(drops["sales_value"], 1430)
        self.assertEqual(drops["closing_qty"], 24)
        self.assertEqual(drops["closing_value"], 1120)
        self.assertIsNone(drops["opening_value"])
        self.assertNotIn("source_row", drops.get("extra") or {})
