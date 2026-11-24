"""Phase 2 sales recovery. Gemini is mocked. No network."""

from __future__ import annotations

import copy
import datetime
import json
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from pydantic import ValidationError

from services.base_validator import validate_sales_result
from services.failure_taxonomy import FailureType, classify_failure
from services.hybrid_schema import (
    HybridSchemaError,
    SalesExtractionOutput,
    SalesLineFixed,
    SalesLineOutput,
    ensure_json_value,
    sales_recovery_json_schema,
)
from services.recovery_policy import recovery_policy, recovery_policy_for_result
from services.recovery_runner import (
    RecoveryProviderError,
    apply_phase2_recovery,
    build_recovery_payload,
    merge_stock_recovery,
    phase2_recovery_enabled,
)
from services.sales_statement_extractor import (
    _apply_stock_identity_validation,
    _maybe_apply_phase2_recovery,
    empty_line_item,
)
from services.vertex_gemini_client import payload_to_generate_config


def _item(name, opening, receipts, sales, closing, **extra_fields):
    item = empty_line_item()
    item["product_name"] = name
    item["product_code"] = "P-1"
    item["packing"] = "10S"
    item["opening_qty"] = opening
    item["receipts_qty"] = receipts
    item["sales_qty"] = sales
    item["closing_qty"] = closing
    item["extra"] = {"batch": "KEEP"}
    item["extra"].update(extra_fields)
    return item


_QTY_MAPPING = {
    "Opening": "opening_qty",
    "Purchase": "receipts_qty",
    "Sales": "sales_qty",
    "Out": "stock_out_qty",
    "Closing": "closing_qty",
}
_QTY_HEADERS = ("Opening", "Purchase", "Sales", "Out", "Closing")
_PHARMA_HEADERS = ("Opening", "Purchase", "Sales", "Stock-Out", "Closing")


def _statement(items, *, method="swil_erp", fail_count=0, valid=True, mapping=None):
    extra = {
        "extraction_method": method,
        "stock_identity_kind": "opening_receipts_sales_closing",
        "stock_identity_fail_count": fail_count,
        "stock_validation": {"is_valid": valid},
    }
    if mapping:
        extra["column_mapping"] = mapping
    statement = {
        "source_file": "statement.xlsx",
        "stockist_name": "ACME PHARMA",
        "line_items": items,
        "totals": {
            "sales_value": None,
            "closing_value": None,
            "extra": extra,
        },
    }
    statement["phase1"] = {
        "route": {"requires_ocr": False, "pdf_kind": "digital"},
        "validation": validate_sales_result(statement),
    }
    return statement


def _bad_stock():
    return _statement(
        [_item("PRODUCT A", 100, 50, 20, 150)],
        fail_count=1,
        valid=False,
    )


def _good_stock():
    return _statement([_item("PRODUCT A", 100, 50, 20, 130)])


def _recovery_json(**fixed):
    body = {
        "line_items": [
            {
                "fixed": {
                    "product_name": "PRODUCT A",
                    "opening_qty": 100,
                    "receipts_qty": 50,
                    "sales_qty": 20,
                    "closing_qty": 130,
                },
                "dynamic": {"scheme": "2+1"},
            }
        ]
    }
    body["line_items"][0]["fixed"].update(fixed)
    return json.dumps(body)


class HybridSchemaTests(unittest.TestCase):
    def test_fixed_valid_null_and_zero_stay_distinct(self):
        row = SalesLineFixed(
            product_name="PRODUCT A",
            opening_qty=100,
            receipts_qty=0,
            sales_qty=None,
            closing_qty="0",
        )
        self.assertEqual(row.opening_qty, 100.0)
        self.assertEqual(row.receipts_qty, 0.0)
        self.assertIsNone(row.sales_qty)
        self.assertEqual(row.closing_qty, 0.0)
        omitted = SalesLineFixed()
        self.assertIsNone(omitted.opening_qty)
        blank = SalesLineFixed(sales_qty="-", closing_qty="")
        self.assertIsNone(blank.sales_qty)
        self.assertIsNone(blank.closing_qty)

    def test_invalid_numeric_is_rejected(self):
        with self.assertRaises((HybridSchemaError, ValidationError)):
            SalesLineFixed(sales_qty="not-a-number")
        with self.assertRaises((HybridSchemaError, ValidationError)):
            SalesLineFixed(opening_qty=True)
        with self.assertRaises((HybridSchemaError, ValidationError)):
            SalesExtractionOutput(totals={"sales_value": "not-a-number"})

    def test_unknown_fixed_field_is_rejected(self):
        with self.assertRaises(ValidationError):
            SalesLineFixed(batch="B1")

    def test_dynamic_json_values(self):
        row = SalesLineOutput(
            fixed={"product_name": "PRODUCT A", "sales_qty": 0},
            dynamic={
                "batch": "B1",
                "free_qty": 2,
                "near_expiry": False,
                "dump": None,
                "territory": ["NORTH", 1],
                "scheme": {"name": "2+1", "active": True, "qty": None},
            },
        )
        self.assertEqual(row.dynamic["batch"], "B1")
        self.assertEqual(row.dynamic["free_qty"], 2)
        self.assertIs(row.dynamic["near_expiry"], False)
        self.assertIsNone(row.dynamic["dump"])
        self.assertEqual(row.dynamic["territory"], ["NORTH", 1])
        self.assertEqual(row.dynamic["scheme"]["name"], "2+1")
        self.assertEqual(row.fixed.sales_qty, 0.0)

    def test_dynamic_rejects_non_json(self):
        for value in (
            datetime.datetime(2026, 1, 1),
            Decimal("1.5"),
            b"bytes",
            {"tags": {"a", "b"}},
            {"when": datetime.date(2026, 1, 1)},
        ):
            with self.assertRaises((HybridSchemaError, ValidationError)):
                ensure_json_value(value)
            with self.assertRaises((HybridSchemaError, ValidationError)):
                SalesLineOutput(fixed={}, dynamic={"bad": value} if not isinstance(value, dict) else value)


