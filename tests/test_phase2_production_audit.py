"""Production-readiness audit for Phase 2 Gemini recovery.

No network. Gemini is mocked. PHASE2_RECOVERY_ENABLED stays off by default.
"""

from __future__ import annotations

import copy
import json
import os
import unittest
from unittest.mock import patch

from services.hybrid_schema import SalesExtractionOutput
from services.recovery_runner import (
    RecoveryProviderError,
    apply_phase2_recovery,
    merge_stock_recovery,
    phase2_recovery_enabled,
    _adopt_recovered_rows,
    _merge_returned_numbers,
    _parse_recovery_text,
)
from services.sales_statement_extractor import (
    _apply_stock_identity_validation,
    _maybe_apply_phase2_recovery,
    empty_line_item,
)
from services.base_validator import validate_sales_result


def _item(name, opening, receipts, sales, closing, **extra_fields):
    item = empty_line_item()
    item["product_name"] = name
    item["product_code"] = "P-1"
    item["packing"] = "10S"
    item["opening_qty"] = opening
    item["receipts_qty"] = receipts
    item["sales_qty"] = sales
    item["closing_qty"] = closing
    item["extra"] = dict(extra_fields)
    return item


def _statement(items, *, method="pharma_hub_jun_jul_photo", fail_count=0, valid=True):
    statement = _apply_stock_identity_validation(
        {
            "source_file": "audit.jpeg",
            "line_items": items,
            "totals": {
                "extra": {
                    "extraction_method": method,
                    "stock_identity_kind": "opening_receipts_sales_closing",
                    "stock_identity_fail_count": fail_count,
                    "stock_validation": {"is_valid": valid},
                }
            },
        }
    )
    # Recompute fail metadata from the real identity pass.
    fail_count = int(statement["totals"]["extra"].get("stock_identity_fail_count") or 0)
    statement["totals"]["extra"]["stock_validation"] = statement["totals"]["extra"].get(
        "stock_validation"
    ) or {"is_valid": fail_count == 0}
    statement["phase1"] = {
        "route": {"requires_ocr": True, "file_kind": "image"},
        "validation": validate_sales_result(statement),
    }
    return statement


def _sales_json(**rows):
    """Build SalesExtractionOutput JSON from product_name -> qty dicts."""
    items = []
    for name, qtys in rows.items():
        fixed = {
            "product_name": name,
            "opening_qty": qtys.get("opening_qty"),
            "receipts_qty": qtys.get("receipts_qty"),
            "sales_qty": qtys.get("sales_qty"),
            "closing_qty": qtys.get("closing_qty"),
        }
        items.append({"fixed": fixed, "dynamic": {}})
    return json.dumps({"line_items": items, "dynamic": {}})


class Phase2DefaultFlagTests(unittest.TestCase):
    def test_phase2_defaults_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PHASE2_RECOVERY_ENABLED", None)
            self.assertFalse(phase2_recovery_enabled())
        statement = _statement([_item("BAD", 100, 50, 20, 150)])
        with patch.dict(os.environ, {"PHASE2_RECOVERY_ENABLED": "false"}):
            out = _maybe_apply_phase2_recovery(statement, b"x", "audit.jpeg")
        self.assertIs(out, statement)
        self.assertNotIn("phase2", out)


class CorrectPythonNeverOverwrittenTests(unittest.TestCase):
    def test_merge_does_not_rewrite_healthy_row(self):
        statement = _statement(
            [_item("GOOD", 10, 0, 4, 6), _item("BAD", 100, 50, 20, 150)]
        )
        parsed = SalesExtractionOutput.model_validate_json(
            _sales_json(
                GOOD={"opening_qty": 10, "receipts_qty": 0, "sales_qty": 3, "closing_qty": 7},
                BAD={
                    "opening_qty": 100,
                    "receipts_qty": 50,
                    "sales_qty": 20,
                    "closing_qty": 130,
                },
            )
        )
        merged = merge_stock_recovery(statement, parsed)
        by_name = {row["product_name"]: row for row in merged["line_items"]}
        self.assertEqual(by_name["GOOD"]["sales_qty"], 4)
        self.assertEqual(by_name["GOOD"]["closing_qty"], 6)
        self.assertNotIn("field_sources", by_name["GOOD"].get("extra") or {})
        self.assertEqual(by_name["BAD"]["closing_qty"], 130.0)
        self.assertEqual(
            by_name["BAD"]["extra"]["field_sources"]["closing_qty"], "gemini_phase2"
        )
        self.assertEqual(by_name["BAD"]["extra"]["recovery_original"]["closing_qty"], 150)

    def test_same_value_does_not_stamp_gemini_provenance(self):
        statement = _statement([_item("BAD", 100, 50, 20, 150)])
        parsed = SalesExtractionOutput.model_validate_json(
            _sales_json(
                BAD={
                    "opening_qty": 100,
                    "receipts_qty": 50,
                    "sales_qty": 20,
                    "closing_qty": 130,
                }
            )
        )
        merged = merge_stock_recovery(statement, parsed)
        sources = merged["line_items"][0]["extra"]["field_sources"]
        self.assertEqual(sources["opening_qty"], "python")
        self.assertEqual(sources["closing_qty"], "gemini_phase2")
        self.assertNotIn("opening_qty", merged["line_items"][0]["extra"]["recovery_original"])


