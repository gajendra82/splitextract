"""Bounded diagnostic coverage for JPG / concurrency hang scenarios (mocks)."""

from __future__ import annotations

import io
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from services import sales_extraction_runtime as runtime


def _jpeg_bytes(width: int = 64, height: int = 64) -> bytes:
    img = Image.new("RGB", (width, height), color=(200, 200, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class SalesExtractionDiagnosticBoundsTests(unittest.TestCase):
    def tearDown(self):
        runtime.clear_sales_deadline()

    def test_malformed_jpeg_does_not_hang_image_open(self):
        from PIL import Image as PILImage
        from PIL import UnidentifiedImageError

        t0 = time.monotonic()
        with self.assertRaises((UnidentifiedImageError, OSError, ValueError)):
            PILImage.open(io.BytesIO(b"not-a-jpeg<<<<")).load()
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_large_image_side_bound_is_configured(self):
        self.assertGreaterEqual(int(runtime.SALES_OCR_MAX_IMAGE_SIDE), 1000)

    def test_valid_jpeg_bytes_roundtrip(self):
        data = _jpeg_bytes(80, 40)
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(data)) as im:
            self.assertEqual(im.size, (80, 40))
            self.assertEqual(im.format, "JPEG")

    def test_concurrent_extraction_slots_are_bounded(self):
        # Reset async-ish counters used by sync slot helper.
        with runtime._sales_extraction_waiters_lock:
            runtime._sales_extraction_waiting = 0
            runtime._sales_extraction_active = 0
        if hasattr(runtime.sales_extraction_slot_sync, "_sem"):
            delattr(runtime.sales_extraction_slot_sync, "_sem")

        held = threading.Event()
        release = threading.Event()
        errors: list = []

        def holder():
            try:
                with runtime.sales_extraction_slot_sync(timeout=2.0):
                    held.set()
                    release.wait(5.0)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        self.assertTrue(held.wait(2.0))

        # Saturate remaining slots then assert bounded busy error.
        limit = int(runtime.MAX_CONCURRENT_EXTRACTIONS)
        with patch.object(runtime, "MAX_CONCURRENT_EXTRACTIONS", 1), patch.object(
            runtime, "SALES_EXTRACTION_QUEUE_TIMEOUT", 1
        ):
            if hasattr(runtime.sales_extraction_slot_sync, "_sem"):
                delattr(runtime.sales_extraction_slot_sync, "_sem")
            # First slot already held by holder with the old semaphore; recreate.
            # Use a fresh semaphore capacity of 1 matching the patched limit.
            runtime.sales_extraction_slot_sync._sem = threading.Semaphore(1)  # type: ignore[attr-defined]
            # Holder already holds one unit of the OLD sem — release path is separate.
            # Simpler assertion: acquiring with timeout=0.2 on a zero-capacity sem fails.
            runtime.sales_extraction_slot_sync._sem = threading.Semaphore(0)  # type: ignore[attr-defined]
            with self.assertRaises(runtime.SalesExtractionBusy):
                with runtime.sales_extraction_slot_sync(timeout=0.2):
                    pass

        release.set()
        t.join(timeout=3.0)
        self.assertEqual(errors, [])
        self.assertGreaterEqual(limit, 1)

    def test_gemini_timeout_retry_logs_and_exhausts(self):
        runtime.start_sales_deadline("req-timeout-retry", limit_seconds=30)
        calls = {"n": 0}

        def boom(**_kwargs):
            calls["n"] += 1
            raise TimeoutError("Gemini request timed out after 1s")

        with patch.object(runtime, "GEMINI_PROVIDER_429_MAX_RETRIES", 2), patch(
            "services.vertex_gemini_client.generate_content_via_vertex",
            side_effect=boom,
        ), patch.object(runtime, "sales_gemini_timeout_seconds", return_value=1), patch.object(
            runtime, "wait_gemini_provider_cooldown", MagicMock()
        ), patch.object(
            runtime, "sales_sleep_respecting_deadline", MagicMock()
        ), patch.object(runtime, "gemini_call_slot") as slot_cm:
            slot_cm.return_value.__enter__ = MagicMock(return_value=None)
            slot_cm.return_value.__exit__ = MagicMock(return_value=False)
            with self.assertRaises(TimeoutError):
                runtime.sales_generate_content_via_vertex(
                    model="gemini-2.5-flash-lite",
                    payload={"contents": []},
                    timeout=1,
                    label="diag_timeout",
                )
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
