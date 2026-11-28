"""HIMALAYA OCT 26 Company [Summary] XLSX — native openpyxl OP/IN/OUT/CL extract.

Regression for Sales && Stock Statement Company [Summary] workbooks:
PRODUCT / COMPANY | PACKING | OP QTY/AMT | IN QTY/AMT | OP+IN | OUT | CL | PROFIT.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from openpyxl import load_workbook

from services.sales_statement_extractor import (
    _parse_company_summary_op_in_out_xls,
    extract_sales_statement,
)
from services.stock_reconciliation import apply_stock_reconciliation

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "himalaya_oct26_company_summary.xlsx"
)


def _sheet_rows(path: Path):
    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        return [list(row) for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


@unittest.skipUnless(FIXTURE.exists(), "himalaya_oct26_company_summary.xlsx missing")
class TestHimalayaOct26CompanySummaryXlsx(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = _sheet_rows(FIXTURE)
        cls.raw = FIXTURE.read_bytes()

    def test_native_parser_extracts_26_rows_by_headers(self):
        result = _parse_company_summary_op_in_out_xls(
            self.rows, FIXTURE.name, ".xlsx", "Sheet1"
        )
        self.assertIsNotNone(result)
        items = result.get("line_items") or []
        self.assertEqual(len(items), 26)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "company_summary_op_in_out_xls")
        self.assertFalse(extra.get("ocr_used"))
        self.assertFalse(extra.get("llm_used"))
        self.assertIn("opening_value", extra.get("source_columns_detected") or [])
        self.assertIn("purchase_value", extra.get("source_columns_detected") or [])
        self.assertIn("sales_value", extra.get("source_columns_detected") or [])
        self.assertIn("profit", extra.get("source_columns_detected") or [])

    def test_extract_sales_statement_26_rows_and_header_mapping(self):
        """Full XLSX call path must not yield semantic_xls items=0."""
        result = extract_sales_statement(self.raw, FIXTURE.name)
        items = result.get("line_items") or []
        self.assertEqual(len(items), 26)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "company_summary_op_in_out_xls")
        self.assertNotEqual(extra.get("total_row_source"), "sales_total_value_recovery")
        self.assertFalse(
            str(extra.get("sales_value_source") or "").startswith(
                "sales_total_value_recovery"
            )
        )
        # Header → field mapping (OP/IN/OUT/CL QTY+AMT, PROFIT).
        by_name = {
            str(it.get("product_name") or "").upper(): it for it in items
        }
        bonni = by_name["BONNISAN LIQUID"]
        self.assertEqual(float(bonni["opening_qty"]), 196.0)  # OP QTY
        self.assertEqual(float(bonni["opening_value"]), 11779.6)  # OP AMT
        self.assertEqual(float(bonni["receipts_qty"]), 1120.0)  # IN QTY
        self.assertEqual(float(bonni["extra"]["purchase_value"]), 67312.0)  # IN AMT
        self.assertEqual(float(bonni["extra"]["total_stock_qty"]), 1316.0)  # OP+IN QTY
        self.assertEqual(float(bonni["extra"]["total_value"]), 79091.6)  # OP+IN AMT
        self.assertEqual(float(bonni["sales_qty"]), 252.0)  # OUT QTY
        self.assertEqual(float(bonni["sales_value"]), 26195.68)  # OUT AMT
        self.assertEqual(float(bonni["closing_qty"]), 1064.0)  # CL QTY
        self.assertEqual(float(bonni["closing_value"]), 63946.4)  # CL AMT
        self.assertEqual(float(bonni["extra"]["profit"]), -59.74)  # PROFIT
        validation = extra.get("stock_validation") or {}
        self.assertTrue(validation.get("is_valid"))
        self.assertEqual(int(validation.get("row_reconciled_count") or 0), 26)

    def test_maps_amt_columns_and_preserves_zero_vs_missing(self):
        result = _parse_company_summary_op_in_out_xls(
            self.rows, FIXTURE.name, ".xlsx", "Sheet1"
        )
        by_name = {
            str(it.get("product_name") or "").upper(): it
            for it in (result.get("line_items") or [])
        }
        bonni = by_name["BONNISAN LIQUID"]
        self.assertEqual(float(bonni.get("opening_qty")), 196.0)
        self.assertEqual(float(bonni.get("opening_value")), 11779.6)
        self.assertEqual(float(bonni.get("receipts_qty")), 1120.0)
        self.assertEqual(float((bonni.get("extra") or {}).get("purchase_value")), 67312.0)
        self.assertEqual(float((bonni.get("extra") or {}).get("total_value")), 79091.6)
        self.assertEqual(float(bonni.get("sales_qty")), 252.0)
        self.assertEqual(float(bonni.get("sales_value")), 26195.68)
        self.assertEqual(float(bonni.get("closing_qty")), 1064.0)
        self.assertEqual(float(bonni.get("closing_value")), 63946.4)
        self.assertEqual(float((bonni.get("extra") or {}).get("profit")), -59.74)
        fs = (bonni.get("extra") or {}).get("field_source") or {}
        self.assertEqual(fs.get("opening_value"), "printed")
        self.assertEqual(fs.get("profit"), "printed")

        gasex = by_name["GASEX SY"]
        self.assertEqual(float(gasex.get("opening_qty")), 0.0)
        self.assertEqual(float(gasex.get("sales_qty")), 0.0)
        self.assertEqual(float(gasex.get("closing_qty")), 0.0)
        self.assertEqual(((gasex.get("extra") or {}).get("field_source") or {}).get("opening_qty"), "printed")

    def test_company_period_and_printed_grand_total(self):
        result = extract_sales_statement(self.raw, FIXTURE.name)
        self.assertEqual(result.get("company_name"), "HIMALAYA WELLNESS COMPANY")
        self.assertEqual(result.get("period_from"), "2026-10-01")
        self.assertEqual(result.get("period_to"), "2026-10-31")
        totals = result.get("totals") or {}
        extra = totals.get("extra") or {}
        self.assertEqual(extra.get("total_row_source"), "xls_grand_total")
        printed = extra.get("printed_total_row") or {}
        self.assertEqual(float(printed.get("opening_value")), 3533866.3)
        self.assertEqual(float(printed.get("purchase_value")), 1619173.99)
        self.assertEqual(float(printed.get("total_value")), 5153040.29)
        self.assertEqual(float(printed.get("sales_value")), 3924401.58)
        self.assertEqual(float(printed.get("closing_value")), 1663389.4)
        self.assertEqual(float(printed.get("profit")), -196486.49)
        self.assertEqual(float(totals.get("sales_value")), 3924401.58)
        self.assertEqual(float(totals.get("closing_value")), 1663389.4)
        self.assertEqual(float(extra.get("opening_qty")), 23484.0)
        self.assertEqual(float(extra.get("receipts_qty")), 12369.0)
        self.assertEqual(float(extra.get("total_stock_qty")), 35853.0)
        self.assertEqual(float(extra.get("sales_qty")), 23009.0)
        self.assertEqual(float(extra.get("closing_qty")), 12844.0)

    def test_values_match_openpyxl_cells_not_hardcoded_source(self):
        """Spot-check extracted rows against live workbook cells."""
        wb = load_workbook(FIXTURE, data_only=True)
        ws = wb[wb.sheetnames[0]]
        result = extract_sales_statement(self.raw, FIXTURE.name)
        items = result.get("line_items") or []
        # Row 5 in Excel is first product (BONNISAN LIQUID).
        self.assertEqual(items[0].get("product_name"), ws.cell(5, 2).value)
        self.assertEqual(float(items[0].get("opening_qty")), float(ws.cell(5, 4).value))
        self.assertEqual(float(items[0].get("opening_value")), float(ws.cell(5, 5).value))
        self.assertEqual(float(items[0].get("receipts_qty")), float(ws.cell(5, 6).value))
        self.assertEqual(
            float((items[0].get("extra") or {}).get("purchase_value")),
            float(ws.cell(5, 7).value),
        )
        self.assertEqual(float(items[0].get("sales_qty")), float(ws.cell(5, 10).value))
        self.assertEqual(float(items[0].get("sales_value")), float(ws.cell(5, 11).value))
        self.assertEqual(float(items[0].get("closing_qty")), float(ws.cell(5, 12).value))
        self.assertEqual(float(items[0].get("closing_value")), float(ws.cell(5, 13).value))
        # Grand total row 33.
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(float(extra.get("opening_value")), float(ws.cell(33, 5).value))
        self.assertEqual(float(extra.get("purchase_value")), float(ws.cell(33, 7).value))
        self.assertEqual(
            float((result.get("totals") or {}).get("sales_value")),
            float(ws.cell(33, 11).value),
        )
        self.assertEqual(
            float((result.get("totals") or {}).get("closing_value")),
            float(ws.cell(33, 13).value),
        )
        self.assertEqual(float(extra.get("profit")), float(ws.cell(33, 16).value))
        wb.close()

    def test_all_26_row_money_fields_match_excel_cells(self):
        """Every product OP/IN/OUT/CL AMT (+ OP+IN AMT, PROFIT) vs openpyxl."""
        wb = load_workbook(FIXTURE, data_only=True)
        ws = wb[wb.sheetnames[0]]
        result = extract_sales_statement(self.raw, FIXTURE.name)
        items = result.get("line_items") or []
        self.assertEqual(len(items), 26)
        for i, it in enumerate(items):
            excel_row = 5 + i
            extra = it.get("extra") or {}
            self.assertEqual(it.get("product_name"), ws.cell(excel_row, 2).value, i)
            self.assertAlmostEqual(
                float(it.get("opening_value")), float(ws.cell(excel_row, 5).value), places=2
            )
            self.assertAlmostEqual(
                float(extra.get("purchase_value")), float(ws.cell(excel_row, 7).value), places=2
            )
            self.assertAlmostEqual(
                float(extra.get("total_value")), float(ws.cell(excel_row, 9).value), places=2
            )
            self.assertAlmostEqual(
                float(it.get("sales_value")), float(ws.cell(excel_row, 11).value), places=2
            )
            self.assertAlmostEqual(
                float(it.get("closing_value")), float(ws.cell(excel_row, 13).value), places=2
            )
            self.assertAlmostEqual(
                float(extra.get("profit")), float(ws.cell(excel_row, 16).value), places=2
            )
        wb.close()

    def test_financial_validation_separate_from_qty_and_identity(self):
        """Qty stock_validation stays valid; money uses footer vs lines, not OP+IN−OUT."""
        result = extract_sales_statement(self.raw, FIXTURE.name)
        extra = (result.get("totals") or {}).get("extra") or {}
        qty = extra.get("stock_validation") or {}
        fin = extra.get("financial_validation") or {}
        self.assertTrue(qty.get("is_valid"))
        self.assertEqual(int(qty.get("row_reconciled_count") or 0), 26)
        self.assertTrue(fin.get("is_valid"))
        self.assertFalse(fin.get("value_identity_enforced"))
        self.assertFalse(fin.get("value_identity_applicable"))
        self.assertFalse(fin.get("corrected_values"))
        self.assertEqual(fin.get("mismatch_count"), 0)
        self.assertEqual(fin.get("mismatches"), [])
        # This sheet has many rows where printed CL AMT ≠ OP+IN−OUT AMT.
        self.assertGreaterEqual(int(fin.get("row_value_identity_break_count") or 0), 1)

        printed = fin.get("printed_totals") or {}
        lines = fin.get("line_sums") or {}
        for field in (
            "opening_value",
            "purchase_value",
            "total_value",
            "sales_value",
            "closing_value",
            "profit",
        ):
            self.assertAlmostEqual(float(printed[field]), float(lines[field]), places=2)

        # Row diagnostic must not fail qty-valid rows via money identity.
        bonni = (result.get("line_items") or [])[0]
        recon = (bonni.get("extra") or {}).get("reconciliation") or {}
        self.assertTrue(recon.get("valid"))
        value = recon.get("value") or {}
        self.assertTrue(value.get("valid"))
        self.assertFalse(value.get("identity_applicable"))
        self.assertEqual(value.get("basis"), "printed_excel_values")

    def test_financial_mismatch_flagged_without_correcting_values(self):
        """Footer≠line sum is flagged; extracted printed money is left untouched."""
        result = extract_sales_statement(self.raw, FIXTURE.name)
        items = result.get("line_items") or []
        before = float(items[0].get("closing_value"))
        totals = result.setdefault("totals", {})
        extra = totals.setdefault("extra", {})
        # Corrupt only the footer sales figure — do not touch line items.
        printed = dict(extra.get("printed_total_row") or {})
        printed["sales_value"] = round(float(printed["sales_value"]) + 1000.0, 2)
        extra["printed_total_row"] = printed
        totals["sales_value"] = printed["sales_value"]
        apply_stock_reconciliation(result, request_id="fin-mismatch")
        fin = ((result.get("totals") or {}).get("extra") or {}).get(
            "financial_validation"
        ) or {}
        self.assertFalse(fin.get("is_valid"))
        self.assertGreaterEqual(int(fin.get("mismatch_count") or 0), 1)
        self.assertFalse(fin.get("corrected_values"))
        fields = {m.get("field") for m in (fin.get("mismatches") or [])}
        self.assertIn("sales_value", fields)
        self.assertAlmostEqual(float(items[0].get("closing_value")), before, places=2)
        # Qty validation remains independent.
        self.assertTrue(
            (((result.get("totals") or {}).get("extra") or {}).get("stock_validation") or {}).get(
                "is_valid"
            )
        )

    def test_empty_line_items_never_valid(self):
        empty = {
            "line_items": [],
            "totals": {"sales_value": None, "closing_value": None, "extra": {}},
        }
        apply_stock_reconciliation(empty, request_id="empty-xlsx")
        validation = (empty.get("totals") or {}).get("extra", {}).get("stock_validation") or {}
        self.assertFalse(validation.get("is_valid"))


if __name__ == "__main__":
    unittest.main()
