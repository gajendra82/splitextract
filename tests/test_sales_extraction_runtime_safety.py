"""Runtime safety / reliability for Secondary Sales (mocks only).

Proves deadlines, cancellation, semaphore release, OCR budget/cache,
Gemini slot gating, and terminal status — without changing parsers.
"""

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


class DeadlineAndCancelTests(unittest.TestCase):
    def tearDown(self):
        runtime.clear_sales_deadline()

    def test_deadline_exceeded_during_ocr(self):
        runtime.start_sales_deadline("req-ocr-dl", limit_seconds=0.01)
        time.sleep(0.02)
        with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
            runtime.check_sales_deadline("ocr")

    def test_deadline_exceeded_before_gemini(self):
        runtime.start_sales_deadline("req-gem-dl", limit_seconds=0.01)
        time.sleep(0.02)
        with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
            with runtime.gemini_call_slot("gemini_precheck"):
                pass

    def test_gemini_429_near_deadline_stops(self):
        runtime.start_sales_deadline("req-429", limit_seconds=0.05)
        time.sleep(0.06)
        with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
            runtime.sales_sleep_respecting_deadline(5.0, "gemini_retry")

    def test_gemini_503_near_deadline_stops(self):
        runtime.start_sales_deadline("req-503", limit_seconds=0.05)
        time.sleep(0.06)
        with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
            runtime.sales_sleep_respecting_deadline(8.0, "gemini_retry")

    def test_cancellation_during_ocr(self):
        runtime.start_sales_deadline("req-c-ocr", limit_seconds=60)
        runtime.request_cancel_sales_extraction("req-c-ocr")
        with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
            runtime.check_sales_deadline("ocr")

    def test_cancellation_before_gemini(self):
        runtime.start_sales_deadline("req-c-gem", limit_seconds=60)
        runtime.request_cancel_sales_extraction("req-c-gem")
        with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
            with runtime.gemini_call_slot("gemini_cancel"):
                pass

    def test_pdf_page_loop_deadline(self):
        runtime.start_sales_deadline("req-pdf", limit_seconds=0.01)
        time.sleep(0.02)
        with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
            runtime.check_sales_deadline("pdf_processing")

    def test_runtime_context_shared(self):
        runtime.start_sales_deadline("req-ctx", limit_seconds=30, file_size=123)
        ctx = runtime.get_sales_runtime_context()
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.request_id, "req-ctx")
        self.assertEqual(ctx.file_size, 123)
        self.assertGreater(ctx.remaining_seconds(), 0)


class OcrBudgetCacheTests(unittest.TestCase):
    def tearDown(self):
        runtime.clear_sales_deadline()

    def test_ocr_budget_exhausted(self):
        real = SimpleNamespace(
            image_to_string=lambda *a, **k: "x",
            image_to_data=lambda *a, **k: {"text": []},
        )
        gated = runtime.wrap_pytesseract_module(real)
        runtime.start_sales_deadline("req-bud", limit_seconds=60)
        runtime._deadline_local.ocr_budget = 1
        ctx = runtime.get_sales_runtime_context()
        if ctx:
            ctx.ocr_budget = 1
        with patch.object(runtime, "sales_tesseract_slot") as slot, patch.object(
            runtime, "run_bounded_sales_tesseract", side_effect=lambda fn, label: fn()
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):

            @contextmanager
            def _slot(_label="t"):
                yield

            slot.side_effect = _slot
            gated.image_to_string(b"abc", config="--psm 6")
            with self.assertRaises(runtime.SalesOcrBudgetExceeded):
                gated.image_to_string(b"def", config="--psm 6")

    def test_ocr_cache_prevents_duplicate_calls(self):
        calls = {"n": 0}

        def fake_image_to_string(*a, **k):
            calls["n"] += 1
            return "OPENING"

        real = SimpleNamespace(
            image_to_string=fake_image_to_string,
            image_to_data=lambda *a, **k: {"text": []},
        )
        gated = runtime.wrap_pytesseract_module(real)
        runtime.start_sales_deadline("req-cache2", limit_seconds=60)

        class FakeImage:
            size = (8, 8)
            mode = "L"

            def tobytes(self):
                return b"\x01" * 64

        img = FakeImage()
        with patch.object(runtime, "sales_tesseract_slot") as slot, patch.object(
            runtime, "run_bounded_sales_tesseract", side_effect=lambda fn, label: fn()
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):

            @contextmanager
            def _slot(_label="t"):
                yield

            slot.side_effect = _slot
            gated.image_to_string(img, config="--psm 6")
            gated.image_to_string(img, config="--psm 6")
        self.assertEqual(calls["n"], 1)

    def test_ocr_timeout_releases_slot(self):
        active = 0
        lock = threading.Lock()

        @contextmanager
        def fake_slot(label="tesseract_ocr"):
            nonlocal active
            with lock:
                active += 1
            try:
                yield
            finally:
                with lock:
                    active -= 1

        real = SimpleNamespace(
            image_to_string=lambda *a, **k: (time.sleep(3) or "never"),
            image_to_data=lambda *a, **k: {"text": []},
        )
        gated = runtime.wrap_pytesseract_module(real)
        runtime.start_sales_deadline("req-tess-to", limit_seconds=30)
        with patch.object(runtime, "sales_tesseract_slot", fake_slot), patch.object(
            runtime, "SALES_TESSERACT_CALL_TIMEOUT_SECONDS", 1
        ), patch.object(
            runtime, "terminate_sales_tesseract_children", MagicMock()
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):
            with self.assertRaises(TimeoutError):
                gated.image_to_string(None)
        self.assertEqual(active, 0)


