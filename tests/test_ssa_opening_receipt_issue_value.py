"""STOCK & SALES ANALYSIS Opening/Receipt/Issue/Closing qty+value + DUMP."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_order_form_stock_statement_text,
    _is_saleable_stock_report_text,
    _is_ssa_opening_receipt_issue_value_text,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000736167_2026_08_ZA_24_8137_03092026173805.pdf"
)

HEADER = (
    "PRAKASH MEDICAL STORE\n"
    "STOCK & SALES ANALYSIS 01/08/2026 - 31/08/2026\n"
    "ITEM DESCRIPTION  OPENING  RECEIPT  ISSUE  CLOSING  DUMP\n"
    "QTY. VALUE QTY. VALUE QTY. VALUE QTY. VALUE QTY.\n"
)

BUSY = (
    "STOCK & SALES ANALYSIS\n"
    "Item Description Opening Purchases Return Others Total Sales Closing Rate\n"
)

SALEABLE = (
    "Saleable Stock Report\n"
    "Particular | | Opn | Rec | Issue | Bal\n"
)


class TestSsaOriDetection(unittest.TestCase):
    def test_detects_receipt_issue_dump_not_other_layouts(self):
        self.assertTrue(_is_ssa_opening_receipt_issue_value_text(HEADER))
        self.assertFalse(_is_ssa_opening_receipt_issue_value_text(BUSY))
        self.assertFalse(_is_ssa_opening_receipt_issue_value_text(SALEABLE))
        self.assertFalse(_is_ssa_opening_receipt_issue_value_text(""))
        self.assertFalse(_is_saleable_stock_report_text(HEADER))
        self.assertFalse(_is_order_form_stock_statement_text(HEADER))

    def test_stockist_is_shop_name_not_phone_email_line(self):
        from services.sales_statement_extractor import (
            _parse_ssa_opening_receipt_issue_value,
        )

        text = (
            "M\\s INDIAN DRUGS & SURGICALS\n"
            "SARUPATHAR TOWN, PIN- 785601 DIST- GOLAGHAT (ASSAM)\n"
            "Phone : 7002629113 E-Mail : dasmedical989@gmail.com\n"
            "GSTIN : 18BADPD9210M1ZO\n"
            "STOCK & SALES ANALYSIS  (HIMALAYA ZANDRA) 01-08-2026 - 03-09-2026\n"
            "ITEM DESCRIPTION  OPENING  RECEIPT  ISSUE  CLOSING  DUMP\n"
            "QTY. VALUE QTY. VALUE QTY. VALUE QTY. VALUE QTY.\n"
            "BONNISAN 30ML DROP 30ML 34 1968.60 - 0.00 - 0.00 34 1968.60 34\n"
            "BONNISON 100ML SYP 100ML 92 4991.56 112 6731.20 122 7666.77 82 4449.00 82\n"
            "TOTAL 1319 166385.92 990 141336.34 1010 156029.52 1299 158923.33 0\n"
        )
        parsed = _parse_ssa_opening_receipt_issue_value(text, "das.pdf", "pdf")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["stockist_name"], "M/S INDIAN DRUGS & SURGICALS")
        self.assertNotIn("Phone", parsed["stockist_name"])
        self.assertIn("SARUPATHAR", str(parsed.get("stockist_address") or ""))

    def test_stockist_without_medical_pharma_keyword(self):
        from services.sales_statement_extractor import (
            _parse_ssa_opening_receipt_issue_value,
        )

        text = (
            "R P AND SON'S.\n"
            "KHIRIYA GHAT\n"
            "BETTIAH\n"
            "PIN CODE 845438\n"
            "STOCK & SALES ANALYSIS  (HIMALAYA WELLNESS COMPANY (ZEAL)) "
            "01-08-2026 - 31-08-2026\n"
            "ITEM DESCRIPTION  OPENING  RECEIPT  ISSUE  CLOSING  DUMP\n"
            "QTY. VALUE QTY. VALUE QTY. VALUE QTY. VALUE QTY.\n"
            "AACTARIL SOAP 75 GM 75 GM 51 3743.69 0 0.00 20 1673.54 31 2275.57 31\n"
            "TOTAL 100 1000.00 0 0.00 20 200.00 80 800.00 80\n"
        )
        parsed = _parse_ssa_opening_receipt_issue_value(text, "rp.pdf", "pdf")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["stockist_name"], "R P AND SON'S.")
        self.assertEqual(parsed["period_from"], "2026-08-01")
        self.assertEqual(parsed["period_to"], "2026-08-31")


@unittest.skipUnless(FIXTURE.is_file(), "missing Prakash SSA fixture")
class TestSsaOriFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.by_name = {
            str(item.get("product_name") or "").upper(): item
            for item in cls.result["line_items"]
        }
        cls.names = [
            str(item.get("product_name") or "") for item in cls.result["line_items"]
        ]

    def test_uses_ssa_ori_parser(self):
        self.assertEqual(
            self.extra.get("extraction_method"), "ssa_opening_receipt_issue_value"
        )
        self.assertEqual(self.result["stockist_name"], "PRAKASH MEDICAL STORE")
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertGreaterEqual(len(self.result["line_items"]), 28)
        self.assertNotIn("Invoices", self.result)

    def test_totals_and_purchase_detail_are_not_products(self):
        joined = " | ".join(self.names).upper()
        self.assertNotIn("TOTAL", joined)
        self.assertNotIn("PURCHASE DETAIL", joined)
        self.assertNotIn("HIMALAYA WELLNESS COMPNY", joined)
        self.assertNotIn("HIMALAYS WELLNESS COMPANY", joined)
        self.assertFalse(any(name.upper() in {"HIMALYA", "HIMALAYA"} for name in self.names))

    def test_sample_rows_and_dash_zero(self):
        liv = next(
            item
            for item in self.result["line_items"]
            if str(item.get("product_name") or "").upper().startswith("LIV 52 DS SYP")
        )
        self.assertEqual(liv["opening_qty"], 140.0)
        self.assertEqual(liv["receipts_qty"], 0.0)
        self.assertEqual(liv["sales_qty"], 70.0)
        self.assertEqual(liv["sales_value"], 16160.46)
        self.assertEqual(liv["closing_qty"], 70.0)
        self.assertEqual(liv["closing_value"], 16901.85)
        self.assertEqual(liv["extra"]["opening_value"], 33803.70)

        lukol = self.by_name["LUKOL TAB"]
        self.assertEqual(lukol["opening_qty"], 0.0)
        self.assertEqual(lukol["receipts_qty"], 50.0)
        self.assertEqual(lukol["extra"]["receipts_value"], 8544.00)
        self.assertEqual(lukol["sales_qty"], 0.0)
        self.assertEqual(lukol["closing_qty"], 50.0)

        confido = next(
            item
            for item in self.result["line_items"]
            if "CONFIDO" in str(item.get("product_name") or "").upper()
        )
        self.assertEqual(confido["packing"], "1CC")
        self.assertEqual(confido["opening_qty"], 100.0)
        self.assertEqual(confido["receipts_qty"], 100.0)
        self.assertEqual(confido["closing_qty"], 200.0)

    def test_printed_grand_totals(self):
        self.assertEqual(self.result["totals"].get("sales_value"), 94830.56)
        self.assertEqual(self.result["totals"].get("closing_value"), 430534.70)
        self.assertEqual(self.extra.get("opening_value"), 263435.48)
        self.assertEqual(self.extra.get("receipts_value"), 253268.57)
        self.assertNotEqual(self.result["totals"].get("sales_value"), 385495.67)


if __name__ == "__main__":
    unittest.main()