class GeminiSchemaTests(unittest.TestCase):
    def test_schema_allows_dynamic_keys(self):
        schema = sales_recovery_json_schema()
        encoded = json.dumps(schema)
        self.assertIn("additionalProperties", encoded)
        self.assertIn("opening_qty", encoded)

    def test_recovery_schema_is_vertex_compatible_shallow_dynamic(self):
        """Regression: deep anyOf under dynamic caused Vertex 400 INVALID_ARGUMENT."""
        schema = sales_recovery_json_schema()
        encoded = json.dumps(schema)
        self.assertLess(len(encoded), 8000)
        self.assertLess(encoded.count("anyOf"), 40)
        for path in (
            schema["properties"]["dynamic"],
            schema["$defs"]["SalesLineOutput"]["properties"]["dynamic"],
        ):
            self.assertEqual(path.get("type"), "object")
            self.assertIs(path.get("additionalProperties"), True)
            self.assertNotIn("anyOf", json.dumps(path))

    def test_phase2_payload_sets_structured_output_only(self):
        payload = build_recovery_payload(
            _bad_stock(),
            strategy="targeted_stock_extraction",
            filename="statement.xlsx",
        )
        config_in = payload["generationConfig"]
        self.assertEqual(config_in["responseMimeType"], "application/json")
        self.assertIn("additionalProperties", json.dumps(config_in["responseJsonSchema"]))
        self.assertNotIn("response_schema", config_in)
        config = payload_to_generate_config(payload)
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertIsNotNone(config.response_json_schema)
        self.assertFalse(getattr(config, "response_schema", None))

    def test_existing_generation_config_stays_free_text(self):
        payload = {"generationConfig": {"temperature": 0, "maxOutputTokens": 8192}}
        config = payload_to_generate_config(payload)
        self.assertEqual(config.temperature, 0)
        self.assertEqual(config.max_output_tokens, 8192)
        self.assertIsNone(config.response_mime_type)
        self.assertIsNone(config.response_json_schema)
        self.assertFalse(getattr(config, "response_schema", None))

    def test_real_recovery_request_structure_includes_ocr_and_failing_rows(self):
        """Mirror the GT-20 stored-OCR Phase 2 request shape (no network)."""
        statement = _bad_stock()
        statement["totals"]["extra"]["di_ocr_override"] = True
        statement["totals"]["extra"]["ocr_text"] = (
            "PRODUCT A  Opening 100  Purchase 50  Sales 20  Closing 130\n"
            "Keep failing-row context visible in OCR evidence."
        )
        statement["phase1"]["validation"] = validate_sales_result(statement)
        payload = build_recovery_payload(
            statement,
            strategy="targeted_stock_extraction",
            file_bytes=b"",
            filename="0000700084_demo.pdf",
            use_row_crops=False,
        )
        parts = payload["contents"][0]["parts"]
        self.assertGreaterEqual(len(parts), 2)
        self.assertTrue(all("text" in part for part in parts))
        joined = "\n".join(part["text"] for part in parts)
        self.assertIn("PRODUCT A", joined)
        self.assertIn("Original OCR evidence", joined)
        self.assertIn("Opening 100", joined)
        gen = payload["generationConfig"]
        self.assertEqual(gen["responseMimeType"], "application/json")
        self.assertEqual(gen["temperature"], 0)
        self.assertEqual(gen["maxOutputTokens"], 8192)
        schema = gen["responseJsonSchema"]
        self.assertEqual(schema.get("title"), "SalesExtractionOutput")
        self.assertIs(schema["properties"]["dynamic"]["additionalProperties"], True)
        config = payload_to_generate_config(payload)
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertIs(config.response_json_schema["properties"]["dynamic"]["additionalProperties"], True)
        self.assertFalse(getattr(config, "response_schema", None))


class FailureTaxonomyTests(unittest.TestCase):
    def _validation(self, reasons, false_checks=(), method="swil_erp"):
        checks = {
            "schema": True,
            "quality": True,
            "stock_identity": True,
            "rows": True,
            "duplicates": True,
        }
        for name in false_checks:
            checks[name] = False
        return {
            "accepted": False,
            "reasons": list(reasons),
            "checks": checks,
            "parser": method,
        }

    def test_every_failure_type(self):
        cases = [
            (FailureType.EMPTY_OUTPUT, ["empty_line_items"], ("rows",), "swil_erp", {}),
            (FailureType.SCHEMA_INVALID, ["schema_invalid"], ("schema",), "swil_erp", {}),
            (
                FailureType.STOCK_IDENTITY_FAILURE,
                ["stock_identity_failure"],
                ("stock_identity",),
                "swil_erp",
                {},
            ),
            (FailureType.DUPLICATE_ROWS, ["duplicate_rows"], ("duplicates",), "swil_erp", {}),
            (FailureType.MISSING_FIELDS, ["missing_product_names"], (), "swil_erp", {}),
            (
                FailureType.LOW_TEXT_EVIDENCE,
                ["low_ocr_quality_score"],
                (),
                "swil_erp",
                {"route": {"pdf_kind": "digital"}},
            ),
            (
                FailureType.OCR_REQUIRED,
                ["needs_ocr"],
                (),
                "swil_erp",
                {"route": {"requires_ocr": True}},
            ),
            (FailureType.GENERIC_PARSER_FAILURE, ["high_ocr_noise"], (), "", {}),
            (
                FailureType.LLM_EXTRACTION_FAILURE,
                ["extraction_failed"],
                (),
                "gemini_extraction_fallback",
                {},
            ),
        ]
        for expected, reasons, false_checks, method, extra in cases:
            found = classify_failure(
                self._validation(reasons, false_checks, method),
                extraction_method=method,
                **extra,
            )
            self.assertEqual(found, expected, expected.value)

    def test_provider_error_and_accepted(self):
        self.assertEqual(
            classify_failure({"accepted": True}, provider_error="429"),
            FailureType.PROVIDER_ERROR,
        )
        self.assertIsNone(classify_failure({"accepted": True, "reasons": []}))

    def test_real_stock_identity_validation(self):
        statement = _bad_stock()
        failure = classify_failure(
            statement["phase1"]["validation"],
            extraction_method="swil_erp",
        )
        self.assertEqual(failure, FailureType.STOCK_IDENTITY_FAILURE)
        self.assertFalse(statement["phase1"]["validation"]["accepted"])
        self.assertEqual(
            statement["phase1"]["validation"]["failure_type"],
            "STOCK_IDENTITY_FAILURE",
        )