class SemaphoreReleaseTests(unittest.TestCase):
    def setUp(self):
        runtime._gemini_call_semaphore = threading.Semaphore(
            runtime.MAX_CONCURRENT_GEMINI_REQUESTS
        )
        with runtime._gemini_call_lock:
            runtime._gemini_call_active = 0
            runtime._gemini_call_waiting = 0
        runtime._sales_extraction_async_sem = asyncio.Semaphore(
            runtime.MAX_CONCURRENT_EXTRACTIONS
        )
        with runtime._sales_extraction_waiters_lock:
            runtime._sales_extraction_waiting = 0
            runtime._sales_extraction_active = 0
        runtime.sales_extraction_slot_sync._sem = threading.Semaphore(  # type: ignore
            runtime.MAX_CONCURRENT_EXTRACTIONS
        )

    def tearDown(self):
        runtime.clear_sales_deadline()

    def test_extraction_semaphore_released_after_failure(self):
        with self.assertRaises(RuntimeError):
            with runtime.sales_extraction_slot_sync(timeout=2):
                raise RuntimeError("boom")
        with runtime._sales_extraction_waiters_lock:
            self.assertEqual(runtime._sales_extraction_active, 0)
        # Next acquire must succeed immediately.
        with runtime.sales_extraction_slot_sync(timeout=1) as waited:
            self.assertLess(waited, 1.0)

    def test_gemini_semaphore_released_after_failure(self):
        runtime.start_sales_deadline("req-gsem", limit_seconds=30)
        with self.assertRaises(RuntimeError):
            with runtime.gemini_call_slot("gemini_fail"):
                raise RuntimeError("provider down")
        with runtime._gemini_call_lock:
            self.assertEqual(runtime._gemini_call_active, 0)

    def test_tesseract_semaphore_released_after_failure(self):
        active = 0

        @contextmanager
        def fake_slot(label="t"):
            nonlocal active
            active += 1
            try:
                yield
            finally:
                active -= 1

        with patch.object(runtime, "sales_tesseract_slot", fake_slot), patch.object(
            runtime, "run_bounded_sales_tesseract", side_effect=RuntimeError("ocr boom")
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):
            runtime.start_sales_deadline("req-tsem", limit_seconds=30)
            gated = runtime.wrap_pytesseract_module(
                SimpleNamespace(image_to_string=lambda *a, **k: "x")
            )
            with self.assertRaises(RuntimeError):
                gated.image_to_string(b"x", config="--psm 6")
        self.assertEqual(active, 0)

    def test_sales_vertex_wrapper_uses_gemini_slot(self):
        runtime.start_sales_deadline("req-wrap", limit_seconds=30)
        seen = {"in_slot": False}

        def fake_raw(*, model, payload, timeout):
            seen["in_slot"] = runtime._gemini_call_active >= 1
            return {"ok": True}

        with patch(
            "services.vertex_gemini_client.generate_content_via_vertex",
            side_effect=fake_raw,
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):
            out = runtime.sales_generate_content_via_vertex(
                model="m", payload={"contents": []}, timeout=10
            )
        self.assertEqual(out, {"ok": True})
        self.assertTrue(seen["in_slot"])
        with runtime._gemini_call_lock:
            self.assertEqual(runtime._gemini_call_active, 0)


class EarlyVisionAndFormatTests(unittest.TestCase):
    def test_poor_ocr_goes_to_vision_without_full_probe(self):
        from services import sales_statement_extractor as sse

        vision_result = {
            "stockist_name": "X",
            "line_items": [{"product_name": "A", "sales_qty": 1}],
            "totals": {"extra": {}},
        }
        with patch.object(sse, "_ocr_image_to_text", return_value="@@@"), patch(
            "services.gemini_extraction_fallback.try_gemini_vision_extract",
            return_value=vision_result,
        ) as vision, patch.object(
            sse, "_image_known_ocr_native_format", return_value=False
        ):
            early, skip = sse._maybe_early_vision_for_image(b"img", "bad.jpg", ".jpg")
        self.assertTrue(skip)
        self.assertEqual(early, vision_result)
        vision.assert_called_once()

    def test_known_format_does_not_unexpectedly_switch_parser(self):
        from services import sales_statement_extractor as sse

        with patch.object(sse, "_ocr_image_to_text", return_value="noise"), patch(
            "services.gemini_extraction_fallback.try_gemini_vision_extract"
        ) as vision, patch.object(
            sse, "_image_known_ocr_native_format", return_value=True
        ):
            early, skip = sse._maybe_early_vision_for_image(b"img", "known.jpg", ".jpg")
        self.assertFalse(skip)
        self.assertIsNone(early)
        vision.assert_not_called()

    def test_group_wise_detector_still_true(self):
        from services.sales_statement_extractor import _is_group_wise_sales_opstock_format

        text = "Group Wise Sales\nOp.Stock Purchase Sales Cl.Stock\nABANA 1 0 1 0"
        self.assertTrue(_is_group_wise_sales_opstock_format(text))


