"""Concurrency hardening for Secondary Sales extraction (mocks only)."""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
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


class SalesAdmissionControlTests(unittest.TestCase):
    def setUp(self):
        runtime.sales_extraction_slot_sync._sem = threading.Semaphore(  # type: ignore[attr-defined]
            runtime.MAX_CONCURRENT_EXTRACTIONS
        )
        with runtime._sales_extraction_waiters_lock:
            runtime._sales_extraction_waiting = 0
            runtime._sales_extraction_active = 0

    def test_third_request_waits_until_slot_frees(self):
        self.assertEqual(runtime.MAX_CONCURRENT_EXTRACTIONS, 2)

        active = 0
        max_active = 0
        lock = threading.Lock()
        hold = threading.Event()
        two_in = threading.Event()
        finished = []

        def worker(idx: int):
            nonlocal active, max_active
            with runtime.sales_extraction_slot_sync(timeout=5.0):
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                    if active >= 2:
                        two_in.set()
                try:
                    hold.wait(timeout=5)
                finally:
                    with lock:
                        active -= 1
                        finished.append(idx)

        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True)
            for i in range(3)
        ]
        for t in threads:
            t.start()

        self.assertTrue(two_in.wait(timeout=3))
        with lock:
            self.assertEqual(max_active, 2)
            self.assertEqual(active, 2)

        hold.set()
        for t in threads:
            t.join(timeout=5)
            self.assertFalse(t.is_alive())
        self.assertEqual(sorted(finished), [0, 1, 2])
        self.assertLessEqual(max_active, 2)


