"""Performance / self-recovery bounds for Secondary Sales (mocks only)."""

from __future__ import annotations

import threading
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services import sales_extraction_runtime as runtime
from services.extraction_quality import assess_source_text_quality


class OcrCacheAndBudgetTests(unittest.TestCase):
    def tearDown(self):
        runtime.clear_sales_deadline()

    def test_duplicate_ocr_input_is_cached(self):
        calls = {"n": 0}

        def fake_image_to_string(*a, **k):
            calls["n"] += 1
            return "OPENING RECEIPTS SALES CLOSING"

        real = SimpleNamespace(
            image_to_string=fake_image_to_string,
            image_to_data=lambda *a, **k: {"text": []},
        )
        gated = runtime.wrap_pytesseract_module(real)
        runtime.start_sales_deadline("req-cache", limit_seconds=60)

        class FakeImage:
            def __init__(self):
                self.size = (10, 10)
                self.mode = "L"

            def tobytes(self):
                return b"\x00" * 100

        img = FakeImage()
        with patch.object(runtime, "sales_tesseract_slot") as slot, patch.object(
            runtime, "run_bounded_sales_tesseract", side_effect=lambda fn, label: fn()
        ), patch.object(runtime, "mark_sales_stage", MagicMock()):
            @contextmanager
            def _slot(_label="t"):
                yield

            slot.side_effect = _slot
            a = gated.image_to_string(img, config="--psm 6")
            b = gated.image_to_string(img, config="--psm 6")
        self.assertEqual(a, b)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(runtime.sales_ocr_calls(), 1)
        self.assertGreaterEqual(
            int(getattr(runtime._deadline_local, "ocr_cache_hits", 0) or 0), 1
        )

    def test_ocr_budget_stops_new_work(self):
        real = SimpleNamespace(
            image_to_string=lambda *a, **k: "x",
            image_to_data=lambda *a, **k: {"text": []},
        )
        gated = runtime.wrap_pytesseract_module(real)
        runtime.start_sales_deadline("req-budget", limit_seconds=60)
        runtime._deadline_local.ocr_budget = 1
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

    def test_cancellation_stops_new_ocr(self):
        real = SimpleNamespace(
            image_to_string=lambda *a, **k: "x",
            image_to_data=lambda *a, **k: {"text": []},
        )
        gated = runtime.wrap_pytesseract_module(real)
        runtime.start_sales_deadline("req-cancel", limit_seconds=60)
        runtime.request_cancel_sales_extraction("req-cancel")
        with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
            gated.image_to_string(b"abc", config="--psm 6")

    def test_sleep_respects_deadline(self):
        runtime.start_sales_deadline("req-sleep", limit_seconds=0.05)
        time.sleep(0.06)
        with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
            runtime.sales_sleep_respecting_deadline(5.0, "gemini_retry")


class EarlyVisionDecisionTests(unittest.TestCase):
    def test_garbage_ocr_requests_fallback(self):
        quality = assess_source_text_quality("@@@ ### $$$")
        self.assertTrue(quality["should_fallback"])

    def test_stock_headers_keep_ocr_path(self):
        text = (
            "OPENING RECEIPTS SALES CLOSING\n"
            "ABANA TAB 10 0 2 8\n"
            "ARJUNA TAB 5 1 1 5\n"
        ) * 4
        quality = assess_source_text_quality(text)
        self.assertFalse(quality["should_fallback"])

    def test_bad_ocr_invokes_gemini_early(self):
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

    def test_good_ocr_does_not_invoke_gemini_early(self):
        from services import sales_statement_extractor as sse

        good = (
            "OPENING RECEIPTS SALES CLOSING\n"
            "ABANA TAB 10 0 2 8\n" * 5
        )
        with patch.object(sse, "_ocr_image_to_text", return_value=good), patch(
            "services.gemini_extraction_fallback.try_gemini_vision_extract"
        ) as vision, patch.object(
            sse, "_image_known_ocr_native_format", return_value=False
        ):
            early, skip = sse._maybe_early_vision_for_image(b"img", "good.jpg", ".jpg")
        self.assertFalse(skip)
        self.assertIsNone(early)
        vision.assert_not_called()

    def test_known_format_keeps_ocr_probes(self):
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


class SemaphoreReleaseTests(unittest.TestCase):
    def setUp(self):
        runtime._gemini_call_semaphore = threading.Semaphore(
            runtime.MAX_CONCURRENT_GEMINI_REQUESTS
        )
        with runtime._gemini_call_lock:
            runtime._gemini_call_active = 0
            runtime._gemini_call_waiting = 0

    def tearDown(self):
        runtime.clear_sales_deadline()
        with runtime._gemini_call_lock:
            runtime._gemini_call_active = 0
            runtime._gemini_call_waiting = 0
        runtime._gemini_call_semaphore = threading.Semaphore(
            runtime.MAX_CONCURRENT_GEMINI_REQUESTS
        )

    def test_gemini_slot_released_on_deadline(self):
        runtime.start_sales_deadline("req-g", limit_seconds=0.01)
        time.sleep(0.02)
        with self.assertRaises(runtime.SalesExtractionDeadlineExceeded):
            with runtime.gemini_call_slot("gemini_test"):
                pass
        with runtime._gemini_call_lock:
            self.assertEqual(runtime._gemini_call_active, 0)

    def test_gemini_slot_released_after_success(self):
        runtime.start_sales_deadline("req-g2", limit_seconds=30)
        with runtime.gemini_call_slot("gemini_ok"):
            self.assertEqual(runtime._gemini_call_active, 1)
        with runtime._gemini_call_lock:
            self.assertEqual(runtime._gemini_call_active, 0)

    def test_two_gemini_calls_max_concurrent(self):
        runtime.start_sales_deadline("req-g3", limit_seconds=30)
        entered = threading.Event()
        release = threading.Event()
        max_active = 0
        lock = threading.Lock()

        def worker():
            nonlocal max_active
            with runtime.gemini_call_slot("gemini_parallel"):
                with lock:
                    max_active = max(max_active, runtime._gemini_call_active)
                entered.set()
                release.wait(2)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        with runtime._gemini_call_lock:
            # Third should be waiting; active capped at MAX
            self.assertLessEqual(runtime._gemini_call_active, runtime.MAX_CONCURRENT_GEMINI_REQUESTS)
        release.set()
        for t in threads:
            t.join(timeout=3)
        self.assertLessEqual(max_active, runtime.MAX_CONCURRENT_GEMINI_REQUESTS)
        with runtime._gemini_call_lock:
            self.assertEqual(runtime._gemini_call_active, 0)

    def test_classify_document_types(self):
        self.assertEqual(runtime.classify_sales_document("a.jpg"), "image")
        self.assertEqual(runtime.classify_sales_document("a.xlsx"), "xlsx")
        self.assertEqual(runtime.classify_sales_document("a.xls"), "xls")
        self.assertEqual(runtime.classify_sales_document("a.docx"), "docx")
        self.assertEqual(runtime.classify_sales_document("a.pdf"), "pdf")


class QualityGateUnchangedTests(unittest.TestCase):
    def test_empty_extract_still_requests_fallback(self):
        from services.extraction_quality import evaluate_extraction_quality

        quality = evaluate_extraction_quality(
            {"line_items": [], "totals": {"extra": {"extraction_method": "gemini_vision"}}}
        )
        self.assertTrue(quality["should_fallback"])


if __name__ == "__main__":
    unittest.main()
