"""Original-image Gemini fallback when structured stock OCR is unreliable."""

from __future__ import annotations

import base64
import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.sales_statement_extractor import empty_result, extract_sales_statement
from services.stock_image_vision import (
    STOCK_IMAGE_VISION_PROMPT,
    apply_direct_stock_image_gemini,
    assess_stock_structured_quality,
    normalize_stock_gemini_extraction,
)

ZL_IMAGE = Path(
    "/var/www/html/splitextract/0000736197_2026_08_ZL_13_281_07092026082501 (1).jpg"
)


def _item(name: str, **fields):
    row = {
        "product_name": name,
        "packing": "10TAB",
        "opening_qty": 4,
        "receipts_qty": 1,
        "sales_qty": 2,
        "sales_value": 20,
        "closing_qty": 3,
        "closing_value": 30,
        "extra": {},
    }
    row.update(fields)
    return row


def _structured(method: str = "main_stock_sales_statement", rows: int = 4, **extra):
    result = empty_result("sheet.jpg", "jpg")
    result["line_items"] = [_item(f"ABANA {index}") for index in range(rows)]
    result["totals"]["extra"]["extraction_method"] = method
    result["totals"]["extra"]["layout"] = extra.pop(
        "layout", "opening_receive_issue_closing"
    )
    result["totals"]["extra"].update(extra)
    return result


def _gemini_response(payload: dict):
    return SimpleNamespace(
        json=lambda: {
            "candidates": [
                {"content": {"parts": [{"text": json.dumps(payload)}]}}
            ]
        }
    )


class StockImageQualityDecisionTests(unittest.TestCase):
    def test_good_structured_ocr_does_not_need_gemini(self):
        decision = assess_stock_structured_quality(_structured())
        self.assertFalse(decision["needs_direct_gemini"])
        self.assertEqual(decision["reasons"], [])

    def test_unknown_schema_requests_direct_gemini(self):
        decision = assess_stock_structured_quality(
            _structured(method="", layout=""),
            {"schema": "unknown"},
        )
        self.assertTrue(decision["needs_direct_gemini"])
        self.assertIn("schema_unknown", decision["reasons"])

    def test_missing_headers_request_direct_gemini(self):
        decision = assess_stock_structured_quality(
            _structured(),
            {"required_columns_detected": False, "detected_headers": []},
        )
        self.assertTrue(decision["needs_direct_gemini"])
        self.assertIn("required_headers_missing", decision["reasons"])

    def test_poor_ocr_quality_requests_direct_gemini(self):
        decision = assess_stock_structured_quality(
            _structured(),
            {"ocr_confidence": 0.2},
        )
        self.assertTrue(decision["needs_direct_gemini"])
        self.assertIn("poor_ocr_quality", decision["reasons"])

    def test_high_identity_failure_rate_requests_direct_gemini(self):
        result = _structured(method="geometry_probe", layout="unmapped")
        result["totals"]["extra"]["stock_identity_fail_count"] = 6
        decision = assess_stock_structured_quality(result)
        self.assertTrue(decision["needs_direct_gemini"])
        self.assertIn("identity_failure_rate", decision["reasons"])
        self.assertGreaterEqual(decision["identity_failure_rate"], 0.35)


