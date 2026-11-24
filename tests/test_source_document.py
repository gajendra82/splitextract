"""Shared source document and layout-adoption gate.

These tests use constructed files. They do not call Paddle, Vertex, or Gemini.
"""

import io
import unittest
from unittest.mock import patch

import fitz
from openpyxl import Workbook

from services.layout_candidates import layout_candidates, select_layout_engine
from services.source_document import classify_cell
from services.source_grounding import apply_source_grounding


def _statement():
    return {
        "line_items": [
            {
                "product_name": "AMOX",
                "opening_qty": 10,
                "receipts_qty": 0,
                "sales_qty": 2,
                "closing_qty": 8,
                "extra": {},
            }
        ],
        "totals": {"extra": {}},
    }


class CellKindTests(unittest.TestCase):
    def test_blank_zero_number_and_unreadable_stay_distinct(self):
        self.assertEqual(classify_cell(None, quantity=True), "blank")
        self.assertEqual(classify_cell("-", quantity=True), "blank")
        self.assertEqual(classify_cell(0, quantity=True), "explicit_zero")
        self.assertEqual(classify_cell("0", quantity=True), "explicit_zero")
        self.assertEqual(classify_cell("1O", quantity=True), "unreadable")
        self.assertEqual(classify_cell("1O", quantity=False), "text")
        self.assertEqual(classify_cell(10, quantity=True), "number")


class SourceDocumentTests(unittest.TestCase):
    def test_excel_keeps_sheet_headers_blank_zero_and_dynamic_column(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Stock"
        sheet.append(["Product", "Opening", "Receipt", "Issue", "Closing", "Batch"])
        sheet.append(["AMOX", 10, None, 2, 8, "B1"])
        buffer = io.BytesIO()
        workbook.save(buffer)
        result = _statement()
        result["line_items"][0]["receipts_qty"] = 0
        apply_source_grounding(result, buffer.getvalue(), "stock.xlsx")
        report = result["totals"]["extra"]["source_grounding"]
        document = report["source_document"]
        self.assertEqual(document["format"], "xlsx")
        self.assertEqual(document["reader"], "spreadsheet_cells")
        self.assertEqual(document["sheet_names"], ["Stock"])
        self.assertTrue(document["evidence"]["sufficient"])
        self.assertEqual(document["evidence"]["script"], "unspecified")
        table = document["tables"][0]
        self.assertEqual(table["sheet"], "Stock")
        headers = [cell["header"] for cell in table["headers"]]
        self.assertEqual(headers, ["Product", "Opening", "Receipt", "Issue", "Closing", "Batch"])
        batch = table["headers"][-1]
        self.assertIsNone(batch["field"])
        receipt = next(cell for cell in table["rows"][0]["cells"] if cell["field"] == "receipts_qty")
        self.assertEqual(receipt["kind"], "blank")
        self.assertIsNone(receipt["text"])
        self.assertEqual(receipt["sheet"], "Stock")
        opening = next(cell for cell in table["rows"][0]["cells"] if cell["field"] == "opening_qty")
        self.assertEqual(opening["kind"], "number")
        result["line_items"][0]["sales_qty"] = 5
        apply_source_grounding(result, buffer.getvalue(), "stock.xlsx")
        finding = result["totals"]["extra"]["source_grounding"]["discrepancies"][0]
        self.assertEqual(finding["sheet"], "Stock")
        self.assertEqual(result["line_items"][0]["sales_qty"], 5)

    def test_duplicate_quantity_header_is_recorded_on_the_table(self):
        text = "Product\tOpening\tIssue\tIssue\tClosing\nAMOX\t10\t2\t0\t8\n"
        result = _statement()
        apply_source_grounding(result, text.encode("utf-8"), "stock.txt")
        table = result["totals"]["extra"]["source_grounding"]["source_document"]["tables"][0]
        self.assertIn("sales_qty", table["ambiguous_columns"])
        codes = {item["code"] for item in result["totals"]["extra"]["source_grounding"]["discrepancies"]}
        self.assertIn("ambiguous_column", codes)

    def test_unreadable_text_cell_is_kept(self):
        text = "Product\tOpening\tReceipt\tIssue\tClosing\nAMOX\t10\t0\t1O\t8\n"
        result = _statement()
        result["line_items"][0]["sales_qty"] = 0
        apply_source_grounding(result, text.encode("utf-8"), "stock.txt")
        cells = result["totals"]["extra"]["source_grounding"]["source_document"]["tables"][0]["rows"][0]["cells"]
        issue = next(cell for cell in cells if cell["field"] == "sales_qty")
        self.assertEqual(issue["kind"], "unreadable")
        self.assertEqual(issue["text"], "1O")

    def test_pdf_cells_keep_page_and_measured_coordinates(self):
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        for x, text in ((40, "Product"), (140, "Opening"), (220, "Receipt"), (300, "Issue"), (380, "Closing")):
            page.insert_text((x, 80), text, fontsize=8, fontname="helv")
        for x, text in ((40, "AMOX"), (140, "10"), (220, "0"), (300, "2"), (380, "8")):
            page.insert_text((x, 110), text, fontsize=8, fontname="helv")
        payload = doc.tobytes()
        doc.close()
        result = _statement()
        apply_source_grounding(result, payload, "generic.pdf")
        document = result["totals"]["extra"]["source_grounding"]["source_document"]
        self.assertEqual(document["pages"], [1])
        self.assertTrue(document["evidence"]["coordinates_available"])
        cells = document["tables"][0]["rows"][0]["cells"]
        opening = next(cell for cell in cells if cell["field"] == "opening_qty")
        self.assertEqual(opening["page"], 1)
        self.assertEqual(opening["text"], "10")
        self.assertIsNotNone(opening["coordinates"])
        self.assertIn("x0", opening["coordinates"])
        zero = next(cell for cell in cells if cell["field"] == "receipts_qty")
        self.assertEqual(zero["kind"], "explicit_zero")

    def test_scan_without_a_table_does_not_claim_source_evidence(self):
        result = _statement()

        class _Bundle:
            primary = type("Primary", (), {"text": ""})()

        with patch(
            "services.ocr.orchestrator.OCROrchestrator.recognize",
            return_value=_Bundle(),
        ):
            apply_source_grounding(result, b"\x89PNG\r\n", "scan.png")
        document = result["totals"]["extra"]["source_grounding"]["source_document"]
        self.assertFalse(document["evidence"]["sufficient"])
        self.assertEqual(document["tables"], [])
        self.assertTrue(document["evidence"]["blocks_verification"])
        self.assertEqual(document["evidence"]["language"], "unspecified")


class LayoutAdoptionTests(unittest.TestCase):
    def test_unmeasured_layout_engines_are_not_adopted(self):
        names = {item["name"]: item for item in layout_candidates()}
        self.assertEqual(names["paddleocr-vl"]["status"], "adopted")
        self.assertEqual(names["pp-structurev3"]["status"], "not_adopted")
        self.assertIsNone(names["pp-structurev3"]["benchmark"])
        self.assertEqual(names["docling"]["status"], "not_adopted")
        self.assertIsNone(names["docling"]["benchmark"])
        self.assertEqual(select_layout_engine(), "paddleocr-vl")


if __name__ == "__main__":
    unittest.main()
