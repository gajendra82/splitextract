"""Item Description / Opening Balance / Sales Qty / Purchase Qty landscape PDF."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_daxinsoft_stock_sales_text,
    _is_opening_sales_purchase_statement_text,
    _is_ssa_mexp_stock_sales_text,
    _is_ssa_opening_receipt_issue_value_text,
    _is_ved_stock_sales_statement_text,
    extract_sales_statement,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000735945_2026_08_ZL_34_356_04092026145713.PDF"
)
ARVIND = (
    Path(__file__).resolve().parents[1]
    / "0000736096_2026_08_ZL_24_237_06092026062517.PDF"
)
PRAKASH = (
    Path(__file__).resolve().parents[1]
    / "0000736167_2026_08_ZA_24_8137_03092026173805.pdf"
)

HEADER = (
    "AGRAWAL DRUGS\n"
    "THE HIMALAYA DRUGS COMPANY\n"
    "Stock and Sales Statement from 01-08-2026 to 31-08-2026\n"
    "Item Description  Opening  Sales  Sale  Sales  Sales  Purchase  "
    "Purchase  Purchase  Purchase  Other  Closing  Closing  Expiry  Expiry\n"
    "Balance  Qty.  Free  Amount  Return  Qty.  Free  Amount  Return  "
    "Balance  Amount  In  Out\n"
)

VED = (
    "Stock and sales Statement from : 01/08/2026 to 31/08/2026\n"
    "Particulars  Pkg.  Open. Qty.  Purch. Qty.  Sales & DC  Close Stock\n"
)

SSA = (
    "STOCK & SALES ANALYSIS 01-08-2026 - 31-08-2026\n"
    "ITEM DESCRIPTION  OPENING  RECEIPT  ISSUE  CLOSING  DUMP\n"
    "QTY. VALUE QTY. VALUE QTY. VALUE QTY. VALUE QTY. M.EXP\n"
)


class TestOspDetection(unittest.TestCase):
    def test_detects_layout_from_headers_not_filename(self):
        self.assertTrue(_is_opening_sales_purchase_statement_text(HEADER))
        self.assertFalse(_is_opening_sales_purchase_statement_text(VED))
        self.assertFalse(_is_opening_sales_purchase_statement_text(SSA))
        self.assertFalse(_is_opening_sales_purchase_statement_text(""))
        self.assertFalse(_is_ved_stock_sales_statement_text(HEADER))
        self.assertFalse(_is_ssa_opening_receipt_issue_value_text(HEADER))
        self.assertFalse(_is_ssa_mexp_stock_sales_text(HEADER))
        self.assertFalse(_is_daxinsoft_stock_sales_text(HEADER))


@unittest.skipUnless(FIXTURE.is_file(), "missing Agrawal opening/sales/purchase fixture")
class TestOspFixture(unittest.TestCase):
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

    def test_uses_osp_parser(self):
        self.assertEqual(
            self.extra.get("extraction_method"), "item_desc_opening_sales_purchase"
        )
        self.assertEqual(self.result["stockist_name"], "AGRAWAL DRUGS")
        self.assertIn("HIMALAYA", str(self.result.get("company_name") or "").upper())
        self.assertEqual(self.result["period_from"], "2026-08-01")
        self.assertEqual(self.result["period_to"], "2026-08-31")
        self.assertEqual(len(self.result["line_items"]), 43)
        self.assertNotIn("Invoices", self.result)

    def test_headers_totals_and_debit_notes_are_not_products(self):
        joined = " | ".join(self.names).upper()
        self.assertNotIn("TOTAL", joined)
        self.assertNotIn("PENDING", joined)
        self.assertNotIn("SUPPLIER", joined)
        self.assertFalse(any("WELLNESS COMPANY" in name.upper() for name in self.names))
        self.assertFalse(any("ITEM DESCRIPTION" in name.upper() for name in self.names))

    def test_sales_and_purchase_columns_are_not_swapped(self):
        soap = self.by_name["ACTARIL SOAP"]
        self.assertEqual(soap["packing"], "75GM")
        self.assertEqual(soap["opening_qty"], 52.0)
        self.assertEqual(soap["sales_qty"], 0.0)
        self.assertEqual(soap["receipts_qty"], 0.0)
        self.assertEqual(soap["closing_qty"], 52.0)
        self.assertEqual(soap["closing_value"], 4039.36)

        bonnisan = self.by_name["BONNISAN LIQ 100ML"]
        self.assertEqual(bonnisan["packing"], "1X100ML")
        self.assertEqual(bonnisan["opening_qty"], 0.0)
        self.assertEqual(bonnisan["sales_qty"], 36.0)
        self.assertEqual(bonnisan["sales_value"], 2290.28)
        self.assertEqual(bonnisan["receipts_qty"], 56.0)
        self.assertEqual(bonnisan["extra"]["purchase_value"], 3365.60)
        self.assertEqual(bonnisan["closing_qty"], 20.0)
        self.assertEqual(bonnisan["closing_value"], 1202.00)

        hb = self.by_name["LIV-52 HB CAP"]
        self.assertEqual(hb["opening_qty"], 336.0)
        self.assertEqual(hb["sales_qty"], 672.0)
        self.assertEqual(hb["sales_value"], 101011.53)
        self.assertEqual(hb["receipts_qty"], 336.0)
        self.assertEqual(hb["extra"]["purchase_value"], 49526.80)
        self.assertEqual(hb["closing_qty"], 0.0)
        self.assertNotEqual(hb["sales_qty"], 336.0)

        tab = self.by_name["LIV-52 TAB"]
        self.assertEqual(tab["opening_qty"], 596.0)
        self.assertEqual(tab["sales_qty"], 1048.0)
        self.assertEqual(tab["receipts_qty"], 500.0)
        self.assertEqual(tab["closing_qty"], 48.0)
        self.assertEqual(tab["sales_value"], 154424.95)
        self.assertEqual(tab["closing_value"], 7132.80)

        syp = next(
            item
            for item in self.result["line_items"]
            if item.get("product_name") == "LIV-52 SYP 100ML"
        )
        self.assertEqual(syp["opening_qty"], 949.0)
        self.assertEqual(syp["sales_qty"], 761.0)
        self.assertEqual(syp["closing_qty"], 188.0)
        self.assertEqual(syp["extra"].get("expiry_in_qty"), 6.0)

    def test_printed_grand_totals_not_debit_note_total(self):
        self.assertEqual(self.result["totals"].get("sales_value"), 482404.05)
        self.assertEqual(self.result["totals"].get("closing_value"), 298013.90)
        self.assertNotEqual(self.result["totals"].get("sales_value"), 569111.00)
        self.assertNotEqual(self.result["totals"].get("closing_value"), 303642.00)


@unittest.skipUnless(ARVIND.is_file() and PRAKASH.is_file(), "missing SSA fixtures")
class TestExistingSsaNotStolen(unittest.TestCase):
    def test_arvind_and_prakash_keep_their_parsers(self):
        arvind = extract_sales_statement(ARVIND.read_bytes(), ARVIND.name)
        self.assertEqual(
            (arvind.get("totals") or {}).get("extra", {}).get("extraction_method"),
            "ssa_mexp_stock_sales",
        )
        prakash = extract_sales_statement(PRAKASH.read_bytes(), PRAKASH.name)
        self.assertEqual(
            (prakash.get("totals") or {}).get("extra", {}).get("extraction_method"),
            "ssa_opening_receipt_issue_value",
        )


if __name__ == "__main__":
    unittest.main()
