"""Vertex 429 RESOURCE_EXHAUSTED provider cooldown / circuit breaker tests.

Does not change extraction parsers — only reliability wrapper behavior.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from services import sales_extraction_runtime as runtime
from services.vertex_gemini_client import GeminiProviderError


class GeminiProvider429CooldownTests(unittest.TestCase):
    def setUp(self):
        runtime.reset_gemini_provider_cooldown_state_for_tests()
        runtime.clear_sales_deadline()

    def tearDown(self):
        runtime.reset_gemini_provider_cooldown_state_for_tests()
        runtime.clear_sales_deadline()

    def test_429_activates_cooldown_and_retry_succeeds(self):
        runtime.start_sales_deadline("req-429-ok", limit_seconds=60)
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise GeminiProviderError(429, "RESOURCE_EXHAUSTED")
            return {"ok": True}

        with patch.object(
            runtime, "_compute_429_cooldown_seconds", return_value=0.05
        ), patch(
            "services.vertex_gemini_client.generate_content_via_vertex",
            side_effect=flaky,
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):
            result = runtime.sales_generate_content_via_vertex(
                model="gemini-2.5-flash-lite",
                payload={"contents": []},
                timeout=5,
                label="test_vertex",
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls["n"], 2)
        metrics = runtime.get_gemini_provider_metrics()
        self.assertGreaterEqual(metrics["gemini_429_count"], 1)
        self.assertGreaterEqual(metrics["gemini_success_count"], 1)

    def test_repeated_429_exponential_backoff_capped(self):
        with patch(
            "services.sales_extraction_runtime.random.uniform", return_value=1.0
        ):
            delays = [
                runtime._compute_429_cooldown_seconds(1),
                runtime._compute_429_cooldown_seconds(2),
                runtime._compute_429_cooldown_seconds(3),
                runtime._compute_429_cooldown_seconds(8),
            ]
        self.assertGreaterEqual(delays[0], 8.0)
        self.assertLessEqual(delays[0], 20.0)
        self.assertGreater(delays[1], delays[0])
        self.assertGreater(delays[2], delays[1] * 0.9)
        self.assertLessEqual(
            delays[3], float(runtime.GEMINI_PROVIDER_429_MAX_COOLDOWN_SECONDS)
        )

    def test_shared_cooldown_blocks_second_request(self):
        runtime.start_sales_deadline("req-shared-a", limit_seconds=60)
        runtime.note_gemini_provider_429(
            model="m", label="a", attempt=1
        )
        # Force a short remaining cooldown window.
        with runtime._provider_cooldown_lock:
            runtime._provider_cooldown_until_mono = time.monotonic() + 0.3

        seen = {"called": False}

        def should_not_call_yet(**kwargs):
            seen["called"] = True
            return {"ok": True}

        started = time.monotonic()
        with patch(
            "services.vertex_gemini_client.generate_content_via_vertex",
            side_effect=should_not_call_yet,
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):
            runtime.sales_generate_content_via_vertex(
                model="m",
                payload={"contents": []},
                timeout=5,
                label="b",
            )
        elapsed = time.monotonic() - started
        self.assertTrue(seen["called"])
        self.assertGreaterEqual(elapsed, 0.2)

    def test_semaphore_limit_still_two(self):
        runtime.start_sales_deadline("req-sem", limit_seconds=60)
        active = []
        lock = threading.Lock()
        release = threading.Event()

        def blocking(**kwargs):
            with lock:
                active.append(runtime.gemini_active_slots())
            release.wait(timeout=2)
            return {"ok": True}

        threads = []
        with patch(
            "services.vertex_gemini_client.generate_content_via_vertex",
            side_effect=blocking,
        ), patch.object(runtime, "mark_sales_stage", MagicMock()), patch.object(
            runtime, "wait_gemini_provider_cooldown", MagicMock()
        ):
            for _ in range(4):
                t = threading.Thread(
                    target=lambda: runtime.sales_generate_content_via_vertex(
                        model="m",
                        payload={"contents": []},
                        timeout=5,
                        label="sem",
                    )
                )
                threads.append(t)
                t.start()
            time.sleep(0.2)
            with lock:
                peak = max(active) if active else 0
            release.set()
            for t in threads:
                t.join(timeout=5)
                self.assertFalse(t.is_alive())
        self.assertLessEqual(peak, runtime.MAX_CONCURRENT_GEMINI_REQUESTS)

    def test_cooldown_does_not_hold_semaphore(self):
        runtime.start_sales_deadline("req-slot-free", limit_seconds=60)
        slots_during_wait = []

        original_wait = runtime.wait_gemini_provider_cooldown

        def tracking_wait(**kwargs):
            slots_during_wait.append(runtime.gemini_active_slots())
            return original_wait(**kwargs)

        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise GeminiProviderError(429, "RESOURCE_EXHAUSTED")
            return {"ok": True}

        with patch.object(
            runtime, "_compute_429_cooldown_seconds", return_value=0.1
        ), patch(
            "services.vertex_gemini_client.generate_content_via_vertex",
            side_effect=flaky,
        ), patch.object(
            runtime, "wait_gemini_provider_cooldown", side_effect=tracking_wait
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):
            runtime.sales_generate_content_via_vertex(
                model="m",
                payload={"contents": []},
                timeout=5,
                label="free_slot",
            )
        # During cooldown waits, Gemini active slots must be 0.
        self.assertTrue(slots_during_wait)
        self.assertTrue(all(s == 0 for s in slots_during_wait))

    def test_cooldown_respects_deadline(self):
        runtime.start_sales_deadline("req-dl", limit_seconds=0.4)
        with runtime._provider_cooldown_lock:
            runtime._provider_cooldown_until_mono = time.monotonic() + 30.0
            runtime._provider_cooldown_active = True
        with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
            runtime.wait_gemini_provider_cooldown(label="dl", model="m")

    def test_retry_limit_stops_after_max_429s(self):
        runtime.start_sales_deadline("req-max", limit_seconds=60)
        calls = {"n": 0}

        def always_429(**kwargs):
            calls["n"] += 1
            raise GeminiProviderError(429, "RESOURCE_EXHAUSTED")

        with patch.object(
            runtime, "_compute_429_cooldown_seconds", return_value=0.01
        ), patch(
            "services.vertex_gemini_client.generate_content_via_vertex",
            side_effect=always_429,
        ), patch.object(runtime, "mark_sales_stage", MagicMock()), patch.object(
            runtime, "GEMINI_PROVIDER_429_MAX_RETRIES", 3
        ):
            with self.assertRaises(GeminiProviderError):
                runtime.sales_generate_content_via_vertex(
                    model="m",
                    payload={"contents": []},
                    timeout=5,
                    label="max",
                )
        self.assertEqual(calls["n"], 3)

    def test_successful_path_unchanged(self):
        runtime.start_sales_deadline("req-ok", limit_seconds=60)

        def ok(**kwargs):
            return {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}

        with patch(
            "services.vertex_gemini_client.generate_content_via_vertex",
            side_effect=ok,
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):
            result = runtime.sales_generate_content_via_vertex(
                model="m",
                payload={"contents": []},
                timeout=5,
                label="ok",
            )
        self.assertIn("candidates", result)
        self.assertEqual(runtime.get_gemini_provider_cooldown_remaining(), 0.0)


if __name__ == "__main__":
    unittest.main()
