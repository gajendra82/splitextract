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
        self.assertIn(report["reader"], {"pdf_header_positions", "pdf_word_positions"})
        self.assertGreater(report["blank_read_as_zero"], 0)
        self.assertGreater(report["discrepancy_count"], 0)
        self.assertGreater(report["explicit_zero"], 0)
        names = {item["product_name"]: item for item in result["line_items"]}
        self.assertEqual(names["ZERO ISSUE"]["sales_qty"], 0.0)
        self.assertNotIn("source_discrepancy", names["ZERO ISSUE"].get("extra") or {})
        self.assertTrue(names["MISSING ISSUE"]["extra"]["source_discrepancy"])
        self.assertIn("blank_read_as_zero", names["MISSING ISSUE"]["extra"]["source_validation_error"])
        self.assertIsNone(names["HIORA-GA GEL"]["sales_value"])
        validation = validate_sales_result(result)
        self.assertFalse(validation["accepted"])
        self.assertFalse(independently_verify(result, validation=validation)["passed"])

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
        missing = next(item for item in recovered["line_items"] if item["product_name"] == "MISSING ISSUE")
        self.assertIsNone(missing["sales_qty"])
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

        class _Bundle:
            primary = type("Primary", (), {"text": ""})()

        with patch(
            "services.ocr.orchestrator.OCROrchestrator.recognize",
            return_value=_Bundle(),
        ) as recognize:
            apply_source_grounding(result, b"\x89PNG", "scan.png")
        self.assertEqual(recognize.call_count, 1)
        report = result["totals"]["extra"]["source_grounding"]
        self.assertEqual(report["status"], "insufficient_source_evidence")
        self.assertEqual(report["discrepancy_count"], 0)
        self.assertTrue(report["blocks_verification"])
        self.assertNotIn("source_discrepancy", result["line_items"][0].get("extra") or {})
        apply_source_grounding(result, b"AMOX 10", "notes.txt")
        report = result["totals"]["extra"]["source_grounding"]
        self.assertEqual(report["status"], "insufficient_source_evidence")
        self.assertEqual(report["discrepancy_count"], 0)
        self.assertFalse(report["blocks_verification"])

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
        missing = next(item for item in result["line_items"] if item["product_name"] == "MISSING ISSUE")
        missing["sales_qty"] = None
        payload = _pdf_bytes()
        apply_source_grounding(result, payload, "himal.pdf")
        self.assertEqual(result["totals"]["extra"]["source_grounding"]["discrepancy_count"], 0)
        result["phase1"] = {"validation": validate_sales_result(result)}
        self.assertTrue(result["phase1"]["validation"]["accepted"])
        self.assertEqual(result["phase1"]["validation"]["status"], "pass")
        self.assertTrue(result["phase1"]["validation"]["source_agreement"])
        zero = next(item for item in result["line_items"] if item["product_name"] == "ZERO ISSUE")
        self.assertEqual(zero["sales_qty"], 0.0)

        def caller(_request):
            raise AssertionError("gemini should not run")

        with patch("services.recovery_runner._live_gemini_caller", caller):
            recovered = apply_phase2_recovery(
                result, file_bytes=payload, filename="himal.pdf", gemini_caller=caller
            )
        self.assertEqual(recovered["phase2"]["status"], "not_needed")
        from services.document_intelligence.decisions import VERIFIED, decide_final_status

        verification = independently_verify(
            recovered, validation=recovered["phase1"]["validation"]
        )
        decision = decide_final_status(
            validation=recovered["phase1"]["validation"],
            verification=verification,
            recovery=recovered["phase2"],
        )
        self.assertEqual(decision["status"], VERIFIED)
        self.assertTrue(decision["verified"])

    def test_text_html_and_legacy_doc_use_the_printed_table(self):
        table = "Product\tOpening\tReceipt\tIssue\tClosing\nAMOX\t10\t0\t2\t8\n"
        result = _statement("AMOX", 10, 0, 2, 8)
        apply_source_grounding(result, table.encode("utf-8"), "stock.txt")
        self.assertEqual(result["totals"]["extra"]["source_grounding"]["reader"], "delimited_text")
        self.assertEqual(result["totals"]["extra"]["source_grounding"]["discrepancy_count"], 0)
        html = (
            "<table><tr><th>Product</th><th>Opening</th><th>Receipt</th><th>Issue</th><th>Closing</th></tr>"
            "<tr><td>AMOX</td><td>10</td><td>0</td><td>2</td><td>8</td></tr></table>"
        )
        apply_source_grounding(result, html.encode("utf-8"), "stock.html")
        self.assertEqual(result["totals"]["extra"]["source_grounding"]["discrepancy_count"], 0)
        with patch(
            "services.sales_statement_extractor._extract_legacy_doc_text",
            return_value=table,
        ):
            apply_source_grounding(result, b"\xd0\xcf\x11\xe0", "stock.doc")
        self.assertEqual(result["totals"]["extra"]["source_grounding"]["reader"], "legacy_doc_text")
        result["line_items"][0]["sales_qty"] = 9
        apply_source_grounding(result, table.encode("utf-8"), "stock.txt")
        self.assertTrue(result["line_items"][0]["extra"]["source_discrepancy"])
        self.assertEqual(result["line_items"][0]["sales_qty"], 9)

    def test_unreadable_cell_is_not_treated_as_zero(self):
        text = "Product\tOpening\tReceipt\tIssue\tClosing\nAMOX\t10\t0\t1O\t8\n"
        result = _statement("AMOX", 10, 0, 0, 8)
        apply_source_grounding(result, text.encode("utf-8"), "stock.txt")
        report = result["totals"]["extra"]["source_grounding"]
        self.assertGreater(report["unreadable"], 0)
        self.assertTrue(result["line_items"][0]["extra"]["source_discrepancy"])
        self.assertIn("unreadable_source", result["line_items"][0]["extra"]["source_validation_error"])

    def test_missing_and_duplicate_rows_are_flagged(self):
        text = (
            "Product\tOpening\tReceipt\tIssue\tClosing\n"
            "AMOX\t10\t0\t2\t8\n"
            "BEX\t4\t0\t1\t3\n"
        )
        result = _statement("AMOX", 10, 0, 2, 8)
        second = _statement("AMOX", 10, 0, 2, 8)["line_items"][0]
        result["line_items"].append(second)
        apply_source_grounding(result, text.encode("utf-8"), "stock.txt")
        report = result["totals"]["extra"]["source_grounding"]
        self.assertGreaterEqual(report["missing_rows"], 1)
        self.assertGreaterEqual(report["duplicate_rows"], 1)
        self.assertFalse(validate_sales_result(result)["accepted"])

    def test_scanned_pdf_and_image_use_existing_ocr_text_without_a_new_ocr_call(self):
        doc = fitz.open()
        doc.new_page(width=612, height=792)
        payload = doc.tobytes()
        doc.close()
        ocr = "Product  Opening  Receipt  Issue  Closing\nAMOX  10  0  2  8\n"
        result = _statement("AMOX", 10, 0, 4, 6)
        with patch("services.ocr.orchestrator.OCROrchestrator.recognize", side_effect=AssertionError("ocr")):
            apply_source_grounding(result, payload, "scan.pdf", ocr_text=ocr)
        self.assertEqual(result["totals"]["extra"]["source_grounding"]["reader"], "ocr_text")
        self.assertTrue(result["line_items"][0]["extra"]["source_discrepancy"])
        image = _statement("AMOX", 10, 0, 2, 8)
        with patch("services.ocr.orchestrator.OCROrchestrator.recognize", side_effect=AssertionError("ocr")):
            apply_source_grounding(image, b"\x89PNG\r\n", "scan.png", ocr_text=ocr)
        self.assertEqual(image["totals"]["extra"]["source_grounding"]["discrepancy_count"], 0)

    def test_excel_recovery_uses_cells_and_rejects_an_arithmetic_correction(self):
        import os

        from services.sales_statement_extractor import _maybe_apply_phase2_recovery

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Product", "Opening", "Receipt", "Issue", "Closing"])
        sheet.append(["AMOX", 10, 0, 2, 8])
        buffer = io.BytesIO()
        workbook.save(buffer)
        payload = buffer.getvalue()
        result = _statement("AMOX", 10, 0, 5, 5)
        result["totals"]["extra"]["extraction_method"] = "generic_excel_grid"
        result["totals"]["extra"]["stock_identity_kind"] = "opening_receipts_sales_closing"
        apply_source_grounding(result, payload, "sheet.xlsx")
        result["phase1"] = {"validation": validate_sales_result(result)}
        seen = []

        def caller(request):
            seen.append(request)
            parts = request["contents"][0]["parts"]
            self.assertTrue(all("inline_data" not in part for part in parts))
            self.assertIn("2", parts[1]["text"])
            return json.dumps(
                {
                    "line_items": [
                        {
                            "fixed": {"product_name": "AMOX"},
                            "dynamic": {"Issue": 7, "Closing": 3},
                        }
                    ]
                }
            )

        with patch.dict(os.environ, {"PHASE2_RECOVERY_ENABLED": "true"}):
            with patch(
                "services.recovery_runner.apply_phase2_recovery",
                return_value={"called": True},
            ) as mocked:
                out = _maybe_apply_phase2_recovery(result, payload, "sheet.xlsx")
        self.assertEqual(out, {"called": True})
        mocked.assert_called_once()
        recovered = apply_phase2_recovery(
            result, file_bytes=payload, filename="sheet.xlsx", gemini_caller=caller
        )
        row = recovered["line_items"][0]
        self.assertEqual(row["sales_qty"], 5)
        self.assertEqual(row["closing_qty"], 5)
        self.assertEqual(recovered["phase2"]["status"], "unresolved")
        self.assertTrue(seen)

        def corrected(_request):
            return json.dumps(
                {
                    "line_items": [
                        {
                            "fixed": {"product_name": "AMOX"},
                            "dynamic": {"Issue": 2, "Closing": 8},
                        }
                    ]
                }
            )

        recovered = apply_phase2_recovery(
            result, file_bytes=payload, filename="sheet.xlsx", gemini_caller=corrected
        )
        self.assertEqual(recovered["line_items"][0]["sales_qty"], 2)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 8)
        self.assertEqual(recovered["phase2"]["status"], "recovered")
        self.assertEqual(recovered["totals"]["extra"]["source_grounding"]["discrepancy_count"], 0)

    def test_blank_quantity_is_restored_without_gemini(self):
        result = _parsed_fixture()
        payload = _pdf_bytes()
        apply_source_grounding(result, payload, "himal.pdf")
        result["phase1"] = {"validation": validate_sales_result(result)}
        self.assertFalse(result["phase1"]["validation"]["accepted"])

        def caller(_request):
            raise AssertionError("a blank cell is already evidenced")

        recovered = apply_phase2_recovery(
            result, file_bytes=payload, filename="himal.pdf", gemini_caller=caller
        )
        missing = next(item for item in recovered["line_items"] if item["product_name"] == "MISSING ISSUE")
        zero = next(item for item in recovered["line_items"] if item["product_name"] == "ZERO ISSUE")
        self.assertIsNone(missing["sales_qty"])
        self.assertEqual(zero["sales_qty"], 0.0)
        self.assertEqual(recovered["phase2"]["status"], "recovered")
        self.assertEqual(recovered["phase2"]["attempts"], 0)
        self.assertEqual(recovered["totals"]["extra"]["source_grounding"]["blank_read_as_zero"], 0)
        self.assertTrue(recovered["phase1"]["validation"]["source_agreement"])
        from services.document_intelligence.decisions import (
            RECOVERED_AND_VERIFIED,
            decide_final_status,
        )

        verification = independently_verify(
            recovered, validation=recovered["phase1"]["validation"]
        )
        decision = decide_final_status(
            validation=recovered["phase1"]["validation"],
            verification=verification,
            recovery=recovered["phase2"],
        )
        self.assertEqual(decision["status"], RECOVERED_AND_VERIFIED)
        self.assertTrue(decision["verified"])

    def test_low_name_match_is_unresolved_without_false_row_flags(self):
        text = (
            "Product\tOpening\tReceipt\tIssue\tClosing\n"
            "AMOX\t10\t0\t2\t8\n"
            "BEX\t4\t0\t1\t3\n"
            "CEE\t1\t0\t0\t1\n"
            "DEE\t2\t0\t0\t2\n"
        )
        result = _statement("AMOX", 10, 0, 2, 8)
        for name in ("ZED", "YEW", "XEE"):
            result["line_items"].append(_statement(name, 1, 0, 0, 1)["line_items"][0])
        apply_source_grounding(result, text.encode("utf-8"), "stock.txt")
        report = result["totals"]["extra"]["source_grounding"]
        self.assertEqual(report["low_name_match"], 1)
        self.assertEqual(report["missing_rows"], 0)
        self.assertEqual(report["duplicate_rows"], 0)
        self.assertFalse(any(item.get("extra", {}).get("source_discrepancy") for item in result["line_items"]))
        self.assertFalse(validate_sales_result(result)["accepted"])

    def test_incomplete_page_is_flagged_when_names_align(self):
        doc = fitz.open()
        for product in ("AMOX", "BEX"):
            page = doc.new_page(width=612, height=792)
            for x, text in (
                (48, "Product"),
                (180, "Opening"),
                (250, "Receipt"),
                (320, "Issue"),
                (390, "Closing"),
            ):
                page.insert_text((x, 80), text, fontsize=8)
            page.insert_text((48, 140), product, fontsize=8)
            for x, value in ((180, "10"), (250, "0"), (320, "2"), (390, "8")):
                page.insert_text((x, 140), value, fontsize=8)
        payload = doc.tobytes()
        doc.close()
        result = _statement("AMOX", 10, 0, 2, 8)
        result["line_items"][0]["extra"] = {"source_page": 1}
        apply_source_grounding(result, payload, "pages.pdf")
        report = result["totals"]["extra"]["source_grounding"]
        codes = {item.get("code") for item in report["discrepancies"]}
        self.assertIn("incomplete_page", codes)
        self.assertIn("missing_row", codes)
        self.assertEqual(report["low_name_match"], 0)
        self.assertFalse(validate_sales_result(result)["accepted"])

    def test_unknown_identity_keeps_source_correction_unverified(self):
        from services.document_intelligence.decisions import decide_final_status

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Product", "Opening", "Receipt", "Issue", "Closing"])
        sheet.append(["AMOX", 10, 0, 2, 8])
        buffer = io.BytesIO()
        workbook.save(buffer)
        payload = buffer.getvalue()
        result = _statement("AMOX", 10, 0, 5, 5)
        result["totals"]["extra"]["extraction_method"] = "unseen_customer_layout"
        result["totals"]["extra"]["stock_identity_kind"] = "unseen_customer_layout"
        result = _apply_stock_identity_validation(result)
        apply_source_grounding(result, payload, "sheet.xlsx")
        result["phase1"] = {"validation": validate_sales_result(result)}
        self.assertTrue(result["phase1"]["validation"]["checks"]["stock_identity"])
        self.assertEqual(result["phase1"]["validation"]["business_validation"]["status"], "unknown")
        self.assertEqual(result["phase1"]["validation"]["status"], "unresolved")
        self.assertFalse(result["phase1"]["validation"]["source_agreement"])

        def corrected(_request):
            return json.dumps(
                {
                    "line_items": [
                        {
                            "fixed": {"product_name": "AMOX"},
                            "dynamic": {"Issue": 2, "Closing": 8},
                        }
                    ]
                }
            )

        recovered = apply_phase2_recovery(
            result, file_bytes=payload, filename="sheet.xlsx", gemini_caller=corrected
        )
        self.assertEqual(recovered["line_items"][0]["sales_qty"], 2.0)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 8.0)
        self.assertEqual(recovered["phase2"]["status"], "recovered")
        self.assertTrue(recovered["phase1"]["validation"]["accepted"])
        self.assertEqual(recovered["phase1"]["validation"]["status"], "unresolved")
        decision = decide_final_status(
            validation=recovered["phase1"]["validation"],
            verification={"passed": True},
            recovery=recovered["phase2"],
        )
        self.assertFalse(decision["verified"])
        self.assertEqual(decision["reason"], "identity_profile_unknown")

    def test_source_match_keeps_a_real_arithmetic_failure(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Product", "Opening", "Receipt", "Issue", "Closing"])
        sheet.append(["AMOX", 10, 0, 5, 3])
        buffer = io.BytesIO()
        workbook.save(buffer)
        payload = buffer.getvalue()
        result = _statement("AMOX", 10, 0, 5, 3)
        result["totals"]["extra"]["extraction_method"] = "generic_excel_grid"
        result["totals"]["extra"]["stock_identity_kind"] = "opening_receipts_sales_closing"
        result = _apply_stock_identity_validation(result)
        apply_source_grounding(result, payload, "sheet.xlsx")
        validation = validate_sales_result(result)
        self.assertTrue(validation["source_agreement"])
        self.assertEqual(validation["business_validation"]["status"], "inconsistent")
        self.assertIn("stock_identity_failure", validation["reasons"])
        result["phase1"] = {"validation": validation}

        def balanced(_request):
            return json.dumps(
                {
                    "line_items": [
                        {
                            "fixed": {"product_name": "AMOX"},
                            "dynamic": {"Issue": 2, "Closing": 8},
                        }
                    ]
                }
            )

        recovered = apply_phase2_recovery(
            result, file_bytes=payload, filename="sheet.xlsx", gemini_caller=balanced
        )
        self.assertEqual(recovered["line_items"][0]["sales_qty"], 5)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 3)
        self.assertEqual(recovered["phase2"]["status"], "unresolved")
        self.assertFalse(recovered["totals"]["extra"]["stock_validation"]["is_valid"])
        from services.document_intelligence.decisions import UNRESOLVED, decide_final_status

        verification = independently_verify(
            recovered, validation=recovered["phase1"]["validation"]
        )
        decision = decide_final_status(
            validation=recovered["phase1"]["validation"],
            verification=verification,
            recovery=recovered["phase2"],
        )
        self.assertEqual(decision["status"], UNRESOLVED)
        self.assertFalse(decision["verified"])
        self.assertEqual(decision["reason"], "stock_identity_failure")

    def test_scan_without_ocr_text_calls_the_orchestrator_once(self):
        calls = []

        class _Primary:
            text = "Product  Opening  Receipt  Issue  Closing\nAMOX  10  0  2  8\n"

        class _Bundle:
            primary = _Primary()

        def recognize(_self, _image, page=1, **_kwargs):
            calls.append(page)
            return _Bundle()

        result = _statement("AMOX", 10, 0, 2, 8)
        with patch("services.ocr.orchestrator.OCROrchestrator.recognize", recognize):
            apply_source_grounding(result, b"\x89PNG\r\n", "scan.png")
            self.assertEqual(calls, [1])
            self.assertEqual(result["totals"]["extra"]["source_grounding"]["reader"], "ocr_text")
            self.assertEqual(result["totals"]["extra"]["source_grounding"]["discrepancy_count"], 0)
            apply_source_grounding(result, b"\x89PNG\r\n", "scan.png")
            self.assertEqual(calls, [1])

    def test_ocr_failure_does_not_claim_verification(self):
        from services.document_intelligence.decisions import decide_final_status

        calls = []

        def recognize(_self, _image, page=1, **_kwargs):
            calls.append(page)
            raise RuntimeError("ocr down")

        result = _statement("AMOX", 10, 0, 2, 8)
        with patch("services.ocr.orchestrator.OCROrchestrator.recognize", recognize):
            apply_source_grounding(result, b"\x89PNG\r\n", "scan.png")
            apply_source_grounding(result, b"\x89PNG\r\n", "scan.png")
        self.assertEqual(calls, [1])
        report = result["totals"]["extra"]["source_grounding"]
        self.assertEqual(report["status"], "insufficient_source_evidence")
        self.assertTrue(report["blocks_verification"])
        self.assertTrue(report["ocr_failed"])
        validation = validate_sales_result(result)
        self.assertFalse(validation["source_evidence"])
        self.assertEqual(validation["status"], "unresolved")
        self.assertNotEqual(validation["status"], "pass")
        verification = independently_verify(result, validation=validation)
        self.assertFalse(verification["passed"])
        self.assertIn("insufficient_source_evidence", verification["issues"])
        decision = decide_final_status(
            validation=validation, verification=verification, recovery={}
        )
        self.assertFalse(decision["verified"])