class StockImageGeminiTests(unittest.TestCase):
    def test_good_structured_result_does_not_call_gemini(self):
        with patch("services.stock_image_vision.invoke_stock_gemini") as invoke:
            kept = apply_direct_stock_image_gemini(
                _structured(), b"original-bytes", "sheet.jpg", ".jpg"
            )
        invoke.assert_not_called()
        self.assertEqual(
            kept["totals"]["extra"]["final_selected_extraction"], "structured"
        )

    def test_poor_image_sends_original_bytes_and_reports_quality(self):
        original = b"original-image-bytes"
        parsed = {
            "image_quality": {
                "readable": False,
                "table_visible": False,
                "headers_readable": False,
                "numeric_cells_readable": False,
                "quality": "poor",
                "confidence": 0.1,
            },
            "detected_columns": [],
            "line_items": [_item("INVENTED ROW", opening_qty=99)],
        }
        seen = {}

        def _invoke(payload):
            seen["payload"] = payload
            return _gemini_response(parsed)

        with patch("services.stock_image_vision.invoke_stock_gemini", side_effect=_invoke):
            selected = apply_direct_stock_image_gemini(
                _structured(method="", layout=""),
                original,
                "poor.jpg",
                ".jpg",
                {"schema": "unknown", "image_readability": "poor"},
            )
        encoded = seen["payload"]["contents"][0]["parts"][1]["inline_data"]["data"]
        self.assertEqual(base64.b64decode(encoded), original)
        self.assertEqual(selected["totals"]["extra"]["image_quality"]["quality"], "poor")
        self.assertEqual(selected["line_items"], [])
        self.assertTrue(selected["totals"]["extra"].get("extraction_failed"))
        self.assertNotIn("OPENING", seen["payload"]["contents"][0]["parts"][0]["text"])

    def test_gemini_receives_original_bytes_not_ocr_text(self):
        original = b"\xff\xd8actual-jpeg"
        parsed = {
            "image_quality": {"readable": True, "quality": "good", "confidence": 0.9},
            "detected_columns": [
                {"header": "OPSTK", "semantic": "opening_qty"},
                {"header": "SALE", "semantic": "sales_qty"},
            ],
            "line_items": [_item("ABANA", opening_qty=4, sales_qty=1, closing_qty=3)],
        }
        seen = {}

        def _invoke(payload):
            seen["payload"] = payload
            return _gemini_response(parsed)

        with patch("services.stock_image_vision.invoke_stock_gemini", side_effect=_invoke):
            apply_direct_stock_image_gemini(
                _structured(method="", layout=""),
                original,
                "sheet.jpg",
                ".jpg",
                {"schema": "unknown", "ocr_text": "OPSTK 10 OCR TRANSCRIPT"},
            )
        parts = seen["payload"]["contents"][0]["parts"]
        self.assertEqual(parts[0]["text"], STOCK_IMAGE_VISION_PROMPT)
        self.assertNotIn("OCR TRANSCRIPT", parts[0]["text"])
        self.assertEqual(base64.b64decode(parts[1]["inline_data"]["data"]), original)
        self.assertIn("original stock-statement image", parts[0]["text"])

    def test_gemini_values_are_not_rewritten_to_balance_identity(self):
        raw = {
            "image_quality": {"quality": "good", "readable": True, "confidence": 0.8},
            "line_items": [
                {
                    "product_name": "ABANA",
                    "packing": "10TAB",
                    "opening_qty": 10,
                    "purchase_qty": 0,
                    "sales_qty": 1,
                    "closing_qty": 7,
                }
            ],
        }
        selected = normalize_stock_gemini_extraction(raw, "sheet.jpg", ".jpg")
        item = selected["line_items"][0]
        self.assertEqual(item["opening_qty"], 10)
        self.assertEqual(item["sales_qty"], 1)
        self.assertEqual(item["closing_qty"], 7)

    def test_product_name_numbers_do_not_become_quantities(self):
        raw = {
            "line_items": [
                {
                    "product_name": "LIV 52 TAB",
                    "packing": "60",
                    "opening_qty": 4,
                    "sales_qty": 1,
                    "closing_qty": 3,
                }
            ]
        }
        item = normalize_stock_gemini_extraction(raw, "a.jpg", ".jpg")["line_items"][0]
        self.assertIn("52", item["product_name"])
        self.assertEqual(item["opening_qty"], 4)
        self.assertEqual(item["sales_qty"], 1)

    def test_packing_numbers_do_not_become_quantities(self):
        raw = {
            "detected_columns": [{"header": "PACKING", "semantic": "packing"}],
            "line_items": [
                {
                    "product_name": "ABANA",
                    "packing": "10",
                    "opening_qty": 5,
                    "sales_qty": 2,
                    "cells": {"PACKING": "10"},
                }
            ],
        }
        item = normalize_stock_gemini_extraction(raw, "a.jpg", ".jpg")["line_items"][0]
        self.assertEqual(item["packing"], "10")
        self.assertEqual(item["opening_qty"], 5)
        self.assertEqual(item["sales_qty"], 2)

    def test_sale_val_and_stk_val_do_not_become_sales_qty(self):
        raw = {
            "detected_columns": [
                {"header": "SALE", "semantic": "sales_qty"},
                {"header": "SALE VAL", "semantic": "sales_value"},
                {"header": "STK VAL", "semantic": "stock_value"},
            ],
            "line_items": [
                {
                    "product_name": "ABANA",
                    "packing": "10TAB",
                    "opening_qty": 8,
                    "sales_qty": 880.5,
                    "sales_value": 880.5,
                    "closing_qty": 6,
                    "cells": {
                        "SALE": "2",
                        "SALE VAL": "880.5",
                        "STK VAL": "1200",
                    },
                }
            ],
        }
        item = normalize_stock_gemini_extraction(raw, "a.jpg", ".jpg")["line_items"][0]
        self.assertEqual(item["sales_qty"], 2)
        self.assertEqual(item["sales_value"], 880.5)
        self.assertEqual(item["closing_value"], 1200)
        self.assertNotEqual(item["sales_qty"], item["sales_value"])


