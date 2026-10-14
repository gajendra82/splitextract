"""Sales extraction health probe + hang-guard regressions (mocks only)."""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from services import sales_extraction_runtime as runtime


class SalesExtractionHealthTests(unittest.TestCase):
    def test_health_snapshot_keys(self):
        snap = runtime.get_sales_extraction_health_snapshot()
        for key in (
            "extraction_ready",
            "tesseract_available",
            "extraction_active",
            "extraction_waiting",
            "extraction_limit",
            "gemini_active",
            "gemini_limit",
            "gemini_provider_cooldown_remaining",
            "max_execution_seconds",
            "provider_metrics",
        ):
            self.assertIn(key, snap)

    def test_health_endpoint(self):
        import app as app_module

        client = TestClient(app_module.app)
        resp = client.get("/extract-sales-statement/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn(body.get("status"), ("ready", "degraded"))
        self.assertTrue(body.get("alive"))
        self.assertIn("extraction_ready", body)


class SalesTesseractTimeoutNonblockingTests(unittest.TestCase):
    def tearDown(self):
        runtime.clear_sales_deadline()

    def test_bounded_tesseract_timeout_does_not_wait_on_shutdown(self):
        runtime.start_sales_deadline("req-tess-hang", limit_seconds=30)

        def hang():
            time.sleep(30)

        t0 = time.monotonic()
        with patch.object(runtime, "SALES_TESSERACT_CALL_TIMEOUT_SECONDS", 1), patch.object(
            runtime, "terminate_sales_tesseract_children", MagicMock()
        ), patch.object(runtime, "mark_sales_stage", MagicMock()), patch.object(
            runtime, "sales_tesseract_timeout_seconds", return_value=1
        ):
            with self.assertRaises(TimeoutError):
                runtime.run_bounded_sales_tesseract(hang, "hang_test")
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 5.0, f"tesseract timeout blocked for {elapsed:.1f}s")


class VisionTableNoTimeoutFallbackTests(unittest.TestCase):
    def test_timeout_is_not_swallowed_by_quota_fallback(self):
        from services import stock_vision_table as svt

        tiny_jpeg = (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xd9"
        )

        with patch.object(
            svt,
            "build_table_prompt",
            return_value="prompt",
        ), patch(
            "services.sales_extraction_runtime.sales_generate_content_via_vertex",
            side_effect=TimeoutError("Gemini request timed out after 1s"),
        ), patch(
            "app.call_gemini_with_quota",
            MagicMock(side_effect=AssertionError("must not fall back on timeout")),
        ), patch(
            "app.get_current_model_config",
            return_value={"name": "gemini-2.5-flash-lite", "timeout": 1},
        ):
            with self.assertRaises(TimeoutError):
                svt.extract_stock_table_vision(
                    [tiny_jpeg],
                    {"label": "stock_vision_table", "prompt": "p"},
                )


if __name__ == "__main__":
    unittest.main()
