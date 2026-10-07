"""Internal invoice AI client: success, invalid JSON, timeout, 4xx/5xx, no external LLM."""

import unittest
from threading import Lock
from unittest.mock import MagicMock, patch

import requests

from services.internal_invoice_ai import (
    InternalInvoiceAIError,
    DEFAULT_INVOICE_AI_API_URL,
    extract_structured_invoice,
    invoice_ai_api_url,
    line_items_from_full_data,
    invoice_summary_from_full_data,
)


def _ok_payload():
    return {
        "success": True,
        "request_id": "req-test-1",
        "result": {
            "invoice_summary": {
                "invoice_no": "INV-100",
                "invoice_date": "2026-01-15",
                "customer": "TEST HOSPITAL",
                "vendor": "TEST VENDOR",
                "total": "1100",
                "tax": "100",
            },
            "line_items": {
                "count": 1,
                "items": [{
                    "product_description": "SAMPLE TAB",
                    "quantity": "10",
                    "unit_price": "100",
                    "total_amount": "1000",
                }],
            },
        },
        "fallback_used": [],
        "ocr": {"page_count": 1},
    }


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class InvoiceAIUrlTests(unittest.TestCase):
    def test_default_url_is_https_internal_endpoint(self):
        with patch.dict("os.environ", {"INVOICE_AI_API_URL": DEFAULT_INVOICE_AI_API_URL}, clear=False):
            self.assertEqual(invoice_ai_api_url(), DEFAULT_INVOICE_AI_API_URL)
            self.assertTrue(invoice_ai_api_url().startswith("https://"))

    def test_http_url_rejected(self):
        with patch.dict("os.environ", {"INVOICE_AI_API_URL": "http://evil.example/extract"}, clear=False):
            with self.assertRaises(InternalInvoiceAIError) as ctx:
                invoice_ai_api_url()
            self.assertEqual(ctx.exception.category, "invalid_config")


class InvoiceAIExtractTests(unittest.TestCase):
    def setUp(self):
        from services import internal_invoice_ai as mod
        mod._result_cache_ctx.set({})
        mod._pdf_bytes_ctx.set(None)

    def test_successful_extraction_maps_schema(self):
        pdf = b"%PDF-1.4 fake"
        with patch(
            "services.internal_invoice_ai.requests.post",
            return_value=FakeResponse(200, _ok_payload()),
        ) as post:
            parsed = extract_structured_invoice(pdf)
        self.assertEqual(post.call_args.kwargs.get("timeout")[0], 10)
        self.assertTrue(post.call_args.args[0].startswith("https://"))
        files = post.call_args.kwargs["files"]
        self.assertEqual(files["file"][2], "application/pdf")
        summary = invoice_summary_from_full_data(parsed)
        self.assertEqual(summary.get("invoice_no"), "INV-100")
        items = line_items_from_full_data(parsed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["product_description"], "SAMPLE TAB")

    def test_invalid_json_is_controlled_error(self):
        with patch(
            "services.internal_invoice_ai.requests.post",
            return_value=FakeResponse(200, payload=None),
        ):
            with self.assertRaises(InternalInvoiceAIError) as ctx:
                extract_structured_invoice(b"%PDF-1.4 x")
            self.assertEqual(ctx.exception.category, "invalid_response")

    def test_timeout_no_external_fallback(self):
        with patch(
            "services.internal_invoice_ai.requests.post",
            side_effect=requests.Timeout("timed out"),
        ), patch("services.internal_invoice_ai.time.sleep"):
            with self.assertRaises(InternalInvoiceAIError) as ctx:
                extract_structured_invoice(b"%PDF-1.4 x")
            self.assertEqual(ctx.exception.category, "timeout")

    def test_http_4xx_no_retry_no_fallback(self):
        post = MagicMock(return_value=FakeResponse(400, {"success": False, "error": "bad"}))
        with patch("services.internal_invoice_ai.requests.post", post):
            with self.assertRaises(InternalInvoiceAIError) as ctx:
                extract_structured_invoice(b"%PDF-1.4 x")
            self.assertEqual(ctx.exception.category, "http_4xx")
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(post.call_count, 1)

    def test_http_5xx_retries_then_fails(self):
        post = MagicMock(return_value=FakeResponse(503, {"success": False, "error": "down"}))
        with patch("services.internal_invoice_ai.requests.post", post), patch(
            "services.internal_invoice_ai.time.sleep"
        ):
            with self.assertRaises(InternalInvoiceAIError) as ctx:
                extract_structured_invoice(b"%PDF-1.4 x")
            self.assertEqual(ctx.exception.category, "http_5xx")
            self.assertGreaterEqual(post.call_count, 2)

    def test_unavailable_is_controlled_failure(self):
        with patch(
            "services.internal_invoice_ai.requests.post",
            side_effect=requests.ConnectionError("refused"),
        ), patch("services.internal_invoice_ai.time.sleep"):
            with self.assertRaises(InternalInvoiceAIError) as ctx:
                extract_structured_invoice(b"%PDF-1.4 x")
            self.assertEqual(ctx.exception.category, "unavailable")


class AppExtractWrapperTests(unittest.TestCase):
    def test_text_extract_uses_internal_api_only(self):
        import app as app_module

        stats = app_module.create_ocr_stats()
        lock = Lock()
        with patch.object(
            app_module,
            "extract_structured_invoice",
            return_value={"data": {"invoice_summary": {"invoice_no": "A1"}, "line_items": {"items": [{"product_description": "X"}]}}},
        ) as extract:
            parsed = app_module.extract_full_data_from_text_gemini("ocr text", stats, lock)
        extract.assert_called_once()
        self.assertEqual(parsed["data"]["invoice_summary"]["invoice_no"], "A1")
        self.assertGreaterEqual(stats["internal_ai_calls"], 1)

    def test_text_extract_timeout_returns_none_without_external_llm(self):
        import app as app_module

        stats = app_module.create_ocr_stats()
        lock = Lock()
        with patch.object(
            app_module,
            "extract_structured_invoice",
            side_effect=InternalInvoiceAIError("timeout", "timed out"),
        ):
            parsed = app_module.extract_full_data_from_text_gemini("ocr text", stats, lock)
        self.assertIsNone(parsed)
        self.assertFalse(hasattr(app_module, "call_gemini_with_quota"))
        self.assertFalse(hasattr(app_module, "generate_content_via_vertex"))


if __name__ == "__main__":
    unittest.main()
