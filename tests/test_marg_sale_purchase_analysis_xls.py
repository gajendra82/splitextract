"""Marg two-row STOCK & SALES ANALYSIS: SALE PURCHASES and PURCHASE SALES."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _find_marg_sale_purchase_analysis_header,
    _parse_marg_closing_mexp_xls,
    _parse_marg_opening_receipt_issue,
    _parse_marg_sale_purchase_analysis_xls,
    extract_sales_statement,
)

PLAIN = [
    ["SOME MEDICAL"],
    ["ITEM DESCRIPTION", "OPENING", "RECEIPT", "ISSUE", "CLOSING"],
    ["ABANA TAB              50'S", 10, 0, 2, 8],
]

MEXP_ROWS = [
    ["BHARAT MEDICALS"],
    ["ITEM DESCRIPTION", "OPENING", "RECEIPT", "ISSUE", "CLOSING M.EXP"],
    ["ABANA TAB              50'S.", 57, 0, 2, "    55  6/27"],
]

ANALYSIS = [
    ["MAA BIMLA PHARMA"],
    ["KUNWAR SINGH COLONY CHAS BOKARO,JHARKHAND-827013"],
    ["Phone : 7654647686 E-Mail : maabimlaph19@gmail.com"],
    ["STOCK & SALES ANALYSIS  (HIMALAYA(ZEAL)) 01-08-2026 - 31-08-2026  Reorder : Sale X 1.50"],
    [
        "ITEM DESCRIPTION",
        "OPENING",
        "",
        " SALE",
        "REPL./",
        "TOTAL",
        "",
        "PURCHASE",
        "REPL./",
        "CLOSING",
        "RE-",
    ],
    [
        "",
        "STOCK",
        "PURCHASES",
        "RETURN",
        "OTHERS",
        "STOCK",
        "SALES",
        "RETURN",
        "OTHERS",
        "STOCK",
        "ORDER",
    ],
    ["HIMALAYA(ZEAL)"],
    [
        "CONFIDO                      1*60",
        36.0,
        100.0,
        0.0,
        0.0,
        136.0,
        19.0,
        0.0,
        0.0,
        117.0,
        "   -     9",
    ],
    [
        "LIV 52 200ML SYP             200ML",
        "  -5",
        70.0,
        0.0,
        0.0,
        65.0,
        30.0,
        0.0,
        0.0,
        35.0,
        "  10     0",
    ],
    [
        "RUMALAYA FORTE TAB           1*30",
        16.0,
        100.0,
        2.0,
        0.0,
        118.0,
        2.0,
        0.0,
        0.0,
        116.0,
        "   -     0",
    ],
    [" Quantity", 518.0, 270.0, 2.0, 0.0, 790.0, 298.0, 0.0, 0.0, 492.0, "216           198"],
    [
        " Value in Rs.",
        60400.0,
        41019.0,
        299.0,
        0.0,
        101719.0,
        39782.0,
        0.0,
        0.0,
        64996.0,
        "28703            24K",
    ],
    ["Import Purchase ONLINE", "No Manual Entry", "MARG ERP Rs.5550"],
]

FIXTURE = Path(
    r"C:\Users\adity\Downloads\ZL_2026_August"
    r"\0000734542_2026_08_ZL_34_708_07092026021422.XLS"
)


class TestMargSalePurchaseAnalysisDetect(unittest.TestCase):
    def test_older_marg_headers_stay_on_their_parsers(self):
        self.assertIsNone(_find_marg_sale_purchase_analysis_header(PLAIN))
        self.assertIsNone(_find_marg_sale_purchase_analysis_header(MEXP_ROWS))
        self.assertIsNone(_parse_marg_sale_purchase_analysis_xls(
            PLAIN, "plain.xls", ".xls", "Sheet1"
        ))
        parsed = _parse_marg_opening_receipt_issue(PLAIN, "plain.xls", ".xls", "Sheet1")
        self.assertEqual(
            parsed["totals"]["extra"]["extraction_method"],
            "marg_opening_receipt_issue",
        )
        mexp = _parse_marg_closing_mexp_xls(MEXP_ROWS, "mexp.xls", ".xls", "Sheet1")
        self.assertEqual(mexp["line_items"][0]["closing_qty"], 55.0)

    def test_two_row_header_maps_sale_purchases_and_purchase_sales(self):
        parsed = _parse_marg_sale_purchase_analysis_xls(
            ANALYSIS, "analysis.xls", ".xls", "Sheet1"
        )
        self.assertEqual(
            parsed["totals"]["extra"]["extraction_method"],
            "marg_sale_purchase_analysis",
        )
        self.assertEqual(parsed["stockist_name"], "MAA BIMLA PHARMA")
        self.assertIn("KUNWAR SINGH", parsed["stockist_address"])
        self.assertEqual(parsed["company_name"], "HIMALAYA(ZEAL)")
        self.assertEqual(parsed["period_from"], "2026-08-01")
        self.assertEqual(parsed["period_to"], "2026-08-31")
        names = [item["product_name"] for item in parsed["line_items"]]
        self.assertEqual(
            names,
            ["CONFIDO", "LIV 52 200ML SYP", "RUMALAYA FORTE TAB"],
        )
        confido = parsed["line_items"][0]
        self.assertEqual(confido["packing"], "1*60")
        self.assertEqual(confido["opening_qty"], 36.0)
        self.assertEqual(confido["receipts_qty"], 100.0)
        self.assertEqual(confido["sales_qty"], 19.0)
        self.assertEqual(confido["closing_qty"], 117.0)
        self.assertEqual(confido["reorder_qty"], 0.0)
        self.assertEqual(confido["extra"]["order_qty"], 9.0)
        liv = parsed["line_items"][1]
        self.assertEqual(liv["opening_qty"], -5.0)
        self.assertEqual(liv["receipts_qty"], 70.0)
        self.assertEqual(liv["sales_qty"], 30.0)
        self.assertEqual(liv["closing_qty"], 35.0)
        self.assertEqual(liv["reorder_qty"], 10.0)
        rumalaya = parsed["line_items"][2]
        self.assertEqual(rumalaya["sales_return_qty"], 2.0)
        self.assertEqual(rumalaya["receipts_qty"], 100.0)
        self.assertEqual(rumalaya["closing_qty"], 116.0)
        self.assertEqual(parsed["totals"]["sales_value"], 39782.0)
        self.assertEqual(parsed["totals"]["closing_value"], 64996.0)
        self.assertEqual(parsed["totals"]["extra"]["opening_value"], 60400.0)
        self.assertEqual(parsed["totals"]["extra"]["purchase_value"], 41019.0)


@unittest.skipUnless(FIXTURE.is_file(), "missing Marg sale/purchase analysis workbook")
class TestMargSalePurchaseAnalysisFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            item["product_name"]: item for item in cls.result["line_items"]
        }

    def test_extracts_products_without_total_rows(self):
        self.assertEqual(
            self.extra.get("extraction_method"), "marg_sale_purchase_analysis"
        )
        self.assertEqual(self.result["stockist_name"], "MAA BIMLA PHARMA")
        self.assertEqual(self.result["company_name"], "HIMALAYA(ZEAL)")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertEqual(len(self.result["line_items"]), 11)
        names = " | ".join(self.by_name).upper()
        self.assertNotIn("QUANTITY", names)
        self.assertNotIn("VALUE", names)
        self.assertNotIn("HIMALAYA", names)
        self.assertNotIn("MARG ERP", names)
        self.assertEqual(self.extra.get("stock_identity_fail_count"), 0)
        self.assertEqual(self.result["totals"]["sales_value"], 39782.0)
        self.assertEqual(self.result["totals"]["closing_value"], 64996.0)
        self.assertEqual(self.extra.get("opening_value"), 60400.0)

    def test_columns_keep_sale_return_and_negative_opening(self):
        confido = self.by_name["CONFIDO"]
        self.assertEqual(confido["packing"], "1*60")
        self.assertEqual(confido["opening_qty"], 36.0)
        self.assertEqual(confido["receipts_qty"], 100.0)
        self.assertEqual(confido["sales_qty"], 19.0)
        self.assertEqual(confido["closing_qty"], 117.0)
        self.assertEqual(confido["extra"]["order_qty"], 9.0)

        liv = self.by_name["LIV 52 200ML SYP"]
        self.assertEqual(liv["packing"], "200ML")
        self.assertEqual(liv["opening_qty"], -5.0)
        self.assertEqual(liv["closing_qty"], 35.0)
        self.assertEqual(liv["reorder_qty"], 10.0)

        sugarfree = self.by_name["LIV 52 SYP SUGARFREE"]
        self.assertEqual(sugarfree["opening_qty"], -4.0)
        self.assertEqual(sugarfree["closing_qty"], -4.0)

        rumalaya = self.by_name["RUMALAYA FORTE TAB"]
        self.assertEqual(rumalaya["sales_return_qty"], 2.0)
        self.assertEqual(rumalaya["receipts_qty"], 100.0)
        self.assertEqual(rumalaya["sales_qty"], 2.0)
        self.assertEqual(rumalaya["closing_qty"], 116.0)
        self.assertEqual(rumalaya["subtotal_qty"], 118.0)


if __name__ == "__main__":
    unittest.main()
