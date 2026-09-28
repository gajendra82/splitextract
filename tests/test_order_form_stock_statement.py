"""STOCK STATEMENT ORDER FORM CODE / PRODUCT NAME / PACK PDF."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_code_item_stock_statement_text,
    _is_order_form_stock_statement_text,
    _is_saleable_stock_report_text,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000735421_2026_08_ZL_24_258A_04092026035008.pdf"
)

HEADER = (
    "J M D MEDICAL AGENCY\n"
    "FROM DATE  01/08/2026 TO 31/08/2026\n"
    "STOCK STATEMENT  ORDER FORM  HIMALAYA\n"
    "CODE   PRODUCT NAME              PACK       OPENING   PURCHASE  "
    "PURCH.RET       SALE      VALUE   SALE RET.    STOCK  STOCK VALUE\n"
)

GANESH = (
    "GANESH AGENCY\n"
    "Stock Statment : HIMALAYA ZEAL\n"
    "Code  Item Description  Packing  Opening Purchase  Sales  Closing  "
    "Stock-Value  Sales-Value\n"
)

SALEABLE = (
    "PARAS MEDICOS\n"
    "Saleable Stock Report\n"
    "Particular | | Opn | Rec | Issue | Bal\n"
)


class TestOrderFormDetection(unittest.TestCase):
    def test_detects_layout_from_headers_not_filename(self):
        self.assertTrue(_is_order_form_stock_statement_text(HEADER))
        self.assertFalse(_is_order_form_stock_statement_text(GANESH))
        self.assertFalse(_is_order_form_stock_statement_text(SALEABLE))
        self.assertFalse(_is_order_form_stock_statement_text(""))
        self.assertFalse(_is_code_item_stock_statement_text(HEADER))
        self.assertFalse(_is_saleable_stock_report_text(HEADER))


@unittest.skipUnless(FIXTURE.is_file(), "missing JMD order-form fixture")
class TestOrderFormFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_code = {
            item.get("product_code"): item for item in cls.result["line_items"]
        }
        cls.names = [
            str(item.get("product_name") or "") for item in cls.result["line_items"]
        ]

    def test_uses_order_form_parser(self):
        self.assertEqual(self.extra.get("extraction_method"), "order_form_stock_statement")
        self.assertEqual(len(self.result["line_items"]), 82)
        self.assertEqual(self.result["stockist_name"], "J M D MEDICAL AGENCY")
        self.assertIn("HIMALAYA", str(self.result.get("company_name") or "").upper())
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertNotIn("Invoices", self.result)

    def test_product_name_excludes_pack_and_opening(self):
        soap = self.by_code["000414"]
        self.assertEqual(soap["product_name"], "AACTRAIL SOAP")
        self.assertEqual(soap["packing"], "75GM")
        self.assertEqual(soap["opening_qty"], 55.0)
        self.assertEqual(soap["receipts_qty"], 0.0)
        self.assertEqual(soap["sales_qty"], 0.0)
        self.assertEqual(soap["closing_qty"], 55.0)
        self.assertEqual(soap["closing_value"], 4037.42)
        self.assertNotIn("75GM", soap["product_name"])
        self.assertNotIn("55.00", soap["product_name"])

        joined = " | ".join(self.names)
        self.assertNotIn("75GM 55.00", joined)
        self.assertFalse(any(re.search(r"\d+\.\d{2}$", name) for name in self.names))

    def test_sample_rows_not_column_shifted(self):
        abana = self.by_code["000265"]
        self.assertEqual(abana["product_name"], "ABANA TAB")
        self.assertEqual(abana["packing"], "60S")
        self.assertEqual(abana["opening_qty"], 9.0)
        self.assertEqual(abana["sales_qty"], 6.0)
        self.assertEqual(abana["sales_value"], 936.18)
        self.assertEqual(abana["closing_qty"], 3.0)
        self.assertEqual(abana["closing_value"], 468.09)

        liv = self.by_code["000172"]
        self.assertEqual(liv["product_name"], "LIV 52 DS SYP SUGAR FREE")
        self.assertEqual(liv["packing"], "100 ML")
        self.assertEqual(liv["opening_qty"], 148.82)
        self.assertEqual(liv["closing_qty"], 148.82)

        vgel = self.by_code["000128"]
        self.assertEqual(vgel["product_name"], "V-GEL 30GM")
        self.assertEqual(vgel["packing"], "30GM")
        self.assertEqual(vgel["opening_qty"], 30.0)
        self.assertEqual(vgel["sales_qty"], 6.0)
        self.assertEqual(vgel["closing_qty"], 24.0)

    def test_printed_totals(self):
        self.assertEqual(self.result["totals"].get("sales_value"), 69822.55)
        self.assertEqual(self.result["totals"].get("closing_value"), 459701.39)
        self.assertEqual(self.extra.get("actual_sale_value"), 73756.37)


if __name__ == "__main__":
    unittest.main()