class DailyStockSourceMatchTests(unittest.TestCase):
    def test_printed_adjustment_is_in_closing_and_gemini_is_not_called(self):
        """Cl. Stock = Op + Pur - Sales Retu. - Sales Qty + Adj./Dmg.

        Order Qty is excluded. CLARINA is 51 + 0 - 0 - 9 + (-1) = 41.
        """
        import os

        from services.document_intelligence.decisions import VERIFIED
        from services.sales_statement_extractor import extract_sales_statement

        text = """
NARAYAN MEDICAL (NEW)                                                02/09/2026
BHANDERHATI, HOOGHLY                                                 17:26:01
** Daily Stock Summary (Company-wise) **          From 01/08/2026 to 31/08/2026
Company:HIMALAYA ZEUS [M177]
        Product & Pack        |  Op. | Pur. |Sales|Sales| Adj./| Cl.  | Order |
                              | Stock| Qty. |Retu.| Qty.| Dmg. |Stock | Qty.  |
AACTARIL SOAP,75GM                 62      0     0    10      0     52     -52
CLARINA ANTI ACNE FACEWAS,60ML     51      0     0     9     -1     41     -41
HIORA K TOOTH PASTE,100GM           0     50     0    10      0     40     -40
"""

        def boom(_payload):
            raise AssertionError("printed quantities already balance; Gemini must not be called")

        with patch.dict(os.environ, {"PHASE2_RECOVERY_ENABLED": "true"}), patch(
            "services.recovery_runner._live_gemini_caller", side_effect=boom
        ):
            result = extract_sales_statement(text.encode("utf-8"), "narayan.txt")
        clarina = next(
            item for item in result["line_items"] if "CLARINA" in item["product_name"]
        )
        self.assertEqual(clarina["opening_qty"], 51)
        self.assertEqual(clarina["receipts_qty"], 0)
        self.assertEqual(clarina["sales_qty"], 9)
        self.assertEqual(clarina["closing_qty"], 41)
        self.assertEqual(clarina["extra"]["sales_return_qty"], 0)
        self.assertEqual(clarina["extra"]["adj_dmg_qty"], -1)
        self.assertEqual(clarina["extra"]["order_qty"], -41)
        self.assertEqual(clarina["extra"]["expected_closing"], 41)
        self.assertTrue(clarina["extra"]["stock_identity_ok"])
        extra = result["totals"]["extra"]
        self.assertEqual(
            extra["stock_identity_formula"],
            "closing=opening+purchase-sales_return-sales+adj_dmg; order_qty excluded",
        )
        self.assertEqual(extra["stock_identity_fail_count"], 0)
        self.assertTrue(result["phase1"]["validation"]["source_agreement"])
        self.assertEqual(result["phase1"]["validation"]["status"], "pass")
        self.assertEqual(result["phase2"]["status"], "not_needed")
        decision = result["document_intelligence"]["decision"]
        self.assertEqual(decision["status"], VERIFIED)
        self.assertTrue(decision["verified"])