class ProvenanceAfterProjectionAndAdoptionTests(unittest.TestCase):
    def test_adopt_stamps_gemini_only_on_returned_fields(self):
        original = {
            "line_items": [],
            "totals": {
                "extra": {
                    "di_ocr_override": True,
                    "ocr_table_html": "<table><tr><td>A</td></tr></table>",
                }
            },
        }
        parsed = _parse_recovery_text(
            _sales_json(
                A={
                    "opening_qty": 100,
                    "receipts_qty": None,
                    "sales_qty": 20,
                    "closing_qty": 80,
                }
            )
        )
        adopted = _adopt_recovered_rows(original, parsed)
        row = adopted["line_items"][0]
        self.assertEqual(row["receipts_qty"], None)
        self.assertEqual(row["extra"]["field_sources"]["opening_qty"], "gemini_phase2")
        self.assertNotIn("receipts_qty", row["extra"]["field_sources"])
        self.assertNotIn("recovery_original", row["extra"])
        self.assertTrue(adopted["totals"]["extra"]["di_ocr_override"])

    def test_merge_returned_numbers_does_not_overwrite_when_no_failing_rows(self):
        statement = _statement([_item("OK", 10, 0, 4, 6)], fail_count=0, valid=True)
        parsed = SalesExtractionOutput.model_validate_json(
            _sales_json(
                OK={
                    "opening_qty": 99,
                    "receipts_qty": 0,
                    "sales_qty": 4,
                    "closing_qty": 6,
                }
            )
        )
        merged = _merge_returned_numbers(statement, parsed)
        self.assertEqual(merged["line_items"][0]["opening_qty"], 10)


class PartialRecoveryTests(unittest.TestCase):
    def test_only_failing_product_is_updated_end_to_end(self):
        statement = _statement(
            [_item("GOOD", 10, 0, 4, 6), _item("BAD", 100, 50, 20, 150)],
            method="pharma_hub_jun_jul_photo",
        )
        from services.stock_row_crops import StockRowCrop

        def caller(_payload):
            return json.dumps(
                {
                    "product_name": "BAD",
                    "Opening": 100,
                    "Purchase": 50,
                    "Sales": 20,
                    "Stock-Out": 0,
                    "Closing": 130,
                    # Also tries to damage GOOD if the runner ever sends it.
                    "GOOD_opening": 1,
                }
            )

        crops = [
            StockRowCrop(
                "BAD",
                b"CROP-BAD",
                20,
                10,
                True,
                True,
                header_labels=("Opening", "Purchase", "Sales", "Stock-Out", "Closing"),
            )
        ]
        with patch("services.stock_row_crops.locate_row_crops", return_value=crops):
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=caller,
                file_bytes=b"jpeg",
                filename="audit.jpeg",
            )
        by_name = {row["product_name"]: row for row in recovered["line_items"]}
        self.assertEqual(recovered["phase2"]["status"], "recovered")
        self.assertEqual(by_name["GOOD"]["closing_qty"], 6)
        self.assertEqual(by_name["BAD"]["closing_qty"], 130.0)
        self.assertEqual(
            by_name["BAD"]["extra"]["field_sources"]["closing_qty"], "gemini_phase2"
        )


class FailureSafetyTests(unittest.TestCase):
    def test_malformed_json_preserves_original(self):
        statement = _statement([_item("BAD", 100, 50, 20, 150)])
        before = copy.deepcopy(statement)
        recovered = apply_phase2_recovery(
            statement,
            gemini_caller=lambda _p: "not-json {{{",
            filename="audit.jpeg",
            file_bytes=b"jpeg",
        )
        self.assertEqual(statement, before)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 150)
        self.assertEqual(recovered["phase2"]["status"], "unresolved")
        self.assertGreaterEqual(recovered["phase2"]["attempts"], 1)

    def test_provider_error_preserves_original(self):
        statement = _statement([_item("BAD", 100, 50, 20, 150)])

        def caller(_payload):
            raise RecoveryProviderError("429")

        recovered = apply_phase2_recovery(
            statement,
            gemini_caller=caller,
            filename="audit.jpeg",
            file_bytes=b"jpeg",
        )
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 150)
        self.assertEqual(recovered["phase2"]["failure_type"], "PROVIDER_ERROR")
        self.assertEqual(recovered["phase2"]["status"], "unresolved")

    def test_timeout_like_empty_response_preserves_original(self):
        statement = _statement([_item("BAD", 100, 50, 20, 150)])
        recovered = apply_phase2_recovery(
            statement,
            gemini_caller=lambda _p: "",
            filename="audit.jpeg",
            file_bytes=b"jpeg",
        )
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 150)
        self.assertEqual(recovered["phase2"]["status"], "unresolved")


