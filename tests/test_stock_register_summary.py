"""STOCK REGISTER - SUMMARY keeps blank S.RTN and P.RTN as zero."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_stock_register_summary_text,
    _is_psr_closstock_text,
    extract_sales_statement,
)


HEADER = """
KEDIA ASSOCIATES
STOCK REGISTER - SUMMARY
PERIOD : 01-08-2026 TO 31-08-2026
ITEM NAME UOM O.B. PUR. S.RTN SALE P.RTN C.B. Expir
"""

OTHER = """
Product Stock Report
ITEM NAME Opening Purchase Sale Closing
"""

FIXTURE = Path(
    r"C:\Users\adity\Downloads\ZL_2026_August"
    r"\0000734539_2026_08_ZL_18_375_04092026181930.pdf"
)


class TestStockRegisterDetection(unittest.TestCase):
    def test_detects_only_this_register(self):
        self.assertTrue(_is_stock_register_summary_text(HEADER))
        self.assertFalse(_is_stock_register_summary_text(OTHER))
        self.assertFalse(_is_psr_closstock_text(HEADER))
        self.assertFalse(_is_stock_register_summary_text(""))


@unittest.skipUnless(FIXTURE.is_file(), "missing stock register fixture")
class TestStockRegisterFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            item["product_name"]: item for item in cls.result["line_items"]
        }

    def test_header(self):
        self.assertEqual(self.extra.get("extraction_method"), "stock_register_summary")
        self.assertEqual(self.result["stockist_name"], "KEDIA ASSOCIATES")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertGreater(len(self.result["line_items"]), 40)
        names = " | ".join(self.by_name).upper()
        self.assertNotIn("TOTAL", names)
        self.assertNotIn("PUR.VAL", names)

    def test_blank_return_columns_do_not_shift_sale(self):
        blem = self.by_name["BLEMINOR ANTI BLEMISH CRE"]
        self.assertEqual(blem["packing"], "30ML")
        self.assertEqual(blem["opening_qty"], 6.0)
        self.assertEqual(blem["receipts_qty"], 0.0)
        self.assertEqual(blem["extra"]["sales_return_qty"], 0.0)
        self.assertEqual(blem["sales_qty"], 0.0)
        self.assertEqual(blem["extra"]["purchase_return_qty"], 0.0)
        self.assertEqual(blem["closing_qty"], 6.0)
        self.assertEqual(blem["extra"]["expiry"], "03-28")
        self.assertEqual(blem["extra"]["manufacturer"], "HIMALAYA ZEAL")

        clarina = self.by_name["CLARINA CREAM"]
        self.assertEqual(clarina["opening_qty"], 61.0)
        self.assertEqual(clarina["sales_qty"], 7.0)
        self.assertEqual(clarina["closing_qty"], 54.0)
        self.assertNotEqual(clarina["closing_qty"], 7.0)

        liv = self.by_name["LIV-52 SYP 200ML"]
        self.assertEqual(liv["packing"], "200ML")
        self.assertEqual(liv["opening_qty"], 113.0)
        self.assertEqual(liv["sales_qty"], 112.0)
        self.assertEqual(liv["closing_qty"], 1.0)
        self.assertNotEqual(liv["sales_qty"], 1.0)

    def test_printed_returns_stay_in_their_columns(self):
        vgel = self.by_name["V-GEL"]
        self.assertEqual(vgel["opening_qty"], 8.0)
        self.assertEqual(vgel["extra"]["sales_return_qty"], 3.0)
        self.assertEqual(vgel["sales_qty"], 7.0)
        self.assertEqual(vgel["closing_qty"], 4.0)
        self.assertEqual(vgel["extra"]["expiry"], "12-25")
        self.assertEqual(vgel["extra"]["manufacturer"], "HIMALAYA ZANDRA")

        bonni = self.by_name["BONNISAN 200ML"]
        self.assertEqual(bonni["opening_qty"], 1340.0)
        self.assertEqual(bonni["extra"]["sales_return_qty"], 1.0)
        self.assertEqual(bonni["sales_qty"], 493.0)
        self.assertEqual(bonni["closing_qty"], 848.0)
        self.assertEqual(bonni["extra"]["manufacturer"], "THE HIMALAYA DRUGS")

        quista = self.by_name["QUISTA KIDZ VAN"]
        self.assertEqual(quista["opening_qty"], 8.0)
        self.assertEqual(quista["extra"]["sales_return_qty"], 5.0)
        self.assertEqual(quista["sales_qty"], 13.0)
        self.assertEqual(quista["closing_qty"], 0.0)

        rumalaya = self.by_name["RUMALAYA FORTE"]
        self.assertEqual(rumalaya["opening_qty"], 25.0)
        self.assertEqual(rumalaya["sales_qty"], 10.0)
        self.assertEqual(rumalaya["extra"]["purchase_return_qty"], 1.0)
        self.assertEqual(rumalaya["closing_qty"], 14.0)
        self.assertEqual(rumalaya["extra"]["manufacturer"], "HD5")

    def test_grand_totals(self):
        self.assertEqual(self.extra.get("opening_qty"), 5028.0)
        self.assertEqual(self.extra.get("sale_return_qty"), 17.0)
        self.assertEqual(self.extra.get("sales_qty"), 1651.0)
        self.assertEqual(self.extra.get("purchase_return_qty"), 2.0)
        self.assertEqual(self.extra.get("closing_qty"), 3392.0)
        self.assertEqual(self.result["totals"].get("sales_value"), 227548.32)
        self.assertEqual(self.result["totals"].get("closing_value"), 366174.36)
        self.assertEqual(self.extra.get("opening_value"), 585958.06)
        self.assertEqual(self.extra.get("purchase_return_value"), -178.63)


if __name__ == "__main__":
    unittest.main()