class EndpointTerminalTests(unittest.TestCase):
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

    def test_no_fake_http_200_on_extraction_failure(self):
        failed = _ok_sales_result(
            extraction_failed=True,
            extraction_quality={"reasons": ["empty_line_items"]},
        )
        with patch.object(
            self.app_module, "extract_sales_statement", return_value=failed
        ):
            client = TestClient(self.app)
            resp = client.post(
                "/extract-sales-statement",
                files={"file": ("bad.jpg", b"fake", "image/jpeg")},
            )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual((resp.json().get("detail") or {}).get("error"), "extraction_failed")

    def test_deadline_returns_422_and_releases_slot(self):
        def slow_extract(file_bytes, filename):
            time.sleep(2.0)
            return _ok_sales_result()

        with patch.object(
            self.app_module, "extract_sales_statement", side_effect=slow_extract
        ), patch(
            "services.sales_extraction_runtime.SALES_EXTRACTION_MAX_EXECUTION_SECONDS",
            1,
        ):
            client = TestClient(self.app)
            resp = client.post(
                "/extract-sales-statement",
                files={"file": ("slow.jpg", b"fake", "image/jpeg")},
            )
        self.assertEqual(resp.status_code, 422)
        detail = resp.json().get("detail") or {}
        self.assertEqual(detail.get("error"), "extraction_failed")
        self.assertIn("extraction_deadline_exceeded", detail.get("reasons") or [])
        with runtime._sales_extraction_waiters_lock:
            self.assertEqual(runtime._sales_extraction_active, 0)

    def test_unexpected_exception_becomes_terminal_failed(self):
        with patch.object(
            self.app_module,
            "extract_sales_statement",
            side_effect=RuntimeError("unexpected boom"),
        ):
            client = TestClient(self.app)
            resp = client.post(
                "/extract-sales-statement",
                files={"file": ("x.jpg", b"fake", "image/jpeg")},
            )
        self.assertEqual(resp.status_code, 500)
        with runtime._sales_extraction_waiters_lock:
            self.assertEqual(runtime._sales_extraction_active, 0)

    def test_request_status_terminal_after_success(self):
        with patch.object(
            self.app_module, "extract_sales_statement", return_value=_ok_sales_result()
        ):
            client = TestClient(self.app)
            resp = client.post(
                "/extract-sales-statement",
                files={"file": ("ok.jpg", b"fake", "image/jpeg")},
                headers={"X-Request-ID": "term-ok-1"},
            )
        self.assertEqual(resp.status_code, 200)
        prog = runtime.get_sales_progress("term-ok-1")
        self.assertIsNotNone(prog)
        self.assertIn(prog.get("status"), {"completed", "failed"})
        self.assertIn(prog.get("stage"), {"completed", "failed"})

    def test_event_loop_remains_responsive(self):
        """Offloaded blocking work must not starve the event loop."""
        from concurrent.futures import ThreadPoolExecutor

        async def _run():
            started = threading.Event()
            release = threading.Event()

            def blocking():
                started.set()
                release.wait(timeout=3)
                return "done"

            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=1) as pool:
                task = loop.run_in_executor(pool, blocking)
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(started.is_set())
                ticks = 0
                for _ in range(10):
                    ticks += 1
                    await asyncio.sleep(0)
                self.assertEqual(ticks, 10)
                release.set()
                self.assertEqual(await task, "done")

        asyncio.run(_run())


class NoOrphanProcessTests(unittest.TestCase):
    def tearDown(self):
        runtime.clear_sales_deadline()

    def test_terminate_tesseract_children_invoked_on_timeout(self):
        terminator = MagicMock()
        real = SimpleNamespace(
            image_to_string=lambda *a, **k: (time.sleep(3) or "x"),
            image_to_data=lambda *a, **k: {"text": []},
        )
        gated = runtime.wrap_pytesseract_module(real)
        runtime.start_sales_deadline("req-orphan", limit_seconds=30)
        with patch.object(runtime, "sales_tesseract_slot") as slot, patch.object(
            runtime, "SALES_TESSERACT_CALL_TIMEOUT_SECONDS", 1
        ), patch.object(
            runtime, "terminate_sales_tesseract_children", terminator
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):

            @contextmanager
            def _slot(_label="t"):
                yield

            slot.side_effect = _slot
            with self.assertRaises(TimeoutError):
                gated.image_to_string(None)
        terminator.assert_called()


if __name__ == "__main__":
    unittest.main()
