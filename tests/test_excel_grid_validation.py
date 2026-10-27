"""Generic Excel mapping and stock-identity warnings. No live Gemini calls."""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from services.excel_grid import discrepancies_for_items, extract_workbook
from services.gemini_extraction_fallback import (
    _controlled_failure,
    maybe_apply_gemini_fallback,
    promote_identity_warning,
    sales_statement_rejection,
)


def _workbook(sheets) -> bytes:
    book = Workbook()
    first = True
    for name, rows, hidden in sheets:
        sheet = book.active if first else book.create_sheet(name)
        first = False
        sheet.title = name
        if hidden:
            sheet.sheet_state = "hidden"
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


class DynamicMappingTests(unittest.TestCase):
    def test_reordered_headers_preserve_values_and_source(self):
        payload = _workbook(
            [
                (
                    "Sep",
                    [
                        ["Note", None],
                        ["Closing", "Medicine", "Open Qty", "Received", "Remark"],
                        ["8", "Abana Tab", 10, 2, "keep me"],
                    ],
                    False,
                ),
                (
                    "Other",
                    [
                        ["Medicine", "Open Qty", "Issued", "Close Qty"],
                        ["Liv 52", 4, 1, 3],
                    ],
                    False,
                ),
            ]
        )
        result = extract_workbook(payload, "shital sep.xlsx", ".xlsx")
        items = {item["product_name"]: item for item in result["line_items"]}
        abana = items["Abana Tab"]
        self.assertEqual(abana["closing_qty"], 8.0)
        self.assertEqual(abana["opening_qty"], 10.0)
        self.assertEqual(abana["receipts_qty"], 2.0)
        self.assertIsNone(abana["sales_qty"])
        self.assertEqual(abana["extra"]["unmapped"]["Remark"], "keep me")
        self.assertEqual(abana["extra"]["source"]["cells"]["closing_qty"], "Sep!A3")
        self.assertEqual(abana["extra"]["source"]["cells"]["product_name"], "Sep!B3")
        self.assertIn("Liv 52", items)
        self.assertEqual(items["Liv 52"]["extra"]["source"]["sheet"], "Other")

    def test_blank_cell_is_not_rewritten_to_zero(self):
        payload = _workbook(
            [
                (
                    "Sep",
                    [
                        ["Medicine", "Opening", "Purchase", "Sales", "Closing"],
                        ["Abana Tab", 10, None, 1, 9],
                    ],
                    False,
                )
            ]
        )
        item = extract_workbook(payload, "shital sep.xlsx", ".xlsx")["line_items"][0]
        self.assertIsNone(item["receipts_qty"])
        self.assertEqual(item["opening_qty"], 10.0)
        self.assertEqual(item["sales_qty"], 1.0)
        finding = discrepancies_for_items([item])[0]
        self.assertEqual(finding["status"], "incomplete")

    def test_stock_mismatch_keeps_printed_closing(self):
        payload = _workbook(
            [
                (
                    "Sep",
                    [
                        ["Medicine", "Opening", "Purchase", "Sales", "Closing"],
                        ["Abana Tab", 10, 0, 1, 5],
                    ],
                    False,
                )
            ]
        )
        result = extract_workbook(payload, "shital sep.xlsx", ".xlsx")
        item = result["line_items"][0]
        self.assertEqual(item["closing_qty"], 5.0)
        self.assertEqual(item["sales_qty"], 1.0)
        self.assertIn("stock_identity_failure", result["totals"]["extra"]["validation_warnings"])
        discrepancy = result["totals"]["extra"]["stock_identity_discrepancies"][0]
        self.assertEqual(discrepancy["expected_closing"], 9.0)
        self.assertEqual(discrepancy["closing_qty"], 5.0)

    def test_ambiguous_header_uses_mocked_model_on_original_grid(self):
        payload = _workbook(
            [
                (
                    "Sep",
                    [
                        ["Item", "Qty", "Closing"],
                        ["Abana Tab", 4, 6],
                    ],
                    False,
                )
            ]
        )
        seen = {}

        def invoke(prompt: str) -> str:
            seen["prompt"] = prompt
            return json.dumps({"columns": {"0": "product_name", "1": "sales_qty", "2": "closing_qty"}})

        result = extract_workbook(payload, "shital sep.xlsx", ".xlsx", invoke=invoke)
        item = result["line_items"][0]
        self.assertIn("\"header\": \"Qty\"", seen["prompt"])
        self.assertNotIn("\"product_name\": \"Abana Tab\"", seen["prompt"])
        self.assertEqual(item["sales_qty"], 4.0)
        self.assertEqual(item["closing_qty"], 6.0)
        self.assertEqual(item["product_name"], "Abana Tab")


