"""Product Stock Report with ClosStock / Clos.Amt columns on separate lines."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_psr_closstock_text,
    _is_product_stock_report_text,
    _parse_product_stock_report,
    extract_sales_statement,
)


ATUL = """
Product Stock Report
ATUL MEDICO
Mfg Company: HIMALAYA WELLNESS CO (ZANDRA)
From: 01/08/2026 To: 31/08/2026
Product Name Opening Purchase Total Sale SaleRet Exp/Dmg Closing Stock Cls Amt Order Qty
BONNISAN DROPS 65.00 50.00 115.00 6.00 0.00 0.00 109.00 7214.71 0.00
BONNISAN LIQ 200 ML 27.00 0.00 27.00 26.00 2.00 2.00 1.00 101.31 0.00
"""

CLOSSTOCK = """
Product Stock Report
A.B.C. AGENCIES
MFG Company: HIMALAYA (ZL)
From: 01/08/2026 To: 31/08/2026
Product Name
Opening
Purchase
Total
Sale
SaleRet
Exp/Dmg
ClosStock
Clos.Amt
OrderQty
"""

FIXTURE = Path(
    r"C:\Users\adity\Downloads\ZL_2026_August"
    r"\0000735216_2026_08_ZL_06_313_03092026122105.pdf"
)


class TestPsrClosstockDetection(unittest.TestCase):
    def test_does_not_claim_the_zandra_line_layout(self):
        self.assertTrue(_is_product_stock_report_text(ATUL))
        self.assertFalse(_is_psr_closstock_text(ATUL))
        self.assertTrue(_is_psr_closstock_text(CLOSSTOCK))
        parsed = _parse_product_stock_report(ATUL, "atul.txt", "txt")
        self.assertIsNotNone(parsed)
        liq = next(i for i in parsed["line_items"] if "200 ML" in i["product_name"])
        self.assertEqual(liq["sales_qty"], 26.0)
        self.assertEqual(liq["extra"]["sale_return"], 2.0)
        self.assertEqual(liq["extra"]["exp_damage"], 2.0)


@unittest.skipUnless(FIXTURE.is_file(), "missing ClosStock fixture")
class TestPsrClosstockFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            str(item.get("product_name") or "").upper(): item
            for item in cls.result["line_items"]
        }

    def test_uses_column_parser(self):
        self.assertEqual(self.extra.get("extraction_method"), "psr_closstock_columns")
        self.assertEqual(self.result["stockist_name"], "A.B.C. AGENCIES")
        self.assertIn("HIMALAYA", str(self.result.get("company_name") or "").upper())
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertEqual(len(self.result["line_items"]), 25)

    def test_sale_return_and_expiry_stay_in_their_columns(self):
        liv = self.by_name["LIV 52 SF SYP 200ML"]
        self.assertEqual(liv["opening_qty"], 42.0)
        self.assertEqual(liv["receipts_qty"], 0.0)
        self.assertEqual(liv["sales_qty"], 36.0)
        self.assertEqual(liv["extra"]["sale_return"], 4.0)
        self.assertEqual(liv["extra"]["exp_damage"], 0.0)
        self.assertEqual(liv["closing_qty"], 10.0)
        self.assertEqual(liv["closing_value"], 1641.30)
        self.assertEqual(liv["extra"]["order_qty"], 22.0)
        self.assertEqual(liv["sales_value"], 0.0)
        self.assertNotIn("sales_value_from_cls_amt_rate", liv["extra"])

        sf = self.by_name["LIV 52 SF SYP 100ML"]
        self.assertEqual(sf["sales_qty"], 0.0)
        self.assertEqual(sf["extra"]["sale_return"], 1.0)
        self.assertEqual(sf["extra"]["exp_damage"], 1.0)
        self.assertEqual(sf["closing_qty"], 1.0)
        self.assertEqual(sf["closing_value"], 94.98)

        pilex = self.by_name["PILEX TAB"]
        self.assertEqual(pilex["sales_qty"], 84.0)
        self.assertEqual(pilex["extra"]["sale_return"], 1.0)
        self.assertEqual(pilex["closing_qty"], 808.0)
        self.assertEqual(pilex["closing_value"], 117669.04)

        triphala = self.by_name["TRIPHALA TAB"]
        self.assertEqual(triphala["sales_qty"], 16.0)
        self.assertEqual(triphala["extra"]["sale_return"], 2.0)
        self.assertEqual(triphala["closing_qty"], 38.0)

    def test_purchase_is_not_shifted_into_sale(self):
        gasex = self.by_name["GASEX SYP 200ML (ELAICHI)"]
        self.assertEqual(gasex["opening_qty"], 37.0)
        self.assertEqual(gasex["receipts_qty"], 28.0)
        self.assertEqual(gasex["extra"]["total_stock"], 65.0)
        self.assertEqual(gasex["sales_qty"], 41.0)
        self.assertEqual(gasex["closing_qty"], 24.0)
        self.assertEqual(gasex["closing_value"], 2431.44)
        self.assertEqual(gasex["extra"]["order_qty"], 17.0)

        soap = self.by_name["AACTARIL SOAP 75 GM"]
        self.assertEqual(soap["opening_qty"], 7.0)
        self.assertEqual(soap["sales_qty"], 2.0)
        self.assertEqual(soap["closing_qty"], 5.0)
        self.assertEqual(soap["closing_value"], 390.70)
        self.assertEqual(soap["sales_value"], 0.0)

    def test_footer_totals_are_not_products(self):
        names = " | ".join(
            str(item.get("product_name") or "") for item in self.result["line_items"]
        ).upper()
        self.assertNotIn("GRAND TOTAL", names)
        self.assertNotIn("AMOUNT TOTAL", names)
        self.assertEqual(self.result["totals"].get("closing_value"), 462943.83)
        self.assertEqual(self.result["totals"].get("sales_value"), 132920.0)
        self.assertEqual(self.extra.get("opening_qty"), 3949.0)
        self.assertEqual(self.extra.get("receipts_qty"), 336.0)
        self.assertEqual(self.extra.get("sales_qty"), 1010.0)
        self.assertEqual(self.extra.get("saleret_qty"), 8.0)
        self.assertEqual(self.extra.get("exp_dmg_qty"), 1.0)
        self.assertEqual(self.extra.get("closing_qty"), 3282.0)
        self.assertEqual(self.extra.get("order_qty"), 403.0)
        self.assertEqual(self.extra.get("opening_value"), 544454.0)
        self.assertEqual(self.extra.get("purchase_value"), 40374.0)
        self.assertEqual(self.extra.get("sale_return_value"), 1308.0)
        self.assertEqual(self.extra.get("exp_dmg_value"), 95.0)
        self.assertEqual(self.extra.get("closing_amount"), 462944.0)
        self.assertEqual(self.extra.get("stock_identity_fail_count"), 0)


if __name__ == "__main__":
    unittest.main()
