"""Sanitized Vertex 429/503 diagnostics — no live Google calls."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from google.genai import errors as genai_errors

from services.vertex_gemini_client import (
    GeminiProviderError,
    _safe_vertex_error_headers,
    _sanitize_vertex_error_details,
    _vertex_provider_error_diag,
    generate_content_via_vertex,
)


def _api_error(code: int, body: dict, headers: dict | None = None) -> genai_errors.APIError:
    response = None
    if headers is not None:
        response = type("FakeResponse", (), {"headers": headers})()
    return genai_errors.APIError(code, body, response)


VERTEX_QUOTA_BODY = {
    "error": {
        "code": 429,
        "message": "Resource exhausted. Please try again later.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "RATE_LIMIT_EXCEEDED",
                "metadata": {
                    "quotaMetric": "aiplatform.googleapis.com/generate_content_requests",
                    "quotaId": "GenerateContentRequestsPerMinutePerProjectPerBaseModel",
                    "quotaLimit": "60",
                    "private_key": "-----BEGIN PRIVATE KEY-----SECRET",
                    "authorization": "Bearer leaked-token",
                },
            },
            {
                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": "8s",
            },
        ],
        "customer_text": "should never be logged",
        "invoice_image": "base64-blob",
    }
}


class TestSanitizeVertexErrorDetails(unittest.TestCase):
    def test_keeps_quota_and_retry_fields(self):
        cleaned = _sanitize_vertex_error_details(VERTEX_QUOTA_BODY)
        blob = str(cleaned)
        self.assertIn("quotaMetric", blob)
        self.assertIn("aiplatform.googleapis.com/generate_content_requests", blob)
        self.assertIn("quotaId", blob)
        self.assertIn("GenerateContentRequestsPerMinutePerProjectPerBaseModel", blob)
        self.assertIn("quotaLimit", blob)
        self.assertIn("60", blob)
        self.assertIn("retryDelay", blob)
        self.assertIn("8s", blob)
        self.assertIn("RATE_LIMIT_EXCEEDED", blob)
        self.assertIn("RESOURCE_EXHAUSTED", blob)

    def test_strips_sensitive_and_arbitrary_fields(self):
        cleaned = _sanitize_vertex_error_details(VERTEX_QUOTA_BODY)
        blob = str(cleaned)
        self.assertNotIn("private_key", blob)
        self.assertNotIn("BEGIN PRIVATE KEY", blob)
        self.assertNotIn("authorization", blob.lower())
        self.assertNotIn("Bearer leaked-token", blob)
        self.assertNotIn("customer_text", blob)
        self.assertNotIn("should never be logged", blob)
        self.assertNotIn("invoice_image", blob)
        self.assertNotIn("base64-blob", blob)

    def test_safe_headers_only(self):
        headers = {
            "Retry-After": "8",
            "x-goog-retry-info": "retryDelay=8s",
            "x-goog-quota-limit": "60",
            "Authorization": "Bearer secret-token",
            "Cookie": "session=abc",
            "X-Custom": "nope",
        }
        safe = _safe_vertex_error_headers(type("R", (), {"headers": headers})())
        self.assertEqual(safe.get("Retry-After"), "8")
        self.assertEqual(safe.get("x-goog-retry-info"), "retryDelay=8s")
        self.assertEqual(safe.get("x-goog-quota-limit"), "60")
        joined = " ".join(f"{k}={v}" for k, v in safe.items())
        self.assertNotIn("secret-token", joined)
        self.assertNotIn("Authorization", joined)
        self.assertNotIn("Cookie", joined)
        self.assertNotIn("session=abc", joined)
        self.assertNotIn("X-Custom", joined)

    def test_diag_uses_sdk_attributes(self):
        exc = _api_error(
            429,
            VERTEX_QUOTA_BODY,
            {"Retry-After": "8", "Authorization": "Bearer hide-me"},
        )
        self.assertEqual(exc.code, 429)
        self.assertEqual(exc.status, "RESOURCE_EXHAUSTED")
        self.assertTrue(exc.message)
        self.assertTrue(hasattr(exc, "details"))
        self.assertTrue(hasattr(exc, "response"))
        diag = _vertex_provider_error_diag(exc, "gemini-2.5-flash-lite")
        self.assertEqual(diag["event"], "VERTEX_GEMINI_PROVIDER_ERROR")
        self.assertEqual(diag["http_code"], 429)
        self.assertEqual(diag["status"], "RESOURCE_EXHAUSTED")
        self.assertEqual(diag["model"], "gemini-2.5-flash-lite")
        self.assertIn("quotaMetric", str(diag["details"]))
        self.assertEqual(diag["headers"].get("Retry-After"), "8")
        self.assertNotIn("hide-me", str(diag))


class _RaiseModels:
    def __init__(self, exc: Exception):
        self._exc = exc

    def generate_content(self, **_kwargs):
        raise self._exc


class _RaiseClient:
    def __init__(self, exc: Exception):
        self.models = _RaiseModels(exc)


class TestGenerateContentViaVertexErrors(unittest.TestCase):
    _payload = {"contents": [{"role": "user", "parts": [{"text": "ping"}]}]}

    def test_429_raises_provider_error_unchanged(self):
        exc = _api_error(429, VERTEX_QUOTA_BODY, {"Retry-After": "4"})
        with patch(
            "services.vertex_gemini_client.get_vertex_gemini_client",
            return_value=_RaiseClient(exc),
        ):
            with self.assertLogs(
                "services.vertex_gemini_client", level="WARNING"
            ) as logs:
                with self.assertRaises(GeminiProviderError) as caught:
                    generate_content_via_vertex(
                        "gemini-2.5-flash-lite", self._payload, timeout=5
                    )
        self.assertEqual(caught.exception.code, 429)
        self.assertEqual(str(caught.exception), str(exc))
        self.assertIsInstance(caught.exception.__cause__, genai_errors.APIError)
        joined = "\n".join(logs.output)
        self.assertIn("VERTEX_GEMINI_PROVIDER_ERROR", joined)
        self.assertIn("quotaMetric", joined)
        self.assertNotIn("BEGIN PRIVATE KEY", joined)
        self.assertNotIn("Bearer leaked-token", joined)
        self.assertNotIn("hide-me", joined)

    def test_503_raises_provider_error_unchanged(self):
        body = {"error": {"code": 503, "status": "UNAVAILABLE", "message": "busy"}}
        exc = _api_error(503, body)
        with patch(
            "services.vertex_gemini_client.get_vertex_gemini_client",
            return_value=_RaiseClient(exc),
        ):
            with self.assertRaises(GeminiProviderError) as caught:
                generate_content_via_vertex(
                    "gemini-2.5-flash-lite", self._payload, timeout=5
                )
        self.assertEqual(caught.exception.code, 503)
        self.assertEqual(str(caught.exception), str(exc))

    def test_non_429_503_is_reraised(self):
        exc = _api_error(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT"}})
        with patch(
            "services.vertex_gemini_client.get_vertex_gemini_client",
            return_value=_RaiseClient(exc),
        ):
            with self.assertRaises(genai_errors.APIError) as caught:
                generate_content_via_vertex(
                    "gemini-2.5-flash-lite", self._payload, timeout=5
                )
        self.assertIs(caught.exception, exc)
        self.assertEqual(caught.exception.code, 400)
        self.assertNotIsInstance(caught.exception, GeminiProviderError)


if __name__ == "__main__":
    unittest.main()
