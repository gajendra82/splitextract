"""Image-only STOCK & SALES ANALYSIS PDF — OCR-tolerant SSA qty/value path."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_ssa_opening_receipt_issue_dump_text,
    _is_ssa_opening_receipt_issue_dump_text_fuzzy,
    _ssa_qty_value_result_usable,
    empty_line_item,
    empty_result,
    extract_sales_statement,
)


class TestSsaDumpOcrTolerant(unittest.TestCase):
    def test_fuzzy_accepts_ocr_typos(self):
        preview = (
            "ADARSH PHARMA\n"
            "STOCK & SALES ANALYSIS 01-08-2026 ~ 31-08-2026\n"
            "ITEM DESCRIPTION OPEXING RECETeT 1Shur CLOSING oe\n"
            "ary VALUE ary value ary Vauut ary VALUE\n"
        )
        self.assertFalse(_is_ssa_opening_receipt_issue_dump_text(preview))
        self.assertTrue(_is_ssa_opening_receipt_issue_dump_text_fuzzy(preview))

    def test_fuzzy_rejects_unrelated(self):
        self.assertFalse(
            _is_ssa_opening_receipt_issue_dump_text_fuzzy(
                "Sales & Stock Statement Op.Bal Receipt Issue Closing\n"
            )
        )

    def test_usable_accepts_opening_qty_without_opening_value(self):
        result = empty_result("x.pdf", "pdf")
        for qty in (20, 13, 25, 33, 27):
            item = empty_line_item()
            item["product_name"] = f"P{qty}"
            item["opening_qty"] = float(qty)
            item["closing_value"] = 100.0
            result["line_items"].append(item)
        self.assertTrue(_ssa_qty_value_result_usable(result))

    def test_usable_accepts_short_continuation_page(self):
        """Last SSA page may have only a few products with receipt/closing values."""
        result = empty_result("p3.pdf", "pdf")
        forte = empty_line_item()
        forte["product_name"] = "RUMALAYA FORTE TABLETS"
        forte["opening_qty"] = 0.0
        forte["receipts_qty"] = 100.0
        forte["closing_qty"] = 100.0
        forte["closing_value"] = 15429.34
        forte["extra"] = {"receipts_value": 14694.6}
        result["line_items"].append(forte)
        tabs = empty_line_item()
        tabs["product_name"] = "RUMALAYA TABLETS 60 S"
        tabs["receipts_qty"] = 50.0
        tabs["closing_qty"] = 50.0
        tabs["closing_value"] = 6950.49
        result["line_items"].append(tabs)
        zero = empty_line_item()
        zero["product_name"] = "TAGARA TAB"
        result["line_items"].append(zero)
        self.assertTrue(_ssa_qty_value_result_usable(result))

    def test_live_adarsh_pdf(self):
        path = Path(
            "/var/www/html/splitextract/"
            "0000734695_2026_08_ZL_04_742_04092026132445.pdf"
        )
        if not path.exists():
            self.skipTest("fixture PDF not present")
        result = extract_sales_statement(path.read_bytes(), path.name)
        method = ((result.get("totals") or {}).get("extra") or {}).get(
            "extraction_method"
        )
        self.assertIn(method, {"ssa_qty_value_vision_pdf", "ssa_qty_value_vision"})
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 40)
        names = [str(i.get("product_name") or "") for i in items]
        self.assertFalse(any("STOCK & SALES" in n.upper() for n in names))
        # Page 3 continuation products must be present.
        self.assertTrue(
            any("RUMALAYA FORTE" in n.upper() for n in names),
            names[-10:],
        )
        self.assertTrue(
            any(
                "RUMALAYA TABLET" in n.upper() or "RUMALAYA TABLETS" in n.upper()
                for n in names
            ),
            names[-10:],
        )
        # At least some real movement rows
        moved = [
            i
            for i in items
            if (i.get("opening_qty") or 0) > 0 or (i.get("sales_qty") or 0) > 0
            or (i.get("receipts_qty") or 0) > 0
        ]
        self.assertGreaterEqual(len(moved), 5)
        if result.get("stockist_name"):
            self.assertIn("ADARSH", result["stockist_name"].upper())


if __name__ == "__main__":
    unittest.main()
