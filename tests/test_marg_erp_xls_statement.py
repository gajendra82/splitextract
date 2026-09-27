"""Marg ERP Himalaya/Zeal STOCK & SALES STATEMENT (.xls) — format-specific checks."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _find_marg_erp_xls_header,
    _parse_marg_erp_xls,
    extract_sales_statement,
)

SAMPLE = Path(
    r"C:\Users\amnsa\Downloads\ZL_2026_August\0000700212_2026_08_ZL_18_373_03092026133511.xls"
)


class TestMargErpHeaderDetect(unittest.TestCase):
    def test_purchase_sale_rate_header(self):
        rows = [
            ["AMIT PHARMACEUTICALS"],
            [
                "PRODUCT DESCRIPTION",
                "OPENING\nSTOCK",
                "PURCHASE\nQUANTITY",
                "SALE RETURN\nQUANTITY",
                "REPLACE+\nOTHERS",
                "TOTAL\nRECEIVE",
                "SALE\nQUANTITY",
                "P/R\nQUANTITY",
                "REPLACE+\nOTHERS",
                "CLOSING\nSTOCK",
                "RATE",
            ],
        ]
        found = _find_marg_erp_xls_header(rows)
        self.assertIsNotNone(found)
        idx, roles, variant = found
        self.assertEqual(idx, 1)
        self.assertEqual(variant, "purchase_sale_rate")
        self.assertEqual(roles["product_name"], 0)
        self.assertEqual(roles["opening_qty"], 1)
        self.assertEqual(roles["sales_qty"], 6)
        self.assertEqual(roles["closing_qty"], 9)
        self.assertEqual(roles["rate"], 10)
        self.assertEqual(roles["replace_in_qty"], 4)
        self.assertEqual(roles["replace_out_qty"], 8)
        self.assertNotIn("last_month_sale", roles)

    def test_legacy_item_op_header_not_detected_as_marg(self):
        rows = [
            ["Item", "Pack", "Op.", "Pur", "Sale", "Bal.", "BVal", "SVal"],
            ["FOO TAB", "10s", 1, 2, 3, 0, 10, 20],
        ]
        self.assertIsNone(_find_marg_erp_xls_header(rows))

    def test_parse_rows_maps_qty_and_rate_values(self):
        rows = [
            ["AMIT PHARMACEUTICALS"],
            ["PLOT NO-A/17,ASHOK NAGAR BHUBANESWAR-751009 ODISHA"],
            ["-THE HIMALAYA-ZEAL STOCK & SALES STATEMENT 01-08-2026 - 31-08-2026"],
            [
                "PRODUCT DESCRIPTION",
                "OPENING\nSTOCK",
                "PURCHASE\nQUANTITY",
                "SALE RETURN\nQUANTITY",
                "REPLACE+\nOTHERS",
                "TOTAL\nRECEIVE",
                "SALE\nQUANTITY",
                "P/R\nQUANTITY",
                "REPLACE+\nOTHERS",
                "CLOSING\nSTOCK",
                "RATE",
            ],
            ["CLARINA CREAM 30GM", 37.0, 0.0, 0.0, 0.0, 37.0, 3.0, 0.0, 0.0, 34.0, 116.71],
            ["LIV 52 TAB 100 S", 69.0, 100.0, 0.0, 0.0, 169.0, 55.0, 0.0, 0.0, 114.0, 148.6],
            ["TOTAL QUANTITY", 106.0, 100.0, 0.0, 0.0, 206.0, 58.0, 0.0, 0.0, 148.0, 0.0],
            ["TOTAL VALUE", 1000.0, 2000.0, 0.0, 0.0, 3000.0, 8516.0, 0.0, 0.0, 20900.0, 0.0],
        ]
        header = _find_marg_erp_xls_header(rows)
        result = _parse_marg_erp_xls(rows, "sample.xls", ".xls", header)
        self.assertEqual(result["stockist_name"], "AMIT PHARMACEUTICALS")
        self.assertEqual(result["company_name"], "THE HIMALAYA-ZEAL")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        self.assertEqual(len(result["line_items"]), 2)
        cream = result["line_items"][0]
        self.assertEqual(cream["opening_qty"], 37.0)
        self.assertEqual(cream["receipts_qty"], 0.0)
        self.assertEqual(cream["sales_qty"], 3.0)
        self.assertEqual(cream["closing_qty"], 34.0)
        self.assertEqual(cream["sales_value"], 350.13)
        self.assertEqual(cream["closing_value"], 3968.14)
        liv = result["line_items"][1]
        self.assertEqual(liv["receipts_qty"], 100.0)
        self.assertEqual(liv["sales_qty"], 55.0)
        self.assertEqual(result["totals"]["sales_value"], 8516.0)
        self.assertEqual(result["totals"]["closing_value"], 20900.0)
        self.assertNotIn("last_month_sale", cream["extra"])
        self.assertNotIn("last_month_sale_value", result["totals"]["extra"])

    def test_last_month_sale_column_does_not_shift_sale_or_rate(self):
        rows = [
            ["B.K.ENTERPRISERS"],
            ["PLOT NO.487,BHINGARPUR, BALIANTA,KHURDA-752115, ODISHA."],
            ["HIMALAYA ZEAL STOCK & SALES STATEMENT 01-08-2026 - 31-08-2026"],
            [
                "PRODUCT DESCRIPTION",
                "OPENING\nSTOCK",
                "PURCHASE\nQUANTITY",
                "SALE RETURN\nQUANTITY",
                "REPLACE+\nOTHERS",
                "TOTAL\nRECEIVE",
                "SALE\nQUANTITY",
                "P/R\nQUANTITY",
                "REPLACE+\nOTHERS",
                "CLOSING\nSTOCK",
                "RATE",
                "LAST\nMONTH SALE",
            ],
            ["CONFIDO (TAB) 60S", 3.0, 100.0, 0.0, 0.0, 103.0, 3.0, 0.0, 0.0, 100.0, 155.92, 7.0],
            ["LIV 52 SYP 200ML", 87.0, 140.0, 0.0, 0.0, 227.0, 103.0, 0.0, 0.0, 124.0, 153.31, 88.0],
            ["TOTAL QUANTITY", 90.0, 240.0, 0.0, 0.0, 330.0, 106.0, 0.0, 0.0, 224.0, 0.0, 95.0],
            ["TOTAL VALUE", 50911.0, 55087.0, 0.0, 0.0, 105998.0, 33390.0, 0.0, 0.0, 71796.0, 0.0, 26947.66],
        ]
        found = _find_marg_erp_xls_header(rows)
        self.assertIsNotNone(found)
        _idx, roles, variant = found
        self.assertEqual(variant, "purchase_sale_rate")
        self.assertEqual(roles["sales_qty"], 6)
        self.assertEqual(roles["closing_qty"], 9)
        self.assertEqual(roles["rate"], 10)
        self.assertEqual(roles["last_month_sale"], 11)
        result = _parse_marg_erp_xls(rows, "sample.xls", ".xls", found)
        confido = result["line_items"][0]
        self.assertEqual(confido["sales_qty"], 3.0)
        self.assertEqual(confido["sales_value"], 467.76)
        self.assertEqual(confido["closing_qty"], 100.0)
        self.assertEqual(confido["extra"]["rate"], 155.92)
        self.assertEqual(confido["extra"]["last_month_sale"], 7.0)
        liv = result["line_items"][1]
        self.assertEqual(liv["sales_qty"], 103.0)
        self.assertEqual(liv["extra"]["last_month_sale"], 88.0)
        self.assertEqual(result["totals"]["sales_value"], 33390.0)
        self.assertEqual(result["totals"]["closing_value"], 71796.0)
        self.assertEqual(result["totals"]["extra"]["last_month_sale_qty"], 95.0)
        self.assertEqual(result["totals"]["extra"]["last_month_sale_value"], 26947.66)


@unittest.skipUnless(SAMPLE.exists(), "sample Marg ERP xls not on this machine")
class TestMargErpSampleFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(SAMPLE.read_bytes(), SAMPLE.name)

    def test_metadata_and_totals(self):
        self.assertEqual(self.result["stockist_name"], "AMIT PHARMACEUTICALS")
        self.assertIn("BHUBANESWAR", self.result["stockist_address"] or "")
        self.assertEqual(self.result["company_name"], "THE HIMALAYA-ZEAL")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertEqual(self.result["totals"]["sales_value"], 68333.0)
        self.assertEqual(self.result["totals"]["closing_value"], 208868.0)

    def test_line_items_not_header_junk(self):
        names = [i["product_name"] for i in self.result["line_items"]]
        self.assertEqual(len(names), 27)
        self.assertTrue(all("MARG ERP" not in n.upper() for n in names))
        self.assertTrue(all(not n.upper().startswith("TOTAL") for n in names))
        pilex = next(i for i in self.result["line_items"] if i["product_name"] == "PILEX TAB 60 S")
        self.assertEqual(pilex["opening_qty"], 200.0)
        self.assertEqual(pilex["sales_qty"], 105.0)
        self.assertEqual(pilex["closing_qty"], 95.0)
        self.assertEqual(pilex["extra"]["rate"], 158.05)


if __name__ == "__main__":
    unittest.main()