class UnrecognizedHeaderTests(unittest.TestCase):
    def test_unknown_header_uses_mocked_text_map_not_a_picture(self):
        payload = _workbook(
            [
                (
                    "HIMALAYA DRUG COMPANY OTX_SEP",
                    [
                        ["Company title"],
                        ["Particulars Name", "OPN", "REC", "SLS", "BAL"],
                        ["Abana Tab", 10, 2, 1, 5],
                    ],
                    False,
                )
            ]
        )
        seen = {}

        def invoke(prompt: str) -> str:
            seen["prompt"] = prompt
            return json.dumps(
                {
                    "header_index": 1,
                    "columns": {
                        "0": "product_name",
                        "1": "opening_qty",
                        "2": "receipts_qty",
                        "3": "sales_qty",
                        "4": "closing_qty",
                    },
                }
            )

        self.assertIsNone(extract_workbook(payload, "statement.xlsx", ".xlsx"))
        result = extract_workbook(payload, "statement.xlsx", ".xlsx", invoke=invoke)
        item = result["line_items"][0]
        self.assertIn("OPN", seen["prompt"])
        self.assertIn("\"index\": 1", seen["prompt"])
        self.assertNotIn("inline_data", seen["prompt"])
        self.assertEqual(item["product_name"], "Abana Tab")
        self.assertEqual(item["closing_qty"], 5.0)
        self.assertEqual(item["opening_qty"], 10.0)
        self.assertEqual(item["sales_qty"], 1.0)
        discrepancy = result["totals"]["extra"]["stock_identity_discrepancies"]
        mismatch = [row for row in discrepancy if row["status"] == "stock_identity_failure"]
        self.assertEqual(mismatch[0]["expected_closing"], 11.0)
        self.assertEqual(mismatch[0]["closing_qty"], 5.0)

    def test_excel_fallback_maps_cells_and_does_not_call_vision(self):
        payload = _workbook(
            [
                (
                    "HIMALAYA DRUG COMPANY OTX_SEP",
                    [
                        ["Company title"],
                        ["Particulars Name", "OPN", "REC", "SLS", "BAL"],
                        ["Abana Tab", 10, 2, 1, 5],
                    ],
                    False,
                )
            ]
        )
        empty = {
            "source_file": "statement.xlsx",
            "line_items": [],
            "totals": {"extra": {"extraction_method": "semantic_xls"}},
        }

        def invoke(prompt: str) -> str:
            self.assertIn("Particulars Name", prompt)
            return json.dumps(
                {
                    "header_index": 1,
                    "columns": {
                        "0": "product_name",
                        "1": "opening_qty",
                        "2": "receipts_qty",
                        "3": "sales_qty",
                        "4": "closing_qty",
                    },
                }
            )

        with patch(
            "services.excel_grid.vertex_column_invoke",
            side_effect=invoke,
        ), patch(
            "services.gemini_extraction_fallback._call_gemini",
            side_effect=AssertionError("vision re-extract"),
        ), patch(
            "services.gemini_extraction_fallback._document_parts",
            side_effect=AssertionError("sheet photograph"),
        ):
            kept = maybe_apply_gemini_fallback(
                empty, payload, "statement.xlsx", ".xlsx"
            )
        detail = sales_statement_rejection(kept, "statement.xlsx")
        self.assertIsNone(detail)
        self.assertEqual(kept["line_items"][0]["product_name"], "Abana Tab")
        self.assertEqual(kept["line_items"][0]["closing_qty"], 5.0)
        self.assertNotEqual(kept["totals"]["extra"].get("gemini_fallback"), "validation_failed")
        self.assertIn(
            "stock_identity_failure",
            kept["totals"]["extra"].get("validation_warnings") or [],
        )


class ValidationGateTests(unittest.TestCase):
    def test_identity_failure_keeps_rows_and_does_not_call_gemini(self):
        result = {
            "source_file": "shital sep.xlsx",
            "line_items": [
                {
                    "product_name": "Abana Tab",
                    "opening_qty": 10,
                    "receipts_qty": 0,
                    "sales_qty": 1,
                    "closing_qty": 5,
                    "extra": {"stock_identity_ok": False},
                }
            ],
            "totals": {"extra": {"extraction_method": "semantic_xls"}},
        }
        quality = {
            "quality": "poor",
            "score": 70,
            "should_fallback": True,
            "reasons": ["stock_identity_failure"],
        }
        with patch(
            "services.gemini_extraction_fallback.evaluate_extraction_quality",
            return_value=quality,
        ), patch(
            "services.gemini_extraction_fallback._call_gemini",
            side_effect=AssertionError("vision re-extract"),
        ):
            kept = maybe_apply_gemini_fallback(result, b"PK", "shital sep.xlsx", ".xlsx")
        self.assertEqual(kept["line_items"][0]["closing_qty"], 5)
        self.assertEqual(kept["line_items"][0]["sales_qty"], 1)
        self.assertEqual(kept["totals"]["extra"]["gemini_fallback"], "validation_warning")
        self.assertNotIn("extraction_failed", kept["totals"]["extra"])
        self.assertIn("stock_identity_failure", kept["totals"]["extra"]["validation_warnings"])
        self.assertEqual(
            kept["line_items"][0]["extra"]["validation_warning"],
            "stock_identity_failure",
        )

    def test_promote_identity_warning_blocks_rejection(self):
        result = {
            "line_items": [{"product_name": "Abana Tab", "closing_qty": 5}],
            "totals": {
                "extra": {
                    "extraction_failed": True,
                    "extraction_quality": {
                        "reasons": ["stock_identity_failure"],
                        "quality": "poor",
                        "score": 70,
                    },
                }
            },
        }
        self.assertTrue(promote_identity_warning(result))
        self.assertNotIn("extraction_failed", result["totals"]["extra"])

    def test_empty_extract_stays_a_real_failure(self):
        failed = _controlled_failure(
            "shital sep.xlsx",
            ".xlsx",
            {"reasons": ["empty_line_items"], "quality": "poor", "score": 0},
            "validation_failed",
        )
        self.assertFalse(promote_identity_warning(failed))
        self.assertTrue(failed["totals"]["extra"]["extraction_failed"])
        detail = sales_statement_rejection(failed, "statement.xlsx")
        self.assertEqual(detail["error"], "extraction_failed")
        self.assertEqual(detail["reasons"], ["empty_line_items"])
        self.assertEqual(detail["gemini_fallback"], "validation_failed")


