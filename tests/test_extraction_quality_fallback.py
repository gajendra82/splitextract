"""Existing parsers stay authoritative. Gemini runs only for a failed quality gate."""

import unittest
from unittest import mock

from services.extraction_quality import evaluate_extraction_quality
from services.gemini_extraction_fallback import _call_gemini, maybe_apply_gemini_fallback


def _item(name="ABANA TAB", opening=10, receipts=0, sales=2, closing=8, sales_value=0):
    return {
        "product_name": name,
        "packing": "60'S",
        "opening_qty": opening,
        "receipts_qty": receipts,
        "sales_qty": sales,
        "closing_qty": closing,
        "sales_value": sales_value,
        "closing_value": 0,
    }


class QualityGateTests(unittest.TestCase):
    def test_named_parser_is_not_sent_to_gemini(self):
        result = {
            "line_items": [_item() for _ in range(6)],
            "totals": {
                "extra": {
                    "extraction_method": "marg_qty_value_dump_xls",
                    "stock_identity_fail_count": 6,
                    "stock_identity_kind": "opening_receipts_sales_closing",
                }
            },
        }
        quality = evaluate_extraction_quality(result)
        self.assertFalse(quality["should_fallback"])
        self.assertEqual(quality["quality"], "good")

    def test_quantity_only_zeros_are_valid(self):
        result = {
            "line_items": [
                _item(name=f"ROW {index}", opening=0, receipts=0, sales=0, closing=0)
                for index in range(8)
            ],
            "totals": {"extra": {"extraction_method": "gemini_vision"}},
        }
        quality = evaluate_extraction_quality(result)
        self.assertFalse(quality["should_fallback"])
        self.assertNotIn("missing_numeric_values", quality["reasons"])

    def test_empty_extract_requests_fallback(self):
        quality = evaluate_extraction_quality(
            {"line_items": [], "totals": {"extra": {"extraction_method": "gemini_vision"}}}
        )
        self.assertTrue(quality["should_fallback"])
        self.assertIn("empty_line_items", quality["reasons"])

    def test_ocr_garbage_requests_fallback(self):
        result = {
            "line_items": [_item(name="0pening"), _item(name="5a1es"), _item(name="C1osing")],
            "totals": {"extra": {"extraction_method": "gemini_vision"}},
        }
        quality = evaluate_extraction_quality(result)
        self.assertTrue(quality["should_fallback"])
        self.assertIn("high_ocr_noise", quality["reasons"])

    def test_closing_only_sheet_does_not_fail_identity(self):
        result = {
            "line_items": [
                _item(name=f"ROW {index}", opening=0, receipts=0, sales=0, closing=4)
                for index in range(10)
            ],
            "totals": {
                "extra": {
                    "extraction_method": "gemini_vision",
                    "stock_identity_kind": "opening_receipts_sales_closing",
                    "stock_identity_fail_count": 10,
                }
            },
        }
        quality = evaluate_extraction_quality(result)
        self.assertNotIn("stock_identity_failure", quality["reasons"])
        self.assertFalse(quality["should_fallback"])

    def test_repeated_rows_request_fallback(self):
        row = _item(name="ARJUNA TAB", opening=1, receipts=0, sales=1, closing=0)
        result = {
            "line_items": [dict(row) for _ in range(8)],
            "totals": {"extra": {"extraction_method": "gemini_vision"}},
        }
        quality = evaluate_extraction_quality(result)
        self.assertIn("duplicate_rows", quality["reasons"])
        self.assertEqual(quality["quality"], "poor")

    def test_named_parser_keeps_repeated_rows(self):
        row = _item(name="ARJUNA TAB")
        result = {
            "line_items": [dict(row) for _ in range(8)],
            "totals": {"extra": {"extraction_method": "marg_qty_value_dump_xls"}},
        }
        quality = evaluate_extraction_quality(result)
        self.assertFalse(quality["should_fallback"])

    def test_backwards_period_requests_fallback_for_generic_extract(self):
        result = {
            "period_from": "2026-09-01",
            "period_to": "2026-08-01",
            "line_items": [_item(name=f"ROW {index}") for index in range(4)],
            "totals": {"extra": {"extraction_method": "gemini_vision"}},
        }
        quality = evaluate_extraction_quality(result)
        self.assertIn("invalid_period", quality["reasons"])


