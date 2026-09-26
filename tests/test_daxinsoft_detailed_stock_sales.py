"""Profitmaker Stock & Sales Statement Detailed (O.Bal / Sal.Ret / Cl.Bal)."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _daxin_detailed_join_name,
    _is_daxinsoft_detailed_stock_sales_text,
    _is_daxinsoft_stock_sales_text,
    extract_sales_statement,
)


HEADER = (
    "SRI VEERABRAHMENDRA PHARMA\n"
    "Stock & Sales Statement Detailed\n"
    "From 01/08/2026 To 31/08/2026\n"
    "Product Name  Packing  O.Bal  Rcpts  Sal.Ret  Total  Sales  "
    "Pur.Ret  Total  Cl.Bal  Cl.Value  Age\n"
    "Company :HIMALAYA WELLNESS CO\n"
    "Generated in PROFITMAKER software\n"
)

OSTK = (
    "Stock & Sales Statement\n"
    "Product Name  Packing  O.Stk  Purc  Tot  Sale  Qoh  Value  Age\n"
)

FIXTURE = Path(
    r"C:\Users\adity\Downloads\ZL_2026_August"
    r"\0000735113_2026_08_ZL_01_406_03092026104916.Pdf"
)


class TestDaxinsoftDetailedDetection(unittest.TestCase):
    def test_detects_detailed_layout_only(self):
        self.assertTrue(_is_daxinsoft_detailed_stock_sales_text(HEADER))
        self.assertFalse(_is_daxinsoft_stock_sales_text(HEADER))
        self.assertFalse(_is_daxinsoft_detailed_stock_sales_text(OSTK))
        self.assertFalse(_is_daxinsoft_detailed_stock_sales_text(""))

    def test_joins_rotated_stockist_fragments(self):
        self.assertEqual(
            _daxin_detailed_join_name(
                ["SRI", "VEERABRAHMENDR", "HMENDRA PHARMA"]
            ),
            "SRI VEERABRAHMENDRA PHARMA",
        )


@unittest.skipUnless(FIXTURE.is_file(), "missing Detailed Profitmaker fixture")
class TestDaxinsoftDetailedFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            str(item.get("product_name") or "").upper(): item
            for item in cls.result["line_items"]
        }

    def test_uses_detailed_parser(self):
        self.assertEqual(
            self.extra.get("extraction_method"), "daxinsoft_detailed_stock_sales"
        )
        self.assertEqual(self.result["stockist_name"], "SRI VEERABRAHMENDRA PHARMA")
        self.assertIn("HIMALAYA", str(self.result.get("company_name") or "").upper())
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertEqual(len(self.result["line_items"]), 26)
        self.assertNotIn("DAXINSOFT", str(self.result.get("stockist_name") or "").upper())

    def test_columns_are_not_shifted(self):
        soap = self.by_name["AACTARIL SOAP"]
        self.assertEqual(soap["packing"], "75GMS")
        self.assertEqual(soap["opening_qty"], 54.0)
        self.assertEqual(soap["receipts_qty"], 0.0)
        self.assertEqual(soap["sales_qty"], 23.0)
        self.assertEqual(soap["closing_qty"], 31.0)
        self.assertEqual(soap["closing_value"], 2480.35)
        self.assertEqual((soap.get("extra") or {}).get("age_days"), 368.0)
        self.assertNotEqual(soap["closing_qty"], 2480.35)
        self.assertNotEqual(soap["closing_value"], 368.0)

        confido = self.by_name["CONFIDO"]
        self.assertEqual(confido["opening_qty"], 62.0)
        self.assertEqual(confido["receipts_qty"], 100.0)
        self.assertEqual(confido["sales_qty"], 75.0)
        self.assertEqual(confido["closing_qty"], 87.0)
        self.assertEqual(confido["closing_value"], 14640.95)

        liv = self.by_name["LIV.52 SYP"]
        self.assertEqual(liv["packing"], "200 ML")
        self.assertEqual(liv["opening_qty"], 0.0)
        self.assertEqual(liv["receipts_qty"], 700.0)
        self.assertEqual(liv["sales_qty"], 700.0)
        self.assertEqual(liv["closing_qty"], 0.0)
        self.assertEqual(liv["closing_value"], 0.0)

        tentex = self.by_name["TENTEX FORTE TAB"]
        self.assertEqual(tentex["packing"], "10`S")
        self.assertEqual(tentex["opening_qty"], 268.0)
        self.assertEqual(tentex["sales_qty"], 113.0)
        self.assertEqual(tentex["closing_qty"], 155.0)
        self.assertEqual(tentex["closing_value"], 15388.01)

    def test_names_stay_whole_and_footer_is_not_a_product(self):
        names = [str(item.get("product_name") or "") for item in self.result["line_items"]]
        joined = " | ".join(names).upper()
        self.assertIn("CLARINA ANTI ACNE CREAM", joined)
        self.assertIn("ORO-T ORAL RINSE 100ML", joined)
        self.assertNotIn("OPENING VALUE", joined)
        self.assertNotIn("PRODUCT NAME", joined)
        self.assertFalse(any(name.upper() == "SRI" for name in names))

    def test_printed_footer_totals(self):
        self.assertEqual(self.result["totals"].get("sales_value"), 466382.39)
        self.assertEqual(self.result["totals"].get("closing_value"), 177767.89)
        self.assertEqual(self.extra.get("opening_value"), 0.0)
        self.assertEqual(self.extra.get("purchase_value"), 387465.50)
        self.assertEqual(self.extra.get("sale_return_value"), 0.0)
        self.assertEqual(self.extra.get("purchase_return_value"), 0.0)
        self.assertEqual(self.extra.get("total_row_source"), "daxinsoft_footer")


if __name__ == "__main__":
    unittest.main()