class StockIdentityRejectionTests(unittest.TestCase):
    """The HTTP 422 that reported only stock_identity_failure."""

    def test_parsed_rows_survive_when_gemini_quality_is_only_the_equation(self):
        parsed_row = {
            "product_name": "ABANA TAB",
            "opening_qty": 10,
            "receipts_qty": 0,
            "sales_qty": 1,
            "closing_qty": 5,
            "extra": {"stock_identity_ok": False},
        }
        result = {
            "source_file": "statement.xlsx",
            "line_items": [dict(parsed_row)],
            "totals": {"extra": {"extraction_method": "semantic_xls"}},
        }
        original_quality = {
            "quality": "poor",
            "score": 40,
            "should_fallback": True,
            "reasons": ["missing_product_names", "stock_identity_failure"],
        }
        gemini_quality = {
            "quality": "poor",
            "score": 70,
            "should_fallback": True,
            "reasons": ["stock_identity_failure"],
        }
        with patch(
            "services.gemini_extraction_fallback.evaluate_extraction_quality",
            side_effect=[original_quality, gemini_quality],
        ), patch(
            "services.gemini_extraction_fallback._document_parts",
            return_value=[{"text": "sheet"}],
        ), patch(
            "services.gemini_extraction_fallback._call_gemini",
            return_value={"line_items": [{"product_name": "ABANA TAB", "closing_qty": 9}]},
        ), patch(
            "services.gemini_extraction_fallback._finish_gemini_result",
            return_value={"line_items": [], "totals": {"extra": {}}},
        ):
            kept = maybe_apply_gemini_fallback(
                result, b"PK", "statement.xlsx", ".xlsx"
            )
        detail = sales_statement_rejection(kept, "statement.xlsx")
        self.assertIsNone(detail)
        self.assertEqual(kept["line_items"][0]["closing_qty"], 5)
        self.assertEqual(kept["line_items"][0]["sales_qty"], 1)
        self.assertEqual(kept["line_items"][0]["opening_qty"], 10)
        extra = kept["totals"]["extra"]
        self.assertNotIn("extraction_failed", extra)
        self.assertNotEqual(extra.get("gemini_fallback"), "validation_failed")
        self.assertEqual(extra.get("gemini_fallback"), "validation_warning")
        self.assertIn("stock_identity_failure", extra.get("validation_warnings") or [])
        self.assertEqual(
            extra["stock_identity_discrepancies"][0]["expected_closing"], 9.0
        )
        self.assertEqual(
            extra["stock_identity_discrepancies"][0]["closing_qty"], 5.0
        )

    def test_real_gate_does_not_reject_a_failed_equation(self):
        result = {
            "source_file": "statement.xlsx",
            "line_items": [
                {
                    "product_name": "ABANA TAB",
                    "opening_qty": 10.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 1.0,
                    "closing_qty": 5.0,
                }
            ],
            "totals": {
                "extra": {
                    "extraction_method": "semantic_xls",
                    "stock_identity_kind": "opening_receipts_sales_closing",
                    "stock_identity_fail_count": 1,
                }
            },
        }
        with patch(
            "services.gemini_extraction_fallback._call_gemini",
            side_effect=AssertionError("vision re-extract"),
        ):
            kept = maybe_apply_gemini_fallback(
                result, b"PK" * 800, "statement.xlsx", ".xlsx"
            )
        self.assertIsNone(sales_statement_rejection(kept, "statement.xlsx"))
        self.assertEqual(kept["line_items"][0]["closing_qty"], 5.0)
        self.assertEqual(kept["totals"]["extra"]["gemini_fallback"], "validation_warning")
        self.assertIn(
            "stock_identity_failure",
            kept["totals"]["extra"]["validation_warnings"],
        )


if __name__ == "__main__":
    unittest.main()
