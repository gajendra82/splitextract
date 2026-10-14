"""Shah Medical Agency Code/Item PDFs: Sep month period + 3-digit product codes."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _CODE_ITEM_CODE_RE,
    _CODE_ITEM_MONTH_RE,
    _is_code_item_stock_statement_text,
    _month_period,
    extract_sales_statement,
)

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
ZENITH = FIXTURES / "shah_medical_agency_zenith_sep2026.pdf"
ZEAL = FIXTURES / "shah_medical_agency_zeal_sep2026.pdf"

TITLE = (
    "SSHHAAHH MMEEDDIICCAALL AAGGEENNCCYY Page 1 Of 1\n"
    "Stock Statment : HIMALAYA [ZENITH] Sep, 2026 01/10/26\n"
    "Code Item Description Packing Opening Purchase Sales Closing "
    "Stock-Value Sales-Value\n"
)


class TestCodeItemCodeAndMonthHelpers(unittest.TestCase):
    def test_three_digit_product_codes_are_accepted(self):
        self.assertTrue(_CODE_ITEM_CODE_RE.match("725"))
        self.assertTrue(_CODE_ITEM_CODE_RE.match("701"))
        self.assertTrue(_CODE_ITEM_CODE_RE.match("3000"))
        self.assertTrue(_CODE_ITEM_CODE_RE.match("10515"))
        self.assertFalse(_CODE_ITEM_CODE_RE.match("12"))
        self.assertFalse(_CODE_ITEM_CODE_RE.match("1234567"))

    def test_month_on_title_line_preferred_over_print_date(self):
        self.assertTrue(_is_code_item_stock_statement_text(TITLE))
        m = _CODE_ITEM_MONTH_RE.search(TITLE)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1)[:3].lower(), "sep")
        self.assertEqual(m.group(2), "2026")
        start, end = _month_period(m.group(1), int(m.group(2)))
        self.assertEqual(start, "2026-09-01")
        self.assertEqual(end, "2026-09-30")


@unittest.skipUnless(ZENITH.is_file(), "missing Shah Zenith fixture")
class TestShahMedicalZenith(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(ZENITH.read_bytes(), ZENITH.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_code = {
            str(item.get("product_code")): item
            for item in cls.result.get("line_items") or []
        }

    def test_period_is_september_not_print_date(self):
        self.assertEqual(
            self.extra.get("extraction_method"), "code_item_stock_statement"
        )
        self.assertEqual(self.result.get("period_from"), "2026-09-01")
        self.assertEqual(self.result.get("period_to"), "2026-09-30")
        self.assertNotEqual(self.result.get("period_from"), "2026-10-01")

    def test_all_printed_products_including_three_digit_codes(self):
        self.assertEqual(self.result.get("stockist_name"), "SHAH MEDICAL AGENCY")
        self.assertIn("ZENITH", str(self.result.get("company_name") or "").upper())
        self.assertEqual(len(self.result.get("line_items") or []), 18)
        confido = self.by_code["725"]
        self.assertIn("CONFIDO", str(confido.get("product_name") or "").upper())
        self.assertEqual(confido.get("opening_qty"), 25.0)
        self.assertEqual(confido.get("receipts_qty"), 100.0)
        self.assertEqual(confido.get("sales_qty"), 32.0)
        self.assertEqual(confido.get("closing_qty"), 93.0)
        pilex = self.by_code["719"]
        self.assertIn("PILEX", str(pilex.get("product_name") or "").upper())
        self.assertEqual(pilex.get("sales_qty"), 141.0)


@unittest.skipUnless(ZEAL.is_file(), "missing Shah Zeal fixture")
class TestShahMedicalZeal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(ZEAL.read_bytes(), ZEAL.name)
        cls.by_code = {
            str(item.get("product_code")): item
            for item in cls.result.get("line_items") or []
        }

    def test_period_and_full_product_count(self):
        self.assertEqual(self.result.get("period_from"), "2026-09-01")
        self.assertEqual(self.result.get("period_to"), "2026-09-30")
        self.assertEqual(len(self.result.get("line_items") or []), 43)
        abana = self.by_code["701"]
        self.assertIn("ABANA", str(abana.get("product_name") or "").upper())
        liv = self.by_code["716"]
        self.assertIn("LIV", str(liv.get("product_name") or "").upper())
        self.assertEqual(liv.get("opening_qty"), 53.0)
        self.assertEqual(liv.get("sales_qty"), 106.0)
        self.assertEqual(liv.get("closing_qty"), 47.0)


if __name__ == "__main__":
    unittest.main()
