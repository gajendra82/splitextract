"""Timeout / deadline reliability for Secondary Sales (mocks only)."""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from services import sales_extraction_runtime as runtime


def _ok_sales_result(**extra_overrides):
    extra = {
        "extraction_method": "mock_parser",
        "gemini_fallback": False,
        **extra_overrides,
    }
    return {
        "stockist_name": "Mock Stockist",
        "line_items": [{"product_name": "ITEM", "sales_qty": 1}],
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "totals": {"extra": extra},
    }


class SalesDeadlineUnitTests(unittest.TestCase):
    def tearDown(self):
        runtime.clear_sales_deadline()

    def test_check_deadline_raises(self):
        runtime.start_sales_deadline("req-d1", limit_seconds=0.01)
        time.sleep(0.02)
        with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
            runtime.check_sales_deadline("ocr")

    def test_gemini_timeout_respects_remaining(self):
        runtime.start_sales_deadline("req-d2", limit_seconds=5)
        self.assertLessEqual(runtime.sales_gemini_timeout_seconds(120), 5)
        self.assertGreaterEqual(runtime.sales_gemini_timeout_seconds(120), 1)

    def test_bounded_tesseract_times_out_and_releases_slot(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        @contextmanager
        def fake_slot(label="tesseract_ocr"):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                yield
            finally:
                with lock:
                    active -= 1

        real = SimpleNamespace(
            image_to_string=lambda *a, **k: (time.sleep(3) or "never"),
            image_to_data=lambda *a, **k: {"text": []},
            Output=SimpleNamespace(DICT="dict"),
        )
        gated = runtime.wrap_pytesseract_module(real)
        runtime.start_sales_deadline("req-tess", limit_seconds=30)
        with patch.object(runtime, "sales_tesseract_slot", fake_slot), patch.object(
            runtime, "SALES_TESSERACT_CALL_TIMEOUT_SECONDS", 1
        ), patch.object(
            runtime, "terminate_sales_tesseract_children", MagicMock()
        ), patch.object(
            runtime, "mark_sales_stage", MagicMock()
        ):
            with self.assertRaises(TimeoutError):
                gated.image_to_string(None)
        self.assertEqual(active, 0)
        self.assertEqual(max_active, 1)


class SalesEndpointDeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module

        cls.app_module = app_module
        cls.app = app_module.app

    def setUp(self):
        runtime._sales_extraction_async_sem = asyncio.Semaphore(
            runtime.MAX_CONCURRENT_EXTRACTIONS
        )
        with runtime._sales_extraction_waiters_lock:
            runtime._sales_extraction_waiting = 0
            runtime._sales_extraction_active = 0

    def test_deadline_returns_422_extraction_failed(self):
        def slow_extract(file_bytes, filename):
            time.sleep(2.0)
            return _ok_sales_result()

        with patch.object(
            self.app_module, "extract_sales_statement", side_effect=slow_extract
        ), patch.object(
            runtime, "SALES_EXTRACTION_MAX_EXECUTION_SECONDS", 1
        ), patch(
            "app.SALES_EXTRACTION_MAX_EXECUTION_SECONDS", 1, create=True
        ):
            # Patch the value imported inside the endpoint by patching runtime const
            # used by run_sales_extract_with_deadline default and wait_for.
            client = TestClient(self.app)

            # Monkeypatch the deadline used by endpoint import at call time.
            with patch(
                "services.sales_extraction_runtime.SALES_EXTRACTION_MAX_EXECUTION_SECONDS",
                1,
            ):
                resp = client.post(
                    "/extract-sales-statement",
                    files={
                        "file": ("slow.jpg", b"fake", "image/jpeg")
                    },
                )
        self.assertEqual(resp.status_code, 422)
        detail = resp.json().get("detail") or {}
        self.assertEqual(detail.get("error"), "extraction_failed")
        self.assertIn("extraction_deadline_exceeded", detail.get("reasons") or [])
        # Admission slot must be free again.
        with runtime._sales_extraction_waiters_lock:
            self.assertEqual(runtime._sales_extraction_active, 0)

    def test_no_false_success_on_extraction_failed(self):
        failed = _ok_sales_result(
            extraction_failed=True,
            extraction_quality={"reasons": ["mock"]},
        )
        with patch.object(
            self.app_module, "extract_sales_statement", return_value=failed
        ):
            client = TestClient(self.app)
            resp = client.post(
                "/extract-sales-statement",
                files={"file": ("bad.xlsx", b"fake", "application/vnd.ms-excel")},
            )
        self.assertEqual(resp.status_code, 422)
        self.assertNotEqual(resp.status_code, 200)
        body = resp.json()
        self.assertNotIn("line_items", body)

    def test_known_good_still_works(self):
        with patch.object(
            self.app_module,
            "extract_sales_statement",
            return_value=_ok_sales_result(),
        ):
            client = TestClient(self.app)
            resp = client.post(
                "/extract-sales-statement",
                files={"file": ("ok.xlsx", b"fake", "application/vnd.ms-excel")},
                headers={"X-Request-ID": "deadline-ok-1"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("stockist_name"), "Mock Stockist")
        status = client.get("/extract-sales-statement/status/deadline-ok-1")
        self.assertEqual(status.status_code, 200)
        payload = status.json()
        self.assertEqual(payload.get("status"), "completed")
        self.assertEqual(payload.get("stage"), "completed")


class GeminiDeadlineRetryTests(unittest.TestCase):
    def tearDown(self):
        runtime.clear_sales_deadline()

    def test_gemini_retry_stops_at_sales_deadline(self):
        import app as app_module
        from services.vertex_gemini_client import GeminiProviderError

        attempts = {"n": 0}

        def always_429(**kwargs):
            attempts["n"] += 1
            raise GeminiProviderError(429, "rate limited")

        runtime.start_sales_deadline("req-g", limit_seconds=1)
        with patch.object(
            app_module,
            "acquire_model_slot_with_wait",
            return_value={"name": "m"},
        ), patch.object(
            app_module, "generate_content_via_vertex", side_effect=always_429
        ), patch.object(
            app_module, "update_request_progress", MagicMock()
        ), patch.object(
            app_module, "release_gemini_inflight_counter", MagicMock()
        ), patch.object(
            app_module, "_log_quota_wait", MagicMock()
        ), patch.object(
            app_module, "MAX_WAIT_TIME", 30
        ), patch.object(
            app_module, "GEMINI_PROVIDER_BACKOFF_MAX_SECONDS", 5
        ), patch("time.sleep", MagicMock()):
            with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
                # burn remaining deadline
                time.sleep(1.05)
                app_module.call_gemini_with_quota(
                    "model", {"contents": []}, timeout=5, request_type="vision"
                )

    def test_gemini_slot_released_after_timeout(self):
        runtime._gemini_call_semaphore = threading.Semaphore(
            runtime.MAX_CONCURRENT_GEMINI_REQUESTS
        )
        with runtime._gemini_call_lock:
            runtime._gemini_call_active = 0
            runtime._gemini_call_waiting = 0

        active_after = []

        def boom():
            with runtime.gemini_call_slot("test"):
                raise TimeoutError("gemini timeout")

        runtime.start_sales_deadline("req-gs", limit_seconds=30)
        with self.assertRaises(TimeoutError):
            boom()
        with runtime._gemini_call_lock:
            active_after.append(runtime._gemini_call_active)
        self.assertEqual(active_after[0], 0)


class ConfigDeadlineTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(runtime.MAX_CONCURRENT_EXTRACTIONS, 2)
        self.assertEqual(runtime.MAX_CONCURRENT_GEMINI_REQUESTS, 2)
        self.assertEqual(runtime.SALES_EXTRACTION_MAX_EXECUTION_SECONDS, 1200)


if __name__ == "__main__":
    unittest.main()
