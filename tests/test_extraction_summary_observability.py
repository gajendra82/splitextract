"""EXTRACTION_SUMMARY + multi-path runtime deadline instrumentation."""

from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock, patch

from services import sales_extraction_runtime as runtime
from services.vertex_gemini_client import GeminiProviderError


class ExtractionSummaryObservabilityTests(unittest.TestCase):
    def setUp(self):
        runtime.reset_gemini_provider_cooldown_state_for_tests()
        runtime.clear_sales_deadline()

    def tearDown(self):
        runtime.reset_gemini_provider_cooldown_state_for_tests()
        runtime.clear_sales_deadline()

    def test_extraction_summary_uses_runtime_instrumentation(self):
        runtime.start_sales_deadline("req-summary", limit_seconds=60)
        with runtime.gemini_call_slot("stock_vision_table"):
            pass
        with runtime.gemini_call_slot("stock_vision_table_recon_recovery"):
            pass
        runtime.note_gemini_provider_429(
            model="m", label="stock_vision_table", attempt=1
        )
        runtime.note_sales_gemini_cooldown_wait(12_500.0)
        runtime.clear_sales_deadline()

        with self.assertLogs(
            "services.sales_extraction_runtime", level=logging.INFO
        ) as captured:
            runtime.log_sales_extraction_event(
                "req-summary",
                filename="page_1.jpg",
                file_type="image_multipage",
                page_count=2,
                total_duration_seconds=105.873,
                extraction_duration_seconds=105.866,
                final_status="completed",
            )

        summary_lines = [
            line for line in captured.output if "EXTRACTION_SUMMARY" in line
        ]
        self.assertEqual(len(summary_lines), 1)
        line = summary_lines[0]
        self.assertIn("request_id=req-summary", line)
        self.assertIn("page_count=2", line)
        self.assertIn("total_seconds=105.873", line)
        self.assertIn("gemini_calls=2", line)
        self.assertIn("gemini_429_count=1", line)
        self.assertIn("gemini_retry_count=1", line)
        self.assertIn("gemini_cooldown_seconds=12.5", line)
        self.assertIn("gemini_seconds=", line)
        self.assertIn("recovery_seconds=", line)
        self.assertIn("status=completed", line)
        # Must not fabricate missing counters as zeros when stats exist.
        self.assertNotIn("gemini_calls=None", line)

    def test_429_retry_counted_once_not_per_cooldown_chunk(self):
        runtime.start_sales_deadline("req-retry-once", limit_seconds=60)
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise GeminiProviderError(429, "RESOURCE_EXHAUSTED")
            return {"ok": True}

        with patch.object(
            runtime, "_compute_429_cooldown_seconds", return_value=0.12
        ), patch(
            "services.vertex_gemini_client.generate_content_via_vertex",
            side_effect=flaky,
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):
            result = runtime.sales_generate_content_via_vertex(
                model="gemini-2.5-flash-lite",
                payload={"contents": []},
                timeout=5,
                label="stock_vision_table",
            )
        self.assertEqual(result, {"ok": True})
        runtime.clear_sales_deadline()
        stats = runtime.get_last_runtime_stats("req-retry-once") or {}
        self.assertEqual(stats.get("gemini_429_count"), 1)
        self.assertEqual(stats.get("gemini_retries"), 1)
        # Two slot acquisitions: failed attempt + successful retry.
        self.assertEqual(stats.get("gemini_calls"), 2)
        self.assertGreaterEqual(float(stats.get("gemini_cooldown_seconds") or 0), 0.05)

    def test_recovery_label_accumulates_recovery_seconds(self):
        runtime.start_sales_deadline("req-recovery", limit_seconds=30)
        runtime.note_sales_gemini_call(
            5500.0, label="stock_vision_table_recon_recovery"
        )
        runtime.clear_sales_deadline()
        stats = runtime.get_last_runtime_stats("req-recovery") or {}
        self.assertEqual(stats.get("gemini_calls"), 1)
        self.assertEqual(stats.get("recovery_seconds"), 5.5)


if __name__ == "__main__":
    unittest.main()