class RecoveryPolicyTests(unittest.TestCase):
    def test_every_policy(self):
        expected = {
            FailureType.PROVIDER_ERROR: ("provider_retry", 0, False, True),
            FailureType.EMPTY_OUTPUT: ("structured_reread", 2, True, False),
            FailureType.SCHEMA_INVALID: ("structured_output_retry", 2, True, False),
            FailureType.MISSING_FIELDS: ("targeted_fields", 2, True, False),
            FailureType.STOCK_IDENTITY_FAILURE: ("targeted_stock_extraction", 2, True, False),
            FailureType.DUPLICATE_ROWS: ("structured_reread", 1, True, False),
            FailureType.LOW_TEXT_EVIDENCE: ("image_evidence", 1, False, False),
            # OCR recovery (Tess→Gemini transcription) runs upstream; Phase 2
            # may still call Gemini once for image evidence when OCR is empty.
            FailureType.OCR_REQUIRED: ("ocr_path", 1, True, False),
            FailureType.GENERIC_PARSER_FAILURE: ("existing_gemini_fallback", 1, False, False),
            FailureType.LLM_EXTRACTION_FAILURE: ("targeted_recovery", 2, True, False),
        }
        for failure, (strategy, attempts, call_gemini, provider_retry) in expected.items():
            policy = recovery_policy(failure)
            self.assertEqual(policy.strategy, strategy)
            self.assertEqual(policy.max_attempts, attempts)
            self.assertEqual(policy.call_gemini, call_gemini)
            self.assertEqual(policy.provider_retry, provider_retry)

    def test_generic_parser_with_di_ocr_upgrades_to_targeted_fields(self):
        bare = recovery_policy_for_result(FailureType.GENERIC_PARSER_FAILURE, {})
        self.assertEqual(bare.strategy, "existing_gemini_fallback")
        self.assertFalse(bare.call_gemini)
        with_ocr = recovery_policy_for_result(
            FailureType.GENERIC_PARSER_FAILURE,
            {
                "totals": {
                    "extra": {
                        "di_ocr_override": True,
                        "ocr_table_html": "<table><tr><td>A</td></tr></table>",
                    }
                }
            },
        )
        self.assertEqual(with_ocr.strategy, "targeted_fields")
        self.assertTrue(with_ocr.call_gemini)
        self.assertEqual(with_ocr.max_attempts, 2)