class FallbackDispatchTests(unittest.TestCase):
    def test_good_result_does_not_call_gemini(self):
        result = {
            "line_items": [_item()],
            "totals": {"extra": {"extraction_method": "prompt_datewise_layout"}},
        }
        with mock.patch(
            "services.gemini_extraction_fallback._call_gemini"
        ) as caller:
            kept = maybe_apply_gemini_fallback(result, b"pdf-bytes", "a.pdf", ".pdf")
        caller.assert_not_called()
        self.assertEqual(kept["totals"]["extra"]["gemini_fallback"], "not_called")
        self.assertEqual(kept["line_items"][0]["opening_qty"], 10)

    def test_bad_result_accepts_a_valid_gemini_read(self):
        result = {
            "line_items": [],
            "totals": {"sales_value": None, "closing_value": None, "extra": {}},
        }
        parsed = {
            "stockist_name": "PALAK MEDICAL AGENCY",
            "period_from": "2026-08-01",
            "period_to": "2026-08-31",
            "line_items": [
                {
                    "product_name": "ARJUNA TAB",
                    "packing": "60'S",
                    "opening_qty": 27,
                    "receipts_qty": 0,
                    "sales_qty": 5,
                    "sales_value": 1238,
                    "closing_qty": 22,
                    "closing_value": 4854,
                }
            ],
        }
        with mock.patch(
            "services.gemini_extraction_fallback._call_gemini", return_value=parsed
        ) as caller, mock.patch(
            "services.gemini_extraction_fallback._document_parts",
            return_value=[{"text": "page"}],
        ):
            kept = maybe_apply_gemini_fallback(result, b"img", "a.jpg", ".jpg")
        caller.assert_called_once()
        self.assertEqual(kept["line_items"][0]["product_name"], "ARJUNA TAB")
        self.assertEqual(kept["line_items"][0]["sales_value"], 1238)
        self.assertEqual(kept["line_items"][0]["closing_value"], 4854)
        self.assertEqual(kept["totals"]["extra"]["gemini_fallback"], "success")

    def test_invalid_gemini_read_is_not_used(self):
        result = {
            "line_items": [],
            "totals": {"sales_value": None, "closing_value": None, "extra": {}},
        }
        parsed = {"line_items": [{"product_name": "0pening", "opening_qty": None}]}
        with mock.patch(
            "services.gemini_extraction_fallback._call_gemini", return_value=parsed
        ), mock.patch(
            "services.gemini_extraction_fallback._document_parts",
            return_value=[{"text": "page"}],
        ):
            kept = maybe_apply_gemini_fallback(result, b"img", "a.jpg", ".jpg")
        self.assertEqual(kept["line_items"], [])
        self.assertEqual(kept["totals"]["extra"]["gemini_fallback"], "validation_failed")
        self.assertTrue(kept["totals"]["extra"]["extraction_failed"])

    def test_fallback_uses_split_extract_quota_client(self):
        response = mock.Mock()
        response.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "{\"line_items\": []}"}]}}],
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 4},
        }
        with mock.patch(
            "services.gemini_extraction_fallback._document_parts",
            return_value=[{"inline_data": {"mime_type": "image/png", "data": "abc"}}],
        ), mock.patch.dict(
            "sys.modules",
            {
                "app": mock.Mock(
                    call_gemini_with_quota=mock.Mock(return_value=response),
                    get_current_model_config=mock.Mock(
                        return_value={"name": "gemini-2.5-flash-lite", "timeout": 60, "max_output_tokens": 8192}
                    ),
                )
            },
        ):
            import app as app_module

            parsed = _call_gemini(b"img", ".jpg", [{"inline_data": {"mime_type": "image/png", "data": "abc"}}])
        app_module.call_gemini_with_quota.assert_called_once()
        self.assertEqual(
            app_module.call_gemini_with_quota.call_args.kwargs["request_type"],
            "vision",
        )
        self.assertEqual(
            app_module.call_gemini_with_quota.call_args.kwargs["model"],
            "gemini-2.5-flash-lite",
        )
        self.assertEqual(parsed, {"line_items": []})


if __name__ == "__main__":
    unittest.main()
