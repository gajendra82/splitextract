"""Non-blocking POST /extract-sales-statement/submit (mocks only)."""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest.mock import patch

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


class SalesSubmitNonBlockingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module

        cls.app_module = app_module
        cls.app = app_module.app

    def setUp(self):
        runtime.reset_sales_background_runtime_for_tests()
        runtime._sales_extraction_async_sem = asyncio.Semaphore(
            runtime.MAX_CONCURRENT_EXTRACTIONS
        )
        with runtime._sales_extraction_waiters_lock:
            runtime._sales_extraction_waiting = 0
            runtime._sales_extraction_active = 0
        with runtime._progress_lock:
            runtime._sales_request_progress.clear()

    def tearDown(self):
        runtime.reset_sales_background_runtime_for_tests()

    def test_submit_returns_202_queued_immediately(self):
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
                    t0 = time.monotonic()
                    resp = await client.post(
                        "/extract-sales-statement/submit",
                        files={
                            "file": (
                                "stmt.xlsx",
                                b"fake-bytes",
                                "application/vnd.ms-excel",
                            )
                        },
                        headers={"X-Request-ID": "submit-fast-1"},
                    )
                    elapsed = time.monotonic() - t0
                    self.assertEqual(resp.status_code, 202)
                    self.assertLess(elapsed, 1.0)
                    body = resp.json()
                    self.assertTrue(body.get("accepted"))
                    self.assertEqual(body.get("request_id"), "submit-fast-1")
                    self.assertEqual(body.get("status"), "queued")
                    self.assertEqual(body.get("stage"), "queued")
                    self.assertIn("status_url", body)
                    self.assertFalse(started.is_set() and elapsed > 0.5)
                    # Background should eventually start without holding HTTP.
                    deadline = time.time() + 5
                    while time.time() < deadline and not started.is_set():
                        await asyncio.sleep(0.02)
                    self.assertTrue(started.is_set())
                    release.set()
                    # Wait for completed via status.
                    done = False
                    for _ in range(100):
                        status = await client.get(
                            "/extract-sales-statement/status/submit-fast-1"
                        )
                        self.assertEqual(status.status_code, 200)
                        payload = status.json()
                        if payload.get("final_status") == "completed":
                            done = True
                            self.assertEqual(payload.get("status"), "completed")
                            self.assertIsNotNone(payload.get("result"))
                            break
                        await asyncio.sleep(0.05)
                    self.assertTrue(done)

        asyncio.run(run_case())

    def test_submit_does_not_wait_for_ocr_or_gemini(self):
        ocr_entered = threading.Event()
        release = threading.Event()

        def slow_extract(file_bytes, filename):
            # Simulate OCR/Gemini holding the worker for a long time.
            ocr_entered.set()
            release.wait(timeout=15)
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
                    t0 = time.monotonic()
                    resp = await client.post(
                        "/extract-sales-statement/submit",
                        files={
                            "file": (
                                "scan.pdf",
                                b"%PDF-1.4 fake",
                                "application/pdf",
                            )
                        },
                        headers={"X-Request-ID": "submit-no-wait"},
                    )
                    elapsed = time.monotonic() - t0
                    self.assertEqual(resp.status_code, 202)
                    self.assertLess(elapsed, 0.75)
                    # HTTP returned before (or without requiring) OCR finish.
                    self.assertFalse(release.is_set())
                    deadline = time.time() + 5
                    while time.time() < deadline and not ocr_entered.is_set():
                        await asyncio.sleep(0.02)
                    self.assertTrue(ocr_entered.is_set())
                    release.set()
                    for _ in range(100):
                        status = await client.get(
                            "/extract-sales-statement/status/submit-no-wait"
                        )
                        if status.json().get("final_status"):
                            break
                        await asyncio.sleep(0.05)

        asyncio.run(run_case())

    def test_submit_respects_max_concurrent_extractions(self):
        self.assertEqual(runtime.MAX_CONCURRENT_EXTRACTIONS, 2)
        active = 0
        max_active = 0
        lock = threading.Lock()
        hold = threading.Event()
        two_in = threading.Event()
        started_ids = []

        def slow_extract(file_bytes, filename):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                started_ids.append(filename)
                if active >= 2:
                    two_in.set()
            hold.wait(timeout=8)
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
                    responses = []
                    for i in range(3):
                        t0 = time.monotonic()
                        resp = await client.post(
                            "/extract-sales-statement/submit",
                            files={
                                "file": (
                                    f"stmt{i}.xlsx",
                                    b"fake",
                                    "application/vnd.ms-excel",
                                )
                            },
                            headers={"X-Request-ID": f"submit-conc-{i}"},
                        )
                        self.assertLess(time.monotonic() - t0, 1.0)
                        self.assertEqual(resp.status_code, 202)
                        responses.append(resp.json())

                    self.assertTrue(two_in.wait(timeout=5))
                    await asyncio.sleep(0.2)
                    with lock:
                        self.assertEqual(max_active, 2)
                        self.assertEqual(active, 2)
                        self.assertEqual(len(started_ids), 2)

                    hold.set()
                    # Third eventually runs and all complete.
                    for i in range(3):
                        done = False
                        for _ in range(150):
                            status = await client.get(
                                f"/extract-sales-statement/status/submit-conc-{i}"
                            )
                            if status.json().get("final_status") == "completed":
                                done = True
                                break
                            await asyncio.sleep(0.05)
                        self.assertTrue(done, f"request submit-conc-{i}")
                    self.assertLessEqual(max_active, 2)

        asyncio.run(run_case())

    def test_submit_idempotent_same_request_id(self):
        hold = threading.Event()

        def slow_extract(file_bytes, filename):
            hold.wait(timeout=10)
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
                    r1 = await client.post(
                        "/extract-sales-statement/submit",
                        files={
                            "file": (
                                "a.xlsx",
                                b"one",
                                "application/vnd.ms-excel",
                            )
                        },
                        headers={"X-Request-ID": "submit-idem-1"},
                    )
                    r2 = await client.post(
                        "/extract-sales-statement/submit",
                        files={
                            "file": (
                                "b.xlsx",
                                b"two",
                                "application/vnd.ms-excel",
                            )
                        },
                        headers={"X-Request-ID": "submit-idem-1"},
                    )
                    self.assertEqual(r1.status_code, 202)
                    self.assertEqual(r2.status_code, 202)
                    self.assertTrue(r2.json().get("already_queued"))
                    hold.set()
                    for _ in range(100):
                        status = await client.get(
                            "/extract-sales-statement/status/submit-idem-1"
                        )
                        if status.json().get("final_status"):
                            break
                        await asyncio.sleep(0.05)

        asyncio.run(run_case())

    def test_submit_extraction_failed_reaches_failed(self):
        failed = _ok_sales_result(
            extraction_failed=True,
            extraction_quality={"reasons": ["mock_gate"]},
            gemini_fallback={"attempted": True},
        )

        async def run_case():
            from httpx import ASGITransport, AsyncClient

            transport = ASGITransport(app=self.app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                with patch.object(
                    self.app_module,
                    "extract_sales_statement",
                    return_value=failed,
                ):
                    resp = await client.post(
                        "/extract-sales-statement/submit",
                        files={
                            "file": (
                                "bad.xlsx",
                                b"fake",
                                "application/vnd.ms-excel",
                            )
                        },
                        headers={"X-Request-ID": "submit-fail-1"},
                    )
                    self.assertEqual(resp.status_code, 202)
                    payload = None
                    for _ in range(100):
                        status = await client.get(
                            "/extract-sales-statement/status/submit-fail-1"
                        )
                        payload = status.json()
                        if payload.get("final_status"):
                            break
                        await asyncio.sleep(0.05)
                    self.assertIsNotNone(payload)
                    self.assertEqual(payload.get("status"), "failed")
                    self.assertEqual(
                        payload.get("final_status"), "extraction_failed"
                    )
                    err = payload.get("error") or {}
                    self.assertEqual(err.get("error"), "extraction_failed")

        asyncio.run(run_case())

    def test_submit_deadline_exceeded_reaches_failed(self):
        async def run_case():
            from httpx import ASGITransport, AsyncClient

            def raising_runner(*args, **kwargs):
                raise runtime.SalesExtractionDeadlineExceeded(
                    "submit-deadline-1", "ocr", 1.0
                )

            transport = ASGITransport(app=self.app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                with patch.object(
                    runtime,
                    "run_sales_extract_with_deadline",
                    side_effect=raising_runner,
                ):
                    resp = await client.post(
                        "/extract-sales-statement/submit",
                        files={
                            "file": (
                                "late.xlsx",
                                b"fake",
                                "application/vnd.ms-excel",
                            )
                        },
                        headers={"X-Request-ID": "submit-deadline-1"},
                    )
                    self.assertEqual(resp.status_code, 202)
                    payload = None
                    for _ in range(100):
                        status = await client.get(
                            "/extract-sales-statement/status/submit-deadline-1"
                        )
                        payload = status.json()
                        if payload.get("final_status"):
                            break
                        await asyncio.sleep(0.05)
                    self.assertIsNotNone(payload)
                    self.assertEqual(payload.get("status"), "failed")
                    self.assertEqual(
                        payload.get("final_status"), "extraction_failed"
                    )
                    err = payload.get("error") or {}
                    self.assertIn(
                        "extraction_deadline_exceeded",
                        err.get("reasons") or [],
                    )

        asyncio.run(run_case())

    def test_sync_endpoint_still_works(self):
        with patch.object(
            self.app_module,
            "extract_sales_statement",
            return_value=_ok_sales_result(),
        ):
            from fastapi.testclient import TestClient

            client = TestClient(self.app)
            resp = client.post(
                "/extract-sales-statement",
                files={
                    "file": ("ok.xlsx", b"fake", "application/vnd.ms-excel")
                },
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body.get("stockist_name"), "Mock Stockist")


if __name__ == "__main__":
    unittest.main()
