"""Swil receipt/issue validation, quality gate, and mocked Gemini recovery.

The nine product names are the rows named in the SEP HIMAL recovery log.
Quantities are synthetic fixtures. They are not values read from that PDF.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from services.base_validator import validate_sales_result
from services.extraction_quality import evaluate_extraction_quality
from services.recovery_runner import apply_phase2_recovery, build_recovery_payload
from services.sales_statement_extractor import (
    _apply_stock_identity_validation,
    empty_line_item,
    empty_result,
)
from services.stock_identity_profiles import PROFILE_SWIL_RECEIPT, select_profile


# Names only. Quantities below are fixtures, not PDF ground truth.
FAILING_PRODUCTS = (
    "HIORA-GA GEL",
    "LIV 52",
    "LIV 52 DS TAB",
    "LIV 52 DS 200",
    "LIV 52 DS SY",
    "LUKOL",
    "LUKOL SY",
    "RENOSANTE",
    "SEPTILIN SYP",
)


def _item(name, opening, receipts, issue, closing, **extra_fields):
    item = empty_line_item()
    item["product_name"] = name
    item["packing"] = "1"
    item["opening_qty"] = opening
    item["receipts_qty"] = receipts
    item["sales_qty"] = issue
    item["closing_qty"] = closing
    item["extra"].update(extra_fields)
    return item


def _statement(items):
    result = empty_result("swil-fixture.pdf", "pdf")
    result["line_items"] = items
    result["totals"]["extra"]["extraction_method"] = "swil_receipt_pur_value_vision"
    result["totals"]["extra"]["layout"] = "swil_receipt_pur_value"
    return result


def _prepared(items):
    statement = _apply_stock_identity_validation(_statement(items))
    statement["phase1"] = {
        "route": {"requires_ocr": False, "pdf_kind": "digital"},
        "validation": validate_sales_result(statement),
    }
    return statement


def _recovery_json(name, closing, **dynamic):
    dynamic = {
        "Opening Qty": 10,
        "Receipt Qty": 5,
        "Issue/Sales Qty": 2,
        "Closing Qty": closing,
        **dynamic,
    }
    return json.dumps(
        {
            "line_items": [
                {
                    "fixed": {"product_name": name},
                    "dynamic": dynamic,
                }
            ]
        }
    )


class SwilIdentityTests(unittest.TestCase):
    def test_profile_is_receipt_issue_even_when_sale_return_is_present(self):
        item = _item(
            "HIORA-GA GEL",
            10,
            5,
            2,
            13,
            total_stock=15,
            sale_return=0,
            stock_out_qty=4,
            sales_scheme_qty=1,
        )
        statement = _statement([item])
        self.assertEqual(select_profile(statement).profile_id, PROFILE_SWIL_RECEIPT)

    def test_total_qty_is_not_sales_and_scheme_is_not_deducted(self):
        item = _item(
            "LIV 52",
            10,
            5,
            2,
            13,
            total_stock=15,
            stock_out_qty=4,
            sales_scheme_qty=1,
        )
        checked = _apply_stock_identity_validation(_statement([item]))
        row = checked["line_items"][0]
        self.assertEqual(row["sales_qty"], 2)
        self.assertEqual(row["closing_qty"], 13)
        self.assertEqual(row["extra"]["total_stock"], 15)
        self.assertTrue(row["extra"]["stock_identity_ok"])
        self.assertEqual(row["extra"]["expected_closing"], 13)
        self.assertEqual(row["extra"]["expected_total"], 15)
        self.assertTrue(checked["totals"]["extra"]["stock_validation"]["is_valid"])
        self.assertEqual(
            checked["totals"]["extra"]["stock_identity_kind"],
            "swil_receipt_issue",
        )

    def test_wrong_closing_fails_without_rewriting_printed_quantities(self):
        item = _item("LUKOL", 10, 5, 15, 13, total_stock=15)
        checked = _apply_stock_identity_validation(_statement([item]))
        row = checked["line_items"][0]
        self.assertEqual(row["sales_qty"], 15)
        self.assertEqual(row["closing_qty"], 13)
        self.assertFalse(row["extra"]["stock_identity_ok"])
        self.assertIn("expected 0", row["extra"]["validation_error"])
        self.assertEqual(checked["totals"]["extra"]["stock_identity_fail_count"], 1)


class SwilQualityGateTests(unittest.TestCase):
    def test_failing_rows_are_not_a_perfect_keep_even_without_ocr(self):
        items = [
            _item(name, 10, 5, 2, 99, total_stock=15) for name in FAILING_PRODUCTS
        ]
        checked = _apply_stock_identity_validation(_statement(items))
        quality = evaluate_extraction_quality(
            checked,
            {"pdf_kind": "digital", "requires_ocr": False},
        )
        self.assertNotEqual(quality["score"], 100)
        self.assertTrue(quality["should_fallback"])
        self.assertIn("stock_identity_failure", quality["reasons"])
        self.assertEqual(quality["parser"], "swil_receipt_pur_value_vision")

    def test_invalid_statement_is_not_perfect_below_the_percent_gate(self):
        """9 failures among 80 rows is under 20%, and this file still must not score 100."""
        items = [
            _item(name, 10, 5, 2, 99, total_stock=15) for name in FAILING_PRODUCTS
        ]
        items.extend(
            _item(f"PASS {index}", 10, 5, 2, 13, total_stock=15)
            for index in range(71)
        )
        checked = _apply_stock_identity_validation(_statement(items))
        self.assertEqual(checked["totals"]["extra"]["stock_identity_fail_count"], 9)
        self.assertFalse(checked["totals"]["extra"]["stock_validation"]["is_valid"])
        quality = evaluate_extraction_quality(
            checked,
            {"pdf_kind": "digital", "requires_ocr": False},
        )
        self.assertNotEqual(quality["score"], 100)
        self.assertTrue(quality["should_fallback"])
        self.assertIn("stock_identity_failure", quality["reasons"])
        from services.document_intelligence.validation_stack import build_validation_stack
        from services.document_intelligence.verification import independently_verify

        validation = {
            "accepted": False,
            "reasons": ["stock_identity_failure"],
            "checks": {"stock_identity": False},
            "failure_type": "STOCK_IDENTITY_FAILURE",
        }
        verification = independently_verify(checked, validation=validation)
        self.assertFalse(verification["checks"]["stock_identity_ok"])
        stack = build_validation_stack(checked, phase1_validation=validation)
        business = next(layer for layer in stack["layers"] if layer["name"] == "business_identity")
        self.assertFalse(business["passed"])


class SwilRecoveryTests(unittest.TestCase):
    def test_prompt_includes_page_image_headers_values_and_exact_error(self):
        items = [
            _item(name, 10, 5, 2, 99, total_stock=15) for name in FAILING_PRODUCTS
        ]
        items.append(_item("CONTROL ROW", 10, 5, 2, 13, total_stock=15))
        statement = _prepared(items)
        with patch(
            "services.evidence_selector.select_recovery_evidence",
            return_value=[],
        ), patch(
            "services.gemini_extraction_fallback._document_parts",
            return_value=[
                {"inline_data": {"mime_type": "image/png", "data": b"PAGE-1"}}
            ],
        ):
            payload = build_recovery_payload(
                statement,
                strategy="targeted_stock_extraction",
                file_bytes=b"%PDF-fixture",
                filename="swil-fixture.pdf",
            )
        parts = payload["contents"][0]["parts"]
        text = parts[0]["text"]
        self.assertIn(b"PAGE-1", [part.get("inline_data", {}).get("data") for part in parts])
        self.assertIn("Opening Qty", text)
        self.assertIn("Total Qty is opening + receipt, not sales", text)
        self.assertIn("confidence from 0 to 1", text)
        self.assertIn("brief explanation", text)
        for name in FAILING_PRODUCTS:
            self.assertIn(name, text)
            self.assertIn("closing 99.0 != opening 10.0 + receipt 5.0 - issue 2.0", text)
        self.assertNotIn("CONTROL ROW", text)

    def test_valid_correction_is_applied_only_after_revalidation(self):
        statement = _prepared([_item("HIORA-GA GEL", 10, 5, 2, 99, total_stock=15)])
        calls = {"count": 0}

        def caller(_payload):
            calls["count"] += 1
            return _recovery_json(
                "HIORA-GA GEL",
                13,
                evidence="Closing Qty 13",
                confidence=0.9,
                explanation="Printed closing on the row image.",
            )

        recovered = apply_phase2_recovery(
            statement,
            file_bytes=b"%PDF-fixture",
            filename="swil-fixture.pdf",
            gemini_caller=caller,
        )
        row = recovered["line_items"][0]
        self.assertEqual(calls["count"], 1)
        self.assertEqual(recovered["phase2"]["status"], "recovered")
        self.assertEqual(row["closing_qty"], 13)
        self.assertEqual(row["extra"]["recovery_original"]["closing_qty"], 99)
        self.assertEqual(row["extra"]["evidence"], "Closing Qty 13")
        self.assertEqual(
            recovered["phase2"]["original_values"][0]["closing_qty"],
            99,
        )
        self.assertTrue(recovered["phase2"]["recovery_log"][0]["accepted"])

    def test_invalid_correction_keeps_originals_and_logs_the_attempt(self):
        statement = _prepared([_item("LIV 52", 10, 5, 2, 99, total_stock=15)])

        def caller(_payload):
            return _recovery_json(
                "LIV 52",
                50,
                evidence="Closing Qty 50",
                confidence=0.4,
                explanation="Unverified cell.",
            )

        recovered = apply_phase2_recovery(
            statement,
            file_bytes=b"%PDF-fixture",
            filename="swil-fixture.pdf",
            gemini_caller=caller,
        )
        row = recovered["line_items"][0]
        self.assertEqual(row["closing_qty"], 99)
        self.assertNotIn("recovery_original", row["extra"])
        self.assertEqual(recovered["phase2"]["status"], "unresolved")
        self.assertEqual(recovered["phase2"]["manual_review"], "UNRESOLVED")
        self.assertEqual(recovered["phase2"]["attempts"], 2)
        self.assertEqual(len(recovered["phase2"]["recovery_log"]), 2)
        self.assertFalse(recovered["phase2"]["recovery_log"][0]["accepted"])
        self.assertIn("closing=50", recovered["phase2"]["recovery_log"][0]["returned"][0])

    def test_native_text_pdf_still_enters_recovery(self):
        statement = _prepared([_item("SEPTILIN SYP", 10, 5, 2, 99, total_stock=15)])
        with patch.dict(os.environ, {"PHASE2_RECOVERY_ENABLED": "true"}), patch(
            "services.recovery_runner.apply_phase2_recovery",
            return_value=statement,
        ) as recovery:
            from services.sales_statement_extractor import _maybe_apply_phase2_recovery

            _maybe_apply_phase2_recovery(statement, b"%PDF-fixture", "native-text.pdf")
        recovery.assert_called_once()

    def test_null_response_does_not_overwrite_and_stops_at_retry_limit(self):
        statement = _prepared([_item("LUKOL SY", 10, 5, 2, 99, total_stock=15)])
        calls = {"count": 0}

        def caller(_payload):
            calls["count"] += 1
            return json.dumps(
                {
                    "line_items": [
                        {
                            "fixed": {"product_name": "LUKOL SY"},
                            "dynamic": {
                                "Opening Qty": None,
                                "Receipt Qty": None,
                                "Issue/Sales Qty": None,
                                "Closing Qty": None,
                            },
                        }
                    ]
                }
            )

        recovered = apply_phase2_recovery(
            statement,
            file_bytes=b"%PDF-fixture",
            filename="native-text.pdf",
            gemini_caller=caller,
        )
        self.assertEqual(calls["count"], 2)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 99)
        self.assertEqual(recovered["phase2"]["status"], "unresolved")
        self.assertEqual(recovered["phase2"]["manual_review"], "UNRESOLVED")
        self.assertEqual(recovered["phase2"]["attempts"], 2)
        self.assertTrue(
            all(entry["parsed"] for entry in recovered["phase2"]["recovery_log"])
        )


if __name__ == "__main__":
    unittest.main()
