"""DOCX OP./PUR./SR/BAL/BVAL stock tables via header resolver."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import extract_sales_statement
from services.stock_header_resolver import resolve_columns

FIXTURE = Path("0000701432_2026_08_ZL_37_390_05092026065901.docx")


def _cells(*headers: str):
    return [
        {"text": h, "col_index": i, "x_center": float(i)} for i, h in enumerate(headers)
    ]


class TestSrHeaderDisambiguation(unittest.TestCase):
    def test_bare_sr_between_pur_and_bal_is_sales(self):
        cols = resolve_columns(
            _cells(
                "PCODE",
                "PRODUCT NAME",
                "PACK",
                "OP.",
                "PUR.",
                "SR",
                "JAN",
                "FEB",
                "PR",
                "BAL",
                "BVAL",
            )
        )["columns"]
        by = {c["header_text"]: c for c in cols}
        self.assertEqual(by["SR"]["canonical"], "sales_qty")
        self.assertEqual(by["SR"]["reason"], "sr_as_sales_no_sale_col")
        self.assertEqual(by["OP."]["canonical"], "opening_qty")
        self.assertEqual(by["BAL"]["canonical"], "closing_qty")
        self.assertEqual(by["BVAL"]["canonical"], "closing_value")

    def test_sr_stays_sale_return_when_sale_present(self):
        cols = resolve_columns(
            _cells(
                "PCODE",
                "PRODUCT NAME",
                "PACK",
                "OP.",
                "PUR.",
                "SR",
                "JUL",
                "AUG",
                "SALE",
                "PR",
                "BAL",
                "BVAL",
            )
        )["columns"]
        by = {c["header_text"]: c for c in cols}
        self.assertEqual(by["SR"]["canonical"], "sales_return_qty")
        self.assertEqual(by["SALE"]["canonical"], "sales_qty")

    def test_sr_dot_still_serial(self):
        cols = resolve_columns(
            _cells("Sr.", "Product Name", "Pack", "Op.Stk", "Purch", "Sale", "Bal")
        )["columns"]
        self.assertEqual(cols[0]["canonical"], "ignore")
        self.assertEqual(cols[0]["reason"], "serial_number_header")


class TestJyothiOpPurSrBalDocx(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FIXTURE.is_file():
            raise unittest.SkipTest(f"missing {FIXTURE.name}")
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)

    def test_meta_from_word_header_not_filename(self):
        r = self.result
        extra = (r.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "docx_header_resolved_table")
        self.assertEqual(r.get("stockist_name"), "JYOTHI MEDICAL HALL")
        self.assertIn("CHILKALGUDA", r.get("stockist_address") or "")
        self.assertIn("HYDERABAD", r.get("stockist_address") or "")
        self.assertEqual(r.get("company_name"), "HIMALAYA-ZEAL")
        # Printed Word header period is March — not filename 2026_08 / 05092026.
        self.assertEqual(r.get("period_from"), "2026-03-01")
        self.assertEqual(r.get("period_to"), "2026-03-30")
        self.assertEqual(extra.get("statement_month"), "2026-03")
        self.assertNotEqual(r.get("period_from"), "2026-08-01")

    def test_quantities_not_zero_and_pack_not_opening(self):
        r = self.result
        items = r.get("line_items") or []
        self.assertGreaterEqual(len(items), 30)
        abana = next(i for i in items if i.get("product_name") == "ABANA TABLETS")
        self.assertEqual(abana.get("packing"), "60S")
        self.assertEqual(abana.get("opening_qty"), 15.0)
        self.assertEqual(abana.get("receipts_qty"), 0.0)
        self.assertEqual(abana.get("sales_qty"), 15.0)
        self.assertEqual(abana.get("closing_qty"), 0.0)

        diabecon = next(
            i for i in items if i.get("product_name") == "DIABECON DS TABLETS"
        )
        self.assertEqual(diabecon.get("opening_qty"), 50.0)
        self.assertEqual(diabecon.get("sales_qty"), 20.0)
        self.assertEqual(diabecon.get("closing_qty"), 30.0)

        nonzero = sum(
            1
            for i in items
            if any(
                float(i.get(k) or 0) != 0
                for k in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
            )
        )
        self.assertGreaterEqual(nonzero, 20)

    def test_footer_totals_kept(self):
        r = self.result
        extra = (r.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("opening_value"), 86640.0)
        self.assertEqual(r.get("totals", {}).get("closing_value"), 3560.0)
        # Sale(Aug) preferred when JUL/AUG headers are present.
        self.assertEqual(r.get("totals", {}).get("sales_value"), 15205.18)


if __name__ == "__main__":
    unittest.main()
