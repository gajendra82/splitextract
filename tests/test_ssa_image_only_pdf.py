"""Image-only STOCK & SALES ANALYSIS PDF — OCR-tolerant SSA qty/value path."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_ssa_opening_receipt_issue_dump_text,
    _is_ssa_opening_receipt_issue_dump_text_fuzzy,
    _ssa_dump_opening_values_sparse,
    _ssa_dump_repair_opening_values,
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

    def test_repair_fills_opening_value_from_closing_when_no_movement(self):
        """Adarsh-style: vision keeps opening qty but drops OPENING VALUE."""
        items = []
        for name, oq, cv in (
            ("BRESOL NS NASAL SOLUTION", 20.0, 1064.0),
            ("EVECARE FORTE 200 ML", 32.0, 5424.02),
            ("HIMCOLIN GEL 30 GM", 5.0, 1032.9),
            ("PILEX FORTE TAB", 8.0, 812.6),
            ("AACTARIL SOAP 75G", 0.0, 0.0),
        ):
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = oq
            item["opening_value"] = 0.0
            item["receipts_qty"] = 0.0
            item["sales_qty"] = 0.0
            item["closing_qty"] = oq
            item["closing_value"] = cv
            item["extra"] = {"opening_value": 0.0}
            items.append(item)
        # One row with movement — recover via money identity.
        moved = empty_line_item()
        moved["product_name"] = "BRESOL SYRUP 200 ML"
        moved["opening_qty"] = 13.0
        moved["opening_value"] = 0.0
        moved["receipts_qty"] = 0.0
        moved["sales_qty"] = 2.0
        moved["sales_value"] = 297.85
        moved["closing_qty"] = 11.0
        moved["closing_value"] = 1638.19
        moved["extra"] = {"opening_value": 0.0}
        items.append(moved)

        self.assertTrue(_ssa_dump_opening_values_sparse(items))
        repaired = _ssa_dump_repair_opening_values(items)
        by_name = {i["product_name"]: i for i in repaired}
        self.assertEqual(by_name["BRESOL NS NASAL SOLUTION"]["opening_value"], 1064.0)
        self.assertEqual(by_name["EVECARE FORTE 200 ML"]["opening_value"], 5424.02)
        self.assertEqual(by_name["HIMCOLIN GEL 30 GM"]["opening_value"], 1032.9)
        self.assertEqual(by_name["AACTARIL SOAP 75G"]["opening_value"], 0.0)
        self.assertAlmostEqual(
            by_name["BRESOL SYRUP 200 ML"]["opening_value"], 1936.04, places=2
        )
        self.assertFalse(_ssa_dump_opening_values_sparse(repaired))

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
        # OPENING VALUE must reflect for stockist rows with opening qty.
        valued = [
            i
            for i in items
            if abs(float(i.get("opening_qty") or 0)) > 0
            and abs(float(i.get("opening_value") or 0)) > 0
        ]
        self.assertGreaterEqual(len(valued), 10, "opening_value missing for Adarsh")
        bresol = next(
            (i for i in items if "BRESOL NS" in str(i.get("product_name") or "").upper()),
            None,
        )
        if bresol is not None and abs(float(bresol.get("opening_qty") or 0)) > 0:
            self.assertGreater(abs(float(bresol.get("opening_value") or 0)), 0)
        totals_extra = ((result.get("totals") or {}).get("extra") or {})
        self.assertGreater(abs(float(totals_extra.get("opening_value") or 0)), 0)


if __name__ == "__main__":
    unittest.main()