class SalesEndpointConcurrencyTests(unittest.TestCase):
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

    def test_event_loop_health_while_slow_extraction(self):
        started = threading.Event()
        release = threading.Event()

        def slow_extract(file_bytes, filename):
            started.set()
            release.wait(timeout=10)
            return _ok_sales_result()

        async def run_case():
            from httpx import ASGITransport, AsyncClient

            transport = ASGITransport(app=self.app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                with patch.object(
                    self.app_module,
                    "extract_sales_statement",
                    side_effect=slow_extract,
                ):
                    task = asyncio.create_task(
                        client.post(
                            "/extract-sales-statement",
                            files={
                                "file": (
                                    "stmt.xlsx",
                                    b"fake-bytes",
                                    "application/vnd.ms-excel",
                                )
                            },
                        )
                    )
                    # Wait until worker thread has entered extract.
                    deadline = time.time() + 5
                    while time.time() < deadline and not started.is_set():
                        await asyncio.sleep(0.02)
                    self.assertTrue(started.is_set())
                    health = await asyncio.wait_for(
                        client.get("/health"), timeout=2
                    )
                    self.assertEqual(health.status_code, 200)
                    self.assertEqual(health.json().get("status"), "healthy")
                    release.set()
                    resp = await asyncio.wait_for(task, timeout=10)
                    self.assertEqual(resp.status_code, 200)

        asyncio.run(run_case())

    def test_max_two_extractions_concurrent_via_endpoint(self):
        active = 0
        max_active = 0
        lock = threading.Lock()
        hold = threading.Event()
        entered = threading.Event()

        def slow_extract(file_bytes, filename):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                if active >= 2:
                    entered.set()
            hold.wait(timeout=5)
            with lock:
                active -= 1
            return _ok_sales_result()

        async def run_case():
            from httpx import ASGITransport, AsyncClient

            transport = ASGITransport(app=self.app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                with patch.object(
                    self.app_module,
                    "extract_sales_statement",
                    side_effect=slow_extract,
                ):
                    tasks = [
                        asyncio.create_task(
                            client.post(
                                "/extract-sales-statement",
                                files={
                                    "file": (
                                        f"stmt{i}.xlsx",
                                        b"fake",
                                        "application/vnd.ms-excel",
                                    )
                                },
                            )
                        )
                        for i in range(3)
                    ]
                    # Wait until two pipelines are inside extract.
                    deadline = time.time() + 5
                    while time.time() < deadline and not entered.is_set():
                        await asyncio.sleep(0.02)
                    with lock:
                        self.assertEqual(max_active, 2)
                        self.assertEqual(active, 2)
                    hold.set()
                    results = await asyncio.gather(*tasks)
                    for resp in results:
                        self.assertEqual(resp.status_code, 200)
                    self.assertLessEqual(max_active, 2)

        asyncio.run(run_case())

    def test_extraction_failed_returns_422(self):
        failed = _ok_sales_result(
            extraction_failed=True,
            extraction_quality={"reasons": ["mock_gate"]},
            gemini_fallback={"attempted": True},
        )
        with patch.object(
            self.app_module, "extract_sales_statement", return_value=failed
        ):
            client = TestClient(self.app)
            resp = client.post(
                "/extract-sales-statement",
                files={
                    "file": ("bad.xlsx", b"fake", "application/vnd.ms-excel")
                },
            )
            self.assertEqual(resp.status_code, 422)
            body = resp.json()
            detail = body.get("detail") or body
            self.assertEqual(detail.get("error"), "extraction_failed")

    def test_status_endpoint_tracks_request_id(self):
        with patch.object(
            self.app_module,
            "extract_sales_statement",
            return_value=_ok_sales_result(),
        ):
            client = TestClient(self.app)
            resp = client.post(
                "/extract-sales-statement",
                files={
                    "file": ("ok.xlsx", b"fake", "application/vnd.ms-excel")
                },
                headers={"X-Request-ID": "sales-req-test-1"},
            )
            self.assertEqual(resp.status_code, 200)
            status = client.get(
                "/extract-sales-statement/status/sales-req-test-1"
            )
            self.assertEqual(status.status_code, 200)
            payload = status.json()
            self.assertEqual(payload.get("request_id"), "sales-req-test-1")
            self.assertEqual(payload.get("final_status"), "completed")


class TesseractSemaphoreTests(unittest.TestCase):
    def test_gated_calls_respect_global_tesseract_limit(self):
        try:
            import app as app_module
        except Exception:
            self.skipTest("app not importable")

        self.assertEqual(app_module.MAX_TESSERACT_CONCURRENCY, 6)

        active = 0
        max_active = 0
        lock = threading.Lock()
        original_slot = app_module.tesseract_ocr_slot

        @contextmanager
        def counting_slot(task_label="tesseract_ocr"):
            nonlocal active, max_active
            with original_slot(task_label):
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    time.sleep(0.03)
                    yield
                finally:
                    with lock:
                        active -= 1

        real = SimpleNamespace(
            image_to_string=lambda *a, **k: "ok",
            image_to_data=lambda *a, **k: {"text": []},
            Output=SimpleNamespace(DICT="dict"),
        )
        gated = runtime.wrap_pytesseract_module(real)

        with patch.object(app_module, "tesseract_ocr_slot", counting_slot):
            threads = [
                threading.Thread(
                    target=lambda: gated.image_to_string(None), daemon=True
                )
                for _ in range(12)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)
                self.assertFalse(t.is_alive())
        self.assertLessEqual(max_active, 6)
        self.assertGreaterEqual(max_active, 1)


class GeminiSemaphoreTests(unittest.TestCase):
    def setUp(self):
        runtime._gemini_call_semaphore = threading.Semaphore(
            runtime.MAX_CONCURRENT_GEMINI_REQUESTS
        )
        with runtime._gemini_call_lock:
            runtime._gemini_call_active = 0
            runtime._gemini_call_waiting = 0

    def test_gemini_call_slot_caps_at_two(self):
        self.assertEqual(runtime.MAX_CONCURRENT_GEMINI_REQUESTS, 2)
        active = 0
        max_active = 0
        lock = threading.Lock()
        hold = threading.Event()
        two_in = threading.Event()

        def worker():
            nonlocal active, max_active
            with runtime.gemini_call_slot("test"):
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                    if active >= 2:
                        two_in.set()
                hold.wait(timeout=5)
                with lock:
                    active -= 1

        threads = [
            threading.Thread(target=worker, daemon=True) for _ in range(4)
        ]
        for t in threads:
            t.start()
        self.assertTrue(two_in.wait(timeout=3))
        with lock:
            self.assertEqual(max_active, 2)
            self.assertEqual(active, 2)
        hold.set()
        for t in threads:
            t.join(timeout=5)
            self.assertFalse(t.is_alive())
        self.assertLessEqual(max_active, 2)

    def test_call_gemini_with_quota_remains_quota_owner(self):
        import app as app_module

        calls = {"vertex": 0}
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_generate(**kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
                calls["vertex"] += 1
            return {"ok": True}

        with patch.object(
            app_module,
            "acquire_model_slot_with_wait",
            return_value={"name": "m"},
        ), patch.object(
            app_module, "generate_content_via_vertex", side_effect=fake_generate
        ), patch.object(
            app_module, "update_request_progress", MagicMock()
        ), patch.object(app_module, "MAX_WAIT_TIME", 30):
            threads = [
                threading.Thread(
                    target=lambda: app_module.call_gemini_with_quota(
                        "model",
                        {"contents": []},
                        timeout=5,
                        request_type="vision",
                    ),
                    daemon=True,
                )
                for _ in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)
                self.assertFalse(t.is_alive())
        self.assertEqual(calls["vertex"], 4)
        self.assertLessEqual(max_active, runtime.MAX_CONCURRENT_GEMINI_REQUESTS)

    def test_gemini_429_retry_behavior_unchanged(self):
        import app as app_module
        from services.vertex_gemini_client import GeminiProviderError

        attempts = {"n": 0}

        def flaky_generate(**kwargs):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise GeminiProviderError(429, "rate limited")
            return {"ok": True}

        with patch.object(
            app_module,
            "acquire_model_slot_with_wait",
            return_value={"name": "m"},
        ), patch.object(
            app_module, "generate_content_via_vertex", side_effect=flaky_generate
        ), patch.object(
            app_module, "update_request_progress", MagicMock()
        ), patch.object(
            app_module, "release_gemini_inflight_counter", MagicMock()
        ), patch.object(
            app_module, "_log_quota_wait", MagicMock()
        ), patch.object(
            app_module, "MAX_WAIT_TIME", 30
        ), patch.object(
            app_module, "GEMINI_PROVIDER_BACKOFF_MAX_SECONDS", 1
        ), patch("time.sleep", MagicMock()):
            result = app_module.call_gemini_with_quota(
                "model", {"contents": []}, timeout=5, request_type="vision"
            )
        self.assertEqual(result, {"ok": True})
        self.assertGreaterEqual(attempts["n"], 2)

    def test_gemini_timeout_returns_none(self):
        import app as app_module

        with patch.object(
            app_module,
            "acquire_model_slot_with_wait",
            return_value={"name": "m"},
        ), patch.object(
            app_module,
            "generate_content_via_vertex",
            side_effect=TimeoutError("timeout"),
        ), patch.object(
            app_module, "update_request_progress", MagicMock()
        ), patch.object(
            app_module, "release_gemini_inflight_counter", MagicMock()
        ), patch.object(app_module, "MAX_WAIT_TIME", 30):
            result = app_module.call_gemini_with_quota(
                "model", {"contents": []}, timeout=5, request_type="vision"
            )
        self.assertIsNone(result)


class ConfigDefaultsTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(runtime.MAX_CONCURRENT_EXTRACTIONS, 2)
        self.assertEqual(runtime.MAX_CONCURRENT_GEMINI_REQUESTS, 2)
        self.assertEqual(runtime.SALES_EXTRACTION_QUEUE_TIMEOUT, 3600)


if __name__ == "__main__":
    unittest.main()
