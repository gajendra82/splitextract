"""Unit tests for services.reliability (feature flags default off)."""

import os
import threading
import time
import unittest
from unittest.mock import patch

from services.reliability import (
    OCR_WATCHDOG_ENABLED,
    OCR_WATCHDOG_STUCK_SECONDS,
    get_enhanced_heartbeat_snapshot,
    get_heartbeat_age_seconds,
    get_ready_snapshot,
    increment_gemini_active,
    on_heartbeat_update,
    on_request_end,
    on_request_start,
    release_gemini_inflight_counter,
    run_tesseract_call,
    track_llm_stage,
    _watchdog_should_terminate,
)


class ReliabilityHeartbeatTests(unittest.TestCase):
    def tearDown(self):
        on_request_end()

    def test_request_lifecycle_updates_heartbeat(self):
        on_request_start("req-abc", "invoice.pdf", "request_admitted")
        on_heartbeat_update("pdf_opened", page=1, total_pages=5, invoice="INV-1")
        snap = get_enhanced_heartbeat_snapshot()
        self.assertTrue(snap["active"])
        self.assertEqual(snap["request_id"], "req-abc")
        self.assertEqual(snap["stage"], "pdf_opened")
        self.assertEqual(snap["page"], 1)
        self.assertEqual(snap["total_pages"], 5)
        self.assertEqual(snap["invoice"], "INV-1")
        self.assertIsNotNone(snap.get("heartbeat_age"))

        on_request_end({"pages_processed": 5, "total_execution_seconds": 12.0})
        snap = get_enhanced_heartbeat_snapshot()
        self.assertFalse(snap["active"])

    def test_heartbeat_age_increases_while_idle(self):
        on_request_start("req-idle", stage="ocr_started")
        on_heartbeat_update("ocr_started", page=1, total_pages=3)
        time.sleep(0.05)
        age = get_heartbeat_age_seconds()
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0.04)


class ReliabilityLLMCounterTests(unittest.TestCase):
    def tearDown(self):
        on_request_end()

    def test_gpt_stage_paired_transitions(self):
        on_request_start("req-gpt")
        track_llm_stage("openai_request")
        snap = get_enhanced_heartbeat_snapshot()
        self.assertEqual(snap["active_gpt_threads"], 1)
        track_llm_stage("openai_response_received")
        snap = get_enhanced_heartbeat_snapshot()
        self.assertEqual(snap["active_gpt_threads"], 0)

    def test_duplicate_response_does_not_over_decrement(self):
        on_request_start("req-gpt-dup")
        track_llm_stage("openai_request")
        track_llm_stage("openai_response_received")
        track_llm_stage("openai_response_received")
        snap = get_enhanced_heartbeat_snapshot()
        self.assertEqual(snap["active_gpt_threads"], 0)

    def test_gemini_release_on_error_path(self):
        on_request_start("req-gemini")
        track_llm_stage("gemini_text_request")
        self.assertEqual(get_enhanced_heartbeat_snapshot()["active_gemini_threads"], 1)
        release_gemini_inflight_counter()
        self.assertEqual(get_enhanced_heartbeat_snapshot()["active_gemini_threads"], 0)

    def test_request_end_resets_worker_counters(self):
        on_request_start("req-reset")
        increment_gemini_active()
        track_llm_stage("openai_request")
        on_request_end()
        snap = get_enhanced_heartbeat_snapshot()
        self.assertFalse(snap["active"])
        self.assertEqual(snap["active_ocr_threads"], 0)
        self.assertEqual(snap["active_gpt_threads"], 0)
        self.assertEqual(snap["active_gemini_threads"], 0)


class ReliabilityReadyTests(unittest.TestCase):
    def tearDown(self):
        on_request_end()

    def test_ready_true_when_idle(self):
        runtime = {"processing_status": "idle", "waiting_requests": 0}
        body = get_ready_snapshot(runtime)
        self.assertTrue(body["ready"])
        self.assertFalse(body["active_request"])

    def test_ready_false_when_stuck(self):
        on_request_start("req-stuck", stage="ocr_started")
        with patch("services.reliability.get_heartbeat_age_seconds", return_value=9999.0):
            with patch("services.reliability.OCR_WATCHDOG_STUCK_SECONDS", 300):
                body = get_ready_snapshot({"processing_status": "processing"})
        self.assertFalse(body["ready"])


class ReliabilityWatchdogTests(unittest.TestCase):
    def tearDown(self):
        on_request_end()

    def test_should_not_terminate_when_ocr_workers_active(self):
        should, _age = _watchdog_should_terminate(
            active=True,
            last_mono=time.monotonic() - 9999,
            resources={
                "active_ocr_threads": 1,
                "active_gpt_threads": 0,
                "active_gemini_threads": 0,
            },
        )
        self.assertFalse(should)

    def test_should_terminate_when_stale_and_idle(self):
        should, age = _watchdog_should_terminate(
            active=True,
            last_mono=time.monotonic() - 9999,
            resources={
                "active_ocr_threads": 0,
                "active_gpt_threads": 0,
                "active_gemini_threads": 0,
            },
        )
        self.assertTrue(should)
        self.assertGreater(age, OCR_WATCHDOG_STUCK_SECONDS)


class ReliabilityTesseractWrapperTests(unittest.TestCase):
    def test_fast_path_when_flags_disabled(self):
        with patch("services.reliability._tesseract_features_enabled", return_value=False):
            result = run_tesseract_call(lambda: "ok", label="test")
        self.assertEqual(result, "ok")


class ReliabilityConfigTests(unittest.TestCase):
    def test_watchdog_defaults_off(self):
        self.assertFalse(OCR_WATCHDOG_ENABLED)
        self.assertEqual(OCR_WATCHDOG_STUCK_SECONDS, 300)


if __name__ == "__main__":
    unittest.main()
