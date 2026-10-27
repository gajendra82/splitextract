"""Source re-read versus extracted JSON. Gemini is mocked. No network."""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

import fitz
from openpyxl import Workbook

from services.base_validator import validate_sales_result
from services.document_intelligence.verification import independently_verify
from services.recovery_runner import apply_phase2_recovery
from services.sales_statement_extractor import (
    _apply_stock_identity_validation,
    _ensure_stock_qty_value_fields,
    _parse_pdf,
    empty_line_item,
)
from services.source_grounding import apply_source_grounding
from tests.test_swil_opbal_lastpurc_pdf import _fixture_doc


def _pdf_bytes():
    doc = _fixture_doc()
    try:
        return doc.tobytes()
    finally:
        doc.close()


def _parsed_fixture():
    result = _parse_pdf(_pdf_bytes(), "himal.pdf")
    result = _apply_stock_identity_validation(result)
    return _ensure_stock_qty_value_fields(result)


def _statement(name, opening, receipts, sales, closing, packing="10"):
    item = empty_line_item()
    item["product_name"] = name
    item["packing"] = packing
    item["opening_qty"] = opening
    item["receipts_qty"] = receipts
    item["sales_qty"] = sales
    item["closing_qty"] = closing
    item["extra"] = {}
    return {
        "source_file": "sheet.xlsx",
        "line_items": [item],
        "totals": {"extra": {"extraction_method": "excel_grid"}},
    }


