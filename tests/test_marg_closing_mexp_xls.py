"""Marg ITEM DESCRIPTION sheet whose closing column also holds M.EXP."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _parse_marg_closing_mexp_xls,
    _parse_marg_opening_receipt_issue,
    extract_sales_statement,
)


PLAIN = [
    ["SOME MEDICAL"],
    ["ITEM DESCRIPTION", "OPENING", "RECEIPT", "ISSUE", "CLOSING"],
    ["ABANA TAB              50'S", 10, 0, 2, 8],
]

MEXP_ROWS = [
    ["BHARAT MEDICALS"],
    ["STOCK & SALES ANALYSIS  (HIMALAYA ZEAL) 01-08-2026 - 31-08-2026"],
    ["ITEM DESCRIPTION", "OPENING", "RECEIPT", "ISSUE", "CLOSING M.EXP"],
    ["ABANA TAB              50'S.", 57, 0, 2, "    55  6/27"],
    ["HADJOD TABLET          60'S", 0, 60, 0, "    60  6/29"],
    [" TOTAL", 249106, 13238, 85122, 179799],
]

# Purchase-invoice footer under the product grid (KIMALAY / Suraj Shree style).
MEXP_WITH_INVOICE_FOOTER = [
    ["SURAJ SHREE MEDICOSE"],
    ["STOCK & SALES ANALYSIS  (HIMALAYA ZANDRA DIVSION) 01-09-2026 - 30-09-2026"],
    ["ITEM DESCRIPTION", "OPENING", "RECEIPT", "ISSUE", "CLOSING M.EXP"],
    ["BONNISAN DROP 30ML     1X30ML", 95.0, "     -", 20.0, "    75 11/23"],
    ["BRESOL SYP 200ML       1X200M", 26.0, "    -1", 3.0, "    22  2/23"],
    ["THE HIMALAYA DRUG COM INDORE", 5126108555.0, "15-09-2026", "", "15-09-2026    38290.00"],
    ["THE HIMALAYA DRUG COM INDORE", 5126108656.0, "17-09-2026", "", "17-09-2026    33994.00"],
    ["", "", "", "", "TOTAL :   181212.00"],
]

FIXTURE = Path(
    r"C:\Users\adity\Downloads\ZL_2026_August"
    r"\0000735982_2026_08_ZL_20_296_03092026131548.xls"
)


class TestMargClosingMexpDetection(unittest.TestCase):
    def test_plain_closing_sheet_stays_on_the_old_parser(self):
        self.assertIsNone(
            _parse_marg_closing_mexp_xls(PLAIN, "plain.xls", ".xls", "Sheet1")
        )
        parsed = _parse_marg_opening_receipt_issue(PLAIN, "plain.xls", ".xls", "Sheet1")
        self.assertIsNotNone(parsed)
        self.assertEqual(
            parsed["totals"]["extra"]["extraction_method"],
            "marg_opening_receipt_issue",
        )
        self.assertEqual(parsed["line_items"][0]["closing_qty"], 8.0)

    def test_expiry_is_not_glued_onto_closing_qty(self):
        parsed = _parse_marg_closing_mexp_xls(MEXP_ROWS, "mexp.xls", ".xls", "Sheet1")
        self.assertIsNotNone(parsed)
        by_name = {item["product_name"]: item for item in parsed["line_items"]}
        abana = by_name["ABANA TAB"]
        self.assertEqual(abana["packing"], "50'S.")
        self.assertEqual(abana["opening_qty"], 57.0)
        self.assertEqual(abana["sales_qty"], 2.0)
        self.assertEqual(abana["closing_qty"], 55.0)
        self.assertNotEqual(abana["closing_qty"], 556.0)
        self.assertEqual(abana["extra"]["expiry"], "6/27")
        hadjod = by_name["HADJOD TABLET"]
        self.assertEqual(hadjod["receipts_qty"], 60.0)
        self.assertEqual(hadjod["closing_qty"], 60.0)
        self.assertEqual(hadjod["extra"]["expiry"], "6/29")
        self.assertNotIn("TOTAL", by_name)

    def test_invoice_footer_rows_are_not_products(self):
        parsed = _parse_marg_closing_mexp_xls(
            MEXP_WITH_INVOICE_FOOTER, "kimalay.xls", ".xls", "Sheet1"
        )
        self.assertIsNotNone(parsed)
        names = [item["product_name"] for item in parsed["line_items"]]
        self.assertEqual(len(names), 2)
        self.assertNotIn("THE HIMALAYA DRUG COM INDORE", names)
        by_name = {item["product_name"]: item for item in parsed["line_items"]}
        drop = by_name["BONNISAN DROP 30ML"]
        self.assertEqual(drop["opening_qty"], 95.0)
        self.assertEqual(drop["sales_qty"], 20.0)
        self.assertEqual(drop["closing_qty"], 75.0)
        self.assertEqual(drop["extra"]["expiry"], "11/23")
        bresol = by_name["BRESOL SYP 200ML"]
        self.assertEqual(bresol["receipts_qty"], -1.0)
        self.assertEqual(bresol["closing_qty"], 22.0)
        self.assertEqual(
            parsed["totals"]["extra"].get("marg_mexp_junk_rows_skipped"), 2
        )


@unittest.skipUnless(FIXTURE.is_file(), "missing Marg CLOSING M.EXP workbook")
class TestMargClosingMexpFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            item["product_name"]: item for item in cls.result["line_items"]
        }

    def test_extracts_products_and_header(self):
        self.assertEqual(self.extra.get("extraction_method"), "marg_closing_mexp_xls")
        self.assertEqual(self.result["stockist_name"], "BHARAT MEDICALS")
        self.assertEqual(self.result["company_name"], "HIMALAYA ZEAL")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertEqual(len(self.result["line_items"]), 18)
        names = " | ".join(self.by_name).upper()
        self.assertNotIn("TOTAL", names)
        self.assertNotIn("MARG ERP", names)

    def test_closing_qty_excludes_the_expiry(self):
        confido = self.by_name["CONFIDO TAB"]
        self.assertEqual(confido["packing"], "60'S.")
        self.assertEqual(confido["opening_qty"], 255.0)
        self.assertEqual(confido["sales_qty"], 32.0)
        self.assertEqual(confido["closing_qty"], 223.0)
        self.assertEqual(confido["extra"]["expiry"], "6/28")

        liv = self.by_name["LIV-52 SYRUP"]
        self.assertEqual(liv["packing"], "100ML")
        self.assertEqual(liv["opening_qty"], 158.0)
        self.assertEqual(liv["sales_qty"], 124.0)
        self.assertEqual(liv["closing_qty"], 34.0)
        self.assertEqual(liv["extra"]["expiry"], "8/28")

        tentex = self.by_name["TENTEX FORTE TAB"]
        self.assertEqual(tentex["opening_qty"], 354.0)
        self.assertEqual(tentex["sales_qty"], 62.0)
        self.assertEqual(tentex["closing_qty"], 292.0)
        self.assertEqual(tentex["extra"]["expiry"], "9/28")
        self.assertEqual(self.extra.get("stock_identity_fail_count"), 0)


if __name__ == "__main__":
    unittest.main()