class StockEquationSafetyTests(unittest.TestCase):
    def test_unbalanced_gemini_candidate_is_rejected(self):
        statement = _statement([_item("BAD", 100, 50, 20, 150)])
        from services.stock_row_crops import StockRowCrop

        crops = [
            StockRowCrop(
                "BAD",
                b"CROP",
                20,
                10,
                True,
                True,
                header_labels=("Opening", "Purchase", "Sales", "Stock-Out", "Closing"),
            )
        ]

        def caller(_payload):
            # Still unbalanced: 100+50-20 != 140
            return json.dumps(
                {
                    "product_name": "BAD",
                    "Opening": 100,
                    "Purchase": 50,
                    "Sales": 20,
                    "Stock-Out": 0,
                    "Closing": 140,
                }
            )

        with patch("services.stock_row_crops.locate_row_crops", return_value=crops):
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=caller,
                file_bytes=b"jpeg",
                filename="audit.jpeg",
            )
        self.assertEqual(recovered["phase2"]["status"], "unresolved")
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 150)
        self.assertFalse(recovered["phase1"]["validation"]["accepted"])


class OfflinePodComparisonTests(unittest.TestCase):
    """Paddle-path vs Phase 2 on representative Zydus-shaped rows.

    Uses offline fixtures (no live OCR/Gemini). Measures identity acceptance
    and field preservation — not live document accuracy.
    """

    def test_paddle_only_keeps_failure_phase2_can_recover_without_touching_good_rows(self):
        # Representative Zydus photo layout: one good row, one equation failure.
        # Clarina balances (47+0-4=43). Pilex printed closing 74 but stock-out
        # was missed (106+100-131=75 without stock-out) → identity failure.
        paddle_only = _statement(
            [
                _item("H CLARINA ANTI ACNE CREAM", 47, 0, 4, 43, stock_out_qty=0),
                _item("H PILEX TABLETS", 106, 100, 131, 74, stock_out_qty=0),
            ],
            method="pharma_hub_jun_jul_photo",
        )
        self.assertFalse(paddle_only["phase1"]["validation"]["accepted"])
        fail_before = int(
            paddle_only["totals"]["extra"].get("stock_identity_fail_count") or 0
        )
        self.assertGreaterEqual(fail_before, 1)

        from services.stock_row_crops import StockRowCrop

        # Phase 2 mocked to return the printed balancing values for Pilex only.
        def caller(payload):
            return json.dumps(
                {
                    "product_name": "H PILEX TABLETS",
                    "Opening": 106,
                    "Purchase": 100,
                    "Sales": 131,
                    "Stock-Out": 1,
                    "Closing": 74,
                }
            )

        crops = [
            StockRowCrop(
                "H PILEX TABLETS",
                b"CROP",
                20,
                10,
                True,
                True,
                header_labels=("Opening", "Purchase", "Sales", "Stock-Out", "Closing"),
            )
        ]
        calls = []

        def counting_caller(payload):
            calls.append(payload)
            return caller(payload)

        with patch("services.stock_row_crops.locate_row_crops", return_value=crops):
            phase2 = apply_phase2_recovery(
                paddle_only,
                gemini_caller=counting_caller,
                file_bytes=b"jpeg",
                filename="0000700077.jpeg",
            )

        # Paddle-only baseline unchanged.
        self.assertEqual(paddle_only["line_items"][0]["closing_qty"], 43)
        self.assertEqual(paddle_only["line_items"][1]["closing_qty"], 74)
        self.assertEqual(paddle_only["line_items"][1]["extra"].get("stock_out_qty"), 0)

        by_name = {row["product_name"]: row for row in phase2["line_items"]}
        self.assertEqual(by_name["H CLARINA ANTI ACNE CREAM"]["closing_qty"], 43)
        self.assertEqual(by_name["H PILEX TABLETS"]["closing_qty"], 74.0)
        self.assertEqual(by_name["H PILEX TABLETS"]["extra"]["stock_out_qty"], 1)
        self.assertEqual(phase2["phase2"]["status"], "recovered")
        self.assertEqual(len(calls), 1)
        # One crop request — cost proxy for targeted recovery.
        self.assertEqual(phase2["phase2"]["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