class ZlOriginalImageRegressionTests(unittest.TestCase):
    def test_zl_image_gemini_gets_exact_uploaded_bytes(self):
        original = ZL_IMAGE.read_bytes()
        parsed = {
            "image_quality": {
                "readable": True,
                "table_visible": True,
                "headers_readable": True,
                "numeric_cells_readable": True,
                "quality": "good",
                "confidence": 0.91,
            },
            "detected_columns": [
                {"header": "OPSTK", "semantic": "opening_qty"},
                {"header": "PURCH", "semantic": "purchase_qty"},
                {"header": "SALE", "semantic": "sales_qty"},
                {"header": "SALE VAL", "semantic": "sales_value"},
                {"header": "EXP3M", "semantic": "expiry_damage_qty"},
            ],
            "line_items": [
                {
                    "product_name": "ABANA",
                    "packing": "60",
                    "opening_qty": 11,
                    "purchase_qty": 2,
                    "sales_qty": 4,
                    "sales_value": 350.0,
                    "closing_qty": 8,
                    "expiry_damage_qty": 1,
                    "cells": {
                        "OPSTK": "11",
                        "PURCH": "2",
                        "SALE": "4",
                        "SALE VAL": "350.0",
                        "PACKING": "60",
                    },
                }
            ],
        }
        seen = {}

        def _invoke(payload):
            seen["payload"] = payload
            return _gemini_response(parsed)

        structured = _structured(method="", layout="")
        with patch("services.stock_image_vision.invoke_stock_gemini", side_effect=_invoke):
            selected = apply_direct_stock_image_gemini(
                structured,
                original,
                ZL_IMAGE.name,
                ".jpg",
                {"schema": "unknown"},
            )
        encoded = seen["payload"]["contents"][0]["parts"][1]["inline_data"]["data"]
        sent = base64.b64decode(encoded)
        digest = hashlib.sha256(original).hexdigest()
        self.assertEqual(sent, original)
        self.assertEqual(selected["totals"]["extra"]["gemini_input"]["original_sha256"], digest)
        self.assertEqual(
            selected["totals"]["extra"]["gemini_input"]["gemini_input_sha256"], digest
        )
        self.assertFalse(selected["totals"]["extra"]["gemini_input"]["normalized"])
        self.assertEqual(
            selected["totals"]["extra"]["final_selected_extraction"], "gemini"
        )
        self.assertEqual(
            selected["totals"]["extra"]["GEMINI_RAW_EXTRACTION"]["line_items"][0]["closing_qty"],
            8,
        )
        item = selected["line_items"][0]
        self.assertEqual(item["sales_qty"], 4)
        self.assertEqual(item["sales_value"], 350.0)
        self.assertEqual(item["opening_qty"], 11)
        self.assertEqual(item["extra"]["expiry_damage_qty"], 1)
        self.assertEqual(
            selected["totals"]["extra"]["detected_columns"][0]["header"], "OPSTK"
        )

    def test_extract_sales_statement_hook_uses_original_image(self):
        original = ZL_IMAGE.read_bytes()
        parsed = {
            "image_quality": {"quality": "acceptable", "readable": True, "confidence": 0.7},
            "detected_columns": [{"header": "OPSTK", "semantic": "opening_qty"}],
            "line_items": [_item("ABANA", opening_qty=3, closing_qty=3)],
        }
        seen = {}

        def _invoke(payload):
            seen["payload"] = payload
            return _gemini_response(parsed)

        with patch(
            "services.sales_statement_extractor._parse_image",
            return_value=_structured(method="", layout=""),
        ), patch(
            "services.stock_image_vision.invoke_stock_gemini", side_effect=_invoke
        ):
            result = extract_sales_statement(original, ZL_IMAGE.name)
        encoded = seen["payload"]["contents"][0]["parts"][1]["inline_data"]["data"]
        self.assertEqual(base64.b64decode(encoded), original)
        self.assertEqual(result["totals"]["extra"]["extraction_method"], "stock_image_gemini")
        self.assertNotIn("OCR", seen["payload"]["contents"][0]["parts"][0]["text"][:80])


class StockImageFailClosedTests(unittest.TestCase):
    def test_untrusted_structured_fail_closed_on_invalid_gemini_json(self):
        """Garbage OCR + failed Gemini must not return HTTP-200 false success."""
        result = _structured(method="tesseract_heuristic", layout="", rows=8)
        result["totals"]["extra"]["stock_identity_fail_count"] = 7
        with patch(
            "services.stock_image_vision.invoke_stock_gemini",
            return_value=SimpleNamespace(
                json=lambda: {
                    "candidates": [
                        {"content": {"parts": [{"text": "not-json"}]}}
                    ]
                }
            ),
        ), patch(
            "services.stock_image_vision.prepare_original_image",
            return_value=(
                b"img",
                "image/jpeg",
                {
                    "original_sha256": "a",
                    "gemini_input_sha256": "a",
                    "normalized": False,
                    "byte_length": 3,
                },
            ),
        ), patch(
            "services.stock_image_vision._enabled",
            return_value=True,
        ):
            out = apply_direct_stock_image_gemini(
                result,
                b"img",
                "bad.png",
                ".png",
                {"schema": "unknown"},
            )
        self.assertTrue(out["totals"]["extra"].get("extraction_failed"))
        self.assertEqual(
            out["totals"]["extra"].get("extraction_failed_reason"),
            "gemini_invalid_json",
        )


if __name__ == "__main__":
    unittest.main()