class RecoveryRunnerTests(unittest.TestCase):
    def test_accepted_makes_zero_recovery_calls(self):
        statement = _good_stock()
        self.assertTrue(statement["phase1"]["validation"]["accepted"])
        calls = []

        def caller(_payload):
            calls.append(1)
            return _recovery_json()

        before = copy.deepcopy(statement)
        recovered = apply_phase2_recovery(statement, gemini_caller=caller, filename="ok.xlsx")
        self.assertEqual(calls, [])
        self.assertEqual(statement, before)
        self.assertEqual(recovered["phase2"]["status"], "not_needed")
        self.assertEqual(recovered["phase2"]["attempts"], 0)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 130)

    def test_stock_prompt_is_targeted(self):
        good = _item("PRODUCT B", 10, 0, 4, 6)
        bad = _item("PRODUCT A", 100, 50, 20, 150)
        statement = _statement([good, bad], fail_count=1, valid=False)
        payload = build_recovery_payload(
            statement,
            strategy="targeted_stock_extraction",
            filename="statement.xlsx",
        )
        text = payload["contents"][0]["parts"][0]["text"]
        self.assertIn("PRODUCT A", text)
        self.assertNotIn("PRODUCT B", text)
        self.assertIn("Do not calculate a missing quantity.", text)
        self.assertIn("Do not use a stock identity equation", text)
        self.assertIn("Fill opening_qty, receipts_qty, sales_qty, and closing_qty", text)
        self.assertIn("ONLY for the product rows listed below", text)
        self.assertNotIn("Leave canonical quantity fields null", text)
        self.assertNotIn("Op -> opening_qty", text)
        self.assertNotIn("Bal -> closing_qty", text)

    def test_row_crops_are_sent_only_for_failing_rows(self):
        good = _item("PRODUCT B", 10, 0, 4, 6)
        bad = _item("PRODUCT A", 100, 50, 20, 150)
        statement = _statement([good, bad], fail_count=1, valid=False)
        from services.stock_row_crops import StockRowCrop

        seen = {}

        def locate(_file_bytes, _filename, rows):
            seen["rows"] = rows
            return [StockRowCrop("PRODUCT A", b"CROP-A", 12, 8, True, False)]

        with patch(
            "services.stock_row_crops.locate_row_crops",
            side_effect=locate,
        ), patch(
            "services.gemini_extraction_fallback._document_parts",
        ) as full:
            payload = build_recovery_payload(
                statement,
                strategy="targeted_stock_extraction",
                file_bytes=b"jpeg-bytes",
                filename="photo.jpeg",
            )
        self.assertEqual([row["product_name"] for row in seen["rows"]], ["PRODUCT A"])
        full.assert_not_called()
        parts = payload["contents"][0]["parts"]
        self.assertEqual(parts[1]["inline_data"]["data"], b"CROP-A")
        self.assertIn("PRODUCT A", parts[0]["text"])
        self.assertNotIn("PRODUCT B", parts[0]["text"])

    def test_each_failing_crop_is_requested_separately(self):
        first = _item("PRODUCT A", 100, 50, 20, 150)
        second = _item("PRODUCT B", 10, 0, 1, 20)
        statement = _statement(
            [first, second],
            method="pharma_hub_jun_jul_photo",
            fail_count=2,
            valid=False,
        )
        from services.focused_row_request import SEMANTIC_SCHEMA
        from services.stock_row_crops import StockRowCrop

        crops = [
            StockRowCrop("PRODUCT A", b"CROP-A", 20, 10, True, True, header_labels=_PHARMA_HEADERS),
            StockRowCrop("PRODUCT B", b"CROP-B", 20, 10, True, True, header_labels=_PHARMA_HEADERS),
        ]
        seen = []

        def caller(payload):
            parts = payload["contents"][0]["parts"]
            image = parts[1]["inline_data"]["data"]
            seen.append((parts[0]["text"], image, payload["generationConfig"]))
            if image == b"CROP-A":
                body = {
                    "product_name": "PRODUCT A",
                    "Opening": 100,
                    "Purchase": 50,
                    "Sales": 20,
                    "Stock-Out": 0,
                    "Closing": 130,
                    "closing_qty": 43,
                }
            else:
                body = {
                    "product_name": "PRODUCT B",
                    "Purchase": 0,
                    "Sales": 1,
                    "Closing": 9,
                    "closing_qty": 43,
                }
            return json.dumps(body)

        with patch(
            "services.stock_row_crops.locate_row_crops",
            return_value=crops,
        ):
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=caller,
                file_bytes=b"jpeg-bytes",
                filename="photo.jpeg",
            )
        self.assertEqual(len(seen), 2)
        self.assertIn("Do not calculate a missing quantity.", seen[0][0])
        self.assertIn("Target row:\nPRODUCT A", seen[0][0])
        self.assertIn("Target row:\nPRODUCT B", seen[1][0])
        self.assertNotIn("PRODUCT B", seen[0][0])
        self.assertEqual(seen[0][1], b"CROP-A")
        self.assertEqual(seen[1][1], b"CROP-B")
        self.assertEqual(seen[0][2]["responseJsonSchema"], SEMANTIC_SCHEMA)
        self.assertEqual(seen[0][2]["maxOutputTokens"], 1024)
        self.assertEqual(seen[0][2]["responseMimeType"], "application/json")
        self.assertEqual(recovered["phase2"]["status"], "recovered")
        self.assertEqual(recovered["phase2"]["attempts"], 1)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 130.0)
        self.assertEqual(recovered["line_items"][1]["closing_qty"], 9.0)
        self.assertEqual(recovered["line_items"][1]["opening_qty"], 10)
        self.assertNotIn("stock_out_qty", recovered["line_items"][1]["extra"])
        self.assertEqual(recovered["line_items"][0]["extra"]["stock_out_qty"], 0)

    def test_crop_failure_keeps_full_document_evidence(self):
        sentinel = [{"inline_data": {"mime_type": "image/jpeg", "data": "FULL"}}]
        with patch(
            "services.gemini_extraction_fallback._document_parts",
            return_value=sentinel,
        ) as full:
            payload = build_recovery_payload(
                _bad_stock(),
                strategy="targeted_stock_extraction",
                file_bytes=b"not-an-image",
                filename="photo.jpeg",
            )
        full.assert_called_once()
        self.assertEqual(payload["contents"][0]["parts"][1], sentinel[0])

    def test_pdf_does_not_guess_a_row_crop(self):
        sentinel = [{"inline_data": {"mime_type": "image/png", "data": "PAGE"}}]
        with patch(
            "services.gemini_extraction_fallback._document_parts",
            return_value=sentinel,
        ) as full:
            payload = build_recovery_payload(
                _bad_stock(),
                strategy="targeted_stock_extraction",
                file_bytes=b"%PDF-1.4",
                filename="statement.pdf",
            )
        full.assert_called_once()
        self.assertEqual(payload["contents"][0]["parts"][1], sentinel[0])

    def test_merge_updates_only_stock_quantities(self):
        original = _bad_stock()
        parsed = SalesExtractionOutput.model_validate_json(
            _recovery_json(product_name="PRODUCT A", packing="SHOULD-NOT-APPLY", product_code="NOPE")
        )
        merged = merge_stock_recovery(original, parsed)
        row = merged["line_items"][0]
        self.assertEqual(row["product_name"], "PRODUCT A")
        self.assertEqual(row["product_code"], "P-1")
        self.assertEqual(row["packing"], "10S")
        self.assertEqual(row["opening_qty"], 100.0)
        self.assertEqual(row["receipts_qty"], 50.0)
        self.assertEqual(row["sales_qty"], 20.0)
        self.assertEqual(row["closing_qty"], 130.0)
        self.assertEqual(row["extra"]["batch"], "KEEP")
        self.assertEqual(row["extra"]["scheme"], "2+1")
        self.assertTrue(merged["totals"]["extra"]["stock_validation"]["is_valid"])
        self.assertEqual(original["line_items"][0]["closing_qty"], 150)

    def test_null_recovery_does_not_replace_existing_quantity(self):
        original = _bad_stock()
        parsed = SalesExtractionOutput.model_validate_json(
            _recovery_json(sales_qty=None, closing_qty=130)
        )
        merged = merge_stock_recovery(original, parsed)
        self.assertEqual(merged["line_items"][0]["sales_qty"], 20)
        self.assertEqual(merged["line_items"][0]["closing_qty"], 130.0)

    def test_recovery_success_replaces_only_when_validation_passes(self):
        statement = _bad_stock()
        before = copy.deepcopy(statement)

        def caller(_payload):
            return _recovery_json()

        recovered = apply_phase2_recovery(
            statement,
            gemini_caller=caller,
            filename="statement.xlsx",
        )
        # Without a printed-header mapping, bare canonicals are not promoted.
        self.assertEqual(statement, before)
        self.assertEqual(statement["line_items"][0]["closing_qty"], 150)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 150)
        self.assertEqual(recovered["line_items"][0]["product_name"], "PRODUCT A")
        self.assertFalse(recovered["phase1"]["validation"]["accepted"])
        self.assertEqual(recovered["phase2"]["status"], "unresolved")
        self.assertEqual(recovered["phase2"]["failure_type"], "STOCK_IDENTITY_FAILURE")
        self.assertEqual(recovered["phase2"]["attempts"], 2)
        self.assertNotIn("extraction_failed", recovered["totals"]["extra"])

    def test_failed_recovery_keeps_original(self):
        statement = _bad_stock()
        calls = []

        def caller(_payload):
            calls.append(1)
            if len(calls) > 2:
                raise AssertionError("attempt 3")
            return _recovery_json(closing_qty=150)

        recovered = apply_phase2_recovery(
            statement,
            gemini_caller=caller,
            filename="statement.xlsx",
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 150)
        self.assertEqual(recovered["line_items"][0]["product_name"], "PRODUCT A")
        self.assertEqual(recovered["phase2"]["status"], "unresolved")
        self.assertEqual(recovered["phase2"]["attempts"], 2)
        self.assertNotIn("extraction_failed", recovered["totals"]["extra"])
        self.assertFalse(recovered["phase1"]["validation"]["accepted"])

    def test_provider_error_does_not_start_extraction_recovery(self):
        statement = _bad_stock()
        calls = []

        def caller(_payload):
            calls.append(1)
            return _recovery_json()

        recovered = apply_phase2_recovery(
            statement,
            gemini_caller=caller,
            filename="statement.xlsx",
            provider_error="429",
        )
        self.assertEqual(calls, [])
        self.assertEqual(recovered["phase2"]["status"], "unresolved")
        self.assertEqual(recovered["phase2"]["failure_type"], "PROVIDER_ERROR")
        self.assertEqual(recovered["phase2"]["strategy"], "provider_retry")
        self.assertEqual(recovered["phase2"]["attempts"], 0)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 150)

    def test_provider_error_during_call_does_not_retry_recovery(self):
        statement = _bad_stock()
        calls = []

        def caller(_payload):
            calls.append(1)
            raise RecoveryProviderError("429")

        recovered = apply_phase2_recovery(
            statement,
            gemini_caller=caller,
            filename="statement.xlsx",
        )
        self.assertEqual(calls, [1])
        self.assertEqual(recovered["phase2"]["failure_type"], "PROVIDER_ERROR")
        self.assertEqual(recovered["phase2"]["attempts"], 0)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 150)

    def test_non_gemini_policies_do_not_call(self):
        for reasons, route, method in (
            (["low_ocr_quality_score"], {"requires_ocr": False, "pdf_kind": "scanned"}, "swil_erp"),
            (["high_ocr_noise"], {"requires_ocr": False}, ""),
        ):
            statement = _good_stock()
            statement["phase1"]["route"] = route
            statement["phase1"]["validation"] = {
                "accepted": False,
                "reasons": reasons,
                "checks": {
                    "schema": True,
                    "quality": False,
                    "stock_identity": True,
                    "rows": True,
                    "duplicates": True,
                },
                "parser": method,
            }
            statement["totals"]["extra"]["extraction_method"] = method
            calls = []
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=lambda _payload: calls.append(1),
                filename="statement.xlsx",
            )
            self.assertEqual(calls, [], reasons)
            self.assertEqual(recovered["phase2"]["status"], "unresolved")
            self.assertEqual(recovered["phase2"]["attempts"], 0)
            self.assertEqual(recovered["line_items"][0]["closing_qty"], 130)

    def test_ocr_required_policy_allows_one_attempt(self):
        statement = _good_stock()
        statement["phase1"]["route"] = {"requires_ocr": True}
        statement["phase1"]["validation"] = {
            "accepted": False,
            "reasons": ["needs_ocr", "empty_ocr_page"],
            "checks": {
                "schema": True,
                "quality": False,
                "stock_identity": True,
                "rows": True,
                "duplicates": True,
            },
            "parser": "swil_erp",
        }
        statement["totals"]["extra"]["extraction_method"] = "swil_erp"
        calls = []
        recovered = apply_phase2_recovery(
            statement,
            gemini_caller=lambda _payload: calls.append(1) or "{}",
            filename="scan.jpg",
        )
        self.assertEqual(recovered["phase2"]["failure_type"], "OCR_REQUIRED")
        self.assertGreaterEqual(recovered["phase2"]["attempts"], 1)
        self.assertEqual(len(calls), recovered["phase2"]["attempts"])

    def test_flag_defaults_false_and_extractor_skips_recovery(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PHASE2_RECOVERY_ENABLED", None)
            self.assertFalse(phase2_recovery_enabled())
        statement = _bad_stock()
        with patch.dict(os.environ, {"PHASE2_RECOVERY_ENABLED": "false"}):
            same = _maybe_apply_phase2_recovery(statement, b"", "statement.xlsx")
        self.assertIs(same, statement)
        self.assertNotIn("phase2", same)
        with patch.dict(os.environ, {"PHASE2_RECOVERY_ENABLED": "true"}):
            with patch(
                "services.recovery_runner.apply_phase2_recovery",
                return_value={"phase2": {"status": "recovered"}},
            ) as mocked:
                out = _maybe_apply_phase2_recovery(statement, b"abc", "statement.pdf")
        self.assertEqual(out["phase2"]["status"], "recovered")
        mocked.assert_called_once()
        with patch.dict(os.environ, {"PHASE2_RECOVERY_ENABLED": "true"}):
            with patch(
                "services.recovery_runner.apply_phase2_recovery",
            ) as excel_recovery:
                excel = _maybe_apply_phase2_recovery(statement, b"abc", "statement.xlsx")
        excel_recovery.assert_not_called()
        self.assertIs(excel, statement)

    def test_identity_formula_used_by_recovery(self):
        item = _item("PRODUCT A", 100, 50, 20, 150)
        self.assertFalse(
            abs((100 + 50 - 20) - 150) < 0.05
        )
        statement = _apply_stock_identity_validation(
            {
                "line_items": [item],
                "totals": {
                    "extra": {
                        "extraction_method": "swil_erp",
                        "stock_identity_kind": "opening_receipts_sales_closing",
                    }
                },
            }
        )
        self.assertFalse(statement["totals"]["extra"]["stock_validation"]["is_valid"])
        self.assertEqual(statement["totals"]["extra"]["stock_identity_fail_count"], 1)

    def test_pilex_stock_out_is_part_of_the_shared_identity(self):
        """106 + 100 - 131 - 1 = 74. Stock-out is outgoing, not ignored."""
        clarina = _item("H CLARINA ANTI ACNE CREAM", 47, 0, 4, 45, stock_out_qty=0)
        kapikachhu = _item("H KAPIKACHHU TABLETS", 40, 0, 1, 40, stock_out_qty=0)
        manjishtha = _item("H MANJISHTHA TAB", 48, 0, 8, 39, stock_out_qty=1)
        pilex = _item("H PILEX TABLETS", 106, 100, 131, 74, stock_out_qty=1)
        scheme = _item("SCHEME ROW", 10, 0, 2, 7, sales_scheme_qty=1)
        checked = _apply_stock_identity_validation(
            {
                "line_items": [clarina, kapikachhu, manjishtha, pilex, scheme],
                "totals": {
                    "extra": {
                        "extraction_method": "pharma_hub_jun_jul_photo",
                        "stock_identity_kind": "opening_receipts_sales_closing",
                    }
                },
            }
        )
        by_name = {item["product_name"]: item for item in checked["line_items"]}
        self.assertEqual(by_name["H CLARINA ANTI ACNE CREAM"]["extra"]["expected_closing"], 43.0)
        self.assertFalse(by_name["H CLARINA ANTI ACNE CREAM"]["extra"]["stock_identity_ok"])
        self.assertEqual(by_name["H KAPIKACHHU TABLETS"]["extra"]["expected_closing"], 39.0)
        self.assertFalse(by_name["H KAPIKACHHU TABLETS"]["extra"]["stock_identity_ok"])
        self.assertEqual(by_name["H MANJISHTHA TAB"]["extra"]["expected_closing"], 39.0)
        self.assertTrue(by_name["H MANJISHTHA TAB"]["extra"]["stock_identity_ok"])
        self.assertEqual(by_name["SCHEME ROW"]["extra"]["expected_closing"], 7.0)
        self.assertTrue(by_name["SCHEME ROW"]["extra"]["stock_identity_ok"])
        row = by_name["H PILEX TABLETS"]
        self.assertEqual(row["opening_qty"], 106.0)
        self.assertEqual(row["receipts_qty"], 100.0)
        self.assertEqual(row["sales_qty"], 131.0)
        self.assertEqual(row["extra"]["stock_out_qty"], 1)
        self.assertEqual(row["closing_qty"], 74.0)
        self.assertEqual(row["extra"]["expected_closing"], 74.0)
        self.assertTrue(row["extra"]["stock_identity_ok"])
        self.assertEqual(checked["totals"]["extra"]["stock_identity_fail_count"], 2)
        self.assertFalse(checked["totals"]["extra"]["stock_validation"]["is_valid"])

    def test_pilex_semantic_candidate_resolves_when_stock_out_balances(self):
        pilex = _item("H PILEX TABLETS", 106, 100, 131, 74)
        statement = _statement(
            [pilex],
            method="pharma_hub_jun_jul_photo",
            fail_count=1,
            valid=False,
        )
        from services.stock_row_crops import StockRowCrop

        crops = [StockRowCrop("H PILEX TABLETS", b"CROP-PILEX", 2656, 204, True, True, header_labels=_PHARMA_HEADERS)]

        def caller(payload):
            schema = (payload.get("generationConfig") or {}).get("responseJsonSchema") or {}
            self.assertEqual(schema.get("title"), "SemanticRow")
            return json.dumps(
                {
                    "product_name": "H PILEX TABLETS",
                    "Opening": 106,
                    "Purchase": 100,
                    "Sales": 131,
                    "Stock-Out": 1,
                    "Closing": 74,
                    "closing_qty": 43,
                }
            )

        with patch(
            "services.stock_row_crops.locate_row_crops",
            return_value=crops,
        ):
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=caller,
                file_bytes=b"jpeg-bytes",
                filename="0000700077.jpeg",
            )
        row = recovered["line_items"][0]
        self.assertEqual(recovered["phase2"]["status"], "recovered")
        self.assertEqual(recovered["phase2"]["attempts"], 1)
        self.assertEqual(row["opening_qty"], 106.0)
        self.assertEqual(row["receipts_qty"], 100.0)
        self.assertEqual(row["sales_qty"], 131.0)
        self.assertEqual(row["extra"]["stock_out_qty"], 1)
        self.assertEqual(row["closing_qty"], 74.0)
        self.assertEqual(row["extra"]["expected_closing"], 74.0)
        self.assertTrue(row["extra"]["stock_identity_ok"])
        self.assertTrue(recovered["totals"]["extra"]["stock_validation"]["is_valid"])
        self.assertTrue(recovered["phase1"]["validation"]["accepted"])
        self.assertTrue(recovered["phase1"]["validation"]["checks"]["stock_identity"])
        self.assertNotIn("extraction_failed", recovered["totals"]["extra"])

    def test_unbalanced_pilex_candidate_is_still_rejected(self):
        pilex = _item("H PILEX TABLETS", 106, 100, 131, 74, stock_out_qty=0)
        statement = _statement([pilex], fail_count=1, valid=False, mapping=_QTY_MAPPING)
        from services.stock_row_crops import StockRowCrop

        crops = [StockRowCrop("H PILEX TABLETS", b"CROP-PILEX", 2656, 204, True, True, header_labels=_QTY_HEADERS)]

        def caller(_payload):
            return json.dumps(
                {
                    "product_name": "H PILEX TABLETS",
                    "opening_qty": 106,
                    "receipts_qty": 100,
                    "sales_qty": 131,
                    "stock_out_qty": 0,
                    "closing_qty": 74,
                }
            )

        with patch(
            "services.stock_row_crops.locate_row_crops",
            return_value=crops,
        ):
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=caller,
                file_bytes=b"jpeg-bytes",
                filename="0000700077.jpeg",
            )
        self.assertEqual(recovered["phase2"]["status"], "unresolved")
        self.assertEqual(recovered["phase2"]["attempts"], 2)
        self.assertEqual(recovered["line_items"][0]["closing_qty"], 74)
        self.assertEqual(recovered["line_items"][0]["extra"].get("stock_out_qty"), 0)
        self.assertFalse(recovered["phase1"]["validation"]["accepted"])
        self.assertNotIn("extraction_failed", recovered["totals"]["extra"])

    def test_null_stock_out_does_not_clear_a_balancing_value(self):
        pilex = _item("H PILEX TABLETS", 106, 100, 131, 75, stock_out_qty=1)
        statement = _statement(
            [pilex],
            method="pharma_hub_jun_jul_photo",
            fail_count=1,
            valid=False,
        )
        from services.stock_row_crops import StockRowCrop

        crops = [StockRowCrop("H PILEX TABLETS", b"CROP-PILEX", 2656, 204, True, True, header_labels=_PHARMA_HEADERS)]

        def caller(_payload):
            return json.dumps(
                {
                    "product_name": "H PILEX TABLETS",
                    "Closing": 74,
                    "closing_qty": 43,
                    "stock_out_qty": None,
                }
            )

        with patch(
            "services.stock_row_crops.locate_row_crops",
            return_value=crops,
        ):
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=caller,
                file_bytes=b"jpeg-bytes",
                filename="0000700077.jpeg",
            )
        row = recovered["line_items"][0]
        self.assertEqual(recovered["phase2"]["status"], "recovered")
        self.assertEqual(row["extra"]["stock_out_qty"], 1)
        self.assertEqual(row["closing_qty"], 74.0)
        self.assertTrue(row["extra"]["stock_identity_ok"])

    def test_merge_stamps_field_sources_for_corrected_quantities(self):
        original = _bad_stock()
        parsed = SalesExtractionOutput.model_validate_json(
            _recovery_json(product_name="PRODUCT A", closing_qty=130)
        )
        merged = merge_stock_recovery(original, parsed)
        row = merged["line_items"][0]
        sources = row["extra"]["field_sources"]
        self.assertEqual(sources["closing_qty"], "gemini_phase2")
        self.assertEqual(sources["opening_qty"], "python")
        self.assertEqual(sources["sales_qty"], "python")
        self.assertEqual(row["extra"]["recovery_original"]["closing_qty"], 150)
        self.assertEqual(row["closing_qty"], 130.0)
        self.assertEqual(row["opening_qty"], 100)

    def test_ocr_success_parser_empty_uses_ocr_evidence_not_full_document(self):
        """OCR returned table HTML; Python produced no rows → targeted Phase 2."""
        html = (
            "<table><tr><th>Product</th><th>Open</th><th>Sale</th><th>Close</th></tr>"
            "<tr><td>PRODUCT A</td><td>100</td><td>20</td><td>80</td></tr></table>"
        )
        statement = {
            "source_file": "scan.jpg",
            "line_items": [],
            "totals": {
                "extra": {
                    "extraction_method": "vl_html_table_parse",
                    "di_ocr_override": True,
                    "ocr_table_html": html,
                    "ocr_source": "paddleocr-vl",
                }
            },
            "phase1": {
                "route": {"requires_ocr": True, "pdf_kind": "scanned", "file_kind": "image"},
                "validation": {
                    "accepted": False,
                    "reasons": ["empty_line_items"],
                    "checks": {
                        "schema": True,
                        "quality": False,
                        "stock_identity": True,
                        "rows": False,
                        "duplicates": True,
                        "cell_validation": True,
                        "column_mapping": True,
                        "ocr_verified": True,
                    },
                    "parser": "vl_html_table_parse",
                },
            },
        }
        seen = []

        def caller(payload):
            seen.append(payload)
            # Only fields visible in the OCR HTML. Do not invent receipts=0.
            return _recovery_json(
                product_name="PRODUCT A",
                opening_qty=100,
                receipts_qty=None,
                sales_qty=20,
                closing_qty=80,
            )

        with patch(
            "services.gemini_extraction_fallback._document_parts",
        ) as full_doc:
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=caller,
                file_bytes=b"jpeg-bytes",
                filename="scan.jpg",
            )
        full_doc.assert_not_called()
        self.assertTrue(seen)
        parts = seen[0]["contents"][0]["parts"]
        joined = "\n".join(
            str(part.get("text") or "") for part in parts if isinstance(part, dict)
        )
        self.assertIn("Original OCR evidence", joined)
        self.assertIn(html, joined)
        self.assertIn("empty_line_items", joined)
        self.assertFalse(
            any(isinstance(part, dict) and "inline_data" in part for part in parts)
        )
        self.assertEqual(recovered["phase2"]["status"], "recovered")
        self.assertEqual(recovered["phase2"]["failure_type"], "EMPTY_OUTPUT")
        self.assertEqual(len(recovered["line_items"]), 1)
        row = recovered["line_items"][0]
        self.assertEqual(row["product_name"], "PRODUCT A")
        self.assertEqual(row["closing_qty"], 80.0)
        self.assertEqual(row["extra"]["field_sources"]["closing_qty"], "gemini_phase2")
        self.assertTrue(recovered["totals"]["extra"]["di_ocr_override"])
        self.assertEqual(recovered["totals"]["extra"]["ocr_table_html"], html)

    def test_ocr_success_generic_parser_failure_triggers_targeted_gemini(self):
        """DI OCR present + weak parse → Phase 2 targeted_fields, not full vision skip."""
        good = _item("PRODUCT B", 10, 0, 4, 6)
        bad = _item("PRODUCT A", 100, 50, 20, 150)
        statement = _statement([good, bad], method="generic_ocr", fail_count=1, valid=False)
        statement["totals"]["extra"]["di_ocr_override"] = True
        statement["totals"]["extra"]["ocr_table_html"] = (
            "<table><tr><td>PRODUCT A</td><td>100</td><td>50</td><td>20</td><td>130</td></tr></table>"
        )
        # Force generic parser failure classification path when identity is also wrong:
        # stock identity still wins in classify_failure. Use quality-only failure.
        weak = _statement(
            [_item("PRODUCT A", 100, 50, 20, 130)],
            method="",
            fail_count=0,
            valid=True,
        )
        weak["totals"]["extra"]["di_ocr_override"] = True
        weak["totals"]["extra"]["ocr_table_html"] = "<table><tr><td>PRODUCT A</td></tr></table>"
        weak["phase1"]["validation"] = {
            "accepted": False,
            "reasons": ["high_ocr_noise"],
            "checks": {
                "schema": True,
                "quality": False,
                "stock_identity": True,
                "rows": True,
                "duplicates": True,
                "cell_validation": True,
                "column_mapping": True,
                "ocr_verified": True,
            },
            "parser": "",
        }
        calls = []

        def caller(payload):
            calls.append(payload)
            return _recovery_json(closing_qty=130)

        with patch(
            "services.gemini_extraction_fallback._document_parts",
        ) as full_doc:
            recovered = apply_phase2_recovery(
                weak,
                gemini_caller=caller,
                file_bytes=b"jpeg-bytes",
                filename="scan.jpg",
            )
        full_doc.assert_not_called()
        self.assertGreaterEqual(len(calls), 1)
        self.assertEqual(recovered["phase2"]["strategy"], "targeted_fields")
        self.assertEqual(recovered["phase2"]["failure_type"], "GENERIC_PARSER_FAILURE")
        prompt = calls[0]["contents"][0]["parts"][0]["text"]
        self.assertIn("high_ocr_noise", prompt)
        self.assertIn("Validation faults to focus on", prompt)

    def test_stock_equation_recovery_preserves_good_row_and_provenance(self):
        good = _item("PRODUCT B", 10, 0, 4, 6)
        bad = _item("PRODUCT A", 100, 50, 20, 150)
        statement = _statement(
            [good, bad],
            method="pharma_hub_jun_jul_photo",
            fail_count=1,
            valid=False,
        )
        from services.stock_row_crops import StockRowCrop

        crops = [
            StockRowCrop(
                "PRODUCT A",
                b"CROP-A",
                20,
                10,
                True,
                True,
                header_labels=_PHARMA_HEADERS,
            )
        ]

        def caller(payload):
            return json.dumps(
                {
                    "product_name": "PRODUCT A",
                    "Opening": 100,
                    "Purchase": 50,
                    "Sales": 20,
                    "Stock-Out": 0,
                    "Closing": 130,
                }
            )

        with patch(
            "services.stock_row_crops.locate_row_crops",
            return_value=crops,
        ):
            recovered = apply_phase2_recovery(
                statement,
                gemini_caller=caller,
                file_bytes=b"jpeg-bytes",
                filename="photo.jpeg",
            )
        self.assertEqual(recovered["phase2"]["status"], "recovered")
        self.assertEqual(recovered["phase2"]["failure_type"], "STOCK_IDENTITY_FAILURE")
        by_name = {row["product_name"]: row for row in recovered["line_items"]}
        self.assertEqual(by_name["PRODUCT B"]["closing_qty"], 6)
        self.assertNotIn("field_sources", by_name["PRODUCT B"].get("extra") or {})
        self.assertEqual(by_name["PRODUCT A"]["closing_qty"], 130.0)
        self.assertEqual(
            by_name["PRODUCT A"]["extra"]["field_sources"]["closing_qty"],
            "gemini_phase2",
        )
        self.assertEqual(
            by_name["PRODUCT A"]["extra"]["field_sources"]["opening_qty"],
            "python",
        )


if __name__ == "__main__":
    unittest.main()