class SourceGroundingTests(unittest.TestCase):
    def test_swil_fixture_matches_source_and_keeps_blank_distinct_from_zero(self):
        result = _parsed_fixture()
        apply_source_grounding(result, _pdf_bytes(), "himal.pdf")
        report = result["totals"]["extra"]["source_grounding"]
        self.assertEqual(report["status"], "checked")
        self.assertEqual(report["reader"], "pdf_word_positions")
        self.assertEqual(report["discrepancy_count"], 0)
        self.assertGreater(report["blank_read_as_zero"], 0)
        names = {item["product_name"]: item for item in result["line_items"]}
        self.assertEqual(names["ZERO ISSUE"]["sales_qty"], 0.0)
        self.assertIsNone(names["HIORA-GA GEL"]["sales_value"])
        self.assertFalse(any(item.get("extra", {}).get("source_discrepancy") for item in result["line_items"]))

    def test_balanced_wrong_quantity_is_flagged_and_not_rewritten(self):
        result = _parsed_fixture()
        row = next(item for item in result["line_items"] if item["product_name"] == "HIORA-GA GEL")
        row["sales_qty"] = 10
        row["closing_qty"] = 24
        apply_source_grounding(result, _pdf_bytes(), "himal.pdf")
        self.assertEqual(row["sales_qty"], 10)
        self.assertEqual(row["closing_qty"], 24)
        self.assertTrue(row["extra"]["source_discrepancy"])
        self.assertIn("sales_qty", row["extra"]["source_validation_error"])
        self.assertIn("12", row["extra"]["source_validation_error"])
        validation = validate_sales_result(result)
        self.assertFalse(validation["accepted"])
        self.assertIn("source_discrepancy", validation["reasons"])
        verification = independently_verify(result, validation=validation)
        self.assertFalse(verification["passed"])
        self.assertIn("source_discrepancy", verification["issues"])

    def test_explicit_zero_matches_printed_zero(self):
        result = _parsed_fixture()
        row = next(item for item in result["line_items"] if item["product_name"] == "ZERO ISSUE")
        self.assertEqual(row["sales_qty"], 0.0)
        apply_source_grounding(result, _pdf_bytes(), "himal.pdf")
        self.assertNotIn("source_discrepancy", row.get("extra") or {})

    def test_recovery_accepts_only_a_source_matching_correction(self):
        result = _parsed_fixture()
        row = next(item for item in result["line_items"] if item["product_name"] == "HIORA-GA GEL")
        row["sales_qty"] = 10
        row["closing_qty"] = 24
        payload = _pdf_bytes()
        apply_source_grounding(result, payload, "himal.pdf")
        result["phase1"] = {"validation": validate_sales_result(result)}
        seen = []

        def caller(request):
            seen.append(request["contents"][0]["parts"][0]["text"])
            return json.dumps(
                {
                    "line_items": [
                        {
                            "fixed": {"product_name": "HIORA-GA GEL"},
                            "dynamic": {"Issue Qty.": 12, "Closing Balance": 22},
                        }
                    ]
                }
            )

        recovered = apply_phase2_recovery(
            result, file_bytes=payload, filename="himal.pdf", gemini_caller=caller
        )
        fixed = next(item for item in recovered["line_items"] if item["product_name"] == "HIORA-GA GEL")
        self.assertEqual(fixed["sales_qty"], 12)
        self.assertEqual(fixed["closing_qty"], 22)
        self.assertNotIn("source_discrepancy", fixed.get("extra") or {})
        self.assertEqual(recovered["phase2"]["status"], "recovered")
        self.assertTrue(recovered["phase1"]["validation"]["accepted"])
        self.assertEqual(recovered["totals"]["extra"]["source_grounding"]["discrepancy_count"], 0)
        self.assertIn("HIORA-GA GEL", seen[0])
        self.assertIn("source:", seen[0])
        self.assertIn("Issue Qty. = sales_qty", seen[0])
        self.assertEqual(result["line_items"][0]["sales_qty"], 10)

    def test_recovery_rejects_a_balanced_value_the_source_does_not_print(self):
        result = _parsed_fixture()
        row = next(item for item in result["line_items"] if item["product_name"] == "HIORA-GA GEL")
        row["sales_qty"] = 10
        row["closing_qty"] = 24
        payload = _pdf_bytes()
        apply_source_grounding(result, payload, "himal.pdf")
        result["phase1"] = {"validation": validate_sales_result(result)}

        def caller(_request):
            return json.dumps(
                {
                    "line_items": [
                        {
                            "fixed": {"product_name": "HIORA-GA GEL"},
                            "dynamic": {"Issue Qty.": 9, "Closing Balance": 25},
                        }
                    ]
                }
            )

        recovered = apply_phase2_recovery(
            result, file_bytes=payload, filename="himal.pdf", gemini_caller=caller
        )
        fixed = next(item for item in recovered["line_items"] if item["product_name"] == "HIORA-GA GEL")
        self.assertEqual(fixed["sales_qty"], 10)
        self.assertEqual(fixed["closing_qty"], 24)
        self.assertEqual(recovered["phase2"]["status"], "unresolved")
        self.assertEqual(recovered["phase2"]["attempts"], 2)

    def test_null_recovery_does_not_invent_a_quantity(self):
        result = _parsed_fixture()
        row = next(item for item in result["line_items"] if item["product_name"] == "HIORA-GA GEL")
        row["sales_qty"] = 10
        row["closing_qty"] = 24
        payload = _pdf_bytes()
        apply_source_grounding(result, payload, "himal.pdf")
        result["phase1"] = {"validation": validate_sales_result(result)}

        def caller(_request):
            return json.dumps(
                {
                    "line_items": [
                        {
                            "fixed": {"product_name": "HIORA-GA GEL"},
                            "dynamic": {"Issue Qty.": None, "Closing Balance": None},
                        }
                    ]
                }
            )

        recovered = apply_phase2_recovery(
            result, file_bytes=payload, filename="himal.pdf", gemini_caller=caller
        )
        fixed = next(item for item in recovered["line_items"] if item["product_name"] == "HIORA-GA GEL")
        self.assertEqual(fixed["sales_qty"], 10)
        self.assertEqual(fixed["closing_qty"], 24)
        self.assertEqual(recovered["phase2"]["status"], "unresolved")

    def test_excel_cells_match_and_a_wrong_cell_is_flagged(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Product", "Opening", "Receipt", "Issue", "Closing"])
        sheet.append(["AMOX", 10, 0, 2, 8])
        buffer = io.BytesIO()
        workbook.save(buffer)
        payload = buffer.getvalue()
        result = _statement("AMOX", 10, 0, 2, 8)
        apply_source_grounding(result, payload, "sheet.xlsx")
        report = result["totals"]["extra"]["source_grounding"]
        self.assertEqual(report["status"], "checked")
        self.assertEqual(report["discrepancy_count"], 0)
        result["line_items"][0]["sales_qty"] = 5
        result["line_items"][0]["closing_qty"] = 5
        apply_source_grounding(result, payload, "sheet.xlsx")
        self.assertEqual(result["line_items"][0]["sales_qty"], 5)
        self.assertTrue(result["line_items"][0]["extra"]["source_discrepancy"])
        self.assertIn("source_discrepancy", validate_sales_result(result)["reasons"])

    def test_image_and_text_do_not_invent_a_discrepancy(self):
        result = _statement("AMOX", 10, 0, 2, 8)
        for name, payload in (("scan.png", b"\x89PNG"), ("notes.txt", b"AMOX 10")):
            apply_source_grounding(result, payload, name)
            report = result["totals"]["extra"]["source_grounding"]
            self.assertEqual(report["status"], "insufficient_source_evidence")
            self.assertEqual(report["discrepancy_count"], 0)
            self.assertNotIn("source_discrepancy", result["line_items"][0]["extra"])

    def test_docx_table_is_compared(self):
        from docx import Document

        document = Document()
        table = document.add_table(rows=2, cols=5)
        for index, value in enumerate(("Product", "Opening", "Receipt", "Issue", "Closing")):
            table.rows[0].cells[index].text = value
        for index, value in enumerate(("AMOX", "10", "0", "2", "8")):
            table.rows[1].cells[index].text = value
        buffer = io.BytesIO()
        document.save(buffer)
        result = _statement("AMOX", 10, 0, 2, 8)
        apply_source_grounding(result, buffer.getvalue(), "stock.docx")
        self.assertEqual(result["totals"]["extra"]["source_grounding"]["discrepancy_count"], 0)
        result["line_items"][0]["opening_qty"] = 4
        apply_source_grounding(result, buffer.getvalue(), "stock.docx")
        self.assertEqual(result["line_items"][0]["opening_qty"], 4)
        self.assertTrue(result["line_items"][0]["extra"]["source_discrepancy"])

    def test_generic_pdf_table_flags_only_a_real_mismatch(self):
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        for x, text in ((40, "Product"), (140, "Opening"), (220, "Receipt"), (300, "Issue"), (380, "Closing")):
            page.insert_text((x, 80), text, fontsize=8, fontname="helv")
        for x, text in ((40, "AMOX"), (140, "10"), (220, "0"), (300, "2"), (380, "8")):
            page.insert_text((x, 110), text, fontsize=8, fontname="helv")
        payload = doc.tobytes()
        doc.close()
        result = _statement("AMOX", 10, 0, 2, 8)
        apply_source_grounding(result, payload, "generic.pdf")
        report = result["totals"]["extra"]["source_grounding"]
        self.assertEqual(report["status"], "checked")
        self.assertEqual(report["discrepancy_count"], 0)
        result["line_items"][0]["sales_qty"] = 0
        apply_source_grounding(result, payload, "generic.pdf")
        self.assertTrue(result["line_items"][0]["extra"]["source_discrepancy"])
        self.assertIn("2", result["line_items"][0]["extra"]["source_validation_error"])

    def test_clean_statement_does_not_call_gemini(self):
        result = _parsed_fixture()
        payload = _pdf_bytes()
        apply_source_grounding(result, payload, "himal.pdf")
        result["phase1"] = {"validation": validate_sales_result(result)}

        def caller(_request):
            raise AssertionError("gemini should not run")

        with patch("services.recovery_runner._live_gemini_caller", caller):
            recovered = apply_phase2_recovery(
                result, file_bytes=payload, filename="himal.pdf", gemini_caller=caller
            )
        self.assertEqual(recovered["phase2"]["status"], "not_needed")
